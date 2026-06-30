import logging
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


def check(soup: BeautifulSoup, html: str) -> bool:
    html_lower = html.lower()

    if "notify me" in html_lower:
        return False
    if "out of stock" in html_lower:
        return False
    if "add to cart" in html_lower or '"in_stock": true' in html:
        return True

    price = soup.find(attrs={"class": lambda c: c and "price" in c.lower()})
    return price is not None
