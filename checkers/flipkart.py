import json
import logging
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

NEEDS_JS = True

_PRICE_CLASSES = [
    "_30jeq3", "Nx9bqj", "_25b18c", "_16Jk6d", "CxhGGd",
    "hl05eU", "_4b5DiR", "x+jhYQ", "yRaY8j", "_1vC4OE",
]
_ADD_PATTERNS = ["add to cart", "add to bag", "buy now"]
_OOS_PATTERNS = ["sold out", "currently unavailable", "notify me when available"]


def check(soup: BeautifulSoup, html: str) -> bool:
    html_lower = html.lower()

    # ── JSON-LD (most reliable) ───────────────────────────────────────────────
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            for item in (data if isinstance(data, list) else [data]):
                if not isinstance(item, dict):
                    continue
                avail = item.get("offers", {}).get("availability", "")
                if "InStock" in avail:
                    logger.info("[flipkart] JSON-LD: InStock")
                    return True
                if "OutOfStock" in avail or "Discontinued" in avail:
                    logger.info("[flipkart] JSON-LD: OutOfStock")
                    return False
        except Exception:
            pass

    # ── Positive signals first (buttons trump OOS text in other page sections) ─
    for btn in soup.find_all("button"):
        if any(p in btn.get_text(strip=True).lower() for p in _ADD_PATTERNS):
            logger.info("[flipkart] add-to-cart button found")
            return True

    for attr in ("data-testid", "aria-label", "id"):
        for el in soup.find_all(attrs={attr: True}):
            val = (el.get(attr) or "").lower()
            if any(p in val for p in _ADD_PATTERNS):
                return True

    # ── Negative signals (only reached if no positive button/attr found) ───────
    for pattern in _OOS_PATTERNS:
        if pattern in html_lower:
            logger.info(f"[flipkart] OOS signal: '{pattern}'")
            return False

    if "out of stock" in html_lower:
        logger.info("[flipkart] 'out of stock' found")
        return False

    # ── Price classes (weak positive — only reached if no OOS text) ───────────
    for cls in _PRICE_CLASSES:
        if soup.find(["div", "span"], {"class": cls}):
            return True

    if "₹" in html and ("pincode" in html_lower or "delivery" in html_lower):
        return True

    logger.info("[flipkart] no signal, defaulting OUT OF STOCK")
    return False
