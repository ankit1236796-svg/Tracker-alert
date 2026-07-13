import json
import logging
import re

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# Documentation-only (not read by any code — see stock_checker._JS_SITES
# for the actual render=true/false switch, and
# stock_checker._EXTRA_RETRY_ON_INCOMPLETE_SITES for the "don't guess a
# verdict from a still-blocked page" retry/skip logic).
NEEDS_JS = True

# Confirmed via real /debuginventstore results: WooCommerce never emits a
# literal "stock in-stock" marker. Only an OUT-OF-STOCK variation gets an
# explicit availability_html override (a <p class="stock out-of-stock">
# element, JS-swapped in when that combination is selected); an in-stock
# variation's availability_html is simply empty/absent, so "in stock" can
# only be inferred by the ABSENCE of this marker on some variation, never
# by a positive marker of its own. The backslash before each quote is
# optional so this matches both the JSON-escaped form (as it appears when
# availability_html is embedded as a JSON string value inside the page's
# variations data blob) and a plain, directly-rendered one.
_STOCK_OOS_PATTERN = re.compile(r'class=\\?"stock out-of-stock\\?"', re.IGNORECASE)

# Every entry in WooCommerce's variations data blob (one per color/storage
# combination) carries its own "variation_id" — a standard field WooCommerce's
# own core JS (wc-add-to-cart-variation.js) uses to match a selected
# combination to its variation, present regardless of theme/markup
# differences. Counting its occurrences gives the TOTAL possible variation
# count (e.g. 3 colors x 2 storages = 6) without needing to parse the
# color/storage <select> dropdowns and compute their cartesian product
# directly — the variations blob already has exactly one entry per
# combination. Backslash-optional for the same JSON-escaping reason as above.
_VARIATION_ID_PATTERN = re.compile(r'\\?"variation_id\\?"\s*:', re.IGNORECASE)


def _count_total_variations(html: str) -> int:
    """Total possible product variations (e.g. 3 colors x 2 storages = 6
    combinations), counted via "variation_id" occurrences in the raw
    HTML's embedded variations data blob — see _VARIATION_ID_PATTERN."""
    return len(_VARIATION_ID_PATTERN.findall(html))


def _count_out_of_stock_variations(html: str) -> int:
    """Count of variations carrying WooCommerce's out-of-stock
    availability_html marker (class="stock out-of-stock", plain or
    JSON-escaped) in the raw HTML — see _STOCK_OOS_PATTERN."""
    return len(_STOCK_OOS_PATTERN.findall(html))


def _offer_availability(offers) -> str:
    """Extract the first availability string from a JSON-LD 'offers'
    value that may be a single Offer dict, an AggregateOffer dict
    wrapping a nested offers list, or a plain list of Offer dicts."""
    if isinstance(offers, dict):
        avail = offers.get("availability", "")
        if avail:
            return str(avail)
        nested = offers.get("offers", [])
        if isinstance(nested, list):
            for o in nested:
                if isinstance(o, dict) and o.get("availability"):
                    return str(o["availability"])
        elif isinstance(nested, dict) and nested.get("availability"):
            return str(nested["availability"])
    elif isinstance(offers, list):
        for o in offers:
            if isinstance(o, dict) and o.get("availability"):
                return str(o["availability"])
    return ""


def check(soup: BeautifulSoup, html: str) -> bool:
    """
    inventstore.in's stock-detection logic — WooCommerce variation
    counting, NOT a "does an in-stock marker appear" text/class search.

    Real /debuginventstore results confirmed WooCommerce never emits a
    literal "stock in-stock" marker for a purchasable variation (see
    _STOCK_OOS_PATTERN's docstring) — the previous version of this
    checker looked for that marker as a positive "any in-stock" signal,
    which could never actually fire. The correct read is comparative:
    count how many of the product's TOTAL possible variations carry the
    out-of-stock marker, and compare against the total variation count
    itself (both via raw-HTML occurrence counting, not JSON parsing, to
    stay robust against exact-structure differences — see
    _count_total_variations / _count_out_of_stock_variations).

    Detection order:
    1. JSON-LD product-level availability (kept — a structured,
       whole-product signal, not per-variation free text, and not
       implicated in the issue that led to this change).
    2. WooCommerce variation counting: if the out-of-stock count is LESS
       than the total variation count, at least one combination is still
       purchasable -> in stock. If the out-of-stock count EQUALS the
       total (every combination unavailable) -> out of stock. Only
       applied when at least one variation was actually found (total > 0)
       — otherwise this comparison is meaningless.
    3. No signal at all (no variations found, no conclusive JSON-LD)
       -> defaults to out of stock, per this codebase's standing
       principle that a missed alert is safer than a false one.
    """
    # ── JSON-LD ──────────────────────────────────────────────────────────
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except Exception:
            continue
        for item in (data if isinstance(data, list) else [data]):
            if not isinstance(item, dict):
                continue
            avail = _offer_availability(item.get("offers", {}))
            if "InStock" in avail:
                logger.info("[inventstore] JSON-LD: InStock → True")
                return True
            if "OutOfStock" in avail or "Discontinued" in avail:
                logger.info("[inventstore] JSON-LD: OutOfStock/Discontinued → False")
                return False

    # ── WooCommerce variation counting ──────────────────────────────────
    total_variations = _count_total_variations(html)
    out_of_stock_count = _count_out_of_stock_variations(html)
    if total_variations > 0:
        if out_of_stock_count < total_variations:
            logger.info(
                f"[inventstore] {out_of_stock_count}/{total_variations} variations "
                f"out of stock → True (at least one combination still purchasable)"
            )
            return True
        logger.info(
            f"[inventstore] {out_of_stock_count}/{total_variations} variations "
            f"out of stock (all) → False"
        )
        return False

    logger.info(
        "[inventstore] no variations found (0 'variation_id' occurrences) and "
        "no conclusive JSON-LD signal → defaulting OUT OF STOCK (False)"
    )
    return False
