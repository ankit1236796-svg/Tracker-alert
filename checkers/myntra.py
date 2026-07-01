import json
import logging
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

NEEDS_JS = True

_ADD_PATTERNS = ["add to bag", "add to cart", "buy now"]
# "size not available" removed (only some sizes OOS ≠ product OOS; button check handles it)
# "notify me" narrowed to "notify me when available"
_OOS_PATTERNS = ["sold out", "out of stock", "currently out of stock", "notify me when available"]
_PRICE_CLASSES = ["pdp-price", "product-discountedPrice", "pdp-mrp"]
_CART_CLASSES = ["pdp-add-to-bag", "add-to-bag", "addToBag"]


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
                    logger.info("[myntra] JSON-LD: InStock")
                    return True
                if "OutOfStock" in avail:
                    logger.info("[myntra] JSON-LD: OutOfStock")
                    return False
        except Exception:
            pass

    # ── Embedded JSON — stock-specific keys only ──────────────────────────────
    # Excluded: "available" (too broad — fires on payment methods, slots, etc.)
    for key in ('"inStock":true', '"in_stock":true', '"sizes_available":true'):
        if key in html:
            return True
    for key in ('"inStock":false', '"in_stock":false', '"sizes_available":false'):
        if key in html:
            return False

    # ── Cart button classes (strong positive) ─────────────────────────────────
    for cls in _CART_CLASSES:
        if soup.find(attrs={"class": lambda c: c and cls in " ".join(c)}):
            logger.info(f"[myntra] cart class '{cls}' found")
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

    # ── Negative signals (only reached if no positive signal above) ───────────
    for pattern in _OOS_PATTERNS:
        if pattern in html_lower:
            logger.info(f"[myntra] OOS signal: '{pattern}'")
            return False

    # ── Price classes (fallback — only reached if no OOS text) ───────────────
    for cls in _PRICE_CLASSES:
        if soup.find(attrs={"class": lambda c: c and cls in " ".join(c)}):
            return True

    if "₹" in html and ("size" in html_lower or "delivery" in html_lower):
        return True

    logger.info("[myntra] no signal, defaulting OUT OF STOCK")
    return False
