import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
DB_PATH = os.getenv("DB_PATH", "/app/data/stock_alerts.db")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "300"))  # seconds (5 min default)

# Playwright settings
PLAYWRIGHT_HEADLESS = True
PLAYWRIGHT_TIMEOUT = 30000  # ms

# Supported sites
SUPPORTED_SITES = {
    "amazon": ["amazon.in", "amazon.com"],
    "flipkart": ["flipkart.com"],
    "zepto": ["zeptonow.com"],
    "bigbasket": ["bigbasket.com"],
}
