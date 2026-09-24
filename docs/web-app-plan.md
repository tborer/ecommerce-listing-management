# Web App Plan — from single-owner pipeline to a paid multi-user product

Status: **direction decided, implementation not started** (2026-09-23). Decisions made: FastAPI + Next.js stack, CJdropshipping as the first fulfillment provider, official-APIs-first discovery (see §9). Companion to [`production-readiness.md`](production-readiness.md) (the live system's backlog) and [`listing-rules.md`](listing-rules.md) (what the pipeline does today). This doc covers the *product*: what we build around the existing pipeline so other sellers can pay to use it.

## 1. Product goal

A subscription web app that helps a seller run an eBay dropshipping business end to end:

1. **Sources** — the user adds what to scan: eBay searches/categories, specific item URLs, keyword lists, and (best effort) arbitrary "trending products" pages.
2. **Criteria** — the user configures the rules an item must pass to be listed: price range, fees, target margin / minimum profit, match confidence, shipping cost and delivery time caps, category and brand allow/deny lists, daily listing caps.
3. **Review / auto-list** — candidates that pass show up in an approval queue; users can opt in to auto-listing the top N per run, with caps and a kill switch.
4. **eBay connection** — the user connects their own eBay seller account (OAuth consent), and the app sets up the business policies it needs.
5. **Fulfillment** — the user connects a supplier/fulfillment provider. Orders flow eBay → supplier order (user-approved) → tracking → eBay marked shipped.
6. **Monitoring** — the app watches listings, orders, supplier stock, and supplier price drift, and shows it all on a dashboard instead of in emails and JSON files.

## 2. Where we're starting from (honest inventory)

What exists and is reusable (~9k lines of Python, proven in production since 2026-08):

| Capability | Code | Reusable as-is? |
|---|---|---|
| Profit math, match judging, brand/accessory exclusion, category scoring | `pipeline.py` (`compute_profit`, `judge_match`, `_excluded_reason`, …) | **Yes**, once pulled out of the 2k-line script into a pure `criteria`/`matching` module |
| eBay OAuth, Inventory API list/publish/withdraw, Fulfillment API reads, MAD webhook | `ebay/*` | **Mostly** — needs per-tenant credentials instead of Bitwarden or a local file |
| DSers MCP client, mapping, stock/price checks, remap | `dsers/*` | For the owner's account only (see §5) |
| WooCommerce bridge (so DSers can see eBay orders) | `woocommerce/client.py` | Owner's account only; not a multi-tenant pattern |
| Email report | `reporting/*` | Replaced by the dashboard; keep as optional digest |

What is single-owner by construction and has to change:

- **State is JSON files next to the source** (`OUT_DIR = Path(__file__).parent` in 8 modules). With a normal (non-editable) install, those files land inside `site-packages`. This needs a database, and in the short term a configurable data directory.
- **Credentials** are Bitwarden lookups or one `branch11_unattended_creds.json` file. Tenants need encrypted per-account credential storage.
- **Scraping runs through the `openclaw browser` CLI** (a subprocess to a single shared browser). That doesn't isolate tenants or scale; workers need their own headless Playwright (or official APIs, see §4).
- **Scheduling is OpenClaw cron plus `claude -p` sessions.** A product needs a job queue with per-tenant schedules, retries, and rate limits.
- **Tests**: the two test files are live-account scripts (they hit real eBay/WooCommerce). There are no offline unit tests yet, so CI has nothing to run.
- **Hardcoded parameters** (`DEALS_URL_POOL`, `AUTO_LIST_TOP_N_DEFAULT`, margins) become per-tenant settings.

## 3. Recommended architecture

```
 Browser ──> Web frontend (Next.js, Vercel) ──> API (FastAPI) ──> Postgres
                                                  │                  ▲
                                                  └─ enqueue ─> Job queue (Postgres-backed)
                                                                     │
                                         Workers (Python, this package + Playwright)
                                          ├─ discovery  (eBay Browse API, supplier catalog APIs, page scrapes)
                                          ├─ matching / criteria evaluation
                                          ├─ listing    (eBay Inventory API, per-tenant token)
                                          ├─ orders     (eBay Fulfillment API poll → supplier order drafts)
                                          └─ monitors   (stock, price drift, tracking → eBay shipment)
 Stripe (billing) ── webhooks ──> API          eBay MAD + notification webhooks ──> API
```

**Stack recommendation: Python backend + TypeScript frontend.**
- **API + workers: Python (FastAPI, SQLAlchemy + Alembic, Postgres).** It reuses this package directly, and all the hard domain logic is already Python.
- **Queue: Postgres-backed (e.g. Procrastinate)** to start, so there's no Redis to run. Move to Celery/Redis only if volume demands it.
- **Frontend: Next.js on Vercel** (already used for `tb-ecommerce-proxy`). A Python-only alternative is FastAPI + HTMX: one language and faster to MVP, but a weaker fit for rich review tables and dashboards.
- **Auth:** a hosted provider (Clerk or Auth0) or Auth.js; no hand-rolled passwords.
- **Billing: Stripe** Checkout + Customer Portal + webhooks → `subscriptions` table → plan limits enforced in the API and workers.
- **Secrets:** envelope encryption for tenant tokens (eBay refresh tokens, supplier API keys). Each row is encrypted with a data key, and the data key is wrapped by a KMS/master key. Tokens are never shown in the UI after entry.
- **Hosting:** API + workers + Postgres on Render, Fly, or Railway. Playwright workers go in a separate pool with their own concurrency limits.

### Core data model (first cut)

`tenants`, `users`, `memberships`, `subscriptions` ·
`marketplace_connections` (eBay: env, encrypted refresh token, scopes, policy ids) ·
`supplier_connections` (provider, encrypted key/token, status) ·
`sources` (type: ebay_search | ebay_category | item_url | keyword_list | page_url; schedule; enabled) ·
`criteria_profiles` (versioned JSON rules, see below) ·
`discovery_runs` → `candidates` → `supplier_matches` → `evaluations` (pass/fail + per-rule reasons, profile version) ·
`listings` (eBay offer/listing ids, status, source match, cost basis) ·
`orders` / `order_lines` → `supplier_orders` (draft → awaiting_approval → placed → shipped) → `shipments` (tracking, pushed_to_ebay_at) ·
`audit_events` (every externally visible action: who/what/when, and whether auto or approved).

### Criteria engine

A pure, tested module that takes a candidate plus its supplier match plus a criteria profile and returns pass/fail with a reason for each rule. The initial rules map one-to-one to today's parameters:

| Rule | Today's value | Notes |
|---|---|---|
| `max_sale_price` / `min_sale_price` | $100 / – | |
| `fee_pct` | 17% | later: per-category eBay fee table |
| `target_margin_pct` | 18% | |
| `min_profit_abs` | – | new; protects cheap items |
| `min_match_confidence` | HIGH (auto) / MEDIUM (review) | |
| `max_shipping_cost`, `max_delivery_days` | via shipping-policy tiers | |
| `category_allow` / `category_deny`, `brand_deny` | `_excluded_reason` lists | brand deny also covers VeRO risk |
| `auto_list` (enabled, `top_n`, `daily_cap`) | enabled, 5 | **off by default for new tenants** |

A profile change creates a new version, and every evaluation records which version it ran under, so "why did this list?" always has an answer.

## 4. Discovery ("the scraping process")

Users will add sources in the UI. The recommendation is to put **official APIs first and treat page scraping as best effort**, because this is now a paid product running on customers' accounts:

- **eBay Browse API** (`item_summary/search`, category and keyword filters) replaces scraping eBay Deals and search pages (full API mapping and migration steps in §4a). It's official, rate-limited, and there's no bot-detection arms race. Scraping eBay under our developer app's identity puts every customer at risk if eBay objects.
- **Supplier catalog APIs** (AliExpress DS API product search/get, CJ product search) replace scraping `aliexpress.us` search pages for matching. They return stable ids, prices, variants, stock, and freight quotes. That removes the reason for the `PRODUCT_PAGE_JS` price verification pass.
- **Generic page URLs** (blogs, "trending" lists) stay supported as a best-effort *keyword* source: a Playwright worker extracts candidate keywords, which then go through the Browse API. This is today's `KEYWORD_SOURCES` pattern, generalized so users don't need a per-site JS extractor (a generic extractor with an optional per-site selector).
- **Amazon/Walmart pages may be used as inspiration (keywords) only, never as suppliers.** eBay's dropshipping policy only allows fulfilling from a wholesale supplier. Buying from another retailer to ship to the buyer is prohibited and is the top cause of dropshipper suspensions. The product enforces this: a supplier must be a connected wholesale provider.

## Status — first web app slice (2026-09-23)

Built and deployed-ready, **not yet run against live eBay/CJ** (see `docs/deploy.md`):
- **API:** FastAPI (`src/ecommerce_listing_mgmt/webapp/`, entrypoint `app.py`) on Vercel, with Neon Postgres. Includes:
  - email/password login;
  - per-user encrypted CJ API key and eBay OAuth tokens;
  - per-user criteria, schedule and auto-list settings;
  - chunked discovery runs: eBay Deal/Browse API → CJ search, product and freight (`suppliers/cj.py`) → `judge_match` → criteria engine;
  - review queue with List on eBay, Dismiss and Restore;
  - optional auto-list of the top N by profit;
  - daily Vercel cron tick.
- **Dashboard:** Next.js (`web/`) as a second Vercel project, with Dashboard, Settings and Connections pages.
- **CI:** `.github/workflows/ci.yml` runs ruff, pytest, and the Next.js typecheck and build.

## Deployment rollout — website first, then the API (2026-09-24)

One repo, two Vercel projects (details: `docs/deploy.md`):
- **Project 1 — website:** Root Directory **`web`**. Next.js app with the landing page, waitlist, `/privacy`, and the dashboard pages.
- **Project 2 — API:** Root Directory **empty** (repo root). Python/FastAPI with accounts, encrypted CJ and eBay credentials, runs, listing, and the daily cron.

A project imported with an empty Root Directory builds the **API**, not the website. That's what shows the "SourceSnap API" page.

### Stage A — website live (now)
Project 1 works on its own: the landing page, the waitlist and the privacy policy don't need the API. Until `API_ORIGIN` is set, the site hides "Log in" and doesn't forward `/api/*` anywhere, so nothing points at a missing backend.

1. In the Vercel project for the website: Settings → Build and Deployment → **Root Directory = `web`**, then redeploy.
2. Environment variables:
   - `ENABLE_WAITLIST=true`
   - `SMTP_HOST`, `SMTP_PORT`, `SMTP_SECURE`, `SMTP_USER`, `SMTP_PASS`, `SMTP_FROM`
   - `CONTACT_EMAIL`
   - Optional: `WAITLIST_TO`, `LEGAL_ENTITY`, `SITE_URL` (custom domain), `GOOGLE_SITE_VERIFICATION`
3. Storage → connect Neon to this project; this sets `DATABASE_URL` for waitlist storage.
4. Redeploy. Then check: the landing page loads, a test sign-up arrives by email and appears in `waitlist_signups`, and `/privacy`, `/robots.txt` and `/sitemap.xml` load.
5. Google Search Console: add the site, submit `/sitemap.xml`, and request indexing of `/`.

### Stage B — API project (next)
1. Vercel → Add New → Project → the same repo, **Root Directory left empty**. Name it e.g. `sourcesnap-api`.
2. Storage → connect the **same** Neon database. This sets `DATABASE_URL`.
3. Environment variables:

| Variable | Value / where it comes from |
|---|---|
| `ELM_ENCRYPTION_KEY` | Generate with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. Keep a copy; losing it makes every saved CJ key and eBay token unreadable. |
| `CRON_SECRET` | Any long random string; guards `/api/cron/tick` |
| `APP_BASE_URL` | The website URL, e.g. `https://sourcesnap.vercel.app` |
| `EBAY_APP_ID`, `EBAY_CERT_ID`, `EBAY_DEV_ID` | developer.ebay.com → Application Keys (Production) |
| `EBAY_RUNAME` | developer.ebay.com → User Tokens → "Get a Token from eBay via Your Application" |
| `EBAY_ENV` | `production` |
| `ALLOW_SIGNUP` | `false` (the first account can always sign up) |
| `AUTO_LIST_GLOBALLY_DISABLED` | Optional emergency switch: `true` stops all auto-listing |

4. eBay developer portal: set the RuName's **auth accepted URL** to `<website URL>/api/ebay/callback`.
5. Deploy. Opening the API's URL should now redirect to the website.
6. Back in **Project 1**: set `API_ORIGIN` to the API's URL (no trailing slash) and **redeploy**. `API_ORIGIN` is read at build time. "Log in" then appears on the site.
7. Check: sign up as the first user, save the CJ key on Connections, click Run now, connect eBay, and list one low-risk item (see `docs/deploy.md` "Not verified yet").

### Stage C — later
- Custom domain on Project 1, then update `SITE_URL`, `APP_BASE_URL` and the eBay accepted URL to match.
- Move to Vercel Pro before charging customers. Hobby is non-commercial, and Pro also allows hourly cron.

## 4a. Moving off scraping — API replacements and migration steps

Researched 2026-09-23. The primary doc sites for eBay, CJ, and AliExpress are blocked from the environment this was written in, so endpoint names and limits come from search results and API mirrors. Items marked **(verify)** must be confirmed against the live docs before building on them.

### Can we get eBay listings by API? Yes.

| Today (scraped) | API replacement | Access |
|---|---|---|
| eBay Deals pages (`EBAY_TILE_JS`) | **Deal API** `getDealItems` / `getEvents` / `getEventItems`: the same deals data, structured | **Limited Release**: only approved developers. Apply early; don't depend on it. |
| (Deals fallback) | **Browse API** `item_summary/search` with `marketingPrice` (`discountPercentage`, `originalPrice`) in results. Filter or sort client-side for discounted items, which gives "deals" without the Deal API. | Generally available; app token (client-credentials grant, scope `https://api.ebay.com/oauth/api_scope`); no user consent needed |
| eBay search pages (`EBAY_SEARCH_TILE_JS`) | **Browse API** `item_summary/search`: `q`, `category_ids`, `gtin`, `epid`, `filter=` (price range, `buyingOptions:{FIXED_PRICE}`, conditions, `itemLocationCountry`, `deliveryCountry`), `aspect_filter` (brand, color…), `sort` (price, `newlyListed`, `endingSoonest`), paged | Same |
| A single item URL a user pastes | **Browse API** `getItemByLegacyId` (the number in `/itm/<id>`) → `getItem` | Same |
| (no current signal) demand per item | `getItem` → `estimatedAvailabilities.estimatedSoldQuantity`: units sold on that listing. A usable popularity signal. | Same |
| (Item 25, variations) | `getItemsByItemGroup`: all variants of a multi-variation listing | Same |
| "Is this supplier product already all over eBay?" | `item_summary/search_by_image` (marked experimental) **(verify)** | Same |
| Category suggestion (already used by `suggest_category`) | **Taxonomy API** `getCategorySuggestions`, category tree, item aspects | Generally available |
| Sold-price history ("what does this actually sell for?") | **Marketplace Insights API**. **Closed to new users**, and Terapeak (Seller Hub) has no API. | Not available. Use active-listing prices + `estimatedSoldQuantity` instead. |

Default Browse call limits are a few thousand calls/day per app **(verify exact number)**. The app token is ours, not each tenant's, so that quota is **shared across all customers**. Use it deliberately: cache search results per (query, filters) for a few hours across tenants, and file the eBay Application Growth Check before launch.

### Can we search CJ for matching items by API? Yes.

Per-user CJ API key → access token, sent as the `CJ-Access-Token` header. Endpoints (under `https://developers.cjdropshipping.com/api2.0/v1/`):

| Need | Endpoint | Notes |
|---|---|---|
| Keyword / category search | `product/listV2` (also older `product/list`) | Keyword, category, price range, warehouse country, `deliveryTime` (24/48/72h processing), paged |
| Product detail + variants | `product/query`, `product/variant/query` | Stable `pid` / variant `vid`, images, weight, variant attributes. Feeds `judge_match` and Item 25 variations directly. |
| Stock | `product/stock` (by variant / warehouse) **(verify path)** | Replaces DSers `supplier_status` out-of-stock detection for CJ-sourced listings |
| Shipping cost + time | `logistic/freightCalculate` (from/to country, `vid`, qty → method, cost, days) | Replaces scraped AliExpress shipping cost; feeds `max_shipping_cost` / `max_delivery_days` rules |
| Order + tracking | `shopping/order/createOrderV2`, order/logistics queries, webhooks | See §5 |
| Search by image | Exists in CJ's web UI; API endpoint **(verify)** | Would let us match on the eBay item's photo, not just its title |
| "Find me this product" | CJ Sourcing requests (their agents find a factory and add it to the catalog). In the UI; API endpoint **(verify)** | Useful for eBay winners CJ doesn't stock yet; async, days |

Rate limit: free/v1 accounts get **1,000 requests/day**, with higher tiers above that **(verify tiers)**. The key is per tenant, so each customer's CJ quota is their own. That's good for isolation, but matching has to be economical: search once per candidate, fetch detail only for the top few hits, and cache freight quotes per (vid, destination).

### Other suppliers with an API for sourcing *and* fulfillment

| Supplier | Search API | Order API | Why consider it |
|---|---|---|---|
| **AliExpress Open Platform (DS APIs)** | Product get, text/image search (AE-Dropshipper + AE-Image modules), freight query (AE-Freight) **(verify method names)** | Yes (`aliexpress.trade.buy.placeorder` / DS order APIs) | Largest catalog; direct continuation of today's supply side. Needs app approval + per-user authorization. |
| **Wholesale2B API** | Yes, 1.5M+ products from 100+ suppliers | Yes, white-label, multi-warehouse routing to the nearest warehouse | Aggregated **US** suppliers: faster delivery, lower dispute risk. Paid API plan. |
| **Doba Retailer API** | Yes | Yes, place purchase orders via API | US supplier marketplace; supports eBay as a channel |
| **BigBuy API** | Yes | Yes, orders + tracking | EU wholesaler. Relevant only if we expand beyond eBay US. |
| **AutoDS API** | Yes | Yes | Covered in §5; a buy-vs-build option |
| Inventory Source / Flxpoint | Aggregators/middleware normalizing many US suppliers' feeds | Order routing | An option if we want many US suppliers without integrating each one |

**Recommended order:** CJ (decided) → AliExpress DS API → one US-warehouse aggregator (Wholesale2B or Doba), for categories where delivery time matters.

### Matching flow changes shape: from "demand-first" to "supply-first" too

- **Today (demand-first):** eBay deal → search the supplier for the same thing → judge whether it matches. Most candidates die at the match step, and every match is a guess across two catalogs.
- **With supplier APIs, add supply-first:** pull supplier catalog items that fit the tenant's criteria (warehouse country, delivery time, cost range, category), then use the Browse API to check eBay demand and competing prices for each. Every candidate is fulfillable by construction; the question is only "does this sell on eBay, and at what price?"
- **Offer both modes.** Supply-first is the default for new tenants; demand-first stays for users who add eBay searches or item URLs as sources. Both feed the same criteria engine (§3).

### Migration steps

Each step runs on the owner's account first, in shadow mode, before anything cuts over.

1. **eBay app-token client + Browse adapter.** **BUILT 2026-09-23, not yet run against the live API.**
   - `ebay.auth.get_application_token()` mints a client-credentials token from the app keys alone.
   - `ebay/browse.py` holds the Deal API (`getDealItems`, events), Browse (search, `getItemByLegacyId`, item groups, `estimatedSoldQuantity`) and Taxonomy clients. Items are normalized to the scrapers' exact `{id, title, priceNum, url}` tile shape.
   - Each eBay Deals URL maps to category *names*, which are resolved to ids from the live category tree (cached for 30 days).
   - The CLI (`python -m ecommerce_listing_mgmt.ebay.browse token-check | categories | deals | search | item | url`) is for live verification.
   - 17 offline unit tests (`tests/unit/`).
   - Deal API access: the owner reports developer-account access. If a call is refused anyway, discovery falls back per URL to a Browse search of the same categories, keeping only discounted items.
2. **Browse-backed discovery sources.** **BUILT 2026-09-23, behind a flag.** `pipeline.py --discovery-source api` (default stays `scrape`) routes each eBay Deals URL through the Deal API and each keyword-source keyword through a Browse search. Only the keyword *pages* themselves are still browser-extracted.
3. **Shadow compare discovery (about 1 week).** Tooling built: `pipeline.py --stage compare-discovery [--compare-all-pages]` runs both sources over the same URLs and writes `branch11_discovery_compare_<date>.json` (counts, overlap, price mismatches, samples unique to each side). Run Browse discovery next to the live Deals scrape for the same categories. Compare candidate counts, overlap, price accuracy, and how many reach HIGH match. Cut over when Browse is at least as good.
4. **CJ supplier adapter.** Add `suppliers/cj.py` implementing the §5 adapter interface (`search_products`, `get_product`, `quote_shipping`; orders come later). Generalize `judge_match` to take a normalized `SupplierProduct` (title, price, images, variants) instead of scraped AliExpress tile dicts.
5. **Shadow compare matching.** For the same candidates, run the current AliExpress HTML match and the CJ API match side by side. Compare match rate, profit-pass rate, and shipping cost and time.
6. **Supply-first mode.** A CJ catalog pull → Browse demand/price check → criteria. Measure listed-to-sold conversion against demand-first.
7. **Cut over and retire.** Remove Deals/search-page scraping (`EBAY_TILE_JS`, `EBAY_SEARCH_TILE_JS`), AliExpress search scraping (`ALIEXPRESS_TILE_JS`), and product-page price verification (`PRODUCT_PAGE_JS`; supplier APIs return authoritative price/stock). With that, the `openclaw browser` dependency leaves the core pipeline.
8. **What stays scraped:** only user-added "inspiration" pages (trending lists, blogs), and only to extract keywords. Prefer a plain HTTP fetch plus a generic text extractor; use a headless browser only for pages that need JavaScript. Every keyword then goes through the Browse API, never direct to listing.
9. **Second and third suppliers.** AliExpress DS API, then a US aggregator, behind the same adapter. Matching runs against every supplier the tenant has connected and picks the best landed cost that meets their delivery-time rule.

**Still to verify before building:** Browse default call limits and whether `search_by_image` is production-available; the CJ stock endpoint path, image-search and sourcing API availability, and rate-limit tiers; AliExpress DS search/image method names and payment behavior on API-placed orders; Wholesale2B/Doba API pricing and approval.

## 5. Fulfillment providers — research (2026-09)

The key question for each provider is whether a third-party app can create orders and read tracking programmatically, on behalf of *each user's own* supplier account.

| Provider | Programmatic order placement | Tracking | Fit | Notes |
|---|---|---|---|---|
| **CJdropshipping API 2.0** | **Yes**, `shopping/order/createOrderV2`, with `payType=3` (create without paying) or `payType=2` (pay from the user's CJ wallet balance) | Yes (logistics APIs + webhooks) | **Best first integration** | Per-user API key; product search, freight calc, and dispute APIs; US warehouses shorten delivery. Custom-channel orders, so no Shopify/Woo store needed. |
| **AliExpress Open Platform — DS (dropshipper) APIs** | Yes, `aliexpress.trade.buy.placeorder` / DS order APIs; needs app registration and per-user authorization | Yes (order/logistics query) | **Second integration**, largest catalog, closest to today's supply side | Our app must be approved as a dropshipping app; each user authorizes their AliExpress account. Payment confirmation behavior needs hands-on verification (docs host blocked from this environment). |
| **DSers** | No merchant order API. Its Partner/Open API targets *third-party developers building for other merchants*, which is what this product now is. | Auto-syncs into a connected store | Re-evaluate the Partner program | The backlog ruled Partner out because Branch 11 was a single merchant; that reason no longer applies. Today's DSers + WooCommerce bridge path stays for the owner's account only. It needs one Woo store per tenant, so it doesn't scale to a SaaS. |
| **AutoDS API** | Yes, including "Fulfilled by AutoDS" and multi-client "shared user authorization" models | Yes | Buy-vs-build alternative | Access is by approval with an activation fee. AutoDS already supports eBay natively, so it overlaps with (and competes with) this product. |
| **Spocket, Zendrop** | No public order-placement API found | – | Skip for now | Integration is via Shopify/Woo apps. |
| **Printful / Printify** (print on demand) | Yes, full public APIs | Yes | Optional later vertical | Different business model (custom products, no sourcing match). |

**Supplier adapter interface** (one implementation per provider):
`search_products(query)`, `get_product(id)` (variants, price, stock), `quote_shipping(product, variant, dest)`, `create_order(draft)`, `get_order(id)` (status + tracking), plus an optional webhook handler.

**Order flow.** Human approval stays the default, which carries the backlog's hard constraint into the product:
1. Poll eBay `getOrders` per tenant (or subscribe to eBay order notifications).
2. Build a supplier order **draft** from the listing's source match and the buyer's ship-to address.
3. **The user approves in the UI**, and the app calls `create_order` (CJ `payType=3`: order created, paid by the user in CJ; or `payType=2` if the user explicitly enabled wallet auto-pay with per-order and per-day spend caps).
4. Poll the supplier (or receive its webhook) for tracking, then eBay `createShippingFulfillment`. That covers Steps 10–11 from the backlog, which currently aren't built.
5. Customer messages remain eBay's own shipment notifications. The app sends none of its own.

## 6. The three "next steps" from the import, decided here

1. **App shape.** Four screens for the MVP: **Candidates** (review/approve queue with per-rule pass/fail reasons), **Listings** (live listings, cost basis, stock/price drift flags, withdraw), **Orders** (eBay order → supplier draft → approve → tracking), **Settings** (sources, criteria profiles, connections, billing). Stack per §3.
2. **Cron cutover.** Don't point the OpenClaw crons at this repo as it stands, because nothing would be gained and the `Path(__file__).parent` writes would land in the install dir. Instead:
   - (a) Phase 0 makes the data directory configurable.
   - (b) Phase 1's worker runs the owner's account as **tenant #1** in **shadow mode** (`--no-auto-list`, read-only eBay) next to the live crons for about a week, and the two outputs are compared.
   - (c) Then, one cron at a time, disable the old job and enable the worker job. `daily-order-report` and `price-check` go first (read-only), `daily-item-research` (auto-list) last.
3. **CI.** GitHub Actions running ruff plus offline unit tests on every push/PR. The live-account scripts move to `tests/live/` and are excluded from CI, run by hand only.

## 7. Phased delivery

| Phase | Outcome | Main work |
|---|---|---|
| **0. Foundation** (this repo) | Safe to change | CI; configurable data/creds dir (`ELM_DATA_DIR`); extract criteria/matching/profit into a pure module; unit tests for it; move live scripts to `tests/live/` |
| **1. Owner web app** (single tenant) | Travis uses the UI instead of emails | Postgres schema + import of existing JSON ledgers; FastAPI + queue workers wrapping the pipeline; Candidates/Listings/Orders screens; shadow-run then cron cutover |
| **2. Multi-tenant + billing** | Other people can sign up and pay | Auth, tenants, Stripe plans + limits; eBay OAuth onboarding (consent, business-policy opt-in, create shipping-policy tiers); encrypted credential store; per-tenant sources/criteria; auto-list off by default |
| **3. Fulfillment** | Orders ship without manual copy-paste | Supplier adapter; **CJ** first, then AliExpress DS API; order drafts → approve → tracking → eBay shipment; spend caps |
| **4. Discovery hardening** | Reliable, compliant sourcing at scale | §4a steps 1–9: Browse API sources (shadow-compared), CJ catalog matching, supply-first mode, retire page scraping. Steps 1–2 can start in Phase 1, since they're read-only. |
| **5. Launch readiness** | Sellable | eBay Application Growth Check (higher API limits); ToS/privacy (buyer addresses are PII: retention and deletion via the MAD webhook); observability, alerts, support; pricing page |

**Pricing sketch** (to validate): tiers by active listings, sources, and runs per day, e.g. Starter (≤100 listings, 3 sources, review-only), Pro (≤1,000 listings, auto-list, 1 supplier), Scale (more listings, multiple suppliers, wallet auto-pay).

## 8. Guardrails carried from the live system into the product

The backlog's hard constraints become per-tenant product rules, not one person's standing instructions:
- Supplier orders need explicit user approval by default. Auto-pay is opt-in, capped per order and per day, and logged.
- Auto-listing is off by default. When on, it is capped (`top_n`, `daily_cap`), only HIGH-confidence matches qualify, and it has a per-tenant kill switch plus a global one.
- No customer-facing messages from the app.
- Every external write goes into `audit_events`.
- Only wholesale-supplier fulfillment is allowed (eBay dropshipping policy).

## 9. Decisions

Decided 2026-09-23:
1. **Stack: FastAPI (Python API + workers, Postgres) + Next.js frontend.**
2. **First fulfillment provider: CJdropshipping.** AliExpress DS API second.
3. **Discovery: official APIs first** (eBay Browse API + supplier catalog APIs); user-supplied page URLs scraped best-effort for keywords only.

Still open:
4. Hosting provider and auth provider.
5. Whether Travis's own account stays on DSers + Woo long-term or migrates to the new supplier path.
6. Whether to apply to the DSers Partner program anyway, for continuity.
7. Pricing tiers (§7 sketch).

Next step: Phase 0.

## Sources (fulfillment and API research)

- CJdropshipping API 2.0 docs: https://developers.cjdropshipping.com/en/api/start/ ; Shopping (order) API: https://developers.cjdropshipping.cn/en/api/api2/api/shopping.html
- AliExpress Open Platform API reference: https://openservice.aliexpress.com/doc/api.htm ; `aliexpress.trade.buy.placeorder`: https://open.alitrip.com/docs/api.htm?apiId=35446
- AutoDS API: https://www.autods.com/api/ ; https://help.autods.com/en/articles/12699964-autods-api-feature-automate-product-imports-orders-and-sourcing
- eBay dropshipping policy summaries: https://www.salehoo.com/learn/ebay-dropshipping ; https://super-ds.com/blog/ebay-dropshipping-policy-2026
- eBay Application Growth Check: https://developer.ebay.com/grow/application-growth-check ; API call limits: https://developer.ebay.com/develop/get-started/api-call-limits
- Spocket/Zendrop API landscape: https://apitracker.io/a/spocket-co ; https://easync.io/articles/zendrop-review/
- eBay Browse API: https://developer.ebay.com/api-docs/buy/static/api-browse.html ; search: https://developer.ebay.com/develop/api/buy/browse_api/item_summary/search ; field filters: https://developer.ebay.com/api-docs/buy/static/ref-buy-browse-filters.html ; getItem: https://developers.ebay.com/api-docs/buy/browse/resources/item/methods/getItem ; MarketingPrice: https://www.developer.ebay.com/api-docs/buy/browse/types/gct:MarketingPrice
- eBay Deal API (limited release): https://developer.ebay.com/api-docs/buy/static/api-deal.html
- eBay Marketplace Insights (closed to new users): https://developer.ebay.com/api-docs/buy/api-insights.html
- eBay client-credentials grant: https://developer.ebay.com/api-docs/static/oauth-client-credentials-grant.html
- CJ Product API: https://developers.cjdropshipping.cn/en/api/api2/api/product.html ; Logistics API: https://developers.cjdropshipping.com/en/api/api2/api/logistic.html ; image search / sourcing (UI): https://cjdropshipping.com/article-details/71 , https://cjdropshipping.com/sourcing
- Wholesale2B API: https://www.wholesale2b.com/dropship-api-plan.html ; Doba Retailer API: https://open.doba.com/ ; BigBuy API: https://www.bigbuy.eu/en/api_bigbuy.html ; Inventory Source: https://www.inventorysource.com/ultimate-dropshipping-supplier-api-checklist/
