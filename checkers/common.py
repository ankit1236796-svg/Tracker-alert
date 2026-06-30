"""Shared utilities for all site checkers."""

import logging
from urllib.parse import urlparse, urlencode

from config import SUPPORTED_SITES

logger = logging.getLogger(__name__)

SCRAPER_API_KEY = "b0dd6db778e2c40c3f705c01e06125f2"
SCRAPER_API_URL = "https://api.scraperapi.com/"

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


def build_scraper_url(url: str, render_js: bool = False) -> str:
    params = {
        "api_key": SCRAPER_API_KEY,
        "url": url,
        "country_code": "in",
    }
    if render_js:
        params["render"] = "true"
    return f"{SCRAPER_API_URL}?{urlencode(params)}"
