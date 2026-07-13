"""
zyte_client.py
~~~~~~~~~~~~~~
Thin wrapper around Zyte API (https://api.zyte.com/v1/extract) — the
PRIMARY scraping provider now that Scrape.do's credits are exhausted (see
config.SCRAPING_PROVIDER and checkers/common.py's fetch_page(), the
central function every checker/debug command routes through). Scrape.do's
own code path (checkers/common.py's build_scraper_url + a plain GET) is
left fully intact and unused while SCRAPING_PROVIDER="zyte" — flip that
flag back to "scrapedo" the moment Scrape.do credits are recharged, no
code changes needed on either side.

Zyte's API is a single POST endpoint with a JSON body, not a GET-able URL
with query params like Scrape.do's, and it returns the fetched page's HTML
wrapped in a JSON envelope (base64-encoded for a non-JS fetch, a plain
string for a JS-rendered one) rather than as the raw HTTP response body.
fetch_page() below hides all of that and returns a genuine httpx.Response
synthesized from Zyte's reply, so every existing call site's
resp.text / resp.status_code / resp.json() / resp.raise_for_status() usage
keeps working completely unchanged, regardless of which provider actually
served the fetch.

Parameter mapping to Zyte's request fields — the render_js/super_proxy/
geolocation mapping below is verified against Zyte's own documentation
(docs.zyte.com/zyte-api/usage/reference.html, http.html, browser.html,
features.html, extract/actions.html) via web search, since this sandbox
has no live network access to fetch those pages directly (direct WebFetch
of docs.zyte.com returns HTTP 403; WebSearch results cross-confirmed the
same field names/shapes across multiple independent snippets, the same
verification approach already used elsewhere in this codebase for
third-party API surfaces — see checkers/common.py's playWithBrowser
comment for precedent):

  render_js=True    -> "browserHtml": true (Zyte opens a real headless
                        browser; the response's "browserHtml" field is the
                        rendered HTML as a plain string, no decoding needed)
  render_js=False    -> "httpResponseBody": true (no browser; the
                        response's "httpResponseBody" field is the raw
                        HTTP response body, base64-encoded)
  super_proxy=True   -> ALSO forces the browser tier on, even for an
                        otherwise render_js=False site. Zyte API does NOT
                        expose an explicit "premium/residential proxy"
                        toggle the way Scrape.do's super=true does — its
                        proxy selection and anti-bot handling are automatic
                        and managed server-side per request (confirmed via
                        Zyte's own "Automate Proxy Management" / proxy-mode
                        docs). Forcing the full browser tier is the closest
                        verified "try harder against a block" lever Zyte's
                        request fields actually expose, so that's what
                        super_proxy maps to here. Flagged explicitly: unlike
                        the mappings above, this is a best-effort SEMANTIC
                        equivalence, not a literal 1:1 parameter match —
                        worth revisiting once real Zyte responses for a
                        currently-blocked site (e.g. RelianceDigital,
                        ShopAtSC) can be compared against what Scrape.do's
                        super=true used to return for the same URL.
  geolocation         -> always "IN" (matches build_scraper_url's geoCode=in)
  play_with_browser   -> translated to Zyte's "actions" list — see
                        _translate_actions below. Only exercised today by
                        RelianceDigital's DEBUG-ONLY fetch_with_pincode_
                        interaction (admin_handlers.py's /debugreliance2);
                        nothing in the live check cycle uses this.
  custom_wait_ms      -> appended as a trailing {"action": "waitForTimeout",
                        "timeout": <seconds>} action (Zyte's own docs give a
                        `{"action": "waitForTimeout", "timeout": 0}` example
                        — seconds, not ms, hence the /1000 conversion below).
                        Forces the browser tier on if not already active,
                        same reasoning as super_proxy above: an ignored wait
                        request would silently change OnePlus/Unicorn
                        Store's tuned behavior.
  wait_until          -> NOT translatable — Zyte's browser automation has no
                        direct equivalent to Puppeteer's waitUntil strategy
                        (networkidle0 etc.). Accepted but logged and
                        ignored; browserHtml mode already waits for the
                        page to settle by default, and custom_wait_ms above
                        covers the "extra settle buffer" half of what
                        wait_until+custom_wait_ms was doing together.
  set_cookies         -> translated to Zyte's requestCookies field (Zyte's
                        own docs describe this as an "experimental"
                        feature). Only exercised today by /debugreliance's
                        optional pincode-cookie trial (admin_handlers.py) —
                        every current production site's set_cookies is None
                        (see stock_checker.py's _PINCODE_COOKIE_SITES, an
                        empty frozenset).
  custom_headers      -> NOT translated (no current call site passes
                        custom_headers=True in production code — the only
                        reference is a standalone, unused test script for
                        the long-retired Croma checker). Raises
                        NotImplementedError if ever actually requested,
                        rather than silently dropping a real requirement.
"""

import base64
import logging
import os
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

ZYTE_API_URL = "https://api.zyte.com/v1/extract"


def _api_key() -> str:
    # Read at call time (mirrors checkers/common.py's SCRAPEDO_KEY pattern)
    # so a Railway env var change takes effect without an import-order
    # dependency on this module's own import time.
    return os.environ.get("ZYTE_API_KEY", "")


def _translate_actions(play_with_browser: list[dict]) -> list[dict]:
    """
    Best-effort translation of this codebase's Scrape.do playWithBrowser
    action chain (Click/Fill/Wait/WaitSelector — see checkers/common.py's
    build_scraper_url) into Zyte API's own "actions" list. click and
    waitForSelector are confirmed field names/shapes from Zyte's docs; the
    exact field name for a Fill/type action's typed value ("text") and
    waitForTimeout's time unit (assumed seconds, per Zyte's own
    '"timeout": 0' example) are lower-confidence than the rest of this
    module and not independently confirmed beyond that one example — same
    "best guess now, verify via a debug command later" posture already
    used elsewhere in this codebase for target-site CSS selectors. Any
    unrecognized action type is logged and skipped rather than raising, so
    one untranslatable step doesn't abort the whole chain.
    """
    actions = []
    for step in play_with_browser:
        action = step.get("Action")
        if action == "Click":
            actions.append({
                "action": "click",
                "selector": {"type": "css", "value": step["Selector"]},
            })
        elif action == "Fill":
            actions.append({
                "action": "type",
                "selector": {"type": "css", "value": step["Selector"]},
                "text": step.get("Value", ""),
            })
        elif action == "Wait":
            actions.append({
                "action": "waitForTimeout",
                "timeout": max(step.get("Timeout", 0), 0) / 1000,  # ms -> seconds
            })
        elif action == "WaitSelector":
            actions.append({
                "action": "waitForSelector",
                "selector": {"type": "css", "value": step["WaitSelector"]},
            })
        else:
            logger.warning(f"[zyte] no translation for playWithBrowser action {action!r} — skipped")
    return actions


def _cookie_pairs(set_cookies: str, domain: str) -> list[dict]:
    """
    Parses this codebase's "key=value[; key2=value2...]" setCookies
    convention (see build_scraper_url's set_cookies param) into Zyte's
    requestCookies list — {"name", "value", "domain"} per cookie.
    """
    cookies = []
    for pair in set_cookies.split(";"):
        pair = pair.strip()
        if not pair or "=" not in pair:
            continue
        name, value = pair.split("=", 1)
        cookies.append({"name": name.strip(), "value": value.strip(), "domain": domain})
    return cookies


async def fetch_page(
    url: str,
    render_js: bool = False,
    super_proxy: bool = False,
    wait_until: str | None = None,
    custom_wait_ms: int | None = None,
    set_cookies: str | None = None,
    custom_headers: bool = False,
    play_with_browser: list[dict] | None = None,
    timeout: float = 60.0,
) -> httpx.Response:
    """
    Fetch `url` via Zyte API and return a genuine httpx.Response synthesized
    from its JSON reply — see this module's docstring for the full
    parameter mapping. Never silently swallows a failure: a non-2xx
    response from Zyte's OWN endpoint (auth failure, ban/520, rate limit,
    ...) becomes an httpx.Response carrying THAT status code and Zyte's
    raw error body, so resp.raise_for_status() / resp.status_code behave
    the same way a genuine failed fetch already did under Scrape.do. A
    network-level exception (timeout, connection error) is NOT caught here
    and propagates to the caller, exactly matching Scrape.do's own
    plain-httpx-GET behavior that every existing call site already handles.
    """
    if custom_headers:
        raise NotImplementedError(
            "zyte_client.fetch_page: custom_headers=True has no current "
            "production call site and no verified Zyte field mapping yet — "
            "add one (Zyte's customHttpRequestHeaders/requestHeaders, which "
            "field depends on request mode) before using this."
        )

    use_browser = render_js or super_proxy
    body: dict = {"url": url, "geolocation": "IN"}
    if use_browser:
        body["browserHtml"] = True
    else:
        body["httpResponseBody"] = True

    if wait_until:
        logger.info(
            f"[zyte] wait_until={wait_until!r} requested but Zyte API has no "
            f"direct equivalent to Puppeteer's waitUntil strategy — ignored "
            f"(browserHtml mode already waits for the page to settle by "
            f"default; see custom_wait_ms for an explicit extra buffer)."
        )

    actions = _translate_actions(play_with_browser) if play_with_browser else []
    if custom_wait_ms:
        actions.append({"action": "waitForTimeout", "timeout": max(custom_wait_ms, 0) / 1000})
    if actions:
        if not use_browser:
            # actions require the browser tier -- force it on rather than
            # silently dropping a caller-requested wait/interaction.
            use_browser = True
            body.pop("httpResponseBody", None)
            body["browserHtml"] = True
        body["actions"] = actions

    if set_cookies:
        body["requestCookies"] = _cookie_pairs(set_cookies, urlparse(url).netloc)

    api_key = _api_key()
    logger.info(f"[zyte] POST {ZYTE_API_URL} url={url!r} browser={use_browser} actions={len(actions)}")

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(ZYTE_API_URL, json=body, auth=(api_key, ""))

    request = httpx.Request("GET", url)
    if resp.status_code != 200:
        # Zyte's OWN endpoint failed outright (auth, rate limit, ban/520,
        # ...) -- surface THAT status/body rather than pretending the
        # target page was fetched.
        logger.warning(f"[zyte] request failed: HTTP {resp.status_code} {resp.text[:300]!r}")
        return httpx.Response(status_code=resp.status_code, content=resp.content, request=request)

    data = resp.json()
    target_status = data.get("statusCode", 200)
    if "browserHtml" in data:
        html = data["browserHtml"]
    elif "httpResponseBody" in data:
        html = base64.b64decode(data["httpResponseBody"]).decode("utf-8", errors="replace")
    else:
        logger.warning(f"[zyte] response had neither browserHtml nor httpResponseBody: keys={list(data.keys())}")
        html = ""

    logger.info(f"[zyte] target statusCode={target_status} html_length={len(html)}")
    return httpx.Response(status_code=target_status, content=html, request=request)
