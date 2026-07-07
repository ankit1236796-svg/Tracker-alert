#!/usr/bin/env python3
"""
Standalone verification for the EarnKaro (EK Affiliaters) conversion API.
Does NOT touch the bot, database, or any tracked products — safe to run alone.

Usage (provide the key via the environment):
    EARNKARO_API_KEY=your_key python3 test_affiliate_api.py
    EARNKARO_API_KEY=your_key python3 test_affiliate_api.py "https://www.flipkart.com/....."

It prints:
  1. The RAW API response (HTTP status + full JSON body) so we can confirm the
     exact success AND error shapes — especially the error format, which we
     still need to see to finalize error handling.
  2. What affiliate.convert_url() (the real production parser) extracts from it.

Try a few real product URLs from different stores (Flipkart, Myntra, Ajio,
etc.) to see which stores EarnKaro actually converts vs. returns unchanged.
"""

import asyncio
import json
import logging
import os
import sys

import httpx

import affiliate

# A couple of real-ish sample URLs; override by passing one as an argument.
DEFAULT_URLS = [
    "https://www.flipkart.com/apple-iphone-15-black-128-gb/p/itm6ac6485515ae4",
    "https://www.myntra.com/tshirts/roadster/roadster-men-black-tshirt/2313996/buy",
]

API_URL = "https://ekaro-api.affiliaters.in/api/converter/public"


async def _raw_call(url: str, key: str) -> None:
    print("=== RAW API RESPONSE ===")
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.post(
                API_URL,
                headers={"Authorization": f"Bearer {key}"},
                json={"deal": url, "convert_option": "convert_only"},
            )
        print(f"HTTP {r.status_code}")
        try:
            print(json.dumps(r.json(), indent=2, ensure_ascii=False))
        except Exception:
            print("(non-JSON body)")
            print(r.text[:2000])
    except Exception as exc:
        print(f"request failed: {exc}")


async def _check(url: str, key: str) -> None:
    print("\n" + "=" * 70)
    print(f"Input URL: {url}\n")
    await _raw_call(url, key)

    print("\n=== affiliate.convert_url() RESULT ===")
    converted = await affiliate.convert_url(url)
    print(f"convert_url -> {converted!r}")
    if converted is None:
        print("❌ convert_url returned None — conversion failed (see logs above).")
    elif converted == url:
        print("⚠️ Same URL returned — store may be unsupported, or link unchanged.")
    else:
        print("✅ Got a distinct affiliate URL.")


async def main() -> None:
    key = os.environ.get("EARNKARO_API_KEY", "")
    if not key:
        print("ERROR: set EARNKARO_API_KEY in the environment before running.")
        sys.exit(1)

    urls = sys.argv[1:] if len(sys.argv) > 1 else DEFAULT_URLS
    for url in urls:
        await _check(url, key)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    asyncio.run(main())
