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
