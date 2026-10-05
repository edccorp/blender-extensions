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
    python tools/customer.py add "Acme Reconstruction LLC" --email buyer@acme.com
    python tools/customer.py add "Smith Engineering" --products recon_toolkit,point_cloud_toolkit
    python tools/customer.py list
    python tools/customer.py needs-email
    python tools/customer.py set-email "Acme Reconstruction LLC" buyer@acme.com
    python tools/customer.py show buyer@acme.com
    python tools/customer.py reissue "Acme Reconstruction LLC"
    python tools/customer.py set-products "Smith Engineering" --products "*"
    python tools/customer.py revoke "Acme Reconstruction LLC"

A customer who has lost their secret is looked up with `show`, by name,
email or the secret itself — the gateway deliberately refuses to re-display
one to anyone who only proved they can type an email address, and points
them here instead. Use `reissue` when the secret may have been seen by
somebody else rather than merely mislaid: sending the same one back cannot
help when the problem is who else has it.
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

#: Internal and beta tools. A "*" does not reach these -- the gateway
#: requires them to be named -- so they are grantable but never implied,
#: which is how the master account and a named beta tester get one and
#: nobody else does. Keep in step with RESTRICTED_PRODUCTS in gateway/main.py.
RESTRICTED_IDS = ("video_forensics_toolkit", "audio_forensics_toolkit",
                  "visibility_toolkit", "recon_calculations", "blendmotion")

GRANTABLE_IDS = PRODUCT_IDS + RESTRICTED_IDS


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


def email_of(value) -> str:
    """The address on an entry, or "" for the compact plain-string form."""
    return "" if isinstance(value, str) else (value.get("email") or "")


def products_of(value) -> list[str]:
    """Display form: 'recon_toolkit (through 2027-07-03)' for dated terms."""
    if isinstance(value, str):
        return ["*"]
    products = value.get("products") or ["*"]
    if isinstance(products, dict):
        return [f"{p} (through {exp})" if exp else p for p, exp in products.items()]
    return list(products)


def find(customers: dict, key: str) -> list[str]:
    """Match by exact repository secret, then customer name, then email.

    Email is here because it is what a support request arrives as: someone
    who has lost their secret writes in from the address they bought with,
    and looking them up should not mean scrolling a list for a name they
    may have spelled differently.
    """
    if key in customers:
        return [key]
    wanted = key.lower()
    by_name = [t for t, v in customers.items() if name_of(v).lower() == wanted]
    if by_name:
        return by_name
    return [t for t, v in customers.items() if email_of(v).lower() == wanted]


def parse_products(raw: str | None) -> list[str] | None:
    """None means all published products (the compact plain-string form).

    ``*`` may be combined with named products -- ``*,video_forensics_toolkit``
    -- which is the master account's shape: everything published, plus the
    internal tools a wildcard deliberately does not reach.
    """
    if raw is None or raw.strip() in ("", "*"):
        return None
    products = [p.strip() for p in raw.split(",") if p.strip()]
    unknown = [p for p in products if p != "*" and p not in GRANTABLE_IDS]
    if unknown:
        die(f"unknown product id(s) {unknown}; valid: "
            f"{', '.join(GRANTABLE_IDS)}, or * for the published four")
    return products


def parse_expires(raw: str | None) -> str | None:
    if raw is None:
        return None
    try:
        return date.fromisoformat(raw.strip()).isoformat()
    except ValueError:
        die(f"--expires must be a YYYY-MM-DD date, got {raw!r}")


def entry_for(name: str, products: list[str] | None, expires: str | None = None,
              email: str | None = None):
    """A customer record: the compact plain string, or a dict when it needs one.

    An email is worth having on every customer now that the gateway can
    send: it is what lets them recover their own secret at /recover instead
    of writing in. Entries made before this carry no address, which is why
    there is a set-email command as well.
    """
    if not (products or expires or email):
        return name          # a name and nothing else stays a plain string
    entry: dict = {"name": name}
    if email:
        entry["email"] = email
    if expires:
        entry["products"] = {p: expires for p in (products or ["*"])}
    elif products is not None:
        entry["products"] = products
    return entry


def resolve_one(customers: dict, key: str) -> str:
    matches = find(customers, key)
    if not matches:
        die(f"no customer matches {key!r} (by repository secret, name or email)")
    if len(matches) > 1:
        listing = "\n".join(f"  {t}  {name_of(customers[t])}" for t in matches)
        die(f"{key!r} matches multiple customers — use the repository secret instead:\n{listing}")
    return matches[0]


def setup_lines(token: str) -> str:
    """What the customer needs in Blender, ready to paste into a reply."""
    return (
        f"\n  Repository secret:\n\n    {token}\n\n"
        "Blender setup for the customer: Preferences > Get Extensions >\n"
        "Repositories > + Add Remote Repository >\n"
        "  URL:    https://extensions.edccorp.com/index.json\n"
        "  tick 'Requires Access Token', paste the repository secret into Secret"
    )


def cmd_add(args) -> None:
    customers, sha = fetch()
    products = parse_products(args.products)
    expires = parse_expires(args.expires)
    token = "edc_" + secrets.token_urlsafe(18)
    email = (args.email or "").strip()
    customers[token] = entry_for(args.name, products, expires, email)
    save(customers, sha, f"Add customer: {args.name}")
    shown = ", ".join(products) if products else "all products"
    if expires:
        shown += f", updates through {expires}"
    print(f"Added {args.name} ({shown}). Live on the gateway within a minute.")
    if not email:
        # Without one they cannot use /recover, and a lost secret comes
        # back here as a support request instead of answering itself.
        print("No email recorded — they will not be able to recover this "
              "secret themselves. Add one with: customer.py set-email "
              f"{args.name!r} <address>")
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


def cmd_show(args) -> None:
    """Look one customer up, for when they write in having lost their secret.

    The gateway refuses to re-display a secret to anyone who only proved
    they can type an email address, and tells them to contact Engineering
    Dynamics Company instead. This is what answers that: one customer, not
    the whole list, so a support reply does not begin by putting every
    other customer's secret on screen.
    """
    customers, _ = fetch()
    token = resolve_one(customers, args.customer)
    value = customers[token]
    print(f"{name_of(value)}")
    if email_of(value):
        print(f"  email:    {email_of(value)}")
    print(f"  products: {', '.join(products_of(value))}")
    print(setup_lines(token))


def cmd_needs_email(args) -> None:
    """The customers with no address on file, which is the backfill list.

    They are the ones /recover cannot help: it finds nothing for them and
    answers "on its way" anyway -- it has to, or it becomes a way to ask
    which firms buy from us one address at a time -- so nothing arrives and
    nothing says why. Every entry made before the gateway could send is in
    this state.

    Names only, deliberately. Working through this list does not need a
    single repository secret on screen, and `list` putting them all there is
    what `show` exists to avoid.
    """
    customers, _ = fetch()
    missing = sorted((name_of(v) for v in customers.values() if not email_of(v)),
                     key=str.lower)
    if not customers:
        print("no customers yet")
        return
    if not missing:
        print(f"All {len(customers)} customers have an address on file — "
              "every one of them can use /recover.")
        return
    print(f"{len(missing)} of {len(customers)} customers have no address on "
          "file and cannot recover their own secret:\n")
    for name in missing:
        print(f"  {name}")
    print("\nAdd one as you learn it:  customer.py set-email <name> <address>")


def cmd_set_email(args) -> None:
    """Record an address for a customer who has none.

    Entries made before the gateway could send carry no address at all, and
    a customer without one cannot recover their own secret -- /recover
    finds nothing for them and they are back to writing in. This is how
    those get filled in as you learn them.
    """
    customers, sha = fetch()
    token = resolve_one(customers, args.customer)
    email = args.email.strip()
    if "@" not in email:
        die(f"{email!r} is not an email address")
    value = customers[token]
    if isinstance(value, str):
        # The compact form is a name and nothing else; it has to become a
        # record before it can hold anything.
        value = {"name": value}
        customers[token] = value
    previous = value.get("email", "")
    value["email"] = email
    save(customers, sha, f"Set email for {name_of(value)}: {email}")
    if previous and previous.lower() != email.lower():
        print(f"Replaced {previous} with {email} for {name_of(value)}.")
    else:
        print(f"{name_of(value)} can now recover their own secret at "
              f"/recover using {email}.")


def cmd_reissue(args) -> None:
    """Replace a customer's secret, keeping everything else about them.

    For a secret that may have been seen by someone else rather than merely
    mislaid -- a forwarded email, a shared screen. Sending the same one
    back cannot help there, because the problem is who else has it.

    The entry is re-keyed rather than rebuilt, so an email address, a
    purchase history and a dated term survive the change; rebuilding it
    from a name and products would quietly drop all three.
    """
    customers, sha = fetch()
    token = resolve_one(customers, args.customer)
    value = customers.pop(token)
    new_token = "edc_" + secrets.token_urlsafe(18)
    customers[new_token] = value
    save(customers, sha, f"Reissue repository secret: {name_of(value)}")
    print(f"Reissued for {name_of(value)}. The old secret stops working "
          "within a minute;")
    print("their Blender will report an access error until the new one is in.")
    print(setup_lines(new_token))


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
    p.add_argument("--email", help="their address; without it they cannot recover "
                                   "their own secret at /recover")
    p.add_argument("--products", help=f"comma-separated ids ({', '.join(PRODUCT_IDS)}); "
                                      f"omit for all published. Internal tools "
                                      f"({', '.join(RESTRICTED_IDS)}) must be named "
                                      f"outright, and can follow a * ")
    p.add_argument("--expires", help="YYYY-MM-DD — updates stop after this date; omit for perpetual")
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("list", help="list customers, repository secrets, and included products")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("set-products", help="change the products included with a customer's repository access")
    p.add_argument("customer", help="customer name, email, or repository secret")
    p.add_argument("--products", help="comma-separated ids, or * for all")
    p.add_argument("--expires", help="YYYY-MM-DD — updates stop after this date; omit for perpetual")
    p.set_defaults(func=cmd_set_products)

    p = sub.add_parser("needs-email", help="list customers with no address on file (the /recover backfill)")
    p.set_defaults(func=cmd_needs_email)

    p = sub.add_parser("set-email", help="record a customer's address so they can use /recover")
    p.add_argument("customer", help="customer name, email, or repository secret")
    p.add_argument("email", help="the address they bought with")
    p.set_defaults(func=cmd_set_email)

    p = sub.add_parser("show", help="show one customer's repository secret (lost-secret requests)")
    p.add_argument("customer", help="customer name, email, or repository secret")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("reissue", help="give a customer a new repository secret and retire the old one")
    p.add_argument("customer", help="customer name, email, or repository secret")
    p.set_defaults(func=cmd_reissue)

    p = sub.add_parser("revoke", help="remove a customer's access")
    p.add_argument("customer", help="customer name, email, or repository secret")
    p.set_defaults(func=cmd_revoke)

    args = parser.parse_args()
    if not ADMIN_TOKEN:
        die("set CUSTOMERS_ADMIN_TOKEN to a fine-grained PAT with "
            f"Contents: read & write on {REPO}")
    args.func(args)


if __name__ == "__main__":
    main()
