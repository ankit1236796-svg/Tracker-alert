import json
import logging
import re
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

NEEDS_JS = True

_ADD_PATTERNS = ["add to cart", "buy now", "add to bag"]

# Delivery-section strings shown on OOS Croma pages. Matched against NORMALIZED
# VISIBLE TEXT (see _normalized_text) — not raw HTML — so a message split across
# <span>/<div> tags or padded with whitespace still matches, and the match no
# longer depends on a specific container class name.
# Checked FIRST — before JSON-LD (which can be stale) and before button text
# (which appears on both in-stock and OOS pages) — so they act as overrides.
_DELIVERY_RESTRICTION_PATTERNS = [
    "not available for your pincode",
    "not available for your location",
    "unfortunately not available for your location",
    "unfortunately not available",
]


def _normalized_text(soup: BeautifulSoup) -> str:
    """
    Full-page VISIBLE text with runs of whitespace collapsed to single spaces
    and lowercased. BeautifulSoup's get_text with a space separator joins text
    from separate tags, so a phrase Croma splits across elements (e.g.
    <span>Not Available</span><span>for your pincode</span>) becomes the
    contiguous string 'not available for your pincode' here — which a raw-HTML
    substring search would miss.
    """
    return re.sub(r"\s+", " ", soup.get_text(" ", strip=True)).lower()

_OOS_PATTERNS = [
    "out of stock", "sold out", "currently unavailable",
    "notify me when available", "coming soon",
]
# "btn-cart" removed — it matched Croma's persistent header cart icon on every page,
# causing false positives once the lambda was using correct BS4 class membership.
# "addToCart" kept — specific enough as an exact class name.
_CART_CLASSES = ["add-to-cart", "addToCart", "plp-add-to-cart"]


# Class tokens that indicate a button is in its DISABLED state on Croma.
# IMPORTANT: 'disable-btn-in-pdp' is a STRUCTURAL hook class Croma renders on
# the Buy Now / Add to Cart buttons in BOTH states, so it must NOT count as
# disabled — matching the bare substring 'disable' (which it contains) flagged
# active buttons as disabled and made every product read out-of-stock.
# The real per-action disabled-state classes are 'disableBuyNow' /
# 'disableCartBtn'; the generic 'disabled'/'inactive' markers are also honored.
_DISABLED_CLASS_MARKERS = ("disabled", "inactive", "disablebuynow", "disablecartbtn")


def _is_disabled(el) -> bool:
    """Return True if a BS4 element is visually/semantically disabled.

    Croma signals an OOS product's Buy Now / Add to Cart buttons ONLY via class
    names, not HTML attributes — e.g. class=['btn', 'disable-btn-in-pdp',
    'disableBuyNow'] with disabled/aria-disabled/style all unset. We match the
    specific per-action state classes (disableBuyNow / disableCartBtn) plus the
    generic 'disabled'/'inactive' markers, but deliberately NOT the structural
    'disable-btn-in-pdp' hook (present on active buttons too). Note 'disabled'
    is matched with its trailing 'd', so it catches btn-disabled / is-disabled
    without catching disable-btn-in-pdp.
    """
    if el.get("disabled") is not None:
        return True
    if el.get("aria-disabled", "").lower() == "true":
        return True
    classes = " ".join(el.get("class", [])).lower()
    return any(marker in classes for marker in _DISABLED_CLASS_MARKERS)


def _offer_availability(offers) -> str:
    """
    Extract the first availability string from an 'offers' value that may be:
      • a single Offer dict     {"availability": "https://schema.org/InStock"}
      • an AggregateOffer dict  {"offers": [{"availability": "..."}], ...}
      • a list of Offer dicts   [{"availability": "..."}, ...]
    Returns "" when no availability can be found.
    """
    if isinstance(offers, dict):
        avail = offers.get("availability", "")
        if avail:
            return str(avail)
        # AggregateOffer: availability lives in the nested offers list
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
    """
    Dump the REAL scraped delivery-section structure to the logs.

    The previous fix assumed "Not Available for your pincode" appears as literal
    text in the scraped HTML. That assumption passed synthetic tests but fails on
    the live page, so we log exactly what the real HTML contains — pattern hits,
    delivery-related elements (whatever their actual class names are), and context
    around key phrases — so the true structure can be read straight from prod logs
    instead of guessed at. Log-only: this function never changes the result.
    """
    html_lower = html.lower()
    text = _normalized_text(soup)
    logger.info(f"[croma][diag] HTML length={len(html)}, visible-text length={len(text)}")
    # Confirm we got a real product page, not a bot-challenge / block page.
    logger.info(f"[croma][diag] head: {html[:200]!r}")

    # 1. Per-pattern presence in BOTH raw HTML and normalized visible text.
    #    A pattern that is False-in-html but True-in-text confirms the message
    #    was split across tags — the exact failure the visible-text match fixes.
    for p in _DELIVERY_RESTRICTION_PATTERNS:
        logger.info(
            f"[croma][diag] restriction {p!r}: in_html={p in html_lower} "
            f"in_visible_text={p in text}"
        )

    # 1b. Visible-text context around 'not available' so the real phrasing is
    #     captured even when raw HTML splits it across tags.
    for kw in ("not available", "unfortunately", "not serviceable"):
        idx = text.find(kw)
        if idx != -1:
            logger.info(f"[croma][diag] visible-text ...{text[max(0, idx - 60):idx + 90]!r}...")

    # 2. Broader keyword presence — reveals alternate phrasing / whether the
    #    serviceability text is present in the scraped HTML at all.
    for kw in (
        "not available", "not serviceable", "unfortunately", "pincode",
        "pin code", "deliver by", "delivered by", "delivery at", "check delivery",
        "enter pincode", "enter your pincode", "notify me", "sold out",
    ):
        if kw in html_lower:
            logger.info(f"[croma][diag] keyword present: {kw!r}")

    # 3. Every element whose class hints at delivery/serviceability, with its
    #    ACTUAL class list and text — so we learn the real class names.
    hits = 0
    for el in soup.find_all(class_=True):
        cls = " ".join(el.get("class", [])).lower()
        if any(tok in cls for tok in ("deliver", "pincode", "serviceab", "availab", "location")):
            txt = el.get_text(" ", strip=True)[:120]
            logger.info(f"[croma][diag] el <{el.name}> class={el.get('class')} text={txt!r}")
            hits += 1
            if hits >= 25:  # cap noise
                logger.info("[croma][diag] (delivery-ish element dump capped at 25)")
                break

    # 4. Context excerpts around the phrases we care about most.
    for kw in ("not available", "unfortunately", "pincode", "notify me"):
        idx = html_lower.find(kw)
        if idx != -1:
            logger.info(f"[croma][diag] ...{html[max(0, idx - 90):idx + 90]!r}...")

    # 5. JSON-LD availability — the RAW value(s). Croma often reports stale
    #    InStock for universally-OOS items, which is what makes the checker
    #    return in-stock; seeing the literal value pins this down.
    found_ld = False
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except Exception:
            continue
        for item in (data if isinstance(data, list) else [data]):
            if isinstance(item, dict) and item.get("offers") is not None:
                avail = _offer_availability(item.get("offers", {}))
                if avail:
                    found_ld = True
                    logger.info(f"[croma][diag] JSON-LD availability={avail!r}")
    if not found_ld:
        logger.info("[croma][diag] JSON-LD availability: none found")

    # 6. OOS text patterns — presence in raw HTML vs visible text. True-in-text
    #    but False-in-html means the OOS wording is split across tags and the
    #    raw-HTML _OOS_PATTERNS check would miss it.
    for p in _OOS_PATTERNS:
        in_html = p in html_lower
        in_text = p in text
        if in_html or in_text:
            logger.info(f"[croma][diag] OOS pattern {p!r}: in_html={in_html} in_visible_text={in_text}")

    # 7. Buy Now / Add-to-Cart button probe — logged here because the main
    #    check() can return on stale InStock JSON-LD BEFORE it ever evaluates
    #    button state, so the disabled-button signal would otherwise never
    #    appear in the trail. Dumps the real classes/attrs + computed disabled.
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


def check(soup: BeautifulSoup, html: str) -> bool:
    html_lower = html.lower()
    text = _normalized_text(soup)

    _log_delivery_diagnostics(soup, html)

    # ── Delivery restriction — highest priority, overrides all other signals ───
    # On OOS pages Croma's delivery section shows "Not Available for your pincode"
    # or "Unfortunately not available for your location". Matched against the
    # normalized VISIBLE TEXT (tag/whitespace/class-agnostic); raw HTML is also
    # checked as a fallback. Checked before JSON-LD (which may carry stale InStock
    # data) and before button text (which exists on both in-stock and OOS pages).
    for pattern in _DELIVERY_RESTRICTION_PATTERNS:
        if pattern in text or pattern in html_lower:
            src = "visible-text" if pattern in text else "raw-html"
            logger.info(f"[croma] delivery restriction ({src}): '{pattern}' → False")
            return False

    # Scoped fallback: within any element whose class hints at delivery/
    # serviceability, an unavailability phrase is a reliable OOS signal — 'not
    # available' is safe here because it is scoped to the delivery element (not
    # matched globally, where it could appear as e.g. 'EMI not available').
    for el in soup.find_all(class_=True):
        cls = " ".join(el.get("class", [])).lower()
        if not any(tok in cls for tok in ("deliver", "serviceab", "pincode", "availab")):
            continue
        etxt = re.sub(r"\s+", " ", el.get_text(" ", strip=True)).lower()
        if any(sig in etxt for sig in ("not available", "unfortunately", "not serviceable")):
            logger.info(
                f"[croma] delivery element class={el.get('class')} "
                f"text={etxt[:80]!r} → False"
            )
            return False

    # ── JSON-LD pass — OutOfStock trusted immediately; InStock deferred ────────
    # Croma's structured data has been observed returning InStock for products
    # that are actually out of stock (stale / incorrect data). Trusting it
    # immediately caused every product to appear in-stock.
    # Strategy: return False on OutOfStock right away (reliable negative signal),
    # but hold any InStock signal and only confirm it after OOS text patterns
    # have had a chance to contradict it.
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
                    logger.info("[croma] JSON-LD: InStock (deferred — checking OOS text first)")
                    json_ld_in_stock = True
                elif "OutOfStock" in avail or "Discontinued" in avail:
                    logger.info("[croma] JSON-LD: OutOfStock/Discontinued → False")
                    return False
        except Exception:
            pass

    # ── OOS text patterns ─────────────────────────────────────────────────────
    for pattern in _OOS_PATTERNS:
        if pattern in html_lower:
            logger.info(f"[croma] OOS text: '{pattern}' → False")
            return False

    # ── Buy Now / Add to Cart button state — evaluated BEFORE confirming stale
    #    InStock JSON-LD. Croma disables these buttons on OOS pages via CLASS
    #    NAMES ONLY (e.g. 'disableBuyNow', 'disableCartBtn', 'disable-btn-in-pdp';
    #    no disabled attribute), and it still serves InStock JSON-LD for products
    #    that are universally out of stock. So a disabled primary button is a
    #    reliable OOS signal that must OVERRIDE the stale InStock, not be skipped
    #    after it. Buttons are collected via cart classes, button/anchor text,
    #    and add-to-cart-ish attributes (the vivo T4R Add-to-Cart uses the class
    #    'pdp-add-to-cart', caught here via its "Add to Cart" text).
    cart_buttons = []
    seen_ids = set()

    def _add_candidate(el) -> None:
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
            logger.info(
                f"[croma] active buy/cart <{el.name}> "
                f"{el.get_text(strip=True)[:30]!r} → True"
            )
            return True
        logger.info(
            f"[croma] all {len(cart_buttons)} buy/cart button(s) disabled → False "
            f"(overrides JSON-LD InStock={json_ld_in_stock})"
        )
        return False

    # ── No actionable buy/cart button found — fall back to JSON-LD InStock ─────
    if json_ld_in_stock:
        logger.info("[croma] JSON-LD InStock confirmed (no buttons to contradict) → True")
        return True

    logger.info("[croma] no conclusive signal → False")
    return False
