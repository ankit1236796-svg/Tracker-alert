from bs4 import BeautifulSoup

from .common import generic_marketplace_check

# Documentation-only (not read by any code — see stock_checker._JS_SITES
# for the actual render=true/false switch).
NEEDS_JS = True

# vijaysales.com is a large multi-category consumer-electronics retailer
# (like Croma/Reliance Digital) — real markup hasn't been inspected from
# this sandbox (no live network access), so this starts from the same
# generic retail JSON-LD/embedded-JSON/OOS-text/button waterfall used for
# those sites (see checkers/reliancedigital.py). Tune against real product
# pages once verified.
_ADD_PATTERNS = ["add to cart", "add to bag", "buy now"]
_OOS_PATTERNS = [
    "out of stock", "sold out", "currently unavailable",
    "notify me when available", "notify me", "coming soon", "not available",
]


def check(soup: BeautifulSoup, html: str) -> bool:
    # BUGFIX: real /check results showed this site's final status inverted
    # — in-stock products were reported OOS and vice versa. The underlying
    # signal detection (JSON-LD/embedded-JSON/OOS-text/button waterfall,
    # shared with unicornstore/inventstore/sangeethamobiles via
    # generic_marketplace_check) is untouched; only the final boolean is
    # flipped here, scoped to vijaysales only.
    detected_in_stock = generic_marketplace_check(soup, html, _ADD_PATTERNS, _OOS_PATTERNS, "vijaysales")
    return not detected_in_stock
