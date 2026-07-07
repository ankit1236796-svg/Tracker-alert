import json
import logging
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# Documentation-only (the real render switch is stock_checker._JS_SITES).
# Starts render=true: the diagnostic probe confirmed JSON-LD availability is
# present and correctly differentiates OOS vs in-stock on mshop.iqoo.com, but
# whether it survives a non-rendered fetch wasn't separately confirmed, so we
# take the accuracy-first default (render=true) and can optimize to render=false
# later if a targeted test shows the JSON-LD is server-rendered (see the
# Reliance Digital / JioMart precedent in stock_checker.py).
NEEDS_JS = True

# JSON-LD is the PRIMARY, proven-reliable signal here (probe: OOS URL ->
# schema.org/OutOfStock, in-stock URL -> schema.org/InStock, clean). The
# storefront renders its Add/Buy buttons via JS (0 buttons seen in the fetched
# HTML), so there is deliberately NO button scan and NO price-presence
# fallback — an iQOO OOS page still shows the price, so treating "price present"
# as in-stock would false-positive. Absent a positive JSON-LD/embedded signal
# we default to OUT OF STOCK (safe: a missed alert, never a false one).
_OOS_PATTERNS = [
    "out of stock", "sold out", "currently unavailable",
    "notify me", "coming soon", "temporarily unavailable",
]


def _offer_availability(offers) -> str:
    """
    Extract the first availability string from an 'offers' value that may be a
    single Offer dict, an AggregateOffer dict wrapping a nested offers list, or
    a plain list of Offer dicts. Returns "" when none is found.
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


def _log_diagnostics(soup: BeautifulSoup, html: str) -> None:
    """Log the decision trail (JSON-LD availability, embedded-JSON stock keys,
    OOS text) so a page-structure change is visible in Railway logs rather than
    guessed at. Log-only: never changes the returned value."""
    html_lower = html.lower()
    logger.info(f"[iqoo][diag] HTML length={len(html)}, head={html[:200]!r}")

    found_ld = False
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except Exception:
            continue
        for item in (data if isinstance(data, list) else [data]):
            if isinstance(item, dict) and item.get("offers") is not None:
                avail = _offer_availability(item.get("offers", {}))
                if avail:
                    found_ld = True
                    logger.info(f"[iqoo][diag] JSON-LD availability={avail!r}")
    if not found_ld:
        logger.info("[iqoo][diag] JSON-LD availability: none found")

    for key in (
        '"in_stock":true', '"inStock":true', '"is_available":true', '"isAvailable":true',
        '"in_stock":false', '"inStock":false', '"is_available":false', '"isAvailable":false',
    ):
        if key in html:
            logger.info(f"[iqoo][diag] embedded JSON key present: {key!r}")

    oos_hits = [p for p in _OOS_PATTERNS if p in html_lower]
    logger.info(f"[iqoo][diag] OOS text patterns present: {oos_hits or 'none'}")


def check(soup: BeautifulSoup, html: str) -> bool:
    html_lower = html.lower()

    _log_diagnostics(soup, html)

    # ── JSON-LD (primary, proven-reliable signal) ─────────────────────────────
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
                    logger.info("[iqoo] JSON-LD: InStock → True")
                    return True
                if "OutOfStock" in avail or "Discontinued" in avail:
                    logger.info("[iqoo] JSON-LD: OutOfStock/Discontinued → False")
                    return False
        except Exception:
            pass

    # ── Embedded JSON (fallback if JSON-LD is ever absent) ────────────────────
    for key in ('"in_stock":true', '"inStock":true', '"is_available":true', '"isAvailable":true'):
        if key in html:
            logger.info(f"[iqoo] embedded JSON {key!r} → True")
            return True
    for key in ('"in_stock":false', '"inStock":false', '"is_available":false', '"isAvailable":false'):
        if key in html:
            logger.info(f"[iqoo] embedded JSON {key!r} → False")
            return False

    # ── Explicit OOS text (last-resort negative signal) ───────────────────────
    for pattern in _OOS_PATTERNS:
        if pattern in html_lower:
            logger.info(f"[iqoo] OOS text: '{pattern}' → False")
            return False

    logger.info("[iqoo] no conclusive signal → defaulting OUT OF STOCK (False)")
    return False
