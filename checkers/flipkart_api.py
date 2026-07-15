"""
checkers/flipkart_api.py
~~~~~~~~~~~~~~~~~~~~~~~~
Flipkart's own Affiliate API as an ALTERNATIVE data source for Flipkart
stock checks, instead of scraping product pages via Zyte/Scrape.do (see
checkers/flipkart.py + checkers/common.py's fetch_page). Controlled by
config.FLIPKART_SOURCE ("scraping" [default, today's unchanged behavior]
vs "api"). NOT wired into stock_checker.check_stock()'s live check cycle
yet — see admin_handlers.py's /debugflipkartapi, the verification step
this needs to go through first, following this codebase's standing "never
guess target-site/third-party-API behavior, verify via a debug command
and real results first" principle.

Endpoint / auth / response shape below are verified via web search
cross-referencing multiple independent sources — this sandbox has no live
network access to fetch affiliate.flipkart.com's own docs directly (a
direct WebFetch of them returns HTTP 403), the same verification approach
already used elsewhere in this codebase for third-party API surfaces (see
zyte_client.py's docstring for precedent):

  - Endpoint: GET https://affiliate-api.flipkart.net/affiliate/product/json
    with a query param "id=<FSN>". Confirmed via
    github.com/ritishgumber/flipkart-affiliate's actual source code (the
    idSearch method's base URL) and its README's usage example
    (`idSearch({id: "PYJEGJJDZQ284MZS"})`), independently.
  - Auth headers: "Fk-Affiliate-Id" and "Fk-Affiliate-Token" — confirmed
    via Flipkart's own affiliate API docs (cross-referenced across
    multiple independent third-party wrapper READMEs/search snippets).
  - Response shape: a top-level "productBaseInfoV1" object containing
    "productId", "title", an "inStock" boolean, and price fields like
    "flipkartSellingPrice": {"currency": "INR", "amount": <float>} —
    confirmed via github.com/atemon/python-flipkart-affiliates-api's
    documented Product class fields, an independent source from the
    endpoint/header confirmation above.

NOT independently confirmed (flagged explicitly, lower confidence than
the above): whether any OTHER field (e.g. a separate shipping-info
"currentlyOutOfStock"-style flag) can override or refine "inStock" for
edge cases — no source available to this sandbox showed a full real
response for a single-product idSearch call specifically (only the
multi-product feed-listing shape and the documented Product class
field list were found). parse_availability() below relies SOLELY on
productBaseInfoV1.inStock, the one field two independent sources agree
on, rather than guessing at unconfirmed extras. /debugflipkartapi dumps
the FULL raw response specifically so this can be revisited with real
data.

The product id passed as id= is the FSN (Flipkart Serial Number) — the
SAME `pid` query-param value url_normalize._flipkart() already extracts
from a tracked Flipkart URL for cross-user dedup grouping, reused here
via extract_product_id() rather than re-implemented.
"""

import logging
import os

import httpx
from bs4 import BeautifulSoup

from url_normalize import _flipkart as _extract_pid

logger = logging.getLogger(__name__)

# Documentation-only, mirrors checkers/flipkart.py's own NEEDS_JS=False —
# irrelevant to the API path itself (no HTML/JS involved at all), only
# used by the scraping FALLBACK in check_stock_with_fallback below.
NEEDS_JS = False

_API_URL = "https://affiliate-api.flipkart.net/affiliate/product/json"
_TIMEOUT = 20.0


def _credentials() -> tuple[str, str]:
    # Read at call time (mirrors checkers/common.py's SCRAPEDO_KEY and
    # zyte_client.py's ZYTE_API_KEY pattern) so a Railway env var change
    # takes effect without an import-order dependency on this module's
    # own import time.
    return (
        os.environ.get("FLIPKART_AFFILIATE_ID", ""),
        os.environ.get("FLIPKART_AFFILIATE_TOKEN", ""),
    )


def extract_product_id(url: str) -> str | None:
    """
    The Flipkart product id (FSN) the Affiliate API's id= param expects —
    reuses url_normalize's own Flipkart `pid` extractor (the SAME
    high-confidence pattern already trusted for cross-user dedup grouping
    in bot.run_stock_check_cycle) rather than re-implementing URL parsing
    here. Returns None if the URL carries no pid= query param (e.g. some
    share links) — callers should fall back to scraping in that case,
    exactly like url_normalize's own dedup fallback does for the same
    condition.
    """
    return _extract_pid(url)


class FlipkartApiError(Exception):
    """Raised for any Affiliate API failure — missing credentials, auth
    error, product not in the affiliate feed, rate limit, network error,
    or an unexpected response shape. Callers should catch this and fall
    back to Zyte/Scrape.do scraping (see check_stock_with_fallback)."""


async def fetch_product(product_id: str) -> dict:
    """
    Calls the Affiliate API for `product_id` (a Flipkart FSN) and returns
    the raw parsed JSON response. Raises FlipkartApiError on ANY failure —
    never returns a partial/guessed result, so the caller can cleanly fall
    back to scraping instead of silently trusting a broken response.
    """
    aff_id, aff_token = _credentials()
    if not aff_id or not aff_token:
        raise FlipkartApiError(
            "FLIPKART_AFFILIATE_ID / FLIPKART_AFFILIATE_TOKEN not set"
        )

    headers = {"Fk-Affiliate-Id": aff_id, "Fk-Affiliate-Token": aff_token}
    params = {"id": product_id}

    logger.info(f"[flipkart_api] GET {_API_URL} id={product_id!r}")
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(_API_URL, headers=headers, params=params)
    except Exception as exc:
        raise FlipkartApiError(f"network error: {exc}") from exc

    logger.info(f"[flipkart_api] status={resp.status_code}")
    if resp.status_code in (401, 403):
        raise FlipkartApiError(f"auth error: HTTP {resp.status_code} — check credentials")
    if resp.status_code == 404:
        raise FlipkartApiError(
            "product not found (HTTP 404) — likely not in Flipkart's affiliate catalog feed"
        )
    if resp.status_code == 429:
        raise FlipkartApiError("rate limited (HTTP 429)")
    if resp.status_code != 200:
        raise FlipkartApiError(f"unexpected HTTP {resp.status_code}: {resp.text[:200]!r}")

    try:
        data = resp.json()
    except Exception as exc:
        raise FlipkartApiError(f"non-JSON response: {resp.text[:200]!r}") from exc

    return data


def parse_availability(data: dict) -> tuple[bool, float | None, str | None]:
    """
    Extract (in_stock, price, title) from a raw fetch_product() response.
    Raises FlipkartApiError if the expected productBaseInfoV1.inStock
    field is missing — an unexpected response shape falls back to
    scraping rather than guesses, exactly like every other failure mode
    fetch_product() already raises for.
    """
    base_info = data.get("productBaseInfoV1")
    if not isinstance(base_info, dict) or "inStock" not in base_info:
        raise FlipkartApiError(
            f"unexpected response shape — no productBaseInfoV1.inStock field "
            f"(top-level keys={list(data.keys())})"
        )

    in_stock = bool(base_info["inStock"])
    title = base_info.get("title")

    price = None
    selling_price = base_info.get("flipkartSellingPrice")
    if isinstance(selling_price, dict) and "amount" in selling_price:
        try:
            price = float(selling_price["amount"])
        except (TypeError, ValueError):
            price = None

    return in_stock, price, title


async def check_stock_via_api(url: str) -> tuple[bool, float | None]:
    """
    High-level API-only entry point: extract the FSN from `url`, call the
    Affiliate API, and return (in_stock, price). Raises FlipkartApiError
    on any failure — no fallback here; see check_stock_with_fallback for
    the version that automatically falls back to scraping.
    """
    product_id = extract_product_id(url)
    if not product_id:
        raise FlipkartApiError(f"could not extract a Flipkart product id (pid) from url={url!r}")
    data = await fetch_product(product_id)
    in_stock, price, _title = parse_availability(data)
    return in_stock, price


async def check_stock_with_fallback(
    url: str, source: str | None = None,
) -> tuple[bool, float | None, str]:
    """
    Returns (in_stock, price, method) where method is "api" or "scraping"
    — whichever actually produced the result, so a caller (or the debug
    command) can tell which path ran. `source` defaults to
    config.FLIPKART_SOURCE; pass it explicitly to override for testing.

    source == "api": tries the Affiliate API first (check_stock_via_api).
    On ANY FlipkartApiError (missing credentials, auth error, product not
    in the affiliate feed, rate limit, network error, unexpected response
    shape), automatically falls back to the scraping path below.

    source == "scraping" (or anything else): skips the API entirely.

    The scraping fallback calls checkers.common.fetch_page +
    checkers.flipkart.check() directly — the SAME functions
    stock_checker.check_stock() uses today for flipkart in production —
    rather than importing stock_checker itself, so this module stays a
    leaf (no risk of a circular import once this IS wired into
    stock_checker.py in a future round).
    """
    # Deferred imports: avoids a module-load-time dependency on config's
    # FLIPKART_SOURCE default changing after this module is imported, and
    # keeps this a leaf module relative to checkers.flipkart/common (no
    # circularity risk if stock_checker.py imports THIS module later).
    from config import FLIPKART_SOURCE
    from . import flipkart as flipkart_checker
    from .common import fetch_page

    source = (source or FLIPKART_SOURCE).strip().lower()

    if source == "api":
        try:
            in_stock, price = await check_stock_via_api(url)
            return in_stock, price, "api"
        except FlipkartApiError as exc:
            logger.warning(f"[flipkart_api] API call failed, falling back to scraping: {exc}")

    resp = await fetch_page(url, render_js=flipkart_checker.NEEDS_JS, site="flipkart")
    html = resp.text
    soup = BeautifulSoup(html, "html.parser")
    in_stock = flipkart_checker.check(soup, html)
    return in_stock, None, "scraping"
