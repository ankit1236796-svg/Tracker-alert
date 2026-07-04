"""
Batch-run the quick-commerce disabled-button-fix verification across the 6
labeled URLs (OOS + in-stock for BigBasket, Zepto, Blinkit) and print the
exact PASS/FAIL summary table format requested — one command, one pasted
output, instead of running test_quickcommerce_checker.py six times by hand.

Not part of the app — bot.py never imports this. Uses the exact production
code path (stock_checker.check_stock()) for each URL.

Usage (run via `railway run python3 test_quickcommerce_batch.py`, wherever
SCRAPEDO_KEY is a real credential):

    python3 test_quickcommerce_batch.py
"""

import asyncio
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

from checkers import detect_site
from stock_checker import check_stock

# (label, url, expected_in_stock)
_TEST_CASES = [
    (
        "Zepto OOS",
        "https://www.zepto.com/pn/motorola-all-new-a200-dual-sim-keypad-phone-with-voice-feature-teal-blue/pvid/1e0a66b9-051b-49bd-9c24-32459bb840c9?marketplaceType=SUPER_SAVER",
        False,
    ),
    (
        "Zepto IN-STOCK",
        "https://www.zepto.com/pn/redmi-15c-5g-smartphone-6gb-ram-128gb-storage-69-inch-display-5g-moonlight-blue/pvid/27ccb107-6791-4c8d-9e7e-c01d927e97a0?marketplaceType=SUPER_SAVER",
        True,
    ),
    ("Blinkit OOS", "https://blinkit.com/prn/x/prid/556901", False),
    ("Blinkit IN-STOCK", "https://blinkit.com/prn/x/prid/708846", True),
    (
        "BigBasket IN-STOCK",
        "https://www.bigbasket.com/pd/40360671/bestor-wired-mouse-with-gaming-mouse-pad-1-unit/"
        "?utm_source=bigbasket&utm_medium=share_product&utm_campaign=share_product&ec_id=10",
        True,
    ),
    (
        "BigBasket OOS",
        "https://www.bigbasket.com/pd/1200050845/sony-playstation-5-1tb-ssd-digital-gaming-console-with-ea-sports-fc26-bundle-white/"
        "?utm_source=bigbasket&utm_medium=share_product&utm_campaign=share_product&ec_id=10",
        False,
    ),
]


async def main():
    results = []
    for label, url, expected in _TEST_CASES:
        print(f"\n{'=' * 90}")
        print(f"--- {label} ---")
        print(f"URL: {url}")
        site = detect_site(url)
        print(f"Detected site: {site}")
        print(f"{'=' * 90}\n")

        if site is None:
            print("ERROR: site not recognized — skipping")
            results.append((label, url, expected, None, "unrecognized site"))
            continue

        try:
            in_stock, price = await check_stock(url, site, pincode=None, caller="batch-verification")
        except Exception as exc:
            print(f"ERROR: {exc}")
            results.append((label, url, expected, None, str(exc)))
            continue

        price_str = f" @ ₹{price:,.0f}" if price is not None else ""
        print(f"\nRESULT: {'IN STOCK' if in_stock else 'OUT OF STOCK'}{price_str}")
        results.append((label, url, expected, in_stock, None))

    print(f"\n\n{'=' * 90}")
    print("SUMMARY")
    print(f"{'=' * 90}")
    passed = 0
    for label, url, expected, actual, error in results:
        expected_str = "IN STOCK" if expected else "OUT OF STOCK"
        if actual is None:
            status = f"❌ ERROR ({error})"
        else:
            actual_str = "IN STOCK" if actual else "OUT OF STOCK"
            ok = actual == expected
            passed += 1 if ok else 0
            status = f"{'✅ PASS' if ok else '❌ FAIL'} (expected {expected_str}, got {actual_str})"
        print(f"{label:<20} {status}")
        print(f"  {url}")
    print(f"{'=' * 90}")
    print(f"FINAL: {passed}/{len(results)} tests passed")


if __name__ == "__main__":
    asyncio.run(main())
