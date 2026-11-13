"""
fetch_guard.py
~~~~~~~~~~~~~~
Decides whether an HTML response that came back with HTTP 200 is actually
the page we wanted, or a bot-check / Robot Check / error page.

Used for Amazon and Flipkart only (see provider_control.SWITCHABLE_SITES):
a response that looks blocked must make the stock check INCONCLUSIVE
(None), never "out of stock" — saving a block page as out-of-stock is what
used to cause fake "back in stock" alerts on the next good fetch.

Pure functions, no I/O, no third-party imports.

NOTE — heuristics, unverified against live pages (no network access when this
was written): the marker ids / phrases / size thresholds below are best
guesses. They err on the side of "inconclusive": the cost of a wrong guess is
a missed check, never a false alert. Tune with /debugprovider output.
"""

from __future__ import annotations

import re


class BlockedPageError(Exception):
    """The fetched page looks like a block/captcha/error page."""


# Block/captcha pages are small. Above this many characters we do not even
# look for block phrases (a real product page can mention "captcha" in a
# script) — saves CPU on the ~1MB product pages that make up almost every
# fetch.
_PHRASE_CHECK_MAX_CHARS = 150_000

_GENERIC_BLOCK_PHRASES = (
    "access denied", "are you a human", "verify you are a human",
    "verify you are human", "unusual traffic", "request unsuccessful",
    "just a moment", "checking your browser", "pardon our interruption",
    "captcha",
)

_AMAZON_BLOCK_PHRASES = (
    "robot check", "enter the characters you see below",
    "type the characters you see in this image", "not a robot",
    "automated access to amazon data", "validatecaptcha",
    "api-services-support@amazon.com", "sorry! something went wrong",
)

# Ids that exist on a real Amazon product (detail) page. Any one is enough.
_AMAZON_PRODUCT_MARKER_RE = re.compile(
    r"""id=["'](?:producttitle|dp-container|centercol|ppd|availability|add-to-cart-button)["']"""
)

_FLIPKART_MIN_PLAUSIBLE_CHARS = 15_000

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


def extract_title(html: str, max_len: int = 80) -> str:
    """Short, whitespace-collapsed <title> text (for diagnostics)."""
    m = _TITLE_RE.search(html or "")
    if not m:
        return ""
    title = re.sub(r"\s+", " ", m.group(1)).strip()
    return title[:max_len]


def _first_phrase(html_lower: str, phrases: tuple[str, ...]) -> str | None:
    return next((p for p in phrases if p in html_lower), None)


def looks_blocked(site: str | None, html: str | None) -> tuple[bool, str]:
    """Returns (blocked, reason). `reason` is "" when not blocked, otherwise a
    short human-readable cause (safe to log/show: contains no secrets)."""
    if not html or not html.strip():
        return True, "empty response body"

    html_lower = html.lower()
    small = len(html) <= _PHRASE_CHECK_MAX_CHARS

    if site == "amazon":
        # A real product page always wins: a Robot Check page never carries
        # these ids, so checking them first avoids false "blocked" verdicts
        # on a product page that happens to mention a phrase below.
        if _AMAZON_PRODUCT_MARKER_RE.search(html_lower):
            return False, ""
        if small:
            phrase = _first_phrase(html_lower, _AMAZON_BLOCK_PHRASES)
            if phrase:
                return True, f"Amazon robot-check/error page (matched {phrase!r})"
        return True, "no Amazon product-page markers (not a product page, or a block page)"

    if site == "flipkart":
        if small:
            phrase = _first_phrase(html_lower, _GENERIC_BLOCK_PHRASES)
            if phrase:
                return True, f"Flipkart block/captcha page (matched {phrase!r})"
        if len(html) < _FLIPKART_MIN_PLAUSIBLE_CHARS:
            return True, f"response too short for a Flipkart product page ({len(html)} chars)"
        return False, ""

    # Any other site (only reached from /debugprovider on arbitrary URLs).
    if small:
        phrase = _first_phrase(html_lower, _GENERIC_BLOCK_PHRASES)
        if phrase:
            return True, f"block/captcha page (matched {phrase!r})"
    if len(html) < 2000:
        return True, f"response very short ({len(html)} chars)"
    return False, ""
