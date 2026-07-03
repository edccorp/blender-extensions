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
   - `CUSTOMER_TOKENS` — JSON mapping token → customer label:
     ```json
     {"edc_Xk39fj2mQ8vL5nR7tY1wZ4": "Acme Reconstruction LLC",
      "edc_P2hN8cV6bM4xK9sD3fG7jQ": "Smith Engineering"}
     ```
     Generate tokens with: `python -c "import secrets; print('edc_' + secrets.token_urlsafe(18))"`
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

## Managing tokens

- **Add a customer**: add an entry to `CUSTOMER_TOKENS` in Railway;
  the service restarts automatically with the new variable.
- **Revoke a customer**: remove their entry. Their Blender shows an
  authentication error on the next sync; installed add-ons keep working.
- Downloads and index fetches are logged with the customer label
  (Railway → Deployments → Logs) — a simple audit trail of who is
  updating.

## Notes

- The Pages origin still serves `index.json`/`packages.json` publicly at
  the `github.io` URL. That is metadata only (names, versions, hashes);
  the binaries live solely in private GitHub releases once
  `MIRROR_ZIPS=0`.
- Rolling back to the unauthenticated setup: point the `extensions` DNS
  CNAME back at `edccorp.github.io`, re-add the custom domain in Pages
  settings, and set `MIRROR_ZIPS` back to `"1"`.
