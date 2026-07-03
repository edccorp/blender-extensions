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
    CUSTOMERS_REPO   optional "owner/repo" of a private repo holding the
                     customer list as a JSON file. When set, the file is
                     fetched with GH_TOKEN and re-read within CUSTOMERS_TTL
                     seconds of any change — add/revoke customers by
                     editing the file, no redeploy. Entries there override
                     CUSTOMER_TOKENS, which remains as a bootstrap/fallback.
    CUSTOMERS_PATH   path of the file in CUSTOMERS_REPO (default customers.json)
    CUSTOMERS_TTL    customer-list cache seconds (default 60)
    ADMIN_API_TOKEN  enables POST /admin/customers (used by the purchase
                     automation, e.g. Power Automate) when set; callers
                     must send it as a Bearer token
    ADMIN_GH_TOKEN   fine-grained PAT with Contents:read&write on
                     CUSTOMERS_REPO, used by /admin/customers to commit
                     new customers (keep GH_TOKEN itself read-only)
    ORIGIN_BASE      Pages origin serving index.json/packages.json
                     (default https://edccorp.github.io/blender-extensions)
    CACHE_TTL        origin cache seconds (default 300)
"""

import base64
import json
import os
import secrets
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
CUSTOMERS_REPO = os.environ.get("CUSTOMERS_REPO", "")
CUSTOMERS_PATH = os.environ.get("CUSTOMERS_PATH", "customers.json")
CUSTOMERS_TTL = int(os.environ.get("CUSTOMERS_TTL", "60"))
ADMIN_API_TOKEN = os.environ.get("ADMIN_API_TOKEN", "")
ADMIN_GH_TOKEN = os.environ.get("ADMIN_GH_TOKEN", "")
PRODUCT_IDS = ("cammatch", "hve_toolkit", "point_cloud_toolkit", "recon_toolkit")

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
_customers_cache: dict = {"at": 0.0, "data": None, "error": None}


async def _customer_tokens() -> dict:
    """Current token -> customer mapping.

    With CUSTOMERS_REPO set, the JSON file is fetched via the GitHub
    contents API using GH_TOKEN and cached for CUSTOMERS_TTL seconds; on
    fetch/parse errors the last good copy keeps serving and the error is
    surfaced on /healthz. CUSTOMER_TOKENS entries are merged underneath,
    so the env var still works as a bootstrap or emergency override —
    though a token revoked in the file stays active if it also lives in
    the env var, so clear CUSTOMER_TOKENS once you migrate.
    """
    if not CUSTOMERS_REPO:
        return CUSTOMER_TOKENS
    now = time.time()
    if _customers_cache["data"] is None or now - _customers_cache["at"] >= CUSTOMERS_TTL:
        url = f"https://api.github.com/repos/{CUSTOMERS_REPO}/contents/{CUSTOMERS_PATH}"
        headers = {"Accept": "application/vnd.github.raw+json"}
        if GH_TOKEN:
            headers["Authorization"] = f"Bearer {GH_TOKEN}"
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(url, headers=headers)
            if resp.status_code != 200:
                raise RuntimeError(f"GitHub returned {resp.status_code}")
            data = json.loads(resp.content)
            if not isinstance(data, dict):
                raise ValueError("customer file must be a JSON object")
            _customers_cache.update(data=data, error=None)
        except Exception as exc:
            _customers_cache["error"] = (
                f"customer list fetch failed ({CUSTOMERS_REPO}/{CUSTOMERS_PATH}): {exc}"
            )
            print(f"[gateway] ERROR: {_customers_cache['error']}")
        # Stamp even on failure so a broken origin isn't hit on every request.
        _customers_cache["at"] = now
    return {**CUSTOMER_TOKENS, **(_customers_cache["data"] or {})}


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


async def _authed_customer(request: Request) -> dict | None:
    """Return {"name", "products"} for the request's Bearer token, or None."""
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        tokens = await _customer_tokens()
        raw = tokens.get(auth[7:].strip())
        if raw is not None:
            return _entitlements(raw)
    return None


async def _require_customer(request: Request) -> dict:
    customer = await _authed_customer(request)
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


async def _customers_file_request(method: str, payload: dict | None = None) -> httpx.Response:
    """Read or write the customer file in CUSTOMERS_REPO via the contents API."""
    url = f"https://api.github.com/repos/{CUSTOMERS_REPO}/contents/{CUSTOMERS_PATH}"
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {ADMIN_GH_TOKEN}",
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        return await client.request(method, url, headers=headers, json=payload)


@app.post("/admin/customers")
async def admin_add_customer(request: Request):
    """Provision a customer: generate a token and commit it to CUSTOMERS_REPO.

    Called by the purchase automation (Power Automate) after payment is
    approved. Body: {"name": "Acme LLC", "email": "buyer@acme.com",
    "products": ["recon_toolkit"]} — email optional, products optional
    (omitted/empty/"*" = all). Retry-safe: a name that already exists
    returns the existing entry instead of minting a duplicate token.
    """
    if not (ADMIN_API_TOKEN and ADMIN_GH_TOKEN and CUSTOMERS_REPO):
        raise HTTPException(
            status_code=503,
            detail="admin API not configured (ADMIN_API_TOKEN, ADMIN_GH_TOKEN, CUSTOMERS_REPO)",
        )
    auth = request.headers.get("authorization", "")
    if not (
        auth.lower().startswith("bearer ")
        and secrets.compare_digest(auth[7:].strip(), ADMIN_API_TOKEN)
    ):
        raise HTTPException(status_code=401, detail="admin token required")

    try:
        body = await request.json()
        assert isinstance(body, dict)
    except Exception:
        raise HTTPException(status_code=400, detail="a JSON object body is required")
    name = str(body.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="'name' is required")
    email = str(body.get("email") or "").strip()
    products = body.get("products") or []
    if isinstance(products, str):
        products = [p.strip() for p in products.split(",") if p.strip()]
    unknown = [p for p in products if p not in PRODUCT_IDS and p != "*"]
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"unknown product ids {unknown}; valid: {', '.join(PRODUCT_IDS)} or *",
        )
    if "*" in products:
        products = []

    resp = await _customers_file_request("GET")
    if resp.status_code == 200:
        payload = resp.json()
        customers = json.loads(base64.b64decode(payload["content"]))
        sha = payload.get("sha")
    elif resp.status_code == 404:
        customers, sha = {}, None  # first customer creates the file
    else:
        raise HTTPException(
            status_code=502, detail=f"GitHub returned {resp.status_code} reading the customer file"
        )

    for existing_token, value in customers.items():
        entry = _entitlements(value)
        if entry and entry["name"].lower() == name.lower():
            print(f"[gateway] admin: {name} already exists, returning existing entry")
            return {
                "token": existing_token,
                "name": entry["name"],
                "products": entry["products"],
                "repository_url": "https://extensions.edccorp.com/index.json",
                "existing": True,
            }

    token = "edc_" + secrets.token_urlsafe(18)
    if email or products:
        customers[token] = {"name": name, "products": products or ["*"]}
        if email:
            customers[token]["email"] = email
    else:
        customers[token] = name
    put_payload = {
        "message": f"Add customer: {name}",
        "content": base64.b64encode(
            (json.dumps(customers, indent=2, sort_keys=True) + "\n").encode()
        ).decode(),
    }
    if sha:
        put_payload["sha"] = sha
    resp = await _customers_file_request("PUT", put_payload)
    if resp.status_code not in (200, 201):
        raise HTTPException(
            status_code=502, detail=f"GitHub returned {resp.status_code} writing the customer file"
        )

    _customers_cache["at"] = 0.0  # token must work on the customer's first sync
    print(f"[gateway] admin: added customer {name} ({', '.join(products) or 'all products'})")
    return {
        "token": token,
        "name": name,
        "products": products or ["*"],
        "repository_url": "https://extensions.edccorp.com/index.json",
        "existing": False,
    }


@app.get("/healthz")
async def healthz():
    tokens = await _customer_tokens()
    body = {"ok": TOKENS_ERROR is None, "tokens_configured": len(tokens)}
    if CUSTOMERS_REPO:
        body["customers_source"] = CUSTOMERS_REPO
        if _customers_cache["error"]:
            body["ok"] = False
            body["customers_error"] = _customers_cache["error"]
    if TOKENS_ERROR:
        body["error"] = TOKENS_ERROR
    return body


@app.get("/")
async def landing():
    html = await _origin_get("index.html")
    return Response(content=html, media_type="text/html")


@app.get("/index.json")
async def index_json(request: Request):
    customer = await _require_customer(request)
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
    customer = await _require_customer(request)
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
