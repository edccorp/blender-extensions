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
                     CUSTOMERS_REPO, used by /admin/customers and the
                     Stripe purchase flow to commit new customers (keep
                     GH_TOKEN itself read-only)
    STRIPE_SECRET_KEY    Stripe API key (a restricted key with read on
                     Checkout Sessions + Products is enough) — enables
                     GET /welcome, the post-payment page that provisions
                     the buyer's token and shows it on screen
    STRIPE_WEBHOOK_SECRET  signing secret (whsec_...) of a Stripe webhook
                     pointed at POST /webhook/stripe for
                     checkout.session.completed — a backstop that
                     provisions the purchase even if the buyer never
                     reaches /welcome
    ORIGIN_BASE      Pages origin serving index.json/packages.json
                     (default https://edccorp.github.io/blender-extensions)
    CACHE_TTL        origin cache seconds (default 300)
"""

import base64
import hashlib
import hmac
import html
import json
import os
import secrets
import time
from datetime import date, timedelta

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse
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
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
# Default license term (days of update service) for purchases whose Payment
# Link has no term_days metadata; empty = perpetual.
LICENSE_TERM_DAYS = os.environ.get("LICENSE_TERM_DAYS", "")
PRODUCT_IDS = ("cammatch", "hve_toolkit", "point_cloud_toolkit", "recon_toolkit")
PRODUCT_NAMES = {
    "cammatch": "CamMatch™",
    "hve_toolkit": "HVE Toolkit™",
    "point_cloud_toolkit": "Point Cloud Toolkit™",
    "recon_toolkit": "Recon Toolkit™",
}

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


def _normalize_products(products) -> dict:
    """Normalize entitlements to {product_id: expiry}.

    expiry is "YYYY-MM-DD" (update service runs through that date, UTC,
    inclusive) or None for perpetual. The list form (["recon_toolkit"])
    and "*" keep working and mean perpetual.
    """
    if isinstance(products, dict):
        return dict(products)
    return {p: None for p in (products or ["*"])}


def _entitlements(raw):
    """Normalize a customer entry to {"name": str, "products": {id: expiry}}.

    A plain string label means every product, forever. The object form
    scopes it — products as a list (perpetual) or a map with expiry dates:
        {"name": "Acme LLC", "products": {"recon_toolkit": "2027-07-03"}}
    """
    if isinstance(raw, str):
        return {"name": raw, "products": {"*": None}}
    if isinstance(raw, dict):
        return {"name": raw.get("name", "unnamed"),
                "products": _normalize_products(raw.get("products"))}
    return None


def _active(expiry) -> bool:
    return expiry is None or str(expiry) >= date.today().isoformat()


def _entitled(customer: dict, product_id: str) -> bool:
    products = customer["products"]
    return any(k in products and _active(products[k]) for k in ("*", product_id))


def _entitlement_state(customer: dict, product_id: str) -> str:
    """'active', 'expired' (was licensed, term lapsed), or 'none'."""
    products = customer["products"]
    expiries = [products[k] for k in ("*", product_id) if k in products]
    if not expiries:
        return "none"
    return "active" if any(_active(e) for e in expiries) else "expired"


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


async def _read_customers_file() -> tuple[dict, str | None]:
    """Return (customers, file sha); a missing file is an empty list."""
    resp = await _customers_file_request("GET")
    if resp.status_code == 200:
        payload = resp.json()
        return json.loads(base64.b64decode(payload["content"])), payload.get("sha")
    if resp.status_code == 404:
        return {}, None
    raise HTTPException(
        status_code=502, detail=f"GitHub returned {resp.status_code} reading the customer file"
    )


async def _write_customers_file(customers: dict, sha: str | None, message: str) -> None:
    payload = {
        "message": message,
        "content": base64.b64encode(
            (json.dumps(customers, indent=2, sort_keys=True) + "\n").encode()
        ).decode(),
    }
    if sha:
        payload["sha"] = sha
    resp = await _customers_file_request("PUT", payload)
    if resp.status_code == 409:
        # Someone else committed between our read and write (e.g. /welcome
        # and the webhook racing); callers re-read and retry.
        raise HTTPException(status_code=409, detail="customer file changed concurrently")
    if resp.status_code not in (200, 201):
        raise HTTPException(
            status_code=502, detail=f"GitHub returned {resp.status_code} writing the customer file"
        )
    _customers_cache["at"] = 0.0  # the new token must work on the very next sync


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
    term_days = body.get("term_days")
    if term_days is not None:
        try:
            term_days = int(term_days)
            if term_days <= 0:
                raise ValueError
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="'term_days' must be a positive integer")

    customers, sha = await _read_customers_file()

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
    if email or products or term_days:
        customers[token] = {
            "name": name,
            "products": _merge_products({}, products or ["*"], term_days),
        }
        if email:
            customers[token]["email"] = email
    else:
        customers[token] = name
    await _write_customers_file(customers, sha, f"Add customer: {name}")
    print(f"[gateway] admin: added customer {name} ({', '.join(products) or 'all products'})")
    return {
        "token": token,
        "name": name,
        "products": _entitlements(customers[token])["products"],
        "repository_url": "https://extensions.edccorp.com/index.json",
        "existing": False,
    }


# --------------------------------------------------------------------------
# Stripe purchase flow: Payment Link -> /welcome (token on screen), with
# /webhook/stripe as a backstop if the buyer never reaches the redirect.

async def _stripe_checkout_session(session_id: str) -> dict:
    """Fetch a checkout session, with line-item products expanded."""
    if not session_id.startswith("cs_"):
        raise HTTPException(status_code=404, detail="unknown checkout session")
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            f"https://api.stripe.com/v1/checkout/sessions/{session_id}",
            params=[("expand[]", "line_items.data.price.product")],
            headers={"Authorization": f"Bearer {STRIPE_SECRET_KEY}"},
        )
    if resp.status_code == 404:
        raise HTTPException(status_code=404, detail="unknown checkout session")
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Stripe returned {resp.status_code}")
    return resp.json()


def _session_products(session: dict) -> list[str]:
    """Product ids for a purchase ([] = all products).

    Read from the checkout session's metadata (Payment Link metadata is
    copied there — set `products` on each link), falling back to a
    `products` metadata key on the Stripe Products themselves.
    """
    raw = (session.get("metadata") or {}).get("products", "")
    ids = [p.strip() for p in raw.split(",") if p.strip()]
    if not ids:
        for item in ((session.get("line_items") or {}).get("data") or []):
            product = (item.get("price") or {}).get("product") or {}
            if isinstance(product, dict):
                raw = (product.get("metadata") or {}).get("products", "")
                ids += [p.strip() for p in raw.split(",") if p.strip()]
    ids = list(dict.fromkeys(ids))
    if "*" in ids:
        return ["*"]
    unknown = [p for p in ids if p not in PRODUCT_IDS]
    if not ids or unknown:
        print(
            f"[gateway] ERROR: session {session.get('id')} has "
            f"{'unknown product ids ' + str(unknown) if unknown else 'no product metadata'} "
            "— set a 'products' metadata key on the Payment Link or Product in Stripe"
        )
        raise HTTPException(
            status_code=500,
            detail=(
                "This purchase is missing product information. Your payment went "
                "through — contact Engineering Dynamics Company and your access "
                "will be set up right away."
            ),
        )
    return ids


def _session_term_days(session: dict) -> int | None:
    """License term for a purchase, in days (None = perpetual).

    From a term_days metadata key on the Payment Link (copied onto the
    session), else the LICENSE_TERM_DAYS env default.
    """
    raw = str((session.get("metadata") or {}).get("term_days", "") or LICENSE_TERM_DAYS).strip()
    if not raw:
        return None
    try:
        days = int(raw)
        if days <= 0:
            raise ValueError(raw)
    except ValueError:
        print(f"[gateway] ERROR: session {session.get('id')} has invalid term_days {raw!r}")
        raise HTTPException(
            status_code=500,
            detail=(
                "This purchase is missing license-term information. Your payment "
                "went through — contact Engineering Dynamics Company and your "
                "access will be set up right away."
            ),
        )
    return days


def _entry_sessions(value: dict) -> list:
    sessions = value.get("stripe_sessions")
    if isinstance(sessions, list):
        return sessions
    legacy = value.get("stripe_session")  # entries written before the merge logic
    return [legacy] if legacy else []


_MISSING = object()


def _merge_products(existing: dict, ids: list, term_days: int | None) -> dict:
    """Fold a purchase of `ids` (for `term_days` of updates) into an entry.

    Perpetual (None) always wins over a dated term. A renewal of a dated
    product extends from its current expiry when still active — renewing
    three months early doesn't forfeit the three months — and from today
    when it already lapsed.
    """
    today = date.today()
    new_expiry = (today + timedelta(days=term_days)).isoformat() if term_days else None
    merged = dict(existing)
    for pid in ids:
        current = merged.get(pid, _MISSING)
        if current is _MISSING:
            merged[pid] = new_expiry
        elif current is None or term_days is None:
            merged[pid] = None
        else:
            base = max(date.fromisoformat(current), today)
            merged[pid] = (base + timedelta(days=term_days)).isoformat()
    return merged


async def _provision_purchase(session: dict) -> dict:
    """Turn a paid checkout session into a customer entry, exactly once.

    A returning buyer (matched by email) keeps their existing token and
    has the purchased products added to it — Blender holds one Secret per
    repository, so a second token would strand their first purchase.
    Result flags: merged (this purchase extended an existing license, so
    the UI must not reveal the full token) and already_processed (this
    session was provisioned before — welcome revisit or webhook replay).
    """
    details = session.get("customer_details") or {}
    email = (details.get("email") or "").strip()
    name = details.get("name") or email or "Unknown customer"
    session_id = session["id"]

    for attempt in (1, 2):
        customers, sha = await _read_customers_file()

        for token, value in customers.items():
            if isinstance(value, dict) and session_id in _entry_sessions(value):
                entry = _entitlements(value)
                return {
                    "token": token, "name": entry["name"], "products": entry["products"],
                    "merged": _entry_sessions(value)[0] != session_id,
                    "already_processed": True,
                }

        ids = _session_products(session)
        term_days = _session_term_days(session)
        try:
            if email:
                for token, value in customers.items():
                    if (
                        isinstance(value, dict)
                        and value.get("email", "").strip().lower() == email.lower()
                    ):
                        value["products"] = _merge_products(
                            _entitlements(value)["products"], ids, term_days
                        )
                        value["stripe_sessions"] = [*_entry_sessions(value), session_id]
                        value.pop("stripe_session", None)
                        entry_name = value.get("name", name)
                        await _write_customers_file(customers, sha, f"Extend license: {entry_name}")
                        print(f"[gateway] stripe: extended {entry_name} <{email}> with {', '.join(ids)}")
                        return {"token": token, "name": entry_name, "products": value["products"],
                                "merged": True, "already_processed": False}

            token = "edc_" + secrets.token_urlsafe(18)
            entry = {
                "name": name,
                "products": _merge_products({}, ids, term_days),
                "stripe_sessions": [session_id],
            }
            if email:
                entry["email"] = email
            customers[token] = entry
            await _write_customers_file(customers, sha, f"Add customer: {name} (Stripe purchase)")
            print(f"[gateway] stripe: provisioned {name} <{email}> ({', '.join(ids)})")
            return {"token": token, "name": name, "products": entry["products"],
                    "merged": False, "already_processed": False}
        except HTTPException as exc:
            if exc.status_code == 409 and attempt == 1:
                continue  # /welcome and the webhook raced; re-read and retry
            raise


def _stripe_signature_ok(payload: bytes, header: str) -> bool:
    """Verify a Stripe-Signature header (t=...,v1=... HMAC-SHA256 scheme)."""
    pairs = [p.split("=", 1) for p in header.split(",") if "=" in p]
    timestamp = next((v for k, v in pairs if k == "t"), "")
    if not timestamp or abs(time.time() - int(timestamp)) > 300:
        return False
    expected = hmac.new(
        STRIPE_WEBHOOK_SECRET.encode(),
        f"{timestamp}.".encode() + payload,
        hashlib.sha256,
    ).hexdigest()
    return any(hmac.compare_digest(expected, v) for k, v in pairs if k == "v1")


def _welcome_html(result: dict) -> str:
    products = _normalize_products(result["products"])
    if "*" in products:
        star = products.pop("*")
        # Each product shows its best coverage: perpetual beats any date,
        # otherwise the later of its own expiry and the bundle's.
        products = {
            p: (None if star is None or products.get(p, star) is None
                else max(products.get(p, star), star))
            for p in PRODUCT_IDS
        }
    items = "".join(
        f"<li>{html.escape(PRODUCT_NAMES.get(p, p))}"
        + (f' <span class="muted">— updates through {html.escape(str(exp))}</span>' if exp else "")
        + "</li>"
        for p, exp in products.items()
    )
    if result["merged"]:
        # Never print the full existing token on an upgrade: purchases only
        # prove control of a card, not ownership of this customer's email.
        masked = html.escape(result["token"][:8]) + "…"
        heading = "Purchase added to your license"
        token_block = f"""
<p>You already have an EDC access token (it starts with
<code>{masked}</code>) — this purchase has been added to it
automatically, so there is <strong>nothing to change in Blender</strong>:</p>
<ol>
<li>Edit &rsaquo; Preferences &rsaquo; Get Extensions</li>
<li>Press the refresh (&#10227;) button on the <b>extensions.edccorp.com</b> repository</li>
<li>Your new product appears — click <b>Install</b></li>
</ol>
<p class="muted">Lost your token or using a new computer? Reply to your
receipt email or contact Engineering Dynamics Company.</p>"""
    else:
        heading = "Payment received — welcome!"
        token_block = f"""
<p><strong>Your access token</strong> (click to select, then copy):</p>
<code class="token">{html.escape(result["token"])}</code>
<p><strong>Set up Blender</strong> (4.2 or newer):</p>
<ol>
<li>Edit &rsaquo; Preferences &rsaquo; Get Extensions &rsaquo; Repositories &rsaquo; <b>+</b> &rsaquo; Add Remote Repository</li>
<li>URL: <code>https://extensions.edccorp.com/index.json</code></li>
<li>Tick <b>Requires Access Token</b> and paste your token into <b>Secret</b></li>
<li>Your products appear under Get Extensions — click <b>Install</b>; updates arrive automatically</li>
</ol>
<p class="muted">Save your token somewhere safe — this page won't show it
again. Lost it or need help? Reply to your receipt email or contact
Engineering Dynamics Company.</p>"""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>Welcome — EDC Software</title>
<style>
  :root {{ color-scheme: light dark;
    --bg: #f6f7f9; --card: #ffffff; --ink: #1c2530; --muted: #5b6773;
    --accent: #0e5a8a; --border: #e2e6ea; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --bg: #10151b; --card: #1a2129; --ink: #e8edf2;
      --muted: #9aa7b2; --accent: #6fb3dc; --border: #2a333d; }} }}
  body {{ margin: 0; background: var(--bg); color: var(--ink);
    font: 16px/1.6 system-ui, "Segoe UI", sans-serif; }}
  main {{ max-width: 40rem; margin: 3rem auto; padding: 0 1.25rem; }}
  .card {{ background: var(--card); border: 1px solid var(--border);
    border-radius: 12px; padding: 2rem; }}
  h1 {{ margin: 0 0 .25rem; font-size: 1.5rem; }}
  .muted {{ color: var(--muted); }}
  code.token {{ display: block; margin: 1rem 0; padding: .9rem 1rem;
    background: var(--bg); border: 1px solid var(--border); border-radius: 8px;
    font-size: 1.05rem; word-break: break-all; user-select: all; }}
  ol li, ul li {{ margin: .35rem 0; }}
  a {{ color: var(--accent); }}
</style></head><body><main><div class="card">
<h1>{heading}</h1>
<p class="muted">Engineering Dynamics Company</p>
<p>Hi {html.escape(result["name"])}, your license now covers:</p>
<ul>{items}</ul>
{token_block}
</div></main></body></html>"""


@app.get("/welcome")
async def welcome(session_id: str = ""):
    if not (STRIPE_SECRET_KEY and ADMIN_GH_TOKEN and CUSTOMERS_REPO):
        raise HTTPException(status_code=503, detail="Stripe provisioning is not configured")
    if not session_id:
        raise HTTPException(status_code=404)
    session = await _stripe_checkout_session(session_id)
    if session.get("payment_status") not in ("paid", "no_payment_required"):
        return HTMLResponse(
            "<h1>Payment still processing</h1><p>Your payment hasn't settled yet. "
            "You'll receive your access token by email once it does — or revisit "
            "this page in a few minutes.</p>",
            status_code=202,
        )
    return HTMLResponse(_welcome_html(await _provision_purchase(session)))


@app.post("/webhook/stripe")
async def stripe_webhook(request: Request):
    if not (STRIPE_WEBHOOK_SECRET and STRIPE_SECRET_KEY and ADMIN_GH_TOKEN and CUSTOMERS_REPO):
        raise HTTPException(status_code=503, detail="Stripe webhook is not configured")
    payload = await request.body()
    if not _stripe_signature_ok(payload, request.headers.get("stripe-signature", "")):
        raise HTTPException(status_code=400, detail="invalid signature")
    event = json.loads(payload)
    if event.get("type") == "checkout.session.completed":
        obj = event.get("data", {}).get("object", {})
        if obj.get("payment_status") in ("paid", "no_payment_required"):
            # Re-fetch (with expanded line items) rather than trusting the
            # event body, then provision — a no-op if /welcome already did.
            session = await _stripe_checkout_session(obj["id"])
            result = await _provision_purchase(session)
            if not result["already_processed"]:
                print(f"[gateway] stripe: webhook provisioned session {obj['id']}")
    return {"received": True}


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
    products = customer["products"]
    if not ("*" in products and _active(products["*"])):
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
    state = _entitlement_state(customer, product_id) if product_id else "active"
    if state != "active":
        detail = (
            f"Your update service for {product_id} has expired — installed "
            "versions keep working, but updates require a renewal. Visit "
            "https://extensions.edccorp.com or contact Engineering Dynamics Company."
            if state == "expired"
            else f"This token is not licensed for {product_id}. "
            "Contact Engineering Dynamics Company to add it."
        )
        raise HTTPException(status_code=403, detail=detail)

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
