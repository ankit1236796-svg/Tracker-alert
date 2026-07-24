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
# Was 20.0 — real-world /mypickups + background-cycle logs (see the
# improved error logging added in the prior round) showed httpx.ReadTimeout
# failures against this endpoint, meaning Apple's fulfillment-messages API
# is sometimes slower to respond than 20s allows. Bumped to 30s (the top of
# the 20-30s range that seemed reasonable) to give real headroom rather
# than nudging by a couple of seconds — a slow-but-successful response is
# still strictly better than a guaranteed timeout. This only affects THIS
# one API call's own timeout; every other checker's fetch_page timeout is
# untouched.
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


# ── Navigate-then-fetch-within-session (replaces the old standalone-URL
# attempts below) ─────────────────────────────────────────────────────────
#
# Real /debugpickup runs showed the fulfillment-messages endpoint hit as a
# cold, standalone URL (render_js=True, then render_js=True+super_proxy=True)
# ReadTimeout's every single time, no exceptions — even with a full headless
# browser. This endpoint is, in Apple's real storefront, only ever triggered
# by JS running ON the loaded product page (referrer + whatever session/
# cookie state that page load established), never hit in isolation. The
# working theory: Apple's bot-protection silently stalls a request that
# doesn't carry that context, rather than rejecting it outright.
#
# Fix: navigate to the actual product page first (full render_js=True load),
# THEN issue the fulfillment-messages fetch as an in-page fetch() from
# WITHIN that same browser session via Scrape.do's "Execute" / Zyte's
# "evaluate" browser action (see zyte_client._translate_actions — this is
# the SAME action-chain mechanism already used for RelianceDigital's pincode
# entry, just a different action type: "Execute"/"evaluate" runs arbitrary
# JS in-page rather than clicking/filling a specific element). A same-origin
# fetch() issued from the product page's own JS naturally carries the real
# referrer and whatever cookies/session that page load established — no
# separate cookie-capture-and-replay step needed.
#
# Confirmed via WebSearch (two independent result sets) that Zyte API's
# actions list supports "evaluate" (in the SAME actions array as click/type/
# waitForTimeout/waitForSelector, not a separate product) and that Scrape.do's
# playWithBrowser supports {"Action": "Execute", "Execute": "<js>"} — same
# verification bar as every other third-party API detail in this codebase
# (direct WebFetch of docs.zyte.com/scrape.do returns 403 from this sandbox).
# NOT confirmed: whether either provider reliably surfaces an evaluate/
# Execute action's own RETURN VALUE in a parseable response field — so
# rather than depend on that, the in-page script ALSO writes its result into
# a hidden marker <div> in the DOM, which shows up in the final captured
# browserHtml the exact same way RelianceDigital's pincode-interaction
# result already does (see checkers/reliancedigital.py's
# fetch_with_pincode_interaction) — a mechanism this codebase already
# relies on elsewhere, unlike the unconfirmed action-return-value path.
_PICKUP_RESULT_MARKER_ID = "__apple_pickup_result__"

# Higher than _FULFILLMENT_TIMEOUT (a bare API call) since this now waits
# for a full product-page render PLUS the in-page fetch to complete —
# matches the same ballpark as RelianceDigital's pincode-interaction tier
# (90s) rather than reusing the old 30s tuned for a much lighter request.
_FULFILLMENT_SESSION_TIMEOUT = 60.0


def _build_in_page_fetch_script(target: str) -> str:
    """
    JS executed IN the browser, after the product page has fully loaded,
    via the Execute/evaluate action. Fetches `target` (the fulfillment-
    messages URL) as a same-origin request from the page's own context —
    real referrer, real cookies, exactly like Apple's own storefront JS —
    then writes the result into a hidden marker <div> (see
    _PICKUP_RESULT_MARKER_ID) so it's recoverable from the plain captured
    HTML afterward. Also RETURNS the same result at the top level (via an
    explicit `return`) in case a provider's evaluate/Execute action DOES
    surface it directly — belt-and-suspenders, since neither extraction
    path is independently confirmed for either provider (see this
    section's module note above).
    """
    return f"""
return (async () => {{
  var marker = document.createElement('div');
  marker.id = {json.dumps(_PICKUP_RESULT_MARKER_ID)};
  marker.style.display = 'none';
  try {{
    const r = await fetch({json.dumps(target)}, {{credentials: 'include'}});
    const t = await r.text();
    marker.setAttribute('data-status', String(r.status));
    marker.textContent = t;
    document.body.appendChild(marker);
    return t;
  }} catch (e) {{
    marker.setAttribute('data-status', 'error');
    marker.textContent = String(e);
    document.body.appendChild(marker);
    return null;
  }}
}})();
""".strip()


def _extract_marker_result(html: str) -> tuple[dict | None, str | None]:
    """
    Parses the hidden marker <div> _build_in_page_fetch_script's JS writes
    into the DOM, out of the final captured product-page HTML. Returns
    (parsed_json, error) — error is a short, specific description of
    exactly which sub-step failed:
      - marker missing entirely: the Execute/evaluate action itself likely
        wasn't supported/didn't run (see zyte_client._translate_actions —
        an unrecognized action type is logged and skipped, not raised).
      - data-status="error": the in-page script's own fetch() call itself
        failed (network error, CORS, etc. — visible in the marker text).
      - present but not valid JSON: fetch() succeeded but Apple returned a
        non-JSON body (likely a block/challenge page) even WITH real
        page-session context.
    """
    soup = BeautifulSoup(html, "html.parser")
    marker = soup.find(id=_PICKUP_RESULT_MARKER_ID)
    if marker is None:
        return None, "marker not found in returned HTML — Execute/evaluate action did not run"

    status = marker.get("data-status", "")
    text = marker.get_text()
    if status == "error":
        return None, f"in-page fetch() failed: {text[:300]}"

    try:
        return json.loads(text), None
    except Exception:
        return None, f"in-page fetch() returned non-JSON (status={status!r}): {text[:300]!r}"


async def _fetch_pickup_availability_attempt_via_session(
    product_url: str, target: str, *, super_proxy: bool, method_label: str,
) -> tuple[dict | None, str | None]:
    """
    One attempt: navigate to product_url (full render_js=True load), run
    the in-page fetch script, then parse its result out of the final
    captured HTML. Returns (data, error) — error is None on success, else
    a short description of exactly which sub-step failed (product-page
    navigation itself, vs. the in-page fetch/marker extraction — see
    _extract_marker_result above), for /debugpickup to report verbatim.
    """
    try:
        resp = await fetch_page(
            product_url, render_js=True, super_proxy=super_proxy,
            play_with_browser=[{"Action": "Execute", "Execute": _build_in_page_fetch_script(target)}],
            timeout=_FULFILLMENT_SESSION_TIMEOUT, site="apple",
        )
    except Exception as exc:
        exc_response = getattr(exc, "response", None)
        status_part = f" http_status={exc_response.status_code}" if exc_response is not None else ""
        body_part = f" response_body={exc_response.text[:300]!r}" if exc_response is not None else ""
        logger.warning(
            f"[apple][resolve] fulfillment-messages ({method_label}) product-page "
            f"navigation failed: {type(exc).__name__}: {exc}{status_part}{body_part}",
            exc_info=True,
        )
        return None, f"product-page navigation failed: {type(exc).__name__}: {exc}"

    logger.info(f"[apple][resolve] fulfillment-messages ({method_label}) navigation status={resp.status_code}")
    if resp.status_code != 200:
        logger.warning(
            f"[apple][resolve] fulfillment-messages ({method_label}) product-page HTTP "
            f"{resp.status_code}: {resp.text[:200]!r}"
        )
        return None, f"product-page HTTP {resp.status_code}: {resp.text[:200]!r}"

    data, err = _extract_marker_result(resp.text)
    if err:
        logger.warning(f"[apple][resolve] fulfillment-messages ({method_label}) {err}")
        return None, err
    return data, None


async def _fetch_pickup_availability(
    sku: str, pincode: str, product_url: str | None = None,
) -> tuple[dict | None, str | None, list[tuple[str, str | None]]]:
    """
    Calls Apple's fulfillment-messages endpoint via the navigate-then-
    fetch-within-session approach (see the module note above this
    section). Returns (data, method, diagnostics):
      - method is "session" or "session_super_proxy" (whichever tier
        succeeded), or None if both failed.
      - diagnostics is a list of (method_label, error_or_None) for EVERY
        tier actually attempted, in order, so a caller like /debugpickup
        can report exactly which sub-step succeeded/failed at each tier —
        not just the final pass/fail.
    Never raises; callers that only need the data can unpack as
    `data, _method, _diag = await ...`.

    Requires product_url — the whole point of this approach is triggering
    the fetch from within a real navigation to that page, so there's no
    meaningful standalone fallback left (the old direct-fetch approach was
    removed entirely after confirming it consistently times out — see
    the module note above). Called with no product_url returns
    (None, None, [...]) immediately rather than guessing.
    """
    target = _build_fulfillment_target(sku, pincode)
    logger.info(f"[apple][resolve] fulfillment-messages target={target!r}")

    if not product_url:
        reason = "no product_url supplied — cannot navigate to establish page session"
        logger.warning(f"[apple][resolve] fulfillment-messages {reason}")
        return None, None, [("session", reason)]

    diagnostics: list[tuple[str, str | None]] = []

    data, err = await _fetch_pickup_availability_attempt_via_session(
        product_url, target, super_proxy=False, method_label="session",
    )
    diagnostics.append(("session", err))
    if data is not None:
        return data, "session", diagnostics

    logger.warning(
        f"[apple][resolve] fulfillment-messages session attempt failed ({err}) — "
        f"retrying with super_proxy=True"
    )
    data, err = await _fetch_pickup_availability_attempt_via_session(
        product_url, target, super_proxy=True, method_label="session_super_proxy",
    )
    diagnostics.append(("session_super_proxy", err))
    if data is not None:
        return data, "session_super_proxy", diagnostics

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

    `url` is the tracked product page URL — _fetch_pickup_availability now
    needs it to navigate there first (establishing real referrer/session
    context) before triggering the in-page fulfillment-messages fetch; see
    that function's module note for why.
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
# Direct httpx GET, no Scrape.do/Zyte involved — a plain fetch_page() call
# (via either provider) was found to consistently fail against this
# endpoint (Zyte: ReadTimeout every time; the navigate-then-execute-in-
# session workaround built for _fetch_pickup_availability above to work
# around that was itself found broken in production — see the
# "marker not found ... Execute/evaluate action did not run" investigation).
# Rather than keep fighting the proxy layer, this now mirrors the reference
# implementation directly: a real browser User-Agent + a real logged-in
# Cookie header, loaded from Railway env vars (APPLE_USER_AGENT,
# APPLE_COOKIES) rather than captured/managed by this bot itself — the
# admin refreshes them manually (their own local Chrome cookie-extraction
# script) when Apple's session expires, the same operational model as
# checkers/croma.py's CROMA_APIM_KEY. See _fetch_official_store_availability
# for the 401/403/non-JSON "cookies likely expired" handling.
#
# Does NOT go through checkers.common.fetch_page/zyte_client.py at all — a
# deliberate, direct httpx call, exactly mirroring checkers/croma.py's
# check_via_api (see that module's docstring). Consequently this checker
# spends ZERO Scrape.do/Zyte credits and is automatically absent from
# database.get_zyte_usage_summary's per-site breakdown / admin_handlers.py's
# /creditusage (that table is only ever written to from inside
# zyte_client.fetch_page) — no separate credit-tracking exclusion needed.
#
# _fetch_pickup_availability above (refine_with_pincode + the opt-in
# /trackpickup system's check_pickup_row) is DELIBERATELY left unchanged —
# still going through the Scrape.do/Zyte navigate-then-execute-in-session
# path, which has its own known bug (see that section's own comments).
# Whether to migrate those to this same cookie-based approach is a
# separate decision, not made here.
#
# Endpoint/params as supplied for this task (a different param combination
# than _build_fulfillment_target's above — fae/little/mts.0/mts.1/fts vs
# fae/pl/mts.0 — both plausibly real variations of what Apple's storefront
# sends in different contexts; kept as its own independent builder rather
# than merged into the existing one, matching this being a separate,
# additive feature). `location` as the pincode param name and 201301 for
# Apple Noida were both independently confirmed via WebSearch cross-
# referencing before this was implemented.
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


def _apple_user_agent() -> str:
    # Read at call time (mirrors checkers/croma.py's _apim_key() pattern)
    # so a Railway env var change (a manual cookie/UA refresh) takes effect
    # without an import-order dependency on this module's own import time.
    return os.environ.get("APPLE_USER_AGENT", "").strip()


def _apple_cookies() -> str:
    return os.environ.get("APPLE_COOKIES", "").strip()


async def _fetch_official_store_availability(sku: str, pincode: str) -> dict | None:
    """
    One direct httpx GET straight to Apple's fulfillment-messages endpoint
    — real browser User-Agent + Cookie headers from APPLE_USER_AGENT/
    APPLE_COOKIES env vars, no Scrape.do/Zyte involved (see this section's
    module note above for why). Returns the parsed JSON on success, None
    on any failure. Never raises.

    401/403, or a 200 that isn't valid JSON (Apple's real API always
    returns JSON when the session is genuinely accepted — a non-JSON 200
    body is the same "silently stalled/challenge page" signature this
    endpoint has shown before, now most likely an expired/rejected
    cookie jar) are both logged as a clear "APPLE_COOKIES likely expired"
    message, mirroring checkers/croma.py's check_via_api 401/403 handling
    for CROMA_APIM_KEY — so an admin scanning logs doesn't have to
    reverse-engineer a generic failure into "go refresh the cookies."
    """
    target = _build_official_store_fulfillment_url(sku, pincode)
    logger.info(f"[apple][official-stores] target={target!r}")

    user_agent = _apple_user_agent()
    cookies = _apple_cookies()
    if not user_agent or not cookies:
        logger.error(
            "[apple][official-stores] APPLE_USER_AGENT and/or APPLE_COOKIES "
            "env var is not set — cannot call Apple's fulfillment-messages "
            "API directly. Skipping this pincode."
        )
        return None

    headers = {
        "User-Agent": user_agent,
        "Cookie": cookies,
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www.apple.com/in/shop/buy-iphone",
    }

    try:
        async with httpx.AsyncClient(timeout=_OFFICIAL_STORE_TIMEOUT) as client:
            resp = await client.get(target, headers=headers)
    except Exception as exc:
        logger.warning(
            f"[apple][official-stores] pincode={pincode!r} request failed: "
            f"{type(exc).__name__}: {exc}",
            exc_info=True,
        )
        return None

    logger.info(f"[apple][official-stores] pincode={pincode!r} status={resp.status_code}")

    if resp.status_code in (401, 403):
        logger.error(
            f"[apple][official-stores] pincode={pincode!r} HTTP {resp.status_code} — "
            f"APPLE_COOKIES likely expired, refresh needed (re-run your local "
            f"cookie-extraction script and update the APPLE_COOKIES / "
            f"APPLE_USER_AGENT Railway env vars). Skipping this pincode."
        )
        return None
    if resp.status_code != 200:
        logger.warning(
            f"[apple][official-stores] pincode={pincode!r} HTTP {resp.status_code}: "
            f"{resp.text[:200]!r}"
        )
        return None

    try:
        return resp.json()
    except Exception:
        logger.error(
            f"[apple][official-stores] pincode={pincode!r} non-JSON response "
            f"(likely a bot-check/challenge page) — APPLE_COOKIES likely "
            f"expired, refresh needed. Skipping this pincode. "
            f"body={resp.text[:200]!r}"
        )
        return None


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
