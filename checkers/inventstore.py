import json
import logging

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# Documentation-only (not read by any code — see stock_checker._JS_SITES
# for the actual render=true/false switch, and
# stock_checker._EXTRA_RETRY_ON_INCOMPLETE_SITES for the "don't guess a
# verdict from a still-blocked page" retry/skip logic).
NEEDS_JS = True

_ADD_PATTERNS = ["add to cart", "add to bag", "buy now", "pre-order"]
_OOS_PATTERNS = [
    "out of stock", "sold out", "currently unavailable",
    "notify me when available", "notify me", "coming soon",
]

# Elements considered part of the product's own "buy button area" — an
# OOS/sold-out phrase only counts as authoritative when it's inside one
# of these (or a direct parent of one), not just matched anywhere on the
# page, where an unrelated section (a "related products" carousel, a
# footer policy blurb) could easily contain "out of stock" text that has
# nothing to do with THIS product.
_BUY_AREA_CLASS_HINTS = (
    "buy-box", "buybox", "product-form", "product-actions",
    "add-to-cart", "addtocart", "product-buy", "pdp-buy", "cart-form",
)

def _offer_availability(offers) -> str:
    """Extract the first availability string from a JSON-LD 'offers'
    value that may be a single Offer dict, an AggregateOffer dict
    wrapping a nested offers list, or a plain list of Offer dicts."""
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


def _visible_text(html: str) -> str:
    text_soup = BeautifulSoup(html, "html.parser")
    for tag in text_soup(["script", "style"]):
        tag.decompose()
    return text_soup.get_text(" ", strip=True)


def _buy_area_elements(soup: BeautifulSoup):
    """Yield elements considered part of the product's own buy-button
    area: every <button>/<a> tag, plus any element whose class/id matches
    a _BUY_AREA_CLASS_HINTS substring, plus each such element's immediate
    parent (to catch a "Sold Out" label sitting next to, or wrapping, a
    disabled button — e.g.
    <div class="buy-box"><button disabled>Notify Me</button></div>)."""
    seen: set[int] = set()
    candidates = list(soup.find_all(["button", "a"]))
    for el in soup.find_all(True):
        attrs_text = " ".join(el.get("class", [])) + " " + (el.get("id") or "")
        if any(hint in attrs_text.lower() for hint in _BUY_AREA_CLASS_HINTS):
            candidates.append(el)

    for el in candidates:
        if id(el) not in seen:
            seen.add(id(el))
            yield el
        parent = el.parent
        if parent is not None and id(parent) not in seen:
            seen.add(id(parent))
            yield parent


def check(soup: BeautifulSoup, html: str) -> bool:
    """
    inventstore.in's own stock-detection waterfall. Real /check results
    showed the previous shared checkers.common.generic_marketplace_check()
    waterfall (OOS text checked BEFORE buttons, and matched anywhere in
    the raw HTML) misreading this site: a confirmed-working, genuinely
    in-stock page shows "Buy Now" clearly with no "out of stock" text
    anywhere, yet the OOS-first, unscoped-match ordering could still be
    thrown off by an unrelated mention elsewhere on the page (e.g. a
    "related products" section referencing a DIFFERENT, actually-OOS
    item). Two changes from the shared waterfall:

    1. Buy Now / Add to Cart presence is now the PRIMARY signal, checked
       BEFORE any OOS text (not after) — a positive purchase affordance
       is trusted immediately rather than being second-guessed by
       scanning for a negative signal first.
    2. "out of stock"/"sold out" is only treated as authoritative when it
       appears within the product's own buy-button area (see
       _buy_area_elements) — a page-wide/unscoped match is no longer
       trusted at all, so a stray mention elsewhere on the page can't
       produce a false OOS read for this product.

    JSON-LD and embedded-JSON stock signals are kept as the highest-
    priority checks (unchanged) — they're structured, not free text.
    Defaults to out of stock when no signal is found at all, per this
    codebase's standing principle that a missed alert is safer than a
    false one.
    """
    # ── JSON-LD ──────────────────────────────────────────────────────────
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except Exception:
            continue
        for item in (data if isinstance(data, list) else [data]):
            if not isinstance(item, dict):
                continue
            avail = _offer_availability(item.get("offers", {}))
            if "InStock" in avail:
                logger.info("[inventstore] JSON-LD: InStock → True")
                return True
            if "OutOfStock" in avail or "Discontinued" in avail:
                logger.info("[inventstore] JSON-LD: OutOfStock/Discontinued → False")
                return False

    # ── Embedded JSON ────────────────────────────────────────────────────
    for key in ('"inStock":true', '"in_stock":true', '"isAvailable":true', '"available":true'):
        if key in html:
            logger.info(f"[inventstore] embedded JSON {key!r} → True")
            return True
    for key in ('"inStock":false', '"in_stock":false', '"isAvailable":false', '"available":false'):
        if key in html:
            logger.info(f"[inventstore] embedded JSON {key!r} → False")
            return False

    # ── PRIMARY signal: Buy Now / Add to Cart presence, checked BEFORE
    # any OOS text ─────────────────────────────────────────────────────
    visible_text = _visible_text(html).lower()
    if any(p in visible_text for p in _ADD_PATTERNS):
        logger.info("[inventstore] Buy Now/Add to Cart text found → True (in stock)")
        return True

    # ── OOS text — authoritative ONLY within the buy-button area. Every
    # candidate element is scanned regardless of disabled state (unlike
    # the positive ADD_PATTERNS check above) — a disabled "Notify Me"
    # button's own text IS the OOS signal here, not something to skip. ──
    for el in _buy_area_elements(soup):
        el_text = el.get_text(" ", strip=True).lower()
        if any(pattern in el_text for pattern in _OOS_PATTERNS):
            logger.info("[inventstore] OOS text found within the buy-button area → False")
            return False

    logger.info("[inventstore] no conclusive signal → defaulting OUT OF STOCK (False)")
    return False
