import json
import logging
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

NEEDS_JS = True

_ADD_PATTERNS = ["add to cart", "add item", "add to bag"]
# "not available"/"item not available" removed (too broad); "notify me" narrowed
# "enabled" removed entirely — it's a feature-flag key, not a stock key
_OOS_PATTERNS = ["out of stock", "sold out", "currently unavailable", "notify me when available"]


def check(soup: BeautifulSoup, html: str) -> bool:
    html_lower = html.lower()

    # ── JSON-LD ───────────────────────────────────────────────────────────────
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            for item in (data if isinstance(data, list) else [data]):
                if not isinstance(item, dict):
                    continue
                avail = item.get("offers", {}).get("availability", "")
                if "InStock" in avail:
                    logger.info("[instamart] JSON-LD: InStock")
                    return True
                if "OutOfStock" in avail:
                    logger.info("[instamart] JSON-LD: OutOfStock")
                    return False
        except Exception:
            pass

    # ── Embedded JSON — stock-specific keys only ──────────────────────────────
    # Excluded: "available" (fires on delivery slots), "enabled" (feature flags)
    for key in ('"in_stock":true', '"inStock":true', '"is_available":true', '"isAvailable":true'):
        if key in html:
            return True
    for key in ('"in_stock":false', '"inStock":false', '"is_available":false', '"isAvailable":false'):
        if key in html:
            return False

    # ── Positive signals first ────────────────────────────────────────────────
    for btn in soup.find_all("button"):
        text = btn.get_text(strip=True).lower()
        if text in ("add", "+"):  # instamart's minimal cart button
            logger.info("[instamart] ADD/+ button found")
            return True
        if any(p in text for p in _ADD_PATTERNS):
            return True

    for attr in ("data-testid", "aria-label", "id"):
        for el in soup.find_all(attrs={attr: True}):
            val = (el.get(attr) or "").lower()
            if "add-to-cart" in val or "addtocart" in val or any(p in val for p in _ADD_PATTERNS):
                return True

    # ── Negative signals ──────────────────────────────────────────────────────
    for pattern in _OOS_PATTERNS:
        if pattern in html_lower:
            logger.info(f"[instamart] OOS signal: '{pattern}'")
            return False

    # ── Generic fallback ──────────────────────────────────────────────────────
    if "₹" in html and "delivery" in html_lower:
        return True

    logger.info("[instamart] no signal, defaulting OUT OF STOCK")
    return False
