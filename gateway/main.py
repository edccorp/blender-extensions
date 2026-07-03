"""EDC Software extensions gateway.

A small authentication gateway for the Blender extensions repository at
extensions.edccorp.com, designed to run on Railway (or any container
host). Blender sends the per-customer token from the repository's
"Secret" field as ``Authorization: Bearer <token>`` on every index fetch
and download; this app validates it and serves:

    GET /                  landing page   (public, proxied from Pages)
    GET /index.json        extension index (token required)
    GET /packages/<file>   add-on zips     (token required, streamed from
                                            the private GitHub release
                                            asset via GH_TOKEN)
    GET /healthz           liveness probe  (public)

Environment variables:
    CUSTOMER_TOKENS  JSON object mapping token -> customer label, e.g.
                     {"edc_a1b2c3...": "Acme Reconstruction LLC"}
    GH_TOKEN         fine-grained GitHub PAT with Contents:read on the
                     product repos (used to stream private release assets)
    ORIGIN_BASE      Pages origin serving index.json/packages.json
                     (default https://edccorp.github.io/blender-extensions)
    CACHE_TTL        origin cache seconds (default 300)
"""

import json
import os
import time

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from starlette.background import BackgroundTask

ORIGIN_BASE = os.environ.get(
    "ORIGIN_BASE", "https://edccorp.github.io/blender-extensions"
).rstrip("/")
GH_TOKEN = os.environ.get("GH_TOKEN", "")
CACHE_TTL = int(os.environ.get("CACHE_TTL", "300"))

try:
    CUSTOMER_TOKENS = json.loads(os.environ.get("CUSTOMER_TOKENS", "{}"))
except json.JSONDecodeError:
    CUSTOMER_TOKENS = {}

app = FastAPI(title="EDC Software extensions gateway", docs_url=None, redoc_url=None)

_origin_cache: dict[str, tuple[float, bytes]] = {}


def _authed_customer(request: Request) -> str | None:
    """Return the customer label for the request's Bearer token, or None."""
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return CUSTOMER_TOKENS.get(auth[7:].strip())
    return None


def _require_customer(request: Request) -> str:
    customer = _authed_customer(request)
    if customer is None:
        raise HTTPException(
            status_code=401,
            detail=(
                "A valid EDC Software access token is required. In Blender, "
                "enable 'Requires Access Token' on the extensions.edccorp.com "
                "repository and paste your token into the Secret field. "
                "Contact Engineering Dynamics Company for a token."
            ),
            headers={"WWW-Authenticate": "Bearer"},
        )
    return customer


async def _origin_get(path: str) -> bytes:
    """Fetch a small file from the Pages origin, cached for CACHE_TTL."""
    now = time.time()
    hit = _origin_cache.get(path)
    if hit is not None and now - hit[0] < CACHE_TTL:
        return hit[1]
    async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
        resp = await client.get(f"{ORIGIN_BASE}/{path}")
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"origin returned {resp.status_code} for {path}")
    _origin_cache[path] = (now, resp.content)
    return resp.content


@app.get("/healthz")
async def healthz():
    return {"ok": True, "tokens_configured": len(CUSTOMER_TOKENS)}


@app.get("/")
async def landing():
    html = await _origin_get("index.html")
    return Response(content=html, media_type="text/html")


@app.get("/index.json")
async def index_json(request: Request):
    customer = _require_customer(request)
    body = await _origin_get("index.json")
    print(f"[gateway] index fetch by {customer}")
    return Response(content=body, media_type="application/json")


@app.get("/packages/{filename}")
async def package(filename: str, request: Request):
    customer = _require_customer(request)
    if "/" in filename or ".." in filename:
        raise HTTPException(status_code=404)

    packages = json.loads(await _origin_get("packages.json"))
    entry = packages.get(filename)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"unknown package {filename}")

    # The API asset endpoint works for private repos; the public
    # browser_download_url does not.
    asset_url = f"https://api.github.com/repos/{entry['repo']}/releases/assets/{entry['asset_id']}"
    headers = {"Accept": "application/octet-stream"}
    if GH_TOKEN:
        headers["Authorization"] = f"Bearer {GH_TOKEN}"

    client = httpx.AsyncClient(follow_redirects=True, timeout=None)
    upstream = await client.send(
        client.build_request("GET", asset_url, headers=headers), stream=True
    )
    if upstream.status_code != 200:
        await upstream.aclose()
        await client.aclose()
        raise HTTPException(status_code=502, detail=f"upstream returned {upstream.status_code}")

    print(f"[gateway] {filename} download by {customer}")

    async def _close():
        await upstream.aclose()
        await client.aclose()

    return StreamingResponse(
        upstream.aiter_bytes(),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            **(
                {"Content-Length": upstream.headers["content-length"]}
                if "content-length" in upstream.headers
                else {}
            ),
        },
        background=BackgroundTask(_close),
    )
