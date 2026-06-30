import logging
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

_PRICE_CLASSES = ["_30jeq3", "Nx9bqj", "_25b18c", "_16Jk6d", "CxhGGd"]


def check(soup: BeautifulSoup, html: str) -> bool:
    html_lower = html.lower()

    if "sold out" in html_lower:
        logger.info("[flipkart] 'sold out' found")
        return False
    if "currently unavailable" in html_lower:
        logger.info("[flipkart] 'currently unavailable' found")
        return False
    if "out of stock" in html_lower and "add to cart" not in html_lower:
        logger.info("[flipkart] 'out of stock' found, no add-to-cart")
        return False

    if "add to cart" in html_lower or "buy now" in html_lower:
        return True

    for price_class in _PRICE_CLASSES:
        if soup.find(["div", "span"], {"class": price_class}):
            return True

    if "₹" in html and ("pincode" in html_lower or "delivery" in html_lower):
        return True

    logger.info("[flipkart] no clear signal found, defaulting OUT OF STOCK")
    return False
