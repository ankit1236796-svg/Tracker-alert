import asyncio
import json
import logging
import os
import random
import re
from urllib.parse import urlencode

import httpx
from bs4 import BeautifulSoup

from .common import fetch_page

logger = logging.getLogger(__name__)

NEEDS_JS = False

# Apple uses "Add to Bag" (not "Add to Cart")
_ADD_PATTERNS = ["add to bag", "add to cart", "buy"]
_OOS_PATTERNS = [
    "out of stock", "sold out", "currently unavailable",
    "notify me when available", "coming soon",
]

# Class tokens that mark a button/anchor as DISABLED via CSS alone, with no
# `disabled`/`aria-disabled` HTML attribute present — the "Croma lesson" (see
# checkers/croma.py's history). Apple's storefront greys out "Add to Bag" via
# class styling in some states, so without this a disabled button could be
# read as active → false in-stock. JSON-LD is checked first and is usually
# authoritative on apple.com/in, so this button scan is only a fallback, but
# it should still respect a class-styled disabled state.
_DISABLED_CLASS_MARKERS = ("disable", "inactive")


def _is_disabled(el) -> bool:
    """Return True if a BS4 element is visually/semantically disabled — via
    the `disabled` attribute, `aria-disabled="true"`, or a _DISABLED_CLASS_MARKERS
    substring in its class list."""
    if el.get("disabled") is not None:
        return True
    if el.get("aria-disabled", "").lower() == "true":
        return True
    classes = " ".join(el.get("class", [])).lower()
    return any(marker in classes for marker in _DISABLED_CLASS_MARKERS)


def check(soup: BeautifulSoup, html: str) -> bool:
    html_lower = html.lower()

    # ── JSON-LD (most reliable on apple.com/in) ───────────────────────────────
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            for item in (data if isinstance(data, list) else [data]):
                if not isinstance(item, dict):
                    continue
                avail = item.get("offers", {}).get("availability", "")
                if "InStock" in avail:
                    logger.info("[apple] JSON-LD: InStock → True")
                    return True
                if "OutOfStock" in avail or "Discontinued" in avail:
                    logger.info("[apple] JSON-LD: OutOfStock/Discontinued → False")
                    return False
        except Exception:
            pass

    # ── Negative signals (checked BEFORE buttons — a disabled Add-to-Bag
    # button's surrounding page usually carries an unambiguous OOS text signal
    # too, and this order is the safer default even where it doesn't) ─────────
    for pattern in _OOS_PATTERNS:
        if pattern in html_lower:
            logger.info(f"[apple] OOS signal: '{pattern}' → False")
            return False

    # ── Buttons — skip disabled (attr, aria, OR class-styled) ─────────────────
    for btn in soup.find_all("button"):
        if _is_disabled(btn):
            continue
        text = btn.get_text(strip=True).lower()
        if any(p in text for p in _ADD_PATTERNS):
            logger.info(f"[apple] active button '{text[:40]}' → True")
            return True

    # ── Attrs — skip disabled — name: "buy", "add to bag" ─────────────────────
    for attr in ("data-testid", "aria-label", "id"):
        for el in soup.find_all(attrs={attr: True}):
            if _is_disabled(el):
                continue
            val = (el.get(attr) or "").lower()
            if "add-to-bag" in val or "addtobag" in val or any(p in val for p in _ADD_PATTERNS):
                logger.info(f"[apple] active attr {attr}='{val[:40]}' → True")
                return True

    logger.info("[apple] no conclusive signal → defaulting OUT OF STOCK (False)")
    return False


# ═══════════════════════════════════════════════════════════════════════════
# Pincode-specific availability via Apple's PUBLIC fulfillment-messages API
#
# https://www.apple.com/in/shop/fulfillment-messages?parts.0=<SKU>&location=<pincode>
# This is the exact endpoint Apple's own storefront JS calls to render nearby-
# store pickup availability — confirmed via multiple independent, currently-
# working third-party implementations (some India-specific), called with no
# API key, no login, and no scraped/reverse-engineered internal app credential
# (unlike the Blinkit/Zepto/JioMart cases, which were rejected for that reason).
#
# Design note — why an "unavailable" pickup signal never asserts OOS on its own:
# Apple has very few physical retail stores in India (a handful of metro
# cities), while its courier delivery network covers far more pincodes. So
# "no store shows pickup availability near this pincode" is the COMMON case
# for most Indian pincodes and does NOT reliably mean the product can't be
# bought/delivered there — it just means pickup data isn't informative here.
# Treating that as OOS would create systematic false negatives for most users.
# The pincode-specific lookup is therefore used ONLY to CONFIRM in-stock when
# it can (a genuine, pincode-specific positive signal); any inconclusive or
# negative pickup result falls back to the existing generic page-based check()
# — so accuracy is never worse than before, only better when it can confirm.
# ═══════════════════════════════════════════════════════════════════════════

_FULFILLMENT_URL = "https://www.apple.com/in/shop/fulfillment-messages"
# Timeout for the direct httpx call in _fetch_pickup_availability (see
# _cookie_auth_fetch) — kept generous (30s vs. the official-store
# checker's 20s) since this endpoint has shown slow-but-successful
# responses in the past; a slow response is still strictly better than a
# timeout. Only affects this one call; every other checker's fetch_page
# timeout is untouched.
_FULFILLMENT_TIMEOUT = 30.0

# Apple part numbers ("SKUs") are alphanumeric, always ending in a 2-letter
# country code + "/A" (e.g. "MG6M4HN/A" for India). Matched generically rather
# than hardcoding "HN" since not every listed product's SKU is guaranteed to
# follow that exact regional suffix.
_SKU_INLINE_PATTERN = re.compile(r'"partNumber"\s*:\s*"([A-Z0-9]{5,14}/A)"')
_SKU_JSONLD_INLINE_PATTERN = re.compile(r'"sku"\s*:\s*"([A-Z0-9]{5,14}/A)"')


def _sku_from_offers(offers) -> str | None:
    """
    JSON-LD's "offers" field is usually a single Offer object (a dict), but
    some product pages embed it as a LIST of Offer objects instead (e.g. one
    entry per variant/color) — indexing it with `.get("sku")` directly
    crashed with "'list' object has no attribute 'get'" for those pages
    (the root cause fixed here, not just caught at a caller). Handles both
    shapes; returns the first sku found across list entries, or None if
    `offers` is neither shape or yields no sku.
    """
    if isinstance(offers, dict):
        sku = offers.get("sku")
        return str(sku) if sku else None
    if isinstance(offers, list):
        for entry in offers:
            if isinstance(entry, dict):
                sku = entry.get("sku")
                if sku:
                    return str(sku)
        return None
    return None


def _extract_sku(soup: BeautifulSoup, html: str) -> str | None:
    """
    Extract Apple's public SKU/part number from the already-fetched product
    page — no extra request needed. It's visible in both the JSON-LD block
    and the page's inline JS config. Tried in order; logs which method (if
    any) succeeded so a page-structure change is visible in Railway logs.
    """
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except Exception:
            continue
        for item in (data if isinstance(data, list) else [data]):
            if not isinstance(item, dict):
                continue
            sku = item.get("sku") or _sku_from_offers(item.get("offers"))
            if sku:
                logger.info(f"[apple][resolve] SKU from JSON-LD: {sku!r}")
                return str(sku)

    m = _SKU_INLINE_PATTERN.search(html)
    if m:
        logger.info(f"[apple][resolve] SKU from inline partNumber: {m.group(1)!r}")
        return m.group(1)

    m = _SKU_JSONLD_INLINE_PATTERN.search(html)
    if m:
        logger.info(f"[apple][resolve] SKU from inline sku field: {m.group(1)!r}")
        return m.group(1)

    logger.warning("[apple][resolve] could not extract SKU/part number from product page")
    return None


def _build_fulfillment_target(sku: str, pincode: str) -> str:
    # Param set verified 2026-07-27 against a live Chrome DevTools capture
    # of a real, working fulfillment-messages request (see the "PARAM SET
    # HISTORY" note above _cookie_auth_fetch below) — `little`, `mts.1`,
    # and `fts` are gone from Apple's current frontend request entirely,
    # and a new `pl=true` param appeared in their place. The OLD param set
    # below (little/mts.1/fts, no pl) consistently 541'd ("Page Not
    # Found") even with a valid, freshly-refreshed session and a real,
    # currently-listed SKU — this is what actually fixed it, not a
    # cookie/header problem.
    params = {
        "fae": "true",
        "pl": "true",
        "mts.0": "regular",
        "parts.0": sku,
        "location": pincode,
    }
    return f"{_FULFILLMENT_URL}?{urlencode(params)}"


# ── Direct cookie-based fetch (shared by every fulfillment-messages caller
# below AND by the official-store checker further down this file) ─────────
#
# Formerly a navigate-to-the-product-page-then-run-an-in-page-fetch()-via-
# Scrape.do's-"Execute"/Zyte's-"evaluate"-browser-action workaround (built
# after a COLD standalone GET was found to consistently ReadTimeout via
# Zyte). That workaround was itself found broken in production — real
# Railway logs showed "marker not found in returned HTML — Execute/
# evaluate action did not run" on every attempt, both the plain-session
# and super_proxy tiers, every cycle. Root-caused (not just worked around
# again) to the in-page script's own JS: it began with a bare top-level
# `return`, which is a SyntaxError when a provider's evaluate/Execute
# action evaluates the supplied source as a plain expression rather than a
# function body (the documented pattern for both providers avoids `return`
# entirely) — so the script likely never ran at all, on either tier, every
# time, which is exactly what a hard syntax error (not a proxy/timing
# issue) would produce.
#
# Rather than keep fighting the proxy layer, this now mirrors the
# reference implementation directly: a real browser User-Agent + a real
# logged-in Cookie header, loaded from Railway env vars (APPLE_USER_AGENT,
# APPLE_COOKIES) rather than captured/managed by this bot itself — the
# admin refreshes them manually (their own local Chrome cookie-extraction
# script) when Apple's session expires. Same operational model as
# checkers/croma.py's CROMA_APIM_KEY.
#
# HEADER HISTORY (worth keeping — this was mis-diagnosed once already):
# a location=<pincode> request via this same cookie-based transport
# initially 541'd ("Page Not Found"). That was first (wrongly) attributed
# to the location= param itself, and _build_fulfillment_target was
# temporarily replaced with a store=<code>-per-official-store lookup
# requiring each of the 6 stores' internal Apple codes to be manually
# captured. Real screenshots later confirmed the TRUE cause was a header
# mismatch: this function's Accept/Referer were wrong and x-skip-redirect
# was missing entirely (now fixed below, matching a real working reference
# implementation exactly). With the correct headers, location=<pincode>
# works for all 6 official stores AND any arbitrary /trackpickup pincode —
# no store-code capture needed at all, so that whole detour (store=<code>,
# config.APPLE_PICKUP_STORE_CODES, resolve_nearest_official_store) was
# reverted. Moral: verify header/cookie correctness before concluding a
# query PARAM is the problem.
# UPDATE: cookies/UA are now sourced primarily from the auto-refreshed
# session in database.apple_session_cookies (a real headless-Chromium
# session, minted periodically by the separate playwright_scraper service —
# see bot.py's apple_cookie_refresh_loop and config.py's
# APPLE_COOKIE_REFRESH_* settings) rather than solely from a manually-pasted
# APPLE_USER_AGENT/APPLE_COOKIES env var pair. The env vars are kept as a
# fallback — used only until the refresher has populated the DB for the
# first time, or if it's unavailable/failing — so a deploy that hasn't set
# up the Playwright refresher behaves exactly as before.
def _resolve_apple_session() -> tuple[str, str, str]:
    """
    Returns (user_agent, cookies, source) — source is "db" when a valid
    Playwright-refreshed session was found, else "env". Either user_agent or
    cookies may be "" if neither source has a usable value (caller already
    handles that as "not configured", same as before this DB-backed lookup
    was added).

    The DB read is wrapped in try/except so a database hiccup degrades to
    the env-var fallback rather than breaking every Apple pickup check —
    this function must never raise.
    """
    try:
        from database import get_apple_session_cookies
        row = get_apple_session_cookies()
    except Exception as exc:
        logger.warning(f"[apple] could not read auto-refreshed session from DB: {exc}")
        row = None

    if row:
        user_agent = (row.get("user_agent") or "").strip()
        cookies = (row.get("cookies") or "").strip()
        if user_agent and cookies:
            return user_agent, cookies, "db"

    # Fallback: manually-pasted Railway env vars (mirrors checkers/croma.py's
    # _apim_key() pattern — read at call time so an env var change takes
    # effect without an import-order dependency on this module's import time).
    user_agent = os.environ.get("APPLE_USER_AGENT", "").strip()
    cookies = os.environ.get("APPLE_COOKIES", "").strip()
    if user_agent and cookies:
        logger.warning(
            "[apple] no auto-refreshed session in the DB (Playwright refresher "
            "never ran or is failing) — falling back to the APPLE_USER_AGENT/"
            "APPLE_COOKIES env vars."
        )
    return user_agent, cookies, "env"


# PARAM SET / HEADER HISTORY, PART 2 (2026-07-27) — worth keeping next to
# the HEADER HISTORY note above: after the Jul 25 header fix, this endpoint
# started 541'ing ("Page Not Found") again — same status/body as the
# original incident, but this time the SKU/pincode/cookies were all
# confirmed correct (byte-identical extracted SKU vs. request parts.0;
# fails identically across multiple SKUs and pincodes; fails even for the
# refresh loop's own in-page fetch executed by a real, freshly-authenticated
# Chromium session — ruling out a session-freshness or per-page cookie-
# affinity explanation). A live Chrome DevTools "Copy as cURL" capture of a
# genuinely working request revealed the actual cause: Apple's frontend no
# longer sends `little`/`mts.1`/`fts` at all, and now sends `pl=true`
# instead — see _build_fulfillment_target's own comment. The captured
# request's Referer was also the SPECIFIC product variant page, not this
# module's old hardcoded generic "https://www.apple.com/in/shop", and it
# carried the standard Fetch-metadata/Client-Hints headers a real same-
# origin `fetch()` call sends (sec-fetch-*, sec-ch-ua*, cache-control,
# pragma, priority) that a plain httpx GET never adds on its own. Both are
# applied below now: `referer` defaults to the real tracked product URL
# (threaded through from every caller below) instead of a generic page, and
# the extra headers are added unconditionally.
_SEC_CH_UA = '"Not)A;Brand";v="99", "Google Chrome";v="128", "Chromium";v="128"'


async def _cookie_auth_fetch(
    target: str, *, log_tag: str, context: str, timeout: float,
    referer: str | None = None, client: httpx.AsyncClient | None = None,
) -> tuple[dict | None, str | None]:
    """
    One direct httpx GET to `target` (an Apple fulfillment-messages URL) —
    real browser User-Agent + Cookie headers from APPLE_USER_AGENT/
    APPLE_COOKIES env vars, no Scrape.do/Zyte involved. Returns
    (data, error) — error is None on success, else a short human-readable
    reason (missing env vars, network error, HTTP status, non-JSON body),
    for callers that want to surface WHY a fetch failed (e.g.
    /debugpickup's diagnostics, or check_pickup_at_official_stores'
    pincode->result reporting). Never raises.

    401/403, or a 200 that isn't valid JSON (Apple's real API always
    returns JSON when the session is genuinely accepted — a non-JSON 200
    body is the same "silently stalled/challenge page" signature this
    endpoint has shown before, now most likely an expired/rejected cookie
    jar) are both logged as a clear "APPLE_COOKIES likely expired"
    message, mirroring checkers/croma.py's check_via_api 401/403 handling
    for CROMA_APIM_KEY — so an admin scanning logs doesn't have to
    reverse-engineer a generic failure into "go refresh the cookies."

    `log_tag`/`context` control only the log-line prefix (e.g.
    log_tag="resolve", context="fulfillment-messages pincode='400051'") so
    each caller's Railway logs stay grep-able under its own existing
    prefix rather than a generic shared one.

    `referer`: the specific product page URL this fulfillment-messages
    call is for — see the PARAM SET / HEADER HISTORY, PART 2 note above.
    Falls back to the generic shop page only when a caller genuinely has
    no specific product URL to give (there's currently no such caller,
    but this keeps the function usable if one is ever added).

    `client`: an optional pre-built httpx.AsyncClient to reuse instead of
    opening a fresh TCP+TLS connection for this one call — passed by
    check_pickup_at_official_stores for its whole batch of pincode
    requests (a real browser session reuses one connection across many
    requests; opening N brand-new connections for N near-simultaneous
    calls is itself a bot signal, independent of request pacing). When
    None (every other call site, all single one-off requests), a client
    is created and torn down for just this call, same as before.
    """
    logger.info(f"[apple][{log_tag}] {context} target={target!r}")

    user_agent, cookies, _source = _resolve_apple_session()
    if not user_agent or not cookies:
        reason = (
            "no Apple session available — neither the auto-refreshed DB "
            "session nor the APPLE_USER_AGENT/APPLE_COOKIES env vars are set"
        )
        logger.error(
            f"[apple][{log_tag}] {context} {reason} — cannot call Apple's "
            f"fulfillment-messages API directly. Skipping."
        )
        return None, reason

    headers = {
        "User-Agent": user_agent,
        "Cookie": cookies,
        "Accept": "*/*",
        "Referer": referer or "https://www.apple.com/in/shop",
        "x-skip-redirect": "true",
        # Fetch-metadata + Client Hints headers a real same-origin
        # fetch() call sends automatically — a plain httpx GET doesn't add
        # any of these on its own. sec-ch-ua* below matches the Chrome
        # version/platform in `user_agent` (Chrome 128 on Windows, see
        # playwright_scraper's _REALISTIC_USER_AGENT) — these are standard
        # Client Hints values for that browser/OS combination, not scraped
        # from a specific captured request, so double-check against a
        # fresh capture if Apple's edge starts treating this differently.
        "sec-ch-ua": _SEC_CH_UA,
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "priority": "u=1, i",
        "pragma": "no-cache",
        "cache-control": "no-cache",
    }

    # Diagnostic — header NAMES + cookie length only, never the cookie
    # value itself (a live Apple session shouldn't land in Railway logs).
    # Added alongside the Playwright refresh's own akamai_markers_present
    # logging to compare "cookies minted" against "cookies actually sent
    # on a failing request" without guessing.
    logger.info(
        f"[apple][{log_tag}] {context} request_headers={sorted(headers.keys())} "
        f"cookie_length={len(cookies)} user_agent={user_agent!r}"
    )

    try:
        if client is not None:
            resp = await client.get(target, headers=headers)
        else:
            async with httpx.AsyncClient(timeout=timeout) as owned_client:
                resp = await owned_client.get(target, headers=headers)
    except Exception as exc:
        reason = f"request failed: {type(exc).__name__}: {exc}"
        logger.warning(f"[apple][{log_tag}] {context} {reason}", exc_info=True)
        return None, reason

    logger.info(f"[apple][{log_tag}] {context} status={resp.status_code}")

    if resp.status_code in (401, 403):
        reason = f"HTTP {resp.status_code} — Apple session likely expired"
        logger.error(
            f"[apple][{log_tag}] {context} HTTP {resp.status_code} — Apple "
            f"session likely expired. If the Playwright auto-refresher "
            f"(PLAYWRIGHT_SCRAPER_URL) is configured it should self-correct "
            f"on its next cycle; otherwise re-run your local cookie-"
            f"extraction script and update the APPLE_COOKIES / "
            f"APPLE_USER_AGENT Railway env vars. Skipping. "
            f"full response (up to 1500 chars): {resp.text[:1500]!r} "
            f"response_headers={dict(resp.headers)}"
        )
        return None, reason
    if resp.status_code != 200:
        reason = f"HTTP {resp.status_code}: {resp.text[:200]!r}"
        logger.warning(
            f"[apple][{log_tag}] {context} {reason} — full response "
            f"(up to 1500 chars): {resp.text[:1500]!r} "
            f"response_headers={dict(resp.headers)}"
        )
        return None, reason

    try:
        return resp.json(), None
    except Exception:
        reason = f"non-JSON response (likely a bot-check/challenge page): {resp.text[:200]!r}"
        logger.error(
            f"[apple][{log_tag}] {context} non-JSON response (likely a "
            f"bot-check/challenge page) — Apple session likely expired; see "
            f"the 401/403 log line above for the same self-correction note. "
            f"full body (up to 1500 chars): {resp.text[:1500]!r} "
            f"response_headers={dict(resp.headers)}"
        )
        return None, reason


async def _fetch_pickup_availability(
    sku: str, pincode: str, product_url: str | None = None,
) -> tuple[dict | None, str | None, list[tuple[str, str | None]]]:
    """
    Calls Apple's fulfillment-messages endpoint directly via
    _build_fulfillment_target + _cookie_auth_fetch above (real Cookie/
    User-Agent headers, no Scrape.do/Zyte). Returns (data, method,
    diagnostics):
      - method is "direct" on success, None on failure.
      - diagnostics is a single-entry list [("direct", error_or_None)] —
        kept as a list (not a bare tuple) for backward compatibility with
        every existing caller (check_pickup_row, refine_with_pincode,
        admin_handlers.py's /debugpickup) that already unpacks/iterates
        it, even though there's only ever one attempt now — no more
        "session"/"session_super_proxy" tiers to report (that whole
        navigate-then-execute-in-session approach is gone; see
        _cookie_auth_fetch's module note above).
    Never raises; callers that only need the data can unpack as
    `data, _method, _diag = await ...`, exactly as before.

    product_url is now used as the request's Referer header (see the
    PARAM SET / HEADER HISTORY, PART 2 note above _cookie_auth_fetch) — a
    real browser's fulfillment-messages call carries the SPECIFIC product
    page's URL as Referer, not a generic shop-page URL, so every caller
    passing a real tracked-product URL here now gets that matched exactly.
    """
    target = _build_fulfillment_target(sku, pincode)
    data, err = await _cookie_auth_fetch(
        target, log_tag="resolve", context=f"fulfillment-messages pincode={pincode!r}",
        timeout=_FULFILLMENT_TIMEOUT, referer=product_url,
    )
    diagnostics: list[tuple[str, str | None]] = [("direct", err)]
    if data is not None:
        return data, "direct", diagnostics
    return None, None, diagnostics


def _evaluate_pickup_availability(data: dict, sku: str) -> bool | None:
    """
    True  - at least one nearby store shows this SKU as pickup available/
             eligible: a genuine, pincode-specific confirmation of in-stock.
    None  - inconclusive: no stores found near this pincode (the common case
             for most Indian pincodes — see module docstring), an
             "unavailable" pickup result (not treated as OOS, for the same
             reason), or an unexpected response shape. Caller falls back to
             the generic check() rather than risk a false OOS.
    """
    logger.info(f"[apple][resolve] raw fulfillment response (truncated): {str(data)[:500]!r}")

    stores = (
        (data.get("body") or {}).get("content", {}).get("pickupMessage", {}).get("stores", [])
    )
    logger.info(f"[apple][resolve] {len(stores)} store(s) returned for this pincode")

    if not stores:
        logger.info(
            "[apple][resolve] no stores found near this pincode (common in India's "
            "sparse Apple Store network) — inconclusive, falling back to generic check"
        )
        return None

    for store in stores:
        part_info = (store.get("partsAvailability") or {}).get(sku, {})
        pickup_display = part_info.get("pickupDisplay", "")
        logger.info(
            f"[apple][resolve] store={store.get('storeName')!r} "
            f"pickupDisplay={pickup_display!r}"
        )
        if pickup_display in ("available", "eligible"):
            logger.info(
                f"[apple][resolve] confirmed available at {store.get('storeName')!r} → True"
            )
            return True

    logger.info(
        "[apple][resolve] no store shows pickup availability for this SKU — NOT "
        "treated as OOS (pickup-only signal; courier delivery coverage is wider "
        "than pickup in India); falling back to generic check"
    )
    return None


# ═══════════════════════════════════════════════════════════════════════════
# Pickup-availability TRACKING (separate from refine_with_pincode above,
# which only ever CONFIRMS the existing generic in-stock check and never
# reports store-level detail). Used by /trackpickup + bot.run_pickup_check_cycle
# — see database.py's pickup_tracking table and bot.py's module docstring
# for the feature. Additive only: nothing below changes check(),
# refine_with_pincode(), or _evaluate_pickup_availability()'s existing
# behavior, so the regular per-pincode stock-confirmation path used in
# production today is untouched.
# ═══════════════════════════════════════════════════════════════════════════

# Best-effort ONLY — unlike the SKU/auth/response-shape details confirmed
# above via multiple independent sources, no confirmed field name for a
# per-store distance or postal address was found on the store object inside
# pickupMessage.stores (independent sources agree on storeName/storeNumber/
# storeListNumber/city/state/partsAvailability, but none show a distance or
# full-address field). Because this only affects optional, cosmetic
# notification text — never the True/False availability signal itself — it's
# safe to speculatively check a short list of plausible keys rather than
# omitting the feature entirely: any that happen to exist in the real
# response get included, any that don't are silently skipped, so a wrong
# guess here can only make a notification slightly less detailed, never
# incorrect.
_STORE_LOCATION_KEYS = (
    "storeDistanceWithUnit", "distance", "address", "city", "state",
)


def _extract_store_location(store: dict) -> str | None:
    """Best-effort, optional 'distance/address' text for a pickup-alert
    notification — see the module note above on why this speculatively
    checks several plausible keys instead of a single confirmed one."""
    parts = []
    for key in _STORE_LOCATION_KEYS:
        val = store.get(key)
        if isinstance(val, str) and val.strip():
            parts.append(val.strip())
    if not parts:
        return None
    # De-dupe while preserving order (e.g. "city" and "state" might both
    # legitimately appear; a key repeating the same text as another doesn't).
    seen = set()
    unique_parts = []
    for p in parts:
        if p not in seen:
            seen.add(p)
            unique_parts.append(p)
    return ", ".join(unique_parts)


def _extract_product_name(soup: BeautifulSoup) -> str | None:
    """Best-effort product display name for a pickup-tracking entry — tries
    JSON-LD's "name" field first (same blocks _extract_sku already scans),
    then falls back to the page <title> tag. Returns None if neither is
    found; the caller falls back to a URL-derived name in that case."""
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except Exception:
            continue
        for item in (data if isinstance(data, list) else [data]):
            if isinstance(item, dict) and item.get("name"):
                return str(item["name"]).strip()

    if soup.title and soup.title.string:
        return soup.title.string.strip()

    return None


def available_stores_for_pickup(data: dict, sku: str) -> list[dict]:
    """
    Returns every store in `data` (a raw fulfillment-messages response) where
    `sku` currently shows pickupDisplay "available"/"eligible" — the same
    positive-signal check _evaluate_pickup_availability uses, but returning
    full per-store detail (name + best-effort location) instead of a
    collapsed True/None verdict, for the pickup-tracker's notification text.
    Empty list means "no store currently shows pickup available for this
    SKU/pincode" — including the case where `data` has zero stores at all.
    """
    stores = (
        (data.get("body") or {}).get("content", {}).get("pickupMessage", {}).get("stores", [])
    )
    available = []
    for store in stores:
        part_info = (store.get("partsAvailability") or {}).get(sku, {})
        pickup_display = part_info.get("pickupDisplay", "")
        if pickup_display in ("available", "eligible"):
            available.append({
                "store_name": store.get("storeName") or "(unnamed store)",
                "location": _extract_store_location(store),
            })
    return available


async def check_pickup_row(bot, row: dict) -> dict:
    """
    Checks every saved pincode for one database.pickup_tracking row RIGHT
    NOW: calls the fulfillment-messages API per pincode, updates the row's
    persisted pincode_status on any change, and sends a pickup-availability
    notification (notifications.send_pickup_alert) on a genuine
    unavailable->available transition.

    Lives HERE rather than in bot.py (where it originated) so both
    bot.run_pickup_check_cycle's scheduled cycle AND handlers.py's
    on-demand /mypickups command can share ONE implementation — handlers.py
    can't import bot.py directly (bot.py imports handlers.router, so that
    would be circular), but both already import checkers.apple.

    Sequential across pincodes WITHIN this row (never concurrent) so
    pincode_status can be safely read-modified-written once at the end
    without a lost-update race between two pincodes of the SAME row
    finishing at different times. Callers may still run different ROWS
    concurrently (see run_pickup_check_cycle's semaphore-gated gather) —
    this only serializes within a row, matching the low pincode-per-row
    counts the feature expects (a handful of pincodes at most).

    Returns {pincode: [store dicts]} for every pincode actually checked
    this call — used by /mypickups to show the caller a live per-pincode
    result immediately, on top of the DB-update-and-notify side effects
    this function already performs (the scheduled cycle simply ignores the
    return value). A pincode whose API call fails this round is absent
    from the returned dict, mirroring its "left untouched" DB-status
    behavior below.

    A pincode whose API call fails (data is None — network error, non-200,
    non-JSON/challenge page) is left completely untouched: mirrors
    bot._apply_result_to_row's "None = inconclusive, skip the write"
    convention for the regular stock checker — a transient failure must
    never flip a previously-available pincode back to unavailable, which
    would otherwise manufacture a spurious future "transition" and a
    duplicate/false alert once the API recovers.

    A successful call with zero available stores (including zero stores
    returned at all) IS a real, known "unavailable" answer for this
    feature's specific question ("is pickup available near this pincode
    right now") — unlike the generic OOS-inference use in
    _evaluate_pickup_availability, there's no separate signal here that a
    "no stores nearby" result could be confused with, so it's safe to
    persist as False.
    """
    # Deferred imports: database.py/notifications.py don't import checkers
    # back (verified — no cycle either direction), but keeping these local
    # avoids any import-order surprise and matches this codebase's
    # established caution around cross-module imports (see
    # checkers/flipkart_api.py's check_stock_with_fallback for the same
    # pattern).
    from database import update_pickup_status
    from notifications import send_pickup_alert

    status = dict(row["pincode_status"])
    changed = False
    results: dict[str, list[dict]] = {}
    for pincode in row["pincodes"]:
        try:
            data, _method, _diag = await _fetch_pickup_availability(row["sku"], pincode, row["url"])
        except Exception as exc:
            logger.error(
                f"[apple][pickup] error checking tracking #{row['id']} pincode={pincode!r}: {exc}"
            )
            continue
        if data is None:
            continue  # inconclusive this call — leave prior status untouched

        stores = available_stores_for_pickup(data, row["sku"])
        results[pincode] = stores
        now_available = bool(stores)
        was_available = bool(status.get(pincode, False))

        if now_available != was_available:
            status[pincode] = now_available
            changed = True

        if now_available and not was_available:
            try:
                await send_pickup_alert(bot, row["user_id"], row["name"], pincode, stores)
            except Exception as exc:
                logger.error(
                    f"[apple][pickup] error sending alert for tracking #{row['id']} "
                    f"pincode={pincode!r}: {exc}"
                )

    if changed:
        update_pickup_status(row["id"], status)

    return results


async def check_channel_pickup_row(bot, row: dict) -> dict:
    """
    Channel-forwarding sibling of check_pickup_row above — same
    fetch/persist/notify shape, but for ONE database.channel_forward_
    pickup_tracking row (now a pincodes LIST + pincode_status dict per
    row, mirroring pickup_tracking's own shape exactly) and forwarding
    alerts to the registered channel (looked up here via
    database.get_forward_channel, same as bot.py's own
    _apply_result_to_channel_forward_row does for the stock side) instead
    of a user_id. Lives here for the same reason check_pickup_row does: bot.py's
    background cycle AND admin_handlers.py's on-demand /checkforwarding
    both need this, and admin_handlers.py can't import bot.py (circular —
    bot.py imports admin_handlers.router), but both already import
    checkers.apple.

    SKU is fetched fresh from the product page if not already cached on
    the row (mirrors bot.py's own _check_apple_official_pickup_group) —
    channel_forward_pickup_tracking rows are created via /addchannelpickup
    with the SKU already resolved at add-time, so this is normally a
    no-op fetch-skip; only relevant if that initial extraction somehow
    didn't get persisted.

    Sequential across pincodes WITHIN this row, same reasoning as
    check_pickup_row's own docstring (avoids a lost-update race on this
    row's single persisted pincode_status dict).

    Returns {pincode: [store dicts]} for every pincode actually checked
    this call — used by /checkforwarding to show a live per-pincode
    result immediately, same as check_pickup_row's own return value is
    used by /mypickups. A pincode whose API call fails this round is
    absent from the returned dict and left untouched in the persisted
    status, mirroring check_pickup_row's own inconclusive-result handling.

    Status + last_checked are persisted unconditionally at the end of
    every call (unlike check_pickup_row, which only writes `if changed`)
    so /listforwarding's last-checked display stays accurate even on
    cycles with no transitions.
    """
    # Deferred imports — see check_pickup_row's own note above for why.
    from database import update_channel_forward_pickup_status, is_forwarding_paused, get_forward_channel
    from notifications import send_channel_pickup_alert

    sku = row.get("sku")
    if not sku:
        try:
            resp = await fetch_page(row["url"], render_js=NEEDS_JS, timeout=30.0, site="apple")
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")
            sku = _extract_sku(soup, resp.text)
        except Exception as exc:
            logger.error(
                f"[apple][channel-pickup] product page fetch/SKU extraction "
                f"failed for #{row['id']} url={row['url']!r}: {exc}"
            )
            return {}
        if not sku:
            logger.warning(
                f"[apple][channel-pickup] could not extract a SKU for "
                f"#{row['id']} url={row['url']!r} — skipping this cycle"
            )
            return {}

    status = dict(row.get("pincode_status") or {})
    results: dict[str, list[dict]] = {}
    channel = None
    for pincode in row.get("pincodes") or []:
        try:
            data, _method, _diag = await _fetch_pickup_availability(sku, pincode, row["url"])
        except Exception as exc:
            logger.error(
                f"[apple][channel-pickup] error checking #{row['id']} pincode={pincode!r}: {exc}"
            )
            continue
        if data is None:
            continue  # inconclusive this call — leave prior status untouched

        stores = available_stores_for_pickup(data, sku)
        results[pincode] = stores
        now_available = bool(stores)
        was_available = bool(status.get(pincode, False))
        status[pincode] = now_available

        if now_available and not was_available:
            if is_forwarding_paused():
                logger.info(
                    f"[apple][channel-pickup] #{row['id']} pincode={pincode!r} "
                    f"transitioned to available but forwarding is paused — "
                    f"alert suppressed."
                )
            else:
                if channel is None:
                    channel = get_forward_channel() or {}
                if not channel:
                    logger.warning(
                        f"[apple][channel-pickup] #{row['id']} pincode={pincode!r} "
                        f"transitioned to available but no channel is "
                        f"registered — alert skipped."
                    )
                else:
                    try:
                        await send_channel_pickup_alert(bot, channel["chat_id"], row["name"], pincode, stores)
                    except Exception as exc:
                        logger.error(
                            f"[apple][channel-pickup] alert failed for #{row['id']} "
                            f"pincode={pincode!r}: {exc}"
                        )

    update_channel_forward_pickup_status(row["id"], status, sku=sku)

    return results


async def refine_with_pincode(
    soup: BeautifulSoup, html: str, pincode: str, generic_result: bool, url: str,
) -> bool:
    """
    Called from stock_checker.py when a pincode is available for an Apple
    product. Tries to CONFIRM in-stock via the public fulfillment-messages
    API; never downgrades the result to OOS based on pincode data alone —
    worst case (SKU not found, API fails, no stores nearby, or nothing
    available for pickup), it returns generic_result unchanged, so accuracy
    is never worse than the pre-existing page-based check.

    `url` is the tracked product page URL. It's threaded through to
    _fetch_pickup_availability for signature backward-compatibility only —
    that function no longer uses it (see its own docstring); kept here so
    this function's own signature/callers don't need to change either.
    """
    sku = _extract_sku(soup, html)
    if not sku:
        logger.warning(
            "[apple][resolve] no SKU extracted — cannot run pincode-specific "
            "lookup, falling back to generic check"
        )
        return generic_result

    data, _method, _diag = await _fetch_pickup_availability(sku, pincode, url)
    if data is None:
        return generic_result

    pincode_result = _evaluate_pickup_availability(data, sku)
    if pincode_result is None:
        return generic_result

    return pincode_result


# ═══════════════════════════════════════════════════════════════════════════
# Official-store pickup checker (checkers.apple.check_pickup_at_official_
# stores + bot.run_apple_official_pickup_cycle) — a THIRD, separate Apple
# signal from check()/refine_with_pincode() above. Checks the SAME
# fulfillment-messages endpoint, but against a FIXED list of India's 6
# physical Apple Store pincodes (config.APPLE_PICKUP_PINCODES) for every
# /add-tracked apple.com product automatically — unlike refine_with_pincode
# (the user's own single saved pincode only) or the separate opt-in
# /trackpickup system (user-chosen pincodes, requires a command to set up).
#
# Shares _cookie_auth_fetch/_resolve_apple_session AND
# _build_fulfillment_target (all defined above, next to
# _fetch_pickup_availability) for the actual request — this checker and
# _fetch_pickup_availability hit the exact same Apple endpoint the exact
# same way (real Cookie/User-Agent headers, no Scrape.do/Zyte,
# location=<pincode> — confirmed via real screenshots to work for all 6
# official-store pincodes once the request headers were fixed; see
# _cookie_auth_fetch's own module note for that history). Consequently
# this checker spends ZERO Scrape.do/Zyte credits and is automatically
# absent from database.get_zyte_usage_summary's per-site breakdown /
# admin_handlers.py's /creditusage (that table is only ever written to
# from inside zyte_client.fetch_page) — no separate credit-tracking
# exclusion needed.
# ═══════════════════════════════════════════════════════════════════════════

_OFFICIAL_STORE_TIMEOUT = 20.0


async def _fetch_official_store_availability(
    sku: str, pincode: str, *, product_url: str | None = None, client: httpx.AsyncClient | None = None,
) -> dict | None:
    """
    One direct httpx GET straight to Apple's fulfillment-messages endpoint
    for one of the 6 fixed official-store pincodes — see _cookie_auth_fetch
    (shared with _fetch_pickup_availability above) for the actual request/
    auth/error-handling logic. Returns the parsed JSON on success, None on
    any failure (missing env vars, network error, HTTP status, non-JSON
    body — all logged there). Never raises.

    `client`: optional shared httpx.AsyncClient, passed through to
    _cookie_auth_fetch — see check_pickup_at_official_stores below, the
    only caller that passes one (its whole 6-pincode batch reuses a
    single connection instead of opening a fresh one per pincode).
    """
    target = _build_fulfillment_target(sku, pincode)
    data, _err = await _cookie_auth_fetch(
        target, log_tag="official-stores", context=f"pincode={pincode!r}",
        timeout=_OFFICIAL_STORE_TIMEOUT, referer=product_url, client=client,
    )
    return data


# Randomized delay between consecutive pincode requests within one
# check_pickup_at_official_stores batch — replaces the old asyncio.gather
# parallel burst (all 6 pincodes hit at the exact same instant), which is
# itself a strong bot signal no real browser session produces. Range
# chosen to look like plausible human inter-request timing without
# meaningfully slowing the official-store cycle (worst case ~5×5s=25s of
# added delay per product, well within APPLE_PICKUP_CHECK_INTERVAL).
_PINCODE_DELAY_MIN_SECONDS = 2.0
_PINCODE_DELAY_MAX_SECONDS = 5.0


async def check_pickup_at_official_stores(
    sku: str, pincodes: list[str], *, product_url: str | None = None,
) -> dict[str, list[dict]]:
    """
    Checks `sku` against every pincode in `pincodes` (config.
    APPLE_PICKUP_PINCODES in production) SEQUENTIALLY — NOT concurrently
    (see _PINCODE_DELAY_MIN/MAX_SECONDS above for why the old
    asyncio.gather-based parallel burst was replaced) — with a randomized
    delay between each pincode, reusing ONE httpx.AsyncClient across the
    whole batch instead of opening a fresh TCP+TLS connection per pincode
    (see _cookie_auth_fetch's `client` param). Both changes exist to make
    this endpoint's traffic look less like an automated burst and more
    like a real session's occasional, connection-reusing requests — part
    of investigating why APPLE_COOKIES' session was dying after ~2 hours.

    Returns {pincode: [store dicts]} — the SAME shape checkers.apple.
    check_pickup_row/available_stores_for_pickup already use, reused here
    rather than a parallel parsing implementation. A pincode whose request
    failed (see _fetch_official_store_availability) is simply absent from
    the returned dict — the caller treats a missing key as "inconclusive
    this cycle for that pincode", never as a confirmed "not available".
    """
    results: dict[str, list[dict]] = {}
    async with httpx.AsyncClient(timeout=_OFFICIAL_STORE_TIMEOUT) as client:
        for i, pincode in enumerate(pincodes):
            data = await _fetch_official_store_availability(sku, pincode, product_url=product_url, client=client)
            if data is not None:
                results[pincode] = available_stores_for_pickup(data, sku)
            if i < len(pincodes) - 1:
                await asyncio.sleep(random.uniform(_PINCODE_DELAY_MIN_SECONDS, _PINCODE_DELAY_MAX_SECONDS))
    return results
