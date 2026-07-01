import asyncio
import logging
import os

from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand

from config import BOT_TOKEN, CHECK_INTERVAL
from database import init_db, get_all_products, update_stock_status
from handlers import router
from stock_checker import check_stock

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def _log_startup_checks():
    key = os.environ.get("SCRAPEDO_KEY", "")
    if key:
        masked = key[:4] + "*" * (len(key) - 4)
        logger.info(f"SCRAPEDO_KEY loaded: {masked}")
    else:
        logger.warning("SCRAPEDO_KEY is NOT set — Scrape.do calls will fail")


# ---------------------------------------------------------------------------
# Background stock checker
# ---------------------------------------------------------------------------

async def stock_checker_loop(bot: Bot):
    """
    Runs every CHECK_INTERVAL seconds.
    Checks all tracked products in parallel (max 3 concurrent ScraperAPI calls).
    Sends an alert when a product transitions from out-of-stock → in-stock.
    """
    logger.info("Stock checker loop started.")
    while True:
        try:
            products = get_all_products()
            logger.info(f"Checking {len(products)} product(s) in parallel…")

            sem = asyncio.Semaphore(3)

            async def _check_one(product: dict):
                async with sem:
                    try:
                        was_in_stock = bool(product["in_stock"])
                        now_in_stock = await check_stock(product["url"], product["site"])
                        update_stock_status(product["id"], now_in_stock)
                        if now_in_stock and not was_in_stock:
                            await send_stock_alert(bot, product)
                    except Exception as exc:
                        logger.error(f"Error processing product #{product['id']}: {exc}")

            await asyncio.gather(*[_check_one(p) for p in products])

        except Exception as exc:
            logger.error(f"Stock checker loop error: {exc}")

        await asyncio.sleep(CHECK_INTERVAL)


async def send_stock_alert(bot: Bot, product: dict):
    """Send an in-stock notification to the product owner."""
    text = (
        "🚨 <b>Back in Stock!</b>\n\n"
        f"📦 <b>{product['name']}</b> is now available on "
        f"<b>{product['site'].capitalize()}</b>!\n\n"
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


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def register_commands(bot: Bot) -> None:
    commands = [
        BotCommand(command="start",  description="Welcome message and command overview"),
        BotCommand(command="add",    description="Track a product (or bulk-add: Name | URL per line)"),
        BotCommand(command="list",   description="View all your tracked products"),
        BotCommand(command="check",  description="Check stock now (filter by store or check all)"),
        BotCommand(command="select", description="Select items to bulk-check or delete"),
        BotCommand(command="remove", description="Stop tracking a product"),
        BotCommand(command="search", description="Search tracked products by name"),
        BotCommand(command="stores", description="List all supported stores"),
        BotCommand(command="pins",   description="Manage your delivery pin codes"),
        BotCommand(command="cancel", description="Cancel the current operation"),
    ]
    await bot.set_my_commands(commands)
    logger.info(f"Registered {len(commands)} bot commands with Telegram")


async def main():
    _log_startup_checks()
    init_db()

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    await register_commands(bot)

    # Start the background checker as a concurrent task
    checker_task = asyncio.create_task(stock_checker_loop(bot))

    logger.info("Bot is starting…")
    try:
        await dp.start_polling(bot, allowed_updates=["message", "callback_query"])
    finally:
        checker_task.cancel()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
