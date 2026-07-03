"""
One-off diagnostic: does Flipkart's stock/availability data survive a
non-JS-rendered (render=false) Scrape.do fetch, or is render=true genuinely
required?

Runs both render modes against a fixed list of manually-confirmed Flipkart
product URLs (see _TEST_CASES below — mix of confirmed in-stock and
confirmed out-of-stock products) and prints a per-URL breakdown plus a
final summary table comparing each mode's checkers.flipkart.check() result
against both the manually-confirmed ground truth and each other.

Not part of the app — bot.py never imports this. Run it manually, once,
wherever SCRAPEDO_KEY is a real credential (e.g. `railway run python3
compare_flipkart_render.py`, or a Railway shell). Costs ~1 credit
(render=false) + ~5 credits (render=true) per URL — with 5 URLs, roughly
30 credits total for the full run. Prints a report; does not modify
anything.

Usage:
    SCRAPEDO_KEY=xxxxx python3 compare_flipkart_render.py
"""

import asyncio
import json
import os

import httpx
from bs4 import BeautifulSoup

from checkers.common import build_scraper_url
from checkers.flipkart import check as flipkart_check, _parse_offers_availability

# (url, expected_in_stock, label) — expected_in_stock is ground truth from
# manual confirmation, not derived from either fetch mode.
_TEST_CASES = [
    (
        "https://www.flipkart.com/apple-2024-ipad-air-m2-128-gb-rom-13-0-inch-wi-fi-only-m2-chip-blue/p/itmb01fe44a7923b?pid=TABHYZDZTHR7AWDH",
        False,
        "iPad Air M2 13\" Blue",
    ),
    (
        "https://www.flipkart.com/apple-ipad-mini-6th-gen-64-gb-rom-8-3-inch-wi-fi-only-a15-bionic-chip-starlight/p/itm8caa558213908?pid=TABG6VNRXAYXG5AK",
        False,
        "iPad Mini 6th Gen Starlight",
    ),
    (
        "https://www.flipkart.com/sony-ps5-digital-cfi-2116b01y-825-gb/p/itm7124b7348127b?pid=GMCHN3VPFGG9NWCB",
        False,
        "Sony PS5 Digital",
    ),
    (
        "https://www.flipkart.com/oneplus-pad-go-2-8-gb-ram-128-rom-12-1-inch-wi-fi-only-mediatek-dimensity-7300-tablet-lavender-drift/p/itmbf04006e3d8e7?pid=TABHGF4ZA7WX8H6G",
        True,
        "OnePlus Pad Go 2",
    ),
    (
        "https://www.flipkart.com/samsung-galaxy-tab-a11-6-gb-ram-128-rom-11-inch-wi-fi-5g-gaming-mediatek-mt8755-tablet-silver/p/itm0c62739da9f41?pid=TABHHQEYZ9DTXKJX",
        True,
        "Samsung Galaxy Tab A11",
    ),
]


async def fetch(url: str, render_js: bool) -> tuple[int, str]:
    scraper_url = build_scraper_url(url, render_js=render_js)
    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
        resp = await client.get(scraper_url)
        return resp.status_code, resp.text


def summarize(label: str, status: int, html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    html_lower = html.lower()

    ld_availability = None
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            for item in (data if isinstance(data, list) else [data]):
                if isinstance(item, dict) and item.get("offers") is not None:
                    avail = _parse_offers_availability(item.get("offers"))
                    if avail:
                        ld_availability = avail
        except Exception:
            pass

    has_add_to_cart_text = any(
        p in html_lower for p in ("add to cart", "buy now", "add to bag")
    )
    has_oos_text = any(
        p in html_lower for p in ("sold out", "currently unavailable", "out of stock")
    )

    checker_result = flipkart_check(soup, html)

    print(f"  --- {label} ---")
    print(f"    HTTP status:            {status}")
    print(f"    HTML byte size:         {len(html.encode('utf-8')):,} bytes")
    print(f"    JSON-LD availability:   {ld_availability!r}")
    print(f"    add-to-cart/buy-now text present: {has_add_to_cart_text}")
    print(f"    OOS text present:       {has_oos_text}")
    print(f"    checkers.flipkart.check() result: {'IN STOCK' if checker_result else 'OUT OF STOCK'}")

    return {
        "status": status,
        "ld_availability": ld_availability,
        "has_add_to_cart_text": has_add_to_cart_text,
        "has_oos_text": has_oos_text,
        "checker_result": checker_result,
    }


async def run_one(url: str, expected_in_stock: bool, label: str) -> dict:
    print(f"\n=== {label} ===")
    print(f"  URL: {url}")
    print(f"  Expected (manually confirmed): {'IN STOCK' if expected_in_stock else 'OUT OF STOCK'}")

    status_no_js, html_no_js = await fetch(url, render_js=False)
    no_js = summarize("render=false (1 credit)", status_no_js, html_no_js)

    status_js, html_js = await fetch(url, render_js=True)
    js = summarize("render=true (5 credits)", status_js, html_js)

    return {
        "label": label,
        "expected_in_stock": expected_in_stock,
        "no_js": no_js,
        "js": js,
    }


def print_summary_table(rows: list[dict]) -> None:
    print("\n\n=== SUMMARY ===")
    header = f"{'Product':<28} {'Expected':<12} {'render=false':<14} {'false OK?':<10} {'render=true':<13} {'true OK?':<9} {'Modes':<10}"
    print(header)
    print("-" * len(header))

    no_js_all_correct = True
    js_all_correct = True
    modes_all_match = True

    for row in rows:
        expected = "IN STOCK" if row["expected_in_stock"] else "OUT OF STOCK"
        no_js_result = row["no_js"]["checker_result"]
        js_result = row["js"]["checker_result"]

        no_js_ok = no_js_result == row["expected_in_stock"]
        js_ok = js_result == row["expected_in_stock"]
        modes_match = no_js_result == js_result

        no_js_all_correct &= no_js_ok
        js_all_correct &= js_ok
        modes_all_match &= modes_match

        no_js_label = "IN STOCK" if no_js_result else "OUT OF STOCK"
        js_label = "IN STOCK" if js_result else "OUT OF STOCK"

        print(
            f"{row['label']:<28} {expected:<12} {no_js_label:<14} "
            f"{'✓' if no_js_ok else '✗':<10} {js_label:<13} "
            f"{'✓' if js_ok else '✗':<9} {'MATCH' if modes_match else 'MISMATCH':<10}"
        )

    print("-" * len(header))
    print(f"render=false accuracy vs manual ground truth: {'ALL CORRECT' if no_js_all_correct else 'SOME WRONG'}")
    print(f"render=true accuracy vs manual ground truth:  {'ALL CORRECT' if js_all_correct else 'SOME WRONG'}")
    print(f"render=false vs render=true agreement:        {'ALL MATCH' if modes_all_match else 'SOME MISMATCH'}")

    print("\n=== VERDICT ===")
    if no_js_all_correct:
        print(
            f"render=false matched manual ground truth on all {len(rows)} URL(s) "
            f"tested — switching Flipkart to render=false looks safe and would "
            f"cut credit cost from 5/request to 1/request."
        )
    elif no_js_all_correct is False and modes_all_match:
        print(
            "render=false matched render=true on every URL, but at least one of "
            "them disagreed with the manually-confirmed ground truth — that's a "
            "checker-logic issue independent of render mode, not a reason by "
            "itself to avoid render=false."
        )
    else:
        print(
            "render=false disagreed with either ground truth or render=true on "
            "at least one URL — render=true still appears necessary for correct "
            "Flipkart detection. See the per-URL breakdown above for which "
            "product(s) failed and why (missing JSON-LD / no button text / no "
            "OOS text under render=false is the usual signature of JS-injected "
            "content)."
        )


async def main():
    if not os.environ.get("SCRAPEDO_KEY"):
        print("SCRAPEDO_KEY is not set — set it to a real key before running this.")
        return

    rows = []
    for url, expected_in_stock, label in _TEST_CASES:
        try:
            row = await run_one(url, expected_in_stock, label)
        except Exception as exc:
            print(f"\n=== {label} ===\n  ERROR: {exc}")
            continue
        rows.append(row)

    if rows:
        print_summary_table(rows)


if __name__ == "__main__":
    asyncio.run(main())
