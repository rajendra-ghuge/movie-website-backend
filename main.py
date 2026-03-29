import os
import asyncio
import json
from typing import Optional
from fastapi import FastAPI, HTTPException, Request, Query
from fastapi.responses import Response, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
import httpx
from dotenv import load_dotenv
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

load_dotenv()

# Configuration
TMDB_API_KEY = os.getenv("TMDB_API_KEY")
TMDB_BASE_URL = os.getenv("TMDB_BASE_URL", "https://api.themoviedb.org/3")
TMDB_IMAGE_BASE_URL = os.getenv("TMDB_IMAGE_BASE_URL", "https://image.tmdb.org/t/p")
CORS_ORIGINS = os.getenv("CORS_ORIGINS", "http://localhost:5173,http://localhost:3000").split(",")

app = FastAPI(title="movie website  Backend")

# Rate Limiting
limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allow all for deployment initially
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


async def get_tmdb_data(path: str, params: dict = None):
    if params is None:
        params = {}
    params["api_key"] = TMDB_API_KEY
    
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(f"{TMDB_BASE_URL}/{path}", params=params, timeout=10.0)
            response.raise_for_status()
            data = response.content
            return Response(content=data, media_type="application/json")
        except httpx.HTTPStatusError as e:
            raise HTTPException(status_code=e.response.status_code, detail=str(e))
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

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

    async with httpx.AsyncClient() as client:
        try:
            # If filtering by cast, use person's tv_credits for TV
            # (discover/tv does NOT support with_cast - it ignores it)
            if "with_cast" in params:
                person_id = params["with_cast"]
                
                movie_task = client.get(f"{TMDB_BASE_URL}/discover/movie", params=movie_params, timeout=10.0)
                tv_task = client.get(f"{TMDB_BASE_URL}/person/{person_id}/tv_credits", params={"api_key": TMDB_API_KEY}, timeout=10.0)
                
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
                return Response(content=json.dumps(merged_data), media_type="application/json")

            # Standard both discovery (no cast filter)
            movie_task = client.get(f"{TMDB_BASE_URL}/discover/movie", params=movie_params, timeout=10.0)
            tv_task = client.get(f"{TMDB_BASE_URL}/discover/tv", params=tv_params, timeout=10.0)
            
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
            
            return Response(content=json.dumps(merged_data), media_type="application/json")
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

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
    # sizes: original, w500, etc.
    async with httpx.AsyncClient() as client:
        image_url = f"{TMDB_IMAGE_BASE_URL}/{size}/{path}"
        try:
            response = await client.get(image_url, timeout=20.0)
            response.raise_for_status()
            img_data = response.content
            return Response(content=img_data, media_type="image/jpeg", headers={
                "Cache-Control": "public, max-age=31536000"
            })
        except Exception as e:
            raise HTTPException(status_code=404, detail="Image not found")

@app.get("/health")
async def health_check():
    return {"status": "ok"}

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
