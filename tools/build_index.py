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
import sys
import tomllib
import urllib.request
import zipfile

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
        entries.append(entry)
        print(f"OK   {repo}: {entry['id']} {entry['version']} ({asset['name']})")
    return sorted(entries, key=lambda e: e["name"].lower()), packages


def render_landing_page(entries):
    taglines = {
        "cammatch": "Professional Camera Matching for Blender",
        "hve_toolkit": "Simulation & Workflow Tools for Blender",
        "point_cloud_toolkit": "Point Cloud Processing & Visualization for Blender",
        "recon_toolkit": "Accident Reconstruction Tools for Blender",
    }
    cards = "\n".join(
        f"""<div class="card">
  <h3>{html.escape(e['name'])}<span class="tm">\u2122</span></h3>
  <p class="tagline">{html.escape(taglines.get(e['id'], e.get('tagline', '')))}</p>
  <p class="desc">{html.escape(e.get('tagline', ''))}</p>
  <div class="meta">
    <span>v{html.escape(e['version'])}</span>
    <span>Blender {html.escape(e.get('blender_version_min', '4.2.0'))}+</span>
  </div>
  <p class="links"><a href="{html.escape(e.get('website', '#'))}">Documentation</a>
     &middot; <a href="{STORE_URL}">Get access</a></p>
</div>"""
        for e in entries
    ) or "<p>No published products yet.</p>"
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>EDC Software \u2014 Blender Extensions</title>
<style>
:root {{
  color-scheme: light dark;
  --accent: #1a6fb5; --accent2: #b85c00;
  --card: rgba(127,127,127,.08); --border: rgba(127,127,127,.25);
}}
* {{ box-sizing: border-box; }}
body {{ font-family: system-ui, -apple-system, "Segoe UI", sans-serif; line-height: 1.6;
       margin: 0; padding: 0 1.25rem 4rem; }}
main {{ max-width: 960px; margin: 0 auto; }}
header.hero {{ text-align: center; padding: 3.5rem 0 2rem; }}
header.hero h1 {{ font-size: 2.2rem; margin: 0 0 .3rem; letter-spacing: .01em; }}
header.hero .sub {{ font-size: 1.15rem; opacity: .75; margin: 0; }}
h2 {{ margin-top: 2.5rem; border-bottom: 1px solid var(--border); padding-bottom: .3rem; }}
.grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
        gap: 1rem; margin-top: 1.5rem; }}
.card {{ background: var(--card); border: 1px solid var(--border); border-radius: 10px;
        padding: 1.1rem 1.25rem; }}
.card h3 {{ margin: 0 0 .2rem; font-size: 1.2rem; }}
.card .tm {{ font-size: .7em; vertical-align: super; opacity: .7; }}
.card .tagline {{ margin: 0 0 .5rem; font-weight: 600; font-size: .92rem; color: var(--accent); }}
.card .desc {{ margin: 0 0 .7rem; font-size: .9rem; opacity: .85; }}
.card .meta {{ display: flex; gap: .8rem; font-size: .82rem; opacity: .7; margin-bottom: .5rem; }}
.card .links {{ margin: 0; font-size: .9rem; }}
a {{ color: var(--accent); text-decoration: none; }}
a:hover {{ text-decoration: underline; }}
.cta {{ display: inline-block; margin-top: 1rem; padding: .6rem 1.4rem; background: var(--accent);
       color: #fff; border-radius: 8px; font-weight: 600; }}
.cta:hover {{ text-decoration: none; opacity: .92; }}
ol li {{ margin: .35rem 0; }}
code {{ background: rgba(127,127,127,.15); padding: .12em .4em; border-radius: 5px; }}
.repo-url {{ display: block; text-align: center; font-size: 1.05rem; margin: 1rem 0;
            padding: .6rem; background: rgba(127,127,127,.1); border-radius: 8px; }}
footer {{ margin-top: 3.5rem; padding-top: 1rem; border-top: 1px solid var(--border);
         font-size: .88rem; opacity: .75; text-align: center; }}
</style>
</head>
<body>
<main>
<header class="hero">
  <h1>EDC Software</h1>
  <p class="sub">Professional Blender tools by Engineering Dynamics Company</p>
  <p><a class="cta" href="{STORE_URL}">Browse the store →</a></p>
</header>

<div class="grid">
{cards}
</div>

<h2 id="install">Get access &amp; install</h2>
<p>Purchase (or claim a free product) from the
<a href="{STORE_URL}">EDC Software store</a> \u2014 you'll receive a personal
<strong>access token</strong> on the confirmation page. Then add the repository to
Blender once, paste in your token, and every product you're licensed for installs
from the extensions list and updates automatically:</p>
<ol>
<li>In Blender (4.2 or later), open <strong>Edit \u2192 Preferences \u2192 Get Extensions</strong>.</li>
<li>Open the <strong>Repositories</strong> dropdown (top right) and choose <strong>+ \u2192 Add Remote Repository</strong>.</li>
<li>Paste the repository URL and name it <strong>EDC Software</strong>:</li>
</ol>
<code class="repo-url">{REPOSITORY_URL}</code>
<p>Enable <strong>Requires Access Token</strong> on the repository and paste your
token into the <strong>Secret</strong> field. Your licensed products then appear
under <em>Available</em> to install, and Blender notifies you when updates are
published.</p>

<h2 id="support">Support</h2>
<p>Official builds, updates, training, and support for these products are
provided by <strong>Engineering Dynamics Company</strong> to its customers.
Visit <a href="https://www.edccorp.com">edccorp.com</a> or contact EDC support
for licensing, training, and assistance.</p>

<footer>
\u00a9 Engineering Dynamics Company. CamMatch\u2122, HVE Toolkit\u2122,
Point Cloud Toolkit\u2122, and Recon Toolkit\u2122 are trademarks of
Engineering Dynamics Company. The software is free software under the GNU GPL;
see each product's repository for license details.
</footer>
</main>
</body>
</html>
"""


def main():
    out_dir = sys.argv[1] if len(sys.argv) > 1 else "site"
    os.makedirs(out_dir, exist_ok=True)
    entries, packages = build_entries(out_dir)
    index = {"version": "v1", "blocklist": [], "data": entries}
    with open(os.path.join(out_dir, "index.json"), "w", encoding="utf-8") as fh:
        json.dump(index, fh, indent=2)
        fh.write("\n")
    with open(os.path.join(out_dir, "packages.json"), "w", encoding="utf-8") as fh:
        json.dump(packages, fh, indent=2)
        fh.write("\n")
    with open(os.path.join(out_dir, "index.html"), "w", encoding="utf-8") as fh:
        fh.write(render_landing_page(entries))
    print(f"Wrote {out_dir}/index.json ({len(entries)} extension(s)) and {out_dir}/index.html")


if __name__ == "__main__":
    main()
