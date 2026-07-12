from bs4 import BeautifulSoup

from .common import generic_marketplace_check

# Documentation-only (not read by any code — see stock_checker._JS_SITES
# for the actual render=true/false switch).
NEEDS_JS = True

# inventstore.in's real markup hasn't been inspected from this sandbox (no
# live network access) — a small single-vendor gadget-reseller site, so
# Shopify's canonical button copy is included alongside generic retail
# phrases, same reasoning as checkers/unicornstore.py. Tune against real
# product pages once verified.
_ADD_PATTERNS = ["add to cart", "add to bag", "buy now", "pre-order"]
_OOS_PATTERNS = [
    "out of stock", "sold out", "currently unavailable",
    "notify me when available", "notify me", "coming soon",
]


def check(soup: BeautifulSoup, html: str) -> bool:
    return generic_marketplace_check(soup, html, _ADD_PATTERNS, _OOS_PATTERNS, "inventstore")
