import asyncio
import logging
import os
import time

from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, BotCommandScopeChat, BotCommandScopeDefault

from access import AccessControlMiddleware, compute_access, STATUS_TRIAL, STATUS_LOCKED
from admin_handlers import router as admin_router
from config import BOT_TOKEN, CHECK_INTERVAL, ADMIN_USER_ID, ACCESS_CHECK_INTERVAL, REMINDER_HOURS_BEFORE_EXPIRY
from database import (
    init_db,
    get_all_products,
    update_stock_status,
    get_user_primary_pincode,
    list_all_users,
    mark_reminder_sent,
    purge_user_data,
    is_service_paused,
    list_paused_user_ids,
)
from handlers import router
from notifications import (
    send_stock_alert,
    should_alert_for_price,
    send_expiry_reminder,
    send_data_purged_notice,
)
from stock_checker import check_stock
from url_normalize import product_group_key

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

async def _apply_result_to_row(
    bot: Bot, product: dict, now_in_stock: bool | None, current_price: float | None
) -> None:
    """
    Apply one group's stock result to a single tracked-product row: persist the
    status and, on an out-of-stock → in-stock transition, fire the alert
    (respecting that row's OWN Amazon price gate). Extracted so the dedup
    fan-out applies the EXACT same per-row logic the non-deduped loop used to,
    once per user tracking the product — no user's alert/price-gate behaviour
    changes.

    now_in_stock is None for an inconclusive check (see
    stock_checker.check_stock's docstring) — skip the DB write/transition
    entirely rather than overwriting a real status with a guess.
    """
    if now_in_stock is None:
        logger.info(f"[bot] #{product['id']} check inconclusive — skipping status update")
        return
    was_in_stock = bool(product["in_stock"])
    update_stock_status(product["id"], now_in_stock)
    if now_in_stock and not was_in_stock:
        if should_alert_for_price(product, current_price):
            await send_stock_alert(bot, product, price=current_price)
        else:
            target_price = product.get("target_price")
            logger.info(
                f"[bot] price gate: #{product['id']} in stock "
                f"@ ₹{current_price:,.0f} > target ₹{target_price:,.0f} — skipping alert"
            )


async def run_stock_check_cycle(bot: Bot) -> dict:
    """
    One stock-check pass with cross-user deduplication.

    Groups every tracked product row (across ALL users) by
    url_normalize.product_group_key — i.e. (site, canonical-product-id,
    pincode). Each group is checked exactly ONCE via check_stock (using any one
    of the equivalent URLs), then that single result fans out to every row in
    the group so each user still gets their own status update + transition
    alert. This collapses redundant Scrape.do requests when multiple users
    track the same product (differently-formatted URLs for the same product
    normalize to the same id, so they group together).

    Safety: pincode is part of the group key ONLY for pincode-sensitive stores
    (Apple + quick-commerce — see url_normalize._PINCODE_SENSITIVE_SITES), so
    those never share a check across different pincodes; pincode-independent
    stores (Amazon, Flipkart, …) drop it and merge across pincodes since their
    result doesn't depend on it. A URL whose id can't be extracted confidently
    keys on its raw string, so distinct products never merge. The per-row
    fan-out is the same logic the old per-row loop ran.

    Extracted from stock_checker_loop so a single cycle is directly testable
    (mirrors run_access_maintenance_cycle). Returns a small stats dict.

    Pause/Resume Service: if the GLOBAL switch is on (is_service_paused()),
    returns immediately — no get_all_products() call, no grouping, no
    provider (Scrape.do/Zyte) requests at all, the efficient "essentially a
    global on/off switch" behavior the feature asks for, rather than
    iterating every user and skipping each one individually. Otherwise,
    products belonging to an INDIVIDUALLY-paused user (checks_paused=1)
    are filtered out before grouping, so their items are simply never
    checked this cycle — still saved in the DB, no notification sent
    either way (silent, admin-only visibility for both pause modes).
    """
    if is_service_paused():
        logger.info("[bot] service globally paused — skipping this check cycle entirely")
        return {"products": 0, "groups": 0, "saved": 0, "paused": True}

    products = get_all_products()
    paused_user_ids = set(list_paused_user_ids())
    if paused_user_ids:
        before_count = len(products)
        products = [p for p in products if p["user_id"] not in paused_user_ids]
        logger.info(
            f"[bot] excluding {before_count - len(products)} product(s) belonging to "
            f"{len(paused_user_ids)} individually-paused user(s) this cycle"
        )

    # One pincode lookup per user per cycle (cached), not per product.
    pincode_by_user: dict[int, str | None] = {}

    def _pincode_for(user_id: int) -> str | None:
        if user_id not in pincode_by_user:
            pincode_by_user[user_id] = get_user_primary_pincode(user_id)
        return pincode_by_user[user_id]

    groups: dict[str, list[dict]] = {}
    for product in products:
        pincode = _pincode_for(product["user_id"])
        product["_pincode"] = pincode
        key = product_group_key(product["site"], product["url"], pincode)
        groups.setdefault(key, []).append(product)

    saved = len(products) - len(groups)
    logger.info(
        f"Checking {len(products)} product(s) in {len(groups)} deduplicated "
        f"group(s) — {saved} redundant check(s) avoided this cycle."
    )

    sem = asyncio.Semaphore(10)

    async def _check_group(rows: list[dict]) -> None:
        async with sem:
            rep = rows[0]  # representative — every row here is the same product
            try:
                now_in_stock, current_price = await check_stock(
                    rep["url"], rep["site"], pincode=rep["_pincode"], caller="background"
                )
            except Exception as exc:
                logger.error(
                    f"Error checking group site={rep['site']!r} url={rep['url']!r} "
                    f"({len(rows)} row(s)): {exc}"
                )
                return
            for product in rows:
                try:
                    await _apply_result_to_row(bot, product, now_in_stock, current_price)
                except Exception as exc:
                    logger.error(f"Error applying result to product #{product['id']}: {exc}")

    await asyncio.gather(*[_check_group(rows) for rows in groups.values()])
    return {"products": len(products), "groups": len(groups), "saved": saved}


async def stock_checker_loop(bot: Bot):
    """
    Runs on a fixed CHECK_INTERVAL period measured from the start of each cycle,
    so the interval is not stacked on top of the checking time — a full cycle
    (checking + wait) targets CHECK_INTERVAL total rather than checking + interval.
    Checks all tracked products with cross-user deduplication (see
    run_stock_check_cycle), max 10 concurrent Scrape.do calls (matching the
    plan's concurrency limit).
    Sends an alert when a product transitions from out-of-stock → in-stock.
    For Amazon items with a target_price, only alerts when price ≤ target.
    """
    logger.info("Stock checker loop started.")
    while True:
        cycle_start = time.monotonic()
        try:
            await run_stock_check_cycle(bot)
        except Exception as exc:
            logger.error(f"Stock checker loop error: {exc}")

        # Sleep only the remainder of CHECK_INTERVAL, measured from cycle start,
        # so total cycle time ≈ CHECK_INTERVAL instead of checking_time + CHECK_INTERVAL.
        elapsed = time.monotonic() - cycle_start
        sleep_for = CHECK_INTERVAL - elapsed
        if sleep_for > 0:
            logger.info(
                f"Cycle finished in {elapsed:.1f}s; sleeping {sleep_for:.1f}s "
                f"until next cycle (interval={CHECK_INTERVAL}s)"
            )
            await asyncio.sleep(sleep_for)
        else:
            logger.warning(
                f"Cycle took {elapsed:.1f}s — longer than CHECK_INTERVAL "
                f"({CHECK_INTERVAL}s); starting next cycle immediately"
            )


# ---------------------------------------------------------------------------
# Access maintenance (expiry reminders + grace-period data purge)
# ---------------------------------------------------------------------------

async def run_access_maintenance_cycle(bot: Bot):
    """
    One pass over all users:
    - Sends a one-time reminder to users within REMINDER_HOURS_BEFORE_EXPIRY of
      their access_until (trial or paid). Tracked via reminder_sent_until so it
      fires exactly once per expiry cycle — comparing against the CURRENT
      access_until means it naturally re-arms the moment access is renewed.
    - Permanently purges tracked items for users whose GRACE_PERIOD_DAYS window
      (past access_until, with no admin block involved) has fully elapsed with
      no renewal. purge_user_data is a no-op on an already-empty list, so this
      is safe to re-run every cycle without double-purging or double-notifying.
    Extracted from access_maintenance_loop so a single cycle is directly
    testable without running the infinite loop.
    """
    users = list_all_users()
    for u in users:
        if u["user_id"] == ADMIN_USER_ID:
            # The admin is exempt from the entire trial/expiry system — never
            # send them an expiry reminder or payment prompt, and never purge
            # their data. database.py's init_db() also keeps their access_until
            # permanently far in the future as a backstop, but this check is
            # the direct guarantee: skip them here regardless of what's stored.
            continue
        info = compute_access(u)

        if info.has_access and info.days_remaining is not None:
            hours_left = info.days_remaining * 24
            if (
                hours_left <= REMINDER_HOURS_BEFORE_EXPIRY
                and u.get("reminder_sent_until") != u.get("access_until")
            ):
                await send_expiry_reminder(
                    bot, u["user_id"], hours_left, info.status == STATUS_TRIAL
                )
                mark_reminder_sent(u["user_id"], u["access_until"])

        elif info.status == STATUS_LOCKED and not u.get("blocked") and u.get("access_until"):
            # LOCKED-by-time (not an admin block) past the full grace window —
            # purge. Explicitly-blocked users are excluded: a block is a
            # moderation action, not a billing lapse, and must never trigger
            # data deletion on its own.
            count = purge_user_data(u["user_id"])
            if count:
                logger.info(
                    f"[access] purged {count} product(s) for expired user {u['user_id']}"
                )
                await send_data_purged_notice(bot, u["user_id"], count)


async def access_maintenance_loop(bot: Bot):
    """
    Runs every ACCESS_CHECK_INTERVAL seconds (separate cadence from the stock
    checker — this needs finer granularity than once/day so the
    REMINDER_HOURS_BEFORE_EXPIRY window isn't missed, but every action in
    run_access_maintenance_cycle is idempotent so running it often is harmless).
    """
    logger.info("Access maintenance loop started.")
    while True:
        try:
            await run_access_maintenance_cycle(bot)
        except Exception as exc:
            logger.error(f"Access maintenance loop error: {exc}")

        await asyncio.sleep(ACCESS_CHECK_INTERVAL)


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
        BotCommand(command="language", description="Change language (English / हिंदी / Hinglish)"),
        BotCommand(command="freetrial", description="Get a bonus free trial by sharing on WhatsApp"),
        BotCommand(command="setwhatsapp", description="Link your WhatsApp Channel/Community for alerts"),
        BotCommand(command="whatsappstatus", description="Check your WhatsApp channel link status"),
        BotCommand(command="cancel", description="Cancel the current operation"),
    ]
    await bot.set_my_commands(commands, scope=BotCommandScopeDefault())
    logger.info(f"Registered {len(commands)} default bot commands with Telegram")

    admin_commands = commands + [
        BotCommand(command="addplan",     description="[admin] Create a plan"),
        BotCommand(command="editplan",    description="[admin] Edit a plan field"),
        BotCommand(command="listplans",   description="[admin] List all plans"),
        BotCommand(command="deleteplan",  description="[admin] Delete an unused plan"),
        BotCommand(command="setuserplan", description="[admin] Assign a user to a plan"),
        BotCommand(command="approve",     description="[admin] Grant/extend access on a plan"),
        BotCommand(command="reject",      description="[admin] Deny a user's access request"),
        BotCommand(command="extend",      description="[admin] Add days without changing plan"),
        BotCommand(command="block",       description="[admin] Lock a user out"),
        BotCommand(command="unblock",     description="[admin] Restore a blocked user"),
        BotCommand(command="pending",     description="[admin] Users in trial or awaiting approval"),
        BotCommand(command="users",       description="[admin] List all users + status"),
        BotCommand(command="finduser",    description="[admin] Full profile for one user"),
        BotCommand(command="broadcast",   description="[admin] Message all active users"),
        BotCommand(command="stats",       description="[admin] Usage & revenue summary"),
        BotCommand(command="whatsapppending", description="[admin] WhatsApp channels awaiting approval"),
        BotCommand(command="whatsappapprove", description="[admin] Approve a user's WhatsApp channel"),
        BotCommand(command="whatsappdisable", description="[admin] Disable a user's WhatsApp forwarding"),
        BotCommand(command="managetracking", description="[admin] Bulk stop tracking / stop plan"),
        BotCommand(command="linksbystore",   description="[admin] Tracked links grouped by store"),
        BotCommand(command="creditusage",    description="[admin] Zyte API credit usage per store (this month / all)"),
        BotCommand(command="pauseservice",   description="[admin] Pause/resume background stock checking"),
        BotCommand(command="resumeservice",  description="[admin] Resume background stock checking"),
    ]
    # Scoped ONLY to the admin's own chat — regular users never see these in
    # their Telegram "/" menu, on top of being functionally unreachable to
    # them (admin_handlers.router is filtered to ADMIN_USER_ID).
    await bot.set_my_commands(admin_commands, scope=BotCommandScopeChat(chat_id=ADMIN_USER_ID))
    logger.info(f"Registered {len(admin_commands)} admin commands scoped to chat {ADMIN_USER_ID}")


async def main():
    _log_startup_checks()
    init_db()

    # Admin web dashboard: runs in a daemon thread in this same process (so it
    # shares this DB file), bound to Railway's $PORT. No-ops if
    # ADMIN_DASHBOARD_PASSWORD isn't set, so this changes nothing for a deploy
    # that hasn't configured it. Never raises — a dashboard failure won't stop
    # the bot. Started after init_db() so the schema exists before any request.
    from dashboard import start_dashboard_in_background
    start_dashboard_in_background()

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())

    # Registered on the Dispatcher itself (not a specific router) so it gates
    # every update regardless of which router ends up handling it. Safe for
    # admin commands too: the middleware unconditionally bypasses when
    # user.id == ADMIN_USER_ID as its very first check.
    access_middleware = AccessControlMiddleware()
    dp.message.outer_middleware(access_middleware)
    dp.callback_query.outer_middleware(access_middleware)

    # admin_router first: its handlers are filtered to ADMIN_USER_ID only, so
    # order relative to the main router doesn't affect regular users (command
    # names don't overlap) but keeps admin commands resolving first for clarity.
    dp.include_router(admin_router)
    dp.include_router(router)

    await register_commands(bot)

    # Background tasks: stock checking (existing) and access maintenance
    # (reminders + grace-period purge) run as independent concurrent loops
    # on their own cadences (CHECK_INTERVAL vs ACCESS_CHECK_INTERVAL).
    checker_task = asyncio.create_task(stock_checker_loop(bot))
    access_task = asyncio.create_task(access_maintenance_loop(bot))

    logger.info("Bot is starting…")
    try:
        await dp.start_polling(bot, allowed_updates=["message", "callback_query"])
    finally:
        checker_task.cancel()
        access_task.cancel()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
