import asyncio
import logging
import os
import random
import time

import httpx
from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, BotCommandScopeChat, BotCommandScopeDefault
from bs4 import BeautifulSoup

from access import AccessControlMiddleware, compute_access, STATUS_TRIAL, STATUS_LOCKED
from admin_handlers import router as admin_router
from config import (
    BOT_TOKEN, CHECK_INTERVAL, ADMIN_USER_ID, ACCESS_CHECK_INTERVAL, REMINDER_HOURS_BEFORE_EXPIRY,
    APPLE_PICKUP_PINCODES, APPLE_OFFICIAL_PICKUP_ALERTS_ENABLED, APPLE_PICKUP_CHECK_INTERVAL,
    PLAYWRIGHT_SCRAPER_URL, PLAYWRIGHT_SCRAPER_INTERNAL_TOKEN, APPLE_COOKIE_REFRESH_INTERVAL,
    APPLE_COOKIE_REFRESH_PRODUCT_URL, APPLE_COOKIE_REFRESH_PINCODE, CROMA_CHECK_INTERVAL,
    APPLE_COOKIE_REFRESH_MAX_ATTEMPTS, APPLE_COOKIE_REFRESH_RETRY_DELAY_MIN_SECONDS,
    APPLE_COOKIE_REFRESH_RETRY_DELAY_MAX_SECONDS,
)
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
    get_all_pickup_tracking,
    get_apple_official_pickup_status,
    upsert_apple_official_pickup_status,
    set_apple_session_cookies,
    get_forward_channel,
    list_channel_forward_products,
    update_channel_forward_status,
    is_forwarding_paused,
    list_channel_forward_pickup,
    get_channel_forward_pincodes,
    log_pickup_alert_event,
)
from handlers import router
from notifications import (
    send_stock_alert,
    should_alert_for_price,
    send_expiry_reminder,
    send_data_purged_notice,
    send_pickup_alert,
    send_channel_stock_alert,
)
from stock_checker import check_stock
from checkers import apple as apple_checker, fetch_page
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

    Croma products are excluded from this cycle entirely — see
    run_croma_check_cycle below, which checks them on its own
    CROMA_CHECK_INTERVAL cadence instead.

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

    # Croma products are excluded here — they run on their own dedicated
    # CROMA_CHECK_INTERVAL cadence instead (see run_croma_check_cycle below
    # and stock_checker_loop's next_croma_run gate), mirroring the Apple
    # pickup group's own separate-interval pattern. Every other site's
    # grouping/checking below is completely unaffected by this filter.
    products = [p for p in get_all_products() if p["site"] != "croma"]
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


# ---------------------------------------------------------------------------
# Croma-only check cycle — runs on its own CROMA_CHECK_INTERVAL cadence
# (see stock_checker_loop's next_croma_run gate) instead of the shared
# CHECK_INTERVAL, mirroring the existing Apple-pickup-group pattern
# (APPLE_PICKUP_CHECK_INTERVAL/next_apple_pickup_run) for isolating one
# site/feature onto its own timing without touching every other site's
# cadence. Croma products are excluded from run_stock_check_cycle above
# (see its own docstring) so they're checked exactly once per cycle, via
# this function only.
# ---------------------------------------------------------------------------

async def run_croma_check_cycle(bot: Bot) -> dict:
    """
    Croma-only sibling of run_stock_check_cycle above — identical
    cross-user dedup + apply-result logic (product_group_key grouping,
    _apply_result_to_row for the status update/alert), just filtered to
    site == "croma" and run on its own clock. Duplicated rather than
    sharing code with run_stock_check_cycle so that function's own
    behavior for every other site stays completely untouched — mirrors
    this codebase's existing precedent of small, deliberate duplication
    over refactoring shared logic (e.g. handlers.py's manual /check flow
    duplicating this same shape from bot.py, for the same "don't risk the
    shared path" reasoning). Returns a small stats dict, same shape as
    run_stock_check_cycle's own.
    """
    if is_service_paused():
        logger.info("[croma] service globally paused — skipping this check cycle entirely")
        return {"products": 0, "groups": 0, "saved": 0, "paused": True}

    products = [p for p in get_all_products() if p["site"] == "croma"]
    paused_user_ids = set(list_paused_user_ids())
    if paused_user_ids:
        before_count = len(products)
        products = [p for p in products if p["user_id"] not in paused_user_ids]
        logger.info(
            f"[croma] excluding {before_count - len(products)} product(s) belonging to "
            f"{len(paused_user_ids)} individually-paused user(s) this cycle"
        )

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
        f"[croma] checking {len(products)} product(s) in {len(groups)} deduplicated "
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
                    f"[croma] error checking group url={rep['url']!r} ({len(rows)} row(s)): {exc}"
                )
                return
            for product in rows:
                try:
                    await _apply_result_to_row(bot, product, now_in_stock, current_price)
                except Exception as exc:
                    logger.error(f"[croma] error applying result to product #{product['id']}: {exc}")

    await asyncio.gather(*[_check_group(rows) for rows in groups.values()])
    return {"products": len(products), "groups": len(groups), "saved": saved}


# ---------------------------------------------------------------------------
# Channel-forwarding stock alerts (separate feature/table from the regular
# per-user products table above — see database.forward_channel /
# channel_forward_tracking and admin_handlers.py's /setchannel, /addchannel,
# /stopforwarding, /listforwarding). Runs on the SAME CHECK_INTERVAL cadence
# as run_stock_check_cycle (called right after it in stock_checker_loop
# below), per the feature's own requirement that these get checked as often
# as regular tracked items — unlike the Apple pickup group further down,
# which deliberately runs on its own longer interval.
# ---------------------------------------------------------------------------

async def _apply_result_to_channel_forward_row(
    bot: Bot, row: dict, now_in_stock: bool | None, current_price: float | None
) -> None:
    """
    Same transition-detection shape as _apply_result_to_row above (was_in_
    stock -> now_in_stock, alert only on a genuine OOS->InStock flip), but
    persists to channel_forward_tracking and alerts the single registered
    channel instead of a product owner. No target_price gate — that's an
    Amazon-per-user concept (products.target_price); this table has no
    such column, and nothing here would set one.
    """
    if now_in_stock is None:
        logger.info(f"[channel-forward] #{row['id']} check inconclusive — skipping status update")
        return
    was_in_stock = bool(row["in_stock"])
    update_channel_forward_status(row["id"], now_in_stock)
    if now_in_stock and not was_in_stock:
        if is_forwarding_paused():
            logger.info(
                f"[channel-forward] #{row['id']} transitioned to in-stock but "
                f"forwarding is paused (/forwardingtoggle) — alert suppressed."
            )
            return
        channel = get_forward_channel()
        if not channel:
            logger.warning(
                f"[channel-forward] #{row['id']} transitioned to in-stock but no "
                f"channel is registered (run /setchannel) — alert skipped."
            )
            return
        await send_channel_stock_alert(bot, channel["chat_id"], row, price=current_price)


async def _check_channel_forward_stock_row(
    row: dict, configured_pincodes: list[str],
) -> tuple[bool | None, float | None]:
    """
    Resolves a pincode for one channel_forward_tracking row's check_stock
    call from the admin-configured channel-forward pincodes (see
    admin_handlers.py's /setchannelpincode) — the substitute for the
    per-user pincode run_stock_check_cycle resolves via
    get_user_primary_pincode, since these rows have no owning user.

    No pincodes configured yet -> pincode=None, exactly today's existing
    behavior (inconclusive for Croma/RelianceDigital/quick-commerce,
    generic-page-only for Apple) — /setchannelpincode simply hasn't been
    run yet.

    Croma/RelianceDigital (and everything else) -> the FIRST configured
    pincode only: their APIs answer "is this deliverable to THIS exact
    address", a genuinely different question per pincode, so there's no
    "try several" — one real pincode is all that's needed to stop coming
    back inconclusive, and picking a second arbitrary one wouldn't make
    the first any less valid.

    Apple -> tries EVERY configured pincode in turn, stopping at the first
    one that confirms in-stock. Unlike Croma/RelianceDigital,
    checkers.apple.refine_with_pincode only ever CONFIRMS an already
    in-stock generic-page result via nearby-store pickup availability —
    it never downgrades — so trying multiple pincodes purely improves the
    odds of catching a genuine confirmation instead of committing to
    whichever pincode happens to be first. Each attempt's pincode/result
    is logged so per-pincode availability is visible in Railway logs even
    though channel_forward_tracking's schema only stores one boolean per
    row (unlike channel_forward_pickup_tracking's own per-pincode dict).
    """
    if not configured_pincodes:
        return await check_stock(row["url"], row["site"], pincode=None, caller="background")

    if row["site"] != "apple":
        return await check_stock(
            row["url"], row["site"], pincode=configured_pincodes[0], caller="background"
        )

    now_in_stock: bool | None = None
    current_price: float | None = None
    for pincode in configured_pincodes:
        now_in_stock, current_price = await check_stock(
            row["url"], "apple", pincode=pincode, caller="background"
        )
        logger.info(
            f"[channel-forward] #{row['id']} apple pincode={pincode!r} -> "
            f"{'IN STOCK' if now_in_stock else ('OUT OF STOCK' if now_in_stock is False else 'INCONCLUSIVE')}"
        )
        if now_in_stock:
            break
    return now_in_stock, current_price


async def run_channel_forward_check_cycle(bot: Bot) -> dict:
    """
    One check pass across every channel_forward_tracking row (stock) —
    channel_forward_pickup_tracking (Apple pickup) is checked separately
    by run_channel_forward_pickup_check_cycle below, on the fully
    decoupled apple_pickup_check_loop cadence (2026-07-28 — see that
    loop's own module note for why: page-render pickup checks launch a
    real headful-browser check per pincode and could otherwise stall this
    fast CHECK_INTERVAL-paced stock cadence for every OTHER admin-curated
    channel-forward product). No cross-row dedup (unlike
    run_stock_check_cycle) — this list is admin-curated and expected to
    be small, so there's no meaningful redundant-fetch cost to save.
    Shares the same is_service_paused() global gate as the regular stock
    checker (a global pause should stop ALL automated checking, not just
    per-user tracking).

    Rows resolve their pincode from the admin-configured channel-forward
    pincode(s) (see /setchannelpincode and _check_channel_forward_stock_row
    above) — channel_forward_tracking rows have no owning user, so
    there's no per-user pincode to resolve the way run_stock_check_cycle
    does. Until an admin configures one, behavior is unchanged from
    before: pincode=None, inconclusive for Croma/RelianceDigital/
    quick-commerce.
    """
    if is_service_paused():
        logger.info("[channel-forward] service globally paused — skipping this check cycle entirely")
        return {"tracked": 0, "paused": True}

    rows = list_channel_forward_products()
    configured_pincodes = get_channel_forward_pincodes()

    sem = asyncio.Semaphore(10)

    async def _check_row(row: dict) -> None:
        async with sem:
            try:
                now_in_stock, current_price = await _check_channel_forward_stock_row(row, configured_pincodes)
            except Exception as exc:
                logger.error(f"[channel-forward] error checking #{row['id']} url={row['url']!r}: {exc}")
                return
            try:
                await _apply_result_to_channel_forward_row(bot, row, now_in_stock, current_price)
            except Exception as exc:
                logger.error(f"[channel-forward] error applying result to #{row['id']}: {exc}")

    await asyncio.gather(*[_check_row(row) for row in rows])
    return {"tracked": len(rows)}


async def run_channel_forward_pickup_check_cycle(bot: Bot) -> dict:
    """
    One check pass across every channel_forward_pickup_tracking row (Apple
    pickup — see checkers.apple.check_channel_pickup_row). Split out of
    run_channel_forward_check_cycle above (2026-07-28) when Apple pickup
    checking as a whole was decoupled onto its own independent,
    concurrent apple_pickup_check_loop — see that loop's own module note.
    Same is_service_paused() global gate as every other cycle; separate
    from is_forwarding_paused(), which only suppresses the ALERT send,
    not the check itself (see check_channel_pickup_row), keeping
    /listforwarding and /checkforwarding accurate even while forwarding
    is toggled off.
    """
    if is_service_paused():
        logger.info("[channel-forward][pickup] service globally paused — skipping this check cycle entirely")
        return {"pickup_tracked": 0, "paused": True}

    pickup_rows = list_channel_forward_pickup()
    sem = asyncio.Semaphore(10)

    async def _check_pickup_row(row: dict) -> None:
        async with sem:
            try:
                await apple_checker.check_channel_pickup_row(bot, row)
            except Exception as exc:
                logger.error(f"[channel-forward][pickup] error checking #{row['id']}: {exc}")

    await asyncio.gather(*[_check_pickup_row(row) for row in pickup_rows])
    return {"pickup_tracked": len(pickup_rows)}


# ---------------------------------------------------------------------------
# Apple Store pickup-availability tracking (separate feature/table from the
# regular stock checker above — see database.pickup_tracking, /trackpickup
# in handlers.py, and checkers/apple.py's pickup section). Runs on the same
# CHECK_INTERVAL cadence as run_stock_check_cycle (called right after it in
# stock_checker_loop below) rather than its own separate timer.
# ---------------------------------------------------------------------------

async def _check_pickup_row(bot: Bot, row: dict) -> dict:
    """
    Thin wrapper around checkers.apple.check_pickup_row — the actual check/
    persist/notify logic now lives there so handlers.py's on-demand
    /mypickups command can share it too, without importing bot.py (which
    would be circular — bot.py imports handlers.router). Kept as a bot.py-
    local name for backward compatibility with existing call sites/tests.
    """
    return await apple_checker.check_pickup_row(bot, row)


async def run_pickup_check_cycle(bot: Bot) -> dict:
    """
    One pickup-availability check pass across every /trackpickup row (all
    users). Same pause semantics as run_stock_check_cycle: a global pause
    skips the cycle entirely, individually-paused users' rows are excluded.
    No cross-user/cross-row deduplication (unlike run_stock_check_cycle) —
    pickup tracking is expected to be low-volume, and each row already
    carries its own cached SKU, so there's no redundant page-fetch to save.
    """
    if is_service_paused():
        logger.info("[pickup] service globally paused — skipping this check cycle entirely")
        return {"tracked": 0, "paused": True}

    rows = get_all_pickup_tracking()
    paused_user_ids = set(list_paused_user_ids())
    if paused_user_ids:
        before_count = len(rows)
        rows = [r for r in rows if r["user_id"] not in paused_user_ids]
        logger.info(
            f"[pickup] excluding {before_count - len(rows)} tracked pickup row(s) "
            f"belonging to {len(paused_user_ids)} individually-paused user(s) this cycle"
        )

    if not rows:
        return {"tracked": 0}

    sem = asyncio.Semaphore(10)

    async def _bounded(row):
        async with sem:
            await _check_pickup_row(bot, row)

    await asyncio.gather(*[_bounded(row) for row in rows])
    return {"tracked": len(rows)}


# ---------------------------------------------------------------------------
# Apple official-store pickup auto-check (checkers.apple.
# check_pickup_at_official_stores + database.apple_official_pickup_status)
# — a THIRD, separate Apple signal, distinct from both the generic
# check()-based alert above and the opt-in /trackpickup system. Runs
# automatically for every /add-tracked apple.com product, checking the 6
# fixed official-store pincodes (config.APPLE_PICKUP_PINCODES) rather than
# any user-chosen ones. See config.py's own module note on all three.
# ---------------------------------------------------------------------------

async def _check_apple_official_pickup_group(bot: Bot, url: str, rows: list[dict]) -> None:
    """
    Checks one distinct Apple product URL (shared across every user
    tracking that exact URL — see apple_official_pickup_status's own
    table comment for why this is keyed by URL, not per-user/per-row) and
    fans out any new-availability notification to every one of those
    users. `rows` is never empty (only called with a real group).
    """
    cached = get_apple_official_pickup_status(url)
    sku = cached["sku"] if cached else None

    if not sku:
        try:
            resp = await fetch_page(url, render_js=apple_checker.NEEDS_JS, timeout=30.0, site="apple")
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")
            sku = apple_checker._extract_sku(soup, resp.text)
        except Exception as exc:
            logger.error(f"[apple][official-stores] product page fetch/SKU extraction failed for {url!r}: {exc}")
            return
        if not sku:
            logger.warning(f"[apple][official-stores] could not extract a SKU from {url!r} — skipping this cycle")
            return

    try:
        results = await apple_checker.check_pickup_at_official_stores(sku, APPLE_PICKUP_PINCODES, product_url=url)
    except Exception as exc:
        logger.error(f"[apple][official-stores] check failed for {url!r} sku={sku!r}: {exc}")
        return

    prior_status: dict = cached["pincode_status"] if cached else {}
    new_status = dict(prior_status)
    representative_name = rows[0]["name"]

    for pincode in APPLE_PICKUP_PINCODES:
        if pincode not in results:
            continue  # this pincode's request failed this cycle — leave its prior status untouched
        stores = results[pincode]
        now_available = bool(stores)
        was_available = bool(prior_status.get(pincode, False))
        new_status[pincode] = now_available

        if now_available and not was_available:
            logger.info(
                f"[apple][official-stores] {url!r} pincode={pincode!r} "
                f"({', '.join(s['store_name'] for s in stores)}) transitioned to available"
            )
            log_pickup_alert_event(
                "official_stores", None, pincode, "transition_true",
                f"sku={sku!r} url={url!r} stores={[s.get('store_name') for s in stores]}",
            )
            if APPLE_OFFICIAL_PICKUP_ALERTS_ENABLED:
                for row in rows:
                    try:
                        send_status = await send_pickup_alert(bot, row["user_id"], representative_name, pincode, stores)
                    except Exception as exc:
                        logger.error(
                            f"[apple][official-stores] alert failed for user {row['user_id']} "
                            f"url={url!r} pincode={pincode!r}: {exc}"
                        )
                        log_pickup_alert_event("official_stores", None, pincode, "alert_send_exception", str(exc))
                    else:
                        event = "alert_sent" if send_status == "sent" else (
                            "alert_suppressed_locked" if send_status == "suppressed_locked" else "alert_send_error"
                        )
                        log_pickup_alert_event(
                            "official_stores", None, pincode, event,
                            f"user_id={row['user_id']} status={send_status}",
                        )
            else:
                logger.info(
                    "[apple][official-stores] alert suppressed — "
                    "config.APPLE_OFFICIAL_PICKUP_ALERTS_ENABLED is False"
                )
                log_pickup_alert_event(
                    "official_stores", None, pincode, "alert_suppressed_config_disabled",
                    f"url={url!r}",
                )

    try:
        upsert_apple_official_pickup_status(url, sku, new_status)
    except Exception as exc:
        logger.error(f"[apple][official-stores] error persisting pincode_status for {url!r}: {exc}")
        log_pickup_alert_event("official_stores", None, None, "status_persist_error", f"url={url!r} {exc}")


async def run_apple_official_pickup_cycle(bot: Bot) -> dict:
    """
    One check pass across every /add-tracked apple.com product's fixed 6
    official-store pincodes (all users, cross-user-deduplicated by exact
    URL — see _check_apple_official_pickup_group). Same pause semantics as
    every other cycle: a global pause skips entirely; individually-paused
    users' rows are excluded before grouping.
    """
    if is_service_paused():
        logger.info("[apple][official-stores] service globally paused — skipping this check cycle entirely")
        return {"products": 0, "groups": 0, "paused": True}

    products = [p for p in get_all_products() if p["site"] == "apple"]
    paused_user_ids = set(list_paused_user_ids())
    if paused_user_ids:
        products = [p for p in products if p["user_id"] not in paused_user_ids]

    if not products:
        return {"products": 0, "groups": 0}

    groups: dict[str, list[dict]] = {}
    for product in products:
        groups.setdefault(product["url"], []).append(product)

    sem = asyncio.Semaphore(10)

    async def _bounded(url, rows):
        async with sem:
            await _check_apple_official_pickup_group(bot, url, rows)

    await asyncio.gather(*[_bounded(url, rows) for url, rows in groups.items()])
    return {"products": len(products), "groups": len(groups)}


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

    Also runs run_channel_forward_check_cycle EVERY iteration (same
    CHECK_INTERVAL cadence as the regular stock check, per that feature's
    own requirement — see database.forward_channel/channel_forward_
    tracking and admin_handlers.py's /addchannel) — a separate,
    admin-curated list whose alerts go to a single registered Telegram
    channel instead of individual users.

    Also runs run_croma_check_cycle (Croma products only — excluded from
    run_stock_check_cycle above) on its OWN CROMA_CHECK_INTERVAL cadence,
    via a "next due" timestamp pattern — every other site's timing here
    is unaffected.

    2026-07-28: Apple pickup checking (run_pickup_check_cycle,
    run_apple_official_pickup_cycle, run_channel_forward_pickup_check_
    cycle) no longer runs from this loop at all — it's a fully
    independent, concurrent task (apple_pickup_check_loop, started
    alongside this one in main()) on its own APPLE_PICKUP_CHECK_INTERVAL
    cadence, so a slow page-render pickup cycle (each pincode check
    launches a real headful-browser check, can legitimately take up to a
    minute or more) can NEVER stall this loop's regular CHECK_INTERVAL-
    paced checking for every other tracked site. See apple_pickup_check_
    loop's own module note for the full detail — this used to be a
    same-loop "next due" timestamp gate (next_apple_pickup_run), same
    pattern as next_croma_run above, but that only controlled how OFTEN
    the pickup cycle STARTED, not how long a slow one blocked everything
    else once it did.
    """
    logger.info("Stock checker loop started.")
    next_croma_run = time.monotonic()  # due immediately on the first iteration
    while True:
        cycle_start = time.monotonic()
        try:
            await run_stock_check_cycle(bot)
        except Exception as exc:
            logger.error(f"Stock checker loop error: {exc}")

        try:
            await run_channel_forward_check_cycle(bot)
        except Exception as exc:
            logger.error(f"Channel-forward checker cycle error: {exc}")

        if cycle_start >= next_croma_run:
            try:
                await run_croma_check_cycle(bot)
            except Exception as exc:
                logger.error(f"Croma checker cycle error: {exc}")
            next_croma_run = cycle_start + CROMA_CHECK_INTERVAL

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
# Apple pickup-checking — fully independent, concurrent loop (2026-07-28),
# decoupled from stock_checker_loop entirely (see that loop's own module
# note on why). Covers every Apple-pickup-related cycle: run_pickup_check_
# cycle (personal /trackpickup rows), run_apple_official_pickup_cycle (the
# fixed-6-official-store auto-check), and run_channel_forward_pickup_check_
# cycle (admin-curated channel-forward pickup rows) — all three now go
# through checkers.apple's page-render pickup checker (playwright_scraper),
# which launches a real headful-browser check per pincode and can take up
# to roughly a minute per check in a realistic worst case (see
# playwright_scraper/main.py's _check_pickup_availability retry-loop note).
# Previously these ran sequentially INSIDE stock_checker_loop, gated by a
# "next due" timestamp so they'd only START every APPLE_PICKUP_CHECK_
# INTERVAL — but that gate only controlled START frequency, not how long a
# slow cycle blocked the same loop's regular CHECK_INTERVAL-paced checking
# for every OTHER tracked site once it began. Running as its own task
# (started in main() alongside stock_checker_loop, access_maintenance_loop,
# apple_cookie_refresh_loop) means a slow pickup cycle can never delay any
# other site's checking, ever, regardless of how long it takes.
#
# Same self-pacing "run, then sleep INTERVAL" pattern as apple_cookie_
# refresh_loop below (not "sleep the remainder" like stock_checker_loop) —
# deliberately simple: if one pass runs long, the next one just starts
# that much later rather than trying to catch up or overlap itself. Since
# this loop is fully independent, a long-running pass here has zero effect
# on any other loop's own cadence.
#
# Telegram alerts (send_pickup_alert / send_channel_pickup_alert) are
# unaffected by this move — they still go through the SAME `bot` instance
# passed into every cycle call here, to the exact same users/chats as
# before; only the scheduling/concurrency changed, not delivery.
# ---------------------------------------------------------------------------

async def apple_pickup_check_loop(bot: Bot):
    logger.info(
        f"[apple][pickup] independent check loop started (interval={APPLE_PICKUP_CHECK_INTERVAL}s)"
    )
    while True:
        try:
            await run_pickup_check_cycle(bot)
        except Exception as exc:
            logger.error(f"[apple][pickup] Pickup checker cycle error: {exc}")

        try:
            await run_apple_official_pickup_cycle(bot)
        except Exception as exc:
            logger.error(f"[apple][pickup] Apple official-store pickup cycle error: {exc}")

        try:
            await run_channel_forward_pickup_check_cycle(bot)
        except Exception as exc:
            logger.error(f"[apple][pickup] Channel-forward pickup checker cycle error: {exc}")

        await asyncio.sleep(APPLE_PICKUP_CHECK_INTERVAL)


# ---------------------------------------------------------------------------
# Apple cookie auto-refresher — replaces manual DevTools cookie extraction +
# a Railway env var update with a periodic real-browser session, run by the
# separate playwright_scraper service (POST /refresh-apple-cookies). A
# genuine Chromium session carries a real, matching TLS fingerprint at the
# point Apple mints the cookies, unlike httpx's own fingerprint — see
# config.py's APPLE_COOKIE_REFRESH_* settings for the full rationale. The
# fast per-check requests (check_pickup_at_official_stores, /trackpickup,
# refine_with_pincode) are UNCHANGED — still plain httpx, reading whatever
# session this loop last stored via database.get_apple_session_cookies (see
# checkers/apple.py's _resolve_apple_session). Runs as its own independent
# loop, own cadence — a Playwright round-trip (~5-30s) has no reason to
# share timing with CHECK_INTERVAL or APPLE_PICKUP_CHECK_INTERVAL.
# ---------------------------------------------------------------------------

async def _request_apple_cookie_refresh() -> dict | None:
    """One POST to playwright_scraper's /refresh-apple-cookies. Returns the
    parsed response dict on any response carrying usable (non-empty)
    cookies + user_agent, else None — network errors, non-200 status,
    non-JSON bodies, and missing cookies/user_agent are all logged here
    and collapsed to None so run_apple_cookie_refresh_cycle's retry loop
    has one simple thing to check. Never raises."""
    headers = {}
    if PLAYWRIGHT_SCRAPER_INTERNAL_TOKEN:
        headers["X-Internal-Token"] = PLAYWRIGHT_SCRAPER_INTERNAL_TOKEN

    try:
        async with httpx.AsyncClient(timeout=90.0) as client:
            resp = await client.post(
                f"{PLAYWRIGHT_SCRAPER_URL}/refresh-apple-cookies",
                json={
                    "url": APPLE_COOKIE_REFRESH_PRODUCT_URL,
                    "pincode": APPLE_COOKIE_REFRESH_PINCODE,
                },
                headers=headers,
            )
    except Exception as exc:
        logger.warning(f"[apple][cookie-refresh] request to playwright_scraper failed: {exc}")
        return None

    if resp.status_code != 200:
        logger.warning(
            f"[apple][cookie-refresh] playwright_scraper returned HTTP "
            f"{resp.status_code}: {resp.text[:300]!r}"
        )
        return None

    try:
        data = resp.json()
    except Exception as exc:
        logger.warning(f"[apple][cookie-refresh] non-JSON response from playwright_scraper: {exc}")
        return None

    cookies = (data.get("cookies") or "").strip()
    user_agent = (data.get("user_agent") or "").strip()
    if not cookies or not user_agent:
        logger.warning(
            f"[apple][cookie-refresh] playwright_scraper response missing "
            f"cookies/user_agent: {data}"
        )
        return None

    return data


async def run_apple_cookie_refresh_cycle() -> bool:
    """
    One refresh CYCLE — up to APPLE_COOKIE_REFRESH_MAX_ATTEMPTS calls to
    _request_apple_cookie_refresh, stopping early the moment one comes
    back with pincode_check_confirmed=True (the strongest available
    signal that Akamai actually cleared this specific session; see
    checkers/apple.py's investigation notes on why headers/params/UA
    alone don't guarantee a working session — this turned out to be
    genuinely probabilistic per-attempt, not deterministic).

    If NONE of the attempts confirm, the LAST usable (cookies+UA
    non-empty) attempt is still stored — matching the ORIGINAL single-
    attempt behavior as a floor, so a run of bad luck never leaves the DB
    session stale for the rest of APPLE_COOKIE_REFRESH_INTERVAL — but only
    after genuinely trying to do better first, not settling for whatever
    the first attempt produced regardless.

    A randomized delay is inserted BETWEEN attempts (not just relying on
    each attempt's own ~15-30s browser-launch-and-dwell duration) —
    repeated rapid page loads from the same IP/session in a tight burst
    is itself a bot signal, so retrying too aggressively could make
    Akamai's assessment WORSE, not better (see
    checkers.apple.check_pickup_at_official_stores's own pincode-delay
    reasoning, applied here for the same reason).

    Returns True if ANY attempt produced a usable session that got
    stored (confirmed or not); False only if every attempt failed
    outright (no usable cookies/user_agent from any of them) — same
    external contract as before, callers don't need to change.
    """
    if not PLAYWRIGHT_SCRAPER_URL:
        return False

    best_result: dict | None = None
    for attempt in range(1, APPLE_COOKIE_REFRESH_MAX_ATTEMPTS + 1):
        data = await _request_apple_cookie_refresh()
        if data is None:
            logger.warning(
                f"[apple][cookie-refresh] attempt {attempt}/{APPLE_COOKIE_REFRESH_MAX_ATTEMPTS} "
                f"produced no usable session"
            )
        else:
            best_result = data  # a usable floor, even if not confirmed
            if data.get("pincode_check_confirmed"):
                logger.info(
                    f"[apple][cookie-refresh] attempt {attempt}/{APPLE_COOKIE_REFRESH_MAX_ATTEMPTS} "
                    f"confirmed (pincode_check_confirmed=True) — using this session, no further attempts needed."
                )
                break
            logger.info(
                f"[apple][cookie-refresh] attempt {attempt}/{APPLE_COOKIE_REFRESH_MAX_ATTEMPTS} "
                f"NOT confirmed (pincode_check_confirmed=False)"
            )

        if attempt < APPLE_COOKIE_REFRESH_MAX_ATTEMPTS:
            delay = random.uniform(
                APPLE_COOKIE_REFRESH_RETRY_DELAY_MIN_SECONDS, APPLE_COOKIE_REFRESH_RETRY_DELAY_MAX_SECONDS
            )
            logger.info(f"[apple][cookie-refresh] retrying in {delay:.1f}s...")
            await asyncio.sleep(delay)

    if best_result is None:
        logger.warning(
            f"[apple][cookie-refresh] all {APPLE_COOKIE_REFRESH_MAX_ATTEMPTS} attempts failed outright — "
            f"DB session left unchanged."
        )
        return False

    set_apple_session_cookies(best_result["cookies"].strip(), best_result["user_agent"].strip())
    logger.info(
        f"[apple][cookie-refresh] stored a freshly-refreshed Apple session "
        f"(pincode_check_confirmed={best_result.get('pincode_check_confirmed')}, "
        f"diagnostics={best_result.get('diagnostics')})"
    )
    return True


async def apple_cookie_refresh_loop():
    """
    Runs every APPLE_COOKIE_REFRESH_INTERVAL seconds, independent of every
    other loop in this file. No-ops (logs once, then returns — never spins
    a pointless forever-loop) when PLAYWRIGHT_SCRAPER_URL isn't configured,
    so a deploy that hasn't set up the Playwright refresher behaves exactly
    as before (APPLE_COOKIES/APPLE_USER_AGENT env vars only).
    """
    if not PLAYWRIGHT_SCRAPER_URL:
        logger.info(
            "[apple][cookie-refresh] PLAYWRIGHT_SCRAPER_URL not set — "
            "auto-refresher disabled, using APPLE_COOKIES/APPLE_USER_AGENT "
            "env vars only."
        )
        return

    logger.info(
        f"[apple][cookie-refresh] loop started (interval="
        f"{APPLE_COOKIE_REFRESH_INTERVAL}s, target={PLAYWRIGHT_SCRAPER_URL})"
    )
    while True:
        try:
            await run_apple_cookie_refresh_cycle()
        except Exception as exc:
            logger.error(f"[apple][cookie-refresh] cycle error: {exc}")
        await asyncio.sleep(APPLE_COOKIE_REFRESH_INTERVAL)


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
        BotCommand(command="trackpickup", description="Track Apple Store pickup availability by pincode"),
        BotCommand(command="mypickups", description="Check your tracked pickup items right now"),
        BotCommand(command="untrackpickup", description="Stop tracking a pickup item"),
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
        BotCommand(command="setchannel",     description="[admin] Register the channel for forwarded stock alerts"),
        BotCommand(command="addchannel",     description="[admin] Forward a product's stock alerts to the channel"),
        BotCommand(command="stopforwarding", description="[admin] Stop forwarding a product to the channel"),
        BotCommand(command="setchannelpincode",    description="[admin] Set default pincode(s) for channel-forward checks"),
        BotCommand(command="addchannelpickup",     description="[admin] Forward Apple pickup alerts to the channel"),
        BotCommand(command="stopforwardingpickup", description="[admin] Stop forwarding a pickup item to the channel"),
        BotCommand(command="listforwarding", description="[admin] List everything forwarding to the channel"),
        BotCommand(command="forwardingtoggle", description="[admin] Turn channel forwarding on/off"),
        BotCommand(command="testforwarding",   description="[admin] Send a test message to the channel"),
        BotCommand(command="checkforwarding",  description="[admin] Check every forwarded item right now"),
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

    # Background tasks: stock checking (existing), access maintenance
    # (reminders + grace-period purge), the Apple cookie auto-refresher,
    # and Apple pickup checking (2026-07-28, decoupled from stock_checker_
    # loop — see apple_pickup_check_loop's own module note) all run as
    # independent concurrent loops on their own cadences (CHECK_INTERVAL
    # vs ACCESS_CHECK_INTERVAL vs APPLE_COOKIE_REFRESH_INTERVAL vs
    # APPLE_PICKUP_CHECK_INTERVAL).
    checker_task = asyncio.create_task(stock_checker_loop(bot))
    access_task = asyncio.create_task(access_maintenance_loop(bot))
    apple_cookie_task = asyncio.create_task(apple_cookie_refresh_loop())
    apple_pickup_task = asyncio.create_task(apple_pickup_check_loop(bot))

    logger.info("Bot is starting…")
    try:
        await dp.start_polling(bot, allowed_updates=["message", "callback_query"])
    finally:
        checker_task.cancel()
        access_task.cancel()
        apple_cookie_task.cancel()
        apple_pickup_task.cancel()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
