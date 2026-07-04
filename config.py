import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
DB_PATH = os.getenv("DB_PATH", "/app/data/stock_alerts.db")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "300"))  # seconds (5 min default)

# ---------------------------------------------------------------------------
# Admin / monetization
# ---------------------------------------------------------------------------
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "5004721766"))
TRIAL_DAYS = int(os.getenv("TRIAL_DAYS", "2"))
GRACE_PERIOD_DAYS = int(os.getenv("GRACE_PERIOD_DAYS", "7"))
REMINDER_HOURS_BEFORE_EXPIRY = int(os.getenv("REMINDER_HOURS_BEFORE_EXPIRY", "6"))
# How often the access-maintenance loop (reminders + grace-period purge) runs.
# Independent of CHECK_INTERVAL (stock checking) — needs finer granularity than
# once/day so the 6-hour-before-expiry reminder window isn't missed, but purging
# is idempotent so running it this often is harmless.
ACCESS_CHECK_INTERVAL = int(os.getenv("ACCESS_CHECK_INTERVAL", "1800"))  # 30 min default

# Number of WhatsApp share+confirm rounds required before /freetrial's
# one-time trial bonus can be claimed — see database.py's
# increment_share_trial_round / activate_share_trial.
SHARE_TRIAL_ROUNDS_REQUIRED = int(os.getenv("SHARE_TRIAL_ROUNDS_REQUIRED", "5"))

# Telegram never notifies a bot when a `url`-type inline button (like "Share
# on WhatsApp") is tapped — there's no event to detect it. This delay is the
# closest available approximation: the "Done" button for each /freetrial
# round is withheld for this many seconds after the share button appears, so
# it can't be spammed through instantly without at least a brief pause. It
# does NOT verify an actual share happened.
SHARE_TRIAL_TAP_DELAY_SECONDS = int(os.getenv("SHARE_TRIAL_TAP_DELAY_SECONDS", "3"))

# Playwright settings
PLAYWRIGHT_HEADLESS = True
PLAYWRIGHT_TIMEOUT = 30000  # ms

# Supported sites
SUPPORTED_SITES = {
    "amazon":          ["amazon.in", "amazon.com"],
    "flipkart":        ["flipkart.com"],
    "zepto":           ["zeptonow.com"],
    "bigbasket":       ["bigbasket.com"],
    "blinkit":         ["blinkit.com"],
    "croma":           ["croma.com"],
    "instamart":       ["swiggy.com"],
    "myntra":          ["myntra.com"],
    "jiomart":         ["jiomart.com"],
    "reliancedigital": ["reliancedigital.in"],
    "apple":           ["apple.com"],
    "oneplus":         ["oneplus.in"],
}
