import logging
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# Page is JS-rendered (stock text is injected client-side) — the real render
# switch is stock_checker._JS_SITES; this flag is documentation-only.
NEEDS_JS = True

# Deliberately ONLY this one keyword, checked against the page's visible
# text (HTML tags/scripts stripped) — no JSON-LD, no embedded JSON, no
# button/class scanning. Replaces the earlier "notify me"/"out of stock"
# keyword pair: OnePlus shows "Priority Delivery" specifically on in-stock
# listings, so presence (not absence of a negative phrase) is now the signal.
_IN_STOCK_KEYWORD = "priority delivery"


def _visible_text(html: str) -> str:
    """Parse html fresh (rather than reusing the caller's `soup`, so this
    never mutates it) and strip <script>/<style> content before extracting
    text — otherwise get_text() would also pick up the keyword if it
    happened to appear inside inline JS, which isn't "visible text"."""
    text_soup = BeautifulSoup(html, "html.parser")
    for tag in text_soup(["script", "style"]):
        tag.decompose()
    return text_soup.get_text(" ", strip=True)


def check(soup: BeautifulSoup, html: str) -> bool:
    visible_text = _visible_text(html).lower()

    if _IN_STOCK_KEYWORD in visible_text:
        logger.info(f"[oneplus] visible-text keyword {_IN_STOCK_KEYWORD!r} found → True (in stock)")
        return True

    logger.info(f"[oneplus] visible-text keyword {_IN_STOCK_KEYWORD!r} NOT found → False (out of stock)")
    return False
