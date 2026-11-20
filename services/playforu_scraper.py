import asyncio
import logging
import re
from typing import Dict, Any, List, Optional
import httpx
from bs4 import BeautifulSoup
from cachetools import TTLCache

logger = logging.getLogger("playforu_scraper")

PLAYFORU_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Referer": "https://play4u.org/",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
}

# In-memory caches:
# Page cache: stores the final paginated response for 6 hours (21600 seconds)
page_cache = TTLCache(maxsize=50, ttl=21600)
# IMDb -> TMDB metadata resolution cache: stores resolved media objects for 24 hours (86400 seconds)
imdb_to_tmdb_cache = TTLCache(maxsize=2000, ttl=86400)


async def resolve_imdb_item(
    item: Dict[str, Any],
    client: httpx.AsyncClient,
    tmdb_api_key: Optional[str]
) -> Dict[str, Any]:
    """
    Resolves an item scraped from PlayForU to TMDB metadata using its IMDb ID.
    Caches results to eliminate redundant upstream calls.
    """
    imdb_id = item.get("imdb_id")
    if not imdb_id or not imdb_id.startswith("tt"):
        return {
            "id": f"playforu_{item.get('title', 'item')}",
            "title": item.get("title", ""),
            "name": item.get("title", ""),
            "poster_path": item.get("poster_path", ""),
            "release_date": item.get("year", ""),
            "first_air_date": item.get("year", ""),
            "vote_average": 7.0,
            "vote_count": 0,
            "media_type": item.get("media_type", "movie"),
            "overview": "",
            "imdb_id": imdb_id or "",
            "season_badge": item.get("season", ""),
            "langs": item.get("langs", [])
        }

    # Check cache first
    cached = imdb_to_tmdb_cache.get(imdb_id)
    if cached:
        res = cached.copy()
        res["langs"] = item.get("langs", [])
        res["season_badge"] = item.get("season", "")
        return res

    if not tmdb_api_key:
        fallback = {
            "id": imdb_id,
            "title": item.get("title", ""),
            "name": item.get("title", ""),
            "poster_path": item.get("poster_path", ""),
            "release_date": item.get("year", ""),
            "first_air_date": item.get("year", ""),
            "vote_average": 7.0,
            "vote_count": 0,
            "media_type": item.get("media_type", "movie"),
            "overview": "",
            "imdb_id": imdb_id,
            "season_badge": item.get("season", ""),
            "langs": item.get("langs", [])
        }
        imdb_to_tmdb_cache[imdb_id] = fallback
        return fallback

    try:
        resp = await client.get(
            f"https://api.themoviedb.org/3/find/{imdb_id}",
            params={"api_key": tmdb_api_key, "external_source": "imdb_id"},
            timeout=8.0
        )
        if resp.status_code == 200:
            data = resp.json()
            target_key = "tv_results" if item.get("media_type") == "tv" else "movie_results"
            results = data.get(target_key, [])
            if not results:
                results = data.get("movie_results", []) or data.get("tv_results", [])

            if results:
                match = results[0]
                media_type = "tv" if ("first_air_date" in match or "name" in match) else "movie"
                name_or_title = match.get("title") or match.get("name") or item.get("title", "")
                release_or_air = match.get("release_date") or match.get("first_air_date") or item.get("year", "")

                formatted = {
                    "id": match.get("id"),
                    "title": name_or_title,
                    "name": name_or_title,
                    "poster_path": match.get("poster_path") or item.get("poster_path", ""),
                    "backdrop_path": match.get("backdrop_path"),
                    "release_date": release_or_air,
                    "first_air_date": release_or_air,
                    "vote_average": match.get("vote_average", 7.0),
                    "vote_count": match.get("vote_count", 0),
                    "media_type": match.get("media_type") or media_type,
                    "overview": match.get("overview", ""),
                    "imdb_id": imdb_id,
                    "season_badge": item.get("season", ""),
                    "langs": item.get("langs", []),
                    "is_playforu": True,
                    "from_playforu": True
                }
                imdb_to_tmdb_cache[imdb_id] = formatted
                return formatted
    except Exception as e:
        logger.warning(f"Failed to resolve TMDB for IMDb ID {imdb_id}: {e}")

    fallback = {
        "id": imdb_id,
        "title": item.get("title", ""),
        "name": item.get("title", ""),
        "poster_path": item.get("poster_path", ""),
        "release_date": item.get("year", ""),
        "first_air_date": item.get("year", ""),
        "vote_average": 7.0,
        "vote_count": 0,
        "media_type": item.get("media_type", "movie"),
        "overview": "",
        "imdb_id": imdb_id,
        "season_badge": item.get("season", ""),
        "langs": item.get("langs", []),
        "is_playforu": True,
        "from_playforu": True
    }
    imdb_to_tmdb_cache[imdb_id] = fallback
    return fallback


async def scrape_playforu_latest(
    page: int = 1,
    tmdb_api_key: Optional[str] = None,
    tmdb_client: Optional[httpx.AsyncClient] = None
) -> Dict[str, Any]:
    """
    Scrapes PlayForU latest catalog from https://play4u.org/?cat=latest (with pagination support).
    Returns TMDB-compliant paginated response.
    """
    page_key = f"playforu_latest_{page}"
    cached_page = page_cache.get(page_key)
    if cached_page:
        return cached_page

    target_url = f"https://play4u.org/?cat=latest&page={page}" if page > 1 else "https://play4u.org/?cat=latest"

    should_close_client = tmdb_client is None
    client = tmdb_client or httpx.AsyncClient(timeout=10.0, follow_redirects=True)

    try:
        resp = await client.get(target_url, headers=PLAYFORU_HEADERS)
        if resp.status_code != 200:
            logger.error(f"PlayForU returned HTTP {resp.status_code} for {target_url}")
            return {
                "page": page,
                "results": [],
                "total_pages": 1,
                "total_results": 0
            }

        soup = BeautifulSoup(resp.text, "html.parser")
        cards = soup.select(".poster-card")

        # Determine total pages from pager
        pager_nums = [
            int(a.text.strip())
            for a in soup.select(".pager-pages a.pager-num, .pager-pages span.pager-num")
            if a.text.strip().isdigit()
        ]
        total_pages = max(pager_nums) if pager_nums else 1

        raw_items: List[Dict[str, Any]] = []
        for card in cards:
            a_tag = card.select_one("a.poster-link")
            href = a_tag.get("href", "") if a_tag else ""

            imdb_id = ""
            if "/series/" in href:
                imdb_id = href.split("/series/")[-1].split("/")[0].split("?")[0]
                media_type = "tv"
            elif "/watch/" in href:
                imdb_id = href.split("/watch/")[-1].split("/")[0].split("?")[0]
                media_type = "movie"
            else:
                media_type = "movie"

            img = card.select_one("img")
            img_src = img.get("src", "") if img else ""
            poster_path = ""
            if "/p/" in img_src:
                poster_path = "/" + img_src.split("/p/")[-1].split("/", 1)[-1]
            elif img_src:
                poster_path = img_src

            season_el = card.select_one(".badge-season")
            season_text = season_el.text.strip() if season_el else ""

            data_langs = card.get("data-langs") or ""
            badge_langs = [x.text.strip().upper() for x in card.select(".badge-lang") if x.text.strip()]
            if badge_langs:
                langs = badge_langs
            elif data_langs:
                langs = [l.strip().upper() for l in data_langs.split(",") if l.strip()]
            else:
                langs = []

            name_el = card.select_one(".poster-name")
            fallback_title = name_el.text.strip() if name_el else "Untitled"
            title = card.get("data-title") or fallback_title
            year = card.get("data-year") or ""

            raw_items.append({
                "imdb_id": imdb_id,
                "title": title,
                "year": year,
                "media_type": media_type,
                "poster_path": poster_path,
                "season": season_text,
                "langs": langs
            })

        # Resolve all items in parallel
        tasks = [
            resolve_imdb_item(item, client, tmdb_api_key)
            for item in raw_items
        ]
        resolved_results = await asyncio.gather(*tasks)

        response_data = {
            "page": page,
            "results": list(resolved_results),
            "total_pages": total_pages,
            "total_results": total_pages * len(resolved_results)
        }

        page_cache[page_key] = response_data
        return response_data

    except Exception as e:
        logger.exception(f"Error scraping PlayForU latest page {page}: {e}")
        return {
            "page": page,
            "results": [],
            "total_pages": 1,
            "total_results": 0
        }
    finally:
        if should_close_client:
            await client.aclose()
