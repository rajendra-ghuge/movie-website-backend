import os
import asyncio
import json
import logging
from typing import Any
from fastapi import FastAPI, HTTPException, Request, BackgroundTasks
from fastapi.responses import Response, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import httpx
from dotenv import load_dotenv
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from datetime import date
import psycopg2
from cachetools import TTLCache

load_dotenv()

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
IMAGE_TIMEOUT = httpx.Timeout(20.0, connect=5.0)
DOWNLOAD_API_BASE_URL = os.getenv("DOWNLOAD_API_BASE_URL", "https://example.com/api").rstrip("/")
json_cache = TTLCache(maxsize=300, ttl=300)
tmdb_client: httpx.AsyncClient | None = None

app = FastAPI(title="movie website  Backend")


class DownloadLinksRequest(BaseModel):
    tmdbId: int
    type: str
    season: int | None = None
    episode: int | None = None
    title: str | None = None


@app.on_event("startup")
async def startup_event():
    global tmdb_client
    tmdb_client = httpx.AsyncClient(timeout=REQUEST_TIMEOUT)


@app.on_event("shutdown")
async def shutdown_event():
    if tmdb_client:
        await tmdb_client.aclose()

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

    # Handle certification for India if present
    cert_params = ["certification", "certification.lte", "certification.gte"]
    if any(p in params for p in cert_params):
        if "certification_country" not in params:
            params["certification_country"] = "IN"
            
    return await get_tmdb_data("discover/movie", params)

@app.get("/proxy/discover/tv")
async def discover_tv(request: Request):
    params = dict(request.query_params)
    return await get_tmdb_data("discover/tv", params)

@app.get("/proxy/discover/both")
async def discover_both(request: Request):
    params = dict(request.query_params)
    params["api_key"] = TMDB_API_KEY
    
    # Create specific params for movie and tv to handle different naming conventions
    movie_params = params.copy()
    tv_params = params.copy()
    
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
        
    # with_release_type is movie specific, TV discovery uses different filters
    if "with_release_type" in tv_params:
        del tv_params["with_release_type"]

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

@app.get("/proxy/trending/{media_type}/{time_window}")
async def get_trending(media_type: str, time_window: str, request: Request):
    params = dict(request.query_params)
    return await get_tmdb_data(f"trending/{media_type}/{time_window}", params)

@app.get("/proxy/image/{size}/{path:path}")
async def proxy_image(size: str, path: str):
    image_url = f"{TMDB_IMAGE_BASE_URL}/{size}/{path}"
    
    async def image_streamer():
        try:
            async with httpx.AsyncClient(timeout=IMAGE_TIMEOUT) as client:
                async with client.stream("GET", image_url) as response:
                    response.raise_for_status()
                    async for chunk in response.aiter_bytes(chunk_size=8192):
                        yield chunk
        except Exception:
            logging.exception("Image stream error")
            
    return StreamingResponse(
        image_streamer(), 
        media_type="image/jpeg", 
        headers={"Cache-Control": "public, max-age=31536000"}
    )

@app.get("/health")
async def health_check():
    return {"status": "ok"}


def find_first_list(data: Any, keys: set[str]):
    if isinstance(data, dict):
        for key, value in data.items():
            if key.lower() in keys:
                if isinstance(value, list):
                    return value
                if isinstance(value, dict):
                    return list(value.values())
        for value in data.values():
            found = find_first_list(value, keys)
            if found is not None:
                return found
    elif isinstance(data, list):
        for value in data:
            found = find_first_list(value, keys)
            if found is not None:
                return found
    return None


def extract_download_data(data: dict[str, Any]):
    payload = data.get("extractData", {}).get("data", {}).get("data", {})
    downloads = find_first_list(payload, {"downloads", "downloadlinks"}) or []
    subtitles = find_first_list(payload, {"subtitles", "subtitlesdata", "subtitle", "captions", "tracks"}) or []

    if isinstance(downloads, dict):
        downloads = list(downloads.values())
    if isinstance(subtitles, dict):
        subtitles = list(subtitles.values())

    return {
        "downloads": downloads if isinstance(downloads, list) else [],
        "subtitles": subtitles if isinstance(subtitles, list) else [],
        "raw": data,
    }


@app.post("/downloads/links")
@app.post("/proxy/downloads/links")
async def get_download_links(payload: DownloadLinksRequest):
    media_type = payload.type
    if media_type not in {"movie", "tv"}:
        raise HTTPException(status_code=400, detail="type must be movie or tv")
    if media_type == "tv" and (payload.season is None or payload.episode is None):
        raise HTTPException(status_code=400, detail="season and episode are required for tv")

    request_payload = {"type": media_type, "tmdbId": int(payload.tmdbId)}
    if media_type == "tv":
        request_payload["season"] = int(payload.season)
        request_payload["episode"] = int(payload.episode)

    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            token_response = await client.get(f"{DOWNLOAD_API_BASE_URL}/get-token")
            token_response.raise_for_status()
            token = token_response.json().get("t")
            if not token:
                raise HTTPException(status_code=502, detail="Download token missing")

            link_response = await client.post(
                f"{DOWNLOAD_API_BASE_URL}/download-proxy",
                headers={"Content-Type": "application/json", "x-request-token": token},
                json=request_payload,
            )
            link_response.raise_for_status()
            return extract_download_data(link_response.json())
    except HTTPException:
        raise
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code, detail="Download upstream request failed")
    except Exception:
        logging.exception("Download link request failed")
        raise HTTPException(status_code=500, detail="Download request failed")


DATABASE_URL = os.getenv("DATABASE_URL")


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
