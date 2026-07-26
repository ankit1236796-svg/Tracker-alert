"""
checkers/reliancedigital.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~
As of the article_id migration, the PRODUCTION stock-check path is
check_via_api() further down this file — RelianceDigital's own internal
inventory API, called directly (no Zyte/Scrape.do), the same pattern
checkers/croma.py established. Requires an article_id, auto-extracted
ONCE at /add time (fetch_and_extract_article_id) and persisted on the
product's own row (database.products.reliance_article_id) rather than
re-scraped every cycle. See stock_checker.py's new site=="reliancedigital"
special case.

check()/fetch_with_pincode_interaction() below — the OLD page-scraping
checker (Zyte/Scrape.do, JSON-LD/button-text based) — are UNCHANGED and
still fully present, but no longer called from stock_checker.check_stock()
for reliancedigital. Kept intentionally (not deleted) since whether a
no-article-id product should fall back to page-scraping is an open
question, not decided as part of this migration.
"""

import json
import logging
import re

import httpx
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
        site="reliancedigital",
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


# ═══════════════════════════════════════════════════════════════════════════
# Direct-API checker (article_id -> inventory API), replacing the
# page-scraping check() above for products that have a successfully
# extracted article_id. article_id is auto-extracted ONCE, at /add time
# (see fetch_and_extract_article_id below and handlers.py's
# _resolve_reliance_article_id), stored on the product's own row
# (database.products.reliance_article_id), and looked up by URL for the
# recurring check (database.get_reliance_article_id_for_url) — NOT
# re-derived from the URL on every call the way checkers/croma.py's
# itemID is, because RelianceDigital's article_id isn't embedded in the
# URL at all; it only exists on the live page.
#
# check() above is UNCHANGED and still fully present — it's simply no
# longer called from stock_checker.check_stock() for reliancedigital once
# this ships (see that module's new site=="reliancedigital" branch). Kept
# intentionally, not deleted, in case a page-scraping fallback for
# no-article-id products is wanted later — that's an explicit open
# question, not decided here.
# ═══════════════════════════════════════════════════════════════════════════

# Best-guess extraction of RelianceDigital's internal "article_id" from a
# live product page — ported from a reference cheerio/JS implementation,
# NOT independently verified against a real reliancedigital.in page from
# this sandbox (no live network access). Two methods, tried in order:
#   1. The specifications list's "Item Code" row (primary — most direct).
#   2. A 9-digit number embedded in the og:image meta tag's URL, matching
#      "-<9 digits>-i-1" immediately before that suffix (fallback, used
#      only when method 1 finds nothing).
# See admin_handlers.py's /debugreliancedigital — the verification loop
# this needs before being fully trusted, following the same "best guess,
# verify via a debug command, tune from real results" pattern already
# used throughout this codebase's newer checkers (e.g.
# checkers/reliancedigital.py's own _PINCODE_INPUT_SELECTOR above).
_ARTICLE_ID_OG_IMAGE_PATTERN = re.compile(r"-(\d{9})-i-1")


def extract_article_id(soup: BeautifulSoup) -> tuple[str | None, str | None]:
    """
    Returns (article_id, method) from an already-parsed product page:
      method == "item_code" — found via the "Item Code" specifications row.
      method == "og_image"  — found via the og:image URL regex fallback.
      (None, None)          — neither method found anything.
    """
    for li in soup.select("li.specifications-list"):
        first_span = li.find("span")
        if first_span and first_span.get_text(strip=True) == "Item Code":
            right = li.select_one(".specifications-list--right ul")
            if right:
                value = right.get_text(strip=True)
                if value:
                    return value, "item_code"

    og_image = soup.find("meta", attrs={"property": "og:image"})
    content = og_image.get("content") if og_image else None
    if content:
        m = _ARTICLE_ID_OG_IMAGE_PATTERN.search(content)
        if m:
            return m.group(1), "og_image"

    return None, None


async def fetch_and_extract_article_id(url: str) -> tuple[str | None, str | None]:
    """
    One-time, /add-time fetch of `url` + extract_article_id() above.
    Returns (article_id, method) — (None, None) on any failure. Never
    raises.

    Goes through checkers.common.fetch_page with super_proxy=True — NOT a
    plain/direct request. RelianceDigital blocks Railway's outbound IP at
    the Akamai WAF edge (confirmed via /debugreliance and
    playwright_scraper/README.md's own diagnostics: a plain/unproxied
    fetch gets HTTP 403 before real page content is even served), and
    super_proxy=True is this codebase's own established default for
    getting past that specific block (see /debugreliance's "default"
    tier). render_js is left False: Item Code / og:image are both
    server-rendered, SEO-relevant content — same reasoning as this
    module's NEEDS_JS=False for check() above — so a non-rendered fetch
    is expected to be sufficient and cheaper.

    Costs real Zyte/Scrape.do credits (tracked under site="reliancedigital"
    in /creditusage, same as any other fetch_page call) — but only ONCE
    per product URL ever successfully extracted (see
    database.get_reliance_article_id_for_url, which callers should check
    FIRST to reuse another user's already-extracted id for the identical
    URL rather than calling this again), not on every recurring check
    like the old page-scraping checker this replaces.
    """
    logger.info(f"[reliancedigital][extract] fetching {url!r} (super_proxy=True) for article_id extraction")
    try:
        resp = await fetch_page(url, render_js=False, super_proxy=True, timeout=60.0, site="reliancedigital")
    except Exception as exc:
        logger.warning(f"[reliancedigital][extract] fetch failed for {url!r}: {type(exc).__name__}: {exc}")
        return None, None

    if resp.status_code != 200:
        logger.warning(
            f"[reliancedigital][extract] HTTP {resp.status_code} for {url!r} — "
            f"cannot extract article_id from a non-200 response"
        )
        return None, None

    try:
        soup = BeautifulSoup(resp.text, "html.parser")
        article_id, method = extract_article_id(soup)
    except Exception as exc:
        logger.warning(f"[reliancedigital][extract] parsing failed for {url!r}: {type(exc).__name__}: {exc}")
        return None, None

    if article_id:
        logger.info(f"[reliancedigital][extract] {url!r} → article_id={article_id!r} (method={method})")
    else:
        logger.warning(
            f"[reliancedigital][extract] could not extract an article_id from {url!r} "
            f"via either method (Item Code spec field or og:image regex)"
        )
    return article_id, method


_INVENTORY_URL = "https://www.reliancedigital.in/ext/raven-api/inventory/multi/articles-v2"
_INVENTORY_TIMEOUT = 15.0
# Confirmed OOS signals per the reference implementation — anything else in
# error.type is unrecognized and treated as inconclusive (None) rather than
# guessed, per this codebase's standing "don't guess on an unverified
# response shape" principle (see checkers/croma.py's _promise_lines and its
# own module docstring for the same reasoning).
_OUT_OF_STOCK_ERROR_TYPES = frozenset({"OutOfStockError", "FaultyArticleError"})


def _first_article_error_type(data: dict) -> str | None:
    """
    Defensively walks data -> data -> articles[0] -> error -> type,
    tolerating any level being missing/None/an unexpected shape rather
    than assuming a fixed structure and crashing — mirrors
    checkers/croma.py's _promise_lines for the identical reasoning (an
    unverified third-party JSON shape supplied as a reference
    implementation, not independently confirmed against a live response
    from this sandbox).
    """
    outer = data.get("data") if isinstance(data, dict) else None
    if not isinstance(outer, dict):
        return None
    articles = outer.get("articles")
    if not isinstance(articles, list) or not articles:
        return None
    first = articles[0]
    if not isinstance(first, dict):
        return None
    error = first.get("error")
    if not isinstance(error, dict):
        return None
    error_type = error.get("type")
    return str(error_type) if error_type else None


async def check_via_api(article_id: str, pincode: str | None) -> bool | None:
    """
    Sole production stock-check path for RelianceDigital, via its own
    internal inventory API (see this module's docstring section above for
    the endpoint/payload/response details). Returns:
      True  - a 200 response with no error, or an error.type outside
              _OUT_OF_STOCK_ERROR_TYPES: available at this pincode.
      False - error.type is OutOfStockError or FaultyArticleError: a
              genuine, confirmed answer from a real API response — not a
              guess.
      None  - inconclusive: no pincode available, network error/timeout,
              non-200, non-JSON response, or an UNRECOGNIZED error.type
              (not confirmed as an OOS signal — see
              _first_article_error_type's note). Never raises; the caller
              (stock_checker.check_stock()) must treat None as "skip this
              update", the same convention checkers/croma.py's
              check_via_api uses.

    Takes `article_id` directly (NOT `url`, unlike checkers/croma.py's
    check_via_api) — RelianceDigital's article_id isn't embedded in the
    URL at all, so it can't be re-derived here; the caller must already
    have looked it up (database.get_reliance_article_id_for_url) before
    calling this.

    Does NOT go through checkers.common.fetch_page — a direct httpx call,
    same as checkers/croma.py's check_via_api, since this is
    RelianceDigital's own inventory endpoint (apparently unauthenticated —
    no API key/token in the reference headers, unlike Croma's
    oms-apim-subscription-key), not a scrape needing Zyte/Scrape.do.
    Consequently this spends ZERO Zyte/Scrape.do credits on the recurring
    check and is automatically absent from
    database.get_zyte_usage_summary's per-site breakdown (that table is
    only ever written to from inside zyte_client.fetch_page) — no
    separate credit-tracking exclusion needed, mirrors checkers/croma.py's
    own module docstring on this exact point.

    NOT independently verified against a live reliancedigital.in response
    from this sandbox (no live network access) — endpoint/payload/
    response-path supplied directly as a reference implementation. Flag
    via /debugreliancedigital if a real response doesn't match this shape.
    """
    if not pincode:
        logger.warning(
            "[reliancedigital] no pincode set for this user — the inventory "
            "API requires a real delivery pincode, cannot check without "
            "one. Use /pins to add one."
        )
        return None

    headers = {
        "accept": "application/json, text/plain, */*",
        "content-type": "application/json",
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36"
        ),
        "origin": "https://www.reliancedigital.in",
        "referer": "https://www.reliancedigital.in/",
    }
    payload = {
        "articles": [{"article_id": str(article_id), "custom_json": {}, "quantity": 1}],
        "phone_number": "0",
        "pincode": str(pincode),
        "request_page": "pdp",
    }

    logger.info(f"[reliancedigital] POST {_INVENTORY_URL} article_id={article_id!r} pincode={pincode!r}")
    try:
        async with httpx.AsyncClient(timeout=_INVENTORY_TIMEOUT) as client:
            resp = await client.post(_INVENTORY_URL, headers=headers, json=payload)
    except Exception as exc:
        logger.warning(f"[reliancedigital] inventory API request failed: {type(exc).__name__}: {exc}")
        return None

    logger.info(f"[reliancedigital] inventory API status={resp.status_code}")
    if resp.status_code != 200:
        logger.warning(f"[reliancedigital] inventory API HTTP {resp.status_code}: {resp.text[:300]!r}")
        return None

    try:
        data = resp.json()
    except Exception:
        logger.warning(f"[reliancedigital] inventory API non-JSON response: {resp.text[:300]!r}")
        return None

    error_type = _first_article_error_type(data)
    if error_type is None:
        in_stock = True
    elif error_type in _OUT_OF_STOCK_ERROR_TYPES:
        in_stock = False
    else:
        logger.warning(
            f"[reliancedigital] unrecognized error.type={error_type!r} for "
            f"article_id={article_id!r} — treating as inconclusive rather "
            f"than guessing true/false (only OutOfStockError/"
            f"FaultyArticleError are confirmed OOS signals here)"
        )
        return None

    logger.info(
        f"[reliancedigital] article_id={article_id!r} pincode={pincode!r} → "
        f"{'IN STOCK' if in_stock else 'OUT OF STOCK'} (error.type={error_type!r})"
    )
    return in_stock
