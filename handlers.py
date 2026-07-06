import asyncio
import logging
from datetime import datetime
from urllib.parse import urlparse, quote

from aiogram import Router, F
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
)

from states import AddProductStates, PinCodeStates, SearchStates, SelectStates
from database import (
    add_product,
    list_products,
    remove_product,
    search_products,
    add_pin_code,
    remove_pin_code,
    list_pin_codes,
    get_product_by_id_for_user,
    update_stock_status,
    get_user_primary_pincode,
    get_user,
    get_or_create_user,
    has_used_share_trial,
    request_share_trial,
    get_share_trial_rounds,
    increment_share_trial_round,
    reset_share_trial_rounds,
    get_user_lang,
    set_user_lang,
)
from access import check_can_add_item, compute_access, access_denied_text, REASON_ITEM_LIMIT
from notifications import send_stock_alert, should_alert_for_price
from stock_checker import detect_site, check_stock
from translations import t, LANG_LABEL
from config import (
    SUPPORTED_SITES,
    TRIAL_DAYS,
    ADMIN_USER_ID,
    SHARE_TRIAL_ROUNDS_REQUIRED,
    SHARE_TRIAL_TAP_DELAY_SECONDS,
    UNRELIABLE_SITES,
    COMING_SOON_DOMAINS,
)

logger = logging.getLogger(__name__)
router = Router()


def _format_last_checked(raw) -> str:
    """
    Render a stored 'YYYY-MM-DD HH:MM:SS' timestamp (IST) as a human-friendly
    12-hour string, e.g. '02 Jul 2026, 8:02 PM'. Returns 'Never' when unset.
    %-I/%I are avoided for portability; the 12-hour hour is computed manually.
    """
    if not raw:
        return "Never"
    text = str(raw)
    dt = None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            dt = datetime.strptime(text, fmt)
            break
        except ValueError:
            continue
    if dt is None:
        return text  # unknown format — show as-is rather than crash
    hour12 = dt.hour % 12 or 12
    ampm = "AM" if dt.hour < 12 else "PM"
    return f"{dt.strftime('%d %b %Y')}, {hour12}:{dt.minute:02d} {ampm}"

_SUPPORTED_SITES_TEXT = (
    "amazon.in · flipkart.com · zeptonow.com · bigbasket.com · "
    "blinkit.com · swiggy.com (Instamart) · myntra.com"
)

def _coming_soon_message(url: str, lang: str = "en") -> str | None:
    """
    Return a "coming soon" message (in `lang`) for a domain in
    config.COMING_SOON_DOMAINS (deliberately pulled, not simply unbuilt), or
    None otherwise — lets /add give an honest, specific reason instead of
    lumping it in with genuinely unsupported sites.
    """
    host = urlparse(url).netloc.lower().replace("www.", "")
    if any(host == d or host.endswith("." + d) for d in COMING_SOON_DOMAINS):
        return t("coming_soon_croma", lang)
    return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _auto_name(url: str, site: str) -> str:
    """Derive a short display name from a URL when bulk-adding."""
    try:
        path = urlparse(url).path.rstrip("/")
        slug = path.split("/")[-1][:40] if path else "product"
    except Exception:
        slug = "product"
    return f"{site.capitalize()}: {slug}"


def _parse_bulk_lines(text: str) -> list[tuple[str, str]]:
    """
    Parse lines of the form "product name | URL".
    Returns only lines that have a non-empty name and a valid http(s) URL.
    Returns an empty list if no valid entries found (caller falls back to
    the normal single-name flow when the user just types a name).
    """
    entries = []
    for line in text.splitlines():
        line = line.strip()
        if "|" not in line:
            continue
        name_part, _, url_part = line.partition("|")
        name_part = name_part.strip()
        url_part = url_part.strip()
        if name_part and url_part.startswith(("http://", "https://")):
            entries.append((name_part, url_part))
    return entries


async def _process_bulk(message: Message, entries: list[tuple[str, str]]) -> None:
    """Add a list of (name, url) pairs and send a summary reply."""
    user_id = message.from_user.id
    results = []
    for idx, (name, url) in enumerate(entries):
        site = detect_site(url)
        if site is None:
            coming_soon = _coming_soon_message(url)
            if coming_soon:
                results.append(f"🚧 Coming soon — <b>{name}</b>: <code>{url[:60]}</code>")
            else:
                results.append(f"❌ Unsupported site — <b>{name}</b>: <code>{url[:60]}</code>")
            continue
        # Re-checked per iteration (not once before the loop) so the item
        # count reflects items already added earlier in this same bulk batch —
        # otherwise a large paste could blow past the plan limit in one shot.
        allowed, reason, limit_msg = check_can_add_item(user_id, site)
        if not allowed:
            if reason == REASON_ITEM_LIMIT:
                # Every remaining item would fail identically — stop instead
                # of repeating the same paragraph once per leftover item.
                remaining = len(entries) - idx
                results.append(
                    f"🚫 Item limit reached — {remaining} remaining item(s) not added."
                )
                break
            results.append(f"⚠️ {limit_msg} — <b>{name}</b>")
            continue
        ok, msg = add_product(user_id, name, url, site)
        if ok:
            results.append(f"✅ <b>{name}</b> [{site.capitalize()}]")
        else:
            results.append(f"⚠️ {msg} — <b>{name}</b>")

    await message.answer(
        f"📦 <b>Bulk add results ({len(entries)} item{'s' if len(entries) != 1 else ''}):</b>\n\n"
        + "\n".join(results),
        parse_mode="HTML",
    )


async def _run_search(target: Message | CallbackQuery, user_id: int, keyword: str) -> None:
    """Execute a keyword search and reply with results."""
    send = target.answer if isinstance(target, Message) else target.message.answer
    products = search_products(user_id, keyword)

    if not products:
        await send(
            f"🔍 No products found matching <b>{keyword}</b>.\n"
            "Try a different keyword.",
            parse_mode="HTML",
        )
        return

    lines = [f"🔍 <b>Results for \"{keyword}\"</b> ({len(products)} found)\n"]
    for p in products:
        stock_emoji = "✅" if p["in_stock"] else "❌"
        checked = _format_last_checked(p["last_checked"])
        lines.append(
            f"{stock_emoji} <b>{p['name']}</b> [{p['site'].capitalize()}]\n"
            f"   🕒 Last checked: {checked}\n"
            f"   🔗 <a href=\"{p['url']}\">View product</a>\n"
        )

    await send(
        "\n".join(lines),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


async def _parallel_check(
    products: list[dict],
    bot,
    pincode: str | None = None,
    concurrency: int = 10,
) -> list[tuple[dict, bool]]:
    """Check multiple products concurrently, limited to `concurrency` at a time.

    Mirrors the background loop's (bot.py) was-in-stock vs now-in-stock
    comparison: a manual check that finds an out-of-stock item now in stock
    fires the same proactive send_stock_alert (respecting the Amazon price
    gate) in addition to the chat reply built from the returned results —
    so a genuine OOS -> in-stock transition is never missed just because a
    user happened to check it manually before the next automatic cycle.

    Default concurrency matches the Scrape.do plan's 10 concurrent-request limit.
    """
    sem = asyncio.Semaphore(concurrency)

    async def _one(p: dict) -> tuple[dict, bool]:
        async with sem:
            was_in_stock = bool(p["in_stock"])
            result, current_price = await check_stock(
                p["url"], p["site"], pincode=pincode, caller="manual"
            )
            update_stock_status(p["id"], result)
            if result and not was_in_stock:
                if should_alert_for_price(p, current_price):
                    await send_stock_alert(bot, p, price=current_price)
                else:
                    target_price = p.get("target_price")
                    logger.info(
                        f"[handlers] price gate: #{p['id']} in stock "
                        f"@ ₹{current_price:,.0f} > target ₹{target_price:,.0f} — skipping alert"
                    )
            return p, result

    return list(await asyncio.gather(*[_one(p) for p in products]))


def _unreliable_note(site: str) -> str:
    return " ⚠️ <i>unreliable — under investigation, don't trust this status</i>" if site in UNRELIABLE_SITES else ""


def _format_check_results(results: list[tuple[dict, bool]]) -> str:
    """Format parallel-check results into a readable summary."""
    total = len(results)
    in_stock = [(p, s) for p, s in results if s]
    oos = [(p, s) for p, s in results if not s]
    lines = [f"📊 <b>Check results ({total} item{'s' if total != 1 else ''}):</b>\n"]
    if in_stock:
        lines.append("✅ <b>In Stock:</b>")
        for p, _ in in_stock:
            lines.append(f"  • <b>{p['name']}</b> [{p['site'].capitalize()}]{_unreliable_note(p['site'])}")
            lines.append(f"    <a href=\"{p['url']}\">View →</a>")
    if oos:
        if in_stock:
            lines.append("")
        lines.append("❌ <b>Out of Stock:</b>")
        for p, _ in oos:
            lines.append(f"  • <b>{p['name']}</b> [{p['site'].capitalize()}]{_unreliable_note(p['site'])}")
    return "\n".join(lines)


def _pins_keyboard(pins: list[str]) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text=f"🗑 Remove {p}", callback_data=f"pin_remove:{p}")]
        for p in pins
    ]
    buttons.append(
        [InlineKeyboardButton(text="➕ Add pin code", callback_data="pin_add")]
    )
    buttons.append(
        [InlineKeyboardButton(text="❌ Close", callback_data="pin_close")]
    )
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _check_store_filter_keyboard(products: list[dict]) -> InlineKeyboardMarkup:
    """Keyboard shown as first step of /check — lets user pick a store to filter by."""
    stores = sorted({p["site"] for p in products})
    buttons = [
        [InlineKeyboardButton(
            text=f"🏪 {site.capitalize()}",
            callback_data=f"check_filter:{site}",
        )]
        for site in stores
    ]
    buttons.append(
        [InlineKeyboardButton(text="📦 All Stores", callback_data="check_filter:all")]
    )
    buttons.append(
        [InlineKeyboardButton(text="⚡ Check All Now", callback_data="check_all_now")]
    )
    buttons.append(
        [InlineKeyboardButton(text="🔍 Search", callback_data="search_prompt")]
    )
    buttons.append(
        [InlineKeyboardButton(text="❌ Cancel", callback_data="check_filter:cancel")]
    )
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _check_result_keyboard() -> InlineKeyboardMarkup:
    """Keyboard shown below a single-product check result."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔍 Search products", callback_data="search_prompt")],
    ])


def _select_keyboard(products: list[dict], selected_ids: set[int]) -> InlineKeyboardMarkup:
    """Checkbox keyboard for item selection mode."""
    buttons = []
    for p in products:
        mark = "✅" if p["id"] in selected_ids else "⬜"
        buttons.append([
            InlineKeyboardButton(
                text=f"{mark} {p['name']} [{p['site'].capitalize()}]",
                callback_data=f"sel_toggle:{p['id']}",
            )
        ])
    buttons.append([
        InlineKeyboardButton(text="🔍 Check All", callback_data="sel_check_all"),
        InlineKeyboardButton(text="✅ Check Selected", callback_data="sel_check_selected"),
    ])
    buttons.append([
        InlineKeyboardButton(text="🗑 Delete Selected", callback_data="sel_delete_selected"),
        InlineKeyboardButton(text="🗑 Delete All", callback_data="sel_delete_all"),
    ])
    buttons.append([
        InlineKeyboardButton(text="❌ Cancel", callback_data="sel_cancel"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ---------------------------------------------------------------------------
# /language – pick English / हिंदी / Hinglish
# ---------------------------------------------------------------------------

def _language_keyboard(*, first_run: bool) -> InlineKeyboardMarkup:
    # first_run=True chains into showing the welcome after the pick.
    suffix = ":firstrun" if first_run else ""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="English", callback_data=f"setlang:en{suffix}")],
        [InlineKeyboardButton(text="हिंदी", callback_data=f"setlang:hi{suffix}")],
        [InlineKeyboardButton(text="Hinglish", callback_data=f"setlang:hinglish{suffix}")],
    ])


@router.message(Command("language"))
async def cmd_language(message: Message):
    lang = get_user_lang(message.from_user.id)
    await message.answer(
        t("language_prompt", lang), parse_mode="HTML",
        reply_markup=_language_keyboard(first_run=False),
    )


@router.callback_query(F.data.startswith("setlang:"))
async def callback_setlang(call: CallbackQuery):
    payload = call.data.split(":", 1)[1]
    first_run = payload.endswith(":firstrun")
    lang = payload.replace(":firstrun", "")
    if lang not in ("en", "hi", "hinglish"):
        await call.answer()
        return
    # ensure a row exists (new users hitting the first-run prompt), then set
    get_or_create_user(
        call.from_user.id,
        username=call.from_user.username,
        first_name=call.from_user.first_name,
    )
    set_user_lang(call.from_user.id, lang)
    await call.message.edit_text(t("language_set", lang), parse_mode="HTML")
    await call.answer()
    if first_run:
        await _send_welcome(call.message, call.from_user.id, lang)


# ---------------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------------

async def _send_welcome(target: Message, user_id: int, lang: str) -> None:
    """Render the welcome (status line + body + commands) in the given lang.
    `target` is the Message to reply from (a fresh /start message, or the
    edited language-picker message on first run)."""
    if user_id != ADMIN_USER_ID:
        user_row = get_or_create_user(user_id)
        info = compute_access(user_row)
        if not info.has_access:
            await target.answer(access_denied_text(info), parse_mode="HTML")
            return
        if info.status == "trial":
            days_left = max(0, round(info.days_remaining or 0, 1))
            trial_line = t("status_trial", lang, days=days_left, trial_days=TRIAL_DAYS)
        else:
            plan_name = info.plan["name"] if info.plan else "your plan"
            days_left = max(0, round(info.days_remaining or 0, 1))
            trial_line = t("status_plan", lang, plan=plan_name, days=days_left)
    else:
        trial_line = ""

    await target.answer(
        t("welcome_body", lang, trial_line=trial_line) + t("welcome_commands", lang),
        parse_mode="HTML",
    )


@router.message(CommandStart())
async def cmd_start(message: Message):
    user_id = message.from_user.id

    # First-run auto-prompt: a brand-new non-admin user (no row yet — /start
    # bypasses the middleware that would otherwise create one) is asked to pick
    # a language before anything else; the picker chains into the welcome.
    if user_id != ADMIN_USER_ID and get_user(user_id) is None:
        get_or_create_user(
            user_id,
            username=message.from_user.username,
            first_name=message.from_user.first_name,
        )
        await message.answer(
            t("language_welcome_prompt", "en"), parse_mode="HTML",
            reply_markup=_language_keyboard(first_run=True),
        )
        return

    # Keep the stored Telegram profile fresh, then show the localized welcome.
    get_or_create_user(
        user_id, username=message.from_user.username, first_name=message.from_user.first_name,
    )
    await _send_welcome(message, user_id, get_user_lang(user_id))


# ---------------------------------------------------------------------------
# /freetrial – WhatsApp-share-gated one-time trial bonus
# ---------------------------------------------------------------------------

def _freetrial_round_text(rounds_done: int, *, waiting: bool, lang: str) -> str:
    round_num = min(rounds_done + 1, SHARE_TRIAL_ROUNDS_REQUIRED)
    header = t("ft_header", lang, n=round_num, total=SHARE_TRIAL_ROUNDS_REQUIRED)
    if rounds_done == 0:
        progress_line = t("ft_progress_first", lang, total=SHARE_TRIAL_ROUNDS_REQUIRED)
    else:
        progress_line = t("ft_progress_more", lang, done=rounds_done, total=SHARE_TRIAL_ROUNDS_REQUIRED)
    if waiting:
        status_line = t("ft_waiting", lang, secs=SHARE_TRIAL_TAP_DELAY_SECONDS)
    else:
        status_line = t("ft_ready", lang)
    return header + progress_line + status_line


async def _freetrial_bot_link(bot) -> str:
    me = await bot.get_me()
    return f"https://t.me/{me.username}"


def _freetrial_wa_url(bot_link: str, lang: str) -> str:
    return f"https://wa.me/?text={quote(t('ft_wa_share_text', lang, link=bot_link))}"


def _freetrial_share_only_keyboard(bot_link: str, lang: str) -> InlineKeyboardMarkup:
    """Round's initial keyboard: Share button only. "Done" isn't there yet —
    see _reveal_done_button. Telegram gives the bot no event when a `url`
    button is tapped, so this delay is the closest available approximation
    to "must tap Share before Done," not a real verification of it."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📤 Share on WhatsApp", url=_freetrial_wa_url(bot_link, lang))],
    ])


def _freetrial_full_keyboard(bot_link: str, lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📤 Share on WhatsApp", url=_freetrial_wa_url(bot_link, lang))],
        [InlineKeyboardButton(text="✅ Done", callback_data="freetrial:done")],
    ])


async def _reveal_done_button(bot, message: Message, user_id: int, expected_rounds_done: int) -> None:
    """
    Waits SHARE_TRIAL_TAP_DELAY_SECONDS, then switches this round's message
    from the "please wait" text to the "tap Done" text and reveals the Done
    button — unless the user has since moved on (retried, or somehow already
    claimed) before the delay elapsed, in which case that newer state's own
    reveal task owns showing Done and this one no-ops.
    """
    await asyncio.sleep(SHARE_TRIAL_TAP_DELAY_SECONDS)
    if has_used_share_trial(user_id):
        return
    if get_share_trial_rounds(user_id) != expected_rounds_done:
        return
    lang = get_user_lang(user_id)
    bot_link = await _freetrial_bot_link(bot)
    try:
        await message.edit_text(
            _freetrial_round_text(expected_rounds_done, waiting=False, lang=lang),
            parse_mode="HTML",
            reply_markup=_freetrial_full_keyboard(bot_link, lang),
        )
    except Exception:
        pass  # message may have been edited/deleted already


async def _show_round(target: Message, bot, user_id: int, rounds_done: int, *, is_new_message: bool) -> None:
    """Show a round's share screen (waiting text + Share-only keyboard) and
    schedule the delayed reveal of the "Done" text/button. `target` is the
    Message to send a new reply from (cmd_freetrial) or edit in place
    (callback handlers)."""
    lang = get_user_lang(user_id)
    bot_link = await _freetrial_bot_link(bot)
    text = _freetrial_round_text(rounds_done, waiting=True, lang=lang)
    keyboard = _freetrial_share_only_keyboard(bot_link, lang)
    if is_new_message:
        sent = await target.answer(
            text, parse_mode="HTML", reply_markup=keyboard, disable_web_page_preview=True
        )
    else:
        sent = await target.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    asyncio.create_task(_reveal_done_button(bot, sent, user_id, rounds_done))


_FREETRIAL_CONFIRM_KEYBOARD = InlineKeyboardMarkup(inline_keyboard=[
    [
        InlineKeyboardButton(text="✅ Yes, I confirm", callback_data="freetrial:confirm"),
        InlineKeyboardButton(text="🔄 Retry", callback_data="freetrial:retry"),
    ]
])


def _ft_confirm_text(lang: str) -> str:
    return t("ft_confirm", lang, total=SHARE_TRIAL_ROUNDS_REQUIRED)


@router.message(Command("freetrial"))
async def cmd_freetrial(message: Message):
    user_id = message.from_user.id

    if user_id == ADMIN_USER_ID:
        await message.answer(t("ft_admin_no_need", get_user_lang(user_id)))
        return

    get_or_create_user(
        user_id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
    )
    lang = get_user_lang(user_id)

    if has_used_share_trial(user_id):
        await message.answer(t("ft_already_used", lang), parse_mode="HTML")
        return

    # Resume wherever this user left off (rounds persist in the DB across
    # restarts/breaks) rather than restarting the cycle on every /freetrial call.
    rounds_done = get_share_trial_rounds(user_id)
    if rounds_done >= SHARE_TRIAL_ROUNDS_REQUIRED:
        await message.answer(
            _ft_confirm_text(lang), parse_mode="HTML", reply_markup=_FREETRIAL_CONFIRM_KEYBOARD
        )
        return

    await _show_round(message, message.bot, user_id, rounds_done, is_new_message=True)


@router.callback_query(F.data == "freetrial:done")
async def callback_freetrial_done(call: CallbackQuery):
    user_id = call.from_user.id
    lang = get_user_lang(user_id)
    if has_used_share_trial(user_id):
        await call.message.edit_text(t("ft_already_used", lang), parse_mode="HTML")
        await call.answer()
        return

    rounds_done = increment_share_trial_round(user_id)
    if rounds_done >= SHARE_TRIAL_ROUNDS_REQUIRED:
        await call.message.edit_text(
            _ft_confirm_text(lang), parse_mode="HTML", reply_markup=_FREETRIAL_CONFIRM_KEYBOARD
        )
    else:
        await _show_round(call.message, call.bot, user_id, rounds_done, is_new_message=False)
    await call.answer()


@router.callback_query(F.data == "freetrial:retry")
async def callback_freetrial_retry(call: CallbackQuery):
    user_id = call.from_user.id
    if has_used_share_trial(user_id):
        await call.message.edit_text(t("ft_already_used", get_user_lang(user_id)), parse_mode="HTML")
        await call.answer()
        return

    reset_share_trial_rounds(user_id)
    await _show_round(call.message, call.bot, user_id, 0, is_new_message=False)
    await call.answer()


@router.callback_query(F.data == "freetrial:confirm")
async def callback_freetrial_confirm(call: CallbackQuery):
    user_id = call.from_user.id
    lang = get_user_lang(user_id)
    requested, _updated = request_share_trial(user_id)

    if not requested:
        if has_used_share_trial(user_id):
            await call.message.edit_text(t("ft_already_used", lang), parse_mode="HTML")
        else:
            # Rounds incomplete (e.g. a stale button tapped after a Retry
            # reset the counter elsewhere) — send them back to the real
            # current round instead of silently failing.
            rounds_done = get_share_trial_rounds(user_id)
            await _show_round(call.message, call.bot, user_id, rounds_done, is_new_message=False)
        await call.answer()
        return

    # No longer auto-activates: this creates a pending request the admin
    # must approve via /approve or the dashboard (see request_share_trial).
    await call.message.edit_text(t("ft_request_pending", lang), parse_mode="HTML")
    await call.answer()


# ---------------------------------------------------------------------------
# /add  – FSM: name → link(s) → [target price for Amazon] → save
# ---------------------------------------------------------------------------

@router.message(Command("add"))
async def cmd_add(message: Message, state: FSMContext, command: CommandObject):
    # Inline bulk: /add followed by "Name | URL" lines in the same message
    if command.args:
        entries = _parse_bulk_lines(command.args)
        if entries:
            await state.clear()
            await _process_bulk(message, entries)
            return

    await state.set_state(AddProductStates.waiting_for_name)
    await message.answer(t("add_instructions", get_user_lang(message.from_user.id)), parse_mode="HTML")


@router.message(Command("cancel"), AddProductStates.waiting_for_name)
@router.message(Command("cancel"), AddProductStates.waiting_for_link)
@router.message(Command("cancel"), AddProductStates.waiting_for_target_price)
@router.message(Command("cancel"), SearchStates.waiting_for_keyword)
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(t("cancelled", get_user_lang(message.from_user.id)))


@router.message(AddProductStates.waiting_for_name)
async def receive_name(message: Message, state: FSMContext):
    lang = get_user_lang(message.from_user.id)
    raw = message.text.strip()
    if not raw:
        await message.answer(t("add_empty_input", lang))
        return

    # ── Bulk format: lines of "name | URL" ──────────────────────────────────
    bulk_entries = _parse_bulk_lines(raw)
    if bulk_entries:
        await state.clear()
        await _process_bulk(message, bulk_entries)
        return

    # ── Single flow: treat input as the product name ─────────────────────────
    await state.update_data(product_name=raw)
    await state.set_state(AddProductStates.waiting_for_link)
    await message.answer(
        t("add_name_saved", lang, name=raw, sites=_SUPPORTED_SITES_TEXT), parse_mode="HTML")


@router.message(AddProductStates.waiting_for_link)
async def receive_link(message: Message, state: FSMContext):
    raw = message.text.strip()
    data = await state.get_data()
    name = data["product_name"]
    user_id = message.from_user.id
    lang = get_user_lang(user_id)

    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    urls = [ln for ln in lines if ln.startswith(("http://", "https://"))]

    # ── Multi-URL path ──────────────────────────────────────────────────────
    if len(urls) > 1:
        await state.clear()
        results = []
        for idx, url in enumerate(urls):
            site = detect_site(url)
            if site is None:
                coming_soon = _coming_soon_message(url)
                if coming_soon:
                    results.append(f"🚧 Coming soon: <code>{url[:60]}</code>")
                else:
                    results.append(f"❌ Unsupported site: <code>{url[:60]}</code>")
                continue
            allowed, reason, limit_msg = check_can_add_item(user_id, site)
            if not allowed:
                if reason == REASON_ITEM_LIMIT:
                    remaining = len(urls) - idx
                    results.append(f"🚫 Item limit reached — {remaining} remaining URL(s) not added.")
                    break
                results.append(f"⚠️ {limit_msg}: <code>{url[:60]}</code>")
                continue
            auto_name = _auto_name(url, site)
            ok, msg = add_product(user_id, auto_name, url, site)
            if ok:
                results.append(f"✅ <b>{auto_name}</b> [{site.capitalize()}]")
            else:
                results.append(f"⚠️ {msg}: <code>{url[:60]}</code>")

        summary = "\n".join(results)
        await message.answer(
            f"📦 <b>Bulk add results ({len(urls)} URLs):</b>\n\n{summary}",
            parse_mode="HTML",
        )
        return

    # ── Single-URL path (original flow) ─────────────────────────────────────
    url = lines[0] if lines else raw
    if not url.startswith(("http://", "https://")):
        await message.answer(t("add_invalid_url", lang))
        return

    site = detect_site(url)
    if site is None:
        coming_soon = _coming_soon_message(url, lang)
        if coming_soon:
            await message.answer(coming_soon, parse_mode="HTML")
        else:
            await message.answer(
                t("add_unsupported", lang, sites=_SUPPORTED_SITES_TEXT), parse_mode="HTML")
        return

    # Checked here (before the Amazon target-price sub-flow) so a user who's
    # already at their limit isn't walked through an extra step for nothing.
    allowed, _reason, limit_msg = check_can_add_item(user_id, site)
    if not allowed:
        await state.clear()
        await message.answer(limit_msg, parse_mode="HTML")
        return

    # ── Amazon: ask for optional target price before saving ─────────────────
    if site == "amazon":
        await state.update_data(product_url=url, product_site=site)
        await state.set_state(AddProductStates.waiting_for_target_price)
        await message.answer(t("amazon_target_prompt", lang, name=name), parse_mode="HTML")
        return

    ok, msg = add_product(user_id, name, url, site)
    await state.clear()

    if ok:
        await message.answer(
            t("product_added", lang, name=name, site=site.capitalize(), url=url), parse_mode="HTML")
    else:
        await message.answer(f"⚠️ {msg}")


@router.message(AddProductStates.waiting_for_target_price)
async def receive_target_price(message: Message, state: FSMContext):
    raw = message.text.strip()
    data = await state.get_data()
    name = data["product_name"]
    url = data["product_url"]
    site = data["product_site"]
    user_id = message.from_user.id
    lang = get_user_lang(user_id)

    target_price: float | None = None
    if raw.lower() not in ("/skip", "skip"):
        cleaned = raw.lstrip("₹$").replace(",", "").strip()
        try:
            target_price = float(cleaned)
            if target_price <= 0:
                raise ValueError("price must be positive")
        except (ValueError, TypeError):
            await message.answer(t("target_invalid", lang), parse_mode="HTML")
            return

    # Re-checked here too (not just when the URL was first submitted) in case
    # the user's plan/limit changed during the time spent typing a target price.
    allowed, _reason, limit_msg = check_can_add_item(user_id, site)
    if not allowed:
        await state.clear()
        await message.answer(limit_msg, parse_mode="HTML")
        return

    ok, msg = add_product(user_id, name, url, site, target_price=target_price)
    await state.clear()

    if ok:
        body = t("product_added", lang, name=name, site=site.capitalize(), url=url)
        if target_price:
            body += f"\n💰 ₹{target_price:,.0f}"
        await message.answer(body, parse_mode="HTML")
    else:
        await message.answer(f"⚠️ {msg}")


# ---------------------------------------------------------------------------
# /list
# ---------------------------------------------------------------------------

@router.message(Command("list"))
async def cmd_list(message: Message):
    user_id = message.from_user.id
    lang = get_user_lang(user_id)
    products = list_products(user_id)

    if not products:
        await message.answer(t("list_empty", lang))
        return

    lines = [t("list_header", lang)]
    for p in products:
        stock_emoji = "✅" if p["in_stock"] else "❌"
        checked = _format_last_checked(p["last_checked"])
        target = p.get("target_price")
        price_line = f"\n   💰 Target price: ₹{target:,.0f}" if target is not None else ""
        lines.append(
            f"{stock_emoji} <b>{p['name']}</b> [{p['site'].capitalize()}]\n"
            f"   🆔 ID: <code>{p['id']}</code>\n"
            f"   🕒 Last checked: {checked}{price_line}\n"
            f"   🔗 <a href=\"{p['url']}\">View product</a>\n"
        )

    await message.answer(
        "\n".join(lines),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


# ---------------------------------------------------------------------------
# /remove
# ---------------------------------------------------------------------------

@router.message(Command("remove"))
async def cmd_remove(message: Message):
    user_id = message.from_user.id
    lang = get_user_lang(user_id)
    products = list_products(user_id)

    if not products:
        await message.answer(t("remove_empty", lang))
        return

    buttons = [
        [
            InlineKeyboardButton(
                text=f"🗑 {p['name']} [{p['site'].capitalize()}]",
                callback_data=f"remove:{p['id']}",
            )
        ]
        for p in products
    ]
    buttons.append(
        [InlineKeyboardButton(text="❌ Cancel", callback_data="remove:cancel")]
    )

    await message.answer(
        t("remove_prompt", lang),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@router.callback_query(F.data.startswith("remove:"))
async def callback_remove(call: CallbackQuery):
    payload = call.data.split(":", 1)[1]

    if payload == "cancel":
        await call.message.edit_text("❌ Removal cancelled.")
        await call.answer()
        return

    try:
        product_id = int(payload)
    except ValueError:
        await call.answer("Invalid selection.", show_alert=True)
        return

    deleted = remove_product(call.from_user.id, product_id)
    if deleted:
        await call.message.edit_text(
            f"✅ Product <b>#{product_id}</b> has been removed.",
            parse_mode="HTML",
        )
    else:
        await call.message.edit_text(
            "⚠️ Could not remove that product. It may have already been deleted."
        )
    await call.answer()


# ---------------------------------------------------------------------------
# /check  – step 1: store filter  →  step 2: product list  →  step 3: result
# ---------------------------------------------------------------------------

@router.message(Command("check"))
async def cmd_check(message: Message):
    user_id = message.from_user.id
    lang = get_user_lang(user_id)
    products = list_products(user_id)

    if not products:
        await message.answer(t("check_empty", lang))
        return

    await message.answer(
        t("check_filter_prompt", lang),
        parse_mode="HTML",
        reply_markup=_check_store_filter_keyboard(products),
    )


@router.callback_query(F.data == "check_all_now")
async def callback_check_all_now(call: CallbackQuery):
    """Check every tracked product in parallel."""
    user_id = call.from_user.id
    products = list_products(user_id)
    if not products:
        await call.answer("No products to check!", show_alert=True)
        return

    await call.message.edit_text(
        f"⏳ Checking all <b>{len(products)}</b> product(s) in parallel…\n"
        "This may take a moment.",
        parse_mode="HTML",
    )
    await call.answer()

    results = await _parallel_check(products, call.bot, pincode=get_user_primary_pincode(user_id))
    await call.message.edit_text(
        _format_check_results(results),
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=_check_result_keyboard(),
    )


@router.callback_query(F.data.startswith("check_filter:"))
async def callback_check_filter(call: CallbackQuery):
    payload = call.data.split(":", 1)[1]

    if payload == "cancel":
        await call.message.edit_text("❌ Check cancelled.")
        await call.answer()
        return

    user_id = call.from_user.id
    products = list_products(user_id)

    if payload != "all":
        products = [p for p in products if p["site"] == payload]

    if not products:
        await call.message.edit_text(
            f"📭 No products tracked for <b>{payload.capitalize()}</b>.",
            parse_mode="HTML",
        )
        await call.answer()
        return

    store_label = payload.capitalize() if payload != "all" else "All Stores"
    buttons = [
        [
            InlineKeyboardButton(
                text=f"🔍 {p['name']} [{p['site'].capitalize()}]",
                callback_data=f"check:{p['id']}",
            )
        ]
        for p in products
    ]
    buttons.append(
        [InlineKeyboardButton(text="🔍 Search", callback_data="search_prompt")]
    )
    buttons.append(
        [InlineKeyboardButton(text="❌ Cancel", callback_data="check:cancel")]
    )

    await call.message.edit_text(
        f"🔍 <b>Select a product to check</b> [{store_label}]:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )
    await call.answer()


@router.callback_query(F.data.startswith("check:"))
async def callback_check(call: CallbackQuery):
    payload = call.data.split(":", 1)[1]

    if payload == "cancel":
        await call.message.edit_text("❌ Check cancelled.")
        await call.answer()
        return

    try:
        product_id = int(payload)
    except ValueError:
        await call.answer("Invalid selection.", show_alert=True)
        return

    product = get_product_by_id_for_user(product_id, call.from_user.id)
    if not product:
        await call.message.edit_text("⚠️ Product not found.")
        await call.answer()
        return

    await call.message.edit_text(
        f"⏳ Checking stock for <b>{product['name']}</b>…",
        parse_mode="HTML",
    )
    await call.answer()

    pincode = get_user_primary_pincode(call.from_user.id)
    was_in_stock = bool(product["in_stock"])
    in_stock, current_price = await check_stock(
        product["url"], product["site"], pincode=pincode, caller="manual"
    )
    update_stock_status(product_id, in_stock)

    # Mirror the background loop's transition check (bot.py): a manual check
    # that discovers an OOS -> in-stock flip fires the same proactive alert,
    # in addition to the chat reply below, so it's never silently missed.
    if in_stock and not was_in_stock:
        if should_alert_for_price(product, current_price):
            await send_stock_alert(call.bot, product, price=current_price)
        else:
            target_price = product.get("target_price")
            logger.info(
                f"[handlers] price gate: #{product['id']} in stock "
                f"@ ₹{current_price:,.0f} > target ₹{target_price:,.0f} — skipping alert"
            )

    status_emoji = "✅" if in_stock else "❌"
    status_text = "IN STOCK" if in_stock else "OUT OF STOCK"
    price_line = f"\n💰 Current price: ₹{current_price:,.0f}" if current_price is not None else ""
    warning_line = (
        f"\n⚠️ <i>{product['site'].capitalize()} results are currently unreliable "
        f"(under investigation) — don't trust this status.</i>"
        if product["site"] in UNRELIABLE_SITES else ""
    )
    await call.message.edit_text(
        f"{status_emoji} <b>{product['name']}</b>\n\n"
        f"Status: <b>{status_text}</b>{price_line}\n"
        f"Site: {product['site'].capitalize()}\n"
        f"🔗 <a href=\"{product['url']}\">View product</a>{warning_line}",
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=_check_result_keyboard(),
    )


# ---------------------------------------------------------------------------
# /select  – checkbox selection for bulk check / delete
# ---------------------------------------------------------------------------

@router.message(Command("select"))
async def cmd_select(message: Message, state: FSMContext):
    user_id = message.from_user.id
    products = list_products(user_id)
    if not products:
        await message.answer(t("check_empty", get_user_lang(user_id)))
        return
    await state.set_state(SelectStates.selecting)
    await state.update_data(selected_ids=[])
    await message.answer(
        "☑️ <b>Select items</b>\n\n"
        "Tap to toggle ✅/⬜, then choose an action:",
        parse_mode="HTML",
        reply_markup=_select_keyboard(products, set()),
    )


@router.callback_query(F.data.startswith("sel_toggle:"), SelectStates.selecting)
async def callback_sel_toggle(call: CallbackQuery, state: FSMContext):
    product_id = int(call.data.split(":", 1)[1])
    data = await state.get_data()
    selected = set(data.get("selected_ids", []))
    if product_id in selected:
        selected.discard(product_id)
    else:
        selected.add(product_id)
    await state.update_data(selected_ids=list(selected))
    products = list_products(call.from_user.id)
    await call.message.edit_reply_markup(
        reply_markup=_select_keyboard(products, selected)
    )
    await call.answer()


@router.callback_query(F.data == "sel_check_all", SelectStates.selecting)
async def callback_sel_check_all(call: CallbackQuery, state: FSMContext):
    user_id = call.from_user.id
    products = list_products(user_id)
    if not products:
        await call.answer("No products to check!", show_alert=True)
        return
    await call.message.edit_text(
        f"⏳ Checking all <b>{len(products)}</b> product(s) in parallel…\n"
        "This may take a moment.",
        parse_mode="HTML",
    )
    await call.answer()
    results = await _parallel_check(products, call.bot, pincode=get_user_primary_pincode(user_id))
    await call.message.edit_text(
        _format_check_results(results),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
    await state.clear()


@router.callback_query(F.data == "sel_check_selected", SelectStates.selecting)
async def callback_sel_check_selected(call: CallbackQuery, state: FSMContext):
    user_id = call.from_user.id
    data = await state.get_data()
    selected = set(data.get("selected_ids", []))
    if not selected:
        await call.answer("No items selected! Tap ⬜ to select items first.", show_alert=True)
        return
    products = [p for p in list_products(user_id) if p["id"] in selected]
    if not products:
        await call.answer("Selected products not found.", show_alert=True)
        return
    await call.message.edit_text(
        f"⏳ Checking <b>{len(products)}</b> selected product(s) in parallel…\n"
        "This may take a moment.",
        parse_mode="HTML",
    )
    await call.answer()
    results = await _parallel_check(products, call.bot, pincode=get_user_primary_pincode(user_id))
    await call.message.edit_text(
        _format_check_results(results),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
    await state.clear()


@router.callback_query(F.data == "sel_delete_selected", SelectStates.selecting)
async def callback_sel_delete_selected(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    selected = set(data.get("selected_ids", []))
    if not selected:
        await call.answer("No items selected! Tap ⬜ to select items first.", show_alert=True)
        return
    await call.message.edit_text(
        f"⚠️ <b>Delete {len(selected)} selected item(s)?</b>\n\nThis cannot be undone.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Yes, delete", callback_data="sel_confirm_delete:selected"),
                InlineKeyboardButton(text="↩️ Go back", callback_data="sel_back"),
            ]
        ]),
    )
    await call.answer()


@router.callback_query(F.data == "sel_delete_all", SelectStates.selecting)
async def callback_sel_delete_all(call: CallbackQuery, state: FSMContext):
    products = list_products(call.from_user.id)
    count = len(products)
    if not count:
        await call.answer("No products to delete.", show_alert=True)
        return
    await call.message.edit_text(
        f"⚠️ <b>Delete all {count} tracked product(s)?</b>\n\nThis cannot be undone.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Yes, delete all", callback_data="sel_confirm_delete:all"),
                InlineKeyboardButton(text="↩️ Go back", callback_data="sel_back"),
            ]
        ]),
    )
    await call.answer()


@router.callback_query(F.data.startswith("sel_confirm_delete:"), SelectStates.selecting)
async def callback_sel_confirm_delete(call: CallbackQuery, state: FSMContext):
    delete_type = call.data.split(":", 1)[1]
    user_id = call.from_user.id
    data = await state.get_data()

    if delete_type == "selected":
        selected = set(data.get("selected_ids", []))
        deleted = sum(1 for pid in selected if remove_product(user_id, pid))
        await call.message.edit_text(
            f"✅ Deleted <b>{deleted}</b> product(s).",
            parse_mode="HTML",
        )
    else:
        products = list_products(user_id)
        deleted = sum(1 for p in products if remove_product(user_id, p["id"]))
        await call.message.edit_text(
            f"✅ All <b>{deleted}</b> product(s) deleted.",
            parse_mode="HTML",
        )

    await state.clear()
    await call.answer()


@router.callback_query(F.data == "sel_back", SelectStates.selecting)
async def callback_sel_back(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    selected = set(data.get("selected_ids", []))
    products = list_products(call.from_user.id)
    if not products:
        await call.message.edit_text("📭 No products left to manage.")
        await state.clear()
        await call.answer()
        return
    await call.message.edit_text(
        "☑️ <b>Select items</b>\n\n"
        "Tap to toggle ✅/⬜, then choose an action:",
        parse_mode="HTML",
        reply_markup=_select_keyboard(products, selected),
    )
    await call.answer()


@router.callback_query(F.data == "sel_cancel", SelectStates.selecting)
async def callback_sel_cancel(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text("❌ Selection cancelled.")
    await call.answer()


# ---------------------------------------------------------------------------
# /search  – keyword search across user's tracked products
# ---------------------------------------------------------------------------

@router.message(Command("search"))
async def cmd_search(message: Message, state: FSMContext, command: CommandObject):
    if command.args:
        await state.clear()
        await _run_search(message, message.from_user.id, command.args.strip())
        return

    await state.set_state(SearchStates.waiting_for_keyword)
    await message.answer(
        "🔍 <b>Search your tracked products</b>\n\n"
        "Send me a keyword to search by product name:\n\n"
        "Type /cancel to abort.",
        parse_mode="HTML",
    )


@router.message(SearchStates.waiting_for_keyword)
async def receive_search_keyword(message: Message, state: FSMContext):
    keyword = message.text.strip()
    if not keyword:
        await message.answer("Keyword cannot be empty. Please try again.")
        return
    await state.clear()
    await _run_search(message, message.from_user.id, keyword)


@router.callback_query(F.data == "search_prompt")
async def callback_search_prompt(call: CallbackQuery, state: FSMContext):
    await state.set_state(SearchStates.waiting_for_keyword)
    await call.message.answer(
        "🔍 <b>Search your tracked products</b>\n\n"
        "Send me a keyword to search by product name:\n\n"
        "Type /cancel to abort.",
        parse_mode="HTML",
    )
    await call.answer()


# ---------------------------------------------------------------------------
# /stores  – list all supported stores from config
# ---------------------------------------------------------------------------

@router.message(Command("stores"))
async def cmd_stores(message: Message):
    lines = [t("stores_intro", get_user_lang(message.from_user.id))]
    for site, domains in SUPPORTED_SITES.items():
        domain_str = ", ".join(domains)
        lines.append(f"• <b>{site.capitalize()}</b> — {domain_str}")

    await message.answer(
        "\n".join(lines),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


# ---------------------------------------------------------------------------
# /pins  – manage delivery pin codes
# ---------------------------------------------------------------------------

@router.message(Command("pins"))
async def cmd_pins(message: Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    pins = list_pin_codes(user_id)

    if pins:
        pin_list = " · ".join(f"<code>{p}</code>" for p in pins)
        header = f"📍 <b>Your pin codes:</b> {pin_list}\n\n"
    else:
        header = "📍 <b>You have no pin codes saved yet.</b>\n\n"

    await message.answer(
        header + "Use the buttons below to add or remove pin codes.",
        parse_mode="HTML",
        reply_markup=_pins_keyboard(pins),
    )


@router.callback_query(F.data == "pin_add")
async def callback_pin_add(call: CallbackQuery, state: FSMContext):
    await state.set_state(PinCodeStates.waiting_for_pin)
    await call.message.edit_text(
        "📮 Send me a <b>6-digit pin code</b> to add:",
        parse_mode="HTML",
    )
    await call.answer()


@router.message(PinCodeStates.waiting_for_pin)
async def receive_pin_code(message: Message, state: FSMContext):
    pin = message.text.strip()
    if not pin.isdigit() or len(pin) != 6:
        await message.answer("⚠️ A pin code must be exactly 6 digits. Please try again.")
        return

    user_id = message.from_user.id
    ok, msg = add_pin_code(user_id, pin)
    await state.clear()

    if ok:
        pins = list_pin_codes(user_id)
        pin_list = " · ".join(f"<code>{p}</code>" for p in pins)
        await message.answer(
            f"✅ Pin code <code>{pin}</code> added!\n\n"
            f"📍 <b>Your pin codes:</b> {pin_list}",
            parse_mode="HTML",
            reply_markup=_pins_keyboard(pins),
        )
    else:
        await message.answer(f"⚠️ {msg}")


@router.callback_query(F.data.startswith("pin_remove:"))
async def callback_pin_remove(call: CallbackQuery):
    pin = call.data.split(":", 1)[1]
    removed = remove_pin_code(call.from_user.id, pin)

    if removed:
        pins = list_pin_codes(call.from_user.id)
        if pins:
            pin_list = " · ".join(f"<code>{p}</code>" for p in pins)
            header = f"📍 <b>Your pin codes:</b> {pin_list}\n\n"
        else:
            header = "📍 <b>You have no pin codes saved.</b>\n\n"
        await call.message.edit_text(
            f"✅ Pin code <code>{pin}</code> removed.\n\n"
            + header
            + "Use the buttons below to manage your pin codes.",
            parse_mode="HTML",
            reply_markup=_pins_keyboard(pins),
        )
    else:
        await call.answer("Could not remove pin code.", show_alert=True)

    await call.answer()


@router.callback_query(F.data == "pin_close")
async def callback_pin_close(call: CallbackQuery):
    await call.message.edit_text("📍 Pin code manager closed.")
    await call.answer()
