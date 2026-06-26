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
}


def detect_site(url: str) -> str | None:
    host = urlparse(url).netloc.lower().replace("www.", "")
    for site_key, domains in SUPPORTED_SITES.items():
        for domain in domains:
            if host == domain or host.endswith("." + domain):
                return site_key
    return None


def _check_amazon(soup: BeautifulSoup) -> bool:
    atc = soup.find("input", {"id": "add-to-cart-button"})
    if atc:
        return True
    buy_now = soup.find("input", {"id": "buy-now-button"})
    if buy_now:
        return True
    avail = soup.find("div", {"id": "availability"})
    if avail:
        text = avail.get_text().strip().lower()
        if "in stock" in text or "available" in text:
            return True
        if "currently unavailable" in text or "out of stock" in text:
            return False
    price = soup.find("span", {"class": "a-price-whole"})
    return price is not None


def _check_flipkart(soup: BeautifulSoup) -> bool:
    oos = soup.find(string=lambda t: t and "out of stock" in t.lower())
    if oos:
        return False
    buttons = soup.find_all("button")
    for btn in buttons:
        txt = btn.get_text().strip().lower()
        if "add to cart" in txt or "buy now" in txt:
            return True
    price = soup.find("div", {"class": "_30jeq3"})
    return price is not None


def _check_zepto(soup: BeautifulSoup) -> bool:
    body = soup.get_text().lower()
    if "out of stock" in body or "not available" in body:
        return False
    buttons = soup.find_all("button")
    for btn in buttons:
        if "add" in btn.get_text().strip().lower():
            return True
    price = soup.find(attrs={"class": lambda c: c and "price" in c.lower()})
    return price is not None


def _check_bigbasket(soup: BeautifulSoup) -> bool:
    body = soup.get_text().lower()
    notify = soup.find("button", string=lambda t: t and "notify me" in t.lower())
    if notify:
        return False
    if "out of stock" in body:
        return False
    buttons = soup.find_all("button")
    for btn in buttons:
        if "add" in btn.get_text().strip().lower():
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
        soup = BeautifulSoup(response.text, "html.parser")
        result = checker(soup)
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
