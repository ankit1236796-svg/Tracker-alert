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

from checkers import (
    detect_site, fetch_page, fetch_with_502_retry, CHECKER_MAP, PRICE_EXTRACTOR_MAP,
)
from checkers import apple as apple_checker
from checkers import shopatsc as shopatsc_checker

logger = logging.getLogger(__name__)

# Re-export detect_site so existing imports from this module still work
__all__ = ["detect_site", "check_stock", "batch_check"]

# ---------------------------------------------------------------------------
# Short-lived fetch cache
# ---------------------------------------------------------------------------
# Different users tracking the SAME product URL each get their own row in
# `products` (UNIQUE(user_id, url), not UNIQUE(url)), so the background loop
# previously fired one independent scraping-provider request per tracker even
# though the underlying page/request is identical. The cache key is built
# from the exact fetch parameters (url + render_js + set_cookies + wait_until
# + custom_wait_ms + super_proxy — see _cache_key below), which is
# deterministic per (site, url): set_cookies is never actually set for any
# site today (see _PINCODE_COOKIE_SITES below, an empty frozenset), so this
# is a safe cache key with no risk of serving one user's pincode-specific
# result to another. If a future site starts varying set_cookies per-pincode,
# this remains correct: it just becomes part of the cache key, so
# pincode-specific requests won't collide with each other, they just won't
# share a cache entry either. Provider-agnostic (see checkers.fetch_page) —
# unrelated to which of Scrape.do/Zyte is actually doing the fetching.
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


def _cache_key(
    url: str,
    render_js: bool,
    set_cookies: str | None,
    wait_until: str | None,
    custom_wait_ms: int | None,
    super_proxy: bool,
) -> str:
    return f"{url}|render={render_js}|cookies={set_cookies}|wait_until={wait_until}|custom_wait_ms={custom_wait_ms}|super={super_proxy}"


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


async def _fetch_html(
    url: str,
    site: str,
    render_js: bool = False,
    set_cookies: str | None = None,
    wait_until: str | None = None,
    custom_wait_ms: int | None = None,
    super_proxy: bool = False,
) -> str:
    """
    Fetch url via checkers.fetch_page (provider-aware — Zyte or Scrape.do,
    see config.SCRAPING_PROVIDER), reusing a recent identical fetch when one
    exists. A per-key lock prevents a thundering herd where several
    concurrent check_stock() calls for the same not-yet-cached URL (e.g. the
    background loop's asyncio.gather firing many products at once) each
    launch their own provider request before the first one has a chance to
    populate the cache.
    """
    key = _cache_key(url, render_js, set_cookies, wait_until, custom_wait_ms, super_proxy)
    now = time.monotonic()
    _prune_fetch_cache(now)

    cached = _fetch_cache.get(key)
    if cached is not None and now - cached[0] < _FETCH_CACHE_TTL_SECONDS:
        logger.info(f"[{site}] fetch cache hit (age={now - cached[0]:.0f}s) — provider request skipped")
        return cached[1]

    lock = await _get_fetch_lock(key)
    async with lock:
        # Re-check after acquiring the lock: a concurrent call for the same
        # key may have already populated the cache while we waited.
        now = time.monotonic()
        cached = _fetch_cache.get(key)
        if cached is not None and now - cached[0] < _FETCH_CACHE_TTL_SECONDS:
            logger.info(f"[{site}] fetch cache hit post-lock (age={now - cached[0]:.0f}s) — provider request skipped")
            return cached[1]

        response = await fetch_page(
            url, render_js=render_js, set_cookies=set_cookies,
            wait_until=wait_until, custom_wait_ms=custom_wait_ms,
            super_proxy=super_proxy, timeout=60.0, site=site,
        )
        response.raise_for_status()

        html = response.text
        _fetch_cache[key] = (time.monotonic(), html)
        return html


async def _fetch_direct(
    url: str,
    site: str,
    render_js: bool = False,
    set_cookies: str | None = None,
    wait_until: str | None = None,
    custom_wait_ms: int | None = None,
    super_proxy: bool = False,
) -> str:
    """
    Fetch url WITHOUT the cache/lock machinery in _fetch_html — for
    deliberate retries against the exact same parameters (e.g.
    _EXTRA_RETRY_ON_INCOMPLETE_SITES below), where reusing _fetch_html
    would just replay the same cached blocked/incomplete response instead
    of actually hitting the network again.
    """
    response = await fetch_page(
        url, render_js=render_js, set_cookies=set_cookies,
        wait_until=wait_until, custom_wait_ms=custom_wait_ms,
        super_proxy=super_proxy, timeout=60.0, site=site,
    )
    response.raise_for_status()
    return response.text

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
    # New marketplace checkers (unicornstore, vijaysales, inventstore,
    # sangeethamobiles) — real markup hasn't been inspected from this
    # sandbox (no live network access), so render=true is the safe
    # starting default rather than the credit-saving render=false used
    # for Flipkart/RelianceDigital/JioMart, which was only adopted after
    # those sites' non-rendered behavior was specifically verified. See
    # _SUPER_PROXY_FALLBACK_SITES below for the render=true → super=true
    # escalation these four sites also get.
    "unicornstore", "vijaysales", "inventstore", "sangeethamobiles",
}
# ShopAtSC deliberately NOT here — it's special-cased near the top of
# check_stock() with its own two-stage render escalation (render=false
# first, render=true only if that looks incomplete), the opposite default
# order from every other site in this set. See
# checkers.shopatsc.check_via_html for the full reasoning.

# OnePlus renders incompletely under a plain render=true (confirmed via the
# /debugoneplus admin command — the page's JS/XHR activity hadn't settled
# by the time Scrape.do captured it). waitUntil="networkidle0" plus a fixed
# customWait buffer fixes this; scoped to oneplus only via these per-site
# maps (build_scraper_url's wait_until/custom_wait_ms are no-ops for every
# other site, which don't appear here and so get None → unchanged requests).
#
# Unicorn Store hits the same symptom, confirmed via /debugunicorn: even
# with render=true, Scrape.do was capturing the page before its SPA
# finished loading — visible text was just boilerplate/footer plus the
# literal "Please enable JavaScript to continue using this application"
# fallback text, with the actual product content (price, stock status)
# never appearing. Same fix, same reasoning as OnePlus: waitUntil=
# "networkidle0" plus a customWait buffer. Given 6000ms (the middle of
# the 5000-8000ms range that fixed this class of issue) rather than
# OnePlus's 4000ms, since this page was captured showing essentially
# NO real content (not just one missing signal), suggesting it needs
# more settle time than OnePlus did.
_SITE_WAIT_UNTIL = {"oneplus": "networkidle0", "unicornstore": "networkidle0"}
_SITE_CUSTOM_WAIT_MS = {"oneplus": 4000, "unicornstore": 6000}

# Sites that get a second fetch attempt with Scrape.do's super=true premium
# proxy pool when the render=true fetch looks blocked/incomplete — the same
# symptom (a near-empty or challenge page instead of the real one) that
# real diagnostics confirmed for RelianceDigital as an Akamai WAF block on
# the requesting IP (see playwright_scraper/README.md and the
# /debugreliance admin command). Scoped to just these four new,
# unverified-from-this-sandbox checkers rather than applied globally, so
# no existing site's behavior/cost changes. super=true costs more Scrape.do
# credits, so it's only spent when the first fetch actually looks bad.
_SUPER_PROXY_FALLBACK_SITES = frozenset({
    "unicornstore", "vijaysales", "inventstore", "sangeethamobiles",
})

# Sites whose render=true+super=true fallback fetch gets automatic retry
# on HTTP 502 (Scrape.do's proxy-rotation-failure symptom, sometimes
# carrying an ErrorType: "ROTATION_FAILED" response header) — confirmed
# specifically for Unicorn Store's super=true tier via /debugunicorn.
# Scoped to just this one site rather than all of
# _SUPER_PROXY_FALLBACK_SITES, since 502s haven't been confirmed for the
# other three. See checkers.common.fetch_with_502_retry for the retry
# policy (3 attempts total, ~4s between retries).
_RETRY_502_SITES = frozenset({"unicornstore"})

# Sites that get 1-2 EXTRA fetch retries (fresh, non-cached — see
# _fetch_direct) when the render=true+super=true fallback still looks
# blocked/incomplete, rather than proceeding to run stock-detection text
# matching on HTML that probably isn't the real page. If every retry is
# still incomplete, check_stock() returns (None, None) — an explicit
# "check was inconclusive, skip this update" result distinct from a
# confirmed False (out of stock) verdict — rather than letting
# checkers.inventstore.check() guess at a false OOS read from a
# blocked/challenge page's near-empty text. Scoped to inventstore only;
# every other site keeps proceeding with whatever HTML it has, unchanged.
_EXTRA_RETRY_ON_INCOMPLETE_SITES = frozenset({"inventstore"})
_EXTRA_RETRY_ATTEMPTS = 2

# Heuristics for "the fetched page probably isn't the real one" — an
# unusually short response, or a phrase commonly shown by bot-block/
# challenge pages. Not confirmed against any of these four sites'
# real block pages specifically (no live network access from this
# sandbox); a conservative, generic trigger for retrying with super=true
# rather than silently reporting a possibly-wrong stock result.
_BLOCKED_PAGE_PHRASES = (
    "access denied", "attention required", "are you a human",
    "captcha", "just a moment", "checking your browser",
    "please enable javascript and cookies", "bot detection",
    "request unsuccessful",
)
_MIN_PLAUSIBLE_HTML_LENGTH = 2000


def _visible_text_for_block_check(html: str) -> str:
    """Strip <script>/<style>/<noscript> before extracting text —
    mirrors every checker's own visible-text helper. Used by
    _looks_blocked_or_incomplete's phrase check so it reflects what a
    real viewer would actually SEE, not raw markup that can contain a
    _BLOCKED_PAGE_PHRASES match with nothing to do with whether this
    fetch actually got the real page: an always-present <noscript>
    fallback message (e.g. "please enable javascript..."), a
    third-party anti-bot vendor's own JS bundle literally containing
    strings like "bot detection" or "captcha" in its source/comments, or
    a hidden widget that's part of every normal page load. This was
    confirmed as a real false positive for InventStore via
    /debuginventstore: a fetch that visibly succeeded (46,710 chars of
    real visible text, "Buy Now" present, no OOS text) was still being
    flagged as blocked/incomplete by a phrase match somewhere in the raw
    HTML outside the visible content."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return soup.get_text(" ", strip=True)


def _looks_blocked_or_incomplete(html: str) -> bool:
    # Length check stays against the RAW html — a genuinely empty/tiny
    # response (e.g. a bare error page) is still caught here regardless
    # of visible-text extraction, and this threshold wasn't implicated in
    # the false positive above (InventStore's real page vastly exceeds
    # it either way).
    if len(html) < _MIN_PLAUSIBLE_HTML_LENGTH:
        return True
    visible_text_lower = _visible_text_for_block_check(html).lower()
    return any(phrase in visible_text_lower for phrase in _BLOCKED_PAGE_PHRASES)

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
) -> tuple[bool | None, float | None]:
    """
    Returns (in_stock, current_price).
    current_price is only populated for sites in PRICE_EXTRACTOR_MAP (currently Amazon);
    it is None for all other sites and when extraction fails.

    in_stock is normally a definite bool. It is None ONLY for sites in
    _EXTRA_RETRY_ON_INCOMPLETE_SITES (currently just inventstore) when the
    fetched page still looks blocked/incomplete after every retry tier —
    an explicit "check was inconclusive" result, distinct from a
    confirmed False (out of stock) verdict. Every caller of check_stock()
    must treat in_stock is None as "skip this update" (no database write,
    no stock-transition alert), not as a falsy OOS result.

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

    if site == "shopatsc":
        # Sole signal: HTML text detection via Scrape.do (render=false
        # first, escalating to render=true only if that looks incomplete).
        # The .js JSON endpoint's "available" field was confirmed
        # unreliable for this store and is no longer used at all — see
        # checkers.shopatsc.check_via_html.
        try:
            result = await shopatsc_checker.check_via_html(url)
        except Exception as exc:
            logger.error(f"[shopatsc] check_via_html failed: {exc}")
            return False, None
        logger.info(f"[shopatsc] {url} → {'IN STOCK' if result else 'OUT OF STOCK'}")
        return result, None

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
        logger.info(f"[{site}] setCookies={set_cookies!r} render_js={site in _JS_SITES}")

        html = await _fetch_html(
            url, site,
            render_js=site in _JS_SITES,
            set_cookies=set_cookies,
            wait_until=_SITE_WAIT_UNTIL.get(site),
            custom_wait_ms=_SITE_CUSTOM_WAIT_MS.get(site),
        )

        if site in _SUPER_PROXY_FALLBACK_SITES and _looks_blocked_or_incomplete(html):
            logger.warning(
                f"[{site}] render=true fetch looks blocked/incomplete "
                f"(len={len(html)}) — retrying with super=true (premium proxy)"
            )

            if site in _RETRY_502_SITES:
                # Also retries the super=true fetch itself up to 3 total
                # attempts on HTTP 502 (Scrape.do proxy-rotation-failure
                # symptom), ~4s apart — see checkers.common.fetch_with_502_retry.
                resp, attempts = await fetch_with_502_retry(
                    url,
                    render_js=True,
                    set_cookies=set_cookies,
                    wait_until=_SITE_WAIT_UNTIL.get(site),
                    custom_wait_ms=_SITE_CUSTOM_WAIT_MS.get(site),
                    super_proxy=True,
                    site=site,
                )
                logger.info(f"[{site}] super=true fetch attempts: {attempts}")
                if resp is not None:
                    # Whatever came back — a genuine success, or a still-502
                    # after every retry — becomes the HTML to check, exactly
                    # like the non-retried path below. Never raises/hangs:
                    # the retry helper already bounded attempts and timeouts.
                    html = resp.text
                else:
                    # A non-502 exception (timeout/connection error) aborted
                    # the retry loop early with no response at all — keep the
                    # existing (render=true-only) HTML from above rather than
                    # losing it, the same "proceed with whatever we have"
                    # fallback philosophy as the non-retried branch.
                    logger.warning(
                        f"[{site}] super=true fetch failed outright after "
                        f"{len(attempts)} attempt(s) — keeping the earlier "
                        f"render=true-only HTML"
                    )
            else:
                html = await _fetch_html(
                    url, site,
                    render_js=True,
                    set_cookies=set_cookies,
                    wait_until=_SITE_WAIT_UNTIL.get(site),
                    custom_wait_ms=_SITE_CUSTOM_WAIT_MS.get(site),
                    super_proxy=True,
                )

            if _looks_blocked_or_incomplete(html):
                logger.warning(
                    f"[{site}] super=true retry STILL looks blocked/incomplete "
                    f"(len={len(html)})"
                )

                if site in _EXTRA_RETRY_ON_INCOMPLETE_SITES:
                    for extra_attempt in range(1, _EXTRA_RETRY_ATTEMPTS + 1):
                        logger.warning(
                            f"[{site}] still incomplete — extra retry "
                            f"{extra_attempt}/{_EXTRA_RETRY_ATTEMPTS} (fresh, non-cached fetch)"
                        )
                        try:
                            html = await _fetch_direct(
                                url,
                                site,
                                render_js=True,
                                set_cookies=set_cookies,
                                wait_until=_SITE_WAIT_UNTIL.get(site),
                                custom_wait_ms=_SITE_CUSTOM_WAIT_MS.get(site),
                                super_proxy=True,
                            )
                        except Exception as exc:
                            logger.warning(f"[{site}] extra retry {extra_attempt} failed: {exc}")
                            continue
                        if not _looks_blocked_or_incomplete(html):
                            break

                    if _looks_blocked_or_incomplete(html):
                        # Do NOT run stock-detection text matching against HTML
                        # that still looks like a block/challenge page after
                        # every retry tier — that would be guessing a False
                        # (out of stock) verdict from a page that probably
                        # isn't the real one. Return an explicit "inconclusive,
                        # skip this update" result instead.
                        logger.warning(
                            f"[{site}] still blocked/incomplete after "
                            f"{_EXTRA_RETRY_ATTEMPTS} extra retries (len={len(html)}) — "
                            f"returning an inconclusive (None) result rather than a "
                            f"guessed OOS verdict"
                        )
                        return None, None
                else:
                    logger.warning(f"[{site}] proceeding with whatever HTML was returned")

        soup = BeautifulSoup(html, "html.parser")
        result = checker(soup, html)

        if site == "apple" and pincode:
            result = await apple_checker.refine_with_pincode(
                soup, html, pincode, generic_result=result, url=url,
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
) -> list[tuple[dict, bool | None]]:
    results = []
    for product in products:
        in_stock, _price = await check_stock(
            product["url"], product["site"], pincode=pincode, caller="batch_check"
        )
        results.append((product, in_stock))
        await asyncio.sleep(3)
    return results
