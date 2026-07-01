import json
import logging
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

NEEDS_JS = True

_ADD_PATTERNS = ["add to cart", "buy now", "add to bag"]
_OOS_PATTERNS = [
    "out of stock", "sold out", "currently unavailable",
    "notify me when available", "coming soon",
]
_PRICE_CLASSES = ["pdp-price", "pd-price", "new-price", "cp-price"]
_CART_CLASSES = ["add-to-cart", "addToCart", "btn-cart", "plp-add-to-cart"]


def check(soup: BeautifulSoup, html: str) -> bool:
    html_lower = html.lower()

    # ── JSON-LD (most reliable — product-scoped) ──────────────────────────────
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

    # ── Cart button classes (positive signal) ─────────────────────────────────
    # NOTE: We intentionally do NOT scan embedded JSON for `inStock:true` here.
    # Croma product pages include related/recommended products whose JSON
    # contains `inStock:true` even when the tracked product is OOS — this was
    # the confirmed source of a false-positive alert.
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

    # NOTE: The `₹ + emi/delivery` fallback was removed — it fired on OOS pages
    # because EMI/delivery text appears in recommended-product sections.

    logger.info("[croma] no signal, defaulting OUT OF STOCK")
    return False
