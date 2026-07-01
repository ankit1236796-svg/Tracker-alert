import json
import logging
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

NEEDS_JS = True

_ADD_PATTERNS = ["add to cart", "add to bag"]
_OOS_PATTERNS = ["out of stock", "sold out", "notify me when available", "notify me"]

# Zepto uses a storeId (derived from exact coordinates) for availability.
# A simple pincode cookie is insufficient — true pincode-specific checking
# requires a storeId lookup call. Until that is implemented, results reflect
# Scrape.do's IP geolocation.
_LOCATION_GATE_SIGNALS = [
    "enter your pincode",
    "enter pincode",
    "select your location",
    "please enter your location",
    "add a delivery address",
]


def check(soup: BeautifulSoup, html: str) -> bool:
    html_lower = html.lower()

    # ── Location gate (no delivery area resolved) ─────────────────────────────
    if any(sig in html_lower for sig in _LOCATION_GATE_SIGNALS):
        logger.warning(
            "[zepto] location gate detected — pincode-specific stock unavailable "
            "(Zepto requires storeId); returning OOS"
        )
        return False

    # ── JSON-LD (most reliable — scoped to the specific product) ─────────────
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            for item in (data if isinstance(data, list) else [data]):
                if not isinstance(item, dict):
                    continue
                avail = item.get("offers", {}).get("availability", "")
                if "InStock" in avail:
                    logger.info("[zepto] JSON-LD: InStock")
                    return True
                if "OutOfStock" in avail:
                    logger.info("[zepto] JSON-LD: OutOfStock")
                    return False
        except Exception:
            pass

    # ── Embedded JSON — intentionally limited to unambiguous keys ────────────
    # NOTE: "is_available" is deliberately excluded — Zepto uses this field on
    # store and delivery-slot objects that appear on EVERY page, including OOS
    # product pages. Scanning for it causes false positives when a store or slot
    # is available even though the specific product is out of stock.
    # NOTE: "in_stock" is also excluded for the same reason — Zepto product pages
    # include related/recommended product cards that embed "in_stock":true even
    # when the tracked product is OOS.  JSON-LD above is the reliable signal.

    # ── Positive signals: "Add to Cart" / "Add to Bag" button ────────────────
    # Zepto shows "Notify Me" (not Add to Cart) when OOS — so if we find an
    # actual add-to-cart button or link, the product is in stock.
    for btn in soup.find_all("button"):
        text = btn.get_text(strip=True).lower()
        if text in ("add", "+"):  # zepto's minimal cart button
            if btn.get("disabled") is None:
                logger.info("[zepto] ADD/+ button found")
                return True
        if any(p in text for p in _ADD_PATTERNS):
            if btn.get("disabled") is None:
                return True

    for attr in ("data-testid", "aria-label"):
        for el in soup.find_all(attrs={attr: True}):
            val = (el.get(attr) or "").lower()
            if "add-to-cart" in val or "addtocart" in val or any(p in val for p in _ADD_PATTERNS):
                if el.get("disabled") is None and el.get("aria-disabled", "") != "true":
                    return True

    # ── Negative signals ──────────────────────────────────────────────────────
    for pattern in _OOS_PATTERNS:
        if pattern in html_lower:
            logger.info(f"[zepto] OOS signal: '{pattern}'")
            return False

    logger.info("[zepto] no signal, defaulting OUT OF STOCK")
    return False
