import re
import logging
from typing import Dict, List, Optional
import httpx
from bs4 import BeautifulSoup
from cachetools import TTLCache

logger = logging.getLogger("serials_service")

BASE_URL = "https://zdkserials.site"

def strip_base_url(url: str) -> str:
    """Strips the upstream base domain so only clean relative paths are emitted."""
    if not url:
        return ""
    for prefix in (BASE_URL, "https://zdkserials.site", "http://zdkserials.site"):
        if url.startswith(prefix):
            rel = url[len(prefix):]
            return rel if rel.startswith("/") else f"/{rel}"
    return url

def to_absolute_url(url_or_slug: str, default_prefix: str = "") -> str:
    """Normalizes an incoming slug or relative path to a valid upstream URL."""
    if not url_or_slug:
        return ""
    if url_or_slug.startswith("http://") or url_or_slug.startswith("https://"):
        return url_or_slug
    cleaned = url_or_slug.strip().lstrip("/")
    if default_prefix and not cleaned.startswith(default_prefix):
        cleaned = f"{default_prefix}/{cleaned}"
    if not cleaned.endswith("/"):
        cleaned = f"{cleaned}/"
    return f"{BASE_URL}/{cleaned}"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

# In-memory TTL caches: home (2 hours), channels/shows (4 hours), embeds (6 hours)
home_cache = TTLCache(maxsize=10, ttl=2 * 60 * 60)
shelf_cache = TTLCache(maxsize=30, ttl=2 * 60 * 60)
channel_cache = TTLCache(maxsize=100, ttl=4 * 60 * 60)
show_cache = TTLCache(maxsize=300, ttl=4 * 60 * 60)
embed_cache = TTLCache(maxsize=1000, ttl=6 * 60 * 60)

CHANNEL_COLORS = {
    "star-pravah": "#E50914",
    "zee-marathi": "#FF9900",
    "colors-marathi": "#E91E63",
    "sun-marathi": "#FF5722",
    "star-plus": "#D32F2F",
    "colors-tv": "#9C27B0",
    "zee-tv": "#FF9800",
    "sony-tv": "#00BCD4",
    "star-vijay": "#E91E63",
    "sun-tv": "#FF5722",
    "zee-tamil": "#4CAF50",
    "star-maa": "#D32F2F",
    "colors-bangla": "#E91E63",
    "zee-bangla": "#FF9800",
    "colors-kannada": "#E91E63",
    "zee-kannada": "#FF9800",
    "asianet": "#D32F2F",
}


def detect_language(slug: str, name: str) -> str:
    combined = f"{slug} {name}".lower()
    if "marathi" in combined or "pravah" in combined:
        return "Marathi"
    if "tamil" in combined or "vijay" in combined or slug == "sun-tv":
        return "Tamil"
    if "telugu" in combined or "maa" in combined or "gemini" in combined:
        return "Telugu"
    if "bangla" in combined or "bengali" in combined or "jalsha" in combined:
        return "Bengali"
    if "kannada" in combined or "suvarna" in combined or "udaya" in combined:
        return "Kannada"
    if "malayalam" in combined or "asianet" in combined or "asia-net" in combined or "keralam" in combined or "surya" in combined:
        return "Malayalam"
    return "Hindi"


async def fetch_html(url: str, retries: int = 3) -> str:
    headers = {
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://zdkserials.site/",
    }
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
        for attempt in range(1, retries + 1):
            try:
                resp = await client.get(url, headers=headers)
                resp.raise_for_status()
                return resp.text
            except Exception as e:
                logger.warning(f"Fetch attempt {attempt} failed for {url}: {e}")
                if attempt == retries:
                    raise
    return ""


async def get_home_data() -> Dict:
    cache_key = "home_data"
    cached = home_cache.get(cache_key)
    if cached:
        return cached

    html = await fetch_html(f"{BASE_URL}/")
    soup = BeautifulSoup(html, "html.parser")

    # 1. Parse Channels
    channels = []
    seen_channel_slugs = set()
    for a in soup.find_all("a", href=re.compile(r"/channel/[^/]+/")):
        href = a["href"].rstrip("/")
        slug = href.split("/")[-1]
        if slug in seen_channel_slugs:
            continue
        seen_channel_slugs.add(slug)

        name = a.get_text(strip=True)
        img = a.find("img")
        img_src = ""
        if img:
            img_src = img.get("src") or img.get("data-src") or ""

        # Filter out junk text links that aren't channels
        if not name or len(name) < 2:
            continue

        channels.append({
            "id": slug,
            "name": name,
            "slug": slug,
            "url": f"/channel/{slug}/",
            "logoUrl": img_src,
            "color": CHANNEL_COLORS.get(slug, "#E50914"),
            "language": detect_language(slug, name),
        })

    # 2. Parse Shelves
    shelves = []
    # Sections have zdk-section or h2 headers with see-all links
    for head in soup.find_all("div", class_="zdk-row-head"):
        h2 = head.find("h2")
        if not h2:
            continue
        title = h2.get_text(strip=True)

        see_all_a = head.find("a", class_="zdk-seeall")
        see_all = see_all_a["href"] if see_all_a and "href" in see_all_a.attrs else ""

        # Sibling or parent section scroll container
        parent = head.find_parent("div", class_="zdk-section")
        scroll_div = parent.find("div", class_="zdk-scroll") if parent else head.find_next_sibling("div", class_="zdk-scroll")
        if not scroll_div:
            continue

        episodes = []
        for card in scroll_div.find_all("a", class_="zdk-card"):
            ep_url = card.get("href", "")
            img_tag = card.find("img")
            ep_img = ""
            if img_tag:
                ep_img = img_tag.get("src") or img_tag.get("data-src") or ""

            badge_tag = card.find("span", class_="zdk-badge")
            badge = badge_tag.get_text(strip=True) if badge_tag else "Today"

            ttl_tag = card.find("div", class_="zdk-ttl")
            serial_title = ttl_tag.get_text(strip=True) if ttl_tag else ""

            # Robust fallback for serial_title to avoid "Zdkserials.Site" or empty titles
            if not serial_title or "zdkserials" in serial_title.lower():
                show_m = re.search(r"/show/([^/]+)", ep_url)
                if show_m:
                    serial_title = show_m.group(1).replace("-", " ").title()
                else:
                    ep_m = re.search(r"/episode/([^/]+)", ep_url)
                    if ep_m:
                        raw_slug = ep_m.group(1)
                        clean_slug = re.sub(r"-(?:episode|ep|season-\d+)?-?\d+$", "", raw_slug, flags=re.I)
                        serial_title = clean_slug.replace("-", " ").title()

            if (not serial_title or "zdkserials" in serial_title.lower()) and ep_img:
                img_name = ep_img.split("/")[-1].split(".")[0]
                img_name = re.sub(r"-\d+x\d+$", "", img_name)
                serial_title = img_name.replace("-", " ").replace("_", " ").title()

            serial_title = re.sub(r"\s+", " ", serial_title).strip()
            if not serial_title or "zdkserials" in serial_title.lower():
                serial_title = "TV Serial"

            meta_tag = card.find("div", class_="zdk-meta")
            meta = meta_tag.get_text(strip=True) if meta_tag else ""

            channel_name = ""
            ep_num = ""
            if "·" in meta:
                m_parts = meta.split("·")
                channel_name = m_parts[0].strip()
                ep_num = m_parts[1].strip()
            elif re.match(r"^E\d+", meta, re.I):
                ep_num = meta.strip()
                shelf_lang = detect_language(title, title)
                channel_name = f"{shelf_lang} Serial" if shelf_lang != "Other" else "TV Serial"
            else:
                channel_name = meta

            episodes.append({
                "title": serial_title,
                "url": strip_base_url(ep_url),
                "date": badge,
                "serialTitle": serial_title,
                "channelName": channel_name,
                "imageUrl": ep_img,
                "badge": badge,
                "episodeNumber": ep_num,
            })

        if episodes:
            shelves.append({
                "title": title,
                "language": detect_language(title, title),
                "seeAllUrl": strip_base_url(see_all),
                "episodes": episodes,
            })

    # Sort Shelves: Hindi first, then Marathi, then other languages
    LANG_PRIORITY = {
        "hindi": 1,
        "marathi": 2,
        "tamil": 3,
        "telugu": 4,
        "kannada": 5,
        "bengali": 6,
        "malayalam": 7,
    }
    shelves.sort(key=lambda s: LANG_PRIORITY.get(s.get("language", "").lower(), 99))
    channels.sort(key=lambda c: (LANG_PRIORITY.get(c.get("language", "").lower(), 99), c.get("name", "")))

    result = {
        "channels": channels,
        "shelves": shelves,
    }
    home_cache[cache_key] = result
    return result


async def get_language_latest_episodes(language: str) -> List[Dict]:
    lang = language.lower().strip()
    cache_key = f"latest_{lang}"
    cached = shelf_cache.get(cache_key)
    if cached:
        return cached

    url = f"{BASE_URL}/{lang}/latest-episodes/"
    try:
        html = await fetch_html(url)
    except Exception as e:
        logger.error(f"Failed to fetch latest episodes for {lang}: {e}")
        return []

    soup = BeautifulSoup(html, "html.parser")
    episodes = []
    seen = set()

    for card in soup.find_all("a", class_="zdk-card"):
        ep_url = card.get("href", "")
        if not ep_url or ep_url in seen:
            continue
        seen.add(ep_url)

        img_tag = card.find("img")
        ep_img = (img_tag.get("src") or img_tag.get("data-src") or "") if img_tag else ""
        badge_tag = card.find("span", class_="zdk-badge")
        badge = badge_tag.get_text(strip=True) if badge_tag else "Today"

        ttl_tag = card.find("div", class_="zdk-ttl")
        serial_title = ttl_tag.get_text(strip=True) if ttl_tag else ""

        if not serial_title or "zdkserials" in serial_title.lower():
            show_m = re.search(r"/show/([^/]+)", ep_url)
            if show_m:
                serial_title = show_m.group(1).replace("-", " ").title()
            else:
                ep_m = re.search(r"/episode/([^/]+)", ep_url)
                if ep_m:
                    raw_slug = ep_m.group(1)
                    clean_slug = re.sub(r"-(?:episode|ep|season-\d+)?-?\d+$", "", raw_slug, flags=re.I)
                    serial_title = clean_slug.replace("-", " ").title()

        if (not serial_title or "zdkserials" in serial_title.lower()) and ep_img:
            img_name = ep_img.split("/")[-1].split(".")[0]
            img_name = re.sub(r"-\d+x\d+$", "", img_name)
            serial_title = img_name.replace("-", " ").replace("_", " ").title()

        serial_title = re.sub(r"\s+", " ", serial_title).strip()
        if not serial_title or "zdkserials" in serial_title.lower():
            serial_title = "TV Serial"

        meta_tag = card.find("div", class_="zdk-meta")
        meta = meta_tag.get_text(strip=True) if meta_tag else ""
        channel_name = ""
        ep_num = ""
        if "·" in meta:
            m_parts = meta.split("·")
            channel_name = m_parts[0].strip()
            ep_num = m_parts[1].strip()
        elif re.match(r"^E\d+", meta, re.I):
            ep_num = meta.strip()
            channel_name = f"{language.title()} Serial"
        else:
            channel_name = meta

        episodes.append({
            "title": serial_title,
            "url": strip_base_url(ep_url),
            "date": badge,
            "serialTitle": serial_title,
            "channelName": channel_name,
            "imageUrl": ep_img,
            "badge": badge,
            "episodeNumber": ep_num,
        })

    shelf_cache[cache_key] = episodes
    return episodes


async def get_channel_shows(channel_slug: str) -> List[Dict]:
    cached = channel_cache.get(channel_slug)
    if cached:
        return cached

    url = f"{BASE_URL}/channel/{channel_slug}/"
    html = await fetch_html(url)
    soup = BeautifulSoup(html, "html.parser")

    shows = []
    seen = set()

    for a in soup.find_all("a", href=re.compile(r"/show/[^/]+/?$")):
        href = a["href"].rstrip("/")
        slug = href.split("/")[-1]
        if slug in seen:
            continue
        seen.add(slug)

        title = a.get_text(separator=" ", strip=True)
        img = a.find("img")
        img_src = ""
        if img:
            img_src = img.get("src") or img.get("data-src") or ""

        # Clean title: strip out concatenated language/channel tags like "Marathi · Colors Marathi"
        title = re.sub(r"\s*(Marathi|Hindi|Tamil|Telugu|Bengali|Kannada|Malayalam)\s*[·•-].*$", "", title, flags=re.IGNORECASE).strip()

        if not title:
            title = slug.replace("-", " ").title()

        shows.append({
            "title": title,
            "url": strip_base_url(a["href"]),
            "slug": slug,
            "imageUrl": img_src,
        })

    channel_cache[channel_slug] = shows
    return shows


def _parse_episode_card(a, href: str, fallback_img: str = "") -> Optional[Dict]:
    raw_title = a.get_text(separator=" ", strip=True)
    if not raw_title or raw_title.lower() == "watch now":
        return None

    img = a.find("img")
    img_src = (img.get("src") or img.get("data-src") if img else "") or fallback_img

    n_div = a.find("div", class_="n")
    m_div = a.find("div", class_="m")
    ep_date = ""
    clean_title = ""

    if m_div:
        ep_date = m_div.get_text(strip=True)

    if n_div:
        for badge in n_div.find_all(class_=re.compile(r"new", re.I)):
            badge.decompose()
        clean_title = n_div.get_text(separator=" ", strip=True)

    if not clean_title:
        clean_title = re.sub(r"\bNEW\b", "", raw_title, flags=re.IGNORECASE).strip()
        clean_title = re.sub(r"\s+", " ", clean_title)

    if not ep_date:
        date_match = re.search(r"(\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*(?:\s+\d{2,4})?)", clean_title, flags=re.IGNORECASE)
        if date_match:
            ep_date = date_match.group(1)

    if ep_date and ep_date in clean_title:
        clean_title = clean_title.replace(ep_date, "").strip()

    clean_title = re.sub(r"\s+", " ", clean_title).strip().rstrip("-").strip()

    ep_match = re.search(r"season-(\d+)/(\d+)/watch", href)
    if ep_match:
        ep_num = f"E{ep_match.group(2)}"
        default_title = f"Episode {ep_match.group(2)}"
    else:
        ep_num = ""
        default_title = "Watch Episode"

    title = clean_title if clean_title else default_title

    return {
        "title": title,
        "date": ep_date,
        "url": strip_base_url(href),
        "imageUrl": img_src,
        "episodeNumber": ep_num,
    }


async def get_show_episodes(show_url: str, offset: int = 0, season: str = "1") -> Dict:
    abs_show_url = to_absolute_url(show_url, "show")
    cache_key = f"{abs_show_url}_s_{season}_offset_{offset}"
    cached = show_cache.get(cache_key)
    if cached:
        return cached

    episodes = []
    has_more = False
    detected_season = season

    if offset == 0:
        html = await fetch_html(abs_show_url)
        soup = BeautifulSoup(html, "html.parser")
        seen = set()

        for a in soup.find_all("a", href=re.compile(r"/(show/[^/]+/season-[^/]+/\d+/watch|episode/[^/]+)/?")):
            href = a["href"]
            if href in seen:
                continue

            parsed = _parse_episode_card(a, href)
            if parsed:
                seen.add(href)
                episodes.append(parsed)
                if not detected_season or detected_season == "1":
                    s_m = re.search(r"season-(\d+)/", href)
                    if s_m:
                        detected_season = s_m.group(1)

        # Check if page has zdk-load-more button
        has_more = bool(soup.find("button", id="zdk-load-more") or soup.find("div", class_="zdk-loadmore") or len(episodes) >= 40)
    else:
        # Load next batch via WordPress AJAX endpoint
        slug = abs_show_url.rstrip("/").split("/")[-1]
        data = {
            "action": "zdk_load_more",
            "show": slug,
            "season": str(season or "1"),
            "offset": str(offset),
            "order": "desc",
        }
        headers = {
            "User-Agent": DEFAULT_USER_AGENT,
            "Referer": abs_show_url,
            "X-Requested-With": "XMLHttpRequest",
        }
        try:
            async with httpx.AsyncClient(timeout=12.0) as client:
                resp = await client.post(f"{BASE_URL}/wp-admin/admin-ajax.php", data=data, headers=headers)
                if resp.status_code == 200:
                    j = resp.json()
                    has_more = bool(j.get("more", False))
                    soup = BeautifulSoup(j.get("html", ""), "html.parser")
                    seen = set()
                    for a in soup.find_all("a", href=re.compile(r"/watch")):
                        href = a["href"]
                        if href in seen:
                            continue
                        parsed = _parse_episode_card(a, href)
                        if parsed:
                            seen.add(href)
                            episodes.append(parsed)
        except Exception as e:
            logger.error(f"Failed to load more episodes for {abs_show_url} offset {offset}: {e}")

    result = {
        "episodes": episodes,
        "has_more": has_more,
        "next_offset": (offset + len(episodes)) if has_more else None,
        "season": detected_season or season or "1",
    }
    show_cache[cache_key] = result
    return result


async def resolve_episode_embed(watch_url: str) -> Dict:
    abs_watch_url = to_absolute_url(watch_url)
    cached = embed_cache.get(abs_watch_url)
    if cached:
        return cached

    html = await fetch_html(abs_watch_url)

    # Search for iframe src
    iframe_matches = re.findall(r'<iframe[^>]+src=["\']([^"\']+)["\']', html, re.IGNORECASE)
    embed_url = ""
    server = "unknown"

    if iframe_matches:
        raw_src = iframe_matches[0].strip()
        embed_url = raw_src.replace("&amp;", "&").replace("&#038;", "&")
        if "upbolt" in embed_url:
            server = "upbolt"
        elif "ok.ru" in embed_url:
            server = "okru"
        elif "streamtape" in embed_url:
            server = "streamtape"

    result = {
        "watch_url": strip_base_url(abs_watch_url),
        "embed_url": embed_url,
        "server": server,
    }
    if embed_url:
        embed_cache[abs_watch_url] = result

    return result
