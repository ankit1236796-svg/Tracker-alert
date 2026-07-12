"""Shared utilities for all site checkers."""

import json
import logging
import os
from urllib.parse import urlparse, urlencode

from config import SUPPORTED_SITES

logger = logging.getLogger(__name__)

SCRAPEDO_API_URL = "https://api.scrape.do/"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-IN,en;q=0.9",
}


def detect_site(url: str) -> str | None:
    host = urlparse(url).netloc.lower().replace("www.", "")
    for site_key, domains in SUPPORTED_SITES.items():
        for domain in domains:
            if host == domain or host.endswith("." + domain):
                return site_key
    return None


def build_scraper_url(
    url: str,
    render_js: bool = False,
    set_cookies: str | None = None,
    custom_headers: bool = False,
    wait_until: str | None = None,
    custom_wait_ms: int | None = None,
    super_proxy: bool = False,
) -> str:
    # Read at call time so Railway's runtime env var is always used,
    # regardless of when this module was first imported.
    token = os.environ.get("SCRAPEDO_KEY", "")
    params = {
        "token": token,
        "url": url,
        "geoCode": "in",
    }
    if render_js:
        params["render"] = "true"
    if set_cookies:
        params["setCookies"] = set_cookies
    if custom_headers:
        # Scrape.do's "Custom Headers" feature: when set, it forwards every
        # header on the request TO Scrape.do straight through to the target
        # site, instead of using its own defaults. Needed for endpoints that
        # require caller-supplied auth headers (e.g. Croma's authenticated
        # inventory API) rather than a plain page fetch.
        params["customHeaders"] = "true"
    if wait_until:
        # Scrape.do's Puppeteer-backed render wait condition (e.g.
        # "networkidle0") — only meaningful alongside render_js=True. Opt-in,
        # unused by every existing call site, so this is a no-op for them.
        params["waitUntil"] = wait_until
    if custom_wait_ms:
        # Extra fixed wait (ms) after waitUntil fires — a fallback buffer for
        # pages whose JS keeps mutating the DOM after the network goes idle.
        # Also opt-in/unused by existing call sites.
        params["customWait"] = str(custom_wait_ms)
    if super_proxy:
        # Scrape.do's premium/residential proxy pool ("Super Proxy") — costs
        # more credits per request than the default proxy tier and may not
        # be available on every plan. Opt-in, unused by existing call sites.
        params["super"] = "true"
    return f"{SCRAPEDO_API_URL}?{urlencode(params)}"


# ---------------------------------------------------------------------------
# Generic marketplace stock-check waterfall — shared by newer checkers for
# sites this codebase has no live-network access to individually verify
# against real pages yet (see each caller module for its own per-site
# pattern choices). Ported from checkers/reliancedigital.py, whose own
# AggregateOffer/disabled-class handling grew out of real production
# misreads — this generic version starts from that same lesson rather than
# a naive fresh implementation, but has NOT itself been verified against
# real pages for any site using it. Treat its result as a tuning starting
# point once real /check results (or a dedicated debug command, following
# the /debugreliance precedent) are available for that site.
# ---------------------------------------------------------------------------

_DISABLED_CLASS_MARKERS = ("disable", "inactive")


def _element_is_disabled(el) -> bool:
    """True if a BS4 element is visually/semantically disabled — via the
    `disabled` attribute, `aria-disabled="true"`, or a
    _DISABLED_CLASS_MARKERS substring in its class list."""
    if el.get("disabled") is not None:
        return True
    if el.get("aria-disabled", "").lower() == "true":
        return True
    classes = " ".join(el.get("class", [])).lower()
    return any(marker in classes for marker in _DISABLED_CLASS_MARKERS)


def _offer_availability(offers) -> str:
    """Extract the first availability string from a JSON-LD 'offers' value
    that may be a single Offer dict, an AggregateOffer dict wrapping a
    nested offers list, or a plain list of Offer dicts. Returns "" when
    none is found."""
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


_GENERIC_IN_STOCK_JSON_KEYS = (
    '"inStock":true', '"in_stock":true', '"isAvailable":true',
    '"available":true', '"sellable":true', '"is_available":true',
)
_GENERIC_OUT_OF_STOCK_JSON_KEYS = (
    '"inStock":false', '"in_stock":false', '"isAvailable":false',
    '"available":false', '"sellable":false', '"is_available":false',
)


def generic_marketplace_check(
    soup, html: str, add_patterns: list[str], oos_patterns: list[str], site_label: str,
) -> bool:
    """
    Best-guess generic retail-marketplace stock check: JSON-LD availability
    -> embedded-JSON stock key -> negative (OOS) text -> active
    add-to-cart button/attribute -> default False. `add_patterns`/
    `oos_patterns` are lowercase substrings the caller supplies per site;
    `site_label` is used only for log-line prefixes. Defaults to False
    (out of stock) when no signal is found at all, per this codebase's
    standing principle that a missed alert is safer than a false one.
    """
    html_lower = html.lower()

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
                logger.info(f"[{site_label}] JSON-LD: InStock → True")
                return True
            if "OutOfStock" in avail or "Discontinued" in avail:
                logger.info(f"[{site_label}] JSON-LD: OutOfStock/Discontinued → False")
                return False

    # ── Embedded JSON ────────────────────────────────────────────────────
    for key in _GENERIC_IN_STOCK_JSON_KEYS:
        if key in html:
            logger.info(f"[{site_label}] embedded JSON {key!r} → True")
            return True
    for key in _GENERIC_OUT_OF_STOCK_JSON_KEYS:
        if key in html:
            logger.info(f"[{site_label}] embedded JSON {key!r} → False")
            return False

    # ── Negative signals (checked before buttons) ───────────────────────
    for pattern in oos_patterns:
        if pattern in html_lower:
            logger.info(f"[{site_label}] OOS signal: '{pattern}' → False")
            return False

    # ── Buttons — skip disabled ──────────────────────────────────────────
    for btn in soup.find_all("button"):
        if _element_is_disabled(btn):
            continue
        text = btn.get_text(strip=True).lower()
        if any(p in text for p in add_patterns):
            logger.info(f"[{site_label}] active button '{text[:40]}' → True")
            return True

    # ── Attrs — skip disabled ────────────────────────────────────────────
    for attr in ("data-testid", "aria-label", "id"):
        for el in soup.find_all(attrs={attr: True}):
            if _element_is_disabled(el):
                continue
            val = (el.get(attr) or "").lower()
            if "add-to-cart" in val or "addtocart" in val or any(p in val for p in add_patterns):
                logger.info(f"[{site_label}] active attr {attr}='{val[:40]}' → True")
                return True

    logger.info(f"[{site_label}] no conclusive signal → defaulting OUT OF STOCK (False)")
    return False
