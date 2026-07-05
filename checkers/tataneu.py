import logging
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

NEEDS_JS = True

# Sole detection signal: TataNeu shows the exact phrase "The product is not
# available" (with the pincode after it, e.g. "...not available 400001") only
# on out-of-stock product pages; in-stock pages omit it. Presence → OOS,
# absence → IN STOCK. Based on real visual confirmation of live pages.
#
# Known tradeoff (flagged on an earlier version of this checker, kept per
# product decision): because this is a NEGATIVE signal, absence means IN
# STOCK — so any fetch glitch / error page / blocked response that lacks the
# phrase resolves to IN STOCK rather than OOS, the opposite of most checkers'
# safe default. Mitigated in practice by how specific the phrase is (unlikely
# to appear, or fail to appear, by accident on a genuine product page).
_OOS_PHRASE = "the product is not available"


def check(soup: BeautifulSoup, html: str) -> bool:
    html_lower = html.lower()
    logger.info(f"[tataneu][diag] HTML length={len(html)}, head={html[:200]!r}")

    if _OOS_PHRASE in html_lower:
        idx = html_lower.find(_OOS_PHRASE)
        logger.info(f"[tataneu][diag] context: ...{html[max(0, idx - 40):idx + 90]!r}...")
        logger.info(f"[tataneu] '{_OOS_PHRASE}' found → OUT OF STOCK")
        return False

    logger.info(f"[tataneu] '{_OOS_PHRASE}' not found → IN STOCK")
    return True
