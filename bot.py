import asyncio
import logging
from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from config import BOT_TOKEN
from database import init_db
from handlers import router
from stock_checker import stock_checker_loop

logging.basicConfig(level=logging.INFO)

async def main():
    if BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN_HERE":
        logging.error("Please set your BOT_TOKEN in config.py!")
        return

    # Initialize SQLite Database
    init_db()
    
    # Initialize Bot and Dispatcher
    bot = Bot(
        token=BOT_TOKEN, 
        default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN)
    )
    dp = Dispatcher()
    
    # Include the handlers
    dp.include_router(router)
    
    # Start the background Playwright loop for checking stocks
    asyncio.create_task(stock_checker_loop(bot))
    
    logging.info("Starting Telegram Bot...")
    
    # Start polling updates
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Bot stopped!")
