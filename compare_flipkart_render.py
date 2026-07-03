"""
One-off diagnostic: does Flipkart's stock/availability data survive a
non-JS-rendered (render=false) Scrape.do fetch, or is render=true genuinely
required?

Not part of the app — bot.py never imports this. Run it manually, once,
wherever SCRAPEDO_KEY is a real credential (e.g. `railway run python3
compare_flipkart_render.py`, or a Railway shell). Costs ~1 credit
(render=false) + ~5 credits (render=true) = ~6 credits total for one
product URL. Prints a verdict; does not modify anything.

Usage:
    SCRAPEDO_KEY=xxxxx python3 compare_flipkart_render.py [product_url]

If no URL is given, defaults to a real Flipkart product page.
"""

import asyncio
import json
import os
import sys

import httpx
from bs4 import BeautifulSoup

from checkers.common import build_scraper_url
from checkers.flipkart import check as flipkart_check, _parse_offers_availability

DEFAULT_URL = "https://www.flipkart.com/apple-iphone-15-black-128-gb/p/itm6ac6485515ae4"


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
    has_price_symbol = "₹" in html

    checker_result = flipkart_check(soup, html)

    print(f"\n=== {label} ===")
    print(f"  HTTP status:            {status}")
    print(f"  HTML byte size:         {len(html.encode('utf-8')):,} bytes")
    print(f"  JSON-LD availability:   {ld_availability!r}")
    print(f"  'add to cart/buy now' text present: {has_add_to_cart_text}")
    print(f"  OOS text present:       {has_oos_text}")
    print(f"  ₹ price symbol present: {has_price_symbol}")
    print(f"  checkers.flipkart.check() result:   {'IN STOCK' if checker_result else 'OUT OF STOCK'}")

    return {
        "status": status,
        "size": len(html.encode("utf-8")),
        "ld_availability": ld_availability,
        "has_add_to_cart_text": has_add_to_cart_text,
        "has_oos_text": has_oos_text,
        "has_price_symbol": has_price_symbol,
        "checker_result": checker_result,
    }


async def main():
    if not os.environ.get("SCRAPEDO_KEY"):
        print("SCRAPEDO_KEY is not set — set it to a real key before running this.")
        return

    url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    print(f"Testing: {url}\n")

    status_no_js, html_no_js = await fetch(url, render_js=False)
    status_js, html_js = await fetch(url, render_js=True)

    no_js = summarize("render=false (1 credit)", status_no_js, html_no_js)
    js = summarize("render=true (5 credits)", status_js, html_js)

    print("\n=== VERDICT ===")
    if no_js["status"] != 200:
        print(
            f"render=false returned HTTP {no_js['status']} (likely Flipkart's "
            f"anti-bot layer rejecting a non-rendered fetch outright) — "
            f"render=true is required regardless of DOM content."
        )
    elif no_js["checker_result"] == js["checker_result"] and (
        no_js["ld_availability"] or no_js["has_add_to_cart_text"] or no_js["has_oos_text"]
    ):
        print(
            "render=false produced the SAME stock verdict as render=true, using "
            "a real signal (not just a default-OOS fallback). Switching Flipkart "
            "to render=false looks safe for this URL — but test a few more "
            "product URLs (in-stock AND out-of-stock ones) before rolling it "
            "out, since a single sample can be misleading."
        )
    else:
        print(
            "render=false did NOT reproduce the same verdict / lacked the "
            "signals render=true had. render=true still appears required for "
            "correct Flipkart detection."
        )


if __name__ == "__main__":
    asyncio.run(main())
