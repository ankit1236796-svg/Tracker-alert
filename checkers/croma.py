import json
import logging
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

NEEDS_JS = True

_ADD_PATTERNS = ["add to cart", "buy now", "add to bag"]
# "not available" removed (too broad); "notify me" narrowed
_OOS_PATTERNS = [
    "out of stock", "sold out", "currently unavailable",
    "notify me when available", "coming soon",
]
_PRICE_CLASSES = ["pdp-price", "pd-price", "new-price", "cp-price"]
_CART_CLASSES = ["add-to-cart", "addToCart", "btn-cart", "plp-add-to-cart"]


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
                    logger.info("[croma] JSON-LD: InStock")
                    return True
                if "OutOfStock" in avail or "Discontinued" in avail:
                    logger.info("[croma] JSON-LD: OutOfStock")
                    return False
        except Exception:
            pass

    # ── Embedded JSON — inStock keys only ────────────────────────────────────
    # Excluded: "available" (fires on unrelated objects like payment/slot availability)
    for key in ('"inStock":true', '"in_stock":true', '"isInStock":true'):
        if key in html:
            return True
    for key in ('"inStock":false', '"in_stock":false', '"isInStock":false'):
        if key in html:
            return False

    # ── Cart button classes (positive signal) ─────────────────────────────────
    for cls in _CART_CLASSES:
        if soup.find(attrs={"class": lambda c: c and cls.lower() in " ".join(c).lower()}):
            logger.info(f"[croma] cart class '{cls}' found")
            return True

    # ── Buttons ───────────────────────────────────────────────────────────────
    for btn in soup.find_all("button"):
        if any(p in btn.get_text(strip=True).lower() for p in _ADD_PATTERNS):
            return True

    # ── Attrs ─────────────────────────────────────────────────────────────────
    for attr in ("data-testid", "aria-label", "id"):
        for el in soup.find_all(attrs={attr: True}):
            val = (el.get(attr) or "").lower()
            if any(p in val for p in _ADD_PATTERNS):
                return True

    # ── Negative signals ──────────────────────────────────────────────────────
    for pattern in _OOS_PATTERNS:
        if pattern in html_lower:
            logger.info(f"[croma] OOS signal: '{pattern}'")
            return False

    # ── Price classes (fallback — only reached if no OOS text) ───────────────
    for cls in _PRICE_CLASSES:
        if soup.find(attrs={"class": lambda c: c and cls in " ".join(c)}):
            return True

    if "₹" in html and ("emi" in html_lower or "delivery" in html_lower):
        return True

    logger.info("[croma] no signal, defaulting OUT OF STOCK")
    return False
