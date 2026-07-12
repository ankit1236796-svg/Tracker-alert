"""Shared utilities for all site checkers."""

import logging
import os
from urllib.parse import urlparse, urlencode

from config import SUPPORTED_SITES

logger = logging.getLogger(__name__)

SCRAPEDO_API_URL = "https://api.scrape.do/"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-IN,en;q=0.9",
}


def detect_site(url: str) -> str | None:
    host = urlparse(url).netloc.lower().replace("www.", "")
    for site_key, domains in SUPPORTED_SITES.items():
        for domain in domains:
            if host == domain or host.endswith("." + domain):
                return site_key
    return None


def build_scraper_url(
    url: str,
    render_js: bool = False,
    set_cookies: str | None = None,
    custom_headers: bool = False,
    wait_until: str | None = None,
    custom_wait_ms: int | None = None,
) -> str:
    # Read at call time so Railway's runtime env var is always used,
    # regardless of when this module was first imported.
    token = os.environ.get("SCRAPEDO_KEY", "")
    params = {
        "token": token,
        "url": url,
        "geoCode": "in",
    }
    if render_js:
        params["render"] = "true"
    if set_cookies:
        params["setCookies"] = set_cookies
    if custom_headers:
        # Scrape.do's "Custom Headers" feature: when set, it forwards every
        # header on the request TO Scrape.do straight through to the target
        # site, instead of using its own defaults. Needed for endpoints that
        # require caller-supplied auth headers (e.g. Croma's authenticated
        # inventory API) rather than a plain page fetch.
        params["customHeaders"] = "true"
    if wait_until:
        # Scrape.do's Puppeteer-backed render wait condition (e.g.
        # "networkidle0") — only meaningful alongside render_js=True. Opt-in,
        # unused by every existing call site, so this is a no-op for them.
        params["waitUntil"] = wait_until
    if custom_wait_ms:
        # Extra fixed wait (ms) after waitUntil fires — a fallback buffer for
        # pages whose JS keeps mutating the DOM after the network goes idle.
        # Also opt-in/unused by existing call sites.
        params["customWait"] = str(custom_wait_ms)
    return f"{SCRAPEDO_API_URL}?{urlencode(params)}"
