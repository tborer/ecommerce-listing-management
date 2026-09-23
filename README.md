# ecommerce-listing-mgmt

eBay / AliExpress / DSers / WooCommerce dropship listing automation ("Branch 11"), staged here as the foundation for a future web app. This is a restructured, package-ified copy of a system that has been running in production since 2026-08.

## Status

**This repo is not yet what runs in production.** The live system still runs, unchanged, as a set of standalone cron scripts on the original host. This repo is a cleaned-up, importable-package version of the same code, meant as the starting point for turning it into a real web app (a UI over the candidate review / listing / order / stock state, instead of email reports and JSON files). Bringing this repo live (pointing cron at it, or replacing cron with a proper service) is a separate, deliberate next step -- not done yet.

## What it does

Discovers candidate products from eBay Deals pages (and a rotating pool of other sources), sources a matching AliExpress supplier via DSers, computes profit margin, and auto-publishes the most profitable high-confidence matches to eBay production. Monitors resulting orders, tracks DSers supplier mapping/stock/price drift, and emails daily reports. Full current-state rules and parameters: [`docs/listing-rules.md`](docs/listing-rules.md). Web app product plan: [`docs/web-app-plan.md`](docs/web-app-plan.md). Outstanding-work backlog, prioritization, and the reasoning behind past decisions: [`docs/production-readiness.md`](docs/production-readiness.md) (a point-in-time snapshot as of this repo's initial import -- the live copy on the original host keeps evolving independently until this repo becomes the source of truth).

## Layout

```
src/ecommerce_listing_mgmt/
    pipeline.py           # main orchestrator: discovery -> match -> profit calc -> auto-list
    ebay/
        auth.py            # OAuth (sandbox/production), refresh token handling
        listing.py         # Inventory API: create/publish/withdraw listings
        orders.py          # Fulfillment API: read open orders, mirror to WooCommerce bridge
        deletion_webhook.py  # eBay Marketplace Account Deletion compliance endpoint
    aliexpress/
        checkout.py        # supplier checkout login/2FA research (manual-run only, see file docstring)
    dsers/
        mcp_client.py      # shared DSers MCP OAuth 2.1 + PKCE plumbing
        search.py          # supplier match search
        mapping.py         # supplier SKU mapping for auto-listed items
        price_check.py     # daily cost-drift check against DSers
        stock_check.py     # supplier out-of-stock detection
        stock_remap.py     # supplier remap when stock_check flags a problem
        variation_check.py # multi-variant aspect resolution via DSers option data
    woocommerce/
        client.py          # REST client for the DSers-bridge WooCommerce store
    enrichment/
        enrichment.py      # image/description enrichment via DSers + local OCR
    llm_assist/
        llm_assist.py      # last-resort listing-aspect resolution via a local LLM
    reporting/
        email_report.py    # daily report email (plain SMTP, no LLM)

tests/          # existing eBay auth + WooCommerce client tests
scripts/        # the cron entry-point shell wrappers, carried over for reference
docs/           # current-state rules/parameters reference
config/         # *.example.json templates for every credential file below -- fill in and
                # keep the real files out of git (see .gitignore)
```

## Related repo

[`tb-ecommerce-proxy`](https://github.com/tborer/tb-ecommerce-proxy) is a separate, already-deployed (Vercel) transparent reverse proxy that fronts the WooCommerce bridge store for DSers, which won't accept the backend's real Tailscale URL. Left as its own repo since it has its own deploy pipeline; not folded in here.

## Credentials

Nothing in this repo is a real secret. Every credential file has a `config/*.example.json` template here showing its shape with empty values. The real files (`branch11_unattended_creds.json`, `branch11_woocommerce_creds.json`, `ebay_mad_webhook_config.json`, `branch11_dsers_mcp_tokens.json`) are gitignored and must be created locally (mode 600) wherever this actually runs. Interactive/manual credential use (as opposed to the unattended cron paths) goes through Bitwarden instead -- see `docs/listing-rules.md`'s Credential Handling section.

**Hard constraints carried over unchanged from the live system** (see `docs/listing-rules.md` for the full, current version -- don't rely on this summary): never place a real AliExpress supplier order autonomously; never create/edit/price a real eBay listing autonomously outside the existing top-N auto-list carve-out; never send a customer-facing message autonomously; any transaction-capable credential requires asking first, every time.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
cp config/branch11_unattended_creds.example.json branch11_unattended_creds.json   # fill in, chmod 600
cp config/branch11_woocommerce_creds.example.json branch11_woocommerce_creds.json # fill in, chmod 600
```

Several `dsers/*.py` and `enrichment/enrichment.py` modules were originally standalone [PEP 723](https://peps.python.org/pep-0723/) `uv run --script` files with their own inline dependency blocks (still visible as comments at the top of each file) -- they now import from the installed package instead of a flat sibling directory, so run them as installed console scripts or via `python3 -m ecommerce_listing_mgmt.dsers.search` rather than standalone `uv run`.
