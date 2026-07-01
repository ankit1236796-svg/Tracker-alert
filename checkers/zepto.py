import json
import logging
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

NEEDS_JS = True

_ADD_PATTERNS = ["add to cart", "add to bag"]
_OOS_PATTERNS = ["out of stock", "sold out", "notify me when available"]

# Zepto uses a storeId (derived from exact coordinates) for availability.
# A simple pincode cookie is insufficient — true pincode-specific checking
# requires a storeId lookup call. Until that is implemented, results reflect
# Scrape.do's IP geolocation. These signals detect a location gate page so
# we don't send a false-positive alert when no location was resolved.
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

    # ── JSON-LD ───────────────────────────────────────────────────────────────
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

    # ── Embedded JSON — stock-specific keys only ──────────────────────────────
    for key in ('"in_stock":true', '"inStock":true', '"is_available":true'):
        if key in html:
            return True
    for key in ('"in_stock":false', '"inStock":false', '"is_available":false'):
        if key in html:
            return False

    # ── Positive signals first ────────────────────────────────────────────────
    for btn in soup.find_all("button"):
        text = btn.get_text(strip=True).lower()
        if text in ("add", "+"):  # zepto's minimal cart button
            logger.info("[zepto] ADD/+ button found")
            return True
        if any(p in text for p in _ADD_PATTERNS):
            return True

    for attr in ("data-testid", "aria-label"):
        for el in soup.find_all(attrs={attr: True}):
            val = (el.get(attr) or "").lower()
            if "add-to-cart" in val or "addtocart" in val or any(p in val for p in _ADD_PATTERNS):
                return True

    # ── Negative signals ──────────────────────────────────────────────────────
    for pattern in _OOS_PATTERNS:
        if pattern in html_lower:
            logger.info(f"[zepto] OOS signal: '{pattern}'")
            return False

    logger.info("[zepto] no signal, defaulting OUT OF STOCK")
    return False
