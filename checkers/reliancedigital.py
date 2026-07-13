import json
import logging

from bs4 import BeautifulSoup

from .common import fetch_page

logger = logging.getLogger(__name__)

# Best-guess CSS selectors for RelianceDigital's pincode-entry widget —
# NOT verified against the real site (no live network access from this
# sandbox to inspect it). Kept as top-of-file constants specifically so
# a real inspection can correct them in one place. See
# fetch_with_pincode_interaction below and admin_handlers.py's
# /debugreliance2 — the verification loop this needs to go through
# before being trusted for anything, following the same "best guess,
# verify via a debug command, tune from real results" pattern already
# used throughout this codebase's newer checkers.
_PINCODE_INPUT_SELECTOR = "input[placeholder*='incode' i]"
# Comma-separated CSS selector list — hedges across a couple of
# plausible class-name conventions for the submit/"Check" button, since
# (unlike the input, which usually has an identifying placeholder) a
# submit button has no equally reliable convention to guess from.
_PINCODE_SUBMIT_SELECTOR = "button[class*='pincode' i], button[class*='check' i]"
# Fixed wait after the click/fill/submit sequence, giving the page's own
# JS time to process the pincode change and update its DOM/state before
# Scrape.do captures the final HTML — no specific "update complete"
# selector is known to wait on instead (same unverified-guess caveat).
_PINCODE_INTERACTION_WAIT_MS = 4000

# Documentation-only (not read by any code — see stock_checker._JS_SITES for
# the actual render=true/false switch). Set to False as of the credit-cost
# pass: JSON-LD availability is this checker's primary signal and is
# expected to survive a non-rendered fetch on an SEO-invested retail catalog
# (see stock_checker.py's _JS_SITES comment for the full reasoning). Flip
# back to True if real /check results show JSON-LD/OOS text going missing
# without JS rendering.
NEEDS_JS = False

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


async def fetch_with_pincode_interaction(url: str, pincode: str = "110001") -> str:
    """
    DEBUG-ONLY — not called by check() or wired into stock_checker.py's
    live check_stock() path. Fetches url via the active scraping provider
    (render=true + super=true — see checkers.fetch_page/config.
    SCRAPING_PROVIDER), simulating a real user entering a pincode into the
    page's pincode-check widget before the final HTML is captured:
    click the pincode input -> fill it with `pincode` -> click the
    submit/check button -> wait _PINCODE_INTERACTION_WAIT_MS for the
    page's own JS to process the change.

    Uses the browser-interaction action chain (Click/Fill/Wait — see
    checkers/common.py's build_scraper_url for Scrape.do's own
    "playWithBrowser", or zyte_client.py's _translate_actions for how the
    same chain maps onto Zyte's "actions" field when that's the active
    provider). The exact CSS selectors for RelianceDigital's pincode input/
    submit button are BEST-GUESS, not verified against the real site —
    this function exists specifically so admin_handlers.py's
    /debugreliance2 can reveal whether they actually work, before
    anything here is trusted for production use.
    """
    actions = [
        {"Action": "Click", "Selector": _PINCODE_INPUT_SELECTOR},
        {"Action": "Fill", "Selector": _PINCODE_INPUT_SELECTOR, "Value": pincode},
        {"Action": "Click", "Selector": _PINCODE_SUBMIT_SELECTOR},
        {"Action": "Wait", "Timeout": _PINCODE_INTERACTION_WAIT_MS},
    ]
    resp = await fetch_page(
        url, render_js=True, super_proxy=True, play_with_browser=actions, timeout=90.0,
    )
    resp.raise_for_status()
    return resp.text


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
