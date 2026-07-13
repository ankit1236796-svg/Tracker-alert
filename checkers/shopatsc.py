import logging
import time

import httpx
from bs4 import BeautifulSoup

from .common import fetch_page

logger = logging.getLogger(__name__)

# This site does NOT participate in stock_checker._JS_SITES — it owns its
# own fetch logic, special-cased in stock_checker.check_stock() for
# "shopatsc".

# Scrape.do fetch timeout for the render=false/render=true tiers.
_RENDER_TIMEOUT = 30.0
# super=true (premium/residential proxy) requests can take noticeably
# longer than the default proxy tier — matches the longer timeout used
# for RelianceDigital's own super=true debug trials.
_SUPER_PROXY_TIMEOUT = 60.0

_ADD_PATTERNS = ["add to cart", "buy now"]
_NOTIFY_ONLY_PATTERN = "notify me"

# Minimum visible-text length considered "plausibly a real, fully-loaded
# product page". A fetch that comes back shorter than this (or failed
# outright) is treated as incomplete and escalated to the next tier.
_MIN_PLAUSIBLE_TEXT_LENGTH = 200


def _visible_text(html: str) -> str:
    text_soup = BeautifulSoup(html, "html.parser")
    for tag in text_soup(["script", "style"]):
        tag.decompose()
    return text_soup.get_text(" ", strip=True)


def _text_looks_incomplete(visible_text: str) -> bool:
    return len(visible_text) < _MIN_PLAUSIBLE_TEXT_LENGTH


def check(soup: BeautifulSoup, html: str) -> bool:
    """
    Sole stock-detection signal for ShopAtSC (Sony India's official PS5
    store). The Shopify '.js' JSON product endpoint's "available" field
    was confirmed unreliable for this store specifically — both a real
    in-stock and a real out-of-stock product returned available: true,
    most likely because ShopAtSC runs a separate "Notify Me" waitlist app
    that doesn't touch Shopify's native inventory tracking (which is what
    the .js endpoint actually reflects). Reliance on that endpoint has
    been removed entirely; detection is HTML-text-only: an active "Add to
    cart"/"Buy Now" affordance in the visible text means in stock; a lone
    "Notify Me" affordance with no "Add to cart"/"Buy Now" present means
    out of stock. Defaults to out of stock when neither is found.
    """
    visible_text = _visible_text(html).lower()

    if any(p in visible_text for p in _ADD_PATTERNS):
        logger.info("[shopatsc] add-to-cart/buy-now text found → True (in stock)")
        return True

    if _NOTIFY_ONLY_PATTERN in visible_text:
        logger.info("[shopatsc] 'notify me' found, no add-to-cart → False (out of stock)")
        return False

    logger.info("[shopatsc] no conclusive signal → defaulting OUT OF STOCK (False)")
    return False


async def _fetch_page(
    url: str, render_js: bool, super_proxy: bool = False, timeout: float = _RENDER_TIMEOUT
) -> httpx.Response:
    return await fetch_page(url, render_js=render_js, super_proxy=super_proxy, timeout=timeout, site="shopatsc")


async def check_via_html(url: str) -> bool:
    """
    Sole production fetch path for ShopAtSC. Goes straight to Scrape.do's
    super=true (premium/residential proxy), skipping render=false and
    render=true entirely — both were confirmed failing for this site
    (render=false: HTTP 502; render=true: timeout), the same symptom
    RelianceDigital hit before its own fix, and super=true is the same
    fix that resolved it there too. Since the underlying problem is
    proxy-IP reputation/blocking rather than missing JS-rendered content
    (ShopAtSC's product pages are largely server-rendered), render_js is
    NOT combined with super_proxy here — matching the RelianceDigital
    precedent of super=true alone as the working default.

    Deliberately does not retry render=false/render=true if super=true
    also fails — those two tiers are already known to fail for this
    site, so retrying them would only waste time before an inevitable
    failure. See debug_check() below for a diagnostic that still
    exercises all three tiers, in case Scrape.do's behavior for this
    site changes again in the future.
    """
    resp = await _fetch_page(url, render_js=False, super_proxy=True, timeout=_SUPER_PROXY_TIMEOUT)
    resp.raise_for_status()
    html = resp.text
    soup = BeautifulSoup(html, "html.parser")
    return check(soup, html)


async def debug_check(url: str) -> dict:
    """
    Diagnostic version of the full three-tier Scrape.do escalation
    (render=false, then render=true, then super=true) for the
    /debugsonyofficial admin command (admin_handlers.py) — NOT used by
    the live check_stock() path (which calls check_via_html() directly
    and skips straight to super=true, per the reasoning there). Runs each
    tier in order, stopping as soon as one produces a plausibly-complete
    page, and reports per-tier HTTP status/error/visible-text length/
    timing for every tier actually attempted, plus the final signal,
    verdict, and total elapsed time — so slowness or a tier recovering/
    regressing can be diagnosed from a single command without touching
    production code.
    """
    start = time.monotonic()
    result: dict = {
        "url": url,
        "render_false_status_code": None,
        "render_false_error": None,
        "render_false_visible_text_length": None,
        "render_false_looked_incomplete": None,
        "render_false_elapsed_seconds": None,
        "used_render_true": False,
        "render_true_status_code": None,
        "render_true_error": None,
        "render_true_visible_text_length": None,
        "render_true_looked_incomplete": None,
        "render_true_elapsed_seconds": None,
        "used_super_proxy": False,
        "super_proxy_status_code": None,
        "super_proxy_error": None,
        "super_proxy_visible_text_length": None,
        "super_proxy_elapsed_seconds": None,
        "signal": None,
        "in_stock": None,
        "total_elapsed_seconds": None,
    }

    # ── Tier 1: render=false ────────────────────────────────────────────
    stage_start = time.monotonic()
    html1 = None
    try:
        resp1 = await _fetch_page(url, render_js=False, timeout=_RENDER_TIMEOUT)
        result["render_false_status_code"] = resp1.status_code
        if resp1.status_code == 200:
            html1 = resp1.text
        else:
            result["render_false_error"] = f"HTTP {resp1.status_code}"
    except Exception as exc:
        result["render_false_error"] = f"{type(exc).__name__}: {exc}"
    result["render_false_elapsed_seconds"] = time.monotonic() - stage_start

    text1 = _visible_text(html1) if html1 is not None else ""
    result["render_false_visible_text_length"] = len(text1)
    tier1_incomplete = html1 is None or _text_looks_incomplete(text1)
    result["render_false_looked_incomplete"] = tier1_incomplete

    final_html = html1 if not tier1_incomplete else None

    # ── Tier 2: render=true (only if tier 1 was insufficient) ──────────
    if tier1_incomplete:
        result["used_render_true"] = True
        stage_start = time.monotonic()
        html2 = None
        try:
            resp2 = await _fetch_page(url, render_js=True, timeout=_RENDER_TIMEOUT)
            result["render_true_status_code"] = resp2.status_code
            if resp2.status_code == 200:
                html2 = resp2.text
            else:
                result["render_true_error"] = f"HTTP {resp2.status_code}"
        except Exception as exc:
            result["render_true_error"] = f"{type(exc).__name__}: {exc}"
        result["render_true_elapsed_seconds"] = time.monotonic() - stage_start

        text2 = _visible_text(html2) if html2 is not None else ""
        if html2 is not None:
            result["render_true_visible_text_length"] = len(text2)
        tier2_incomplete = html2 is None or _text_looks_incomplete(text2)
        result["render_true_looked_incomplete"] = tier2_incomplete

        final_html = html2 if not tier2_incomplete else None

        # ── Tier 3: super=true (only if tiers 1 AND 2 were insufficient) ──
        if tier2_incomplete:
            result["used_super_proxy"] = True
            stage_start = time.monotonic()
            html3 = None
            try:
                resp3 = await _fetch_page(
                    url, render_js=False, super_proxy=True, timeout=_SUPER_PROXY_TIMEOUT
                )
                result["super_proxy_status_code"] = resp3.status_code
                if resp3.status_code == 200:
                    html3 = resp3.text
                else:
                    result["super_proxy_error"] = f"HTTP {resp3.status_code}"
            except Exception as exc:
                result["super_proxy_error"] = f"{type(exc).__name__}: {exc}"
            result["super_proxy_elapsed_seconds"] = time.monotonic() - stage_start
            if html3 is not None:
                result["super_proxy_visible_text_length"] = len(_visible_text(html3))
            final_html = html3

    if final_html is None:
        result["signal"] = "all attempted tiers failed or looked incomplete"
        result["total_elapsed_seconds"] = time.monotonic() - start
        return result

    text_to_check = _visible_text(final_html).lower()
    matched_add = next((p for p in _ADD_PATTERNS if p in text_to_check), None)
    if matched_add:
        result["in_stock"] = True
        result["signal"] = f"matched add-pattern {matched_add!r}"
    elif _NOTIFY_ONLY_PATTERN in text_to_check:
        result["in_stock"] = False
        result["signal"] = f"matched {_NOTIFY_ONLY_PATTERN!r}, no add-to-cart pattern found"
    else:
        result["in_stock"] = False
        result["signal"] = "no add-pattern or 'notify me' text found — defaulted to False"

    result["total_elapsed_seconds"] = time.monotonic() - start
    return result
