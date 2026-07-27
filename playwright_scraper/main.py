"""
playwright_scraper/main.py
~~~~~~~~~~~~~~~~~~~~~~~~~~
Standalone Playwright + Chromium service, serving two independent purposes
in the main bot's architecture:

1. A pilot scraper for iQOO and Vivo, built to test replacing their current
   Scrape.do render=true checks — those burn render credits at scale; this
   self-hosts the same JS-rendering step behind an optional metered proxy
   instead. NOT wired into the main bot's production checkers (checkers/
   iqoo.py, checkers/vivo.py are untouched) — /check-stock exists purely for
   standalone testing/comparison.
2. The main bot's Apple pickup-checking IS wired to this service, but only
   for periodic cookie refresh (/refresh-apple-cookies, called by bot.py's
   apple_cookie_refresh_loop every APPLE_COOKIE_REFRESH_INTERVAL) — never
   for the actual per-check requests, which stay on plain httpx in the main
   bot for speed. See that endpoint's own section below for why.

ISOLATION: this is a completely separate Railway service — its own
container, its own process, its own requirements.txt (Playwright is NOT a
dependency of the main bot). If this crashes, gets blocked, or a proxy runs
out of quota: /check-stock's iQOO/Vivo pilot is simply unaffected (nothing
in the main bot calls it); /refresh-apple-cookies failing just means the
main bot's stored Apple session goes unrefreshed until the next cycle or
this service recovers — the main bot falls back to whatever session it last
had, then to the APPLE_COOKIES/APPLE_USER_AGENT env vars (see
checkers/apple.py's _resolve_apple_session). Never fatal to the main bot.

HTTP surface:
  POST /check-stock    Body: {"url": str, "store": "iqoo"|"vivo"}. Returns
                        {"url", "store", "in_stock": bool|None, "signal": str,
                        "attempts": int}. in_stock is None ("check failed")
                        when no conclusive signal was found after retrying —
                        NEVER a guessed False, unlike the main bot's own
                        checkers (deliberate: this is a pilot being tuned, an
                        inconclusive read should surface for investigation,
                        not silently default to "out of stock").
  POST /debug-network  Body: {"url": str, "pincode": str (optional, default
                        "110001")}. Applies pincode as a cookie (best-effort
                        — see _capture_network_calls), loads the page, and
                        records every XHR/fetch response whose URL contains
                        a serviceability/delivery/pincode/availability/
                        stock/fulfillment keyword. Returns {"url", "pincode",
                        "matched_requests": [{"url", "method", "status",
                        "body"}...], "total_requests_seen", "matched_count",
                        "all_responses_seen": [{"url","status",
                        "resource_type"}...] (lightweight, every response —
                        not just matches, capped at 100), "diagnostics":
                        {"goto_status", "goto_error", "final_url",
                        "page_title", "page_crashed",
                        "networkidle_timed_out", "networkidle_error",
                        "html_length", "html_snippet", ...} so a silent
                        navigation failure or anti-bot block page is visible
                        in the response, not just a suspiciously low count.
                        For admin diagnostic use (e.g. RelianceDigital's
                        /debugreliance) — hit directly, no auth (matches
                        /check-stock; this whole service has none).
  POST /debug-dom       Body: {"url": str, "click_text": str (optional)}.
                        _capture_network_calls' sibling for DOM STRUCTURE
                        discovery instead of network traffic (see
                        _capture_dom_elements) — loads the page, waits for
                        it to settle, then returns every element whose
                        data-autom/text/attributes mention pickup,
                        fulfillment, store, zip, pincode, or location, plus
                        the full outerHTML of any pickUpDetails element(s).
                        When click_text is given, also clicks the first
                        element containing that text (case-insensitive)
                        after the initial capture, waits for any resulting
                        modal/overlay to settle, and re-runs the same
                        extraction — returned as "after_click" — so a
                        pincode input hidden behind a "Check availability"
                        trigger becomes visible without guessing at the
                        modal's own selectors first. Returns {"url",
                        "page_title", "data_autom_elements",
                        "pincode_like_inputs", "pickup_related_buttons",
                        "pickup_details_html", "after_click", "diagnostics"
                        (includes "click_result" when click_text was
                        given)}. Built for the 2026-07-27 Apple pickup-
                        widget selector discovery (see checkers/apple.py's
                        investigation notes) — no auth, same as every
                        other endpoint here.
  POST /refresh-apple-cookies
                        Body: {"url": str (an apple.com/<locale>/shop/...
                        product page), "pincode": str}. Loads the page in a
                        real headless-Chromium session, extracts its SKU,
                        and performs an in-page fetch() of Apple's
                        fulfillment-messages endpoint (same query params as
                        the main bot's checkers/apple.py) for `pincode` —
                        this is the exact request Apple's own pickup-
                        availability UI would trigger, run from inside the
                        already-loaded page's JS context so it carries a
                        genuine browser TLS fingerprint, headers, and
                        cookies. Returns {"url", "pincode", "cookies":
                        "name=value; ..." (semicolon-joined, ready to use as
                        a Cookie header), "user_agent": str,
                        "pincode_check_confirmed": bool (True only if that
                        in-page fetch returned HTTP 200 with valid JSON),
                        "diagnostics": {...}}. Requires header
                        "X-Internal-Token: <INTERNAL_REFRESH_TOKEN>" only if
                        that env var is set on this service (unset = no auth,
                        matching every other endpoint's default here). See
                        _refresh_apple_cookies's own docstring for the full
                        reasoning on why this uses an in-page fetch instead
                        of clicking through Apple's pickup UI widget.
  GET  /health         Unauthenticated: {"ok", "max_concurrent_checks",
                        "proxy_configured", "supported_stores",
                        "apple_cookie_refresh_auth_required"}.

Stock-detection logic for iqoo/vivo (check_iqoo_vivo_stock, _OOS_PATTERNS,
_offer_availability) is PORTED from checkers/iqoo.py and checkers/vivo.py —
both already probe-confirmed reliable (JSON-LD offers.availability primary,
embedded-JSON fallback, explicit OOS text last resort) against real
in-stock/OOS product pages. This sandbox has no live network access to
verify the signal still holds when sourced via Playwright+proxy instead of
Scrape.do's render=true — see README.md for the live-verification steps
this needs once deployed.
"""

import importlib.metadata
import json
import logging
import os
import re
import threading
import time
from typing import Callable
from urllib.parse import urlencode, urlparse

from bs4 import BeautifulSoup
from flask import Flask, jsonify, request
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("playwright_scraper")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
PORT = int(os.getenv("PORT", "8080"))
HEADLESS = os.getenv("PLAYWRIGHT_HEADLESS", "true").lower() != "false"

# Optional shared secret for /refresh-apple-cookies only (every other
# endpoint here stays unauthenticated, by design — see the module docstring
# and README). Unset means no auth check, matching this service's existing
# "internal pilot" posture; set it (and the matching
# PLAYWRIGHT_SCRAPER_INTERNAL_TOKEN on the main bot side) once this handles
# real session cookies rather than just stock-check results.
INTERNAL_REFRESH_TOKEN = os.getenv("INTERNAL_REFRESH_TOKEN", "")

# Requirement #7 ("Add resource limits: cap con...") — the message cut off
# there; taken as "cap concurrent browser instances", since that's the
# standard resource-exhaustion risk for a self-hosted scraper (each headless
# Chromium instance can use 150-300MB+ RAM). A Semaphore bounds how many
# checks run in parallel; excess requests queue for a free slot rather than
# spawning unbounded browsers. Flag if a different limit was meant (memory
# ceiling, requests/min, etc.) — Railway's own container limits are separate
# and unaffected by this.
MAX_CONCURRENT_CHECKS = int(os.getenv("MAX_CONCURRENT_CHECKS", "2"))
# How long an incoming request waits for a free concurrency slot before
# giving up (returns a "check failed" result rather than queueing forever).
SLOT_WAIT_TIMEOUT_SECONDS = float(os.getenv("SLOT_WAIT_TIMEOUT_SECONDS", "60"))

NAV_TIMEOUT_MS = int(os.getenv("NAV_TIMEOUT_MS", "20000"))
# How long to wait specifically for the primary stock signal (a JSON-LD
# script tag) to appear before giving up on THIS attempt — requirement #6's
# "stock element isn't found within a reasonable timeout". Not a hard
# failure: the fallback signals (embedded JSON, OOS text) still run against
# whatever HTML is present even if this wait times out.
SIGNAL_WAIT_TIMEOUT_MS = int(os.getenv("SIGNAL_WAIT_TIMEOUT_MS", "8000"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))
RETRY_DELAY_SECONDS = float(os.getenv("RETRY_DELAY_SECONDS", "2"))

# Apple cookie-refresh-specific timing (see _refresh_apple_cookies) — kept
# as its OWN separate knobs rather than reusing NAV_TIMEOUT_MS/SIGNAL_WAIT_
# TIMEOUT_MS above (those tune the iQOO/Vivo /check-stock pilot's JSON-LD
# wait, a different concern). Diagnostic logging confirmed _abck/bm_sz/
# ak_bmsc (Akamai Bot Manager's own marker cookies) were ALL absent from a
# refresh that captured cookies immediately after domcontentloaded with no
# dwell time at all — the session was never validated by Akamai in the
# first place, which is why it died in ~9 minutes instead of the ~2 hours a
# manually-copied, actively-used real browser session showed. These three
# values give Akamai's own sensor/telemetry JS time to run before cookies
# are captured and the browser is torn down.
APPLE_REFRESH_NETWORKIDLE_TIMEOUT_MS = int(os.getenv("APPLE_REFRESH_NETWORKIDLE_TIMEOUT_MS", "8000"))
APPLE_REFRESH_DWELL_MS = int(os.getenv("APPLE_REFRESH_DWELL_MS", "5000"))
APPLE_REFRESH_POST_FETCH_WAIT_MS = int(os.getenv("APPLE_REFRESH_POST_FETCH_WAIT_MS", "2000"))

# /debug-network: default pincode applied when the caller doesn't specify
# one — an arbitrary real Indian pincode, not tied to any particular store.
DEFAULT_DEBUG_PINCODE = os.getenv("DEBUG_NETWORK_DEFAULT_PINCODE", "110001")
# Substrings (case-insensitive) of a response URL worth capturing — these
# are the endpoint-name patterns a serviceability/stock-by-pincode API call
# would plausibly contain.
_NETWORK_CAPTURE_KEYWORDS = (
    "serviceability", "delivery", "pincode", "availability", "stock", "fulfillment",
)
# Cap on how much of each matched response body is kept, so one huge JSON
# blob can't blow up the HTTP response back to the caller.
_MAX_BODY_CHARS = 5000

# Webshare (or any HTTP-auth) proxy — entirely optional. Unset PROXY_HOST
# means "no proxy", so this runs directly for local testing before buying a
# proxy plan; set all four once you have Webshare credentials.
PROXY_HOST = os.getenv("PROXY_HOST", "")
PROXY_PORT = os.getenv("PROXY_PORT", "")
PROXY_USERNAME = os.getenv("PROXY_USERNAME", "")
PROXY_PASSWORD = os.getenv("PROXY_PASSWORD", "")


def _proxy_config() -> dict | None:
    """Playwright's launch(proxy=...) dict, or None if PROXY_HOST/PORT
    aren't both set — proxy is fully optional, never required to run."""
    if not PROXY_HOST or not PROXY_PORT:
        return None
    cfg = {"server": f"http://{PROXY_HOST}:{PROXY_PORT}"}
    if PROXY_USERNAME:
        cfg["username"] = PROXY_USERNAME
    if PROXY_PASSWORD:
        cfg["password"] = PROXY_PASSWORD
    return cfg


# ---------------------------------------------------------------------------
# Anti-detection: vanilla headless Chromium (no user-agent override, no
# viewport, navigator.webdriver=true, empty plugins list) is commonly
# fingerprinted and served a near-empty challenge/block page instead of the
# real site — a live /debug-network run against two real RelianceDigital
# URLs came back with total_requests_seen=1 for BOTH (only the document
# itself, no scripts/XHR at all), which is exactly that symptom, not a
# per-page fluke. The same class of issue was already confirmed and fixed
# this same way for whatsapp_forwarder's WhatsApp Web automation earlier —
# applied here too, to every browser this service launches (not just
# /debug-network), since it's a systemic defense, not RelianceDigital-
# specific.
# ---------------------------------------------------------------------------
_REALISTIC_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)


def _apple_user_agent_for(browser) -> str:
    """
    Apple-specific User-Agent, used ONLY by _refresh_apple_cookies below —
    NOT _REALISTIC_USER_AGENT above, which every OTHER browser this
    service launches still uses unchanged (RelianceDigital/BigBasket/
    Zepto/Blinkit checks, /check-stock, /debug-network).

    2026-07-27 UPDATE: the Chrome/Edge VERSION here is now DERIVED from
    `browser.version` (the real, actually-running Chromium build's own
    reported version) rather than a hardcoded string. Original version:
    the Dockerfile's Playwright image sat pinned at v1.47.0 (Chromium
    129, Sept 2024) unchanged since this service was built, while this
    constant hardcoded "Chrome/150.0.0.0 ... Edg/150.0.0.0" — copied from
    a real captured working request — creating a growing, detectable
    engine-vs-claimed-identity mismatch: the TLS/JA3 fingerprint, HTTP/2
    behavior, and JS feature surface all still reflected the real frozen
    Chromium 129 engine no matter what the header claimed. Deriving the
    version here means bumping the Dockerfile's image tag in the future
    automatically keeps this claim honest, with no separate hardcoded
    number to remember to update. Still a custom desktop-shaped UA
    string, not the browser's own raw default (which announces
    "HeadlessChrome" — a strong, well-known bot signal real desktop
    browsers never send) — keeps the Edge-branded template from the real
    working capture, just with an always-accurate version number.

    checkers/apple.py's own sec-ch-ua header (sent later, on the bot
    service's httpx replay of whatever session this mints) derives its
    version the same way — from the User-Agent string actually being
    sent, not a second hardcoded constant — see its _sec_ch_ua_for.
    """
    chrome_version = browser.version  # e.g. "131.0.6778.85" — real, not guessed
    major = chrome_version.split(".")[0]
    return (
        f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/{chrome_version} Safari/537.36 Edg/{major}.0.0.0"
    )


_STEALTH_INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
window.chrome = window.chrome || { runtime: {} };
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
const originalQuery = window.navigator.permissions && window.navigator.permissions.query;
if (originalQuery) {
    window.navigator.permissions.query = (parameters) => (
        parameters.name === 'notifications'
            ? Promise.resolve({ state: Notification.permission })
            : originalQuery(parameters)
    );
}
"""


def _new_browser_and_context(pw, user_agent: str | Callable[[object], str] = _REALISTIC_USER_AGENT):
    """Launch a browser + context with the anti-detection measures above —
    shared by _render_page (/check-stock), _capture_network_calls
    (/debug-network), and _refresh_apple_cookies (with its own
    _apple_user_agent_for override) so all three get the same defenses,
    not just whichever endpoint happened to be under investigation when
    this was added.

    `user_agent` may be a plain string (every caller except the Apple
    refresh) or a callable taking the just-launched Browser and returning
    a string (_apple_user_agent_for) — the browser instance has to exist
    before its own real version can be read, so a callable is invoked
    AFTER launch but before the context (and therefore the UA) is
    created."""
    # --disable-blink-features=AutomationControlled (2026-07-27, found via
    # a working third-party Apple pickup monitor's own config — see
    # checkers/apple.py's investigation notes) — blocks the CDP-automation
    # signal at the browser-launch level, BEFORE any page ever loads.
    # Different from (and stronger than) _STEALTH_INIT_SCRIPT's
    # navigator.webdriver override below: that patches a JS property
    # AFTER a real browser already set it true, which some fingerprinting
    # can detect as a patched getter; this flag stops Chromium from ever
    # setting the underlying automation flag in the first place. Applied
    # to every browser this service launches (not just Apple) — a
    # systemic defense, not site-specific, same reasoning as every other
    # measure in this function.
    browser = pw.chromium.launch(
        headless=HEADLESS,
        proxy=_proxy_config(),
        args=["--disable-blink-features=AutomationControlled"],
    )
    resolved_user_agent = user_agent(browser) if callable(user_agent) else user_agent
    context = browser.new_context(
        viewport={"width": 1280, "height": 800},
        user_agent=resolved_user_agent,
        locale="en-US",
    )
    context.add_init_script(_STEALTH_INIT_SCRIPT)
    return browser, context


# ---------------------------------------------------------------------------
# Bandwidth optimization: block everything except what's needed to read the
# DOM/JS-injected stock signal. This is the whole point of self-hosting
# against a metered (GB-priced) proxy — a product page with full images can
# be 2-5MB; blocking image/font/media/stylesheet cuts that dramatically
# since we only ever read page.content(), never render anything visually.
# ---------------------------------------------------------------------------
_ALLOWED_RESOURCE_TYPES = {"document", "script", "xhr", "fetch"}


def _make_resource_blocker(stats: dict):
    """Returns a Playwright route handler bound to a per-request stats dict
    (allowed/blocked counts + bytes-so-far isn't available pre-response, so
    we count requests, not bytes) — logged after each check so bandwidth
    savings are visible, not just assumed."""

    def _handler(route):
        resource_type = route.request.resource_type
        if resource_type in _ALLOWED_RESOURCE_TYPES:
            stats["allowed"] = stats.get("allowed", 0) + 1
            route.continue_()
        else:
            stats["blocked"] = stats.get("blocked", 0) + 1
            route.abort()

    return _handler


# ---------------------------------------------------------------------------
# Stock detection — ported from checkers/iqoo.py and checkers/vivo.py
# (identical logic in both; both stores' probe found JSON-LD availability
# reliably differentiates OOS vs in-stock). Returns (in_stock, signal):
# in_stock is True/False for a confident read, None when nothing conclusive
# was found THIS attempt (the caller retries on None rather than defaulting
# to False — see MAX_RETRIES / _fetch_and_check).
# ---------------------------------------------------------------------------
_OOS_PATTERNS = [
    "out of stock", "sold out", "currently unavailable",
    "notify me", "coming soon", "temporarily unavailable",
]


def _offer_availability(offers) -> str:
    """Extract the first availability string from an 'offers' value that may
    be a single Offer dict, an AggregateOffer dict wrapping a nested offers
    list, or a plain list of Offer dicts. Returns "" when none is found."""
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


def check_iqoo_vivo_stock(soup: BeautifulSoup, html: str) -> tuple[bool | None, str]:
    html_lower = html.lower()

    # ── JSON-LD (primary, proven-reliable signal per the original probe) ──
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except Exception:
            continue
        for item in (data if isinstance(data, list) else [data]):
            if not isinstance(item, dict):
                continue
            avail = _offer_availability(item.get("offers", {}))
            if not avail:
                continue
            if "InStock" in avail:
                return True, f"JSON-LD offers.availability={avail!r}"
            if "OutOfStock" in avail or "Discontinued" in avail:
                return False, f"JSON-LD offers.availability={avail!r}"

    # ── Embedded JSON (fallback if JSON-LD is ever absent) ─────────────────
    for key in ('"in_stock":true', '"inStock":true', '"is_available":true', '"isAvailable":true'):
        if key in html:
            return True, f"embedded JSON key {key!r} present"
    for key in ('"in_stock":false', '"inStock":false', '"is_available":false', '"isAvailable":false'):
        if key in html:
            return False, f"embedded JSON key {key!r} present"

    # ── Explicit OOS text (last-resort negative signal) ─────────────────────
    for pattern in _OOS_PATTERNS:
        if pattern in html_lower:
            return False, f"OOS text pattern {pattern!r} found"

    return None, "no conclusive signal found on this attempt"


CHECKERS = {
    "iqoo": check_iqoo_vivo_stock,
    "vivo": check_iqoo_vivo_stock,
}


# ---------------------------------------------------------------------------
# Rendering + retry
# ---------------------------------------------------------------------------
_check_semaphore = threading.Semaphore(MAX_CONCURRENT_CHECKS)


def _render_page(url: str) -> str:
    """Launch a fresh, isolated browser for this one check (simplest
    possible isolation between requests — no shared state, no thread-safety
    concerns with Playwright's sync API — at the cost of ~1-2s browser
    startup per check, acceptable for a pilot's request volume). Bounded by
    _check_semaphore so at most MAX_CONCURRENT_CHECKS browsers run at once."""
    acquired = _check_semaphore.acquire(timeout=SLOT_WAIT_TIMEOUT_SECONDS)
    if not acquired:
        raise RuntimeError(
            f"too many concurrent checks (max {MAX_CONCURRENT_CHECKS}) — "
            f"timed out after {SLOT_WAIT_TIMEOUT_SECONDS}s waiting for a free slot"
        )
    try:
        with sync_playwright() as pw:
            browser, context = _new_browser_and_context(pw)
            try:
                page = context.new_page()
                stats: dict = {}
                page.route("**/*", _make_resource_blocker(stats))
                page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
                try:
                    page.wait_for_selector(
                        'script[type="application/ld+json"]', timeout=SIGNAL_WAIT_TIMEOUT_MS
                    )
                except PlaywrightTimeoutError:
                    logger.info(
                        f"[render] JSON-LD script tag didn't appear within "
                        f"{SIGNAL_WAIT_TIMEOUT_MS}ms for {url} — proceeding with "
                        f"whatever rendered (fallback signals may still catch it)"
                    )
                html = page.content()
                logger.info(
                    f"[render] {url}: {stats.get('allowed', 0)} requests allowed, "
                    f"{stats.get('blocked', 0)} blocked (image/font/media/stylesheet)"
                )
                return html
            finally:
                browser.close()
    finally:
        _check_semaphore.release()


def _fetch_and_check(url: str, store: str) -> dict:
    checker = CHECKERS[store]
    last_signal = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            html = _render_page(url)
        except Exception as exc:
            last_signal = f"page render failed: {exc}"
            logger.warning(f"[check-stock] attempt {attempt}/{MAX_RETRIES} render failed for {url}: {exc}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY_SECONDS)
            continue

        soup = BeautifulSoup(html, "html.parser")
        in_stock, signal = checker(soup, html)
        if in_stock is not None:
            logger.info(f"[check-stock] {url} -> in_stock={in_stock} ({signal}) on attempt {attempt}")
            return {"in_stock": in_stock, "signal": signal, "attempts": attempt}

        last_signal = signal
        logger.info(f"[check-stock] attempt {attempt}/{MAX_RETRIES}: {signal} for {url} — retrying")
        if attempt < MAX_RETRIES:
            time.sleep(RETRY_DELAY_SECONDS)

    logger.error(f"[check-stock] check failed after {MAX_RETRIES} attempts for {url}: {last_signal}")
    return {
        "in_stock": None,
        "signal": f"check failed after {MAX_RETRIES} attempts — last result: {last_signal}",
        "attempts": MAX_RETRIES,
    }


# ---------------------------------------------------------------------------
# /debug-network: record XHR/fetch calls made while loading a page, filtered
# to serviceability/stock-by-pincode-looking endpoints. For admin diagnostic
# use (RelianceDigital's stock signal appears to live behind a pincode-
# gated API call rather than in the page's own embedded JSON — see
# admin_handlers.py's /debugreliance on the main bot side).
# ---------------------------------------------------------------------------

_MAX_ALL_SEEN_REPORTED = 100  # cap the lightweight all-responses list


def _capture_network_calls(url: str, pincode: str) -> dict:
    """Launch an isolated browser, apply `pincode` as a cookie, load `url`,
    and record every XHR/fetch response whose URL contains one of
    _NETWORK_CAPTURE_KEYWORDS.

    The cookie is a best-effort guess at how pincode selection works on the
    target site — this sandbox has no live network access to confirm
    RelianceDigital's actual mechanism (cookie vs. localStorage vs. a UI
    widget the user has to type into and submit). If it turns out a cookie
    alone doesn't trigger the serviceability call, that itself is useful
    diagnostic information (matched_count will be 0 despite total_requests_
    seen being nonzero) — report back what's actually observed so this can
    be adjusted, same as every other live-tuning step this pilot has needed.

    Every step that can silently fail (navigation, the network-idle wait,
    reading page content, an individual response's body) is wrapped and
    logged into the returned "diagnostics" dict rather than left to fail
    invisibly — a prior live run came back with total_requests_seen=1 for
    two different real product pages, which pointed at either an
    unhandled navigation error or the page being served an anti-bot
    challenge instead of the real content; this makes either case visible
    in the response instead of just a suspiciously low count.

    Bounded by the same _check_semaphore /check-stock uses, so total
    concurrent browser instances across both endpoints stays capped."""
    acquired = _check_semaphore.acquire(timeout=SLOT_WAIT_TIMEOUT_SECONDS)
    if not acquired:
        raise RuntimeError(
            f"too many concurrent checks (max {MAX_CONCURRENT_CHECKS}) — "
            f"timed out after {SLOT_WAIT_TIMEOUT_SECONDS}s waiting for a free slot"
        )
    try:
        with sync_playwright() as pw:
            browser, context = _new_browser_and_context(pw)
            try:
                domain = urlparse(url).netloc
                if domain:
                    context.add_cookies([{
                        "name": "pincode", "value": pincode,
                        "domain": domain, "path": "/",
                    }])

                page = context.new_page()
                stats: dict = {}
                page.route("**/*", _make_resource_blocker(stats))

                diagnostics: dict = {
                    "goto_status": None,
                    "goto_error": None,
                    "final_url": None,
                    "page_title": None,
                    "page_crashed": False,
                    "networkidle_timed_out": False,
                    "networkidle_error": None,
                    "content_error": None,
                    "response_listener_errors": 0,
                    "html_length": None,
                    "html_snippet": None,
                }

                def _on_crash(_page):
                    diagnostics["page_crashed"] = True
                    logger.error(f"[debug-network] page CRASHED while loading {url}")

                page.on("crash", _on_crash)

                matched: list[dict] = []
                all_seen: list[dict] = []

                def _on_response(response):
                    try:
                        resource_type = response.request.resource_type
                    except Exception:
                        resource_type = "?"
                    all_seen.append({
                        "url": response.url, "status": response.status, "resource_type": resource_type,
                    })
                    try:
                        url_lower = response.url.lower()
                        if not any(kw in url_lower for kw in _NETWORK_CAPTURE_KEYWORDS):
                            return
                        try:
                            body = response.text()
                        except Exception as exc:
                            body = f"<could not read response body: {exc}>"
                        if body and len(body) > _MAX_BODY_CHARS:
                            body = body[:_MAX_BODY_CHARS] + f"...(truncated, {len(body)} chars total)"
                        matched.append({
                            "url": response.url,
                            "method": response.request.method,
                            "status": response.status,
                            "body": body,
                        })
                    except Exception as exc:
                        diagnostics["response_listener_errors"] += 1
                        logger.error(f"[debug-network] response listener error on {response.url}: {exc}")

                page.on("response", _on_response)

                try:
                    main_response = page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
                    if main_response is not None:
                        diagnostics["goto_status"] = main_response.status
                except Exception as exc:
                    diagnostics["goto_error"] = str(exc)
                    logger.error(f"[debug-network] page.goto failed for {url}: {exc}")

                try:
                    diagnostics["final_url"] = page.url
                except Exception as exc:
                    logger.error(f"[debug-network] could not read page.url for {url}: {exc}")
                try:
                    diagnostics["page_title"] = page.title()
                except Exception as exc:
                    logger.error(f"[debug-network] could not read page.title() for {url}: {exc}")

                if diagnostics["goto_error"] is None:
                    try:
                        page.wait_for_load_state("networkidle", timeout=SIGNAL_WAIT_TIMEOUT_MS)
                    except PlaywrightTimeoutError:
                        diagnostics["networkidle_timed_out"] = True
                        logger.info(f"[debug-network] networkidle wait timed out for {url}")
                    except Exception as exc:
                        diagnostics["networkidle_error"] = str(exc)
                        logger.error(f"[debug-network] networkidle wait raised for {url}: {exc}")

                try:
                    html = page.content()
                    diagnostics["html_length"] = len(html)
                    diagnostics["html_snippet"] = html[:500]
                except Exception as exc:
                    diagnostics["content_error"] = str(exc)
                    logger.error(f"[debug-network] page.content() failed for {url}: {exc}")

                logger.info(
                    f"[debug-network] {url}: {len(all_seen)} response(s) seen, "
                    f"{len(matched)} matched; diagnostics={diagnostics}"
                )
                return {
                    "matched_requests": matched,
                    "total_requests_seen": len(all_seen),
                    "matched_count": len(matched),
                    "all_responses_seen": all_seen[:_MAX_ALL_SEEN_REPORTED],
                    "all_responses_truncated": len(all_seen) > _MAX_ALL_SEEN_REPORTED,
                    "diagnostics": diagnostics,
                }
            finally:
                browser.close()
    finally:
        _check_semaphore.release()


# ---------------------------------------------------------------------------
# /debug-dom: _capture_network_calls' sibling for DOM STRUCTURE discovery
# rather than network traffic — built specifically to find Apple's real
# pincode/pickup-availability widget selectors (2026-07-27 investigation:
# fulfillment-messages never gets a validated Akamai session no matter
# what's sent to it, so the plan shifted to reading the RENDERED PAGE
# instead of replaying an API call — see checkers/apple.py's own notes).
# Neither this sandbox's own outbound fetches (WebFetch) nor the Wayback
# Machine can reach apple.com at all (both blocked outright), so real
# selectors can only come from THIS service's own already-working
# Playwright browser actually loading the page — same reasoning as
# _capture_network_calls existing for RelianceDigital's serviceability
# call instead of guessing at it, and the exact same "verify empirically,
# don't guess" lesson this investigation already learned once with the
# fulfillment-messages param set. Keyword-filtered rather than dumping
# the full DOM (which would be enormous and mostly noise) — every element
# whose data-autom/text/attributes mention pickup, fulfillment, store,
# zip, pincode, or location, since those are the only parts of the page
# actually relevant to this investigation.
# ---------------------------------------------------------------------------
_DOM_CAPTURE_JS = """() => {
    function truncate(s, n) {
        if (!s) return s;
        s = s.trim().replace(/\\s+/g, ' ');
        return s.length > n ? s.slice(0, n) + '...' : s;
    }
    function matchesKeywords(parts) {
        const joined = parts.filter(Boolean).join(' ');
        return /pickup|pick up|fulfil|store|location|zip|pin ?code/i.test(joined);
    }

    const dataAutomElements = Array.from(document.querySelectorAll('[data-autom]'))
        .map(el => ({
            tag: el.tagName.toLowerCase(),
            data_autom: el.getAttribute('data-autom'),
            text: truncate(el.textContent || '', 120),
            aria_label: el.getAttribute('aria-label'),
            aria_disabled: el.getAttribute('aria-disabled'),
        }))
        .filter(e => matchesKeywords([e.data_autom, e.text, e.aria_label]))
        .slice(0, 60);

    const pincodeLikeInputs = Array.from(document.querySelectorAll('input'))
        .map(el => ({
            placeholder: el.placeholder || null,
            aria_label: el.getAttribute('aria-label'),
            name: el.name || null,
            id: el.id || null,
            type: el.type || null,
            data_autom: el.getAttribute('data-autom'),
        }))
        .filter(e => matchesKeywords([e.placeholder, e.aria_label, e.name, e.id, e.data_autom]))
        .slice(0, 30);

    const pickupRelatedButtons = Array.from(document.querySelectorAll('button, a[role="button"], a'))
        .map(el => ({
            tag: el.tagName.toLowerCase(),
            data_autom: el.getAttribute('data-autom'),
            text: truncate(el.textContent || '', 80),
            aria_label: el.getAttribute('aria-label'),
        }))
        .filter(e => matchesKeywords([e.data_autom, e.text, e.aria_label]))
        .slice(0, 40);

    // Full outerHTML of every pickUpDetails element — added 2026-07-27
    // once a real capture confirmed this is the actual container Apple
    // renders pickup status into (data-autom="pickUpDetails"), so its
    // exact clickable structure (the "Check availability" trigger, most
    // likely) can be inspected directly instead of guessed at.
    const pickupDetailsHtml = Array.from(document.querySelectorAll('[data-autom="pickUpDetails"]'))
        .map(el => {
            const html = el.outerHTML || '';
            return html.length > 4000 ? html.slice(0, 4000) + '...(truncated)' : html;
        });

    return {
        page_title: document.title,
        data_autom_elements: dataAutomElements,
        pincode_like_inputs: pincodeLikeInputs,
        pickup_related_buttons: pickupRelatedButtons,
        pickup_details_html: pickupDetailsHtml,
    };
}"""


def _capture_dom_elements(url: str, click_text: str | None = None) -> dict:
    """Launch an isolated browser, load `url`, wait for it to settle, then
    run _DOM_CAPTURE_JS to pull out every keyword-matching element — see
    the module note above for why this exists. Uses _apple_user_agent_for
    when `url` is an apple.com page (matching what production would
    actually send), the default _REALISTIC_USER_AGENT otherwise, same as
    every other endpoint in this file.

    Every step that can silently fail (navigation, the network-idle wait,
    the DOM extraction itself) is captured into "diagnostics" rather than
    left to fail invisibly — mirrors _capture_network_calls' own
    reasoning exactly.

    `click_text` (2026-07-27): a real capture found no pincode input on
    initial page load, but DID find a rendered "Check availability" label
    inside pickUpDetails — consistent with the pincode field living behind
    a click-triggered modal/overlay, not on the page from the start. When
    given, this clicks the FIRST element whose text contains `click_text`
    (case-insensitive substring match, via Playwright's own text= locator
    engine) after the initial capture, waits for any resulting overlay to
    settle, then runs _DOM_CAPTURE_JS a SECOND time — returned as
    "after_click" alongside the original (now "before_click"-equivalent)
    top-level fields, so a modal that's appended to the DOM (not a
    same-origin iframe — those wouldn't be visible to this same
    querySelectorAll-based capture) becomes visible without guessing at
    its own selectors first."""
    acquired = _check_semaphore.acquire(timeout=SLOT_WAIT_TIMEOUT_SECONDS)
    if not acquired:
        raise RuntimeError(
            f"too many concurrent checks (max {MAX_CONCURRENT_CHECKS}) — "
            f"timed out after {SLOT_WAIT_TIMEOUT_SECONDS}s waiting for a free slot"
        )
    try:
        with sync_playwright() as pw:
            is_apple = "apple.com" in urlparse(url).netloc.lower()
            browser, context = _new_browser_and_context(
                pw, user_agent=_apple_user_agent_for if is_apple else _REALISTIC_USER_AGENT,
            )
            try:
                page = context.new_page()

                diagnostics: dict = {
                    "goto_status": None,
                    "goto_error": None,
                    "final_url": None,
                    "page_title": None,
                    "networkidle_timed_out": False,
                    "networkidle_error": None,
                    "extract_error": None,
                    "click_result": None,
                }

                try:
                    main_response = page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
                    if main_response is not None:
                        diagnostics["goto_status"] = main_response.status
                except Exception as exc:
                    diagnostics["goto_error"] = str(exc)
                    logger.error(f"[debug-dom] page.goto failed for {url}: {exc}")

                try:
                    diagnostics["final_url"] = page.url
                    diagnostics["page_title"] = page.title()
                except Exception as exc:
                    logger.error(f"[debug-dom] could not read final_url/page_title for {url}: {exc}")

                if diagnostics["goto_error"] is None:
                    try:
                        page.wait_for_load_state("networkidle", timeout=SIGNAL_WAIT_TIMEOUT_MS)
                    except PlaywrightTimeoutError:
                        diagnostics["networkidle_timed_out"] = True
                        logger.info(f"[debug-dom] networkidle wait timed out for {url}")
                    except Exception as exc:
                        diagnostics["networkidle_error"] = str(exc)
                        logger.error(f"[debug-dom] networkidle wait raised for {url}: {exc}")

                # Same dwell as the reference implementation that inspired
                # this whole investigation used (3s) — enough for the page's
                # own JS to finish rendering pickup-availability state.
                page.wait_for_timeout(3000)

                result = {
                    "page_title": None, "data_autom_elements": [],
                    "pincode_like_inputs": [], "pickup_related_buttons": [],
                    "pickup_details_html": [],
                }
                try:
                    result = page.evaluate(_DOM_CAPTURE_JS)
                except Exception as exc:
                    diagnostics["extract_error"] = str(exc)
                    logger.error(f"[debug-dom] DOM extraction failed for {url}: {exc}")

                after_click = None
                if click_text:
                    click_result: dict = {
                        "attempted": True, "found": False,
                        "clicked_element_text": None, "click_error": None,
                        "post_click_extract_error": None,
                    }
                    try:
                        locator = page.get_by_text(click_text, exact=False).first
                        locator.wait_for(timeout=5000)
                        click_result["found"] = True
                        try:
                            click_result["clicked_element_text"] = locator.inner_text()
                        except Exception:
                            pass
                        locator.click(timeout=5000)
                    except Exception as exc:
                        click_result["click_error"] = str(exc)
                        logger.error(f"[debug-dom] click on text={click_text!r} failed for {url}: {exc}")
                    else:
                        # Bounded, best-effort — a modal opening doesn't
                        # necessarily trigger new network activity, so a
                        # networkidle timeout here is expected/harmless,
                        # not an error (same reasoning as the initial
                        # page-load wait above).
                        try:
                            page.wait_for_load_state("networkidle", timeout=SIGNAL_WAIT_TIMEOUT_MS)
                        except Exception:
                            pass
                        page.wait_for_timeout(3000)
                        try:
                            after_click = page.evaluate(_DOM_CAPTURE_JS)
                        except Exception as exc:
                            click_result["post_click_extract_error"] = str(exc)
                            logger.error(f"[debug-dom] post-click DOM extraction failed for {url}: {exc}")
                    diagnostics["click_result"] = click_result

                logger.info(
                    f"[debug-dom] {url}: {len(result.get('data_autom_elements', []))} data-autom "
                    f"match(es), {len(result.get('pincode_like_inputs', []))} input match(es), "
                    f"{len(result.get('pickup_related_buttons', []))} button match(es); "
                    f"diagnostics={diagnostics}"
                )
                return {**result, "after_click": after_click, "diagnostics": diagnostics}
            finally:
                browser.close()
    finally:
        _check_semaphore.release()


# ---------------------------------------------------------------------------
# /refresh-apple-cookies: launch a real headless-Chromium session, load an
# Apple product page, and trigger the SAME fulfillment-messages request a
# real visitor's own pickup-availability check performs — from INSIDE the
# page's own JS context via page.evaluate(fetch(...)), not a separate
# out-of-band request. Because it runs same-origin from a page that's
# already loaded and passed apple.com's own anti-bot checks, it carries the
# real browser's TLS fingerprint, headers, and cookies exactly the way
# Apple's own frontend JS would — this is the whole point of doing cookie
# acquisition via Playwright instead of the main bot's plain httpx calls
# (httpx's TLS fingerprint doesn't match a real browser's, which is part of
# why a manually-pasted cookie/UA pair was found dying after ~2 hours; see
# the main bot's checkers/apple.py and config.py for that history).
#
# Deliberately does NOT try to click through Apple's pickup-availability UI
# widget (a "See availability" link + pincode input + submit button) — that
# DOM structure isn't something this sandbox can verify live, and a
# selector guess would be fragile. Instead it reproduces exactly what that
# UI would do once submitted: call fulfillment-messages with the page's own
# SKU and the given pincode. Same endpoint, same query params
# (checkers/apple.py's _build_fulfillment_target), same authentication
# (page-context fetch = real browser Cookie/UA), without depending on
# unconfirmed CSS selectors.
# ---------------------------------------------------------------------------

_APPLE_SKU_PATTERN = re.compile(r'"partNumber"\s*:\s*"([A-Z0-9]{5,14}/[A-Z]{1,3})"')


def _extract_apple_sku(html: str) -> str | None:
    m = _APPLE_SKU_PATTERN.search(html)
    return m.group(1) if m else None


def _fulfillment_messages_url(product_url: str, sku: str, pincode: str) -> str | None:
    """Builds the same fulfillment-messages URL checkers/apple.py's
    _build_fulfillment_target does, deriving the locale-prefixed /shop/ path
    from the product URL itself (".../in/shop/buy-iphone/..." -> "/in/shop/
    fulfillment-messages") rather than hardcoding "/in/", so this works for
    any Apple storefront locale. Returns None if the product URL doesn't
    look like an apple.com /shop/ page at all.

    Param set verified 2026-07-27 against a live Chrome DevTools capture of
    a working request — see checkers/apple.py's _build_fulfillment_target
    for the full story: `little`/`mts.1`/`fts` are gone from Apple's
    current frontend request, replaced by `pl=true`. Keeping this in sync
    with that function matters here specifically — this URL is what
    pincode_check_confirmed's own validation probe below fetches, so a
    stale param set here would keep reporting "unconfirmed" even after the
    real checker in checkers/apple.py is fixed."""
    parsed = urlparse(product_url)
    segments = [s for s in parsed.path.split("/") if s]
    if "shop" not in segments:
        return None
    shop_idx = segments.index("shop")
    locale_prefix = "/".join(segments[:shop_idx])
    path = f"/{locale_prefix}/shop/fulfillment-messages" if locale_prefix else "/shop/fulfillment-messages"
    params = {
        "fae": "true", "pl": "true", "mts.0": "regular",
        "parts.0": sku, "location": pincode,
    }
    return f"{parsed.scheme}://{parsed.netloc}{path}?{urlencode(params)}"


_FULFILLMENT_FETCH_JS = """async (target) => {
    const resp = await fetch(target, {
        credentials: 'include',
        headers: { 'Accept': '*/*', 'x-skip-redirect': 'true' },
    });
    const text = await resp.text();
    return { status: resp.status, body: text.slice(0, 2000) };
}"""


def _abck_diagnostics(cookies_list: list[dict]) -> dict:
    """
    2026-07-27 — _abck's raw VALUE (not just its name/presence, unlike
    every other cookie in the jar — see _refresh_apple_cookies' own
    cookie_names diagnostic) is surfaced here specifically. Safe to:
    _abck is Akamai Bot Manager's own anti-bot sensor/telemetry marker,
    not an account/session credential — it grants no access to anything
    on apple.com by itself, unlike the real session cookies (dssid2,
    as_atb, mbox, etc.) that make up the rest of this jar, which stay
    name-only for exactly that reason.

    Motivation: akamai_markers_present only proves the cookie EXISTS, not
    that Akamai actually validated the session behind it — a cookie can
    be present but still reflect an unvalidated/rejected state. Community
    reverse-engineering of Akamai's _abck format (not officially
    published by Akamai, not verified against Apple's specific
    configuration — treat as a lead to check, not a confirmed fact)
    commonly describes it as roughly
    "<sensor_uid>~<validation_flag>~<payload>~<flag>~<flag>", with the
    SECOND field (right after the first "~") reportedly "-1" when the
    sensor's data hasn't been accepted/validated yet, vs. a different
    value once it has. Returned split on "~" so that field is directly
    visible per refresh cycle instead of guessing from presence alone.

    Returns {"abck_value_raw": str|None, "abck_segments": list[str]|None}
    — both None if no _abck cookie was captured at all this cycle.
    """
    abck_cookie = next((c for c in cookies_list if c["name"] == "_abck"), None)
    return {
        "abck_value_raw": abck_cookie["value"] if abck_cookie else None,
        "abck_segments": abck_cookie["value"].split("~") if abck_cookie else None,
    }


def _refresh_apple_cookies(url: str, pincode: str) -> dict:
    """Loads `url`, extracts its SKU, performs an in-page fetch of
    fulfillment-messages for `pincode` (see module note above), and returns
    the resulting session:
      {"cookies": "name=value; name2=value2; ...",
       "user_agent": <the UA this browser context used>,
       "pincode_check_confirmed": bool,  # fulfillment-messages returned a
                                          # 200 with a valid JSON body
       "diagnostics": {...}}
    cookies/user_agent are still returned even when pincode_check_confirmed
    is False (e.g. SKU extraction failed, or the fetch itself errored) —
    the page load alone still mints real anti-bot/session cookies, which
    may still be usable; the caller (the main bot) can decide whether an
    unconfirmed refresh is worth storing. Never raises for anything short
    of a hard Playwright/browser failure (navigation errors, missing SKU,
    and fetch failures are all captured in diagnostics instead).

    Deliberately slower than /check-stock's _render_page: diagnostic
    logging (see akamai_markers_present below) confirmed an earlier
    version of this function — which captured cookies immediately after
    domcontentloaded, blocked images/fonts/stylesheets, and closed the
    browser right after the fetch — produced a session with NONE of
    Akamai Bot Manager's own marker cookies (_abck/bm_sz/ak_bmsc) present
    at all. Akamai never validated the session in the first place, which
    is almost certainly why it died in ~9 minutes rather than the ~2
    hours a manually-copied, actively-used real browser session showed.
    This version gives that validation JS room to actually run: a
    bounded networkidle wait, a dwell period BEFORE the fulfillment
    fetch (not just before cookie extraction), no resource blocking (so
    every subresource the real page would load, loads), and a brief
    keep-alive after the fetch before the browser is torn down."""
    acquired = _check_semaphore.acquire(timeout=SLOT_WAIT_TIMEOUT_SECONDS)
    if not acquired:
        raise RuntimeError(
            f"too many concurrent checks (max {MAX_CONCURRENT_CHECKS}) — "
            f"timed out after {SLOT_WAIT_TIMEOUT_SECONDS}s waiting for a free slot"
        )
    try:
        with sync_playwright() as pw:
            browser, context = _new_browser_and_context(pw, user_agent=_apple_user_agent_for)
            try:
                page = context.new_page()
                # NO resource blocker here (unlike /check-stock and
                # /debug-network, which still use _make_resource_blocker
                # unchanged) — every image/font/stylesheet the real page
                # would load is allowed through, so this page load looks
                # as close to a real visitor's as possible to Akamai's own
                # fingerprinting, not a suspiciously partial one.

                diagnostics: dict = {
                    # The REAL, ground-truth bundled Chromium version this
                    # specific refresh actually ran on — added 2026-07-27
                    # alongside the Docker image version bump + dynamic
                    # User-Agent derivation (see _apple_user_agent_for's own
                    # note) so every future refresh's diagnostics directly
                    # confirm what engine minted THIS session, right next to
                    # akamai_markers_present/pincode_check_confirmed in the
                    # same log line — no need to separately verify a deploy
                    # landed or parse a version out of a UA string by hand.
                    "chromium_version": browser.version,
                    "goto_status": None,
                    "goto_error": None,
                    "networkidle_timed_out": False,
                    "networkidle_error": None,
                    "sku_extracted": None,
                    "fulfillment_url": None,
                    "fulfillment_status": None,
                    "fulfillment_error": None,
                }

                try:
                    main_response = page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
                    if main_response is not None:
                        diagnostics["goto_status"] = main_response.status
                except Exception as exc:
                    diagnostics["goto_error"] = str(exc)
                    logger.error(f"[refresh-apple-cookies] page.goto failed for {url}: {exc}")

                # Bounded, best-effort — NOT the goto's own wait_until (a
                # hard networkidle gate risks the goto itself timing out on
                # a page with ongoing background activity, e.g. an
                # analytics beacon that never truly goes idle; mirrors the
                # identical pattern in _capture_network_calls above). A
                # timeout here just means "proceed with whatever loaded".
                if diagnostics["goto_error"] is None:
                    try:
                        page.wait_for_load_state("networkidle", timeout=APPLE_REFRESH_NETWORKIDLE_TIMEOUT_MS)
                    except PlaywrightTimeoutError:
                        diagnostics["networkidle_timed_out"] = True
                        logger.info(f"[refresh-apple-cookies] networkidle wait timed out for {url} — proceeding anyway")
                    except Exception as exc:
                        diagnostics["networkidle_error"] = str(exc)
                        logger.error(f"[refresh-apple-cookies] networkidle wait raised for {url}: {exc}")

                # Dwell BEFORE the fulfillment fetch (not just before cookie
                # extraction) — the fetch is the ONE request whose resulting
                # session actually matters, so Akamai's sensor/telemetry JS
                # needs this time to run before that request, not after it.
                page.wait_for_timeout(APPLE_REFRESH_DWELL_MS)

                html = ""
                try:
                    html = page.content()
                except Exception as exc:
                    logger.error(f"[refresh-apple-cookies] page.content() failed for {url}: {exc}")

                sku = _extract_apple_sku(html)
                diagnostics["sku_extracted"] = sku

                pincode_check_confirmed = False
                if sku:
                    fulfillment_url = _fulfillment_messages_url(url, sku, pincode)
                    diagnostics["fulfillment_url"] = fulfillment_url
                    if fulfillment_url:
                        try:
                            result = page.evaluate(_FULFILLMENT_FETCH_JS, fulfillment_url)
                            status = result.get("status")
                            diagnostics["fulfillment_status"] = status
                            if status == 200:
                                try:
                                    json.loads(result.get("body", ""))
                                    pincode_check_confirmed = True
                                except Exception:
                                    diagnostics["fulfillment_error"] = "non-JSON response body"
                            else:
                                diagnostics["fulfillment_error"] = f"HTTP {status}"
                        except Exception as exc:
                            diagnostics["fulfillment_error"] = str(exc)
                            logger.warning(
                                f"[refresh-apple-cookies] in-page fulfillment-messages "
                                f"fetch failed for {url}: {exc}"
                            )
                    else:
                        diagnostics["fulfillment_error"] = "product URL is not an apple.com /shop/ page"
                else:
                    diagnostics["fulfillment_error"] = "could not extract SKU/part number from product page"

                # Keep the context alive a bit longer after the fetch before
                # snapshotting cookies and closing the browser — any
                # post-fetch Set-Cookie rotation or sensor callback the page
                # triggers in response to that request gets a chance to land
                # instead of being cut off by an immediate browser.close().
                try:
                    page.evaluate("() => document.title")
                except Exception as exc:
                    logger.warning(f"[refresh-apple-cookies] post-fetch keep-alive evaluate failed for {url}: {exc}")
                page.wait_for_timeout(APPLE_REFRESH_POST_FETCH_WAIT_MS)

                cookies_list = context.cookies()
                cookie_header = "; ".join(f"{c['name']}={c['value']}" for c in cookies_list)

                # Diagnostic only — NAMES, never values (this Cookie header is
                # a live Apple session; values shouldn't land in Railway
                # logs). Added to investigate a session dying in ~9 minutes
                # (far faster than the ~2-hour manual-cookie pattern this
                # replaced) — _abck/bm_sz/ak_bmsc are Akamai Bot Manager's
                # own marker cookie names; their ABSENCE, or presence in an
                # under-validated state, would support the hypothesis that
                # this flow captures cookies before Akamai's own sensor/
                # telemetry JS has had time to validate the session (no dwell
                # time between page load and the in-page fetch, and the
                # browser closes immediately after — see the module note
                # above _refresh_apple_cookies). Not yet acted on — this is
                # observation only until the actual log output is reviewed.
                diagnostics["cookie_names"] = sorted(c["name"] for c in cookies_list)
                diagnostics["akamai_markers_present"] = {
                    name: (name in diagnostics["cookie_names"])
                    for name in ("_abck", "bm_sz", "ak_bmsc")
                }

                # 2026-07-27 — _abck's raw VALUE (not just its name/presence,
                # unlike every other cookie above — see cookie_names) is
                # logged here specifically. Safe to: _abck is Akamai Bot
                # Manager's own anti-bot sensor/telemetry marker, not an
                # account/session credential — it grants no access to
                # anything on apple.com by itself, unlike the real session
                # cookies (dssid2, as_atb, mbox, etc.) that make up the rest
                # of this jar, which stay name-only for exactly that reason.
                #
                # Motivation: akamai_markers_present above only proves the
                # cookie EXISTS, not that Akamai actually validated the
                # session behind it — a cookie can be present but still
                # reflect an unvalidated/rejected state. Community reverse-
                # engineering of Akamai's _abck format (not officially
                # published by Akamai, not verified against Apple's specific
                # configuration — treat as a lead to check, not a confirmed
                # fact) commonly describes it as roughly
                # "<sensor_uid>~<validation_flag>~<payload>~<flag>~<flag>",
                # with the SECOND field (right after the first "~")
                # reportedly "-1" when the sensor's data hasn't been
                # accepted/validated yet, vs. a different value once it has.
                # Logged split on "~" so that field is directly visible per
                # refresh cycle instead of guessing from presence alone.
                diagnostics.update(_abck_diagnostics(cookies_list))

                logger.info(
                    f"[refresh-apple-cookies] {url}: {len(cookies_list)} cookie(s) captured, "
                    f"pincode_check_confirmed={pincode_check_confirmed}, diagnostics={diagnostics}"
                )

                return {
                    "cookies": cookie_header,
                    # Recomputed from the same (already-launched) browser
                    # instance rather than a stored constant — a pure,
                    # deterministic function of browser.version, so this
                    # is guaranteed identical to whatever was actually
                    # applied to the context above.
                    "user_agent": _apple_user_agent_for(browser),
                    "pincode_check_confirmed": pincode_check_confirmed,
                    "diagnostics": diagnostics,
                }
            finally:
                browser.close()
    finally:
        _check_semaphore.release()


# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------

def create_app() -> Flask:
    app = Flask(__name__)

    @app.route("/check-stock", methods=["POST"])
    def check_stock():
        data = request.get_json(silent=True) or {}
        url = (data.get("url") or "").strip()
        store = (data.get("store") or "").strip().lower()
        if not url or not store:
            return jsonify({"error": "url and store are required"}), 400
        if store not in CHECKERS:
            return jsonify({
                "error": f"unsupported store {store!r}",
                "supported_stores": sorted(CHECKERS),
            }), 400

        result = _fetch_and_check(url, store)
        return jsonify({"url": url, "store": store, **result}), 200

    @app.route("/debug-network", methods=["POST"])
    def debug_network():
        data = request.get_json(silent=True) or {}
        url = (data.get("url") or "").strip()
        pincode = (data.get("pincode") or "").strip() or DEFAULT_DEBUG_PINCODE
        if not url:
            return jsonify({"error": "url is required"}), 400

        try:
            result = _capture_network_calls(url, pincode)
        except Exception as exc:
            logger.error(f"[debug-network] failed for {url}: {exc}")
            return jsonify({"url": url, "pincode": pincode, "error": str(exc)}), 502

        return jsonify({"url": url, "pincode": pincode, **result}), 200

    @app.route("/debug-dom", methods=["POST"])
    def debug_dom():
        data = request.get_json(silent=True) or {}
        url = (data.get("url") or "").strip()
        click_text = (data.get("click_text") or "").strip() or None
        if not url:
            return jsonify({"error": "url is required"}), 400

        try:
            result = _capture_dom_elements(url, click_text=click_text)
        except Exception as exc:
            logger.error(f"[debug-dom] failed for {url}: {exc}")
            return jsonify({"url": url, "error": str(exc)}), 502

        return jsonify({"url": url, **result}), 200

    @app.route("/refresh-apple-cookies", methods=["POST"])
    def refresh_apple_cookies():
        if INTERNAL_REFRESH_TOKEN:
            supplied = request.headers.get("X-Internal-Token", "")
            if supplied != INTERNAL_REFRESH_TOKEN:
                return jsonify({"error": "unauthorized"}), 401

        data = request.get_json(silent=True) or {}
        url = (data.get("url") or "").strip()
        pincode = (data.get("pincode") or "").strip() or DEFAULT_DEBUG_PINCODE
        if not url:
            return jsonify({"error": "url is required"}), 400

        try:
            result = _refresh_apple_cookies(url, pincode)
        except Exception as exc:
            logger.error(f"[refresh-apple-cookies] failed for {url}: {exc}")
            return jsonify({"url": url, "pincode": pincode, "error": str(exc)}), 502

        return jsonify({"url": url, "pincode": pincode, **result}), 200

    @app.route("/health", methods=["GET"])
    def health():
        # playwright_version: the installed pip package version (cheap —
        # reads package metadata, no browser launch) — added 2026-07-27 to
        # make a Railway deploy's actual version directly verifiable via a
        # single GET instead of having to infer it from log lines. Kept in
        # lockstep with the Dockerfile's base image tag (see requirements.txt
        # — the two are pinned together deliberately), so this is strong
        # evidence of whether a Docker-image version bump actually deployed,
        # without needing the heavier confirmation of an actual browser
        # launch (see /refresh-apple-cookies's own chromium_version in its
        # diagnostics for that — the real, ground-truth bundled engine
        # version, from a browser that's already being launched anyway for
        # that endpoint's real work).
        try:
            playwright_version = importlib.metadata.version("playwright")
        except Exception:
            playwright_version = None
        return jsonify({
            "ok": True,
            "max_concurrent_checks": MAX_CONCURRENT_CHECKS,
            "proxy_configured": _proxy_config() is not None,
            "supported_stores": sorted(CHECKERS),
            "apple_cookie_refresh_auth_required": bool(INTERNAL_REFRESH_TOKEN),
            "playwright_version": playwright_version,
        })

    return app


def main() -> None:
    app = create_app()
    from waitress import serve
    logger.info(f"[http] serving on 0.0.0.0:{PORT} (max_concurrent_checks={MAX_CONCURRENT_CHECKS})")
    serve(app, host="0.0.0.0", port=PORT, threads=MAX_CONCURRENT_CHECKS + 2)


if __name__ == "__main__":
    main()
