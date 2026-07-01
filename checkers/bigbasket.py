import json
import logging
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

NEEDS_JS = True

_ADD_PATTERNS = ["add to cart", "add to basket", "buy now"]
_OOS_PATTERNS = ["out of stock", "sold out", "currently unavailable", "notify me when available"]

# When no delivery location is recognised, BigBasket shows a location gate.
# These signals mean the stock result would be for an unknown location —
# treat as unavailable rather than risking a false-positive alert.
_LOCATION_GATE_SIGNALS = [
    "enter your pincode",
    "enter pincode",
    "please select a delivery location",
    "select delivery location",
    "add a delivery address",
    "service not available in your area",
]


def check(soup: BeautifulSoup, html: str) -> bool:
    html_lower = html.lower()

    logger.info(f"[bigbasket] HTML length={len(html)}, first 200 chars: {html[:200]!r}")

    # ── Location gate (no delivery area set) ─────────────────────────────────
    if any(sig in html_lower for sig in _LOCATION_GATE_SIGNALS):
        logger.warning("[bigbasket] location gate detected — no delivery area set, returning OOS")
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
                    logger.info("[bigbasket] JSON-LD: InStock → True")
                    return True
                if "OutOfStock" in avail:
                    logger.info("[bigbasket] JSON-LD: OutOfStock → False")
                    return False
        except Exception:
            pass

    # ── Embedded JSON — bigbasket's own stock field ───────────────────────────
    for key in ('"in_stock": true', '"in_stock":true', '"inStock":true'):
        if key in html:
            logger.info(f"[bigbasket] embedded JSON key {key!r} → True")
            return True
    for key in ('"in_stock": false', '"in_stock":false', '"inStock":false'):
        if key in html:
            logger.info(f"[bigbasket] embedded JSON key {key!r} → False")
            return False

    # ── Positive signals first ────────────────────────────────────────────────
    for btn in soup.find_all("button"):
        text = btn.get_text(strip=True).lower()
        if text in ("add", "+"):  # bigbasket's compact cart button
            logger.info("[bigbasket] ADD/+ button found → True")
            return True
        if any(p in text for p in _ADD_PATTERNS):
            logger.info(f"[bigbasket] add pattern in button '{text[:40]}' → True")
            return True

    for attr in ("data-testid", "aria-label", "id"):
        for el in soup.find_all(attrs={attr: True}):
            val = (el.get(attr) or "").lower()
            if "add-to-cart" in val or "addtocart" in val or any(p in val for p in _ADD_PATTERNS):
                logger.info(f"[bigbasket] add pattern in {attr}='{val[:40]}' → True")
                return True

    # ── Negative signals ──────────────────────────────────────────────────────
    for pattern in _OOS_PATTERNS:
        if pattern in html_lower:
            logger.info(f"[bigbasket] OOS signal: '{pattern}' → False")
            return False

    # ── Price element (fallback — only reached if no OOS text found) ──────────
    price = soup.find(attrs={"class": lambda c: c and any("price" in cls.lower() for cls in c)})
    if price:
        logger.info("[bigbasket] price element found, no OOS signals → True")
        return True

    logger.info("[bigbasket] no signal, defaulting OUT OF STOCK")
    return False
