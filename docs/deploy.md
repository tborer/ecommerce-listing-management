# Deploying the web app

Two Vercel projects from this one repo:

| Project | Root directory | What it is | URL example |
|---|---|---|---|
| **API** (the existing project) | `/` (repo root) | FastAPI, entrypoint `app.py` (`[tool.vercel] entrypoint = "app:app"` in `pyproject.toml`) | `https://ecommerce-listing-management.vercel.app` |
| **Web** (new) | `web` | Public landing page at `/`, plus the Next.js dashboard at `/dashboard`. Proxies `/api/*` to the API so the login cookie is same-site (except `/api/waitlist`, which the web app handles itself). | `https://listing-manager-web.vercel.app` |

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

### Landing page, waitlist and SEO (Web project)

`/` is a static, SEO-focused landing page. The signed-in app moved to `/dashboard`, and its pages (and `/login`) are marked `noindex`. More Web project environment variables:

| Variable | Value |
|---|---|
| `ENABLE_WAITLIST` | `true` shows "Join the waitlist" buttons (header, hero, final section) that open an email sign-up modal. Anything else shows "Get started" linking to `/login`, and `/api/waitlist` is closed. |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_SECURE`, `SMTP_USER`, `SMTP_PASS`, `SMTP_FROM` | Your mail server. Each sign-up is emailed to you with the subscriber as Reply-To. `SMTP_SECURE=true` means TLS from the start (port 465); `false` uses STARTTLS (port 587). `SMTP_USER`/`SMTP_PASS` can be empty for servers without auth. |
| `WAITLIST_TO` | Optional: where sign-ups are delivered. Defaults to `SMTP_USER` (then `SMTP_FROM`). Visitors never see this address; it's only used server-side. |
| `SITE_URL` | Optional: canonical site URL (set it when you add a custom domain). Defaults to the Vercel production URL. |
| `SITE_NAME` | Optional: product name shown on the page, title and social image (default "Listing Manager"). |
| `GOOGLE_SITE_VERIFICATION` | Optional: the token for Search Console's "HTML tag" verification method. |

The landing page is pre-rendered at build time, so **redeploy after changing any of these**. Vercel only applies environment variable changes to new deployments anyway.

A direct waitlist link: `https://<site>/#waitlist` opens the sign-up modal straight away.

Spam protection is a hidden honeypot field, strict email validation, and a per-instance rate limit (5 per 10 minutes per IP). It's enough for a waitlist. Add a CAPTCHA if bots show up.

**Google Search Console:**
1. Add the site: a Domain property (DNS TXT record) if you have a custom domain; otherwise a URL-prefix property verified with the HTML tag (put the token in `GOOGLE_SITE_VERIFICATION` and redeploy).
2. Submit `https://<site>/sitemap.xml` under Sitemaps.
3. Use URL Inspection on `https://<site>/` and click "Request indexing".
4. The site already serves `robots.txt`, `sitemap.xml`, a canonical URL, Open Graph/Twitter cards with a generated preview image, and JSON-LD structured data (SoftwareApplication, WebSite, FAQPage).

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
