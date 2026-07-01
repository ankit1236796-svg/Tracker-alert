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
# "btn-cart" removed — it matched Croma's persistent header cart icon on every page,
# causing false positives once the lambda was using correct BS4 class membership.
# "addToCart" kept — specific enough as an exact class name.
_CART_CLASSES = ["add-to-cart", "addToCart", "plp-add-to-cart"]


def _is_disabled(el) -> bool:
    """Return True if a BS4 element is visually/semantically disabled."""
    if el.get("disabled") is not None:
        return True
    if el.get("aria-disabled", "").lower() == "true":
        return True
    classes = " ".join(el.get("class", [])).lower()
    return "disabled" in classes or "inactive" in classes


def _offer_availability(offers) -> str:
    """
    Extract the first availability string from an 'offers' value that may be:
      • a single Offer dict     {"availability": "https://schema.org/InStock"}
      • an AggregateOffer dict  {"offers": [{"availability": "..."}], ...}
      • a list of Offer dicts   [{"availability": "..."}, ...]
    Returns "" when no availability can be found.
    """
    if isinstance(offers, dict):
        avail = offers.get("availability", "")
        if avail:
            return str(avail)
        # AggregateOffer: availability lives in the nested offers list
        nested = offers.get("offers", [])
        if isinstance(nested, list):
            for o in nested:
                if isinstance(o, dict):
                    a = o.get("availability", "")
                    if a:
                        return str(a)
        elif isinstance(nested, dict):
            a = nested.get("availability", "")
            if a:
                return str(a)
    elif isinstance(offers, list):
        for o in offers:
            if isinstance(o, dict):
                a = o.get("availability", "")
                if a:
                    return str(a)
    return ""


def check(soup: BeautifulSoup, html: str) -> bool:
    html_lower = html.lower()

    # ── JSON-LD pass — OutOfStock trusted immediately; InStock deferred ────────
    # Croma's structured data has been observed returning InStock for products
    # that are actually out of stock (stale / incorrect data). Trusting it
    # immediately caused every product to appear in-stock.
    # Strategy: return False on OutOfStock right away (reliable negative signal),
    # but hold any InStock signal and only confirm it after OOS text patterns
    # have had a chance to contradict it.
    json_ld_in_stock = False
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            for item in (data if isinstance(data, list) else [data]):
                if not isinstance(item, dict):
                    continue
                avail = _offer_availability(item.get("offers", {}))
                if not avail:
                    continue
                if "InStock" in avail:
                    logger.info("[croma] JSON-LD: InStock (deferred — checking OOS text first)")
                    json_ld_in_stock = True
                elif "OutOfStock" in avail or "Discontinued" in avail:
                    logger.info("[croma] JSON-LD: OutOfStock/Discontinued → False")
                    return False
        except Exception:
            pass

    # ── OOS text patterns ─────────────────────────────────────────────────────
    for pattern in _OOS_PATTERNS:
        if pattern in html_lower:
            logger.info(f"[croma] OOS text: '{pattern}' → False")
            return False

    # ── JSON-LD InStock confirmed (OOS text did not contradict it) ────────────
    if json_ld_in_stock:
        logger.info("[croma] JSON-LD InStock confirmed (no OOS text) → True")
        return True

    # ── Cart button classes (exact class membership via BS4 class_= filter) ───
    # NOTE: Previously used attrs={"class": lambda c: cls in " ".join(c)} which
    # is BROKEN — BS4 passes individual class strings to the lambda, so
    # " ".join(str) character-joins rather than word-joins. Use class_=cls
    # instead, which BS4 correctly resolves to exact class-membership testing.
    for cls in _CART_CLASSES:
        for el in soup.find_all(class_=cls):
            if _is_disabled(el):
                logger.info(f"[croma] class '{cls}' on <{el.name}> is disabled — skipping")
                continue
            logger.info(f"[croma] active class '{cls}' on <{el.name}> → True")
            return True

    # ── Buttons — skip disabled ────────────────────────────────────────────────
    for btn in soup.find_all("button"):
        if _is_disabled(btn):
            continue
        text = btn.get_text(strip=True).lower()
        if any(p in text for p in _ADD_PATTERNS):
            logger.info(f"[croma] active button '{text[:40]}' → True")
            return True

    # ── Attribute checks ──────────────────────────────────────────────────────
    for attr in ("data-testid", "aria-label", "id"):
        for el in soup.find_all(attrs={attr: True}):
            if _is_disabled(el):
                continue
            val = (el.get(attr) or "").lower()
            if any(p in val for p in _ADD_PATTERNS):
                logger.info(f"[croma] active {attr}='{val[:40]}' → True")
                return True

    logger.info("[croma] no conclusive signal → False")
    return False
