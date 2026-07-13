import logging
import time
from urllib.parse import urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup

from .common import build_scraper_url, HEADERS

logger = logging.getLogger(__name__)

# Documentation-only (not read by any code — see stock_checker._JS_SITES
# for the actual render=true/false switch on the fallback page fetch).
NEEDS_JS = True

_JS_ENDPOINT_TIMEOUT = 20.0

_ADD_PATTERNS = ["add to cart", "buy now"]
_NOTIFY_ONLY_PATTERN = "notify me"


def _js_endpoint_url(url: str) -> str:
    """ShopAtSC is a Shopify store — appending '.js' to a product URL's path
    (preserving any query string) returns Shopify's lightweight JSON product
    view, e.g. https://shopatsc.com/products/foo?variant=1 ->
    https://shopatsc.com/products/foo.js?variant=1."""
    parts = urlsplit(url)
    path = parts.path if parts.path.endswith(".js") else parts.path.rstrip("/") + ".js"
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, parts.fragment))


async def check_via_js_endpoint(url: str) -> bool | None:
    """
    Primary signal: Shopify's '<product>.js' JSON endpoint, which carries an
    "available" boolean with no HTML parsing needed — the most reliable
    signal when it works. Returns True/False when the endpoint is reachable
    and returns valid JSON with an "available" key, or None if it fails,
    isn't reachable, or doesn't look like Shopify's JSON — signaling the
    caller (stock_checker.check_stock) to fall back to a normally-rendered
    page fetch + check() instead. Routed through Scrape.do (render_js=False
    — this is a JSON API response, not a page needing browser rendering)
    rather than a direct fetch, consistent with every other checker in this
    codebase.
    """
    js_url = _js_endpoint_url(url)
    try:
        scraper_url = build_scraper_url(js_url, render_js=False)
        async with httpx.AsyncClient(
            headers=HEADERS, follow_redirects=True, timeout=_JS_ENDPOINT_TIMEOUT
        ) as client:
            resp = await client.get(scraper_url)
        if resp.status_code != 200:
            logger.info(f"[shopatsc] .js endpoint HTTP {resp.status_code} — falling back to page fetch")
            return None
        data = resp.json()
    except Exception as exc:
        logger.info(f"[shopatsc] .js endpoint failed ({exc!r}) — falling back to page fetch")
        return None

    if not isinstance(data, dict) or "available" not in data:
        logger.info("[shopatsc] .js endpoint returned no usable 'available' key — falling back to page fetch")
        return None

    available = bool(data["available"])
    logger.info(f"[shopatsc] .js endpoint available={available!r} → {available} (primary signal)")
    return available


def _visible_text(html: str) -> str:
    text_soup = BeautifulSoup(html, "html.parser")
    for tag in text_soup(["script", "style"]):
        tag.decompose()
    return text_soup.get_text(" ", strip=True)


def check(soup: BeautifulSoup, html: str) -> bool:
    """
    Fallback path only — used when check_via_js_endpoint() (the primary
    signal, tried first by stock_checker.check_stock) returned None.
    Operates on a normally-rendered (Scrape.do render=true) page fetch:
    an active "Add to cart"/"Buy Now" affordance in the visible text means
    in stock; a lone "Notify Me" affordance with no "Add to cart" present
    means out of stock. Defaults to out of stock when neither is found.
    """
    visible_text = _visible_text(html).lower()

    if any(p in visible_text for p in _ADD_PATTERNS):
        logger.info("[shopatsc] fallback page: add-to-cart/buy-now text found → True (in stock)")
        return True

    if _NOTIFY_ONLY_PATTERN in visible_text:
        logger.info("[shopatsc] fallback page: 'notify me' found, no add-to-cart → False (out of stock)")
        return False

    logger.info("[shopatsc] fallback page: no conclusive signal → defaulting OUT OF STOCK (False)")
    return False


async def debug_check(url: str) -> dict:
    """
    Diagnostic version of the two-stage shopatsc flow (.js endpoint first,
    HTML-scrape fallback second) for the /debugsonyofficial admin command
    (admin_handlers.py) — NOT used by the live check_stock() path (which
    calls check_via_js_endpoint()/check() directly and only cares about
    the final bool). Runs through the exact same logic but captures rich
    diagnostics at each stage — success/failure and why, the raw signal
    value, whether the fallback fired, and total elapsed time — instead
    of collapsing straight to a bool, so a failure/fallback/slowness can
    be diagnosed from a single command without touching production code.
    """
    start = time.monotonic()
    result: dict = {
        "url": url,
        "js_endpoint_url": None,
        "js_success": False,
        "js_status_code": None,
        "js_error": None,
        "js_raw_available": None,
        "used_fallback": False,
        "fallback_status_code": None,
        "fallback_error": None,
        "signal": None,
        "in_stock": None,
        "elapsed_seconds": None,
    }

    js_url = _js_endpoint_url(url)
    result["js_endpoint_url"] = js_url

    try:
        scraper_url = build_scraper_url(js_url, render_js=False)
        async with httpx.AsyncClient(
            headers=HEADERS, follow_redirects=True, timeout=_JS_ENDPOINT_TIMEOUT
        ) as client:
            resp = await client.get(scraper_url)
        result["js_status_code"] = resp.status_code
        if resp.status_code != 200:
            result["js_error"] = f"HTTP {resp.status_code}"
        else:
            try:
                data = resp.json()
            except Exception as exc:
                result["js_error"] = f"response was not valid JSON: {exc}"
                data = None
            if data is not None:
                if not isinstance(data, dict) or "available" not in data:
                    result["js_error"] = "JSON parsed but no 'available' key present"
                else:
                    result["js_success"] = True
                    result["js_raw_available"] = data["available"]
    except httpx.TimeoutException as exc:
        result["js_error"] = f"timeout: {exc}"
    except Exception as exc:
        result["js_error"] = f"{type(exc).__name__}: {exc}"

    if result["js_success"]:
        in_stock = bool(result["js_raw_available"])
        result["in_stock"] = in_stock
        result["signal"] = f".js endpoint 'available' field = {result['js_raw_available']!r}"
        result["elapsed_seconds"] = time.monotonic() - start
        return result

    # .js endpoint didn't work — fall back to the normal render=true page fetch.
    result["used_fallback"] = True
    try:
        fallback_scraper_url = build_scraper_url(url, render_js=True)
        async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=60.0) as client:
            resp = await client.get(fallback_scraper_url)
        result["fallback_status_code"] = resp.status_code
        html = resp.text
    except httpx.TimeoutException as exc:
        result["fallback_error"] = f"timeout: {exc}"
        result["elapsed_seconds"] = time.monotonic() - start
        return result
    except Exception as exc:
        result["fallback_error"] = f"{type(exc).__name__}: {exc}"
        result["elapsed_seconds"] = time.monotonic() - start
        return result

    visible_text = _visible_text(html).lower()
    matched_add = next((p for p in _ADD_PATTERNS if p in visible_text), None)
    if matched_add:
        result["in_stock"] = True
        result["signal"] = f"fallback page text matched add-pattern {matched_add!r}"
    elif _NOTIFY_ONLY_PATTERN in visible_text:
        result["in_stock"] = False
        result["signal"] = f"fallback page text matched {_NOTIFY_ONLY_PATTERN!r}, no add-to-cart pattern found"
    else:
        result["in_stock"] = False
        result["signal"] = "fallback page: no add-pattern or 'notify me' text found — defaulted to False"

    result["elapsed_seconds"] = time.monotonic() - start
    return result
