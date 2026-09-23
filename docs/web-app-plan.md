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

- **eBay Browse API** (`item_summary/search`, category and keyword filters) replaces scraping eBay Deals and search pages. It's official, rate-limited, and there's no bot-detection arms race. Scraping eBay under our developer app's identity puts every customer at risk if eBay objects.
- **Supplier catalog APIs** (AliExpress DS API product search/get, CJ product search) replace scraping `aliexpress.us` search pages for matching. They return stable ids, prices, variants, stock, and freight quotes. That removes the reason for the `PRODUCT_PAGE_JS` price verification pass.
- **Generic page URLs** (blogs, "trending" lists) stay supported as a best-effort *keyword* source: a Playwright worker extracts candidate keywords, which then go through the Browse API. This is today's `KEYWORD_SOURCES` pattern, generalized so users don't need a per-site JS extractor (a generic extractor with an optional per-site selector).
- **Amazon/Walmart pages may be used as inspiration (keywords) only, never as suppliers.** eBay's dropshipping policy only allows fulfilling from a wholesale supplier. Buying from another retailer to ship to the buyer is prohibited and is the top cause of dropshipper suspensions. The product enforces this: a supplier must be a connected wholesale provider.

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
| **4. Discovery hardening** | Reliable, compliant sourcing at scale | eBay Browse API sources; supplier-catalog matching replaces AliExpress HTML scraping; generic keyword extractor for page URLs |
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

## Sources (fulfillment research)

- CJdropshipping API 2.0 docs: https://developers.cjdropshipping.com/en/api/start/ ; Shopping (order) API: https://developers.cjdropshipping.cn/en/api/api2/api/shopping.html
- AliExpress Open Platform API reference: https://openservice.aliexpress.com/doc/api.htm ; `aliexpress.trade.buy.placeorder`: https://open.alitrip.com/docs/api.htm?apiId=35446
- AutoDS API: https://www.autods.com/api/ ; https://help.autods.com/en/articles/12699964-autods-api-feature-automate-product-imports-orders-and-sourcing
- eBay dropshipping policy summaries: https://www.salehoo.com/learn/ebay-dropshipping ; https://super-ds.com/blog/ebay-dropshipping-policy-2026
- eBay Application Growth Check: https://developer.ebay.com/grow/application-growth-check ; API call limits: https://developer.ebay.com/develop/get-started/api-call-limits
- Spocket/Zendrop API landscape: https://apitracker.io/a/spocket-co ; https://easync.io/articles/zendrop-review/
