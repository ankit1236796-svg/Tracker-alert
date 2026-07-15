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

# ---------------------------------------------------------------------------
# Scraping provider (Scrape.do vs Zyte API)
# ---------------------------------------------------------------------------
# Scrape.do's credits ran out — Zyte API (https://api.zyte.com/v1/extract) is
# now the PRIMARY provider every checker fetch routes through (see
# checkers/common.py's fetch_page(), the central function all checkers and
# /debug* commands call, and zyte_client.py for the actual Zyte request/
# response handling). Scrape.do's own code path (build_scraper_url + a plain
# GET) is left FULLY INTACT and simply unused while this is "zyte" — flip it
# back to "scrapedo" here (or via the SCRAPING_PROVIDER env var) the moment
# Scrape.do credits are recharged; no code changes needed either way.
SCRAPING_PROVIDER = os.getenv("SCRAPING_PROVIDER", "zyte").strip().lower()
# ZYTE_API_KEY itself is read directly via os.environ in zyte_client.py at
# call time, not through this module — mirroring how SCRAPEDO_KEY is read
# directly via os.environ in checkers/common.py rather than through
# config.py, so a Railway env var change takes effect without an
# import-order dependency.

# ---------------------------------------------------------------------------
# Flipkart Affiliate API (alternative to Zyte/Scrape.do scraping, Flipkart
# only) — see checkers/flipkart_api.py. Defaults to "scraping" (today's
# unchanged behavior, checkers/flipkart.py via checkers.common.fetch_page);
# "api" makes checkers.flipkart_api.check_stock_with_fallback() try
# Flipkart's own Affiliate API first, automatically falling back to
# scraping on any failure (auth error, product not in the affiliate feed,
# rate limit, network error, unexpected response shape). NOT wired into
# the live check cycle yet — see admin_handlers.py's /debugflipkartapi,
# the verification step this needs to go through first regardless of
# which value this flag has.
FLIPKART_SOURCE = os.getenv("FLIPKART_SOURCE", "scraping").strip().lower()
# FLIPKART_AFFILIATE_ID / FLIPKART_AFFILIATE_TOKEN are read directly via
# os.environ in checkers/flipkart_api.py at call time, not through this
# module — mirroring SCRAPEDO_KEY/ZYTE_API_KEY's own pattern above.

# Supported sites
#
# Vijay Sales (vijaysales.com) deliberately NOT added — investigated and
# skipped, not "not yet built". Four separate diagnostic passes (via
# test_new_store_signals.py against real confirmed-OOS/in-stock product pages)
# each found a real reliability problem: JSON-LD offers.availability is
# static/stale (reads "InStock" on a confirmed-OOS page too); page-wide OOS
# text ("currently unavailable", "notify me") appears near the buy box on BOTH
# OOS and in-stock pages (likely a generic price-drop-alert widget, not real
# stock text); the Add to Cart button's disabled-attribute was INCONSISTENT
# across render modes for the same product (disabled=None at render=false,
# disabled='' at render=true) — a headless-browser hydration-timing artifact,
# not a real signal (the same trap that caused Croma's flip-flopping); and a
# dedicated stock-status class element found on the page ("instock__text")
# turned out to be identical + hidden (display:none) on both OOS and in-stock
# pages, i.e. dead/unused markup. A targeted search for Magento/Adobe
# Commerce's own internal GraphQL+MSI field names (stock_status, is_salable,
# salable_quantity) — the last, most specific hypothesis tried — also came
# back completely empty at both render modes, meaning the real stock data is
# most likely fetched via a separate XHR call after page load that's invisible
# to any HTML fetch regardless of render mode. Shipping a checker on any of
# the four unreliable signals would produce false alerts; skip until a better
# diagnostic approach is found or Vijay Sales changes its site architecture.
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
    # Brand storefronts (BBK group, like OnePlus). Bare-domain entries so the
    # shopping subdomains resolve via detect_site's endswith("."+domain) check
    # (e.g. mshop.vivo.com → vivo.com, mshop.iqoo.com/shop.iqoo.com → iqoo.com).
    "vivo":            ["vivo.com"],
    "iqoo":            ["iqoo.com"],
    "unicornstore":     ["unicornstore.in"],
    "vijaysales":       ["vijaysales.com"],
    "inventstore":      ["inventstore.in"],
    "sangeethamobiles": ["sangeethamobiles.com"],
    "shopatsc":         ["shopatsc.com"],
}

# Domains handled specially in /add with a "Coming Soon" message instead of
# the generic "unsupported site" one — see handlers.py's _coming_soon_message.
COMING_SOON_DOMAINS = {"croma.com"}

# Per-site display-name override for user-facing text (Telegram messages,
# product listings, /stores). Falls back to site.capitalize() via
# get_site_label() below for every site not listed here — most internal
# keys capitalize fine on their own (e.g. "amazon" -> "Amazon"), so this
# stays small and only covers sites where that would look wrong or omit
# useful context (e.g. "shopatsc" -> "Shopatsc" loses the fact that it's
# Sony India's official PS5 store).
SITE_DISPLAY_NAMES = {
    "shopatsc": "ShopAtSC (PS5 Official Site)",
}


def get_site_label(site: str) -> str:
    return SITE_DISPLAY_NAMES.get(site, site.capitalize())

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

# ---------------------------------------------------------------------------
# WhatsApp channel forwarding (separate whatsapp_forwarder/ service — see
# that directory's README for deployment. NOT the same process as this bot;
# Playwright/browser-automation dependencies live entirely over there.)
# ---------------------------------------------------------------------------
# Base URL of the whatsapp_forwarder service's internal API (no trailing
# slash), e.g. "https://whatsapp-forwarder.up.railway.app". Left unset by
# default — whatsapp_client.py treats an empty value as "feature not
# configured" and never attempts a request, so the bot behaves identically to
# today until this is explicitly set.
WHATSAPP_FORWARDER_URL = os.getenv("WHATSAPP_FORWARDER_URL", "").rstrip("/")
# Shared secret the bot sends as a Bearer token on every forward request; must
# match the same value configured on the whatsapp_forwarder service.
WHATSAPP_FORWARDER_SECRET = os.getenv("WHATSAPP_FORWARDER_SECRET", "")
