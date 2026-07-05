import logging
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

NEEDS_JS = True

# Sole detection signal, by explicit design: presence of the word "currently"
# anywhere on the page (case-insensitive) means OUT OF STOCK, absence means
# IN STOCK — no fallback chain. Not yet verified against real captured
# TataNeu HTML. Known, accepted risk: "currently" is a generic word that can
# appear in unrelated page furniture (view-count widgets, "currently
# trending" sections, cookie banners, etc.), and any fetch glitch/error page
# that happens not to contain it will default to IN STOCK rather than OOS —
# the opposite of every other checker's safe-default behavior. Flagged
# explicitly and kept anyway per product decision.
_OOS_SIGNAL = "currently"


def _log_diagnostics(soup: BeautifulSoup, html: str) -> None:
    """Log-only decision trail: whether the sole signal was found, and
    where (best-effort) it occurs on the page, to help spot false positives
    from unrelated content once real production traffic comes through."""
    html_lower = html.lower()
    logger.info(f"[tataneu][diag] HTML length={len(html)}, head={html[:200]!r}")

    present = _OOS_SIGNAL in html_lower
    logger.info(f"[tataneu][diag] '{_OOS_SIGNAL}' present: {present}")
    if present:
        idx = html_lower.find(_OOS_SIGNAL)
        # Show surrounding context so a false match (e.g. "currently viewing",
        # "currently trending") is visible directly in the log line, not just
        # a bare True/False.
        logger.info(f"[tataneu][diag] context: ...{html[max(0, idx - 60):idx + 80]!r}...")
        count = html_lower.count(_OOS_SIGNAL)
        if count > 1:
            logger.info(f"[tataneu][diag] '{_OOS_SIGNAL}' occurs {count} times on this page")


def check(soup: BeautifulSoup, html: str) -> bool:
    _log_diagnostics(soup, html)

    html_lower = html.lower()
    if _OOS_SIGNAL in html_lower:
        logger.info(f"[tataneu] '{_OOS_SIGNAL}' found → OUT OF STOCK")
        return False

    logger.info(f"[tataneu] '{_OOS_SIGNAL}' not found → IN STOCK")
    return True
