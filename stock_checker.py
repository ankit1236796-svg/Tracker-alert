import logging
import asyncio
from urllib.parse import urlparse

from playwright.async_api import async_playwright, Page, TimeoutError as PWTimeout

from config import PLAYWRIGHT_HEADLESS, PLAYWRIGHT_TIMEOUT, SUPPORTED_SITES

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Site detection
# ---------------------------------------------------------------------------

def detect_site(url: str) -> str | None:
    """Return a canonical site key ('amazon', 'flipkart', …) or None."""
    host = urlparse(url).netloc.lower().replace("www.", "")
    for site_key, domains in SUPPORTED_SITES.items():
        for domain in domains:
            if host == domain or host.endswith("." + domain):
                return site_key
    return None


# ---------------------------------------------------------------------------
# Per-site checkers
# ---------------------------------------------------------------------------

async def _check_amazon(page: Page, url: str) -> bool:
    await page.goto(url, wait_until="domcontentloaded", timeout=PLAYWRIGHT_TIMEOUT)

    # "Add to Cart" or "Buy Now" present → in stock
    add_to_cart = await page.query_selector("#add-to-cart-button")
    buy_now = await page.query_selector("#buy-now-button")
    if add_to_cart or buy_now:
        return True

    # Explicit OOS text
    oos_sel = "#outOfStock, .a-color-price:has-text('Currently unavailable')"
    oos = await page.query_selector(oos_sel)
    if oos:
        return False

    # Look for availability text in the page
    availability = await page.query_selector("#availability span")
    if availability:
        text = (await availability.inner_text()).strip().lower()
        return "in stock" in text or "available" in text

    return False


async def _check_flipkart(page: Page, url: str) -> bool:
    await page.goto(url, wait_until="domcontentloaded", timeout=PLAYWRIGHT_TIMEOUT)

    # "Add to Cart" button
    atc = await page.query_selector("button._2KpZ6l._2U9uOA._3v1-ww, button._2KpZ6l")
    if atc:
        text = (await atc.inner_text()).strip().lower()
        if "add to cart" in text or "buy now" in text:
            return True

    # Out-of-stock banner
    oos = await page.query_selector("._16FRp0")  # Flipkart OOS class
    if oos:
        return False

    # Generic sold-out text
    body_text = (await page.inner_text("body")).lower()
    if "sold out" in body_text or "out of stock" in body_text:
        return False

    # If we found a price, assume in stock
    price = await page.query_selector("._30jeq3")
    return price is not None


async def _check_zepto(page: Page, url: str) -> bool:
    await page.goto(url, wait_until="networkidle", timeout=PLAYWRIGHT_TIMEOUT)

    # Zepto uses "Add" button on product cards
    add_btn = await page.query_selector("button:has-text('Add')")
    if add_btn:
        return True

    body_text = (await page.inner_text("body")).lower()
    if "out of stock" in body_text or "not available" in body_text:
        return False

    # Fallback: price element present
    price = await page.query_selector("[class*='price']")
    return price is not None


async def _check_bigbasket(page: Page, url: str) -> bool:
    await page.goto(url, wait_until="networkidle", timeout=PLAYWRIGHT_TIMEOUT)

    # BigBasket "Add" button
    add_btn = await page.query_selector("button[class*='add-btn'], button:has-text('Add')")
    if add_btn:
        return True

    # "Notify Me" → out of stock
    notify = await page.query_selector("button:has-text('Notify Me')")
    if notify:
        return False

    body_text = (await page.inner_text("body")).lower()
    if "out of stock" in body_text:
        return False

    # Last resort: presence of a price
    price = await page.query_selector("[class*='discnt-price'], [class*='price']")
    return price is not None


_CHECKER_MAP = {
    "amazon": _check_amazon,
    "flipkart": _check_flipkart,
    "zepto": _check_zepto,
    "bigbasket": _check_bigbasket,
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def check_stock(url: str, site: str) -> bool:
    """
    Launch a browser, navigate to *url*, apply the appropriate checker,
    and return True if the item is in stock.
    """
    checker = _CHECKER_MAP.get(site)
    if checker is None:
        logger.warning(f"No checker for site '{site}' – defaulting to False.")
        return False

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=PLAYWRIGHT_HEADLESS)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
        )
        page = await context.new_page()
        try:
            result = await checker(page, url)
            logger.info(f"[{site}] {url} → {'IN STOCK' if result else 'OUT OF STOCK'}")
            return result
        except PWTimeout:
            logger.error(f"Timeout checking {url}")
            return False
        except Exception as exc:
            logger.error(f"Error checking {url}: {exc}")
            return False
        finally:
            await browser.close()


async def batch_check(products: list[dict]) -> list[tuple[dict, bool]]:
    """Check multiple products one at a time (avoids hammering servers)."""
    results = []
    for product in products:
        in_stock = await check_stock(product["url"], product["site"])
        results.append((product, in_stock))
        await asyncio.sleep(2)  # polite delay between requests
    return results
