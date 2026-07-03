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
    CUSTOMER_TOKENS  JSON object mapping token -> customer. A plain string
                     label entitles the token to every product:
                       {"edc_a1b2c3...": "Acme Reconstruction LLC"}
                     An object form scopes it to specific products (ids from
                     the extension manifests: cammatch, hve_toolkit,
                     point_cloud_toolkit, recon_toolkit; "*" = all):
                       {"edc_a1b2c3...": {"name": "Acme Reconstruction LLC",
                                          "products": ["recon_toolkit"]}}
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

_raw_tokens = os.environ.get("CUSTOMER_TOKENS", "{}")
try:
    CUSTOMER_TOKENS = json.loads(_raw_tokens)
    TOKENS_ERROR = None
except json.JSONDecodeError as exc:
    # Fail loudly: a malformed variable would otherwise silently lock out
    # every customer with 401s. healthz surfaces the error too.
    CUSTOMER_TOKENS = {}
    TOKENS_ERROR = f"CUSTOMER_TOKENS is not valid JSON: {exc}"
    print(f"[gateway] ERROR: {TOKENS_ERROR}")
if not CUSTOMER_TOKENS and _raw_tokens.strip() not in ("", "{}"):
    TOKENS_ERROR = TOKENS_ERROR or "CUSTOMER_TOKENS parsed to an empty object"
    print(f"[gateway] WARNING: {TOKENS_ERROR}")

app = FastAPI(title="EDC Software extensions gateway", docs_url=None, redoc_url=None)

_origin_cache: dict[str, tuple[float, bytes]] = {}


def _entitlements(raw):
    """Normalize a CUSTOMER_TOKENS value to {"name": str, "products": [...]}.

    A plain string label means the customer is entitled to every product
    (the original format keeps working). An object form scopes the token:
        {"name": "Acme LLC", "products": ["cammatch", "recon_toolkit"]}
    "*" (or an omitted/empty products list) means all products.
    """
    if isinstance(raw, str):
        return {"name": raw, "products": ["*"]}
    if isinstance(raw, dict):
        products = raw.get("products") or ["*"]
        return {"name": raw.get("name", "unnamed"), "products": list(products)}
    return None


def _entitled(customer: dict, product_id: str) -> bool:
    return "*" in customer["products"] or product_id in customer["products"]


def _authed_customer(request: Request) -> dict | None:
    """Return {"name", "products"} for the request's Bearer token, or None."""
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        raw = CUSTOMER_TOKENS.get(auth[7:].strip())
        if raw is not None:
            return _entitlements(raw)
    return None


def _require_customer(request: Request) -> dict:
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
    body = {"ok": TOKENS_ERROR is None, "tokens_configured": len(CUSTOMER_TOKENS)}
    if TOKENS_ERROR:
        body["error"] = TOKENS_ERROR
    return body


@app.get("/")
async def landing():
    html = await _origin_get("index.html")
    return Response(content=html, media_type="text/html")


@app.get("/index.json")
async def index_json(request: Request):
    customer = _require_customer(request)
    body = await _origin_get("index.json")
    if "*" not in customer["products"]:
        index = json.loads(body)
        index["data"] = [
            e for e in index.get("data", []) if _entitled(customer, e.get("id", ""))
        ]
        body = json.dumps(index, indent=2).encode()
    print(f"[gateway] index fetch by {customer['name']}")
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
    product_id = entry.get("id", "")
    if product_id and not _entitled(customer, product_id):
        raise HTTPException(
            status_code=403,
            detail=(
                f"This token is not licensed for {product_id}. "
                "Contact Engineering Dynamics Company to add it."
            ),
        )

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

    print(f"[gateway] {filename} download by {customer['name']}")

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
