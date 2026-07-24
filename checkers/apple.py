import asyncio
import json
import logging
import os
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
# checkers/croma.py's CROMA_APIM_KEY, and the exact approach already
# proven out for the official-store checker below (_fetch_official_store_
# availability originally had its own near-identical copy of this; the two
# were unified into this one shared helper once both existed, since the
# only difference between them was the target URL's param set and log
# labels — the auth/error-handling logic itself was already duplicated
# verbatim, so keeping two copies risked exactly the "fixed one, forgot
# the other" bug this refactor eliminates).
def _apple_user_agent() -> str:
    # Read at call time (mirrors checkers/croma.py's _apim_key() pattern)
    # so a Railway env var change (a manual cookie/UA refresh) takes effect
    # without an import-order dependency on this module's own import time.
    return os.environ.get("APPLE_USER_AGENT", "").strip()


def _apple_cookies() -> str:
    return os.environ.get("APPLE_COOKIES", "").strip()


async def _cookie_auth_fetch(
    target: str, *, log_tag: str, context: str, timeout: float,
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
    """
    logger.info(f"[apple][{log_tag}] {context} target={target!r}")

    user_agent = _apple_user_agent()
    cookies = _apple_cookies()
    if not user_agent or not cookies:
        reason = "APPLE_USER_AGENT and/or APPLE_COOKIES env var is not set"
        logger.error(
            f"[apple][{log_tag}] {context} {reason} — cannot call Apple's "
            f"fulfillment-messages API directly. Skipping."
        )
        return None, reason

    headers = {
        "User-Agent": user_agent,
        "Cookie": cookies,
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www.apple.com/in/shop/buy-iphone",
    }

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(target, headers=headers)
    except Exception as exc:
        reason = f"request failed: {type(exc).__name__}: {exc}"
        logger.warning(f"[apple][{log_tag}] {context} {reason}", exc_info=True)
        return None, reason

    logger.info(f"[apple][{log_tag}] {context} status={resp.status_code}")

    if resp.status_code in (401, 403):
        reason = f"HTTP {resp.status_code} — APPLE_COOKIES likely expired"
        logger.error(
            f"[apple][{log_tag}] {context} HTTP {resp.status_code} — "
            f"APPLE_COOKIES likely expired, refresh needed (re-run your "
            f"local cookie-extraction script and update the APPLE_COOKIES "
            f"/ APPLE_USER_AGENT Railway env vars). Skipping."
        )
        return None, reason
    if resp.status_code != 200:
        reason = f"HTTP {resp.status_code}: {resp.text[:200]!r}"
        logger.warning(f"[apple][{log_tag}] {context} {reason}")
        return None, reason

    try:
        return resp.json(), None
    except Exception:
        reason = f"non-JSON response (likely a bot-check/challenge page): {resp.text[:200]!r}"
        logger.error(
            f"[apple][{log_tag}] {context} non-JSON response (likely a "
            f"bot-check/challenge page) — APPLE_COOKIES likely expired, "
            f"refresh needed. body={resp.text[:200]!r}"
        )
        return None, reason


async def _fetch_pickup_availability(
    sku: str, pincode: str, product_url: str | None = None,
) -> tuple[dict | None, str | None, list[tuple[str, str | None]]]:
    """
    Calls Apple's fulfillment-messages endpoint directly (see
    _cookie_auth_fetch above). Returns (data, method, diagnostics):
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

    product_url is accepted for backward compatibility with every existing
    call site's signature but is no longer used — the old navigate-first
    approach needed it to establish a page session before triggering the
    in-page fetch; the direct cookie-based approach doesn't navigate
    anywhere, so there's nothing left to pass it to.
    """
    target = _build_fulfillment_target(sku, pincode)
    data, err = await _cookie_auth_fetch(
        target, log_tag="resolve", context=f"fulfillment-messages pincode={pincode!r}",
        timeout=_FULFILLMENT_TIMEOUT,
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
# Shares _cookie_auth_fetch/_apple_user_agent/_apple_cookies (defined above,
# next to _fetch_pickup_availability) for the actual HTTP call — both this
# and _fetch_pickup_availability hit the exact same Apple endpoint the
# exact same way (real Cookie/User-Agent headers, no Scrape.do/Zyte); only
# the target URL's param set (see _build_official_store_fulfillment_url vs
# _build_fulfillment_target — kept as two independent builders, since
# they're plausibly real variations Apple's storefront sends in different
# contexts, not confirmed identical) and what happens with the result
# differ between the two features. Consequently this checker spends ZERO
# Scrape.do/Zyte credits and is automatically absent from database.
# get_zyte_usage_summary's per-site breakdown / admin_handlers.py's
# /creditusage (that table is only ever written to from inside
# zyte_client.fetch_page) — no separate credit-tracking exclusion needed.
#
# Endpoint/params as supplied for this task (a different param combination
# than _build_fulfillment_target's above — fae/little/mts.0/mts.1/fts vs
# fae/pl/mts.0). `location` as the pincode param name and 201301 for Apple
# Noida were both independently confirmed via WebSearch cross-referencing
# before this was implemented.
# ═══════════════════════════════════════════════════════════════════════════

_OFFICIAL_STORE_TIMEOUT = 20.0


def _build_official_store_fulfillment_url(sku: str, pincode: str) -> str:
    params = {
        "fae": "true",
        "little": "false",
        "parts.0": sku,
        "mts.0": "regular",
        "mts.1": "sticky",
        "fts": "true",
        "location": pincode,
    }
    return f"{_FULFILLMENT_URL}?{urlencode(params)}"


async def _fetch_official_store_availability(sku: str, pincode: str) -> dict | None:
    """
    One direct httpx GET straight to Apple's fulfillment-messages endpoint
    for one official-store pincode — see _cookie_auth_fetch (shared with
    _fetch_pickup_availability above) for the actual request/auth/error-
    handling logic. Returns the parsed JSON on success, None on any
    failure (missing env vars, network error, HTTP status, non-JSON body —
    all logged there). Never raises.
    """
    target = _build_official_store_fulfillment_url(sku, pincode)
    data, _err = await _cookie_auth_fetch(
        target, log_tag="official-stores", context=f"pincode={pincode!r}",
        timeout=_OFFICIAL_STORE_TIMEOUT,
    )
    return data


async def check_pickup_at_official_stores(sku: str, pincodes: list[str]) -> dict[str, list[dict]]:
    """
    Checks `sku` against every pincode in `pincodes` (config.
    APPLE_PICKUP_PINCODES in production) concurrently, returning
    {pincode: [store dicts]} — the SAME shape checkers.apple.
    check_pickup_row/available_stores_for_pickup already use, reused here
    rather than a parallel parsing implementation. A pincode whose request
    failed (see _fetch_official_store_availability) is simply absent from
    the returned dict — the caller treats a missing key as "inconclusive
    this cycle for that pincode", never as a confirmed "not available".
    """
    async def _one(pincode: str) -> tuple[str, list[dict] | None]:
        data = await _fetch_official_store_availability(sku, pincode)
        if data is None:
            return pincode, None
        return pincode, available_stores_for_pickup(data, sku)

    results = await asyncio.gather(*[_one(p) for p in pincodes])
    return {pincode: stores for pincode, stores in results if stores is not None}
