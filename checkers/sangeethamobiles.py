from bs4 import BeautifulSoup

from .common import generic_marketplace_check

# Documentation-only (not read by any code — see stock_checker._JS_SITES
# for the actual render=true/false switch).
NEEDS_JS = True

# sangeethamobiles.com is a large mobile-phone retail chain (comparable in
# scale to Reliance Digital/Croma) — real markup hasn't been inspected
# from this sandbox (no live network access), so this starts from the
# generic retail JSON-LD/embedded-JSON/OOS-text/button waterfall (see
# checkers/reliancedigital.py). Tune against real product pages once
# verified.
_ADD_PATTERNS = ["add to cart", "add to bag", "buy now", "book now"]
_OOS_PATTERNS = [
    "out of stock", "sold out", "currently unavailable",
    "notify me when available", "notify me", "coming soon", "not available",
]


def check(soup: BeautifulSoup, html: str) -> bool:
    return generic_marketplace_check(soup, html, _ADD_PATTERNS, _OOS_PATTERNS, "sangeethamobiles")
