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


def build_scraper_url(url: str, render_js: bool = False, set_cookies: str | None = None) -> str:
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
    return f"{SCRAPEDO_API_URL}?{urlencode(params)}"
