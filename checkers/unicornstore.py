from bs4 import BeautifulSoup

from .common import generic_marketplace_check

# Documentation-only (not read by any code — see stock_checker._JS_SITES
# for the actual render=true/false switch).
NEEDS_JS = True

# shop.unicornstore.in's real markup hasn't been inspected from this
# sandbox (no live network access) — the "shop." subdomain and
# single-vendor gadget-reseller profile are consistent with a Shopify
# storefront, so Shopify's canonical button copy ("Sold out" / "Add to
# cart") is included alongside the generic retail phrases used elsewhere
# in this codebase (see checkers/reliancedigital.py). Tune against real
# product pages once verified — e.g. via a dedicated debug command
# following the /debugreliance precedent in admin_handlers.py.
_ADD_PATTERNS = ["add to cart", "add to bag", "buy now", "pre-order"]
_OOS_PATTERNS = [
    "out of stock", "sold out", "currently unavailable",
    "notify me when available", "notify me", "coming soon",
]


def check(soup: BeautifulSoup, html: str) -> bool:
    return generic_marketplace_check(soup, html, _ADD_PATTERNS, _OOS_PATTERNS, "unicornstore")
