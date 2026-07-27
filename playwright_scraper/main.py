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

import json
import logging
import os
import re
import threading
import time
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


def _new_browser_and_context(pw):
    """Launch a browser + context with the anti-detection measures above —
    shared by _render_page (/check-stock) and _capture_network_calls
    (/debug-network) so both get the same defenses, not just whichever
    endpoint happened to be under investigation when this was added."""
    browser = pw.chromium.launch(headless=HEADLESS, proxy=_proxy_config())
    context = browser.new_context(
        viewport={"width": 1280, "height": 800},
        user_agent=_REALISTIC_USER_AGENT,
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
            browser, context = _new_browser_and_context(pw)
            try:
                page = context.new_page()
                # NO resource blocker here (unlike /check-stock and
                # /debug-network, which still use _make_resource_blocker
                # unchanged) — every image/font/stylesheet the real page
                # would load is allowed through, so this page load looks
                # as close to a real visitor's as possible to Akamai's own
                # fingerprinting, not a suspiciously partial one.

                diagnostics: dict = {
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

                logger.info(
                    f"[refresh-apple-cookies] {url}: {len(cookies_list)} cookie(s) captured, "
                    f"pincode_check_confirmed={pincode_check_confirmed}, diagnostics={diagnostics}"
                )

                return {
                    "cookies": cookie_header,
                    "user_agent": _REALISTIC_USER_AGENT,
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
        return jsonify({
            "ok": True,
            "max_concurrent_checks": MAX_CONCURRENT_CHECKS,
            "proxy_configured": _proxy_config() is not None,
            "supported_stores": sorted(CHECKERS),
            "apple_cookie_refresh_auth_required": bool(INTERNAL_REFRESH_TOKEN),
        })

    return app


def main() -> None:
    app = create_app()
    from waitress import serve
    logger.info(f"[http] serving on 0.0.0.0:{PORT} (max_concurrent_checks={MAX_CONCURRENT_CHECKS})")
    serve(app, host="0.0.0.0", port=PORT, threads=MAX_CONCURRENT_CHECKS + 2)


if __name__ == "__main__":
    main()
