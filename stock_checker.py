"""
stock_checker.py
~~~~~~~~~~~~~~~~
Lightweight stock detection using httpx + BeautifulSoup.
"""

import logging
import asyncio
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from config import SUPPORTED_SITES

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-IN,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
}


def detect_site(url: str) -> str | None:
    host = urlparse(url).netloc.lower().replace("www.", "")
    for site_key, domains in SUPPORTED_SITES.items():
        for domain in domains:
            if host == domain or host.endswith("." + domain):
                return site_key
    return None


def _check_amazon(soup: BeautifulSoup, html: str) -> bool:
    avail = soup.find("div", {"id": "availability"})
    if avail:
        text = avail.get_text(" ", strip=True).lower()
        logger.info(f"[amazon] availability text: {text}")
        if "currently unavailable" in text or "out of stock" in text:
            return False
        if "in stock" in text or "available" in text:
            return True

    oos_div = soup.find("div", {"id": "outOfStock"})
    if oos_div:
        return False

    html_lower = html.lower()
    if "currently unavailable" in html_lower:
        return False
    if "add to cart" in html_lower or "add-to-cart" in html_lower:
        return True
    if "buy now" in html_lower:
        return True

    price = soup.find("span", {"class": "a-price-whole"})
    if price:
        return True

    return False


def _check_flipkart(soup: BeautifulSoup, html: str) -> bool:
    html_lower = html.lower()
    if "out of stock" in html_lower or "sold out" in html_lower:
        return False
    if "add to cart" in html_lower or "buy now" in html_lower:
        return True
    price = soup.find("div", {"class": "_30jeq3"})
    return price is not None


def _check_zepto(soup: BeautifulSoup, html: str) -> bool:
    html_lower = html.lower()
    if "out of stock" in html_lower or "not available" in html_lower:
        return False
    if '"in_stock":true' in html or '"inStock":true' in html:
        return True
    if '"in_stock":false' in html or '"inStock":false' in html:
        return False
    buttons = soup.find_all("button")
    for btn in buttons:
        if "add" in btn.get_text().strip().lower():
            return True
    return False


def _check_bigbasket(soup: BeautifulSoup, html: str) -> bool:
    html_lower = html.lower()
    if "notify me" in html_lower:
        return False
    if "out of stock" in html_lower:
        return False
    if "add to cart" in html_lower or '"in_stock": true' in html:
        return True
    price = soup.find(attrs={"class": lambda c: c and "price" in c.lower()})
    return price is not None


_CHECKER_MAP = {
    "amazon": _check_amazon,
    "flipkart": _check_flipkart,
    "zepto": _check_zepto,
    "bigbasket": _check_bigbasket,
}


async def check_stock(url: str, site: str) -> bool:
    checker = _CHECKER_MAP.get(site)
    if checker is None:
        logger.warning(f"No checker for site '{site}'")
        return False
    try:
        async with httpx.AsyncClient(
            headers=HEADERS,
            follow_redirects=True,
            timeout=20.0,
        ) as client:
            response = await client.get(url)
            response.raise_for_status()

        html = response.text
        soup = BeautifulSoup(html, "html.parser")
        result = checker(soup, html)
        logger.info(f"[{site}] {url} → {'IN STOCK' if result else 'OUT OF STOCK'}")
        return result

    except httpx.HTTPStatusError as e:
        logger.error(f"HTTP error {e.response.status_code} for {url}")
        return False
    except Exception as exc:
        logger.error(f"Error checking {url}: {exc}")
        return False


async def batch_check(products: list[dict]) -> list[tuple[dict, bool]]:
    results = []
    for product in products:
        in_stock = await check_stock(product["url"], product["site"])
        results.append((product, in_stock))
        await asyncio.sleep(2)
    return results
