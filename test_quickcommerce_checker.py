"""
Manually verify the disabled-button-detection fix (bigbasket/zepto/blinkit/
croma) against a real product URL, using the EXACT production code path the
live bot uses — stock_checker.check_stock() — not a separate test-only
script.

Croma included here specifically to verify the BASIC (non-authenticated)
approach: Scrape.do + render=true fetching the public product page, same
pattern as the other 3 sites — as distinct from the authenticated
/details-pwa inventory endpoint (see test_croma_inventory_endpoint.py),
which is a different, separately-investigated dead end. This script exists
to confirm the basic path (already deployed, was never itself shown to
fail) still works, with a real 60s production timeout, not the 30s used
while testing the authenticated endpoint.

Not part of the app — bot.py never imports this.

This can't find product URLs or take screenshots itself (no live browsing
from where this was written — see the accompanying chat message). You
supply the URL; find it by browsing the site yourself and picking one
product you've visually confirmed OUT OF STOCK (disabled/greyed Add button)
and one you've visually confirmed IN STOCK.

For the clearest confirmation, right-click the Add/+ button in your browser
→ Inspect, and compare its class/disabled attribute against the
"cart button text=... class=... disabled_attr=... aria-disabled=... →
_is_disabled=..." line this script prints — that's the direct correlation
between what you see on the page and what the checker computed.

The site (bigbasket/zepto/blinkit/croma) is auto-detected from the URL —
just pass the product URL, nothing else.

Usage (run via `railway run python3 test_quickcommerce_checker.py`, wherever
SCRAPEDO_KEY is a real credential):

    python3 test_quickcommerce_checker.py <product_url>

    e.g.
    python3 test_quickcommerce_checker.py https://www.bigbasket.com/pd/...
    python3 test_quickcommerce_checker.py https://www.zeptonow.com/pn/.../pvid/...
    python3 test_quickcommerce_checker.py https://blinkit.com/prn/.../prid/...
    python3 test_quickcommerce_checker.py https://www.croma.com/.../p/...
"""

import asyncio
import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

from checkers import detect_site
from stock_checker import check_stock

_VALID_SITES = {"bigbasket", "zepto", "blinkit", "croma"}


async def main():
    if len(sys.argv) < 2:
        print("Usage: python3 test_quickcommerce_checker.py <product_url>")
        print(f"  URL must be from one of: {', '.join(sorted(_VALID_SITES))}")
        sys.exit(1)

    url = sys.argv[1]
    site = detect_site(url)
    if site not in _VALID_SITES:
        print(f"Detected site: {site!r} — this script only covers {', '.join(sorted(_VALID_SITES))}.")
        print("(Other stores work fine via the bot's own /check — this script is scoped "
              "to verifying the disabled-button fix on these 3 specifically.)")
        sys.exit(1)

    print(f"\n{'=' * 70}")
    print(f"Detected site: {site} — Checking: {url}")
    print(f"{'=' * 70}\n")

    in_stock, price = await check_stock(url, site, pincode=None, caller="manual-verification")

    print(f"\n{'=' * 70}")
    price_str = f" @ ₹{price:,.0f}" if price is not None else ""
    print(f"RESULT: {'✅ IN STOCK' if in_stock else '❌ OUT OF STOCK'}{price_str}")
    print(f"{'=' * 70}")
    print(
        "\nCompare the class=/disabled_attr=/aria-disabled=/_is_disabled= "
        "values in the [diag] lines above against DevTools → Inspect on the "
        "actual Add/+ button on this page. That correlation — not just the "
        "final result — is what confirms the fix caught the right state."
    )


if __name__ == "__main__":
    asyncio.run(main())
