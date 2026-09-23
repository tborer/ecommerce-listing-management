#!/usr/bin/env python3
"""Branch 11 last-resort aspect resolution via node5's local LLM
(llama3.1:8b, http://100.97.38.121:11434, zero token cost -- see
PRODUCTION-READINESS.md's node5-assist item for the full design).

**Why this exists**: every deterministic tier in `_resolve_aspect_value()`
(branch11_ebay_listing.py) already tried and failed for these candidates --
they're real, HIGH/MEDIUM-verdict, profit-passing items that only fail
because a required item aspect (often but not always variation-enabled --
Color/Size/etc.) has no generic eBay value and no exact/scattered-word
textual evidence. Travis's explicit decision (2026-09-04, via chat):
resolving these is a genuine judgment call an LLM is well-suited for, and
autonomous (not chat-gated) is acceptable here specifically because the
items are already high-confidence matches -- the ONLY missing piece is
this one decision.

**The one non-negotiable safety rail, carried over unchanged from every
other tier in this pipeline**: node5 may ONLY pick from eBay's real,
already-offered values for the aspect -- never free text. This is enforced
in code (`_validate_resolution`), not just requested in the prompt --
a response naming anything else is rejected, not coerced. Combined with a
required "high confidence" self-report per aspect (anything else is
treated as unresolved), this keeps the same "never guess" bar the rest of
the pipeline already holds itself to, just with a different resolution
mechanism behind it.

Reuses `list_candidate()`'s existing `manual_aspects` parameter (built for
exactly this shape of problem, previously only ever filled in by Travis
himself) -- no new eBay API surface, no new listing pathway. Every call is
logged to `branch11_llm_assist_log.json` for after-the-fact audit, since
autonomous LLM-influenced listings are a new trust surface even though the
mechanism itself is fully deterministic-gated.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from pathlib import Path

OLLAMA_BASE_URL = "http://100.97.38.121:11434"
# node5's two installed models per its own real config: llama3.1:8b is the
# everyday/execution model, mistral-nemo:12b ("research" alias) is the
# reasoning model for harder judgment calls -- this task is squarely the
# latter (Travis's framing, 2026-09-04), so that's the default. Kept as a
# module constant (not hardcoded inline) so an A/B comparison against
# llama3.1:8b is a one-line change, which is exactly how the two real test
# cases below were run against both.
OLLAMA_MODEL = "mistral-nemo:12b"
OLLAMA_TIMEOUT_S = 90  # generous -- node5 is a personal home box, not a guaranteed-available service

LOG_PATH = Path(__file__).parent / "branch11_llm_assist_log.json"

# 2026-09-04: real testing (both against llama3.1:8b) found the model
# self-reporting "confidence: high" while citing evidence that plainly
# doesn't exist in the source text -- one case fabricated a claim that
# dimension_text "explicitly lists 31cm" when dimension_text had no
# measurements at all; another inferred Color="Silver" purely from the
# word "Metal" in the title, no color ever stated anywhere. Self-reported
# confidence alone is NOT a trustworthy gate. Fix: require a verbatim
# quote from the actual source text for every "high" answer, and verify
# in code (_quote_appears_in_source()) that the quote is a REAL substring
# of the real source text -- not just requested in the prompt, enforced
# after the fact, same "don't trust, verify" pattern as every other
# evidence tier in this pipeline (_value_present_in_text() etc.).
_PROMPT_TEMPLATE = """You are helping resolve missing product-listing details for a real eBay resale item, sourced from a specific AliExpress supplier listing. Accuracy matters: a wrong answer creates a real, live, customer-facing listing.

SOURCE TEXT -- this is the ONLY evidence that exists. Nothing outside the text between the markers below is real or knowable.
=== SOURCE TEXT START ===
eBay listing title: {ebay_title}
eBay category: {category_name}
AliExpress source title: {ali_title}
AliExpress source description: {ali_description}
AliExpress specifications/dimensions text:
{dimension_text}
=== SOURCE TEXT END ===

For each item specific below, decide whether the SOURCE TEXT above contains a real, explicit statement identifying the correct value from its allowed_values list.

CRITICAL RULES -- read carefully, these are checked automatically, not just requested:
1. You may use ONLY facts stated word-for-word in the SOURCE TEXT above. No outside knowledge, no assumptions, no associations. Example of what NOT to do: the word "Metal" appearing in a title does NOT mean the color is "Silver" -- that is a guess, not a fact from the source text, even though it sounds plausible.
2. The allowed_values list is NOT evidence. It is only the set of choices eBay permits -- a value being in that list says nothing about whether it's correct for THIS item.
3. For every aspect you answer with confidence "high", you MUST include, in the "quote" field, an exact word-for-word substring copied from the SOURCE TEXT above that states or directly implies the value. If you cannot find a real quote like this, you MUST set confidence to "low" and leave "quote" empty -- do not fabricate a quote, and do not paraphrase the source text into something that looks like a quote but isn't verbatim.
4. It is correct and expected for most aspects to end up "low" confidence. A wrong "high" answer is far worse than an honest "low."

ASPECTS TO RESOLVE:
{aspects_json}

Respond with ONLY a JSON object (no other text, no markdown fences), in exactly this shape:
{{"resolutions": [{{"aspect": "<aspect name>", "value": "<verbatim from allowed_values>", "confidence": "high"|"low", "quote": "<exact word-for-word substring from SOURCE TEXT, or empty string if confidence is low>", "reasoning": "<one short sentence>"}}, ...]}}
"""


def _call_ollama(prompt: str, model: str = OLLAMA_MODEL) -> str | None:
    body = {
        "model": model,
        "prompt": prompt,
        "format": "json",
        "stream": False,
        "options": {"temperature": 0},
    }
    req = urllib.request.Request(
        f"{OLLAMA_BASE_URL}/api/generate",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=OLLAMA_TIMEOUT_S) as resp:
            return json.loads(resp.read()).get("response")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        print(f"[llm-assist] node5 unreachable/error: {exc}")
        return None


def _normalize(text: str) -> str:
    return " ".join(text.lower().split())


def _quote_appears_in_source(quote: str, source_text: str) -> bool:
    """Whitespace/case-insensitive substring check -- the real enforcement
    behind the prompt's quote requirement. A quote that isn't genuinely in
    the source text means the model fabricated its evidence (confirmed
    live, 2026-09-04 -- see module docstring), regardless of what
    confidence it claimed."""
    if not quote or not quote.strip():
        return False
    return _normalize(quote) in _normalize(source_text)


def _validate_resolution(aspects_needed: list[dict], raw_response: str | None, source_text: str) -> dict[str, str] | None:
    """Returns {aspect_name: value} only if EVERY requested aspect got a
    high-confidence answer that's (a) verbatim one of its own real allowed
    values AND (b) backed by a quote that's a real substring of the actual
    source text -- any single failure (unparseable response, low
    confidence, a value not in the real list, a fabricated/missing quote)
    fails the WHOLE resolution, same "never partial-guess" contract as
    build_item_aspects(). The quote check is the real safety rail here --
    self-reported confidence alone was confirmed live to be untrustworthy.
    """
    if not raw_response:
        return None
    try:
        parsed = json.loads(raw_response)
        resolutions = {r["aspect"]: r for r in parsed["resolutions"]}
    except (json.JSONDecodeError, KeyError, TypeError):
        print(f"[llm-assist] unparseable response, treating as unresolved: {raw_response!r}")
        return None

    allowed_by_aspect = {a["name"]: set(a["allowed_values"]) for a in aspects_needed}
    result: dict[str, str] = {}
    for name, allowed in allowed_by_aspect.items():
        r = resolutions.get(name)
        if not r or r.get("confidence") != "high":
            print(f"[llm-assist]   {name}: no high-confidence answer -- unresolved")
            return None
        value = r.get("value")
        if value not in allowed:
            print(f"[llm-assist]   {name}: picked {value!r}, not a real allowed value -- rejected, unresolved")
            return None
        quote = r.get("quote", "")
        if not _quote_appears_in_source(quote, source_text):
            print(f"[llm-assist]   {name}: quote {quote!r} not actually found in source text -- "
                  f"fabricated evidence, rejected regardless of stated confidence")
            return None
        result[name] = value
        print(f"[llm-assist]   {name}: {value!r} (quote: {quote!r})")
    return result


def _log(sku: str, aspects_needed: list[dict], raw_response: str | None, result: dict[str, str] | None) -> None:
    entries = []
    if LOG_PATH.exists():
        try:
            entries = json.loads(LOG_PATH.read_text())
        except json.JSONDecodeError:
            entries = []
    entries.append({
        "sku": sku,
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "aspects_needed": aspects_needed,
        "raw_response": raw_response,
        "result": result,
    })
    LOG_PATH.write_text(json.dumps(entries, indent=2))


def resolve_aspects_via_llm(sku: str, ebay_title: str, category_name: str,
                             aspects_needed: list[dict], ali_title: str, ali_description: str,
                             dimension_text: str = "", model: str = OLLAMA_MODEL) -> dict[str, str] | None:
    """`aspects_needed`: list of {"name": str, "allowed_values": list[str]}
    -- the required aspects still unresolved after every deterministic
    tier already failed. Returns {aspect_name: chosen_value} only if EVERY
    aspect resolved with high confidence to a real allowed value backed by
    a quote that's genuinely in the source text, else None (caller falls
    back to its existing AUTO_LIST_FAILED behavior -- this function never
    partially resolves). `model` defaults to OLLAMA_MODEL but is
    overridable for A/B comparison against node5's other installed model."""
    dimension_text = dimension_text or "(none captured)"
    source_text = "\n".join([ebay_title, category_name, ali_title, ali_description, dimension_text])
    prompt = _PROMPT_TEMPLATE.format(
        ebay_title=ebay_title, category_name=category_name,
        ali_title=ali_title, ali_description=ali_description,
        dimension_text=dimension_text,
        aspects_json=json.dumps(aspects_needed, indent=2),
    )
    print(f"[llm-assist] {sku}: asking node5 ({model}) to resolve {[a['name'] for a in aspects_needed]}")
    raw_response = _call_ollama(prompt, model=model)
    result = _validate_resolution(aspects_needed, raw_response, source_text)
    _log(sku, aspects_needed, raw_response, result)
    return result
