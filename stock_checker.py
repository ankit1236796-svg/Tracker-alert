"""
stock_checker.py
~~~~~~~~~~~~~~~~
Thin orchestration layer — delegates per-site logic to checkers/.
"""

import logging
import asyncio

import httpx
from bs4 import BeautifulSoup

from checkers import detect_site, build_scraper_url, HEADERS, CHECKER_MAP

logger = logging.getLogger(__name__)

# Re-export detect_site so existing imports from this module still work
__all__ = ["detect_site", "check_stock", "batch_check"]

# Sites that need JS rendering
_JS_SITES = {"zepto", "bigbasket"}


async def check_stock(url: str, site: str) -> bool:
    checker = CHECKER_MAP.get(site)
    if checker is None:
        logger.warning(f"No checker for site '{site}'")
        return False
    try:
        scraper_url = build_scraper_url(url, render_js=site in _JS_SITES)

        async with httpx.AsyncClient(
            headers=HEADERS,
            follow_redirects=True,
            timeout=60.0,
        ) as client:
            response = await client.get(scraper_url)
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
        await asyncio.sleep(3)
    return results
