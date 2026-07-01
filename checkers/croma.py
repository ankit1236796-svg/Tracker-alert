import json
import logging
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

NEEDS_JS = True

_ADD_PATTERNS = ["add to cart", "buy now", "add to bag"]
_OOS_PATTERNS = [
    "out of stock", "sold out", "currently unavailable",
    "notify me when available", "coming soon",
]
_CART_CLASSES = ["add-to-cart", "addToCart", "btn-cart", "plp-add-to-cart"]


def _is_disabled(el) -> bool:
    """Return True if a BS4 element is visually/semantically disabled."""
    if el.get("disabled") is not None:
        return True
    if el.get("aria-disabled", "").lower() == "true":
        return True
    classes = " ".join(el.get("class", [])).lower()
    return "disabled" in classes or "inactive" in classes


def check(soup: BeautifulSoup, html: str) -> bool:
    html_lower = html.lower()

    # ── JSON-LD (most reliable — product-scoped) ──────────────────────────────
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            for item in (data if isinstance(data, list) else [data]):
                if not isinstance(item, dict):
                    continue
                avail = item.get("offers", {}).get("availability", "")
                if "InStock" in avail:
                    logger.info("[croma] JSON-LD: InStock → True")
                    return True
                if "OutOfStock" in avail or "Discontinued" in avail:
                    logger.info("[croma] JSON-LD: OutOfStock/Discontinued → False")
                    return False
        except Exception:
            pass

    # ── Cart button classes — skip disabled elements ───────────────────────────
    # NOTE: On OOS Croma pages the "Add to Cart" button is rendered with the same
    # CSS class (e.g. "add-to-cart") but the element carries `disabled` /
    # `aria-disabled="true"` / a "disabled" CSS class. We MUST filter these out
    # or every OOS page looks like a false positive (recurring bug source).
    for cls in _CART_CLASSES:
        el = soup.find(attrs={"class": lambda c: c and cls.lower() in " ".join(c).lower()})
        if el and not _is_disabled(el):
            logger.info(f"[croma] active cart class '{cls}' found → True")
            return True
        elif el:
            logger.info(f"[croma] cart class '{cls}' found but element is disabled — skipping")

    # ── Buttons — skip disabled ────────────────────────────────────────────────
    for btn in soup.find_all("button"):
        if _is_disabled(btn):
            continue
        text = btn.get_text(strip=True).lower()
        if any(p in text for p in _ADD_PATTERNS):
            logger.info(f"[croma] active button '{text[:40]}' → True")
            return True

    # ── Attrs ─────────────────────────────────────────────────────────────────
    for attr in ("data-testid", "aria-label", "id"):
        for el in soup.find_all(attrs={attr: True}):
            if _is_disabled(el):
                continue
            val = (el.get(attr) or "").lower()
            if any(p in val for p in _ADD_PATTERNS):
                logger.info(f"[croma] active attr {attr}='{val[:40]}' → True")
                return True

    # ── Negative signals ──────────────────────────────────────────────────────
    for pattern in _OOS_PATTERNS:
        if pattern in html_lower:
            logger.info(f"[croma] OOS signal: '{pattern}' → False")
            return False

    # NOTE: Price class fallback removed — Croma shows product price even when
    # OOS (for reference), causing systematic false positives on OOS pages.
    # NOTE: Embedded JSON `inStock:true` scan removed (prev fix) — fires on
    # recommended product JSON embedded on OOS pages.

    logger.info("[croma] no conclusive signal → defaulting OUT OF STOCK (False)")
    return False
