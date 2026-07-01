from .common import detect_site, build_scraper_url, HEADERS
from . import amazon, flipkart, zepto, bigbasket, blinkit, croma, instamart, myntra
from . import jiomart, reliancedigital, apple

CHECKER_MAP = {
    "amazon":          amazon.check,
    "flipkart":        flipkart.check,
    "zepto":           zepto.check,
    "bigbasket":       bigbasket.check,
    "blinkit":         blinkit.check,
    "croma":           croma.check,
    "instamart":       instamart.check,
    "myntra":          myntra.check,
    "jiomart":         jiomart.check,
    "reliancedigital": reliancedigital.check,
    "apple":           apple.check,
}

__all__ = ["detect_site", "build_scraper_url", "HEADERS", "CHECKER_MAP"]
