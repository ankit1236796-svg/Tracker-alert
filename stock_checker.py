"""
stock_checker.py
~~~~~~~~~~~~~~~~
Thin orchestration layer — delegates per-site logic to checkers/.
"""

import logging
import asyncio
import time

import httpx
from bs4 import BeautifulSoup

from checkers import detect_site, build_scraper_url, HEADERS, CHECKER_MAP, PRICE_EXTRACTOR_MAP
from checkers import apple as apple_checker

logger = logging.getLogger(__name__)

# Re-export detect_site so existing imports from this module still work
__all__ = ["detect_site", "check_stock", "batch_check"]

# ---------------------------------------------------------------------------
# Short-lived fetch cache
# ---------------------------------------------------------------------------
# Different users tracking the SAME product URL each get their own row in
# `products` (UNIQUE(user_id, url), not UNIQUE(url)), so the background loop
# previously fired one independent Scrape.do request per tracker even though
# the underlying page/request is identical. Today `build_scraper_url()` is
# fully deterministic per (site, url) — set_cookies is never actually set
# for any site (see _PINCODE_COOKIE_SITES below, an empty frozenset) — so the
# exact scraper_url is a safe cache key with no risk of serving one user's
# pincode-specific result to another. If a future site starts varying
# set_cookies per-pincode, this remains correct: it just becomes part of the
# cache key (via the full scraper_url), so pincode-specific requests won't
# collide with each other, they just won't share a cache entry either.
#
# This caches the raw HTML fetch only, not the final in_stock/price result —
# Apple's per-user pincode refinement (refine_with_pincode) and Amazon's
# price extraction still run fresh against the (possibly cached) HTML on
# every call.
_FETCH_CACHE_TTL_SECONDS = 240  # 4 min — inside the 3-5 min window requested;
                                 # comfortably under the 300s default
                                 # CHECK_INTERVAL, so it collapses duplicate
                                 # requests within one background cycle
                                 # without materially delaying detection of a
                                 # real stock change.
_fetch_cache: dict[str, tuple[float, str]] = {}
_fetch_locks: dict[str, asyncio.Lock] = {}
_locks_guard = asyncio.Lock()


async def _get_fetch_lock(key: str) -> asyncio.Lock:
    async with _locks_guard:
        lock = _fetch_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _fetch_locks[key] = lock
        return lock


def _prune_fetch_cache(now: float) -> None:
    expired = [k for k, (ts, _) in _fetch_cache.items() if now - ts >= _FETCH_CACHE_TTL_SECONDS]
    for k in expired:
        _fetch_cache.pop(k, None)
        _fetch_locks.pop(k, None)


async def _fetch_html(scraper_url: str, site: str) -> str:
    """
    Fetch scraper_url, reusing a recent identical fetch when one exists.
    A per-key lock prevents a thundering herd where several concurrent
    check_stock() calls for the same not-yet-cached URL (e.g. the background
    loop's asyncio.gather firing many products at once) each launch their own
    Scrape.do request before the first one has a chance to populate the cache.
    """
    now = time.monotonic()
    _prune_fetch_cache(now)

    cached = _fetch_cache.get(scraper_url)
    if cached is not None and now - cached[0] < _FETCH_CACHE_TTL_SECONDS:
        logger.info(f"[{site}] fetch cache hit (age={now - cached[0]:.0f}s) — Scrape.do request skipped")
        return cached[1]

    lock = await _get_fetch_lock(scraper_url)
    async with lock:
        # Re-check after acquiring the lock: a concurrent call for the same
        # scraper_url may have already populated the cache while we waited.
        now = time.monotonic()
        cached = _fetch_cache.get(scraper_url)
        if cached is not None and now - cached[0] < _FETCH_CACHE_TTL_SECONDS:
            logger.info(f"[{site}] fetch cache hit post-lock (age={now - cached[0]:.0f}s) — Scrape.do request skipped")
            return cached[1]

        async with httpx.AsyncClient(
            headers=HEADERS,
            follow_redirects=True,
            timeout=60.0,
        ) as client:
            response = await client.get(scraper_url)
            response.raise_for_status()

        html = response.text
        _fetch_cache[scraper_url] = (time.monotonic(), html)
        return html

# Sites that need JS rendering
#
# Flipkart deliberately excluded: empirically verified via
# compare_flipkart_render.py against 5 manually-confirmed products (3 OOS,
# 2 in-stock) that render=false reproduces the same checkers.flipkart.check()
# result as render=true in every case — JSON-LD availability and OOS/button
# text both survive the non-rendered fetch. Cuts Flipkart from 5 credits to
# 1 credit per Scrape.do request. If Flipkart's markup changes and this
# stops holding, re-add "flipkart" here.
#
# Reliance Digital and JioMart also excluded (credit-cost pass, no direct
# render=true-vs-false A/B test possible this time — no live network access
# was available to run one; this is architectural inference, NOT the same
# empirical proof Flipkart got). Both checkers' primary signal is JSON-LD
# `offers.availability`, and both sites are large, SEO-invested retail
# catalogs whose product-schema markup is almost always injected server-side
# regardless of client-side rendering (crawlers need it without executing
# JS) — the same property that held for Flipkart. If real /check results
# show this doesn't hold (JSON-LD/OOS text missing without JS), re-add the
# site here.
#
# Instamart and TataNeu deliberately KEPT here despite the same cost
# incentive — each has a specific reason to distrust a render=false fetch:
# Instamart is a quick-commerce app already documented above
# (_PINCODE_COMPLEX_SITES) as needing a JS-driven session/cookie flow just to
# resolve location, suggesting the base page itself may only hydrate its
# content (JSON-LD, buttons) via JS — a false fetch would silently and
# permanently default every product to OOS (no signals found). TataNeu is
# worse: its ONLY signal is the ABSENCE of a negative text phrase (see
# checkers/tataneu.py), so a JS-dependent page that's simply empty without
# rendering would resolve every product to IN STOCK — a systematic
# false-positive-alert risk, not just a missed one. Not touching either
# without an explicit real-URL test first.
#
# Blinkit — EXPERIMENTAL, pending real-URL verification (see
# checkers/blinkit.py's NEEDS_JS comment for the full reasoning and the
# rollback plan). Removed from this set to try render=false: unlike
# Instamart, Blinkit's own documented behavior (_PINCODE_COMPLEX_SITES above)
# is to fall back to a DEFAULT LOCALITY'S catalog (not a blank/gated page)
# when location cookies are missing — a content-bearing fallback that's
# plausibly resolved server-side, not proof this survives non-JS rendering,
# but a real reason to suspect it might. The Cloudflare bot-management
# cookies (__cf_bm/_cfuvid) documented there remain an open risk Scrape.do's
# non-render fetch may or may not clear on its own. Failure mode if wrong is
# the SAME safe direction as Instamart (defaults to OOS, not a false
# positive) — if real /check results show this breaking (stuck OOS on a
# confirmed-in-stock product), revert by adding "blinkit" back to this set;
# this was deliberately committed separately from the Reliance
# Digital/JioMart switch so it can be reverted independently.
_JS_SITES = {
    "zepto", "bigbasket", "croma", "instamart", "myntra",
    "oneplus", "tataneu", "vivo", "iqoo",
}

# No sites currently support pincode-specific stock via simple cookie injection.
# Blinkit was here previously (`pincode=<value>`) but real captured Blinkit
# traffic confirms the backend does not read a cookie literally named "pincode"
# — see _PINCODE_COMPLEX_SITES below. Kept as an empty frozenset (rather than
# removed) so the _QUICK_COMMERCE_SITES union and the branching logic below
# stay structurally valid if a future site is confirmed to support it.
_PINCODE_COOKIE_SITES = frozenset()

# Sites that require session/storeId/coordinate resolution for location-specific
# stock — simple cookie injection won't work; results reflect Scrape.do's IP
# geolocation instead.
#
# BigBasket: location is stored server-side in a Django session tied to a
# logged-in account (addr_id). A bare `pincode=<value>` cookie is NOT read by
# BigBasket for stock determination. Injecting it could bypass the location gate
# and let the page render, but the stock shown would be for Scrape.do's proxy IP
# (typically a metro city), not the user's pincode. This is worse than letting
# the location gate fire (which the checker correctly treats as OOS).
#
# Blinkit: real captured HTTP traffic (live HAR/network capture, not docs) shows
# Blinkit resolves location via a 3-step flow — GET /location/autoSuggest
# (pincode text → place_id) → GET /location/info (place_id → lat/lon +
# is_serviceable) → then the actual product page needs gr_1_lat, gr_1_lon, and a
# NUMERIC gr_1_locality cookie (not the pincode string) alongside gr_1_deviceId,
# plus Cloudflare bot-management cookies (__cf_bm, _cfuvid) that a plain HTTP
# client can't obtain without a JS-capable/browser-fingerprint-matching fetch.
# A bare `pincode=<value>` cookie (the previous approach here) is not part of
# this flow and is silently ignored — one documented real-world case shows
# Blinkit falling back to a DEFAULT locality (e.g. Ahmedabad 380015) whenever
# location cookies are missing/invalid, rather than erroring. That default-
# fallback behavior is exactly why two different pincodes (132001 and 400052)
# previously produced the SAME result: neither was actually being read: both
# silently collapsed to the same default/proxy-geolocated locality. Cookie
# injection removed; implementing true pincode accuracy would require making
# the autoSuggest/info calls first, which is a larger integration than a single
# cookie and is not implemented here.
_PINCODE_COMPLEX_SITES = frozenset({"zepto", "instamart", "bigbasket", "blinkit"})

_QUICK_COMMERCE_SITES = _PINCODE_COOKIE_SITES | _PINCODE_COMPLEX_SITES


async def check_stock(
    url: str, site: str, pincode: str | None = None, caller: str = "unknown"
) -> tuple[bool, float | None]:
    """
    Returns (in_stock, current_price).
    current_price is only populated for sites in PRICE_EXTRACTOR_MAP (currently Amazon);
    it is None for all other sites and when extraction fails.

    `caller` is a label ("background" | "manual" | ...) identifying which code
    path invoked this check — logged up front so the rest of this request's
    log lines (including any per-site [site][diag] trail) can be tied back to
    whether it came from the automatic background loop or a manual /check.
    Purely diagnostic: does not affect behavior.
    """
    logger.info(f"[{site}] check_stock called: caller={caller!r} pincode={pincode!r} url={url!r}")

    checker = CHECKER_MAP.get(site)
    if checker is None:
        logger.warning(f"No checker for site '{site}'")
        return False, None

    set_cookies = None
    if site in _QUICK_COMMERCE_SITES:
        if pincode:
            if site in _PINCODE_COOKIE_SITES:
                # Inject pincode as a cookie so the target site serves
                # location-specific stock rather than IP-geolocated defaults.
                set_cookies = f"pincode={pincode}"
                logger.info(f"[{site}] pincode={pincode!r} → setCookies={set_cookies!r}")
            else:
                if site == "bigbasket":
                    logger.warning(
                        f"[bigbasket] pincode {pincode} saved but BigBasket uses "
                        f"server-side session location (Django sessionid + addr_id). "
                        f"A bare pincode= cookie is NOT read for stock data — previously "
                        f"injecting it bypassed BigBasket's location gate and returned "
                        f"stock for Scrape.do's proxy IP (not pincode {pincode}), causing "
                        f"false in-stock alerts. Cookie injection removed. Stock will now "
                        f"reflect proxy geolocation or trigger the location gate (→ OOS)."
                    )
                elif site == "blinkit":
                    logger.warning(
                        f"[blinkit] pincode {pincode} saved but Blinkit resolves location "
                        f"via GET /location/autoSuggest (pincode→place_id) then "
                        f"GET /location/info (place_id→lat/lon+is_serviceable), then reads "
                        f"gr_1_lat/gr_1_lon/a NUMERIC gr_1_locality cookie on the product "
                        f"page — plus Cloudflare bot-management cookies. A bare pincode= "
                        f"cookie is NOT part of this flow and was silently ignored; Blinkit "
                        f"falls back to a DEFAULT locality on missing/invalid location "
                        f"cookies (documented real-world case: Ahmedabad 380015), which is "
                        f"why different pincodes previously produced the SAME result. "
                        f"Cookie injection removed. Stock will now reflect proxy geolocation "
                        f"or trigger the location gate (→ OOS), not delivery at pincode {pincode}."
                    )
                else:
                    logger.warning(
                        f"[{site}] pincode {pincode} saved but {site} requires "
                        f"a storeId/session lookup — results reflect Scrape.do IP "
                        f"geolocation, not delivery at pincode {pincode}"
                    )
        else:
            logger.warning(
                f"[{site}] no pincode set — stock shown for Scrape.do IP "
                f"geolocation, not the user's delivery area. "
                f"Use /pins to add a pincode for more accurate results."
            )
    elif site == "apple":
        # Apple uses a genuinely public API (no cookie/session needed) — see
        # checkers/apple.py:refine_with_pincode. It runs AFTER the main page
        # fetch below (it needs the SKU extracted from that page), not here.
        if pincode:
            logger.info(f"[apple] pincode={pincode!r} set — will attempt pincode-specific pickup lookup")
        else:
            logger.warning(
                f"[apple] no pincode set — stock reflects the generic product page "
                f"only (JSON-LD/Add to Bag/OOS text), not pincode-specific pickup "
                f"availability. Use /pins to add a pincode for more accurate results."
            )

    try:
        scraper_url = build_scraper_url(url, render_js=site in _JS_SITES, set_cookies=set_cookies)
        logger.info(f"[{site}] setCookies={set_cookies!r} render_js={site in _JS_SITES}")
        logger.info(f"[{site}] scraper_url (truncated)={scraper_url[:120]!r}")

        html = await _fetch_html(scraper_url, site)
        soup = BeautifulSoup(html, "html.parser")
        result = checker(soup, html)

        if site == "apple" and pincode:
            result = await apple_checker.refine_with_pincode(
                soup, html, pincode, generic_result=result
            )

        price: float | None = None
        price_extractor = PRICE_EXTRACTOR_MAP.get(site)
        if price_extractor is not None:
            price = price_extractor(soup, html)

        price_str = f" @ ₹{price:,.0f}" if price is not None else ""
        logger.info(f"[{site}] {url} → {'IN STOCK' if result else 'OUT OF STOCK'}{price_str}")
        return result, price

    except httpx.HTTPStatusError as e:
        logger.error(f"HTTP error {e.response.status_code} for {url}")
        return False, None
    except Exception as exc:
        logger.error(f"Error checking {url}: {exc}")
        return False, None


async def batch_check(
    products: list[dict],
    pincode: str | None = None,
) -> list[tuple[dict, bool]]:
    results = []
    for product in products:
        in_stock, _price = await check_stock(
            product["url"], product["site"], pincode=pincode, caller="batch_check"
        )
        results.append((product, in_stock))
        await asyncio.sleep(3)
    return results
