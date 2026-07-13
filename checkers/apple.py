import json
import logging
import re
from urllib.parse import urlencode

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
_FULFILLMENT_TIMEOUT = 20.0

# Apple part numbers ("SKUs") are alphanumeric, always ending in a 2-letter
# country code + "/A" (e.g. "MG6M4HN/A" for India). Matched generically rather
# than hardcoding "HN" since not every listed product's SKU is guaranteed to
# follow that exact regional suffix.
_SKU_INLINE_PATTERN = re.compile(r'"partNumber"\s*:\s*"([A-Z0-9]{5,14}/A)"')
_SKU_JSONLD_INLINE_PATTERN = re.compile(r'"sku"\s*:\s*"([A-Z0-9]{5,14}/A)"')


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
            sku = item.get("sku") or (item.get("offers") or {}).get("sku")
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


async def _fetch_pickup_availability(sku: str, pincode: str) -> dict | None:
    """
    Calls Apple's public fulfillment-messages API. Returns the raw parsed
    JSON, or None on any failure (network error, non-200, non-JSON response —
    e.g. a block/challenge page). Never raises; the caller falls back to the
    generic page-based check() on None.
    """
    target = _build_fulfillment_target(sku, pincode)
    # render_js=False: this is a JSON API endpoint, not a page needing a
    # headless-browser render (same reasoning as Blinkit's autoSuggest/info
    # calls) — and every working third-party implementation calls it cold,
    # with no special headers/cookies required.
    logger.info(f"[apple][resolve] fulfillment-messages target={target!r}")

    try:
        resp = await fetch_page(target, render_js=False, timeout=_FULFILLMENT_TIMEOUT, site="apple")
    except Exception as exc:
        logger.warning(f"[apple][resolve] fulfillment-messages request failed: {exc}")
        return None

    logger.info(f"[apple][resolve] fulfillment-messages status={resp.status_code}")
    if resp.status_code != 200:
        logger.warning(
            f"[apple][resolve] fulfillment-messages HTTP {resp.status_code}: {resp.text[:200]!r}"
        )
        return None

    try:
        data = resp.json()
    except Exception:
        logger.warning(
            f"[apple][resolve] fulfillment-messages non-JSON response "
            f"(likely a block/challenge page): {resp.text[:300]!r}"
        )
        return None

    return data


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


async def refine_with_pincode(
    soup: BeautifulSoup, html: str, pincode: str, generic_result: bool
) -> bool:
    """
    Called from stock_checker.py when a pincode is available for an Apple
    product. Tries to CONFIRM in-stock via the public fulfillment-messages
    API; never downgrades the result to OOS based on pincode data alone —
    worst case (SKU not found, API fails, no stores nearby, or nothing
    available for pickup), it returns generic_result unchanged, so accuracy
    is never worse than the pre-existing page-based check.
    """
    sku = _extract_sku(soup, html)
    if not sku:
        logger.warning(
            "[apple][resolve] no SKU extracted — cannot run pincode-specific "
            "lookup, falling back to generic check"
        )
        return generic_result

    data = await _fetch_pickup_availability(sku, pincode)
    if data is None:
        return generic_result

    pincode_result = _evaluate_pickup_availability(data, sku)
    if pincode_result is None:
        return generic_result

    return pincode_result
