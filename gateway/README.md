# EDC Software extensions gateway (Railway)

Per-customer authentication for the Blender extensions channel. Blender
sends the token from the repository's **Secret** field as an
`Authorization: Bearer` header on every index fetch and download; this
FastAPI service validates it, serves the index, and streams the add-on
zips **directly from the private GitHub release assets** — no public
copies anywhere.

```
Blender ──token──▶ extensions.edccorp.com (this gateway on Railway)
                     ├── /            landing page   (public)
                     ├── /index.json  index          (token required)
                     └── /packages/*  zips           (token required,
                                                      streamed from private
                                                      GitHub releases)
```

## Deploy on Railway (one time)

1. **Create the service**: Railway → New Project → Deploy from GitHub repo →
   `edccorp/blender-extensions`. Set the service **root directory** to
   `gateway/` (Settings → Source). Railway detects Python and uses
   `railway.json` for the start command.
2. **Set environment variables** (service → Variables):
   - `GH_TOKEN` — a fine-grained PAT with **Contents: read** on the four
     product repos (same scope as the `PRODUCTS_TOKEN` Actions secret).
   - `CUSTOMER_TOKENS` — JSON mapping token → customer. A plain label
     entitles the token to **every** product; an object form scopes it to
     specific products (each customer's Blender then only sees and can
     download what they are licensed for):
     ```json
     {"edc_Xk39fj2mQ8vL5nR7tY1wZ4": "Acme Reconstruction LLC",
      "edc_P2hN8cV6bM4xK9sD3fG7jQ": {"name": "Smith Engineering",
                                     "products": ["recon_toolkit", "point_cloud_toolkit"]}}
     ```
     Product ids: `cammatch`, `hve_toolkit`, `point_cloud_toolkit`,
     `recon_toolkit` (or `"*"` for all). Generate tokens with:
     `python -c "import secrets; print('edc_' + secrets.token_urlsafe(18))"`
   - `CUSTOMERS_REPO` (optional, recommended once you have more than a
     couple of customers) — see **Managing customers at scale** below;
     tokens then live in a private repo instead of this variable.
3. **Attach the domain**: service → Settings → Networking → Custom Domain →
   `extensions.edccorp.com`. Railway shows a CNAME target; update the DNS
   record for `extensions` (currently pointing at `edccorp.github.io`) to
   that target. Remove the custom domain from the GitHub Pages settings of
   this repo (Pages keeps serving at `edccorp.github.io/blender-extensions`
   as the gateway's origin).
4. **Flip the publisher to gateway mode**: in this repo's publish workflow,
   set `MIRROR_ZIPS: "0"` on the build step (zips stop being copied to the
   public Pages site; only `index.json`, `packages.json`, and the landing
   page remain there). Run **Publish extensions index**.

## Customer instructions

Preferences → Get Extensions → Repositories → `extensions.edccorp.com` →
tick **Requires Access Token** → paste the customer's token into
**Secret**. Everything else (install, update notifications) works as
before — unauthenticated clients get a 401 with a friendly message.

## Managing customers at scale (recommended)

Editing `CUSTOMER_TOKENS` in the Railway dashboard is fine for one or two
tokens, but past that, move the list into a **private GitHub repo**. The
gateway re-reads the file within a minute of any change — no redeploy —
and the repo's commit history is a free audit trail of every add, scope
change, and revocation.

One-time setup:

1. Create a **private** repo `edccorp/edc-extensions-customers` (empty is fine).
2. Add that repo to the fine-grained PAT used as the gateway's `GH_TOKEN`
   (github.com → Settings → Developer settings → Fine-grained tokens →
   edit → Repository access). Contents: read is already enough.
3. In Railway (service → Variables) add `CUSTOMERS_REPO=edccorp/edc-extensions-customers`.
4. For the management CLI, create a **second** fine-grained PAT with
   **Contents: read & write** on `edc-extensions-customers` only, and set it on your
   machine: `setx CUSTOMERS_ADMIN_TOKEN github_pat_...`
5. Migrate any tokens out of `CUSTOMER_TOKENS` (re-add them via the CLI),
   then set `CUSTOMER_TOKENS={}` — entries left there stay active even if
   revoked in the file, so don't keep both.

Day-to-day, from the repo root:

```
python tools/customer.py add "Acme Reconstruction LLC"
python tools/customer.py add "Smith Engineering" --products recon_toolkit,point_cloud_toolkit
python tools/customer.py list
python tools/customer.py set-products "Smith Engineering" --products "*"
python tools/customer.py revoke "Acme Reconstruction LLC"
```

`add` generates the token, commits the change, and prints the token once
along with the customer-facing Blender setup steps. Changes go live on
the gateway within ~60 seconds (`CUSTOMERS_TTL`). You can also edit
`customers.json` directly on github.com — same format as
`CUSTOMER_TOKENS`, token → label or `{"name", "products"}` object.

- **Revoke**: their Blender shows an authentication error on the next
  sync; already-installed add-ons keep working but stop updating.
- Downloads and index fetches are logged with the customer label
  (Railway → Deployments → Logs) — who is updating, and when.
- If the customers file ever fails to fetch or parse, the gateway keeps
  serving the **last good copy** and reports the problem at `/healthz`
  (`"ok": false` + `customers_error`).

## Notes

- The Pages origin still serves `index.json`/`packages.json` publicly at
  the `github.io` URL. That is metadata only (names, versions, hashes);
  the binaries live solely in private GitHub releases once
  `MIRROR_ZIPS=0`.
- Rolling back to the unauthenticated setup: point the `extensions` DNS
  CNAME back at `edccorp.github.io`, re-add the custom domain in Pages
  settings, and set `MIRROR_ZIPS` back to `"1"`.
