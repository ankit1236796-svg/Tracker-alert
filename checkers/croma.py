import json
import logging
import re
import aiohttp
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

NEEDS_JS = True

_ADD_PATTERNS = ["add to cart", "buy now", "add to bag"]

_DELIVERY_RESTRICTION_PATTERNS = [
    "not available for your pincode",
    "not available for your location",
    "unfortunately not available for your location",
    "unfortunately not available",
]


def _normalized_text(soup: BeautifulSoup) -> str:
    return re.sub(r"\s+", " ", soup.get_text(" ", strip=True)).lower()


_OOS_PATTERNS = [
    "out of stock", "sold out", "currently unavailable",
    "notify me when available", "coming soon",
]
_CART_CLASSES = ["add-to-cart", "addToCart", "plp-add-to-cart"]


_DISABLED_CLASS_MARKERS = ("disabled", "inactive", "disablebuynow", "disablecartbtn")


def _is_disabled(el) -> bool:
    if el.get("disabled") is not None:
        return True
    if el.get("aria-disabled", "").lower() == "true":
        return True
    classes = " ".join(el.get("class", [])).lower()
    return any(marker in classes for marker in _DISABLED_CLASS_MARKERS)


def _offer_availability(offers) -> str:
    if isinstance(offers, dict):
        avail = offers.get("availability", "")
        if avail:
            return str(avail)
        nested = offers.get("offers", [])
        if isinstance(nested, list):
            for o in nested:
                if isinstance(o, dict):
                    a = o.get("availability", "")
                    if a:
                        return str(a)
        elif isinstance(nested, dict):
            a = nested.get("availability", "")
            if a:
                return str(a)
    elif isinstance(offers, list):
        for o in offers:
            if isinstance(o, dict):
                a = o.get("availability", "")
                if a:
                    return str(a)
    return ""


def _log_delivery_diagnostics(soup: BeautifulSoup, html: str) -> None:
    html_lower = html.lower()
    text = _normalized_text(soup)
    logger.info(f"[croma][diag] HTML length={len(html)}, visible-text length={len(text)}")
    logger.info(f"[croma][diag] head: {html[:200]!r}")

    for p in _DELIVERY_RESTRICTION_PATTERNS:
        logger.info(
            f"[croma][diag] restriction {p!r}: in_html={p in html_lower} in_visible_text={p in text}"
        )

    for kw in ("not available", "unfortunately", "not serviceable"):
        idx = text.find(kw)
        if idx != -1:
            logger.info(f"[croma][diag] visible-text ...{text[max(0, idx - 60):idx + 90]!r}...")

    for kw in (
        "not available", "not serviceable", "unfortunately", "pincode",
        "pin code", "deliver by", "delivered by", "delivery at", "check delivery",
        "enter pincode", "enter your pincode", "notify me", "sold out",
    ):
        if kw in html_lower:
            logger.info(f"[croma][diag] keyword present: {kw!r}")

    hits = 0
    for el in soup.find_all(class_=True):
        cls = " ".join(el.get("class", [])).lower()
        if any(tok in cls for tok in ("deliver", "pincode", "serviceab", "availab", "location")):
            txt = el.get_text(" ", strip=True)[:120]
            logger.info(f"[croma][diag] el <{el.name}> class={el.get('class')} text={txt!r}")
            hits += 1
            if hits >= 25:
                logger.info("[croma][diag] (delivery-ish element dump capped at 25)")
                break

    for kw in ("not available", "unfortunately", "pincode", "notify me"):
        idx = html_lower.find(kw)
        if idx != -1:
            logger.info(f"[croma][diag] ...{html[max(0, idx - 90):idx + 90]!r}...")

    found_ld = False
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            for item in (data if isinstance(data, list) else [data]):
                if isinstance(item, dict) and item.get("offers") is not None:
                    avail = _offer_availability(item.get("offers", {}))
                    if avail:
                        found_ld = True
                        logger.info(f"[croma][diag] JSON-LD availability={avail!r}")
        except Exception:
            pass
    if not found_ld:
        logger.info("[croma][diag] JSON-LD availability: none found")

    for p in _OOS_PATTERNS:
        in_html = p in html_lower
        in_text = p in text
        if in_html or in_text:
            logger.info(f"[croma][diag] OOS pattern {p!r}: in_html={in_html} in_visible_text={in_text}")

    btn_count = 0
    for el in soup.find_all(["button", "a"]):
        label = " ".join(filter(None, [
            el.get_text(" ", strip=True),
            el.get("aria-label", "") or "",
            el.get("data-testid", "") or "",
            el.get("id", "") or "",
            " ".join(el.get("class", []) or []),
        ])).lower()
        if any(pat in label for pat in _ADD_PATTERNS):
            logger.info(
                f"[croma][diag] buy/cart <{el.name}> "
                f"text={el.get_text(' ', strip=True)[:40]!r} class={el.get('class')} "
                f"disabled_attr={el.get('disabled')!r} aria-disabled={el.get('aria-disabled')!r} "
                f"style={el.get('style')!r} → _is_disabled={_is_disabled(el)}"
            )
            btn_count += 1
            if btn_count >= 10:
                break
    if btn_count == 0:
        logger.info("[croma][diag] no Buy Now / Add-to-Cart button matched")


async def fetch_html_for_croma(url: str) -> str:
    """Fetch Croma page with consistent headers to avoid dynamic HTML"""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Accept-Encoding": "gzip, deflate",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers, timeout=30) as resp:
            return await resp.text()


def check(soup: BeautifulSoup, html: str) -> bool:
    html_lower = html.lower()
    text = _normalized_text(soup)

    _log_delivery_diagnostics(soup, html)

    # ── Delivery restriction — highest priority ───
    for pattern in _DELIVERY_RESTRICTION_PATTERNS:
        if pattern in text or pattern in html_lower:
            src = "visible-text" if pattern in text else "raw-html"
            logger.info(f"[croma] delivery restriction ({src}): '{pattern}' → False")
            return False

    # Scoped delivery-element check
    for el in soup.find_all(class_=True):
        cls = " ".join(el.get("class", [])).lower()
        if not any(tok in cls for tok in ("deliver", "serviceab", "pincode", "availab")):
            continue
        etxt = re.sub(r"\s+", " ", el.get_text(" ", strip=True)).lower()
        if any(sig in etxt for sig in ("not available", "unfortunately", "not serviceable")):
            logger.info(f"[croma] delivery element → False")
            return False

    # ── JSON-LD pass ────────
    json_ld_in_stock = False
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            for item in (data if isinstance(data, list) else [data]):
                if not isinstance(item, dict):
                    continue
                avail = _offer_availability(item.get("offers", {}))
                if not avail:
                    continue
                if "InStock" in avail:
                    logger.info("[croma] JSON-LD: InStock (deferred)")
                    json_ld_in_stock = True
                elif "OutOfStock" in avail or "Discontinued" in avail:
                    logger.info("[croma] JSON-LD: OutOfStock → False")
                    return False
        except Exception:
            pass

    # ── OOS text patterns ─────────────────────────────────────────────────────
    for pattern in _OOS_PATTERNS:
        if pattern in html_lower:
            logger.info(f"[croma] OOS text: '{pattern}' → False")
            return False

    # ── Button state ──────────────────────────────────────────────────────────
    cart_buttons = []
    seen_ids = set()

    def _add_candidate(el):
        if id(el) not in seen_ids:
            seen_ids.add(id(el))
            cart_buttons.append(el)

    for cls in _CART_CLASSES:
        for el in soup.find_all(class_=cls):
            _add_candidate(el)
    for el in soup.find_all(["button", "a"]):
        if any(p in el.get_text(strip=True).lower() for p in _ADD_PATTERNS):
            _add_candidate(el)
    for attr in ("data-testid", "aria-label", "id"):
        for el in soup.find_all(attrs={attr: True}):
            if any(p in (el.get(attr) or "").lower() for p in _ADD_PATTERNS):
                _add_candidate(el)

    if cart_buttons:
        active = [b for b in cart_buttons if not _is_disabled(b)]
        if active:
            el = active[0]
            logger.info(f"[croma] active buy/cart → True")
            return True
        logger.info(f"[croma] all {len(cart_buttons)} buttons disabled → False")
        return False

    # ── Final fallback: only trust JSON-LD if NO buttons found
    if json_ld_in_stock and not cart_buttons:
        logger.info("[croma] JSON-LD InStock confirmed → True")
        return True

    logger.info("[croma] no conclusive signal → False")
    return False


async def check_with_headers(url: str) -> bool:
    """Public API: use this for Croma checks (with consistent headers)"""
    html = await fetch_html_for_croma(url)
    soup = BeautifulSoup(html, 'html.parser')
    return check(soup, html)
