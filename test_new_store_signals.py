#!/usr/bin/env python3
"""
Diagnostic probe for candidate NEW stores (Vijay Sales, Vivo, iQOO, Oppo,
Unicorn Store) — run in the Railway Console, where SCRAPEDO_KEY is set. Does
NOT touch the bot, database, or any tracked products.

Purpose: before writing any checker, SEE the real page structure — is the site
reachable through Scrape.do, does it need JS rendering, and which stock signal
(JSON-LD availability, OOS text, add-to-cart button state, Shopify
"available" flag, …) is actually present. This mirrors the _log_diagnostics
pattern used in checkers/bigbasket.py etc., but standalone.

Usage — pass one or more REAL product URLs, ideally one confirmed OUT-OF-STOCK
and one confirmed IN-STOCK per store so we can compare what changes:

    python3 test_new_store_signals.py \
        "https://shop.unicornstore.in/products/<oos-item>" \
        "https://shop.unicornstore.in/products/<in-stock-item>" \
        "https://www.vijaysales.com/<some-product>" \
        "https://www.vivo.com/in/products/x300-ultra"

For each URL it fetches via Scrape.do TWICE (render=false = 1 credit, then
render=true = 5 credits) and prints the candidate signals for each, so we can
pick the cheapest render mode that still exposes a reliable signal.
"""

import asyncio
import json
import re
import sys

import httpx
from bs4 import BeautifulSoup

from checkers.common import build_scraper_url, HEADERS

_OOS_PATTERNS = [
    "out of stock", "out-of-stock", "sold out", "currently unavailable",
    "notify me", "coming soon", "temporarily unavailable", "not available",
]
_POS_PATTERNS = [
    "add to cart", "add to bag", "add to basket", "buy now", "in stock",
    "add to wishlist and buy", "shop now",
]
_PLATFORM_MARKERS = {
    "Shopify": ["cdn.shopify.com", "shopify.theme", "/cdn/shop/", "myshopify", "shopify-section"],
    "Magento/Adobe": ["mage/", "magento", "/static/version", "adobe commerce", "mage-init", "catalog-product"],
    "Next.js/React SPA": ["__next_data__", "_next/static", "window.__nuxt__", "data-reactroot"],
}


def _jsonld_availability(soup: BeautifulSoup) -> list[str]:
    found: list[str] = []

    def _avails(o) -> list[str]:
        r: list[str] = []
        if isinstance(o, dict):
            if o.get("availability"):
                r.append(str(o["availability"]))
            if o.get("offers"):
                r += _avails(o["offers"])
        elif isinstance(o, list):
            for x in o:
                r += _avails(x)
        return r

    for s in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(s.string or "")
        except Exception:
            continue
        for item in (data if isinstance(data, list) else [data]):
            if isinstance(item, dict) and item.get("offers"):
                found += _avails(item.get("offers"))
    return found


# Class-name substrings that commonly mark a dedicated stock-status element on
# custom storefronts (esp. Magento PWA / BEM-style themes like Vijay Sales' —
# "button__root_highPriority", "product__price--addtocart" are exactly this
# naming convention). A structural status element (e.g.
# <div class="product__stock--outOfStock">Out of Stock</div>) is a far more
# reliable signal than page-wide text search or button disabled-state, which
# can pick up unrelated widgets or hydration-timing noise.
_STOCK_CLASS_MARKERS = ("stock", "availability", "in-stock", "out-of-stock", "outofstock", "unavailable")


def _ancestor_classes(el, levels: int = 3) -> list[str]:
    """Class lists of up to `levels` parent elements, innermost first — helps
    spot a wrapping stock-status container around a button."""
    chain = []
    node = el.parent
    for _ in range(levels):
        if node is None or not hasattr(node, "get"):
            break
        cls = node.get("class")
        if cls:
            chain.append(" ".join(cls))
        node = node.parent
    return chain


def _text_context(html_text: str, pattern: str, window: int = 120) -> str:
    """~window chars of VISIBLE-text context around the first occurrence of
    `pattern`, so we can see whether an OOS/positive phrase sits near the buy
    box or in an unrelated part of the page (e.g. a related-products widget)."""
    idx = html_text.lower().find(pattern)
    if idx == -1:
        return ""
    start, end = max(0, idx - window), idx + len(pattern) + window
    return "…" + html_text[start:end].replace("\n", " ").strip() + "…"


def _probe(html: str) -> dict:
    low = html.lower()
    soup = BeautifulSoup(html, "html.parser")
    visible_text = soup.get_text(" ", strip=True)
    platforms = [name for name, marks in _PLATFORM_MARKERS.items() if any(m in low for m in marks)]

    buttons = []
    for el in soup.find_all(["button", "input", "a"]):
        label = " ".join(filter(None, [
            el.get_text(" ", strip=True),
            el.get("value", "") or "",
            el.get("name", "") or "",
            " ".join(el.get("class", []) or []),
        ])).lower()
        if any(p in label for p in ("add to cart", "add to bag", "buy now", "add to basket")) and len(label) < 90:
            buttons.append(
                f"<{el.name}> disabled={el.get('disabled')!r} aria-disabled={el.get('aria-disabled')!r} "
                f"class={el.get('class')} text={el.get_text(' ', strip=True)[:35]!r} "
                f"ancestors={_ancestor_classes(el)}"
            )
            if len(buttons) >= 6:
                break

    # Structural stock-status elements: any element whose OWN class (not text)
    # contains a stock-related marker. Printed regardless of what it says, so
    # we can see the class naming convention even if the displayed word isn't
    # "stock" (e.g. a class like "product__availability--out" wrapping "Notify Me").
    stock_class_elements = []
    for el in soup.find_all(class_=True):
        classes = " ".join(el.get("class", [])).lower()
        if any(m in classes for m in _STOCK_CLASS_MARKERS):
            text = el.get_text(" ", strip=True)
            if 0 < len(text) < 60:  # skip huge container divs; want the leaf status text
                stock_class_elements.append(f"class={el.get('class')} text={text!r}")
            if len(stock_class_elements) >= 8:
                break

    oos_hits = [p for p in _OOS_PATTERNS if p in low]
    positive_hits = [p for p in _POS_PATTERNS if p in low]

    return {
        "platforms": platforms,
        "jsonld_availability": _jsonld_availability(soup),
        "shopify_available_flags": re.findall(r'"available"\s*:\s*(true|false)', low)[:8],
        "oos_text": oos_hits,
        "oos_text_context": {p: _text_context(visible_text, p) for p in oos_hits},
        "positive_text": positive_hits,
        "positive_text_context": {p: _text_context(visible_text, p) for p in positive_hits},
        "stock_class_elements": stock_class_elements,
        "buttons": buttons,
    }


async def _fetch(url: str, render: bool) -> tuple[int, str]:
    scraper_url = build_scraper_url(url, render_js=render)
    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=90.0) as client:
        r = await client.get(scraper_url)
    return r.status_code, r.text


async def _run(url: str) -> None:
    print("\n" + "=" * 80)
    print(f"URL: {url}")
    for render in (False, True):
        print(f"\n--- render={render} ({'5 credits' if render else '1 credit'}) ---")
        try:
            status, html = await _fetch(url, render)
        except Exception as exc:
            print(f"  FETCH FAILED: {exc}")
            continue
        print(f"  HTTP {status}, HTML length={len(html)}")
        if status != 200 or len(html) < 500:
            print("  ⚠️  likely blocked / challenge / empty — reachability problem")
        info = _probe(html)
        print(f"  platform markers      : {info['platforms'] or 'none detected'}")
        print(f"  JSON-LD availability  : {info['jsonld_availability'] or 'NONE'}")
        print(f"  shopify available flag: {info['shopify_available_flags'] or 'none'}")
        print(f"  OOS text present      : {info['oos_text'] or 'none'}")
        for p, ctx in info["oos_text_context"].items():
            print(f"      context for {p!r}: {ctx}")
        print(f"  positive text present : {info['positive_text'] or 'none'}")
        for p, ctx in info["positive_text_context"].items():
            print(f"      context for {p!r}: {ctx}")
        print(f"  stock-status class elements ({len(info['stock_class_elements'])}):")
        for s in info["stock_class_elements"]:
            print(f"      {s}")
        print(f"  add/buy buttons ({len(info['buttons'])}):")
        for b in info["buttons"]:
            print(f"      {b}")


async def main() -> None:
    urls = sys.argv[1:]
    if not urls:
        print("Pass one or more real product URLs as arguments (see the module docstring).")
        return
    for url in urls:
        await _run(url)
    print("\nDone. Paste this whole output back so the checkers can be written from real signals.")


if __name__ == "__main__":
    asyncio.run(main())
