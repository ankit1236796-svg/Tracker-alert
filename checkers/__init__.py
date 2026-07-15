from .common import detect_site, build_scraper_url, fetch_page, HEADERS, fetch_with_502_retry
from . import amazon, flipkart, zepto, bigbasket, blinkit, croma, instamart, myntra
from . import jiomart, reliancedigital, apple, oneplus, tataneu, vivo, iqoo
from . import unicornstore, vijaysales, inventstore, sangeethamobiles, shopatsc
# flipkart_api: an ALTERNATIVE data source for Flipkart (Affiliate API
# instead of scraping) — deliberately NOT added to CHECKER_MAP below, so
# it stays completely unwired from the live check cycle (see
# checkers/flipkart_api.py's module docstring and admin_handlers.py's
# /debugflipkartapi, the verification step this needs first).
from . import flipkart_api

CHECKER_MAP = {
    "amazon":           amazon.check,
    "flipkart":         flipkart.check,
    "zepto":            zepto.check,
    "bigbasket":        bigbasket.check,
    "blinkit":          blinkit.check,
    "croma":            croma.check,
    "instamart":        instamart.check,
    "myntra":           myntra.check,
    "jiomart":          jiomart.check,
    "reliancedigital":  reliancedigital.check,
    "apple":            apple.check,
    "oneplus":          oneplus.check,
    "tataneu":          tataneu.check,
    "vivo":             vivo.check,
    "iqoo":             iqoo.check,
    "unicornstore":     unicornstore.check,
    "vijaysales":       vijaysales.check,
    "inventstore":      inventstore.check,
    "sangeethamobiles": sangeethamobiles.check,
    "shopatsc":         shopatsc.check,
}

# Sites that expose a price extractor alongside their stock checker.
# Used by check_stock() to return the current price alongside the bool result.
PRICE_EXTRACTOR_MAP = {
    "amazon": amazon.extract_price,
}

__all__ = [
    "detect_site", "build_scraper_url", "fetch_page", "HEADERS", "fetch_with_502_retry",
    "CHECKER_MAP", "PRICE_EXTRACTOR_MAP",
]
