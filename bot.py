import asyncio
import logging
import os
from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from config import BOT_TOKEN
from database import init_db
from handlers import router
from stock_checker import stock_checker_loop

logging.basicConfig(level=logging.INFO)

# --- DUMMY WEB SERVER CODE START ---
async def handle_web(request):
    return web.Response(text="Stock Alert Bot is running perfectly!")

async def start_web_server():
    app = web.Application()
    app.router.add_get('/', handle_web)
    runner = web.AppRunner(app)
    await runner.setup()
    # Render automatically $PORT environment variable set karta hai
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    logging.info(f"Dummy Web Server started on port {port}")
# --- DUMMY WEB SERVER CODE END ---

async def main():
    if BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN_HERE":
        logging.error("Please set your BOT_TOKEN in config.py!")
        return

    # Initialize SQLite Database
    init_db()
    
    bot = Bot(
        token=BOT_TOKEN, 
        default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN)
    )
    dp = Dispatcher()
    dp.include_router(router)
    
    # 1. Start Web Server (Taki Render crash na kare)
    asyncio.create_task(start_web_server())
    
    # 2. Start Playwright loop
    asyncio.create_task(stock_checker_loop(bot))
    
    logging.info("Starting Telegram Bot...")
    
    # 3. Start Telegram bot polling
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Bot stopped!")
