from .common import detect_site, build_scraper_url, HEADERS
from . import amazon, flipkart, zepto, bigbasket, blinkit, croma, instamart, myntra
from . import jiomart, reliancedigital, apple, oneplus, tataneu, vivo, iqoo
from . import unicornstore, vijaysales, inventstore, sangeethamobiles

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
}

# Sites that expose a price extractor alongside their stock checker.
# Used by check_stock() to return the current price alongside the bool result.
PRICE_EXTRACTOR_MAP = {
    "amazon": amazon.extract_price,
}

__all__ = ["detect_site", "build_scraper_url", "HEADERS", "CHECKER_MAP", "PRICE_EXTRACTOR_MAP"]
