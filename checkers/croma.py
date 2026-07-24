"""
checkers/croma.py
~~~~~~~~~~~~~~~~~~
Croma's own internal inventory/order-promising API — a free, structured
JSON endpoint (the request/response terminology — promiseLines,
allocationRuleID, organizationCode, sourcingClassification — matches
Oracle Order Management/Commerce conventions, consistent with Croma
running on an Oracle Commerce-family stack) rather than scraping
croma.com's product pages via Zyte/Scrape.do. REPLACES the previous
HTML-scraping checker entirely — the old check() (JSON-LD/button/
delivery-restriction text matching) is deleted, not kept alongside this.
See config.py's SUPPORTED_SITES/UNRELIABLE_SITES history for why: the old
scraper was pulled from production after being observed flipping between
correct and fully-inverted results, then degrading to reporting every
product OOS, with no root cause ever found.

Endpoint / headers / payload shape as supplied directly for this task
(presumably captured via real browser network inspection) — NOT
independently re-verified against a live Croma request from this sandbox
(no live network access to croma.com). See the itemID note below for the
one part of this flow that IS a guess.

  POST https://api.croma.com/inventory/oms/v2/tms/details-pwa/
  Headers: accept, content-type, oms-apim-subscription-key (env
  CROMA_APIM_KEY, defaults to the key supplied for this task — expected to
  need rotation eventually, see check_via_api's 401/403 handling), origin,
  referer.
  Body: {"promise": {"allocationRuleID": "SYSTEM", "checkInventory": "Y",
  "organizationCode": "CROMA", "sourcingClassification": "EC",
  "promiseLines": {"promiseLine": [{"fulfillmentType": "HDEL",
  "itemID": <id>, "lineId": "1", "requiredQty": "1",
  "shipToAddress": {"zipCode": <pincode>}, "extn": {"widerStoreFlag": "N"}}]}}}

Stock signal: promise.suggestedOption.option.promiseLines.promiseLine is a
non-empty list -> deliverable/in stock at that pincode; empty or missing
-> out of stock. Every level of this path is walked defensively
(isinstance-guarded, tolerating a list where a dict is expected) rather
than assuming a fixed shape — mirrors this codebase's established handling
for other third-party JSON shapes seen to vary (e.g. checkers/apple.py's
_sku_from_offers, added after a real "'list' object has no attribute
'get'" crash on a similarly-shaped field).

NOT confirmed (flagged explicitly, unlike the endpoint/payload/response-
path above, which came directly from the task): how to derive `itemID`
from a tracked Croma product URL. There is no existing extractor for
Croma anywhere in this codebase (url_normalize.py's own comment already
notes Croma's URL id pattern "wasn't verified to high confidence"). This
module extracts it via a regex against the common `/p/<id>` trailing-
path-segment convention seen across many Indian e-commerce storefronts —
a BEST GUESS, not verified against a real croma.com URL from this
sandbox. If real /debugcroma runs show tracked URLs don't match this
pattern, extract_item_id() is the first place to fix.

Does NOT go through checkers.common.fetch_page / zyte_client.py at all —
a deliberate, direct httpx call, since this is Croma's own free
public-facing API, not a scrape needing Zyte/Scrape.do. Consequently this
checker spends ZERO Zyte/Scrape.do credits and is automatically absent
from database.get_zyte_usage_summary's per-site breakdown (that table is
only ever written to from inside zyte_client.fetch_page) — no
credit-tracking code needed or touched for this.
"""

import logging
import os
import re

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# This site does NOT participate in stock_checker._JS_SITES or go through
# checkers.common.fetch_page at all — it owns its own direct HTTP call,
# special-cased in stock_checker.check_stock() for "croma" (mirrors
# checkers/shopatsc.py's own check_via_html special-case).
NEEDS_JS = False

_INVENTORY_URL = "https://api.croma.com/inventory/oms/v2/tms/details-pwa/"
_TIMEOUT = 10.0

# Supplied directly for this task; may expire/rotate — see check_via_api's
# 401/403 handling, which logs a clear "key needs to be refreshed" message
# rather than silently guessing at stock status.
_DEFAULT_APIM_KEY = "1131858141634e2abe2efb2b3a2a2a5d"


def _apim_key() -> str:
    # Read at call time (mirrors checkers/common.py's SCRAPEDO_KEY and
    # zyte_client.py's ZYTE_API_KEY pattern) so a Railway env var change
    # takes effect without an import-order dependency on this module's own
    # import time.
    return os.environ.get("CROMA_APIM_KEY", "").strip() or _DEFAULT_APIM_KEY


# Best-guess extraction of Croma's product itemID from a tracked URL's
# trailing "/p/<id>" path segment — see this module's docstring for why
# this is flagged as unverified rather than confirmed.
_ITEM_ID_RE = re.compile(r"/p/([A-Za-z0-9]+)(?:[/?#]|$)")


def extract_item_id(url: str) -> str | None:
    m = _ITEM_ID_RE.search(url)
    return m.group(1) if m else None


def _promise_lines(data: dict) -> list:
    """
    Defensively walks promise -> suggestedOption -> option -> promiseLines
    -> promiseLine, tolerating any intermediate level being a list instead
    of a dict (or missing/None) rather than assuming a fixed shape and
    crashing — see this module's docstring for why.
    """
    promise = data.get("promise") if isinstance(data, dict) else None
    if not isinstance(promise, dict):
        return []

    suggested = promise.get("suggestedOption")
    if isinstance(suggested, list):
        suggested = suggested[0] if suggested else None
    if not isinstance(suggested, dict):
        return []

    option = suggested.get("option")
    if isinstance(option, list):
        option = option[0] if option else None
    if not isinstance(option, dict):
        return []

    promise_lines = option.get("promiseLines")
    if not isinstance(promise_lines, dict):
        return []

    lines = promise_lines.get("promiseLine")
    if isinstance(lines, list):
        return lines
    if isinstance(lines, dict):
        return [lines]
    return []


async def check_via_api(url: str, pincode: str | None) -> bool | None:
    """
    Sole production stock-check path for Croma, via its own internal
    inventory/order-promising API (see this module's docstring for the
    endpoint/payload/response details). Returns:
      True  - promiseLine is a non-empty list: deliverable at this pincode.
      False - promiseLine is empty/missing: a genuine, confirmed answer
              from a real API response — not a guess.
      None  - inconclusive: no pincode available, no itemID could be
              extracted from the URL, the API key was rejected (401/403 —
              logged clearly so it can be rotated), a network error/
              timeout, or a non-JSON/unexpected response. Never raises;
              the caller (stock_checker.check_stock()) must treat None as
              "skip this update", the same convention every other
              inconclusive-capable checker in this codebase follows (see
              checkers/apple.py, checkers/inventstore.py).
    """
    if not pincode:
        logger.warning(
            "[croma] no pincode set for this user — Croma's inventory API "
            "requires a real delivery pincode (shipToAddress.zipCode), "
            "cannot check without one. Use /pins to add one."
        )
        return None

    item_id = extract_item_id(url)
    if not item_id:
        logger.warning(f"[croma] could not extract an itemID from url={url!r}")
        return None

    headers = {
        "accept": "application/json",
        "content-type": "application/json",
        "oms-apim-subscription-key": _apim_key(),
        "origin": "https://www.croma.com",
        "referer": "https://www.croma.com/",
    }
    payload = {
        "promise": {
            "allocationRuleID": "SYSTEM",
            "checkInventory": "Y",
            "organizationCode": "CROMA",
            "sourcingClassification": "EC",
            "promiseLines": {
                "promiseLine": [
                    {
                        "fulfillmentType": "HDEL",
                        "itemID": item_id,
                        "lineId": "1",
                        "requiredQty": "1",
                        "shipToAddress": {"zipCode": pincode},
                        "extn": {"widerStoreFlag": "N"},
                    }
                ]
            },
        }
    }

    logger.info(f"[croma] POST {_INVENTORY_URL} itemID={item_id!r} pincode={pincode!r}")
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(_INVENTORY_URL, headers=headers, json=payload)
    except Exception as exc:
        logger.warning(f"[croma] inventory API request failed: {type(exc).__name__}: {exc}")
        return None

    logger.info(f"[croma] inventory API status={resp.status_code}")
    if resp.status_code in (401, 403):
        logger.error(
            f"[croma] inventory API returned HTTP {resp.status_code} — the "
            f"oms-apim-subscription-key has likely EXPIRED and needs to be "
            f"refreshed (set a new value for the CROMA_APIM_KEY env var). "
            f"Skipping this product for now."
        )
        return None
    if resp.status_code != 200:
        logger.warning(f"[croma] inventory API HTTP {resp.status_code}: {resp.text[:300]!r}")
        return None

    try:
        data = resp.json()
    except Exception:
        logger.warning(f"[croma] inventory API non-JSON response: {resp.text[:300]!r}")
        return None

    lines = _promise_lines(data)
    in_stock = len(lines) > 0
    logger.info(
        f"[croma] {url} → {'IN STOCK' if in_stock else 'OUT OF STOCK'} "
        f"({len(lines)} promiseLine(s) for pincode {pincode!r})"
    )
    return in_stock


def check(soup: BeautifulSoup, html: str) -> bool:
    """
    NOT used in production. checkers.croma's real stock-detection logic
    lives entirely in check_via_api() above (Croma's own inventory API) —
    special-cased in stock_checker.check_stock() for site=="croma", the
    exact same pattern checkers/shopatsc.py's check_via_html uses. This
    soup/html-based stub exists ONLY so CHECKER_MAP still has a non-None
    entry for "croma" (stock_checker.check_stock()'s `if checker is None`
    guard would otherwise short-circuit before ever reaching the "croma"
    special case) — mirrors checkers/shopatsc.py's own check() for the
    identical structural reason. Should never actually be invoked with
    real arguments in production.
    """
    logger.warning(
        "[croma] check(soup, html) called directly — this should never "
        "happen in production; checkers.croma.check_via_api (Croma's "
        "internal inventory API) is the real stock-detection path."
    )
    return False
