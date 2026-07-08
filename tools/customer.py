#!/usr/bin/env python3
"""Manage customer repository secrets for the EDC Software extensions gateway.

The customer list lives as customers.json in a private GitHub repo; the
gateway (extensions.edccorp.com) re-reads it within about a minute of any
change, so adding or revoking a customer never touches Railway and needs
no redeploy. Every change is a git commit — the repo history is the audit
trail.

One-time setup on your machine:
    set CUSTOMERS_ADMIN_TOKEN=github_pat_...
        (fine-grained PAT with Contents: read & write on the customers
         repo ONLY — do not reuse the gateway's read-only GH_TOKEN)
    set CUSTOMERS_REPO=edccorp/edc-extensions-customers
        (optional; this is the default)

Usage:
    python tools/customer.py add "Acme Reconstruction LLC"
    python tools/customer.py add "Smith Engineering" --products recon_toolkit,point_cloud_toolkit
    python tools/customer.py list
    python tools/customer.py set-products "Smith Engineering" --products "*"
    python tools/customer.py revoke "Acme Reconstruction LLC"

`add` prints the new repository secret exactly once — send it to the customer, it is
not stored anywhere except the customers file.
"""

import argparse
import base64
import json
import os
import secrets
import sys
import urllib.error
import urllib.request
from datetime import date

REPO = os.environ.get("CUSTOMERS_REPO", "edccorp/edc-extensions-customers")
PATH = os.environ.get("CUSTOMERS_PATH", "customers.json")
ADMIN_TOKEN = os.environ.get("CUSTOMERS_ADMIN_TOKEN", "")
API_URL = f"https://api.github.com/repos/{REPO}/contents/{PATH}"
PRODUCT_IDS = ("cammatch", "hve_toolkit", "point_cloud_toolkit", "recon_toolkit")


def die(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)
    sys.exit(1)


def api(method: str = "GET", payload: dict | None = None):
    request = urllib.request.Request(API_URL, method=method)
    request.add_header("Authorization", f"Bearer {ADMIN_TOKEN}")
    request.add_header("Accept", "application/vnd.github+json")
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        request.add_header("Content-Type", "application/json")
    return urllib.request.urlopen(request, data)


def github_message(exc: urllib.error.HTTPError) -> str:
    try:
        return json.load(exc).get("message", "")
    except Exception:
        return ""


def explain_http_error(exc: urllib.error.HTTPError, writing: bool) -> None:
    detail = github_message(exc)
    hints = {
        401: "GitHub rejected CUSTOMERS_ADMIN_TOKEN — the PAT is invalid or expired",
        403: (
            "the PAT is not allowed to write here. On the fine-grained PAT page, "
            "set Repository permissions > Contents to 'Read and write', make sure "
            f"{REPO} is in the PAT's repository list, and click Update"
        ),
        404: (
            f"GitHub can't see {REPO} with this PAT — check the repo exists with "
            "exactly that name and is in the PAT's repository list"
        ),
        409: "the file changed on GitHub while this command ran — just re-run it",
    }
    hint = hints.get(exc.code, "")
    suffix = f" (GitHub says: {detail})" if detail else ""
    die(f"HTTP {exc.code} {'writing' if writing else 'reading'} "
        f"{REPO}/{PATH}: {hint or 'unexpected error'}{suffix}")


def fetch() -> tuple[dict, str | None]:
    """Return (customers, file sha). Missing file -> ({}, None)."""
    try:
        with api() as resp:
            body = json.load(resp)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            # Missing file and PAT-can't-see-repo both 404 here; assume the
            # former (first run) — a bad PAT then fails loudly on the write.
            return {}, None
        explain_http_error(exc, writing=False)
    customers = json.loads(base64.b64decode(body["content"]))
    if not isinstance(customers, dict):
        die(f"{PATH} in {REPO} is not a JSON object")
    return customers, body["sha"]


def save(customers: dict, sha: str | None, message: str) -> None:
    payload = {
        "message": message,
        "content": base64.b64encode(
            (json.dumps(customers, indent=2, sort_keys=True) + "\n").encode()
        ).decode(),
    }
    if sha:
        payload["sha"] = sha
    try:
        with api("PUT", payload):
            pass
    except urllib.error.HTTPError as exc:
        explain_http_error(exc, writing=True)


def name_of(value) -> str:
    return value if isinstance(value, str) else value.get("name", "unnamed")


def products_of(value) -> list[str]:
    """Display form: 'recon_toolkit (through 2027-07-03)' for dated terms."""
    if isinstance(value, str):
        return ["*"]
    products = value.get("products") or ["*"]
    if isinstance(products, dict):
        return [f"{p} (through {exp})" if exp else p for p, exp in products.items()]
    return list(products)


def find(customers: dict, key: str) -> list[str]:
    """Match by exact repository secret first, then case-insensitive customer name."""
    if key in customers:
        return [key]
    return [t for t, v in customers.items() if name_of(v).lower() == key.lower()]


def parse_products(raw: str | None) -> list[str] | None:
    """None means all products (stored as the compact plain-string form)."""
    if raw is None or raw.strip() in ("", "*"):
        return None
    products = [p.strip() for p in raw.split(",") if p.strip()]
    unknown = [p for p in products if p not in PRODUCT_IDS]
    if unknown:
        die(f"unknown product id(s) {unknown}; valid: {', '.join(PRODUCT_IDS)} or *")
    return products


def parse_expires(raw: str | None) -> str | None:
    if raw is None:
        return None
    try:
        return date.fromisoformat(raw.strip()).isoformat()
    except ValueError:
        die(f"--expires must be a YYYY-MM-DD date, got {raw!r}")


def entry_for(name: str, products: list[str] | None, expires: str | None = None):
    if expires:
        return {"name": name, "products": {p: expires for p in (products or ["*"])}}
    return name if products is None else {"name": name, "products": products}


def resolve_one(customers: dict, key: str) -> str:
    matches = find(customers, key)
    if not matches:
        die(f"no customer matches {key!r} (by repository secret or name)")
    if len(matches) > 1:
        listing = "\n".join(f"  {t}  {name_of(customers[t])}" for t in matches)
        die(f"{key!r} matches multiple customers — use the repository secret instead:\n{listing}")
    return matches[0]


def cmd_add(args) -> None:
    customers, sha = fetch()
    products = parse_products(args.products)
    expires = parse_expires(args.expires)
    token = "edc_" + secrets.token_urlsafe(18)
    customers[token] = entry_for(args.name, products, expires)
    save(customers, sha, f"Add customer: {args.name}")
    shown = ", ".join(products) if products else "all products"
    if expires:
        shown += f", updates through {expires}"
    print(f"Added {args.name} ({shown}). Live on the gateway within a minute.")
    print(f"\n  Repository secret (send to customer, shown only once):\n\n    {token}\n")
    print("Blender setup for the customer: Preferences > Get Extensions >")
    print("Repositories > + Add Remote Repository >")
    print("  URL:    https://extensions.edccorp.com/index.json")
    print("  tick 'Requires Access Token', paste the repository secret into Secret")


def cmd_list(args) -> None:
    customers, _ = fetch()
    if not customers:
        print("no customers yet")
        return
    width = max(len(name_of(v)) for v in customers.values())
    for token, value in sorted(customers.items(), key=lambda kv: name_of(kv[1]).lower()):
        print(f"{name_of(value):<{width}}  {token}  {', '.join(products_of(value))}")


def cmd_set_products(args) -> None:
    customers, sha = fetch()
    token = resolve_one(customers, args.customer)
    name = name_of(customers[token])
    products = parse_products(args.products)
    expires = parse_expires(args.expires)
    if isinstance(customers[token], dict):  # keep email/stripe_sessions
        if expires:
            customers[token]["products"] = {p: expires for p in (products or ["*"])}
        else:
            customers[token]["products"] = products or ["*"]
    else:
        customers[token] = entry_for(name, products, expires)
    save(customers, sha, f"Set products for {name}: {args.products or '*'}")
    shown = ", ".join(products_of(customers[token]))
    print(f"{name} now has repository access to: {shown} (live within a minute)")


def cmd_revoke(args) -> None:
    customers, sha = fetch()
    token = resolve_one(customers, args.customer)
    name = name_of(customers.pop(token))
    save(customers, sha, f"Revoke customer: {name}")
    print(f"Revoked {name}. Their Blender loses access within a minute;")
    print("already-installed add-ons keep working but stop updating.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("add", help="add a customer and print their new repository secret")
    p.add_argument("name", help='customer label, e.g. "Acme Reconstruction LLC"')
    p.add_argument("--products", help=f"comma-separated ids ({', '.join(PRODUCT_IDS)}); omit for all")
    p.add_argument("--expires", help="YYYY-MM-DD — updates stop after this date; omit for perpetual")
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("list", help="list customers, repository secrets, and included products")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("set-products", help="change the products included with a customer's repository access")
    p.add_argument("customer", help="customer name or repository secret")
    p.add_argument("--products", help="comma-separated ids, or * for all")
    p.add_argument("--expires", help="YYYY-MM-DD — updates stop after this date; omit for perpetual")
    p.set_defaults(func=cmd_set_products)

    p = sub.add_parser("revoke", help="remove a customer's access")
    p.add_argument("customer", help="customer name or repository secret")
    p.set_defaults(func=cmd_revoke)

    args = parser.parse_args()
    if not ADMIN_TOKEN:
        die("set CUSTOMERS_ADMIN_TOKEN to a fine-grained PAT with "
            f"Contents: read & write on {REPO}")
    args.func(args)


if __name__ == "__main__":
    main()
