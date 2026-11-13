"""
proxy_client.py
~~~~~~~~~~~~~~~
Third fetch provider ("proxy") next to Zyte (zyte_client.py) and Scrape.do
(checkers/common.py's build_scraper_url path). A plain httpx GET routed
through a rotating residential proxy taken from the PROXY_URL env var
(full URL including credentials, e.g. http://user:pass@host:port).

Returns a genuine httpx.Response — the same shape the other providers
return — so every caller's resp.text / resp.status_code /
resp.raise_for_status() keeps working unchanged.

Scope: only Amazon and Flipkart use this provider (see
provider_control.SWITCHABLE_SITES). It cannot render JavaScript, so it
refuses render_js / browser-action requests instead of silently degrading.

SECRETS: the proxy URL (and its user/password) must never reach logs, chat
messages or exception text. Every error leaving this module goes through
redact(), and the original exception is NOT chained (`from None`) because
its message/traceback could embed the proxy URL.

Limitation (cannot be verified offline): httpx's TLS/HTTP fingerprint is not
a real browser's, so a residential IP alone may still get Robot-Check pages
from Amazon. Such pages are detected by fetch_guard and treated as
inconclusive, never as out-of-stock.
"""

from __future__ import annotations

import logging
import os
import time
from urllib.parse import unquote, urlparse

logger = logging.getLogger(__name__)

# httpx only speaks http(s) proxies out of the box; socks5 needs the
# httpx[socks] extra, which this project does not install.
_ALLOWED_SCHEMES = ("http", "https")

# Realistic desktop-Chrome navigation headers. Accept-Encoding deliberately
# omits "br"/"zstd": httpx only decodes those when optional packages are
# installed, and an undecodable body would look like a garbage/blocked page.
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "en-IN,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Cache-Control": "max-age=0",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}


class ProxyFetchError(Exception):
    """A proxy-provider failure. The message is already redacted."""


def _raw_url() -> str:
    # Read at call time (like SCRAPEDO_KEY / ZYTE_API_KEY) so an env change
    # takes effect without an import-order dependency.
    return os.environ.get("PROXY_URL", "").strip()


def proxy_configured() -> bool:
    """True iff PROXY_URL is set to a usable http(s) proxy URL. No side
    effects, never logs the value."""
    raw = _raw_url()
    if not raw:
        return False
    try:
        parsed = urlparse(raw)
        _ = parsed.port  # raises ValueError on a malformed port
    except ValueError:
        return False
    return parsed.scheme in _ALLOWED_SCHEMES and bool(parsed.hostname)


def redact(text: object) -> str:
    """Strip the proxy URL, username and password from `text`. Safe to call
    on any string (including when PROXY_URL is unset)."""
    out = str(text)
    raw = _raw_url()
    if not raw:
        return out
    out = out.replace(raw, "***")
    try:
        parsed = urlparse(raw)
        secrets = {parsed.username, parsed.password}
        secrets |= {unquote(s) for s in secrets if s}
    except ValueError:
        secrets = set()
    for secret in secrets:
        # A very short secret would mangle unrelated text; the full URL has
        # already been replaced above in that case.
        if secret and len(secret) >= 4:
            out = out.replace(secret, "***")
    return out


async def fetch_page(
    url: str,
    render_js: bool = False,
    set_cookies: str | None = None,
    custom_headers: bool = False,
    wait_until: str | None = None,
    custom_wait_ms: int | None = None,
    super_proxy: bool = False,
    play_with_browser: list[dict] | None = None,
    timeout: float = 60.0,
    site: str | None = None,
):
    """GET `url` through the rotating proxy and return the httpx.Response.

    Parameter names mirror zyte_client.fetch_page / checkers.common.fetch_page
    so it is a drop-in provider. A fresh AsyncClient (= fresh connection) is
    used for every request so a rotating gateway can hand out a new IP.
    Raises ProxyFetchError (redacted) on network/proxy failures; a non-2xx
    response from the target is returned as-is for the caller's
    raise_for_status().
    """
    import httpx  # imported lazily so the pure helpers above stay importable

    if render_js or play_with_browser or wait_until or custom_wait_ms or custom_headers:
        raise NotImplementedError(
            "proxy provider is plain HTTP only: render_js / browser actions / "
            "wait_until / custom_headers are not supported"
        )
    if not proxy_configured():
        raise ProxyFetchError("PROXY_URL is not configured (or is not a valid http(s) proxy URL)")

    headers = dict(BROWSER_HEADERS)
    if set_cookies:
        headers["Cookie"] = set_cookies  # same "k=v; k2=v2" convention as the other providers

    started = time.monotonic()
    try:
        async with httpx.AsyncClient(
            proxy=_raw_url(), headers=headers, follow_redirects=True, timeout=timeout,
        ) as client:
            response = await client.get(url)
    except Exception as exc:  # noqa: BLE001 — everything is re-raised redacted
        elapsed = time.monotonic() - started
        message = redact(f"{type(exc).__name__}: {exc}")
        logger.warning(f"[proxy] GET {url!r} failed after {elapsed:.1f}s: {message}")
        raise ProxyFetchError(message) from None

    logger.info(
        f"[proxy] GET {url!r} -> HTTP {response.status_code}, "
        f"{len(response.content)} bytes in {time.monotonic() - started:.1f}s"
    )
    return response
