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
SHARE_TRIAL_TAP_DELAY_SECONDS = int(os.getenv("SHARE_TRIAL_TAP_DELAY_SECONDS", "10"))

# Sites whose results are confirmed unreliable enough that AUTOMATIC alerts
# must be suppressed until root-caused and fixed — added after Croma was
# observed flipping between correct and fully-inverted results across two
# manual checks ~8-9 minutes apart, for the same tracked products. Manual
# /check still runs and shows a result, but with an explicit reliability
# warning (see handlers.py) rather than silently trusting it. Remove a site
# from this set once the root cause is found and fixed.
UNRELIABLE_SITES = {"croma"}

# Playwright settings
PLAYWRIGHT_HEADLESS = True
PLAYWRIGHT_TIMEOUT = 30000  # ms

# Supported sites
#
# Croma deliberately excluded (not "not yet built" — actively removed): the
# checker was confirmed to flip between correct and fully-inverted results
# across consecutive checks, then later degraded to reporting every product
# OOS regardless of real status, with no root cause identified. Shipping
# that behavior would produce false alerts (or false silence) for users, so
# it's pulled from /add and /stores until the underlying cause is found and
# fixed. checkers/croma.py, its CHECKER_MAP/_JS_SITES entries, and
# config.UNRELIABLE_SITES are all left intact — re-adding "croma" here is
# the only step needed to bring it back once fixed.
SUPPORTED_SITES = {
    "amazon":          ["amazon.in", "amazon.com"],
    "flipkart":        ["flipkart.com"],
    "zepto":           ["zeptonow.com", "zepto.com"],
    "bigbasket":       ["bigbasket.com"],
    "blinkit":         ["blinkit.com"],
    "instamart":       ["swiggy.com"],
    "myntra":          ["myntra.com"],
    "jiomart":         ["jiomart.com"],
    "reliancedigital": ["reliancedigital.in"],
    "apple":           ["apple.com"],
    "oneplus":         ["oneplus.in"],
    "tataneu":         ["tataneu.com"],
}

# Domains handled specially in /add with a "Coming Soon" message instead of
# the generic "unsupported site" one — see handlers.py's _coming_soon_message.
COMING_SOON_DOMAINS = {"croma.com"}

# ---------------------------------------------------------------------------
# Affiliate-link conversion (EarnKaro / EK Affiliaters — see affiliate.py)
# ---------------------------------------------------------------------------
# Stores for which the bot attempts EarnKaro affiliate-link conversion on the
# "back in stock" alert. Conversion is best-effort: any failure falls back to
# the original URL, so a store listed here that EarnKaro doesn't actually
# support just won't convert (no harm). Amazon is ALWAYS excluded (handled
# separately via its own Associates tag) regardless of what's configured here.
# Override without a redeploy via the AFFILIATE_ENABLED_SITES env var
# (comma-separated site keys, e.g. "flipkart,myntra,ajio"); the default is the
# set confirmed working against the live API. The API key itself is read
# separately from EARNKARO_API_KEY at call time (see affiliate.py) and is never
# stored in code.
AFFILIATE_ENABLED_SITES = {
    s.strip().lower()
    for s in os.getenv("AFFILIATE_ENABLED_SITES", "flipkart,myntra").split(",")
    if s.strip()
}
AFFILIATE_ENABLED_SITES.discard("amazon")  # never, regardless of config
