# Deploying the web app

Two Vercel projects from this one repo:

| Project | Root directory | What it is | URL example |
|---|---|---|---|
| **API** (the existing project) | `/` (repo root) | FastAPI, entrypoint `app.py` (`[tool.vercel] entrypoint = "app:app"` in `pyproject.toml`) | `https://ecommerce-listing-management.vercel.app` |
| **Web** (new) | `web` | Next.js dashboard. Proxies `/api/*` to the API so the login cookie is same-site. | `https://listing-manager-web.vercel.app` |

Users only ever visit the **Web** URL.

## Why the old build failed

Vercel detected Python at the repo root but found no web app to serve. The only thing that looked like a server was `ebay/deletion_webhook.py`. That's the eBay account-deletion webhook, and it runs as a long-lived service on the original host, so pointing Vercel at it (as its error message suggested) would have been wrong. The repo root now has `app.py` exporting the FastAPI `app`, and the host-only dependencies (DSers MCP client, OCR) moved to the `pipeline` extra, so Vercel installs only what the API needs.

## 1. Database (Neon)

In the **API** project: Storage → Create / Connect Database → **Neon** (Vercel Marketplace), and connect it to the project. This injects `DATABASE_URL`. Tables are created automatically on first request.

## 2. API project environment variables

| Variable | Value |
|---|---|
| `DATABASE_URL` | Set by the Neon integration |
| `ELM_ENCRYPTION_KEY` | Output of `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. **Keep a copy.** Losing it makes every saved API key/token unreadable. |
| `CRON_SECRET` | Any long random string. Vercel Cron sends it automatically; `/api/cron/tick` rejects calls without it. |
| `EBAY_APP_ID`, `EBAY_CERT_ID`, `EBAY_DEV_ID` | Production keyset from developer.ebay.com → Application Keys |
| `EBAY_RUNAME` | The RuName (eBay Redirect URL name) from User Tokens → "Get a Token from eBay via Your Application" |
| `EBAY_ENV` | `production` (or `sandbox` with sandbox keys) |
| `APP_BASE_URL` | The **Web** project URL, e.g. `https://listing-manager-web.vercel.app` (where eBay sends users back after connecting) |
| `ALLOW_SIGNUP` | `false` for now. The very first account can always be created; after that sign-up is closed unless this is `true`. |
| `AUTO_LIST_GLOBALLY_DISABLED` | Optional kill switch: `true` stops all auto-listing for every user. |

## 3. Web project

New Vercel project → import this same repo → **Root Directory: `web`** (framework: Next.js). Environment variable:

| Variable | Value |
|---|---|
| `API_ORIGIN` | The **API** project URL, no trailing slash. Read at **build** time, so redeploy after changing it. |

## 4. eBay developer portal

On the RuName used for `EBAY_RUNAME`, set **Your auth accepted URL** to

    https://<web-project-url>/api/ebay/callback

and fill in the privacy policy URL eBay asks for. Users click **Connect eBay** on the Connections page, approve on eBay, and land back on Connections. The app asks for these scopes: `api_scope` (categories/aspects), `sell.inventory` (create/publish listings), `sell.account.readonly` (read business policies). The account must be opted in to business policies.

## 5. CJdropshipping

Each user pastes their own API key on the Connections page (CJ: My CJ → Authorization → API). It's encrypted before it's stored. CJ access tokens are cached (encrypted) and refreshed automatically.

## Scheduling on the Hobby plan

`vercel.json` registers one cron: `GET /api/cron/tick` at **14:00 UTC daily**. Hobby allows once-a-day crons, and Vercel may fire them any time within that hour. Each tick:
- starts a run for every user whose chosen daily time has passed since their last scheduled run;
- advances any run still in progress.

A user who picks a later hour than the cron runs at the next day's tick. "Run now" on the dashboard works any time, and the dashboard keeps the run moving while it's open.

Runs are chunked: each request does ~45 s of work (`ELM_RUN_BUDGET_SECONDS`) and the run's state lives in the database, so no single request has to fit a whole run. CJ calls are spaced ~1.1 s apart (free-tier throttle), so matching takes roughly 3-4 s per eBay item.

Two limits to know about:
- **Commercial use:** Vercel's Hobby plan is for personal, non-commercial use. Move to Pro before charging customers. Pro also allows hourly crons (change the schedule in `vercel.json`).
- **Multiple users:** with many users, one daily tick won't finish everyone's runs. Move runs to a real worker/queue then (web-app-plan.md §3).

## Local development

```bash
pip install -e ".[dev]"
export ELM_ENCRYPTION_KEY=$(python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
uvicorn app:app --reload --port 8000        # API, SQLite at ./elm-dev.db
cd web && npm install && npm run dev         # http://localhost:3000, proxies /api to :8000
pytest -q                                    # offline unit tests
```

## Not verified yet (no network access to eBay/CJ from the build environment)

The CJ client, eBay Deal/Browse discovery, eBay OAuth and listing calls are covered by unit tests against faked responses and a full browser run against a fake backend, but **none has been run against the live CJ or eBay APIs**. Check these on first real use:
1. **CJ:** "Save & verify" on Connections succeeds, and a Run finds matches with prices/shipping.
2. **eBay connect:** the round-trip lands back on Connections showing "Connected", and Settings lists your business policies.
3. **eBay listing:** "List on eBay" on one low-risk item publishes. If eBay rejects it, the exact eBay error is shown on the row under "Listing failed".
