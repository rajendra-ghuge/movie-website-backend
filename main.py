import os
import re
import asyncio
import json
import logging
from datetime import date

import httpx
import psycopg2
from cachetools import TTLCache
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.extension import _rate_limit_exceeded_handler
from slowapi.util import get_remote_address


load_dotenv()

logging.basicConfig(
    level=logging.ERROR,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

# Silence noisy libraries and access logs
logging.getLogger("httpx").setLevel(logging.ERROR)
logging.getLogger("httpcore").setLevel(logging.ERROR)
logging.getLogger("uvicorn.access").setLevel(logging.ERROR)

# Configuration
TMDB_API_KEY = os.getenv("TMDB_API_KEY")
TMDB_BASE_URL = os.getenv("TMDB_BASE_URL", "https://api.themoviedb.org/3")
TMDB_IMAGE_BASE_URL = os.getenv("TMDB_IMAGE_BASE_URL", "https://image.tmdb.org/t/p")
CORS_ORIGINS = os.getenv(
    "CORS_ORIGINS",
    "http://localhost:5173,http://127.0.0.1:5173,http://localhost:5174,http://127.0.0.1:5174,http://localhost:3000,http://127.0.0.1:3000"
).split(",")
CORS_ORIGINS = [origin.strip() for origin in CORS_ORIGINS if origin.strip()]
CORS_ALLOW_ALL = "*" in CORS_ORIGINS
REQUEST_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
IMAGE_TIMEOUT = httpx.Timeout(20.0, connect=10.0)
DATABASE_URL = os.getenv("DATABASE_URL")
json_cache = TTLCache(maxsize=300, ttl=300)
tmdb_client: httpx.AsyncClient | None = None
image_client: httpx.AsyncClient | None = None

def get_image_client() -> httpx.AsyncClient:
    global image_client
    if image_client is None or image_client.is_closed:
        limits = httpx.Limits(max_keepalive_connections=100, max_connections=200, keepalive_expiry=120.0)
        image_client = httpx.AsyncClient(
            limits=limits,
            timeout=IMAGE_TIMEOUT
        )
    return image_client

app = FastAPI(title="movie website  Backend")


@app.on_event("startup")
async def startup_event():
    logging.getLogger("uvicorn.access").setLevel(logging.ERROR)
    logging.getLogger("httpx").setLevel(logging.ERROR)
    logging.getLogger("httpcore").setLevel(logging.ERROR)
    global tmdb_client, image_client
    tmdb_client = httpx.AsyncClient(timeout=REQUEST_TIMEOUT)
    limits = httpx.Limits(max_keepalive_connections=100, max_connections=200, keepalive_expiry=120.0)
    image_client = httpx.AsyncClient(
        limits=limits,
        timeout=IMAGE_TIMEOUT
    )


@app.on_event("shutdown")
async def shutdown_event():
    if tmdb_client:
        await tmdb_client.aclose()
    if image_client:
        await image_client.aclose()

# Rate Limiting
limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if CORS_ALLOW_ALL else CORS_ORIGINS,
    allow_credentials=not CORS_ALLOW_ALL,
    allow_methods=["*"],
    allow_headers=["*"],
)



def make_cache_key(path: str, params: dict):
    return path, tuple(sorted((str(key), str(value)) for key, value in params.items()))


async def get_tmdb_data(path: str, params: dict = None):
    if params is None:
        params = {}
    if "certification_country" not in params:
        params["certification_country"] = "IN"
    params["api_key"] = TMDB_API_KEY

    cache_key = make_cache_key(path, params)
    cached = json_cache.get(cache_key)
    if cached:
        return Response(
            content=cached,
            media_type="application/json",
            headers={"Cache-Control": "public, max-age=300"}
        )

    try:
        should_close_client = tmdb_client is None
        client = tmdb_client or httpx.AsyncClient(timeout=REQUEST_TIMEOUT)
        response = await client.get(f"{TMDB_BASE_URL}/{path}", params=params)
        response.raise_for_status()
        data = response.content
        json_cache[cache_key] = data
        return Response(
            content=data,
            media_type="application/json",
            headers={"Cache-Control": "public, max-age=300"}
        )
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code, detail="Upstream request failed")
    except Exception:
        logging.exception("TMDB request failed")
        raise HTTPException(status_code=500, detail="Backend request failed")
    finally:
        if 'client' in locals() and should_close_client:
            await client.aclose()

@app.get("/proxy/movie/now_playing")
async def get_now_playing_movies(request: Request):
    params = dict(request.query_params)
    return await get_tmdb_data("movie/now_playing", params)

@app.get("/proxy/movie/{movie_id}")
async def get_movie_details(movie_id: int, request: Request):
    params = dict(request.query_params)
    return await get_tmdb_data(f"movie/{movie_id}", params)

@app.get("/proxy/movie/{movie_id}/videos")
async def get_movie_videos(movie_id: int):
    return await get_tmdb_data(f"movie/{movie_id}/videos")

@app.get("/proxy/movie/{movie_id}/credits")
async def get_movie_credits(movie_id: int):
    return await get_tmdb_data(f"movie/{movie_id}/credits")

@app.get("/proxy/movie/{movie_id}/similar")
async def get_movie_similar(movie_id: int, request: Request):
    params = dict(request.query_params)
    return await get_tmdb_data(f"movie/{movie_id}/similar", params)

@app.get("/proxy/movie/{movie_id}/recommendations")
async def get_movie_recommendations(movie_id: int, request: Request):
    params = dict(request.query_params)
    return await get_tmdb_data(f"movie/{movie_id}/recommendations", params)

@app.get("/proxy/tv/{tv_id}")
async def get_tv_details(tv_id: int, request: Request):
    params = dict(request.query_params)
    return await get_tmdb_data(f"tv/{tv_id}", params)

@app.get("/proxy/tv/{tv_id}/recommendations")
async def get_tv_recommendations(tv_id: int, request: Request):
    params = dict(request.query_params)
    return await get_tmdb_data(f"tv/{tv_id}/recommendations", params)

@app.get("/proxy/tv/{tv_id}/season/{season_number}")
async def get_tv_season_details(tv_id: int, season_number: int, request: Request):
    params = dict(request.query_params)
    return await get_tmdb_data(f"tv/{tv_id}/season/{season_number}", params)

@app.get("/proxy/movie/{movie_id}/keywords")
async def get_movie_keywords(movie_id: int):
    return await get_tmdb_data(f"movie/{movie_id}/keywords")

@app.get("/proxy/tv/{tv_id}/keywords")
async def get_tv_keywords(tv_id: int):
    return await get_tmdb_data(f"tv/{tv_id}/keywords")

@app.get("/proxy/genre/{media_type}/list")
async def get_genres(media_type: str, request: Request):
    if media_type not in {"movie", "tv"}:
        raise HTTPException(status_code=400, detail="media_type must be 'movie' or 'tv'")

    params = dict(request.query_params)
    return await get_tmdb_data(f"genre/{media_type}/list", params)

@app.get("/proxy/keyword/{keyword_id}/movies")
async def get_keyword_movies(keyword_id: int, request: Request):
    params = dict(request.query_params)
    return await get_tmdb_data(f"keyword/{keyword_id}/movies", params)


@app.get("/proxy/discover/movie")
async def discover_movies(request: Request):
    params = dict(request.query_params)
    
    # Quality filters to avoid "garbage" results
    if "include_adult" not in params:
        params["include_adult"] = "false"
    if "sort_by" not in params:
        params["sort_by"] = "popularity.desc"

    return await get_tmdb_data("discover/movie", params)

@app.get("/proxy/discover/tv")
async def discover_tv(request: Request):
    params = dict(request.query_params)
    if "sort_by" not in params:
        params["sort_by"] = "first_air_date.desc"
    return await get_tmdb_data("discover/tv", params)

@app.get("/proxy/discover/both")
async def discover_both(request: Request):
    params = dict(request.query_params)
    if "certification_country" not in params:
        params["certification_country"] = "IN"
    params["api_key"] = TMDB_API_KEY
    
    # Create specific params for movie and tv to handle different naming conventions
    movie_params = params.copy()
    tv_params = params.copy()
    movie_sort_by = movie_params.pop("movie_sort_by", None)
    tv_sort_by = tv_params.pop("tv_sort_by", None)
    movie_params.pop("tv_sort_by", None)
    tv_params.pop("movie_sort_by", None)
    
    # Handle release date limits
    if "primary_release_date.lte" in params and "first_air_date.lte" not in params:
        tv_params["first_air_date.lte"] = params["primary_release_date.lte"]
    if "first_air_date.lte" in params and "primary_release_date.lte" not in params:
        movie_params["primary_release_date.lte"] = params["first_air_date.lte"]
        
    # Handle sorting
    if params.get("sort_by") == "primary_release_date.desc":
        tv_params["sort_by"] = "first_air_date.desc"
    elif params.get("sort_by") == "first_air_date.desc":
        movie_params["sort_by"] = "primary_release_date.desc"
    # Allow popularity.desc for TV shows so top-rated web series appear instead of daily serial air dates

    if movie_sort_by:
        movie_params["sort_by"] = movie_sort_by
    if tv_sort_by:
        tv_params["sort_by"] = tv_sort_by
        
    # with_release_type is movie specific, TV discovery uses different filters
    if "with_release_type" in tv_params:
        del tv_params["with_release_type"]

    # Filter out low-vote daily soaps/serials from TV shows while keeping new movies intact
    if "vote_count.gte" not in params:
        tv_params["vote_count.gte"] = 15
    if "without_genres" not in params:
        tv_params["without_genres"] = "10766,10764,10767,10763"

    cache_key = make_cache_key("discover/both", params)
    cached = json_cache.get(cache_key)
    if cached:
        return Response(
            content=cached,
            media_type="application/json",
            headers={"Cache-Control": "public, max-age=300"}
        )

    try:
        should_close_client = tmdb_client is None
        client = tmdb_client or httpx.AsyncClient(timeout=REQUEST_TIMEOUT)
        # If filtering by cast, use person's tv_credits for TV
        # (discover/tv does NOT support with_cast - it ignores it)
        if "with_cast" in params:
            person_id = params["with_cast"]
            
            movie_task = client.get(f"{TMDB_BASE_URL}/discover/movie", params=movie_params)
            tv_task = client.get(f"{TMDB_BASE_URL}/person/{person_id}/tv_credits", params={"api_key": TMDB_API_KEY})

            movie_res, tv_res = await asyncio.gather(movie_task, tv_task)
            movie_res.raise_for_status()
            tv_res.raise_for_status()
            
            movie_data = movie_res.json()
            tv_data = tv_res.json()

            # Get actual TV credits, filter for quality, sort by popularity
            tv_results = [r for r in tv_data.get("cast", []) if r.get("poster_path")]
            tv_results.sort(key=lambda x: x.get("popularity", 0), reverse=True)
            tv_results = tv_results[:20]

            combined_results = movie_data.get("results", []) + tv_results

            merged_data = {
                "page": movie_data.get("page", 1),
                "results": combined_results,
                "total_results": movie_data.get("total_results", 0) + len(tv_results),
                "total_pages": movie_data.get("total_pages", 0)
            }
            data = json.dumps(merged_data)
            json_cache[cache_key] = data
            return Response(content=data, media_type="application/json", headers={"Cache-Control": "public, max-age=300"})

        # Standard both discovery (no cast filter)
        movie_task = client.get(f"{TMDB_BASE_URL}/discover/movie", params=movie_params)
        tv_task = client.get(f"{TMDB_BASE_URL}/discover/tv", params=tv_params)

        movie_res, tv_res = await asyncio.gather(movie_task, tv_task)
        movie_res.raise_for_status()
        tv_res.raise_for_status()

        movie_data = movie_res.json()
        tv_data = tv_res.json()

        combined_results = movie_data.get("results", []) + tv_data.get("results", [])

        merged_data = {
            "page": movie_data.get("page", 1),
            "results": combined_results,
            "total_results": movie_data.get("total_results", 0) + tv_data.get("total_results", 0),
            "total_pages": max(movie_data.get("total_pages", 0), tv_data.get("total_pages", 0))
        }

        data = json.dumps(merged_data)
        json_cache[cache_key] = data
        return Response(content=data, media_type="application/json", headers={"Cache-Control": "public, max-age=300"})
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code, detail="Upstream request failed")
    except Exception:
        logging.exception("Combined discovery failed")
        raise HTTPException(status_code=500, detail="Backend request failed")
    finally:
        if 'client' in locals() and should_close_client:
            await client.aclose()

@app.get("/proxy/search/multi")
async def search_multi(request: Request):
    params = dict(request.query_params)
    return await get_tmdb_data("search/multi", params)

@app.get("/proxy/find/{external_id}")
async def find_by_external_id(external_id: str, request: Request):
    """Finds TMDB media by external ID (e.g. IMDb ID tt0111161)."""
    params = dict(request.query_params)
    params.setdefault("external_source", "imdb_id")
    return await get_tmdb_data(f"find/{external_id}", params)

@app.get("/convert/imdb/{imdb_id}")
async def convert_imdb_to_tmdb(imdb_id: str, season: int = 1, episode: int = 1):
    """Converts IMDb ID to TMDB ID and returns the corresponding Play4U iframe embed URL."""
    res = await get_tmdb_data(f"find/{imdb_id}", {"external_source": "imdb_id"})
    if hasattr(res, "body"):
        data = json.loads(res.body.decode("utf-8"))
        movie_results = data.get("movie_results", [])
        if movie_results:
            m = movie_results[0]
            tmdb_id = m.get("id")
            return {
                "success": True,
                "media_type": "movie",
                "imdb_id": imdb_id,
                "tmdb_id": tmdb_id,
                "title": m.get("title"),
                "embed_url": f"https://play4u.org/watch/{imdb_id}?embed=1"
            }
        tv_results = data.get("tv_results", [])
        if tv_results:
            tv = tv_results[0]
            tmdb_id = tv.get("id")
            s_pad = f"{int(season):02d}"
            ep_pad = f"{int(episode):02d}"
            return {
                "success": True,
                "media_type": "tv",
                "imdb_id": imdb_id,
                "tmdb_id": tmdb_id,
                "title": tv.get("name"),
                "season": season,
                "episode": episode,
                "embed_url": f"https://play4u.org/series/{imdb_id}/s-{s_pad}/ep-{ep_pad}?embed=1"
            }
    raise HTTPException(status_code=404, detail="TMDB ID not found for given IMDb ID")

@app.get("/proxy/trending/{media_type}/{time_window}")
async def get_trending(media_type: str, time_window: str, request: Request):
    params = dict(request.query_params)
    return await get_tmdb_data(f"trending/{media_type}/{time_window}", params)

@app.get("/proxy/image/{size}/{path:path}")
async def proxy_image(size: str, path: str):
    image_url = f"{TMDB_IMAGE_BASE_URL}/{size}/{path}"
    
    async def image_streamer():
        try:
            client = get_image_client()
            async with client.stream("GET", image_url) as response:
                response.raise_for_status()
                async for chunk in response.aiter_bytes(chunk_size=8192):
                    yield chunk
        except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.ConnectError) as e:
            logging.error(f"Image stream timeout for {path}: {e}")
        except Exception as e:
            logging.error(f"Image stream error for {path}: {e}")
            
    return StreamingResponse(
        image_streamer(), 
        media_type="image/jpeg", 
        headers={"Cache-Control": "public, max-age=31536000"}
    )

# ==============================================================================
# Direct Stream Resolvers (Play4U Embed for S0)
# ==============================================================================

PLAYDRIVE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Referer": "https://play4u.org/",
    "Accept": "application/json, text/html, */*"
}

async def get_movie_stream(tmdb_id: str) -> dict | None:
    """
    Checks if Movie is available on Play4U by TMDB ID, and returns the direct Play4U embed URL.
    """
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            # 1. Search Play4U by TMDB ID
            search_resp = await client.get(
                f"https://play4u.org/api/search.php?q={tmdb_id}&per_page=5", 
                headers=PLAYDRIVE_HEADERS
            )
            item = None
            if search_resp.status_code == 200:
                for it in search_resp.json().get("items", []):
                    if str(it.get("tmdb_id")) == str(tmdb_id):
                        item = it
                        break
                if not item and search_resp.json().get("items"):
                    item = search_resp.json()["items"][0]

            # Fallback to TMDB details if not matched directly
            if not item:
                try:
                    tmdb_resp = await get_tmdb_data(f"movie/{tmdb_id}")
                    if tmdb_resp and hasattr(tmdb_resp, 'body'):
                        meta = json.loads(tmdb_resp.body.decode('utf-8'))
                        imdb_id_tmdb = meta.get("imdb_id")
                        title_tmdb = meta.get("title")
                        if imdb_id_tmdb:
                            search_resp = await client.get(
                                f"https://play4u.org/api/search.php?q={imdb_id_tmdb}&per_page=5", 
                                headers=PLAYDRIVE_HEADERS
                            )
                            if search_resp.status_code == 200 and search_resp.json().get("items"):
                                item = search_resp.json()["items"][0]
                        if not item and title_tmdb:
                            clean_title = re.sub(r'[^a-zA-Z0-9\s]', ' ', title_tmdb).strip()
                            search_resp = await client.get(
                                f"https://play4u.org/api/search.php?q={clean_title}&per_page=5", 
                                headers=PLAYDRIVE_HEADERS
                            )
                            if search_resp.status_code == 200 and search_resp.json().get("items"):
                                item = search_resp.json()["items"][0]
                except Exception as e:
                    logging.error(f"Error querying TMDB fallback for movie {tmdb_id}: {e}")

            if not item:
                return None

            imdb_id = item.get("imdb_id")
            target_url = f"https://play4u.org/watch/{imdb_id}" if imdb_id else f"https://play4u.org{item.get('watch')}"

            # Verify watch page is live on Play4U
            page_resp = await client.get(target_url, headers=PLAYDRIVE_HEADERS)
            if page_resp.status_code != 200:
                return None

            embed_target = f"{target_url}?embed=1"
            return {
                "success": True,
                "media_type": "movie",
                "tmdb_id": tmdb_id,
                "imdb_id": imdb_id,
                "title": item.get("title"),
                "embed_url": embed_target,
                "stream_url": embed_target
            }
    except Exception as e:
        logging.error(f"Error resolving movie stream for TMDB ID {tmdb_id}: {e}")
        return None


async def get_tv_stream(tmdb_id: str, season: int = 1, episode: int = 1) -> dict | None:
    """
    Checks if TV Episode is available on Play4U by TMDB ID, and returns the direct Play4U embed URL.
    """
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            # 1. Search Play4U by TMDB ID
            search_resp = await client.get(
                f"https://play4u.org/api/search.php?q={tmdb_id}&per_page=5", 
                headers=PLAYDRIVE_HEADERS
            )
            item = None
            if search_resp.status_code == 200:
                for it in search_resp.json().get("items", []):
                    if str(it.get("tmdb_id")) == str(tmdb_id):
                        item = it
                        break
                if not item and search_resp.json().get("items"):
                    item = search_resp.json()["items"][0]

            # Fallback to TMDB external_ids if not matched directly
            if not item:
                try:
                    ext_resp = await get_tmdb_data(f"tv/{tmdb_id}/external_ids")
                    imdb_id_tmdb = None
                    if ext_resp and hasattr(ext_resp, 'body'):
                        meta = json.loads(ext_resp.body.decode('utf-8'))
                        imdb_id_tmdb = meta.get("imdb_id")
                    if imdb_id_tmdb:
                        search_resp = await client.get(
                            f"https://play4u.org/api/search.php?q={imdb_id_tmdb}&per_page=5", 
                            headers=PLAYDRIVE_HEADERS
                        )
                        if search_resp.status_code == 200 and search_resp.json().get("items"):
                            item = search_resp.json()["items"][0]
                except Exception as e:
                    logging.error(f"Error querying TMDB fallback for TV {tmdb_id}: {e}")

            if not item:
                return None

            # Check available seasons/episodes from metadata
            if item.get("series_season"):
                try:
                    max_season = int(item["series_season"])
                    if int(season) > max_season:
                        return None
                except Exception:
                    pass

            if item.get("series_episode") and item.get("series_season"):
                try:
                    max_season = int(item["series_season"])
                    max_ep = int(item["series_episode"])
                    if int(season) == max_season and int(episode) > max_ep:
                        return None
                except Exception:
                    pass

            imdb_id = item.get("imdb_id")
            s_pad = f"{int(season):02d}"
            ep_pad = f"{int(episode):02d}"
            target_url = f"https://play4u.org/series/{imdb_id}/s-{s_pad}/ep-{ep_pad}" if imdb_id else f"https://play4u.org{item.get('watch').rstrip('/')}/s-{s_pad}/ep-{ep_pad}"

            # Verify series episode page is live on Play4U
            page_resp = await client.get(target_url, headers=PLAYDRIVE_HEADERS)
            if page_resp.status_code != 200:
                return None

            # Strict check: Play4U redirects non-existent seasons back to s-01/ep-01?cont=1
            if f"s-{s_pad}/ep-{ep_pad}" not in str(page_resp.url) or "cont=1" in str(page_resp.url):
                return None

            embed_target = f"{target_url}?embed=1"
            return {
                "success": True,
                "media_type": "tv",
                "tmdb_id": tmdb_id,
                "imdb_id": imdb_id,
                "season": season,
                "episode": episode,
                "title": item.get("title"),
                "embed_url": embed_target,
                "stream_url": embed_target
            }
    except Exception as e:
        logging.error(f"Error resolving TV stream for TMDB ID {tmdb_id} S{season}E{episode}: {e}")
        return None


@app.get("/proxy/stream/{media_type}/{tmdb_id}")
@app.get("/stream/{media_type}/{tmdb_id}")
async def resolve_stream_endpoint(
    media_type: str,
    tmdb_id: str,
    season: int = 1,
    episode: int = 1,
    request: Request = None
):
    """
    Main S0 stream resolver for Movies and TV Series.
    Uses get_movie_stream and get_tv_stream directly in main.py.
    """
    if media_type == "movie":
        res = await get_movie_stream(tmdb_id)
    else:
        res = await get_tv_stream(tmdb_id, season=season or 1, episode=episode or 1)

    if not res or not res.get("embed_url"):
        return {
            "success": False,
            "message": "Play4U stream not available. Switching to backup server...",
            "fallback_server": 1
        }

    embed_url = res["embed_url"]
    return {
        "success": True,
        "media_type": media_type,
        "tmdb_id": tmdb_id,
        "season": season,
        "episode": episode,
        "title": res.get("title"),
        "source_id": "play4u",
        "source_label": "S0",
        "available_sources": ["play4u"],
        "embed_url": embed_url,
        "stream_url": embed_url
    }


@app.get("/health")
async def health_check():
    return {"status": "ok"}


def get_client_ip(request: Request) -> str:
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()

    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        return real_ip.strip()

    return request.client.host if request.client else ""


def save_visit_to_db(uid: str, visit_date: date, ip_address: str):
    if not DATABASE_URL:
        logging.error("Analytics Database error: DATABASE_URL is not configured")
        return

    conn = None
    cur = None
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        
        cur.execute("""
            INSERT INTO daily_visits (uid, visit_date, ip_address, last_visited_at) 
            VALUES (%s, %s, %s, CURRENT_TIMESTAMP) 
            ON CONFLICT (uid, visit_date) 
            DO UPDATE SET
                ip_address = EXCLUDED.ip_address,
                last_visited_at = CURRENT_TIMESTAMP;
        """, (uid, visit_date, ip_address))
        
        conn.commit()
    except Exception as e:
        logging.error(f"Analytics Database error: {e}")
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

@app.get("/proxy/stats/visit")
async def record_visit(uid: str, request: Request, background_tasks: BackgroundTasks):
    background_tasks.add_task(save_visit_to_db, uid, date.today(), get_client_ip(request))
    return {"status": "ok", "message": "Visit recorded"}

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
