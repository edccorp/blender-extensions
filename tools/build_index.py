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
            pkg_dir = os.path.join(out_dir, "packages")
            os.makedirs(pkg_dir, exist_ok=True)
            with open(os.path.join(pkg_dir, asset["name"]), "wb") as fh:
                fh.write(data)
            entry["archive_url"] = f"{PAGES_BASE}/packages/{asset['name']}"
        else:
            entry["archive_url"] = asset["browser_download_url"]
        entry["archive_size"] = len(data)
        entry["archive_hash"] = "sha256:" + hashlib.sha256(data).hexdigest()
        entries.append(entry)
        print(f"OK   {repo}: {entry['id']} {entry['version']} ({asset['name']})")
    return sorted(entries, key=lambda e: e["name"].lower())


def render_landing_page(entries):
    rows = "\n".join(
        f"<tr><td><a href='{html.escape(e.get('website', '#'))}'>{html.escape(e['name'])}</a></td>"
        f"<td>{html.escape(e['version'])}</td>"
        f"<td>{html.escape(e.get('tagline', ''))}</td>"
        f"<td><a href='{html.escape(e['archive_url'])}'>zip</a></td></tr>"
        for e in entries
    ) or "<tr><td colspan='4'>No published extensions yet.</td></tr>"
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>EDC Software — Blender Extensions</title>
<style>
:root {{ color-scheme: light dark; }}
body {{ font-family: system-ui, sans-serif; line-height: 1.6; max-width: 800px;
       margin: 0 auto; padding: 2rem 1.25rem 4rem; }}
code {{ background: rgba(127,127,127,.15); padding: .12em .35em; border-radius: 4px; }}
table {{ border-collapse: collapse; width: 100%; margin: 1rem 0; }}
th, td {{ border: 1px solid rgba(127,127,127,.4); padding: .5rem .6rem; text-align: left; }}
</style>
</head>
<body>
<h1>EDC Software — Blender Extensions</h1>
<p>Extensions repository for the Blender products of
<strong>Engineering Dynamics Company</strong>. Add it once and Blender
offers updates for every EDC product automatically.</p>
<h2>Add to Blender (4.2+)</h2>
<ol>
<li>Open <strong>Edit → Preferences → Get Extensions</strong>.</li>
<li>Open the <strong>Repositories</strong> dropdown (top-right) and click <strong>+ → Add Remote Repository</strong>.</li>
<li>Paste this URL: <code>{REPOSITORY_URL}</code></li>
<li>Name it <strong>EDC Software</strong> and confirm.</li>
</ol>
<p>The EDC products then appear in the extensions list to install, and
Blender notifies you when updates are available.</p>
<h2>Available extensions</h2>
<table>
<tr><th>Product</th><th>Version</th><th>Description</th><th>Download</th></tr>
{rows}
</table>
</body>
</html>
"""


def main():
    out_dir = sys.argv[1] if len(sys.argv) > 1 else "site"
    os.makedirs(out_dir, exist_ok=True)
    entries = build_entries(out_dir)
    index = {"version": "v1", "blocklist": [], "data": entries}
    with open(os.path.join(out_dir, "index.json"), "w", encoding="utf-8") as fh:
        json.dump(index, fh, indent=2)
        fh.write("\n")
    with open(os.path.join(out_dir, "index.html"), "w", encoding="utf-8") as fh:
        fh.write(render_landing_page(entries))
    print(f"Wrote {out_dir}/index.json ({len(entries)} extension(s)) and {out_dir}/index.html")


if __name__ == "__main__":
    main()
