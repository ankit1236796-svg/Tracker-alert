import logging
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


def check(soup: BeautifulSoup, html: str) -> bool:
    html_lower = html.lower()

    if "out of stock" in html_lower or "not available" in html_lower:
        return False
    if '"in_stock":true' in html or '"inStock":true' in html:
        return True
    if '"in_stock":false' in html or '"inStock":false' in html:
        return False

    for btn in soup.find_all("button"):
        if "add" in btn.get_text().strip().lower():
            return True

    return False
