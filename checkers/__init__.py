from .common import detect_site, build_scraper_url, HEADERS
from . import amazon, flipkart, zepto, bigbasket

CHECKER_MAP = {
    "amazon": amazon.check,
    "flipkart": flipkart.check,
    "zepto": zepto.check,
    "bigbasket": bigbasket.check,
}

__all__ = ["detect_site", "build_scraper_url", "HEADERS", "CHECKER_MAP"]
