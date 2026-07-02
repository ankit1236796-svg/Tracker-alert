"""
notifications.py
~~~~~~~~~~~~~~~~~
Shared proactive-alert logic used by both the automatic background loop
(bot.py) and manual check flows (handlers.py). Kept in its own module because
bot.py imports `router` from handlers.py — handlers.py importing back from
bot.py would be circular.
"""

import logging

from aiogram import Bot

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
    """Send an in-stock notification to the product owner."""
    price_line = f"\n💰 <b>Current price: ₹{price:,.0f}</b>" if price is not None else ""
    text = (
        "🚨 <b>Back in Stock!</b>\n\n"
        f"📦 <b>{product['name']}</b> is now available on "
        f"<b>{product['site'].capitalize()}</b>!{price_line}\n\n"
        f"🛒 <a href=\"{product['url']}\">Buy it now →</a>"
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


async def _safe_send(bot: Bot, user_id: int, text: str) -> bool:
    """Send a plain HTML message to a user, logging (not raising) on failure —
    e.g. the user blocked the bot. Returns whether it succeeded."""
    try:
        await bot.send_message(chat_id=user_id, text=text, parse_mode="HTML")
        return True
    except Exception as exc:
        logger.error(f"Failed to message user {user_id}: {exc}")
        return False


async def send_approval_notice(bot: Bot, user_id: int, plan_name: str, days: int, access_until: str):
    await _safe_send(
        bot, user_id,
        "✅ <b>Access approved!</b>\n\n"
        f"📦 Plan: <b>{plan_name}</b>\n"
        f"➕ Days added: <b>{days}</b>\n"
        f"📅 Access until: <b>{access_until}</b>\n\n"
        "Thanks for your payment — you're all set. Use /list to see your tracked items.",
    )


async def send_rejection_notice(bot: Bot, user_id: int, reason: str | None):
    reason_line = f"\n\nReason: {reason}" if reason else ""
    await _safe_send(
        bot, user_id,
        "❌ <b>Your access request was not approved.</b>"
        f"{reason_line}\n\nContact the admin if you have questions.",
    )


async def send_expiry_reminder(bot: Bot, user_id: int, hours_left: float, is_trial: bool):
    kind = "trial" if is_trial else "paid access"
    await _safe_send(
        bot, user_id,
        f"⏰ <b>Your {kind} expires in about {round(hours_left)} hour(s).</b>\n\n"
        "💳 To keep your alerts running, send an Amazon Gift Card to the admin "
        "(details to be shared) and include your Telegram user ID.\n\n"
        "📩 The admin will review and extend your access after payment.",
    )


async def send_block_notice(bot: Bot, user_id: int):
    await _safe_send(
        bot, user_id,
        "🚫 <b>Your access has been blocked by the admin.</b>\n\n"
        "Contact the admin if you believe this is a mistake.",
    )


async def send_unblock_notice(bot: Bot, user_id: int):
    await _safe_send(bot, user_id, "✅ <b>Your access has been restored.</b> Welcome back!")


async def send_data_purged_notice(bot: Bot, user_id: int, count: int):
    await _safe_send(
        bot, user_id,
        f"🗑 Your <b>{count}</b> tracked item(s) have been permanently deleted "
        "after your access grace period expired without renewal.\n\n"
        "You can start fresh any time once your access is restored.",
    )
