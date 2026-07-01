import json
import logging
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

NEEDS_JS = False

# Apple uses "Add to Bag" (not "Add to Cart")
_ADD_PATTERNS = ["add to bag", "add to cart", "buy"]
_OOS_PATTERNS = [
    "out of stock", "sold out", "currently unavailable",
    "notify me when available", "coming soon",
]


def check(soup: BeautifulSoup, html: str) -> bool:
    html_lower = html.lower()

    # ── JSON-LD (most reliable on apple.com/in) ───────────────────────────────
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            for item in (data if isinstance(data, list) else [data]):
                if not isinstance(item, dict):
                    continue
                avail = item.get("offers", {}).get("availability", "")
                if "InStock" in avail:
                    logger.info("[apple] JSON-LD: InStock → True")
                    return True
                if "OutOfStock" in avail or "Discontinued" in avail:
                    logger.info("[apple] JSON-LD: OutOfStock/Discontinued → False")
                    return False
        except Exception:
            pass

    # ── Buttons ───────────────────────────────────────────────────────────────
    for btn in soup.find_all("button"):
        if btn.get("disabled") is not None or btn.get("aria-disabled", "") == "true":
            continue
        text = btn.get_text(strip=True).lower()
        if any(p in text for p in _ADD_PATTERNS):
            logger.info(f"[apple] active button '{text[:40]}' → True")
            return True

    # ── Attrs ─────────────────────────────────name: "buy", "add to bag" ──────
    for attr in ("data-testid", "aria-label", "id"):
        for el in soup.find_all(attrs={attr: True}):
            if el.get("disabled") is not None or el.get("aria-disabled", "") == "true":
                continue
            val = (el.get(attr) or "").lower()
            if "add-to-bag" in val or "addtobag" in val or any(p in val for p in _ADD_PATTERNS):
                logger.info(f"[apple] active attr {attr}='{val[:40]}' → True")
                return True

    # ── Negative signals ──────────────────────────────────────────────────────
    for pattern in _OOS_PATTERNS:
        if pattern in html_lower:
            logger.info(f"[apple] OOS signal: '{pattern}' → False")
            return False

    logger.info("[apple] no conclusive signal → defaulting OUT OF STOCK (False)")
    return False
