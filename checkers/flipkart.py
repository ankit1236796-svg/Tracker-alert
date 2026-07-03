import json
import logging
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# See stock_checker.py's _JS_SITES for why this is False — empirically
# verified that render=false reproduces render=true's check() result.
NEEDS_JS = False

_PRICE_CLASSES = [
    "_30jeq3", "Nx9bqj", "_25b18c", "_16Jk6d", "CxhGGd",
    "hl05eU", "_4b5DiR", "x+jhYQ", "yRaY8j", "_1vC4OE",
]
_ADD_PATTERNS = ["add to cart", "add to bag", "buy now", "proceed to buy"]
_OOS_PATTERNS = ["sold out", "currently unavailable", "notify me when available"]

# Delivery restriction phrases — ScraperAPI's server IP may be outside coverage
# zone, causing Flipkart to show this instead of the real add-to-cart button.
# These are NOT out-of-stock signals; suppress OOS inference when detected.
_DELIVERY_RESTRICTION_PHRASES = [
    "not deliverable in your location",
    "not serviceable in your location",
    "delivery not available",
    "currently not available in your location",
]


def _parse_offers_availability(offers) -> str | None:
    """
    Return the schema:availability string from a JSON-LD `offers` value.
    Handles three shapes Flipkart uses:
      - Single Offer dict:       {"@type": "Offer", "availability": "..."}
      - AggregateOffer dict:     {"@type": "AggregateOffer", "offers": [...]}
      - List of Offer dicts:     [{"@type": "Offer", ...}, ...]
    Returns the first availability string found, or None.
    """
    if isinstance(offers, list):
        for o in offers:
            avail = _parse_offers_availability(o)
            if avail:
                return avail
        return None

    if not isinstance(offers, dict):
        return None

    offer_type = offers.get("@type", "")

    if offer_type == "AggregateOffer":
        nested = offers.get("offers", [])
        return _parse_offers_availability(nested)

    return offers.get("availability") or None


def check(soup: BeautifulSoup, html: str) -> bool:
    html_lower = html.lower()

    # Detect delivery restriction early — Flipkart shows this when ScraperAPI's
    # IP is outside the coverage zone. It is NOT an OOS signal.
    delivery_restricted = any(p in html_lower for p in _DELIVERY_RESTRICTION_PHRASES)
    if delivery_restricted:
        logger.info("[flipkart] delivery restriction detected — suppressing OOS inference")

    # ── JSON-LD (most reliable) ───────────────────────────────────────────────
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            for item in (data if isinstance(data, list) else [data]):
                if not isinstance(item, dict):
                    continue
                avail = _parse_offers_availability(item.get("offers"))
                if avail is None:
                    continue
                if "InStock" in avail:
                    logger.info("[flipkart] JSON-LD: InStock")
                    return True
                if "OutOfStock" in avail or "Discontinued" in avail:
                    logger.info("[flipkart] JSON-LD: OutOfStock")
                    return False
        except Exception:
            pass

    # ── Positive signals (buttons trump OOS text in other page sections) ──────
    for btn in soup.find_all("button"):
        if any(p in btn.get_text(strip=True).lower() for p in _ADD_PATTERNS):
            logger.info("[flipkart] add-to-cart button found")
            return True

    # Flipkart uses <a> anchors as buy buttons on variant/bundle pages
    for a in soup.find_all("a"):
        text = a.get_text(strip=True).lower()
        href = (a.get("href") or "").lower()
        role = (a.get("role") or "").lower()
        if any(p in text for p in _ADD_PATTERNS):
            if "checkout" in href or "/cart" in href or role == "button":
                logger.info("[flipkart] add-to-cart <a> link found")
                return True

    for attr in ("data-testid", "aria-label", "id"):
        for el in soup.find_all(attrs={attr: True}):
            val = (el.get(attr) or "").lower()
            if any(p in val for p in _ADD_PATTERNS):
                return True

    # ── Negative signals (skip if delivery-restricted; those aren't OOS) ──────
    if not delivery_restricted:
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

    # Price present with delivery/pincode info = product exists, purchasable
    if "₹" in html and ("pincode" in html_lower or "delivery" in html_lower):
        return True

    logger.info("[flipkart] no signal, defaulting OUT OF STOCK")
    return False
