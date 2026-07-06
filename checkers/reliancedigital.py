import json
import logging
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

NEEDS_JS = True

_ADD_PATTERNS = ["add to cart", "add to bag", "buy now"]
_OOS_PATTERNS = [
    "out of stock", "sold out", "currently unavailable",
    "notify me when available", "coming soon",
]

# Class tokens that mark a button/anchor as DISABLED via CSS alone, with no
# `disabled`/`aria-disabled` HTML attribute present — the "Croma lesson" (see
# checkers/croma.py's history). Reliance Digital's PDP greys out its
# "Add to Cart" button on OOS products via class styling, so without this a
# disabled button was being read as active → false in-stock. No known
# structural class collision has been observed here, so the broader "disable"
# substring is used as-is; if a production log ever shows an active button
# being misflagged, add the offending class to an explicit exclusion rather
# than narrowing this.
_DISABLED_CLASS_MARKERS = ("disable", "inactive")


def _is_disabled(el) -> bool:
    """Return True if a BS4 element is visually/semantically disabled — via
    the `disabled` attribute, `aria-disabled="true"`, or a _DISABLED_CLASS_MARKERS
    substring in its class list."""
    if el.get("disabled") is not None:
        return True
    if el.get("aria-disabled", "").lower() == "true":
        return True
    classes = " ".join(el.get("class", [])).lower()
    return any(marker in classes for marker in _DISABLED_CLASS_MARKERS)


def _offer_availability(offers) -> str:
    """
    Extract the first availability string from an 'offers' value that may be a
    single Offer dict, an AggregateOffer dict wrapping a nested offers list, or
    a plain list of Offer dicts. Reliance Digital is a marketplace, so a given
    product can carry multiple seller offers (a list) — the old
    `offers.get("availability")` raised AttributeError on a list and silently
    dropped the most reliable signal. Returns "" when none is found.
    """
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
    html_lower = html.lower()

    # ── JSON-LD ───────────────────────────────────────────────────────────────
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            for item in (data if isinstance(data, list) else [data]):
                if not isinstance(item, dict):
                    continue
                avail = _offer_availability(item.get("offers", {}))
                if "InStock" in avail:
                    logger.info("[reliancedigital] JSON-LD: InStock → True")
                    return True
                if "OutOfStock" in avail or "Discontinued" in avail:
                    logger.info("[reliancedigital] JSON-LD: OutOfStock/Discontinued → False")
                    return False
        except Exception:
            pass

    # ── Embedded JSON ─────────────────────────────────────────────────────────
    for key in ('"inStock":true', '"in_stock":true', '"isAvailable":true'):
        if key in html:
            logger.info(f"[reliancedigital] embedded JSON {key!r} → True")
            return True
    for key in ('"inStock":false', '"in_stock":false', '"isAvailable":false'):
        if key in html:
            logger.info(f"[reliancedigital] embedded JSON {key!r} → False")
            return False

    # ── Negative signals (checked BEFORE buttons — a disabled Add-to-Cart
    # button's surrounding page usually carries an unambiguous OOS text signal
    # too, and this order is the safer default even where it doesn't) ─────────
    for pattern in _OOS_PATTERNS:
        if pattern in html_lower:
            logger.info(f"[reliancedigital] OOS signal: '{pattern}' → False")
            return False

    # ── Buttons — skip disabled (attr, aria, OR class-styled) ─────────────────
    for btn in soup.find_all("button"):
        if _is_disabled(btn):
            continue
        text = btn.get_text(strip=True).lower()
        if any(p in text for p in _ADD_PATTERNS):
            logger.info(f"[reliancedigital] active button '{text[:40]}' → True")
            return True

    # ── Attrs — skip disabled ─────────────────────────────────────────────────
    for attr in ("data-testid", "aria-label", "id"):
        for el in soup.find_all(attrs={attr: True}):
            if _is_disabled(el):
                continue
            val = (el.get(attr) or "").lower()
            if "add-to-cart" in val or "addtocart" in val or any(p in val for p in _ADD_PATTERNS):
                logger.info(f"[reliancedigital] active attr {attr}='{val[:40]}' → True")
                return True

    logger.info("[reliancedigital] no conclusive signal → defaulting OUT OF STOCK (False)")
    return False
