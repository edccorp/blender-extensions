#!/usr/bin/env python3
"""Build the EDC Software extensions repository index for Blender.

For each product repo, fetches the latest GitHub release, downloads its
add-on zip, reads the ``blender_manifest.toml`` inside it (the manifest is
the source of truth for id/name/version), and writes:

    <out>/index.json      Blender remote-repository listing (schema "v1")
    <out>/index.html      Human-readable landing page with instructions
    <out>/packages/*.zip  The add-on zips (when MIRROR_ZIPS is on)

Zips are mirrored onto the published site by default so customers can
download them even while the product repos are private. Set MIRROR_ZIPS=0
to instead point archive_url at the GitHub release assets directly (only
useful once the product repos are public).

Products whose latest release has no manifest in the zip (releases cut
before extension support) are skipped with a warning and appear once a
new release is published.

Usage:  python tools/build_index.py [out_dir]        # default: site/

GITHUB_TOKEN must be able to read the product repos (while they are
private, the default workflow token cannot — CI uses the PRODUCTS_TOKEN
secret when it is configured).
"""

import hashlib
import html
import io
import json
import os
import shutil
import sys
import tomllib
import urllib.request
import zipfile

CONTENT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "content")

PRODUCTS = [
    "edccorp/CamMatch",
    "edccorp/HVEToolkit",
    "edccorp/PointCloudToolkit",
    "edccorp/ReconToolkit",
]

PAGES_BASE = "https://extensions.edccorp.com"
REPOSITORY_URL = f"{PAGES_BASE}/index.json"
STORE_URL = f"{PAGES_BASE}/store"

MIRROR_ZIPS = os.environ.get("MIRROR_ZIPS", "1").lower() not in ("0", "false", "no")

# Manifest fields copied into each index entry when present.
MANIFEST_FIELDS = (
    "schema_version",
    "id",
    "name",
    "tagline",
    "version",
    "type",
    "maintainer",
    "license",
    "blender_version_min",
    "blender_version_max",
    "website",
    "tags",
    "platforms",
    "permissions",
)


def _fetch(url, accept="application/vnd.github+json"):
    req = urllib.request.Request(url, headers={"Accept": accept})
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req) as resp:
        return resp.read()


def _latest_release(repo):
    try:
        return json.loads(_fetch(f"https://api.github.com/repos/{repo}/releases/latest"))
    except urllib.error.HTTPError as err:
        if err.code == 404:
            return None
        raise


def _manifest_from_zip(data):
    """Return the parsed blender_manifest.toml from the zip, or None."""
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for name in zf.namelist():
            parts = name.split("/")
            if parts[-1] == "blender_manifest.toml" and len(parts) <= 2:
                return tomllib.loads(zf.read(name).decode("utf-8"))
    return None


def build_entries(out_dir):
    entries = []
    packages = {}
    releases = {}  # product id -> {"version", "notes", "published"} for pages
    for repo in PRODUCTS:
        release = _latest_release(repo)
        if release is None:
            print(f"SKIP {repo}: no releases (or no access with this token)")
            continue
        asset = next(
            (a for a in release.get("assets", []) if a["name"].endswith(".zip")),
            None,
        )
        if asset is None:
            print(f"SKIP {repo}: latest release ({release.get('tag_name')}) has no zip asset")
            continue
        # The API asset endpoint works for private repos too (the public
        # browser_download_url does not).
        data = _fetch(asset["url"], accept="application/octet-stream")
        manifest = _manifest_from_zip(data)
        if manifest is None:
            print(
                f"SKIP {repo}: {asset['name']} has no blender_manifest.toml "
                f"(release predates extension support; cut a new release)"
            )
            continue
        entry = {k: manifest[k] for k in MANIFEST_FIELDS if k in manifest}
        if MIRROR_ZIPS:
            # Public mode: the zips are copied onto the Pages site itself.
            pkg_dir = os.path.join(out_dir, "packages")
            os.makedirs(pkg_dir, exist_ok=True)
            with open(os.path.join(pkg_dir, asset["name"]), "wb") as fh:
                fh.write(data)
        # In gateway mode (MIRROR_ZIPS=0) the same /packages/ URL is served by
        # the authenticated Railway gateway, which resolves the download via
        # packages.json and streams the private GitHub release asset.
        entry["archive_url"] = f"{PAGES_BASE}/packages/{asset['name']}"
        entry["archive_size"] = len(data)
        entry["archive_hash"] = "sha256:" + hashlib.sha256(data).hexdigest()
        packages[asset["name"]] = {
            "id": manifest.get("id", ""),
            "repo": repo,
            "asset_id": asset["id"],
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
        releases[manifest.get("id", "")] = {
            "version": manifest.get("version", ""),
            "notes": release.get("body") or "",
            "published": (release.get("published_at") or "")[:10],
        }
        entries.append(entry)
        print(f"OK   {repo}: {entry['id']} {entry['version']} ({asset['name']})")
    return sorted(entries, key=lambda e: e["name"].lower()), packages, releases


SHARED_CSS = """
:root {
  color-scheme: light dark;
  --accent: #1a6fb5; --accent2: #b85c00;
  --card: rgba(127,127,127,.08); --border: rgba(127,127,127,.25);
}
* { box-sizing: border-box; }
body { font-family: system-ui, -apple-system, "Segoe UI", sans-serif; line-height: 1.6;
       margin: 0; padding: 0 1.25rem 4rem; }
main { max-width: 960px; margin: 0 auto; }
header.hero { text-align: center; padding: 3.5rem 0 2rem; }
header.hero h1 { font-size: 2.2rem; margin: 0 0 .3rem; letter-spacing: .01em; }
header.hero .sub { font-size: 1.15rem; opacity: .75; margin: 0; }
h2 { margin-top: 2.5rem; border-bottom: 1px solid var(--border); padding-bottom: .3rem; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
        gap: 1rem; margin-top: 1.5rem; }
.card { background: var(--card); border: 1px solid var(--border); border-radius: 10px;
        padding: 1.1rem 1.25rem; display: flex; flex-direction: column; }
.card h3 { margin: 0 0 .2rem; font-size: 1.2rem; }
.card .tm { font-size: .7em; vertical-align: super; opacity: .7; }
.card .tagline { margin: 0 0 .5rem; font-weight: 600; font-size: .92rem; color: var(--accent); }
.card .desc { margin: 0 0 .7rem; font-size: .9rem; opacity: .85; }
.card .meta { display: flex; gap: .8rem; font-size: .82rem; opacity: .7; margin-bottom: .7rem; }
.card .actions { margin-top: auto; display: flex; align-items: center; gap: .9rem; }
.card .price { font-weight: 700; }
.price .term { font-size: .68em; font-weight: 400; opacity: .7; white-space: nowrap; }
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
a.btn { display: inline-block; background: var(--accent); color: #fff; border-radius: 8px;
        padding: .45rem 1.1rem; font-weight: 600; }
a.btn:hover { text-decoration: none; filter: brightness(1.1); }
a.btn.ghost { background: transparent; color: var(--accent); border: 1px solid var(--accent); }
ol li, ul li { margin: .35rem 0; }
code { background: rgba(127,127,127,.15); padding: .12em .4em; border-radius: 5px; }
.repo-url { display: block; text-align: center; font-size: 1.05rem; margin: 1rem 0;
            padding: .6rem; background: rgba(127,127,127,.1); border-radius: 8px; }
footer { margin-top: 3.5rem; padding-top: 1rem; border-top: 1px solid var(--border);
         font-size: .88rem; opacity: .75; text-align: center; }
"""

FOOTER_HTML = """<footer>
\u00a9 Engineering Dynamics Company. CamMatch\u2122, HVE Toolkit\u2122,
Point Cloud Toolkit\u2122, and Recon Toolkit\u2122 are trademarks of
Engineering Dynamics Company. Blender\u00ae is a registered trademark of the
Blender Foundation. The software is free software under the GNU GPL;
see each product's repository for license details.
</footer>"""


def load_products_content():
    """{product id: parsed toml} for every file in content/products/."""
    content = {}
    products_dir = os.path.join(CONTENT_DIR, "products")
    if not os.path.isdir(products_dir):
        return content
    for fname in sorted(os.listdir(products_dir)):
        if fname.endswith(".toml"):
            with open(os.path.join(products_dir, fname), "rb") as fh:
                content[fname[:-5]] = tomllib.load(fh)
    return content


# Shown next to every paid price. Keep in sync with the license term the
# gateway stamps (LICENSE_TERM_DAYS) and the "Includes" list on the pages.
UPDATE_TERM_SHORT = "1‑yr updates"


def _buy_actions(pid, price, big=False):
    if price and price.strip().lower() == "free":
        # /checkout sends free-only selections to the /register form.
        return ('<span class="price">Free</span>\n'
                f'<a class="btn" href="{PAGES_BASE}/checkout?products={pid}">Get it free</a>')
    if price:
        return (f'<span class="price">{html.escape(price)}'
                f'<span class="term"> · {UPDATE_TERM_SHORT}</span></span>\n'
                f'<a class="btn" href="{PAGES_BASE}/checkout?products={pid}">Buy now</a>')
    return f'<a class="btn ghost" href="{STORE_URL}">See the store</a>'


DISPLAY_ORDER = ["cammatch", "point_cloud_toolkit", "recon_toolkit", "hve_toolkit"]


def render_landing_page(entries, content):
    def order(e):
        return DISPLAY_ORDER.index(e["id"]) if e["id"] in DISPLAY_ORDER else len(DISPLAY_ORDER)

    cards = []
    for e in sorted(entries, key=order):
        pid = e["id"]
        c = content.get(pid, {})
        learn = (f'<a href="products/{pid}.html">Learn more \u2192</a>'
                 if pid in content else
                 f'<a href="{html.escape(e.get("website", "#"))}">Documentation</a>')
        cards.append(f"""<div class="card">
  <h3>{html.escape(c.get('name', e['name']))}</h3>
  <p class="tagline">{html.escape(c.get('tagline', e.get('tagline', '')))}</p>
  <p class="desc">{html.escape(e.get('tagline', ''))}</p>
  <div class="meta">
    <span>v{html.escape(e['version'])}</span>
    <span>Blender {html.escape(e.get('blender_version_min', '4.2.0'))}+</span>
  </div>
  <p class="desc">{learn}</p>
  <div class="actions">{_buy_actions(pid, c.get('price', ''))}</div>
</div>""")
    cards = "\n".join(cards) or "<p>No published products yet.</p>"
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>EDC Software \u2014 Blender Extensions</title>
<style>{SHARED_CSS}</style>
</head>
<body>
<main>
<header class="hero">
  <h1>EDC Software</h1>
  <p class="sub">Professional Blender tools by Engineering Dynamics Company</p>
  <p style="margin-top:1.2rem"><a class="btn" href="{STORE_URL}">Build your bundle \u2192</a></p>
</header>

<div class="grid">
{cards}
</div>

<h2 id="install">Install &amp; automatic updates</h2>
<p>Add the EDC Software repository to Blender once, and every product installs
from the extensions list and updates automatically:</p>
<ol>
<li>In Blender (4.2 or later), open <strong>Edit \u2192 Preferences \u2192 Get Extensions</strong>.</li>
<li>Open the <strong>Repositories</strong> dropdown (top right) and choose <strong>+ \u2192 Add Remote Repository</strong>.</li>
<li>Paste the repository URL and name it <strong>EDC Software</strong>:</li>
</ol>
<code class="repo-url">{REPOSITORY_URL}</code>
<p>The EDC products then appear under <em>Available</em> to install, and Blender
notifies you when updates are published. Direct zip downloads are linked on
each product card above for offline installs
(<strong>Preferences \u2192 Get Extensions \u2192 Install from Disk</strong>).</p>

<h2 id="support">Support</h2>
<p>Official builds, updates, training, and support for these products are
provided by <strong>Engineering Dynamics Company</strong> to its customers.
Visit <a href="https://www.edccorp.com">edccorp.com</a> or contact EDC support
for licensing, training, and assistance.</p>

{FOOTER_HTML}
</main>
</body>
</html>
"""


def render_product_page(pid, c, release):
    """One product page from its content/products/<pid>.toml."""
    def esc(key, default=""):
        return html.escape(str(c.get(key, default)))

    def paragraphs(text):
        return "\n".join(f"<p>{html.escape(p.strip())}</p>"
                         for p in text.split("\n\n") if p.strip())

    hero_img = ""
    if c.get("hero_image"):
        hero_img = (f'<img class="hero-img" src="../assets/{html.escape(c["hero_image"])}" '
                    f'alt="{esc("name")}">')
    features = "\n".join(f"<li>{html.escape(f)}</li>" for f in c.get("features", []))
    includes = "\n".join(f"<li>{html.escape(i)}</li>" for i in c.get("includes", []))
    requirements = "\n".join(f"<li>{html.escape(r)}</li>" for r in c.get("requirements", []))
    sections = "\n".join(
        f"<h2>{html.escape(s.get('title', ''))}</h2>\n{paragraphs(s.get('body', ''))}"
        for s in c.get("sections", [])
    )
    version_box = ""
    if release:
        # No release-notes body: these are the first public releases, and the
        # GitHub notes reference pre-launch internal work.
        version_box = f"""<h2>Latest version</h2>
<p><strong>v{html.escape(release['version'])}</strong>
&middot; delivered through the <a href="{PAGES_BASE}/">EDC Software repository</a>
with automatic update notifications in Blender.</p>"""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc('name')} \u2014 EDC Software</title>
<style>{SHARED_CSS}
.crumb {{ margin: 1.4rem 0 0; font-size: .9rem; }}
.phero {{ display: grid; grid-template-columns: 1.1fr .9fr; gap: 2rem;
          align-items: center; padding: 2rem 0 1rem; }}
@media (max-width: 720px) {{ .phero {{ grid-template-columns: 1fr; }} }}
.phero h1 {{ font-size: 2rem; margin: .2rem 0 .4rem; }}
.badge {{ display: inline-block; font-size: .8rem; font-weight: 600; letter-spacing: .03em;
         color: var(--accent); border: 1px solid var(--accent); border-radius: 999px;
         padding: .15rem .7rem; }}
.phero .tagline {{ font-size: 1.15rem; font-weight: 600; margin: 0 0 .8rem; opacity: .85; }}
.phero .actions {{ display: flex; align-items: center; gap: 1rem; margin-top: 1.2rem; }}
.phero .price {{ font-size: 1.5rem; font-weight: 700; }}
.hero-img {{ width: 100%; border-radius: 12px; border: 1px solid var(--border); }}
ul.checks {{ list-style: none; padding: 0; columns: 2; column-gap: 2rem; }}
@media (max-width: 720px) {{ ul.checks {{ columns: 1; }} }}
ul.checks li {{ break-inside: avoid; padding-left: 1.4rem; position: relative; }}
ul.checks li::before {{ content: "\u2713"; position: absolute; left: 0; color: var(--accent);
                        font-weight: 700; }}
.includes {{ background: var(--card); border: 1px solid var(--border); border-radius: 10px;
            padding: 1.1rem 1.5rem; }}
</style>
</head>
<body>
<main>
<p class="crumb"><a href="../">\u2190 EDC Software</a></p>
<div class="phero">
  <div>
    <span class="badge">{esc('badge')}</span>
    <h1>{esc('name')}</h1>
    <p class="tagline">{esc('tagline')}</p>
    {paragraphs(c.get('pitch', ''))}
    <div class="actions">{_buy_actions(pid, c.get('price', ''), big=True)}</div>
  </div>
  <div>{hero_img}</div>
</div>

<h2>Capabilities</h2>
<ul class="checks">
{features}
</ul>

<h2>Your purchase includes</h2>
<div class="includes"><ul class="checks">
{includes}
</ul></div>

{sections}

<h2>Requirements</h2>
<ul>
{requirements}
</ul>

{version_box}

<h2>Install &amp; updates</h2>
<p>Purchases are delivered instantly: pay, receive your access token on the
confirmation page, and add the EDC Software repository to Blender once \u2014
your products install from the extensions list and update automatically.
See <a href="{PAGES_BASE}/#install">setup instructions</a>. Installed
versions keep working even after an update term ends.</p>

{FOOTER_HTML}
</main>
</body>
</html>
"""


def main():
    out_dir = sys.argv[1] if len(sys.argv) > 1 else "site"
    os.makedirs(out_dir, exist_ok=True)
    entries, packages, releases = build_entries(out_dir)
    content = load_products_content()
    index = {"version": "v1", "blocklist": [], "data": entries}
    with open(os.path.join(out_dir, "index.json"), "w", encoding="utf-8") as fh:
        json.dump(index, fh, indent=2)
        fh.write("\n")
    with open(os.path.join(out_dir, "packages.json"), "w", encoding="utf-8") as fh:
        json.dump(packages, fh, indent=2)
        fh.write("\n")
    with open(os.path.join(out_dir, "index.html"), "w", encoding="utf-8") as fh:
        fh.write(render_landing_page(entries, content))

    pages_dir = os.path.join(out_dir, "products")
    os.makedirs(pages_dir, exist_ok=True)
    for pid, c in content.items():
        with open(os.path.join(pages_dir, f"{pid}.html"), "w", encoding="utf-8") as fh:
            fh.write(render_product_page(pid, c, releases.get(pid)))
    assets_src = os.path.join(CONTENT_DIR, "assets")
    if os.path.isdir(assets_src):
        shutil.copytree(assets_src, os.path.join(out_dir, "assets"), dirs_exist_ok=True)
    print(
        f"Wrote {out_dir}/index.json ({len(entries)} extension(s)), index.html, "
        f"and {len(content)} product page(s)"
    )


if __name__ == "__main__":
    main()
