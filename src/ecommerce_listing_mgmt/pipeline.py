#!/usr/bin/env python3
"""Branch 11 pipeline, Steps 1-4: eBay Deals discovery -> AliExpress match -> profit calc.

Formalizes the interactive prototyping done 2026-08-25 into a reusable script.
See ~/openclaw-troubleshooting/branch11-listing-rules.md for the current
parameters/rubric this implements, and STATUS.md for the full decision history.

Steps 1-4 (research/matching) are read-only, no credentials used -- discovery
is tiered (CORE_DEALS_URLS always, EXPANSION_DEALS_URLS only if CORE didn't
find enough good candidates, see run_pipeline()'s min_good_items). **Step 7
auto-listing (added 2026-08-27, Travis's explicit decision) is NOT read-only**
-- see AUTO_LIST_ENABLED/run_auto_listing() below: after research, the top
AUTO_LIST_TOP_N most profitable HIGH-verdict passing candidates get published
for real on PRODUCTION, unattended, no chat approval, via a dedicated local
credential path (branch11_unattended_creds.json) -- see
run_auto_listing()'s docstring for the full credential-access design.

Usage:
    python3 branch11_pipeline.py [--price-ceiling 100] [--fee-pct 0.17] [--margin-pct 0.16]
        [--max-candidates 15] [--max-query-variations 2]
        [--auto-list-top-n 5] [--auto-list-replenish-buffer 5] [--no-auto-list] [--min-good-items 5]

Output: writes branch11_candidates.json in this directory.
"""
from __future__ import annotations

import argparse
import difflib
import json
import math
import re
import subprocess
import time
import urllib.request
from dataclasses import dataclass, asdict
from datetime import date
from pathlib import Path
from urllib.parse import quote

OUT_DIR = Path(__file__).parent

# Kill switch (Hard Constraints, branch11-listing-rules.md: "an easy kill
# switch, potentially at multiple points"). Flip to False to halt all
# autonomous production publishing immediately -- research/matching/email
# reporting continue unaffected either way. Also overridable per-run via
# --no-auto-list without editing this file.
AUTO_LIST_ENABLED = True

# Default count of top-profit HIGH-verdict passing candidates auto-published
# per run -- Travis's 2026-08-27 decision. Configurable via --auto-list-top-n.
AUTO_LIST_TOP_N_DEFAULT = 5

# Added 2026-08-31 (PRODUCTION-READINESS.md Item 36, Travis's explicit
# request): how many EXTRA candidates beyond auto_list_top_n run_auto_listing()
# is willing to verify+attempt when earlier ones fail, instead of just leaving
# the slot unfilled (the original 2026-08-30 design's tradeoff -- see memory
# incident-2026-08-30-branch11-rate-limit). Bounds a bad day's extra browser-
# verification work; a day where the first auto_list_top_n all succeed costs
# nothing extra regardless of this value. Configurable via
# --auto-list-replenish-buffer.
AUTO_LIST_REPLENISH_BUFFER_DEFAULT = 5

# Exclusion list for whole product categories this account can't/shouldn't
# sell -- added 2026-08-31 (PRODUCTION-READINESS.md Item 39), Travis's
# explicit request: cut off excluded items as early in the pipeline as
# possible, not just at listing time. Checked in _scrape_ebay_tiles() --
# right when a raw eBay tile is scraped, before it ever becomes an
# EbayCandidate -- so an excluded item costs nothing beyond the tile scrape
# itself: no DSers search (step 2), no judging/profit-calc (step 3 Pass 1),
# no browser verification (step 3 Pass 2), no listing attempt. First entry:
# eBay's Medical Devices policy blocks anti-snoring/mouth-tape items on this
# account -- confirmed live 2026-08-31, 4-for-4, eBay's own publish_offer
# error literally said "please do not relist" (see PRODUCTION-READINESS.md
# Item 35 / memory project-branch11-ebay-medical-devices-block). Same
# "deliberately blunt, explainable, hand-curated" pattern as
# _ACCESSORY_WORDS/_BRAND_TOKENS below -- a keyword substring match against
# the eBay tile's own title, checked case-insensitively. Add a new (reason,
# keyword-set) entry here as new excluded categories are identified; never
# loosen the match logic itself to be fuzzier/smarter -- a false exclusion
# (skipping something sellable) just costs one candidate a day, a false
# inclusion (still sourcing eBay Medical Devices items) costs real wasted
# work downstream and repeats a listing attempt eBay explicitly said not to
# retry.
EXCLUDED_CATEGORIES: list[tuple[str, set[str]]] = [
    ("eBay Medical Devices policy (anti-snoring/mouth-tape) -- PRODUCTION-READINESS.md Item 35",
     {"mouth tape", "anti-snoring", "anti snoring", "snore", "snoring"}),
]


def _excluded_reason(title: str) -> str | None:
    """Returns the curated exclusion reason if `title` matches one of
    EXCLUDED_CATEGORIES' keyword sets, else None."""
    lowered = title.lower()
    for reason, keywords in EXCLUDED_CATEGORIES:
        if any(kw in lowered for kw in keywords):
            return reason
    return None

# Ledger of every candidate ever auto-published on production, keyed by sku --
# prevents re-attempting (and risking a duplicate offer error on) an item
# that's already listed if it resurfaces in a later day's Deals scrape. Also
# the start of the tracking ledger flagged as needed for Step 7.5 (weekly
# sourced-item price-check job, branch11-listing-rules.md next-steps item 22).
AUTO_LISTED_LEDGER_PATH = OUT_DIR / "branch11_auto_listed.json"

# Rotating pool, not a fixed daily list (2026-08-29, Travis's decision,
# supersedes the 2026-08-28 CORE/EXPANSION split below it in this history).
# Each day scrapes ROTATION_PAGES_PER_DAY_DEFAULT pages selected by
# _select_daily_urls() -- a deterministic block rotation keyed off the date,
# so the same few pages aren't scraped every single day and the whole pool
# gets covered over successive days as Travis adds more URLs to it. If that
# day's rotation doesn't turn up enough good (HIGH-verdict, profit-passing)
# candidates, run_pipeline() falls back to scraping the *rest* of the pool
# (everything not already scraped today) -- same "expand only if needed"
# behavior as before, just no longer tied to a separately curated list.
#
# Deliberately chosen for generic, non-fitment/non-authenticity-sensitive
# goods (unlike e.g. fine jewelry, coins, or automotive parts, which don't
# suit generic AliExpress sourcing) -- same profile as items that have
# already sold well. Travis replaced the list wholesale 2026-08-29 with a
# wider category/site mix so the eBay account gets a good variety of items,
# same day he dropped the rotation to 2 pages/day (below) so each URL gets
# scraped more thoroughly before moving on. Two entries (droppery.io,
# winninghunter.com) are editorial "best dropshipping products" blog pages,
# not eBay listings -- confirmed 2026-08-29 they carry no links/ids/images of
# their own, just product/niche names in a table or bullet list. Those go
# through KEYWORD_SOURCES below instead of EBAY_TILE_JS: each page's product
# names are extracted as keywords, then each keyword is turned into its own
# eBay *search* (via EBAY_SEARCH_TILE_JS) to find real, priced listings --
# from there they flow through the exact same matching/profit-check code as
# any other tile, so a bad keyword just fails that gate rather than reaching
# auto-listing.
DEALS_URL_POOL = [
    "https://www.ebay.com/deals/tech/computer-accessories",
    "https://www.amazon.com/bestsellers",
    "https://www.ebay.com/deals/home-garden/kitchen-dining-bar",
    "https://www.ebay.com/deals/home-garden/tools",
    "https://droppery.io/the-best-dropshipping-products-for-2026-a-proven-selection-formula-with-winning-niches/",
    "https://www.ebay.com/deals/home-garden/pet-supplies",
    "https://www.salehoo.com/learn/finding-trending-products-to-dropship",
    "https://winninghunter.com/blog/best-dropshipping-products",
    "https://www.ebay.com/deals/trending/home-garden/crafts",
    "https://usadrop.com/trending-dropshipping-products-april",
    "https://www.ebay.com/deals/trending/other-deals/office-furniture-supplies",
    "https://www.repricer.com/blog/trending-products-for-dropshipping",
    "https://www.ebay.com/deals/trending/home-garden/yard-garden-outdoor-living",
    "https://www.ebay.com/deals/trending/automotive/car-accessories",
    "https://www.ebay.com/deals/trending/home-garden/home-improvement",
    "https://www.ebay.com/deals/trending/fashion/sunglasses",
    "https://www.ebay.com/deals/trending/home-garden/lamps-lighting-ceiling-fans",
    "https://www.ebay.com/deals/trending/home-garden/home-organization",
    "https://www.ebay.com/deals/trending/tech/memory-drives-storage",
    "https://www.ebay.com/deals/trending/tech/headphones-portable-audio",
    "https://www.ebay.com/deals/trending/sporting-goods/exercise-fitness",
]

# 2026-09-05: Travis mixed in 13 non-eBay discovery sources into the pool
# for variety (real-listing bestseller pages: Amazon, Walmart, AliExpress
# trending; plus blog-style "trending products" articles: SaleHoo, usadrop,
# repricer, doba, syncee). 5 of his original 13 URLs were dead/wrong-path on
# live check (oberlo.com/trending-products, nextag.com entirely,
# pricegrabber.com/popular-items, pricerunner.com/trending-products,
# salehoo.com/popular-products) and were never added -- Travis is
# researching replacements himself. Of the 8 that resolved, 4 more were
# dropped after live-testing found real, unfixable blockers rather than a
# DOM/selector problem: `walmart.com/top-sellers` and the doba.com blog post
# both hard-block automated navigation (Walmart shows a PerimeterX-style
# "Robot or human? Activate and hold the button" challenge; doba.com shows a
# Cloudflare "Performing security verification" interstitial) -- not
# attempted to defeat, that's bot-detection evasion, not a scraper bug.
# `aliexpress.com/trending` doesn't actually exist -- AliExpress's own
# client-side router serves its real 404 page for that path (confirmed via
# `document.title` === "404 page"), despite a plain `curl` returning 200 for
# the SPA shell. `syncee.com`'s article is real and reachable but only lists
# 10 broad categories ("Pet Supplies", "Clothing", ...) -- individual
# products are only ever mentioned inline in prose sentences, no clean
# DOM-extractable list the way every other source has; Travis chose to drop
# it rather than accept the much noisier candidates a category-level eBay
# search would produce. The remaining 4 (Amazon, SaleHoo, usadrop, repricer)
# each got a live-tested KEYWORD_SOURCES extractor built 2026-09-05, same
# process droppery.io/winninghunter.com went through 2026-08-29 -- see
# AMAZON_BESTSELLERS_KEYWORDS_JS / USADROP_KEYWORDS_JS /
# REPRICER_KEYWORDS_JS below (SaleHoo reuses DROPPERY_KEYWORDS_JS verbatim,
# same table structure). Pool is now 21 URLs: the original 15 eBay Deals
# pages, droppery.io/winninghunter.com from 2026-08-29, plus these 4 new
# ones -- all currently produce real candidates, nothing pending.

ROTATION_PAGES_PER_DAY_DEFAULT = 2  # revised back down from 3 -- Travis's 2026-09-06 ask; other pipeline improvements are already getting more items listed, so no need for wider daily discovery too


def _select_daily_urls(pool: list[str], day: date, pages_per_day: int) -> list[str]:
    """Deterministic block rotation: day 0 gets pool[0:n], day 1 gets
    pool[n:2n], wrapping around once the pool is exhausted -- so growing the
    pool (Travis adding more URLs) naturally lengthens the full rotation
    cycle rather than requiring any state file to track position. Uses the
    proleptic Gregorian ordinal so it's stable across process restarts and
    needs no persisted cursor."""
    if not pool or pages_per_day <= 0:
        return []
    start = (day.toordinal() * pages_per_day) % len(pool)
    n = min(pages_per_day, len(pool))
    return [pool[(start + i) % len(pool)] for i in range(n)]

EBAY_TILE_JS = """() => {
  const tiles = Array.from(document.querySelectorAll(".dne-itemtile[data-listing-id]"));
  const seen = new Set();
  return tiles.map(t => {
    const id = t.getAttribute("data-listing-id");
    const link = t.querySelector("a[href*=\\"/itm/\\"]");
    const title = t.querySelector(".dne-itemtile-title");
    const priceEl = t.querySelector(".dne-itemtile-price");
    const priceText = priceEl ? priceEl.textContent.trim() : null;
    const priceNum = priceText ? parseFloat(priceText.replace(/[^0-9.]/g, "")) : null;
    return { id, title: title ? title.textContent.trim() : null, priceNum,
             url: id ? ("https://www.ebay.com/itm/" + id) : null };
  }).filter(i => i.title && i.priceNum !== null && !seen.has(i.id) && seen.add(i.id));
}"""

EBAY_SEARCH_TILE_JS = """() => {
  // 2026-08-29: rewritten after live-testing found the originally-assumed
  // `.s-item`/`.s-item__*` classes (a well-known older eBay search-results
  // convention, never actually verified live for this exact page before
  // shipping) return ZERO tiles on eBay's real current search page -- eBay
  // has since moved to a `.s-card`/`.su-card-container` component structure.
  // Confirmed live: 62 real `.s-card` elements on a real "Mouth Tape"
  // search, correct id/title/price extracted from all of them. `.s-card__title`
  // carries a hidden accessibility suffix ("...Opens in a new window or tab")
  // baked into its own textContent that must be stripped, not just trimmed.
  const cards = Array.from(document.querySelectorAll("li.s-card"));
  const seen = new Set();
  const results = [];
  for (const card of cards) {
    const linkEl = card.querySelector("a[href*=\\"/itm/\\"]");
    const idMatch = linkEl ? linkEl.href.match(/\\/itm\\/(\\d+)/) : null;
    const id = idMatch ? idMatch[1] : null;
    const titleEl = card.querySelector(".s-card__title");
    let title = titleEl ? titleEl.textContent.trim() : null;
    if (title) title = title.replace(/Opens in a new window or tab$/i, "").trim();
    const priceEl = card.querySelector(".s-card__price");
    const priceText = priceEl ? priceEl.textContent.trim() : null;
    const priceMatch = priceText ? priceText.match(/\\$([\\d,]+\\.?\\d*)/) : null;
    const priceNum = priceMatch ? parseFloat(priceMatch[1].replace(/,/g, "")) : null;
    if (!id || !title || title === "Shop on eBay" || priceNum === null || seen.has(id)) continue;
    seen.add(id);
    results.push({ id, title, priceNum, url: "https://www.ebay.com/itm/" + id });
  }
  return results;
}"""

# Cheap tile-count-only JS for _scroll_to_load_more() -- same selectors as
# EBAY_TILE_JS/EBAY_SEARCH_TILE_JS above, just a .length instead of building
# the full tile array, so polling between scrolls doesn't pay for the full
# extraction (title/price/id parsing) on every iteration.
EBAY_TILE_COUNT_JS = """() => document.querySelectorAll(".dne-itemtile[data-listing-id]").length"""
EBAY_SEARCH_TILE_COUNT_JS = """() => document.querySelectorAll("li.s-card").length"""

# Extracts product/niche names from droppery.io's "best dropshipping
# products" article -- confirmed 2026-08-29 its listings are plain
# <table><tbody><tr><td>Name</td>...</tr> rows with no links/ids of their
# own, so the name text itself is all there is to scrape.
DROPPERY_KEYWORDS_JS = """() => {
  const seen = new Set();
  const out = [];
  document.querySelectorAll("table tbody tr").forEach(tr => {
    const cell = tr.querySelector("td");
    if (!cell) return;
    const name = cell.textContent.trim();
    if (name && name.length <= 60 && !seen.has(name.toLowerCase())) {
      seen.add(name.toLowerCase());
      out.push(name);
    }
  });
  return out;
}"""

# Same idea for winninghunter.com's article -- confirmed 2026-08-29 its
# products are bullet points shaped like "<li><strong>Name:</strong>
# description...</li>", no links/ids either.
WINNINGHUNTER_KEYWORDS_JS = """() => {
  const seen = new Set();
  const out = [];
  document.querySelectorAll("li").forEach(li => {
    const strong = li.querySelector("strong, b");
    if (!strong) return;
    const name = strong.textContent.trim().replace(/:$/, "");
    if (name.length >= 3 && name.length <= 60 && !seen.has(name.toLowerCase())) {
      seen.add(name.toLowerCase());
      out.push(name);
    }
  });
  return out;
}"""

# amazon.com/bestsellers (added 2026-09-05, item 34): unlike droppery.io/
# winninghunter.com this IS a real listing page with its own product links
# and prices -- but deliberately still routed through KEYWORD_SOURCES rather
# than given its own EBAY_TILE_JS-equivalent path, because a new eBay
# listing's title/price get built directly from ebay_item.title/priceNum
# (see run_auto_listing()/woo mirror) -- Amazon's own retail price is not a
# real eBay comp, so using it directly would seed listings with the wrong
# price basis. Extracting just the product name and re-running it through a
# real eBay search (same as the blog sources) keeps every listing's price
# grounded in an actual eBay market price regardless of where the name idea
# came from. Confirmed live 2026-09-05: product tiles are
# `div[data-asin]`, each holding an `<a href="/.../dp/...">` (title) and a
# separate `<a href="/product-reviews/...">` (rating, must be excluded) --
# no stable non-hashed CSS class exists for the title text itself, so the
# title is picked by href pattern instead of class name.
AMAZON_BESTSELLERS_KEYWORDS_JS = """() => {
  const seen = new Set();
  const out = [];
  document.querySelectorAll("div[data-asin]").forEach(tile => {
    if (!tile.getAttribute("data-asin")) return;
    const link = Array.from(tile.querySelectorAll("a")).find(a => {
      const href = a.getAttribute("href") || "";
      const text = a.textContent.trim();
      return href.includes("/dp/") && !href.includes("/product-reviews/") &&
             text && !/^\\$/.test(text) && !/stars/i.test(text);
    });
    const name = link ? link.textContent.trim() : null;
    if (name && name.length >= 3 && name.length <= 150 && !seen.has(name.toLowerCase())) {
      seen.add(name.toLowerCase());
      out.push(name);
    }
  });
  return out;
}"""

# usadrop.com's "Trending Dropshipping Products" article (added 2026-09-05,
# item 34): confirmed live it has no table/list structure at all -- each
# product name is a plain inline <strong> tag inside a paragraph (one
# unrelated "Monthly update:" strong also matches, excluded by its colon --
# no real product name in this article contains one).
USADROP_KEYWORDS_JS = """() => {
  const seen = new Set();
  const out = [];
  document.querySelectorAll("strong").forEach(el => {
    const name = el.textContent.trim();
    if (name.length >= 3 && name.length <= 90 && !name.includes(":") && !seen.has(name.toLowerCase())) {
      seen.add(name.toLowerCase());
      out.push(name);
    }
  });
  return out;
}"""

# repricer.com's "Top Trending Dropshipping Products" article (added
# 2026-09-05, item 34): confirmed live its 10 real products are numbered
# "<h2>1. Smart Water Bottles</h2>"-style headings; other h2's on the page
# (sourcing tips, FAQ-style sections) have no leading number and are
# excluded by the regex not matching them.
REPRICER_KEYWORDS_JS = """() => {
  const seen = new Set();
  const out = [];
  document.querySelectorAll("h2").forEach(el => {
    const text = el.textContent.trim();
    const m = text.match(/^\\d+\\.\\s*(.+)/);
    if (!m) return;
    const name = m[1].trim();
    if (name.length >= 3 && name.length <= 90 && !seen.has(name.toLowerCase())) {
      seen.add(name.toLowerCase());
      out.push(name);
    }
  });
  return out;
}"""

# Maps a DEALS_URL_POOL entry to its keyword-extraction JS, for pages that
# aren't eBay listings at all (see DEALS_URL_POOL comment above). Any other
# non-eBay source Travis adds later needs its own entry here (or its own
# EBAY_TILE_JS-equivalent, if it does turn out to carry real listing links)
# before it'll do anything useful in the rotation.
KEYWORD_SOURCES = {
    "https://www.amazon.com/bestsellers": AMAZON_BESTSELLERS_KEYWORDS_JS,
    # salehoo.com's "25 Trending Products" article (added 2026-09-05, item
    # 34): confirmed live to use the exact same plain
    # <table><tbody><tr><td>Name</td>...</tr> structure as droppery.io, so it
    # reuses DROPPERY_KEYWORDS_JS rather than needing its own JS. A
    # definitions-legend table further down the page ("Fad", "Viral hit",
    # "Seasonal", "Durable trend") also matches the selector and leaks into
    # the tail of the extracted list, but MAX_KEYWORDS_PER_SOURCE (6) only
    # ever takes the first 6 rows, which are all real product names -- so
    # this is harmless in practice, not worth a stricter selector.
    "https://www.salehoo.com/learn/finding-trending-products-to-dropship": DROPPERY_KEYWORDS_JS,
    "https://usadrop.com/trending-dropshipping-products-april": USADROP_KEYWORDS_JS,
    "https://www.repricer.com/blog/trending-products-for-dropshipping": REPRICER_KEYWORDS_JS,
    "https://droppery.io/the-best-dropshipping-products-for-2026-a-proven-selection-formula-with-winning-niches/": DROPPERY_KEYWORDS_JS,
    "https://winninghunter.com/blog/best-dropshipping-products": WINNINGHUNTER_KEYWORDS_JS,
}

# Caps how many keywords one keyword-source page can fan out into (each
# keyword costs its own eBay search-page visit) -- otherwise a single blog
# URL landing in the daily rotation could turn into dozens of extra page
# loads and blow out run time. These are only ever a couple of the pool's
# many URLs, so a handful of keywords per page still contributes a
# reasonable, if smaller, slice of that day's candidates.
MAX_KEYWORDS_PER_SOURCE = 6

# Caps raw tile volume per DEALS_URL_POOL entry (Travis's 2026-08-30
# decision, see memory incident-2026-08-30-branch11-rate-limit): before
# this, neither EBAY_TILE_JS nor _scrape_tiles_for_url() capped how many
# tiles one URL could contribute, and discover_pipeline()'s own --stage
# discover invocation (branch11_daily_run.sh) never passed --limit either
# -- so one day's rotated pages returning unusually many tiles (2026-08-30:
# 339 candidates from 2 pages, vs. a typical ~20-59) had nothing capping it
# before flowing into the per-item DSers-search step downstream. Applies
# uniformly to both scrape paths in _scrape_tiles_for_url() -- a plain eBay
# Deals page, and a keyword-source page's combined results across all of
# its MAX_KEYWORDS_PER_SOURCE keyword searches.
#
# Raised 50 -> 100 on 2026-09-02 alongside _scroll_to_load_more() (below):
# live-tested that day's 2 rotated URLs directly -- a plain Deals page
# (yard-garden-outdoor-living) rendered only 24 tiles on initial load but
# lazy-loaded to 70+ once scrolled to the bottom, meaning this cap was
# never actually binding (nothing scrolled, so nothing came close to the
# old 50). 100 gives headroom above the ~70-72 observed on a real page
# without being effectively unlimited. If a URL still can't surface a
# small number of good (HIGH-verdict, profit-passing) candidates within
# its first 100 scrolled tiles, that's a signal the URL itself is just
# thin/expensive that day (see EXCLUDED_CATEGORIES / --price-ceiling) --
# not something a bigger number here fixes.
MAX_TILES_PER_URL = 100

# How many times _scroll_to_load_more() will scroll-to-bottom-and-wait
# looking for more lazy-loaded tiles, and how long it waits after each
# scroll for that content to render. Added 2026-09-02: confirmed live that
# eBay Deals pages use scroll-triggered lazy loading (EBAY_TILE_JS/
# EBAY_SEARCH_TILE_JS only ever saw whatever was in the initial viewport
# render before this) -- one scroll took a real page from 24 to 72 tiles.
# Stops early once a scroll stops growing the tile count (confirmed live:
# a thin page like car-accessories just stays flat, cheap no-op) rather
# than always paying for MAX_SCROLL_ATTEMPTS on every URL.
MAX_SCROLL_ATTEMPTS = 4
SCROLL_SETTLE_SECONDS = 1.5

ALIEXPRESS_TILE_JS = """() => {
  const links = Array.from(document.querySelectorAll("a.search-card-item[href*=\\"/item/\\"]"));
  const seen = new Set();
  const results = [];
  for (const l of links) {
    const m = l.href.match(/\\/item\\/(\\d+)\\.html/);
    if (!m || seen.has(m[1])) continue;
    seen.add(m[1]);
    const img = l.querySelector("img[alt]");
    const title = img ? img.getAttribute("alt") : null;
    let imageUrl = img ? img.getAttribute("src") : null;
    if (imageUrl && imageUrl.startsWith("//")) imageUrl = "https:" + imageUrl;
    const wrapper = l.closest("[class*=search-item-card-wrapper]") || l.parentElement.parentElement.parentElement;
    const text = wrapper ? wrapper.innerText : "";
    const prices = Array.from(text.matchAll(/\\$\\d+\\.\\d{2}/g)).map(x => x[0]);
    results.push({ id: m[1], title, prices, imageUrl,
                   url: "https://www.aliexpress.us/item/" + m[1] + ".html" });
  }
  return results.slice(0, 20);
}"""

# Extracts the authoritative single price AND shipping cost off an AliExpress
# product page in one visit, for verifying/replacing the noisy multi-value
# prices scraped off search tiles (see find_best_ali_match). Price's primary
# source is the page's own schema.org JSON-LD Product block (`offers.price`)
# -- confirmed live 2026-08-26 to be present and to hold a single clean
# numeric price even when the search tile for the same item scraped several
# unrelated dollar amounts off the card (e.g. tile prices ['$68.94', '$40.49',
# '$17.24'] vs. this item's real, single listed price of $68.94). This is far
# more stable than the page's visible price element, which lives behind a
# CSS-module class with a content-hash suffix (e.g.
# "price-default--current--F8OlYIo") that can change across AliExpress
# frontend deploys -- kept only as a fallback below. Shipping cost comes from
# `.dynamic-shipping-line` (confirmed live 2026-08-26, not a hashed class) --
# its first line reads either "Free shipping" (-> $0) or "Shipping: $X.XX".
# Description also comes off the same ld+json Product block's `description`
# field (added 2026-08-27, zero extra page visits -- confirmed live present
# and clean on real items, e.g. "Durable embroidered USA flag, 3x5 ft,
# waterproof polyester fabric..."). Travis's decision, 2026-08-27: the eBay
# listing's description must be the AliExpress item's own description, same
# reasoning as the title -- it's what's actually being shipped, so it keeps
# the listing honest even when the research-stage match is imperfect. No DOM
# fallback for this one (unlike price) -- an absent/unverified description
# should read as None and block listing (see list_candidate()), not silently
# fall back to something unreviewed.
PRODUCT_PAGE_JS = """() => {
  let priceInfo = null;
  let description = null;
  try {
    const scripts = Array.from(document.querySelectorAll('script[type="application/ld+json"]'));
    outer:
    for (const s of scripts) {
      let data;
      try { data = JSON.parse(s.textContent); } catch (e) { continue; }
      const items = Array.isArray(data) ? data : [data];
      for (const item of items) {
        if (item["@type"] === "Product") {
          if (item.description) description = item.description;
          if (item.offers && item.offers.price) {
            priceInfo = {
              price: parseFloat(item.offers.price),
              currency: item.offers.priceCurrency || null,
              availability: (item.offers.availability || "").split("/").pop() || null,
              source: "ld+json",
            };
          }
          if (priceInfo) break outer;
        }
      }
    }
  } catch (e) { /* fall through to DOM fallback below */ }
  if (!priceInfo) {
    const el = document.querySelector('[class*="price-default--current"]');
    if (el) {
      const n = parseFloat(el.textContent.replace(/[^0-9.]/g, ""));
      if (!isNaN(n)) priceInfo = { price: n, currency: "USD", availability: null, source: "dom-fallback" };
    }
  }

  let shipping = null;
  const shipLine = document.querySelector(".dynamic-shipping-line");
  if (shipLine) {
    const text = shipLine.textContent.trim();
    if (/free shipping/i.test(text)) {
      shipping = 0.0;
    } else {
      const m = text.match(/\\$([\\d.]+)/);
      if (m) shipping = parseFloat(m[1]);
    }
  }

  // Added 2026-08-31 (PRODUCTION-READINESS.md Item 26): real physical
  // dimensions live either in a structured Specifications DOM section (plain
  // text key/value pairs, e.g. "Base to top distance" / "44cm") or stated in
  // ordinary body-page prose (e.g. "the table measures 26.5cm long, 16.8cm
  // wide, and 11cm high") -- confirmed live neither needs OCR/images, today's
  // scraper just never looked. Capture both here (cheap, same page visit,
  // no extra navigation) and let _parse_dimensions_in() in
  // branch11_ebay_listing.py do the actual number/unit/context parsing.
  let specifications = null;
  const specEl = document.querySelector('[class*="specification"], [class*="Specification"]');
  if (specEl) specifications = specEl.innerText.slice(0, 2000);

  // Scoped to the product-detail-page's own main content area, NOT
  // document.body -- confirmed live 2026-08-31 that scanning the whole page
  // picks up unrelated "recommended for you" / cross-sell carousel text
  // (other sellers' completely different products, e.g. a Halloween toy's
  // "6.5cm" description bleeding into a folding table's dimension search).
  // `[class*="pdp-body-top"]` is AliExpress's own stable product-detail-page
  // container naming (confirmed present via a live DOM-ancestor walk from
  // the specifications element itself) -- falls back to document.body only
  // if that container isn't found, so this degrades gracefully rather than
  // silently returning nothing on a page-template variation.
  //
  // **Also temporarily hides `[class*="fusion-card"], [class*="fusion-page-card"]`
  // descendants before reading text, restoring them immediately after** --
  // confirmed live 2026-08-31 that an "Add more" cross-sell widget (other
  // sellers' bundled/suggested products) is nested INSIDE pdp-body-top
  // itself, not a sibling outside it, so the container-scoping above alone
  // doesn't exclude it. "fusion-card"/"fusion-page-card" is AliExpress's own
  // generic naming for these recommendation-card widgets (confirmed via a
  // live DOM-ancestor walk from the actual "Add more" text node).
  // Hide-then-restore on the LIVE DOM, not a detached clone -- `innerText`
  // depends on real layout/rendering, which a cloned-but-unattached node
  // doesn't reliably have; hiding elements makes the browser's own
  // (already-correct) "exclude non-rendered content" behavior do the work.
  const scopeEl = document.querySelector('[class*="pdp-body-top"]') || document.body;
  const crossSellEls = Array.from(scopeEl.querySelectorAll('[class*="fusion-card"], [class*="fusion-page-card"]'));
  const priorDisplay = crossSellEls.map(el => el.style.display);
  crossSellEls.forEach(el => { el.style.display = "none"; });
  const scopedText = scopeEl.innerText;
  crossSellEls.forEach((el, i) => { el.style.display = priorDisplay[i]; });

  const dimensionLines = [];
  const measurementPattern = /\\d+(\\.\\d+)?\\s*(cm|mm|in\\.?|inch|inches|")/i;
  for (const line of scopedText.split("\\n")) {
    if (measurementPattern.test(line) && line.length < 300) {
      dimensionLines.push(line.trim());
      if (dimensionLines.length >= 30) break;
    }
  }

  return { price: priceInfo, shipping, description, specifications, dimensionLines };
}"""


def run_openclaw(*args: str, timeout: int = 90) -> dict:
    """Run an `openclaw` CLI command with --json and parse its output."""
    proc = subprocess.run(
        ["openclaw", *args, "--json"], capture_output=True, text=True, timeout=timeout
    )
    out = proc.stdout
    brace = out.find("{")
    bracket = out.find("[")
    starts = [i for i in (brace, bracket) if i != -1]
    if not starts:
        raise RuntimeError(f"No JSON in openclaw output: {out!r} / stderr={proc.stderr!r}")
    return json.loads(out[min(starts):])


def check_phase3_variant_opportunity(category_id: str, ali_url: str, unresolvable_variation_aspects: list[str]) -> dict | None:
    """Bridges to `branch11_variation_check.py` (a separate `uv run` process
    -- that module needs `mcp`/`httpx2`, which this plain-`python3` process
    doesn't have, same reason `branch11_dsers_search.py` is its own script
    rather than an import here). Only ever called for a candidate that's
    ALREADY about to be skipped for `needs_unresolvable_variation`, so this
    extra subprocess + DSers-API-call cost is rare (a handful/day at most),
    matching this project's frugal DSers-call-volume design elsewhere.
    Fails closed: any subprocess/parse error means "no opportunity," never
    guessed -- this only ever flags a genuine, fully-resolved opportunity
    for Travis's own chat-approved review, never lists anything itself."""
    try:
        proc = subprocess.run(
            [
                "uv", "run", "branch11_variation_check.py",
                "--env", "production", "--category-id", str(category_id), "--ali-url", ali_url,
                *[a for name in unresolvable_variation_aspects for a in ("--unresolvable-aspect", name)],
                "--credential-mode", "local",
            ],
            cwd=OUT_DIR, capture_output=True, text=True, timeout=90,
        )
        return json.loads(proc.stdout).get("opportunity")
    except Exception:  # noqa: BLE001 -- fail-closed, see docstring
        return None


def browser_start() -> None:
    subprocess.run(["openclaw", "browser", "start"], capture_output=True, text=True)


def browser_stop() -> None:
    subprocess.run(["openclaw", "browser", "stop"], capture_output=True, text=True)


def browser_navigate(url: str, timeout: int = 90) -> None:
    """Navigate; tolerates the CLI's own round-trip timeout on slow JS-heavy pages
    (the navigation itself still completes server-side -- see STATUS.md 2026-08-25
    Step 2 entry). Caller should follow with an evaluate() to confirm real state."""
    try:
        subprocess.run(["openclaw", "browser", "navigate", url], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        pass


def browser_evaluate(js_fn: str) -> object:
    result = run_openclaw("browser", "evaluate", "--fn", js_fn)
    return result.get("result")


@dataclass
class EbayCandidate:
    id: str
    title: str
    priceNum: float
    url: str


@dataclass
class AliMatch:
    id: str
    title: str
    prices: list
    url: str
    verdict: str  # HIGH / MEDIUM / NONE
    reasons: list
    match_score: int = 0  # 0-100, see judge_match() -- numeric complement to verdict
    imageUrl: str | None = None
    price_verified: bool = False  # True once `prices` reflects a real product-page visit, not just the search-tile scrape
    shipping_cost: float | None = None  # From the same product-page visit; None means unverified/unknown, not necessarily free
    description: str | None = None  # From the same product-page visit's ld+json Product block; None means unverified -- see list_candidate()
    dimension_text: str | None = None  # Added 2026-08-31 (Item 26): raw Specifications-table + dimension-bearing body text from the same product-page visit, fed into _resolve_aspect_value()'s dimension tier -- see PRODUCT_PAGE_JS


@dataclass
class PipelineResult:
    ebay_item: EbayCandidate
    best_ali_match: AliMatch | None
    profit_check: dict | None


_SIZE_RE = re.compile(r"(\d+)[\s-]*(tier|panel|piece|pc|pack)", re.IGNORECASE)
_VOLTAGE_RE = re.compile(r"\bm?(\d{1,2})\s*[- ]?(?:v\b|volt)", re.IGNORECASE)

# 2026-08-27: Travis flagged three real false-HIGH matches from the automated
# runs -- a Bluetooth speaker matched to a *case for* that speaker, a battery
# matched to a flashlight/torch that merely mentions compatible batteries, and
# wireless earbuds matched to unrelated wired earphones -- plus confirmed a
# real good match (battery-to-battery). All three bad ones share a pattern:
# the AliExpress item is a different *kind* of product than the eBay item,
# even though it shares enough vocabulary to score well on keyword overlap
# alone. These lists are a deliberately blunt, explainable fix for that
# specific failure mode -- not a general product classifier.
_ACCESSORY_WORDS = {
    "case", "cover", "holster", "sleeve", "skin", "pouch", "protector",
    "strap", "band", "holder", "clip", "refill",
}
# Deliberately excludes words like "stand", "mount", "dock", "cartridge" --
# each is also a legitimate standalone product category in its own right
# (a docking "stand", a filter "cartridge"), and blacklisting them produced
# real false positives in testing (an HP docking station wrongly flagged as
# an accessory because the Ali title said "docking station stand"; a GE
# water filter wrongly flagged because "cartridge" is just how filters are
# described). The words kept above are ones that reliably mean "this is an
# accessory for something else," not a product category of their own.
_LIGHT_WORDS = {"torch", "flashlight", "lamp", "light", "led"}
_WIRED = {"wired"}
_WIRELESS = {"wireless", "bluetooth"}

# 2026-08-31 (PRODUCTION-READINESS.md Item 19): construction-material
# mismatch check. Real evidence, not speculative: the item this fix was
# opened for (2026-08-25, a dog-crate match with a metal-frame eBay item
# matched to an Oxford-fabric Ali item), plus 8 more real cases found live
# scanning historical judged output while designing this fix (a real leather
# dining-chair set matched to a wood-leg chair set, HIGH 71; a real aluminum
# laptop stand matched to a plastic laptop holder, HIGH 72; several metal/
# steel outdoor tables matched to aluminum ones, etc.) -- none of the checks
# above catch a same-product-type, different-material mismatch.
#
# **Clustered, not exact-word mismatch** -- "aluminum" and "steel" are
# casually synonymous with "metal" in real listings (an eBay title saying
# "Metal Steel" and an Ali title saying "Aluminum Alloy" are very likely
# describing the same real item), so naive exact-word mismatching would
# falsely flag genuine matches. Only a mismatch ACROSS clusters counts, e.g.
# "leather" vs "wood" or "aluminum" vs "plastic" -- those really are
# different physical products regardless of vocabulary overlap elsewhere.
# Same "deliberately blunt, explainable, hand-curated" pattern as
# _ACCESSORY_WORDS/_BRAND_TOKENS above -- extend as new real cases turn up,
# not an attempt at an exhaustive materials taxonomy.
_MATERIAL_CLUSTERS: list[frozenset[str]] = [
    frozenset({"metal", "steel", "aluminum", "aluminium", "iron", "alloy"}),
    frozenset({"wood", "wooden"}),
    frozenset({"plastic"}),
    frozenset({"leather"}),
    frozenset({"fabric", "oxford", "mesh", "canvas", "cloth", "nylon", "polyester"}),
    frozenset({"glass"}),
    frozenset({"rattan", "wicker"}),
    frozenset({"ceramic"}),
]


def _cluster_hits(words: set[str], clusters: list[frozenset[str]]) -> tuple[set[int], set[str]]:
    """Which of `clusters` at least one word in `words` belongs to, and which
    actual words matched (for building an explainable reason string)."""
    ids: set[int] = set()
    hits: set[str] = set()
    for i, cluster in enumerate(clusters):
        inter = words & cluster
        if inter:
            ids.add(i)
            hits |= inter
    return ids, hits

# 2026-08-29: found live while evaluating the DSers MCP's dsers_find_product
# as a Steps 2-4 alternative -- a real eBay "Apple Pencil Pro" got matched
# HIGH-confidence to a $3.82 "Universal Stylus... For Apple Pencil" (a
# generic third-party knockoff, not a real Apple product), because the word
# "apple" and "pencil" both literally appear in the Ali title. Wasn't caught
# by _ACCESSORY_WORDS above (that list catches "case/cover/etc. for X", not
# "generic knockoff of X itself"). This checks a different, narrower thing:
# does the AliExpress title actually assert the SAME brand as the maker, or
# does the brand only appear as a "for/compatible with <brand>" target --
# the hallmark of an unbranded item claiming compatibility, not a genuine
# branded product. Deliberately a small, curated list of brands actually
# seen in this branch's scraped categories (tech/tools/home-garden/etc.),
# not an exhaustive trademark database -- extend it as new false-HIGHs turn
# up the same way _ACCESSORY_WORDS/_LIGHT_WORDS were built. A brand not on
# this list simply isn't checked (fails open, same tradeoff as everywhere
# else in this rubric), not treated as automatically safe.
_BRAND_TOKENS = {
    "apple", "iphone", "ipad", "macbook", "airpods", "airpod",
    "samsung", "galaxy", "anker", "sony", "bose", "jbl", "google", "pixel",
    "microsoft", "xbox", "nintendo", "logitech", "garmin", "fitbit",
    "gopro", "nikon", "canon", "dyson", "kitchenaid", "ninja", "keurig",
    "instant", "yeti", "stanley", "weber", "traeger",
    "dewalt", "ryobi", "milwaukee", "makita", "bosch", "craftsman",
    # Added 2026-08-29, same day, second real live example found while
    # testing the discover stage against real scraped tech/computer-
    # accessories items: a real "HP G5 100W PD Docking Station" scored
    # MEDIUM against a $5.60 generic "PD100W Game Docking Station For
    # Switch/Steam Deck" -- shares "docking station"/"100w"-ish vocabulary,
    # no HP branding at all. Laptop/PC brands weren't in the original list
    # (built around the phone/tool/appliance categories that prompted the
    # Apple Pencil fix) despite "computer-accessories" being one of the
    # scraped Deals categories from day one -- a real gap, not a hypothetical.
    "hp", "dell", "lenovo", "asus", "acer", "lg",
    # Added 2026-08-29, same day, third real live example found while
    # analyzing why "profitable" candidates included wrong-product matches:
    # a real "Nespresso by Breville" espresso machine scored MEDIUM against
    # a $5 drip-tray accessory ("...Replacement Drip Tray For Nespresso
    # Essenza MINI..."), and a real "SteelSeries Alias" mic scored MEDIUM
    # against a shock mount for an entirely different mic ("...Shock Mount
    # for Blue Yeti Mic..."). Both are exactly the pattern this check exists
    # for (brand asserted as the actual eBay product, only appears as a
    # compatibility target -- or not at all -- on the Ali side); the brands
    # just weren't in the list yet, built around phone/tool/appliance/laptop
    # examples that happened not to include coffee or audio-peripheral
    # brands until now.
    "nespresso", "breville", "steelseries",
}
# Words that, immediately before a brand token, mean the brand is being
# named as a compatibility TARGET ("for Apple Pencil"), not the item's own
# maker ("Apple Pencil Pro" itself, brand at/near the front with no such
# qualifier before it). Deliberately conservative/fail-closed: "for X" also
# appears on some genuinely branded/OEM-compatible listings (a real "A+ for
# HP USB-C G5 Docking Station" got flagged too, 2026-08-29 testing) -- a
# false rejection there is an accepted tradeoff, same reasoning as
# build_item_aspects()'s "wrong data on a real listing is worse than not
# listing it at all."
_COMPAT_QUALIFIERS = {"for", "fits", "fit", "compatible", "replacement", "universal"}


def _brand_authenticity_mismatch(ebay_title: str, ali_title: str) -> str | None:
    """Returns a reason string if the eBay title asserts a known brand
    (`_BRAND_TOKENS`) as the actual product, but every occurrence of that
    brand in the AliExpress title reads as a compatibility target
    (`_COMPAT_QUALIFIERS` immediately before it) rather than the maker --
    or the brand doesn't appear in the Ali title at all. Returns None
    (no mismatch flagged) if the eBay title asserts no known brand, or if
    at least one occurrence in the Ali title reads as genuine."""
    ebay_brands = set(re.findall(r"[a-z0-9]+", ebay_title.lower())) & _BRAND_TOKENS
    if not ebay_brands:
        return None
    ali_tokens = re.findall(r"[a-z0-9]+", ali_title.lower())
    for brand in ebay_brands:
        idxs = [i for i, w in enumerate(ali_tokens) if w == brand]
        if any(not (i > 0 and ali_tokens[i - 1] in _COMPAT_QUALIFIERS) for i in idxs):
            return None  # at least one occurrence reads as the genuine maker
    return (f"brand authenticity mismatch: eBay item asserts "
            f"{'/'.join(sorted(ebay_brands))} as the actual product, but the "
            f"AliExpress match either doesn't mention it or only as a "
            f"\"for/compatible with\" target -- looks like a generic/"
            f"third-party item claiming compatibility, not a genuine "
            f"{'/'.join(sorted(ebay_brands))} product")


def judge_match(ebay_title: str, ebay_price: float, ali_candidates: list[dict]) -> AliMatch | None:
    """Heuristic stand-in for the LLM-judgment step (Step 4).

    Implements the rubric from branch11-listing-rules.md: keyword overlap
    (via a token Jaccard-style measure) blended with whole-title fuzzy
    similarity (difflib.SequenceMatcher) into a single 0-100 `match_score`,
    then a set of hard product-type-mismatch checks (accessory-vs-product,
    light/torch-vs-product, wired-vs-wireless, voltage/model number,
    construction-material) that cap the score low and force verdict to NONE
    regardless of how well the vocabulary otherwise overlaps -- these catch
    the "AliExpress item is a same-vocabulary but different-kind-of-thing
    accessory" failure mode (see the block comment above _ACCESSORY_WORDS
    for the real examples that prompted this). This is still a rule-based
    approximation for
    prototyping, not a real product classifier -- production should
    eventually replace this with an actual lightweight-model call reasoning
    over the same gathered data, per the hybrid architecture principle (LLM
    judgment, not LLM browsing).
    """
    ebay_words = set(re.findall(r"[a-z0-9]+", ebay_title.lower()))
    ebay_sizes = {m.group(0).lower() for m in _SIZE_RE.finditer(ebay_title)}
    ebay_volts = {m.group(1) for m in _VOLTAGE_RE.finditer(ebay_title)}
    ebay_material_ids, ebay_material_words = _cluster_hits(ebay_words, _MATERIAL_CLUSTERS)

    best: AliMatch | None = None
    best_score = -1.0
    for cand in ali_candidates:
        title = cand.get("title") or ""
        ali_words = set(re.findall(r"[a-z0-9]+", title.lower()))
        overlap = len(ebay_words & ali_words) / max(1, len(ebay_words))
        # Secondary signal only (15% weight) -- word order varies a lot between
        # an eBay listing title and an AliExpress one describing the same real
        # product ("Battery Pack For DeWalt ... DCB205" vs "DeWalt DCB205 ...
        # Battery"), and title *length* varies too (a long eBay title vs. a
        # terse Ali one, or vice versa), both of which make a full-title
        # SequenceMatcher ratio an unreliable primary signal on its own --
        # confirmed empirically: weighting it 50/50 against keyword overlap
        # wrongly sank many real matches (e.g. "Anker PowerConf S3 ... Refurb"
        # vs "Anker PowerConf Speakerphone, Zoom Certified" dropped from a
        # correct HIGH to a wrong NONE). Kept as a minor nudge, and run on
        # alphabetically-sorted token strings so at least reordering itself
        # isn't penalized.
        fuzzy = difflib.SequenceMatcher(
            None, " ".join(sorted(ebay_words)), " ".join(sorted(ali_words))
        ).ratio()

        reasons = [f"keyword overlap {overlap:.0%}"]
        match_score = 100 * (0.85 * overlap + 0.15 * fuzzy)
        hard_mismatch = False

        ali_sizes = {m.group(0).lower() for m in _SIZE_RE.finditer(title)}
        if ebay_sizes and ali_sizes and ebay_sizes != ali_sizes:
            match_score -= 30
            reasons.append(f"size/tier mismatch ({ebay_sizes} vs {ali_sizes})")
        elif ebay_sizes and ebay_sizes == ali_sizes:
            reasons.append("size/tier matches")

        ali_volts = {m.group(1) for m in _VOLTAGE_RE.finditer(title)}
        if ebay_volts and ali_volts and ebay_volts.isdisjoint(ali_volts):
            match_score -= 40
            hard_mismatch = True
            reasons.append(f"voltage/model mismatch (ebay {sorted(ebay_volts)}V vs ali {sorted(ali_volts)}V)")

        if (ali_words & _ACCESSORY_WORDS) and not (ebay_words & _ACCESSORY_WORDS):
            match_score -= 45
            hard_mismatch = True
            hit = sorted(ali_words & _ACCESSORY_WORDS)
            reasons.append(f"accessory-only match (ali item is a {'/'.join(hit)} for the product, not the product itself)")

        if (ali_words & _LIGHT_WORDS) and not (ebay_words & _LIGHT_WORDS):
            match_score -= 45
            hard_mismatch = True
            reasons.append("ali item is a light/torch/lamp accessory, not the eBay product")

        if (ebay_words & _WIRELESS and ali_words & _WIRED) or (ebay_words & _WIRED and ali_words & _WIRELESS):
            match_score -= 35
            hard_mismatch = True
            reasons.append("wired/wireless conflict between ebay and ali titles")

        ali_material_ids, ali_material_words = _cluster_hits(ali_words, _MATERIAL_CLUSTERS)
        if ebay_material_ids and ali_material_ids and ebay_material_ids.isdisjoint(ali_material_ids):
            match_score -= 35
            hard_mismatch = True
            reasons.append(
                f"construction material mismatch (ebay: {'/'.join(sorted(ebay_material_words))} "
                f"vs ali: {'/'.join(sorted(ali_material_words))})"
            )

        brand_mismatch = _brand_authenticity_mismatch(ebay_title, title)
        if brand_mismatch:
            match_score -= 50
            hard_mismatch = True
            reasons.append(brand_mismatch)

        prices = [float(p.replace("$", "")) for p in cand.get("prices", [])]
        cheapest = min(prices) if prices else None
        if cheapest is not None:
            if cheapest >= ebay_price:
                match_score -= 40
                reasons.append(f"price implausible (${cheapest:.2f} >= ebay ${ebay_price:.2f})")
            else:
                reasons.append(f"price plausible (${cheapest:.2f} < ebay ${ebay_price:.2f})")

        match_score = max(0, min(100, round(match_score)))
        if hard_mismatch:
            match_score = min(match_score, 29)  # a hard type-mismatch can never read as HIGH/MEDIUM

        if match_score > best_score:
            best_score = match_score
            if match_score >= 50:
                verdict = "HIGH"
            elif match_score >= 30:
                verdict = "MEDIUM"
            else:
                verdict = "NONE"
            best = AliMatch(
                id=cand["id"], title=title, prices=cand.get("prices", []),
                url=cand["url"], verdict=verdict, reasons=reasons, match_score=match_score,
                imageUrl=cand.get("imageUrl"),
            )
    return best if best and best.verdict != "NONE" else (
        AliMatch(id="", title="", prices=[], url="", verdict="NONE", reasons=["no candidate cleared threshold"], match_score=0)
        if best is None else best
    )


def extract_product_page_details(url: str) -> dict | None:
    """Visit a real AliExpress product page and pull its authoritative price
    and shipping cost in one visit.

    Replaces the noisy multi-value prices scraped off search-result tiles
    (which pick up unrelated dollar amounts on the card -- coupon text,
    bundle add-ons, etc.) with the single real listed price, via the page's
    schema.org JSON-LD `Product` block. Falls back to the visible price
    element (a CSS-module class, less stable across frontend deploys) only
    if JSON-LD isn't present. Shipping comes from `.dynamic-shipping-line`
    ("Free shipping" -> $0, "Shipping: $X.XX" -> X.XX); None if that element
    isn't found, meaning unverified/unknown, not necessarily free.

    Returns None on any failure (page didn't load, item pulled, selector
    missing) -- callers should treat that as "keep the tile-scraped price,
    don't block on this" rather than retrying, per Branch 11's non-exhaustive
    verification policy (see find_best_ali_match). Returns
    {"price": dict|None, "shipping": float|None} otherwise -- "price" being
    None (JSON-LD and DOM fallback both missing) is possible even when the
    page itself loaded fine.
    """
    try:
        browser_navigate(url)
        return browser_evaluate(PRODUCT_PAGE_JS)
    except Exception:
        return None


def compute_profit(ebay_price: float, ali_price: float, fee_pct: float, margin_pct: float,
                    ali_shipping: float = 0.0) -> dict:
    budget = ebay_price - (ebay_price * fee_pct) - (ebay_price * margin_pct)
    remaining = budget - ali_price - ali_shipping
    ebay_fee = ebay_price * fee_pct
    target_margin = ebay_price * margin_pct
    # Actual total profit per unit (not just the excess over the target margin):
    # ebay_price - fee - ali_price - shipping == target_margin + remaining. This
    # is the "profit margin" sort key emails are ordered by -- see main().
    total_profit = ebay_price - ebay_fee - ali_price - ali_shipping
    return {
        "ebay_price": ebay_price, "ebay_fee": round(ebay_fee, 2),
        "target_margin": round(target_margin, 2),
        "budget_for_supply_and_shipping": round(budget, 2),
        "ali_price": ali_price, "ali_shipping_assumed": ali_shipping,
        "remaining_after_supply_and_shipping": round(remaining, 2),
        "total_profit": round(total_profit, 2),
        "passes": remaining >= 0,
    }


def suggested_ebay_price(ali_price: float, fee_pct: float, margin_pct: float,
                          ali_shipping: float = 0.0) -> float:
    """Added 2026-09-02 (PRODUCTION-READINESS.md Item 22, the daily DSers
    price-check job): inverse of compute_profit()'s budget formula -- the
    minimum eBay price that restores the *same* target margin_pct at a given
    supplier cost. Solves `ebay_price * (1 - fee_pct - margin_pct) ==
    ali_price + ali_shipping` for ebay_price. Used to give a real, actionable
    number when a DSers cost increase makes a listing's original eBay price
    no longer clear its own target margin -- Travis still decides whether/how
    to actually change the live listing (Hard Constraints forbid auto-editing
    a real listing's price), this just tells him what number to consider.

    Rounds UP to the next cent (not nearest) -- the exact break-even price is
    a fragile floating-point boundary (confirmed live: standard rounding can
    land a cent short, making compute_profit() report `passes: False` for the
    very price meant to just barely pass) -- rounding up guarantees the
    suggested price actually clears the target margin, not just approximates it."""
    exact = (ali_price + ali_shipping) / (1 - fee_pct - margin_pct)
    return math.ceil(exact * 100) / 100


_LEADING_NOISE = {
    "portable", "metal", "small", "large", "wooden", "new", "genuine", "authentic",
    "original", "official", "premium",
}


def select_shipping_policy(env: str, estimated_shipping_cost: float, credential_mode: str = "bitwarden") -> dict | None:
    """Step 6 (eBay side): pick the cheapest "Drop Ship N" fulfillment policy
    whose flat shipping cost covers the AliExpress-side estimated shipping
    cost. Travis's real policies (confirmed live 2026-08-26; the accidental
    "Drop Ship 10 Copy" duplicate at the $9.99 tier was removed by Travis
    2026-08-31, re-verified live same day -- Drop Ship 10 now resolves
    unambiguously): Drop Ship 5 ($4.99), Drop Ship 10 ($9.99), Drop ship 20
    ($20), Drop ship 30 ($30), Drop Ship 100 ($100). Naming is inconsistent
    in case -- matched case-insensitively.

    `credential_mode`: "bitwarden" (default, interactive) or "local"
    (unattended auto-listing path -- see branch11_ebay_listing.py's _call()).

    Returns the policy dict (with 'fulfillmentPolicyId', 'name', 'shippingCost')
    for the smallest covering policy, or None if nothing covers the estimate
    (i.e. estimated_shipping_cost > the largest policy's cost) -- callers must
    treat None as "flag for manual review", not silently pick the largest one.
    """
    from ecommerce_listing_mgmt.ebay.auth import api_base, refresh_access_token, refresh_access_token_unattended

    tok = (refresh_access_token_unattended(env) if credential_mode == "local" else refresh_access_token(env))["access_token"]
    url = f"{api_base(env)}/sell/account/v1/fulfillment_policy?marketplace_id=EBAY_US"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {tok}"})
    with urllib.request.urlopen(req) as resp:
        policies = json.load(resp).get("fulfillmentPolicies", [])

    candidates = []
    for p in policies:
        if not re.match(r"drop\s*ship", p.get("name", ""), re.IGNORECASE):
            continue
        try:
            cost = float(p["shippingOptions"][0]["shippingServices"][0]["shippingCost"]["value"])
        except (KeyError, IndexError, ValueError):
            continue
        candidates.append((cost, p))

    covering = sorted((c for c in candidates if c[0] >= estimated_shipping_cost), key=lambda c: c[0])
    if not covering:
        return None
    cost, policy = covering[0]
    return {
        "fulfillmentPolicyId": policy["fulfillmentPolicyId"],
        "name": policy["name"],
        "shippingCost": cost,
    }


def slug_query(title: str) -> str:
    """Keyword extraction for an AliExpress search query from an eBay title.

    2026-08-25 finding (real pipeline run): a naive first-4-non-stopword-words
    heuristic picks leading adjectives/brand names ("Portable Metal Small Dog...")
    instead of the actual product noun, producing garbage search results. This
    version drops a small list of common leading descriptors and a likely brand
    name (single capitalized first word not itself a common descriptor), then
    keeps more words (6) to give AliExpress's own search ranking more to work
    with. Still a heuristic, not a real fix -- see TODO below.
    """
    words = re.findall(r"[a-zA-Z]+", title)
    if not words:
        return ""
    stop = {"with", "and", "the", "for", "new", "of", "to", "a", "in", "on"}
    # Drop a likely brand name: first word, capitalized, not a common descriptor.
    if words[0][:1].isupper() and words[0].lower() not in _LEADING_NOISE and len(words) > 1:
        words = words[1:]
    words = [w for w in words if w.lower() not in stop and w.lower() not in _LEADING_NOISE]
    keep = words[:6]
    return "-".join(w.lower() for w in keep)


# TODO(Branch 11): this positional/stopword heuristic is a stand-in for real
# query construction. A better version would identify the core product noun
# specifically (e.g. via a small product-category keyword list, or an LLM call
# reasoning over the title) rather than guessing by word position. Revisit
# once Step 4's judgment call is upgraded from the heuristic in judge_match()
# to a real model call -- the same call could plausibly do both.


def broad_query(title: str, keep: int = 4) -> str:
    """Second, broader search-query variation: strip noise words wherever
    they appear in the title (not just leading, like slug_query) and keep
    fewer words. Used as a fallback variation when slug_query's more
    specific 6-word query returns nothing good -- a shorter query gives
    AliExpress's own search ranking a wider net to match against, at the
    cost of specificity. Deliberately a second simple heuristic, not a
    different algorithm -- see the TODO above."""
    words = re.findall(r"[a-zA-Z]+", title)
    stop = {"with", "and", "the", "for", "new", "of", "to", "a", "in", "on"}
    core = [w for w in words if w.lower() not in stop and w.lower() not in _LEADING_NOISE]
    return "-".join(w.lower() for w in core[:keep])


def build_query_variations(title: str, max_variations: int = 2) -> list[str]:
    """Up to `max_variations` distinct, non-empty AliExpress search queries
    for one eBay title -- slug_query's more specific 6-word query first,
    then broad_query's shorter fallback. Kept to two cheap heuristics
    rather than an open-ended set, per Branch 11's non-exhaustive search
    policy: this is meant to catch the common case where the first query is
    too specific, not to enumerate every phrasing."""
    seen: list[str] = []
    for candidate in (slug_query(title), broad_query(title)):
        if candidate and candidate not in seen:
            seen.append(candidate)
        if len(seen) >= max_variations:
            break
    return seen


def search_aliexpress_candidates(query: str, max_results: int) -> list[dict]:
    browser_navigate(f"https://www.aliexpress.us/w/wholesale-{query}.html")
    tiles = browser_evaluate(ALIEXPRESS_TILE_JS) or []
    return tiles[:max_results]


def find_best_ali_match(ebay_title: str, ebay_price: float, max_candidates: int = 15,
                         max_query_variations: int = 2) -> AliMatch | None:
    """Search AliExpress for the best match to one eBay item, then verify its
    price against the real product page. Non-exhaustive by design -- there
    are many eBay candidates to get through per run, so this stops as soon
    as it has a good-enough answer rather than exploring every query
    variation or verifying every candidate:

    1. Try up to `max_query_variations` search queries (see
       build_query_variations), accumulating unique tile-scraped candidates
       up to `max_candidates` total. Stops trying further variations as soon
       as one query's results already judge to HIGH -- a second, broader
       query is only worth the extra page load when the first one didn't
       land a confident match.
    2. Re-scores the accumulated candidate pool with judge_match() after
       each variation (cheap -- pool stays small).
    3. For a HIGH or MEDIUM verdict (per the match-judgment rubric in
       branch11-listing-rules.md, both are meant to be confirmed via the
       real product page before use), makes exactly one attempt to visit
       that single best candidate's product page and replace its
       search-tile prices with the verified real price. No retry on a
       second-best candidate if that visit fails -- falls back to the
       tile-scraped price and moves on, per the same non-exhaustive policy.
    """
    seen_ids: set[str] = set()
    candidates: list[dict] = []
    best: AliMatch | None = None

    for query in build_query_variations(ebay_title, max_query_variations):
        for tile in search_aliexpress_candidates(query, max_candidates):
            if tile["id"] not in seen_ids:
                seen_ids.add(tile["id"])
                candidates.append(tile)

        best = judge_match(ebay_title, ebay_price, candidates)
        if best and best.verdict == "HIGH":
            break  # good enough -- don't spend another page load on a broader query
        if len(candidates) >= max_candidates:
            break

    if best and best.verdict in ("HIGH", "MEDIUM") and best.url:
        verified = extract_product_page_details(best.url)
        price_info = verified.get("price") if verified else None
        if price_info and price_info.get("price") is not None:
            verified_price = price_info["price"]
            # 2026-08-27: re-run the full rubric against the verified price
            # instead of just overwriting `prices` -- the initial verdict was
            # judged against the noisy tile-scraped price, which can make a
            # match look plausible (or implausible) that the real product-page
            # price contradicts. Confirmed in testing: several items kept a
            # stale HIGH verdict even after the verified price made them fail
            # the profit check outright. Re-judging keeps verdict/match_score
            # honest against the number actually used downstream.
            rejudged = judge_match(ebay_title, ebay_price, [{
                "id": best.id, "title": best.title, "prices": [f"${verified_price:.2f}"],
                "url": best.url, "imageUrl": best.imageUrl,
            }])
            best.verdict = rejudged.verdict
            best.match_score = rejudged.match_score
            best.reasons = rejudged.reasons
            best.prices = [f"${verified_price:.2f}"]
            best.price_verified = True
            best.reasons.append(f"price verified from product page (${verified_price:.2f}, {price_info.get('source')})")
        else:
            best.reasons.append("product-page price verification failed -- using search-result price")

        shipping = verified.get("shipping") if verified else None
        if shipping is not None:
            best.shipping_cost = shipping
            best.reasons.append(f"shipping cost from product page: ${shipping:.2f}")
        else:
            best.reasons.append("shipping cost unknown -- not found on product page, treated as $0 in profit calc")

        description = verified.get("description") if verified else None
        if description:
            best.description = description
            best.reasons.append("description captured from product page (ld+json)")
        else:
            best.reasons.append("description not found on product page -- listing creation will block on this candidate")

        if verified:
            specs = verified.get("specifications") or ""
            dim_lines = verified.get("dimensionLines") or []
            if specs or dim_lines:
                best.dimension_text = (specs + "\n" + "\n".join(dim_lines)).strip()

    return best


def _scroll_to_load_more(count_js: str) -> None:
    """Scrolls the current page to the bottom repeatedly, pausing
    SCROLL_SETTLE_SECONDS after each for lazy-loaded content to render,
    stopping once the tile count stops growing (or after
    MAX_SCROLL_ATTEMPTS). Added 2026-09-02 after live-testing found eBay
    Deals/search pages lazy-load tiles on scroll -- neither
    _scrape_tiles_for_url() nor MAX_TILES_PER_URL had ever accounted for
    this, so both were only ever seeing whatever rendered in the initial
    viewport (confirmed live: 24 tiles before scrolling vs. 72 after, on a
    real Deals page). Cheap no-op on a page that doesn't lazy-load anything
    further (confirmed live: a thin category page just stays flat after
    one scroll, so this returns immediately without wasting the remaining
    attempts)."""
    last_count = -1
    for _ in range(MAX_SCROLL_ATTEMPTS):
        browser_evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
        time.sleep(SCROLL_SETTLE_SECONDS)
        count = browser_evaluate(count_js)
        if not isinstance(count, (int, float)) or count <= last_count:
            break
        last_count = count


def _scrape_tiles_for_url(url: str) -> list[dict]:
    """Returns raw {id, title, priceNum, url} tile dicts for one pool URL.
    Ordinary eBay Deals pages are scraped directly with EBAY_TILE_JS. URLs in
    KEYWORD_SOURCES aren't eBay listings at all (see DEALS_URL_POOL comment)
    -- those get keyword-extracted instead (capped at MAX_KEYWORDS_PER_SOURCE
    keywords), and each keyword becomes its own eBay search whose results are
    what actually flow into the pipeline. Either way the caller gets back the
    same shape of tile, so everything downstream (matching, profit-check,
    auto-listing) is unaware of which path a tile came from.

    Scrolls each page before extracting tiles (see _scroll_to_load_more())
    so MAX_TILES_PER_URL is scraped against the page's real lazy-loaded tile
    count, not just its initial-viewport render."""
    if url in KEYWORD_SOURCES:
        browser_navigate(url)
        keywords = (browser_evaluate(KEYWORD_SOURCES[url]) or [])[:MAX_KEYWORDS_PER_SOURCE]
        tiles: list[dict] = []
        for kw in keywords:
            browser_navigate("https://www.ebay.com/sch/i.html?_nkw=" + quote(kw))
            _scroll_to_load_more(EBAY_SEARCH_TILE_COUNT_JS)
            tiles.extend(browser_evaluate(EBAY_SEARCH_TILE_JS) or [])
        return tiles[:MAX_TILES_PER_URL]
    browser_navigate(url)
    _scroll_to_load_more(EBAY_TILE_COUNT_JS)
    return (browser_evaluate(EBAY_TILE_JS) or [])[:MAX_TILES_PER_URL]


def _scrape_ebay_tiles(urls: list[str], price_ceiling: float, seen_ids: set[str],
                        remaining_limit: int | None) -> list[EbayCandidate]:
    """Just the eBay-tile-scraping half of what `_scrape_and_match()` used to
    do in one piece -- split out 2026-08-29 so the new `--stage discover`
    (DSers-search swap, see main()) can reuse the exact same eBay-discovery
    mechanism without the old path's browser-based AliExpress matching
    bundled in. Mutates `seen_ids` in place, same contract as before."""
    candidates: list[EbayCandidate] = []
    excluded_count = 0
    for url in urls:
        tiles = _scrape_tiles_for_url(url)
        for t in tiles:
            if t["priceNum"] is None or t["priceNum"] >= price_ceiling or t["id"] in seen_ids:
                continue
            if _excluded_reason(t["title"]) is not None:
                seen_ids.add(t["id"])
                excluded_count += 1
                continue
            seen_ids.add(t["id"])
            candidates.append(EbayCandidate(**t))
    if excluded_count:
        print(f"[discover] excluded {excluded_count} item(s) via EXCLUDED_CATEGORIES")
    if remaining_limit is not None:
        candidates = candidates[:remaining_limit]
    return candidates


def _build_pipeline_result(ebay_item: EbayCandidate, match: AliMatch | None,
                            fee_pct: float, margin_pct: float) -> PipelineResult:
    """Shared profit-calc step, given a finished AliExpress match -- split
    out 2026-08-29 so both the old browser-matched path (`_scrape_and_match`)
    and the new DSers-matched path (`main()`'s `--stage finish`) compute
    profit identically from that point on."""
    profit = None
    if match and match.verdict != "NONE" and match.prices:
        cheapest = min(float(p.replace("$", "")) for p in match.prices)
        ali_shipping = match.shipping_cost if match.shipping_cost is not None else 0.0
        profit = compute_profit(ebay_item.priceNum, cheapest, fee_pct, margin_pct, ali_shipping=ali_shipping)
    return PipelineResult(ebay_item=ebay_item, best_ali_match=match, profit_check=profit)


def _scrape_and_match(urls: list[str], price_ceiling: float, fee_pct: float, margin_pct: float,
                       seen_ids: set[str], remaining_limit: int | None,
                       max_candidates: int, max_query_variations: int) -> list[PipelineResult]:
    """Scrapes eBay candidates from `urls` (skipping ids already in `seen_ids`,
    mutated in place -- so a later tier never reprocesses an earlier tier's
    items), then runs AliExpress matching + profit calc on each. Shared by
    run_pipeline()'s core and expansion tiers (see there for why they're
    separate calls, not one combined list). **Old single-pass path only**
    (`main()`'s default `--stage full`) -- the new DSers-search path
    (`--stage discover`/`--stage finish`) doesn't use this, see
    `_scrape_ebay_tiles()`/`build_dsers_ali_match()` instead."""
    candidates = _scrape_ebay_tiles(urls, price_ceiling, seen_ids, remaining_limit)
    return [
        _build_pipeline_result(ebay_item,
                                find_best_ali_match(ebay_item.title, ebay_item.priceNum,
                                                     max_candidates=max_candidates,
                                                     max_query_variations=max_query_variations),
                                fee_pct, margin_pct)
        for ebay_item in candidates
    ]


def _count_good(results: list[PipelineResult]) -> int:
    """'Good' = HIGH-verdict AND passes the profit check -- the same bar
    select_auto_list_candidates() uses for auto-list eligibility (Brand/Type
    aspect resolvability aside, which isn't known until the eBay-API-backed
    category-analysis step later in main()). Used to decide whether the
    expansion Deals pages are worth scraping at all."""
    return sum(
        1 for r in results
        if r.best_ali_match and r.best_ali_match.verdict == "HIGH"
        and r.profit_check and r.profit_check["passes"]
    )


def run_pipeline(price_ceiling: float, fee_pct: float, margin_pct: float, limit: int | None = None,
                  max_candidates: int = 15, max_query_variations: int = 2,
                  min_good_items: int = AUTO_LIST_TOP_N_DEFAULT,
                  pages_per_day: int = ROTATION_PAGES_PER_DAY_DEFAULT) -> list[PipelineResult]:
    """Rotating discovery (2026-08-29, Travis's decision, replaces the
    2026-08-28 fixed CORE/EXPANSION split): scrape today's `pages_per_day`
    pages from DEALS_URL_POOL, selected by `_select_daily_urls()`; only
    scrape the *rest* of the pool too if today's rotation didn't turn up at
    least `min_good_items` good (HIGH-verdict, profit-passing) candidates on
    its own. Keeps the common case (today's rotation already finds enough)
    fast, while still broadening search on days it doesn't -- same
    "expand only if needed" behavior as before, just driven by rotation
    instead of a fixed list. Defaults `min_good_items` to
    AUTO_LIST_TOP_N_DEFAULT since the practical reason to want more
    candidates is filling that day's auto-list quota, but it's a separate
    parameter (not hardcoded equal) in case that reasoning changes later.
    """
    todays_urls = _select_daily_urls(DEALS_URL_POOL, date.today(), pages_per_day)
    remaining_pool = [u for u in DEALS_URL_POOL if u not in todays_urls]

    browser_start()
    results: list[PipelineResult] = []
    seen_ids: set[str] = set()
    try:
        results.extend(_scrape_and_match(
            todays_urls, price_ceiling, fee_pct, margin_pct, seen_ids, limit,
            max_candidates, max_query_variations))

        good_count = _count_good(results)
        if good_count >= min_good_items:
            print(f"[pipeline] {good_count} good candidate(s) from today's {len(todays_urls)} rotated page(s) (>= {min_good_items}) -- skipping the rest of the pool.")
        elif not remaining_pool:
            print(f"[pipeline] Only {good_count} good candidate(s) from today's rotation (need {min_good_items}), but the pool has no other pages to fall back to.")
        else:
            remaining_limit = (limit - len(results)) if limit is not None else None
            if remaining_limit is None or remaining_limit > 0:
                print(f"[pipeline] Only {good_count} good candidate(s) from today's rotation (need {min_good_items}) -- scraping the remaining {len(remaining_pool)} pool page(s) too.")
                results.extend(_scrape_and_match(
                    remaining_pool, price_ceiling, fee_pct, margin_pct, seen_ids, remaining_limit,
                    max_candidates, max_query_variations))
            else:
                print(f"[pipeline] Only {good_count} good candidate(s) from today's rotation, but --limit already reached -- skipping the rest of the pool.")
    finally:
        browser_stop()
    return results


def discover_pipeline(price_ceiling: float, pages_per_day: int, limit: int | None = None) -> list[EbayCandidate]:
    """eBay-only discovery for the new staged flow (2026-08-29, DSers-search
    swap for Steps 2-4 -- see branch11-listing-rules.md's AliExpress-search
    section). Used by `main()`'s `--stage discover`: scrapes today's rotated
    Deals pages exactly like `run_pipeline()` does, but stops after the
    eBay-tile/price-filter step -- no AliExpress matching here at all, since
    that now happens in a separate cron step (the DSers MCP tools this needs
    only exist inside an LLM session, not this bare script).

    **Deliberately does NOT replicate run_pipeline()'s "expand to the rest of
    the pool if not enough good candidates" fallback.** That decision needs
    to know match/profit results, which don't exist yet at this point in the
    new split -- matching happens in a later step entirely. This is a known,
    deliberate simplification (arguably a regression) vs. the old
    single-pass path, which stays available unchanged via `--stage full` if
    this ever needs reverting. Revisit (e.g. by having `main()`'s `--stage
    finish` trigger a second discover+match round when `_count_good()` comes
    up short, rather than deciding it here) only if daily auto-list counts
    actually drop because of this -- don't assume it's fine forever just
    because it's simpler today."""
    todays_urls = _select_daily_urls(DEALS_URL_POOL, date.today(), pages_per_day)
    browser_start()
    try:
        seen_ids: set[str] = set()
        return _scrape_ebay_tiles(todays_urls, price_ceiling, seen_ids, limit)
    finally:
        browser_stop()


def _verify_ali_match(match: AliMatch) -> None:
    """The one browser visit to the AliExpress product page, for description
    (and a shipping-cost fallback) -- split out of build_dsers_ali_match()
    2026-08-30 (Travis's decision, see memory
    incident-2026-08-30-branch11-rate-limit) so `--stage finish` can defer
    this to only the handful of candidates that actually rank for
    auto-listing, instead of paying for a real browser page load per
    HIGH/MEDIUM match -- with today's volume (hundreds/day post the
    50-tile-per-URL cap and 16% margin changes) that had ballooned into a
    long unattended churn through candidates that were never going to be
    published anyway. Mutates `match` in place. No-op if there's no URL to
    visit."""
    if not match.url:
        return
    verified = extract_product_page_details(match.url)
    description = verified.get("description") if verified else None
    if description:
        match.description = description
        match.reasons.append("description captured from product page (ld+json)")
    else:
        match.reasons.append("description not found on product page -- listing creation will block on this candidate")
    if match.shipping_cost is None:
        fallback_shipping = verified.get("shipping") if verified else None
        if fallback_shipping is not None:
            match.shipping_cost = fallback_shipping
            match.reasons.append(f"shipping cost from product page (DSers didn't provide one): ${fallback_shipping:.2f}")
        else:
            match.reasons.append("shipping cost unknown -- not found on DSers or product page, treated as $0 in profit calc")
    if verified:
        specs = verified.get("specifications") or ""
        dim_lines = verified.get("dimensionLines") or []
        if specs or dim_lines:
            match.dimension_text = (specs + "\n" + "\n".join(dim_lines)).strip()


def build_dsers_ali_match(ebay_title: str, ebay_price: float, dsers_items: list[dict], verify: bool = True) -> AliMatch:
    """Stage-B consumer for the new DSers-search flow (2026-08-29) -- the
    direct replacement for `find_best_ali_match()`'s role, but takes
    already-fetched `dsers_find_product` result items instead of doing its
    own browser search. Called from `main()`'s `--stage finish`.

    1. Adapts each raw DSers item ({product_id, title, image, price{min,max},
       currency, rating, orders, shipping_cost, ...}) into `judge_match()`'s
       existing candidate shape and runs the exact same rubric (including
       the 2026-08-29 brand-authenticity check) -- no change to matching
       logic itself, only to where the candidates come from.
    2. Unlike the old path, DSers's price is already a single clean number
       (no tile-price ambiguity) and its `shipping_cost` is already real --
       both are trusted directly, no re-judging against a "verified" price
       needed (that step existed specifically to fix noisy tile prices,
       which don't exist in this data source).
    3. **The one thing DSers has no equivalent for at all: description**
       (confirmed live 2026-08-29 via a real `dsers_product_import`/
       `dsers_product_preview` call, see STATUS.md same date) -- for a
       HIGH/MEDIUM verdict, this used to always make the one browser visit
       `find_best_ali_match()` always made (`_verify_ali_match()`, formerly
       inlined here). **2026-08-30: gated behind `verify` (default True, so
       `--stage full`'s old behavior is unchanged)** -- `--stage finish`
       now passes `verify=False` for its first, cheap judging pass over
       every candidate (verdict + profit don't need description, see point
       2 above) and only calls `_verify_ali_match()` after the fact, on the
       small number of candidates that actually rank for auto-listing. See
       main()'s `--stage finish` handling.
    """
    candidates = [
        {
            "id": str(item.get("product_id") or ""),
            "title": item.get("title") or "",
            "prices": [f"${item['price']['min']:.2f}"] if (item.get("price") or {}).get("min") is not None else [],
            "url": item.get("import_url") or "",
            "imageUrl": item.get("image"),
        }
        for item in dsers_items
    ]
    best = judge_match(ebay_title, ebay_price, candidates)

    matched_raw = next((i for i in dsers_items if str(i.get("product_id")) == best.id), None) if best.id else None
    if matched_raw:
        if matched_raw.get("rating") is not None or matched_raw.get("orders") is not None:
            best.reasons.append(f"DSers data: rating {matched_raw.get('rating')}, {matched_raw.get('orders', 0)} real orders")
        if best.verdict in ("HIGH", "MEDIUM") and matched_raw.get("shipping_cost") is not None:
            best.shipping_cost = float(matched_raw["shipping_cost"])
            best.price_verified = True
            best.reasons.append(f"shipping cost from DSers: ${best.shipping_cost:.2f}")

    if verify and best.verdict in ("HIGH", "MEDIUM") and best.url:
        _verify_ali_match(best)

    return best


def annotate_category_analysis(out: list[dict], credential_mode: str = "local") -> None:
    """Runs analyze_category() (branch11_ebay_listing.py) for every candidate
    that passes the profit check, mutating each row in place with a
    `category_analysis` field (or None if it couldn't be computed) -- Travis's
    2026-08-28 request: surface a category-match quality score in the daily
    email, not just at listing time. Limited to passing candidates only (not
    all ~54 scanned/day) to keep the added eBay API call volume reasonable --
    category fit is only actually relevant for items under real listing
    consideration.

    Runs BEFORE run_auto_listing() (below) so `category_analysis.
    needs_unresolvable_variation` can inform which candidates are even
    eligible for autonomous publish (see select_auto_list_candidates()) --
    this is a real, non-cosmetic use of the data, not just a report field.
    """
    from ecommerce_listing_mgmt.ebay.listing import analyze_category

    for row in out:
        if row["profit_check"] and row["profit_check"]["passes"]:
            match = row["best_ali_match"] or {}
            # 2026-08-29: the actual item that would ship, never the eBay
            # comparable title above (that's only for category suggestion)
            # -- see analyze_category()'s ali_text docstring and
            # branch11-listing-rules.md's "Basic variant handling" section.
            ali_text = f"{match.get('title') or ''} {match.get('description') or ''}".strip()
            row["category_analysis"] = analyze_category(
                "production", row["ebay_item"]["title"], credential_mode=credential_mode, ali_text=ali_text,
                dimension_text=match.get("dimension_text") or "")
        else:
            row["category_analysis"] = None


def load_auto_listed_ledger() -> dict:
    """SKUs already auto-published on production, ever -- see AUTO_LISTED_LEDGER_PATH."""
    if AUTO_LISTED_LEDGER_PATH.exists():
        return json.loads(AUTO_LISTED_LEDGER_PATH.read_text())
    return {}


def select_auto_list_candidates(out: list[dict], top_n: int, already_listed: dict) -> list[dict]:
    """Top `top_n` candidates eligible for autonomous production publish, most
    profitable first. Travis's 2026-08-27 decision: only HIGH-verdict matches
    that pass the profit check ("highly rated matches"), excluding anything
    already in the ledger (already listed in a prior run) and anything
    missing a captured AliExpress description (list_candidate() would just
    reject it -- filter here so it doesn't waste one of the top_n slots).

    Also excludes anything whose `category_analysis.needs_unresolvable_variation`
    is True (Travis's 2026-08-28 decision) -- a required aspect that's a real
    product-variation dimension (Size, Color, etc., per eBay's own
    aspectEnabledForVariations flag) with no generic value available means
    the sourced item is genuinely a multi-variant product (e.g. a mattress
    protector sold in several sizes) and a single-SKU listing pinned to one
    arbitrary value would be a guess, not a fact -- skip rather than list
    something possibly wrong. No eBay Variations API (multi-SKU listings)
    support exists in this project yet; revisit this exclusion if/when it
    does. Requires annotate_category_analysis() to have run first -- a row
    with `category_analysis` still None (analysis wasn't computed, or the
    category-suggestion call itself failed) is treated as ineligible too,
    same fail-closed principle as everywhere else in this module.

    Also excludes anything whose AliExpress source (`best_ali_match["id"]`)
    already backs an active ledger entry -- Travis's 2026-08-29 decision, so
    the same physical product can't end up listed twice under two different
    eBay SKUs (e.g. it resurfaces on a later day's scrape as a different
    Deals-page item, or gets independently re-matched to it). Keyed on the
    AliExpress product id rather than the matched URL, since the URL for the
    same SKU is already known to drift day to day (see STATUS.md
    2026-08-28) -- the id is what's actually stable."""
    already_listed_ali_ids = {
        entry["ali_id"] for entry in already_listed.values() if entry.get("ali_id")
    }
    eligible = [
        row for row in out
        if row["best_ali_match"]
        and row["best_ali_match"]["verdict"] == "HIGH"
        and row["profit_check"] and row["profit_check"]["passes"]
        and row["best_ali_match"].get("description")
        and row["sku"] not in already_listed
        and row["best_ali_match"].get("id") not in already_listed_ali_ids
        and row.get("category_analysis")
        and not row["category_analysis"]["needs_unresolvable_variation"]
    ]
    eligible.sort(key=lambda row: row["profit_check"]["total_profit"], reverse=True)
    return eligible[:top_n]


def select_auto_list_pool(out: list[dict], pool_size: int, already_listed: dict) -> list[dict]:
    """Like `select_auto_list_candidates()`, but for the larger candidate
    pool `run_auto_listing()` walks for shortlist replenishment (added
    2026-08-31, PRODUCTION-READINESS.md Item 36). Deliberately does NOT
    require `best_ali_match.description` or `category_analysis` to already
    be present -- most of this pool hasn't been browser-verified yet, since
    that now happens lazily inside `run_auto_listing()`, only for candidates
    it actually reaches. Same HIGH-verdict/profit-passing/not-already-
    listed/dedup-by-ali_id filter as `select_auto_list_candidates()`,
    profit-sorted, sliced to `pool_size` (`top_n` + the replenishment
    buffer)."""
    already_listed_ali_ids = {
        entry["ali_id"] for entry in already_listed.values() if entry.get("ali_id")
    }
    eligible = [
        row for row in out
        if row["best_ali_match"]
        and row["best_ali_match"]["verdict"] == "HIGH"
        and row["profit_check"] and row["profit_check"]["passes"]
        and row["sku"] not in already_listed
        and row["best_ali_match"].get("id") not in already_listed_ali_ids
    ]
    eligible.sort(key=lambda row: row["profit_check"]["total_profit"], reverse=True)
    return eligible[:pool_size]


def _mirror_to_woocommerce(sku: str, row: dict, ledger_entry: dict) -> None:
    """Creates a matching product in the DSers-bridge WooCommerce store
    (`branch11_woocommerce.py`) for a SKU that was just listed for real on
    eBay -- part 1 of the DSers-mapping automation (2026-08-29, item 1).
    Mutates `ledger_entry` in place: sets `woo_product_id` on success, leaves
    it None (with a `woo_mirror_error` note) on failure.

    **Deliberately fails closed and never raises** -- this runs immediately
    after a real eBay listing has already succeeded; a WooCommerce hiccup
    (store down, credentials stale) must never be allowed to look like the
    eBay listing itself failed, or lose track of `listing_id`/`offer_id`
    that already exist for real. Worst case on failure: the SKU sits with
    `woo_product_id: None` and gets silently retried next run (this function
    is only ever called once per SKU, at listing time, so a permanent
    WooCommerce outage would need a human to notice via the ledger --
    acceptable for now, no retry loop built, this is priced as rare).

    Deliberately does NOT attempt the DSers-side mapping itself
    (`dsers_sku_remap`) -- that needs the DSers MCP tools, which only exist
    inside an LLM session, not this bare cron script. See
    `branch11_daily_run.sh`'s third step for that half."""
    try:
        from ecommerce_listing_mgmt.woocommerce import client as woo
        status, body = woo.create_product(
            name=row["ebay_item"]["title"],
            sku=sku,
            price=str(row["ebay_item"]["priceNum"]),
            description=row["best_ali_match"].get("description") or "",
        )
        if status == 201 and isinstance(body, dict) and body.get("id"):
            ledger_entry["woo_product_id"] = body["id"]
            print(f"[auto-list] {sku}: mirrored to WooCommerce bridge store, product id={body['id']}")
        else:
            ledger_entry["woo_mirror_error"] = f"HTTP {status}: {body}"
            print(f"[auto-list] {sku}: WooCommerce mirror FAILED -- {ledger_entry['woo_mirror_error']}")
    except Exception as e:
        ledger_entry["woo_mirror_error"] = str(e)
        print(f"[auto-list] {sku}: WooCommerce mirror FAILED -- {e}")


def run_auto_listing(out: list[dict], top_n: int,
                      replenish_buffer: int = AUTO_LIST_REPLENISH_BUFFER_DEFAULT) -> None:
    """Autonomously publish the top `top_n` eligible candidates to PRODUCTION,
    mutating each row's `listing_status` in place to reflect the real outcome.

    **Credential access (2026-08-27, Travis's decision): uses the local,
    non-Bitwarden credential path** (`credential_mode="local"` -- see
    `branch11_ebay_auth.LOCAL_CREDS_PATH`/`refresh_access_token_unattended()`),
    since the unattended 8 AM cron run has no live session to `bw unlock`.
    Deliberately narrow and separate from all other credential handling in
    this project -- production eBay only, a dedicated file, never touches
    Bitwarden. Still fails closed if that file is missing/incomplete (skips
    auto-listing for the run, logs why, leaves listing_status as NOT_LISTED)
    rather than erroring the whole pipeline.

    **Replenishes the shortlist on failure (added 2026-08-31, Item 36,
    Travis's explicit request)**: instead of only ever attempting exactly
    `top_n` candidates and leaving a slot unfilled if one turns out
    ineligible, walks a larger profit-sorted pool
    (`select_auto_list_pool()`, sized `top_n + replenish_buffer`) and keeps
    pulling the next-best candidate whenever one fails, stopping once
    `top_n` real listings succeed or the pool is exhausted. Each pool
    candidate is verified (`_verify_ali_match()`'s one AliExpress
    product-page visit) **lazily, only when actually reached** -- not all
    upfront -- so a day where the first `top_n` all succeed costs exactly
    what it always did; the extra verification cost only shows up on days
    with failures, and is bounded by `replenish_buffer`. Supersedes the old
    design where Pass 2 (in `main()`'s `--stage finish`) eagerly
    pre-verified exactly `top_n` candidates before this function ever ran --
    that eager pre-verification step was removed, verification now happens
    here instead."""
    from ecommerce_listing_mgmt.ebay.auth import LOCAL_CREDS_PATH

    if not AUTO_LIST_ENABLED:
        print("[auto-list] AUTO_LIST_ENABLED is False -- skipping auto-listing entirely.")
        return
    if not LOCAL_CREDS_PATH.exists():
        print(f"[auto-list] {LOCAL_CREDS_PATH} does not exist -- skipping auto-listing entirely "
              f"(run the one-time bootstrap step first).")
        return

    from ecommerce_listing_mgmt.ebay.listing import list_candidate, analyze_category  # local import: avoid circular dependency, matches branch11_ebay_listing's own pattern

    ledger = load_auto_listed_ledger()
    pool_size = top_n + replenish_buffer
    pool = select_auto_list_pool(out, pool_size, ledger)
    print(f"[auto-list] {len(pool)} candidate(s) in the replenishment pool "
          f"(top_n={top_n}, replenish_buffer={replenish_buffer}, pool_size={pool_size}).")

    if not pool:
        return

    listed_count = 0

    def _record_successful_listing(sku: str, row: dict, match: dict, result: dict, extra_ledger_fields: dict | None = None) -> None:
        """Shared success path for both the normal auto-list flow and the
        LLM-assist fallback below -- factored out 2026-09-04 after the
        LLM-assist branch's first draft duplicated a stripped-down copy of
        this and silently dropped the WooCommerce mirror + several ledger
        fields (ali_url/ali_price/ali_id/woo_product_id/dsers_mapped) that
        Step 8 (order monitoring) and the DSers-mapping cron both depend
        on. One real implementation, called from both places, so they
        can't drift apart again."""
        nonlocal listed_count
        row["listing_status"] = f"LISTED (listingId={result['listing_id']})"
        ledger[sku] = {
            "listing_id": result["listing_id"],
            "offer_id": result["offer_id"],
            "listed_at": date.today().isoformat(),
            "ebay_price": row["ebay_item"]["priceNum"],
            "total_profit": row["profit_check"]["total_profit"],
            "ebay_title": row["ebay_item"]["title"],
            "ali_url": match.get("url"),
            "ali_price": (min(float(p.replace("$", "")) for p in match["prices"])
                          if match.get("prices") else None),
            "ali_shipping_cost": match.get("shipping_cost"),
            "ali_id": match.get("id"),
            "woo_product_id": None,
            "dsers_mapped": False,
            **(extra_ledger_fields or {}),
        }
        _mirror_to_woocommerce(sku, row, ledger[sku])
        AUTO_LISTED_LEDGER_PATH.write_text(json.dumps(ledger, indent=2))  # persist immediately, not just at the end -- a mid-run crash must not lose track of a real listing that already happened
        listed_count += 1

    browser_start()
    try:
        for row in pool:
            if listed_count >= top_n:
                break
            sku = row["sku"]
            match = row["best_ali_match"]

            if not match.get("description"):
                # Lazy verification -- only for a candidate we actually reached,
                # matching this function's own docstring above.
                match_obj = AliMatch(
                    id=match.get("id", ""), title=match.get("title", ""), prices=match.get("prices", []),
                    url=match.get("url", ""), verdict=match.get("verdict", ""),
                    reasons=list(match.get("reasons", [])), match_score=match.get("match_score", 0),
                    imageUrl=match.get("imageUrl"), price_verified=match.get("price_verified", False),
                    shipping_cost=match.get("shipping_cost"), description=match.get("description"),
                )
                _verify_ali_match(match_obj)
                match["description"] = match_obj.description
                match["shipping_cost"] = match_obj.shipping_cost
                match["reasons"] = match_obj.reasons
                match["dimension_text"] = match_obj.dimension_text

                # Recompute category_analysis now that the real description is
                # available -- the earlier annotate_category_analysis() pass
                # (_finalize_and_publish(), before this candidate was ever
                # verified) only had the Ali match's title to go on for
                # anything beyond the original top_n shortlist.
                ali_text = f"{match.get('title') or ''} {match.get('description') or ''}".strip()
                row["category_analysis"] = analyze_category(
                    "production", row["ebay_item"]["title"], credential_mode="local", ali_text=ali_text,
                    dimension_text=match.get("dimension_text") or "")

            if not match.get("description"):
                row["listing_status"] = ("AUTO_LIST_FAILED: No AliExpress match description available "
                                          "-- cannot build an honest listing description, flag for manual review")
                print(f"[auto-list] {sku}: SKIPPED -- no description after verification")
                continue
            if not row.get("category_analysis") or row["category_analysis"]["needs_unresolvable_variation"]:
                ca = row.get("category_analysis") or {}
                opportunity = check_phase3_variant_opportunity(
                    ca.get("category_id"), match.get("url", ""), ca.get("unresolvable_variation_aspects") or [],
                ) if ca.get("category_id") and match.get("url") else None
                if opportunity:
                    row["phase3_variant_plan"] = opportunity
                    row["listing_status"] = (
                        f"PHASE3_OPPORTUNITY: genuine eBay Variations API multi-variant fix available "
                        f"(aspect={opportunity['aspect']}, {len(opportunity['mapping'])} resolved value(s): "
                        f"{list(opportunity['mapping'].values())}) -- NOT auto-listed, needs Travis's chat "
                        f"approval before publishing (Item 25 Phase 3)"
                    )
                    print(f"[auto-list] {sku}: PHASE 3 OPPORTUNITY found -- {opportunity['aspect']} "
                          f"{list(opportunity['mapping'].values())} -- flagged for chat approval, not listed")
                    continue

                # Last resort (2026-09-04, Travis-directed): a real,
                # HIGH/MEDIUM-verdict, profit-passing candidate that ONLY
                # fails on a variant-aspect decision is worth one node5
                # judgment call before giving up, once Phase 3 has already
                # confirmed there's no genuine multi-value DSers data to
                # build a real variation group from. Autonomous per
                # Travis's explicit decision -- safe specifically because
                # branch11_llm_assist.resolve_aspects_via_llm() requires a
                # verbatim quote from the real source text for every
                # answer, verified in code, not just self-reported
                # confidence (that gap was found and closed via real
                # testing before this went anywhere near production --
                # see that module's docstring).
                verdict = match.get("verdict")
                profit_passes = (row.get("profit_check") or {}).get("passes")
                llm_resolved = None
                if verdict in ("HIGH", "MEDIUM") and profit_passes and ca.get("category_id"):
                    from ecommerce_listing_mgmt.ebay.listing import get_required_aspects
                    from ecommerce_listing_mgmt.llm_assist.llm_assist import resolve_aspects_via_llm
                    unresolved_names = ca.get("unresolvable_variation_aspects") or []
                    required = get_required_aspects("production", ca["category_id"], credential_mode="local")
                    aspects_needed = [{"name": a["name"], "allowed_values": a["values"]}
                                       for a in required if a["name"] in unresolved_names]
                    if aspects_needed:
                        llm_resolved = resolve_aspects_via_llm(
                            sku, row["ebay_item"]["title"], ca.get("category_name", ""),
                            aspects_needed, match.get("title", ""), match.get("description", ""),
                            match.get("dimension_text") or "",
                        )

                if llm_resolved:
                    # list_candidate() auto-adds Brand: Unbranded to any
                    # manual_aspects unless Brand is explicitly overridden
                    # (see its own docstring) -- no need to add it here.
                    shipping_cost = match.get("shipping_cost")
                    result = list_candidate("production", row, aliexpress_shipping_cost=shipping_cost or 0.0,
                                             credential_mode="local", manual_aspects=llm_resolved)
                    if result.get("success"):
                        _record_successful_listing(sku, row, match, result,
                                                    extra_ledger_fields={"llm_assisted": True, "llm_resolved_aspects": llm_resolved})
                        row["listing_status"] += f" -- LLM-ASSISTED {llm_resolved}"
                        print(f"[auto-list] {sku}: LISTED via LLM-assist -- node5 resolved {llm_resolved}")
                        continue
                    else:
                        row["listing_status"] = (f"AUTO_LIST_FAILED: LLM-assist resolved {llm_resolved} but "
                                                  f"the real listing attempt still failed -- {result.get('error')}")
                        print(f"[auto-list] {sku}: LLM-assist resolved aspects but listing still failed: {result.get('error')}")
                        continue

                row["listing_status"] = ("AUTO_LIST_FAILED: needs eBay Variations API support "
                                          "(genuine multi-variant product) -- not yet built, flag for manual review")
                print(f"[auto-list] {sku}: SKIPPED -- needs unresolvable variation support")
                continue

            shipping_cost = match.get("shipping_cost")
            result = list_candidate("production", row, aliexpress_shipping_cost=shipping_cost or 0.0,
                                     credential_mode="local")
            if result.get("success"):
                _record_successful_listing(sku, row, match, result)
                print(f"[auto-list] {sku}: LISTED, listingId={result['listing_id']}")
            else:
                row["listing_status"] = f"AUTO_LIST_FAILED: {result.get('error', 'unknown error')}"
                print(f"[auto-list] {sku}: FAILED -- {result.get('error')}")
    finally:
        browser_stop()

    print(f"[auto-list] done: {listed_count}/{top_n} listed from a pool of {len(pool)}.")


def _finalize_and_publish(results: list[PipelineResult], no_auto_list: bool, auto_list_top_n: int,
                           auto_list_replenish_buffer: int = AUTO_LIST_REPLENISH_BUFFER_DEFAULT) -> None:
    """Shared tail, split out 2026-08-29 so both `--stage full` (old
    single-pass path) and `--stage finish` (new DSers-search path) produce
    byte-identical output shape from a `list[PipelineResult]` onward: shapes
    into the output-JSON row format, sorts, runs category analysis +
    auto-listing (which also does the WooCommerce mirror, see
    `_mirror_to_woocommerce()`), and writes the dated/latest JSON files."""
    out = [
        {
            # Deterministic, stable per eBay item -- Travis's 2026-08-26 request:
            # this is what he references in chat to approve a specific item for
            # listing ("list branch11-<id>"), so it must not change between runs
            # for the same underlying eBay item.
            "sku": f"branch11-{r.ebay_item.id}",
            "ebay_item": asdict(r.ebay_item),
            "best_ali_match": asdict(r.best_ali_match) if r.best_ali_match else None,
            "profit_check": r.profit_check,
            # Default -- run_auto_listing() (below, 2026-08-27) mutates this in
            # place to "LISTED (listingId=...)" or "AUTO_LIST_FAILED: ..." for
            # any candidate it actually attempts. Travis's 2026-08-26 request:
            # report this field in every daily email regardless of value.
            "listing_status": "NOT_LISTED",
        }
        for r in results
    ]

    # Sort so passing items come first, then grouped by match verdict (HIGH
    # before MEDIUM before NONE, per Travis's 2026-08-26 request), and within
    # each verdict group ordered by total_profit descending (most profitable
    # first) -- deterministic, done here rather than left to the
    # email-composition step's own interpretation.
    VERDICT_RANK = {"HIGH": 0, "MEDIUM": 1, "NONE": 2}

    def sort_key(row: dict) -> tuple:
        passes = bool(row["profit_check"] and row["profit_check"]["passes"])
        verdict = row["best_ali_match"]["verdict"] if row["best_ali_match"] else "NONE"
        profit = row["profit_check"]["total_profit"] if row["profit_check"] else float("-inf")
        return (0 if passes else 1, VERDICT_RANK.get(verdict, 2), -profit)

    out.sort(key=sort_key)

    annotate_category_analysis(out, credential_mode="local")

    if not no_auto_list:
        run_auto_listing(out, auto_list_top_n, auto_list_replenish_buffer)

    dated_path = OUT_DIR / f"branch11_candidates_{date.today().isoformat()}.json"
    latest_path = OUT_DIR / "branch11_candidates_latest.json"
    dated_path.write_text(json.dumps(out, indent=2))
    latest_path.write_text(json.dumps(out, indent=2))
    print(f"Wrote {len(out)} results to {dated_path} (and {latest_path})")
    for row in out:
        verdict = row["best_ali_match"]["verdict"] if row["best_ali_match"] else "NONE"
        passes = row["profit_check"]["passes"] if row["profit_check"] else None
        print(f"  {row['ebay_item']['title'][:60]!r} (${row['ebay_item']['priceNum']}) -> {verdict}, passes={passes}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", choices=["full", "discover", "finish"], default="full",
                     help="2026-08-29 DSers-search swap for Steps 2-4, see branch11-listing-rules.md. "
                          "'full' (default): old single-pass eBay-scrape + browser-based AliExpress matching, unchanged. "
                          "'discover': eBay-only scrape, writes branch11_ebay_only_<date>.json, no matching/listing yet -- "
                          "the DSers-search cron step (needs the DSers MCP, which this bare script can't call) runs next. "
                          "'finish': reads --dsers-search-file (that step's output), matches via build_dsers_ali_match() "
                          "+ profit-calcs + auto-lists, same as 'full' from that point on.")
    ap.add_argument("--dsers-search-file", type=str, default=None,
                     help="Required for --stage finish: path to the JSON file the DSers-search cron step writes "
                          "(object keyed by sku, each {ebay_item: {...}, dsers_items: [...]})")
    ap.add_argument("--price-ceiling", type=float, default=100.0)
    ap.add_argument("--fee-pct", type=float, default=0.17)
    ap.add_argument("--margin-pct", type=float, default=0.16)  # 0.15 -> 0.18 (2026-08-27, extra safety buffer for unattended auto-listing) -> 0.16 (2026-08-30, Travis's decision: 18% was screening out otherwise-good candidates, 16% keeps most of that buffer while loosening the bar a bit; if too few good items keep surfacing per URL (see MAX_TILES_PER_URL), criteria adjustment -- not deeper scraping -- is the intended lever)
    ap.add_argument("--limit", type=int, default=None, help="Cap eBay candidates processed (only applies to --stage full/discover; AliExpress search is the slow step on the old path)")
    ap.add_argument("--max-candidates", type=int, default=15, help="--stage full only: max unique AliExpress candidates to consider per eBay item, across all query variations")
    ap.add_argument("--max-query-variations", type=int, default=2, help="--stage full only: max AliExpress search-query phrasings to try per eBay item before settling for the best match found so far")
    ap.add_argument("--auto-list-top-n", type=int, default=AUTO_LIST_TOP_N_DEFAULT, help="Autonomously publish this many of the most profitable HIGH-verdict passing candidates to PRODUCTION (Travis's 2026-08-27 decision)")
    ap.add_argument("--auto-list-replenish-buffer", type=int, default=AUTO_LIST_REPLENISH_BUFFER_DEFAULT, help="Extra candidates beyond --auto-list-top-n to verify+attempt if earlier ones fail, instead of leaving the slot unfilled (Travis's 2026-08-31 decision, PRODUCTION-READINESS.md Item 36)")
    ap.add_argument("--no-auto-list", action="store_true", help="Disable autonomous publishing for this run only, regardless of AUTO_LIST_ENABLED")
    ap.add_argument("--min-good-items", type=int, default=AUTO_LIST_TOP_N_DEFAULT, help="--stage full only: only scrape the rest of DEALS_URL_POOL if today's rotated pages didn't find at least this many HIGH-verdict, profit-passing candidates (Travis's 2026-08-28 decision)")
    ap.add_argument("--pages-per-day", type=int, default=ROTATION_PAGES_PER_DAY_DEFAULT, help="How many pages to scrape from DEALS_URL_POOL's daily rotation before falling back to the rest of the pool (Travis's 2026-08-29 decision)")
    args = ap.parse_args()

    if args.stage == "discover":
        candidates = discover_pipeline(args.price_ceiling, args.pages_per_day, args.limit)
        out_path = OUT_DIR / f"branch11_ebay_only_{date.today().isoformat()}.json"
        out_path.write_text(json.dumps([asdict(c) for c in candidates], indent=2))
        print(f"[discover] Wrote {len(candidates)} eBay candidate(s) to {out_path}")
        return

    if args.stage == "finish":
        if not args.dsers_search_file:
            raise SystemExit("--stage finish requires --dsers-search-file")
        search_data = json.loads(Path(args.dsers_search_file).read_text())

        # Pass 1 (2026-08-30, Travis's decision): judge + profit-calc every
        # candidate with verify=False -- no browser needed at all here, see
        # build_dsers_ali_match()'s docstring for why verdict/profit don't
        # depend on the verification visit. Cheap regardless of volume.
        results: list[PipelineResult] = []
        for entry in search_data.values():
            ebay_item = EbayCandidate(**entry["ebay_item"])
            match = build_dsers_ali_match(ebay_item.title, ebay_item.priceNum, entry.get("dsers_items", []), verify=False)
            results.append(_build_pipeline_result(ebay_item, match, args.fee_pct, args.margin_pct))

        # Pass 2 (eager top-N-only verification) removed 2026-08-31,
        # PRODUCTION-READINESS.md Item 36: verification now happens lazily
        # inside run_auto_listing() itself, only for candidates the
        # replenishment pool actually reaches -- see that function's
        # docstring. _finalize_and_publish() (below) handles the rest.
        _finalize_and_publish(results, args.no_auto_list, args.auto_list_top_n, args.auto_list_replenish_buffer)
        return

    # --stage full (default) -- old single-pass path, unchanged
    results = run_pipeline(args.price_ceiling, args.fee_pct, args.margin_pct, limit=args.limit,
                            max_candidates=args.max_candidates, max_query_variations=args.max_query_variations,
                            min_good_items=args.min_good_items, pages_per_day=args.pages_per_day)
    _finalize_and_publish(results, args.no_auto_list, args.auto_list_top_n, args.auto_list_replenish_buffer)


if __name__ == "__main__":
    main()
