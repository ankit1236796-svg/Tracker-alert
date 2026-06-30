import logging
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


def check(soup: BeautifulSoup, html: str) -> bool:
    html_lower = html.lower()

    avail = soup.find("div", {"id": "availability"})
    if avail:
        text = avail.get_text(" ", strip=True).lower()
        logger.info(f"[amazon] availability text: {text}")
        if "currently unavailable" in text or "out of stock" in text:
            return False
        if "in stock" in text or "available" in text:
            return True

    if soup.find("div", {"id": "outOfStock"}):
        return False

    if soup.find("input", {"id": "add-to-cart-button"}):
        return True
    if soup.find("input", {"id": "buy-now-button"}):
        return True
    if soup.find("input", {"name": "submit.add-to-cart"}):
        return True

    if "currently unavailable" in html_lower:
        return False
    if "add to cart" in html_lower:
        return True
    if "buy now" in html_lower:
        return True

    if soup.find("span", {"class": "a-price-whole"}):
        return True

    logger.info("[amazon] no signal found")
    return False
