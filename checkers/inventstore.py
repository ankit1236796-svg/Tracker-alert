import logging

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# Documentation-only (not read by any code — see stock_checker._JS_SITES
# for the actual render=true/false switch, and
# stock_checker._EXTRA_RETRY_ON_INCOMPLETE_SITES for the "don't guess a
# verdict from a still-blocked page" retry/skip logic).
NEEDS_JS = True

_IN_STOCK_PHRASE = "in stock"


def _visible_text(html: str) -> str:
    text_soup = BeautifulSoup(html, "html.parser")
    for tag in text_soup(["script", "style"]):
        tag.decompose()
    return text_soup.get_text(" ", strip=True)


def check(soup: BeautifulSoup, html: str) -> bool:
    """
    inventstore.in's sole stock-detection signal: the literal phrase
    "In Stock" in the page's VISIBLE text (case-insensitive) — the
    product-level status text shown near the price for the
    default-selected variation. Confirmed present on a real in-stock
    page ("...Price After Cashback ₹ 58900 In Stock...") and absent on a
    real out-of-stock page.

    Every previous approach for this site (Buy Now/Add to Cart text,
    WooCommerce variation JSON counting, out-of-stock class counting) is
    removed — each was confirmed unreliable or unnecessary in turn; this
    single, confirmed presence check is sufficient on its own. Defaults
    to out of stock when the phrase isn't found, per this codebase's
    standing principle that a missed alert is safer than a false one.
    """
    visible_text = _visible_text(html).lower()
    if _IN_STOCK_PHRASE in visible_text:
        logger.info("[inventstore] 'In Stock' found in visible text → True")
        return True

    logger.info("[inventstore] 'In Stock' not found in visible text → False (out of stock)")
    return False
