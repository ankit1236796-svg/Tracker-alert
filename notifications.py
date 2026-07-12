"""
notifications.py
~~~~~~~~~~~~~~~~~
Shared proactive-alert logic used by both the automatic background loop
(bot.py) and manual check flows (handlers.py). Kept in its own module because
bot.py imports `router` from handlers.py — handlers.py importing back from
bot.py would be circular.

All user-facing text comes from translations.t(). Text builders take an
explicit `lang` (the dashboard passes the recipient's stored language); the
async senders resolve it themselves via database.get_user_lang so their
callers don't have to.
"""

import asyncio
import html
import logging

from aiogram import Bot

from config import UNRELIABLE_SITES, get_site_label
from database import get_user_lang, is_site_locked
from translations import t
from affiliate import get_affiliate_url
import whatsapp_client

logger = logging.getLogger(__name__)


def should_alert_for_price(product: dict, current_price: float | None) -> bool:
    """Amazon price gate: alert unless a target price is set, a current price
    was found, AND that price is above the target."""
    target_price = product.get("target_price")
    return (
        target_price is None
        or current_price is None
        or current_price <= target_price
    )


async def send_stock_alert(bot: Bot, product: dict, price: float | None = None):
    """
    Send an in-stock notification to the product owner, in their language.
    Sites in config.UNRELIABLE_SITES are gated here — the single call site
    every alert path shares — so a flaky site can't trigger a false push.
    """
    if product["site"] in UNRELIABLE_SITES:
        logger.warning(
            f"[alert-suppressed] site={product['site']!r} is in UNRELIABLE_SITES — "
            f"skipping automatic stock alert for product #{product['id']} to avoid "
            f"a possible false notification. Remove from config.UNRELIABLE_SITES "
            f"once the root cause is fixed."
        )
        return
    # Admin store lock (global or per-user): existing tracked items keep being
    # checked, but their alerts are suppressed while the store is locked —
    # mirroring the UNRELIABLE_SITES gate rather than deleting anything.
    if is_site_locked(product["site"], product["user_id"]):
        logger.info(
            f"[alert-suppressed] site={product['site']!r} is locked (global or for "
            f"user {product['user_id']}) — skipping stock alert for product #{product['id']}."
        )
        return
    lang = get_user_lang(product["user_id"])
    price_line = ""
    if price is not None:
        price_line = t("stock_alert_price_line", lang, price=f"{price:,.0f}")
    # Affiliate-convert the URL at notification time (freshly generated per
    # alert). For ineligible sites (Amazon, or anything not in
    # AFFILIATE_ENABLED_SITES) or on any conversion failure this returns the
    # original URL unchanged, so the alert always carries a working link.
    alert_url = await get_affiliate_url(product["url"], product["site"])
    text = t(
        "stock_alert", lang,
        name=product["name"], site=get_site_label(product["site"]),
        price_line=price_line, url=alert_url,
    )
    try:
        await bot.send_message(
            chat_id=product["user_id"],
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=False,
        )
        logger.info(
            f"Alert sent to user {product['user_id']} for product #{product['id']}"
        )
    except Exception as exc:
        logger.error(f"Failed to send alert: {exc}")

    # Best-effort forward to this user's OWN registered WhatsApp channel (if
    # any — see whatsapp_client.py). Fire-and-forget: scheduled as a separate
    # task rather than awaited, so it can NEVER delay or block the Telegram
    # send above (which has already completed by this point either way), and
    # any failure inside it is caught internally and only logged.
    asyncio.create_task(
        whatsapp_client.forward_alert(
            product, name=product["name"], site=product["site"],
            alert_url=alert_url, price=price, lang=lang,
        )
    )


async def _safe_send(bot: Bot, user_id: int, text: str) -> bool:
    """Send a plain HTML message to a user, logging (not raising) on failure —
    e.g. the user blocked the bot. Returns whether it succeeded."""
    try:
        await bot.send_message(chat_id=user_id, text=text, parse_mode="HTML")
        return True
    except Exception as exc:
        logger.error(f"Failed to message user {user_id}: {exc}")
        return False


# ── Message-text builders (lang-aware) ───────────────────────────────────────
# Pure functions returning the HTML message body in the given language, kept
# separate from the async senders so the web dashboard (which sends via the
# Telegram HTTP API from a non-async thread, resolving the recipient's lang
# from the DB) reuses the EXACT same text the bot sends.

def approval_notice_text(plan_name: str, days: int, access_until: str, lang: str = "en") -> str:
    return t("approval_notice", lang, plan=plan_name, days=days, until=access_until)


def rejection_notice_text(reason: str | None, lang: str = "en") -> str:
    reason_line = t("rejection_reason", lang, reason=reason) if reason else ""
    return t("rejection_notice", lang, reason=reason_line)


def items_removed_text(product_names: list[str], lang: str = "en") -> str:
    # Product names are user-supplied and this goes out as parse_mode=HTML, so
    # each is escaped. Single item keeps the one-line form; many are bulleted.
    names = [html.escape(n or "your item") for n in product_names] or ["your item"]
    tail = t("item_removed_tail", lang)
    if len(names) == 1:
        header = t("item_removed_single", lang, name=names[0])
    else:
        listed = "\n".join(f"• {n}" for n in names)
        header = f"{t('item_removed_multi_header', lang)}\n{listed}"
    return f"{header}\n\n{tail}"


def block_notice_text(lang: str = "en") -> str:
    return t("block_notice", lang)


def unblock_notice_text(lang: str = "en") -> str:
    return t("unblock_notice", lang)


async def send_approval_notice(bot: Bot, user_id: int, plan_name: str, days: int, access_until: str):
    await _safe_send(bot, user_id, approval_notice_text(plan_name, days, access_until, get_user_lang(user_id)))


async def send_rejection_notice(bot: Bot, user_id: int, reason: str | None):
    await _safe_send(bot, user_id, rejection_notice_text(reason, get_user_lang(user_id)))


async def send_expiry_reminder(bot: Bot, user_id: int, hours_left: float, is_trial: bool):
    lang = get_user_lang(user_id)
    kind = t("expiry_kind_trial" if is_trial else "expiry_kind_paid", lang)
    await _safe_send(bot, user_id, t("expiry_reminder", lang, kind=kind, hours=round(hours_left)))


async def send_block_notice(bot: Bot, user_id: int):
    await _safe_send(bot, user_id, block_notice_text(get_user_lang(user_id)))


async def send_unblock_notice(bot: Bot, user_id: int):
    await _safe_send(bot, user_id, unblock_notice_text(get_user_lang(user_id)))


async def send_data_purged_notice(bot: Bot, user_id: int, count: int):
    await _safe_send(bot, user_id, t("data_purged", get_user_lang(user_id), count=count))
