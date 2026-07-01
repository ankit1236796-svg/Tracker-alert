import asyncio
import logging
from urllib.parse import urlparse

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
)
from stock_checker import detect_site, check_stock
from config import SUPPORTED_SITES

logger = logging.getLogger(__name__)
router = Router()

_SUPPORTED_SITES_TEXT = (
    "amazon.in · flipkart.com · zeptonow.com · bigbasket.com · "
    "blinkit.com · croma.com · swiggy.com (Instamart) · myntra.com"
)


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
    for name, url in entries:
        site = detect_site(url)
        if site is None:
            results.append(f"❌ Unsupported site — <b>{name}</b>: <code>{url[:60]}</code>")
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
        checked = p["last_checked"] or "Never"
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


async def _parallel_check(products: list[dict], concurrency: int = 3) -> list[tuple[dict, bool]]:
    """Check multiple products concurrently, limited to `concurrency` at a time."""
    sem = asyncio.Semaphore(concurrency)

    async def _one(p: dict) -> tuple[dict, bool]:
        async with sem:
            result = await check_stock(p["url"], p["site"])
            update_stock_status(p["id"], result)
            return p, result

    return list(await asyncio.gather(*[_one(p) for p in products]))


def _format_check_results(results: list[tuple[dict, bool]]) -> str:
    """Format parallel-check results into a readable summary."""
    total = len(results)
    in_stock = [(p, s) for p, s in results if s]
    oos = [(p, s) for p, s in results if not s]
    lines = [f"📊 <b>Check results ({total} item{'s' if total != 1 else ''}):</b>\n"]
    if in_stock:
        lines.append("✅ <b>In Stock:</b>")
        for p, _ in in_stock:
            lines.append(f"  • <b>{p['name']}</b> [{p['site'].capitalize()}]")
            lines.append(f"    <a href=\"{p['url']}\">View →</a>")
    if oos:
        if in_stock:
            lines.append("")
        lines.append("❌ <b>Out of Stock:</b>")
        for p, _ in oos:
            lines.append(f"  • <b>{p['name']}</b> [{p['site'].capitalize()}]")
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
# /start
# ---------------------------------------------------------------------------

@router.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "👋 <b>Welcome to Ankit's Stock Alert Bot!</b>\n\n"
        "I monitor products on Amazon, Flipkart, Zepto, BigBasket, "
        "Blinkit, Croma, Instamart, and Myntra "
        "and alert you the moment they come back in stock.\n\n"
        "<b>Commands:</b>\n"
        "  /add     – Track product(s); bulk format: <code>Name | URL</code> one per line\n"
        "  /list    – View your tracked products\n"
        "  /remove  – Stop tracking a product\n"
        "  /check   – Check stock (filter by store, or check all at once)\n"
        "  /select  – Select items to bulk-check or delete\n"
        "  /search  – Search your tracked products by name\n"
        "  /stores  – List all supported stores\n"
        "  /pins    – Manage your delivery pin codes\n\n"
        "Use /add to get started!",
        parse_mode="HTML",
    )


# ---------------------------------------------------------------------------
# /add  – FSM: name → link(s) → save
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
    await message.answer(
        "📦 <b>Add product(s)</b>\n\n"
        "<b>Option A — Bulk (one per line):</b>\n"
        "<code>Watch | https://amazon.in/…\n"
        "Shirt | https://flipkart.com/…</code>\n\n"
        "<b>Option B — Single:</b> just send the product name, "
        "then the URL in the next step.\n\n"
        "Type /cancel to abort.",
        parse_mode="HTML",
    )


@router.message(Command("cancel"), AddProductStates.waiting_for_name)
@router.message(Command("cancel"), AddProductStates.waiting_for_link)
@router.message(Command("cancel"), SearchStates.waiting_for_keyword)
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("❌ Cancelled.")


@router.message(AddProductStates.waiting_for_name)
async def receive_name(message: Message, state: FSMContext):
    raw = message.text.strip()
    if not raw:
        await message.answer("Input cannot be empty. Please try again.")
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
        f"✅ Name saved: <b>{raw}</b>\n\n"
        "Step 2 of 2 — Send me the <b>product URL</b>.\n"
        "Paste <b>multiple URLs (one per line)</b> to add several products at once.\n"
        f"Supported: {_SUPPORTED_SITES_TEXT}",
        parse_mode="HTML",
    )


@router.message(AddProductStates.waiting_for_link)
async def receive_link(message: Message, state: FSMContext):
    raw = message.text.strip()
    data = await state.get_data()
    name = data["product_name"]
    user_id = message.from_user.id

    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    urls = [ln for ln in lines if ln.startswith(("http://", "https://"))]

    # ── Multi-URL path ──────────────────────────────────────────────────────
    if len(urls) > 1:
        await state.clear()
        results = []
        for url in urls:
            site = detect_site(url)
            if site is None:
                results.append(f"❌ Unsupported site: <code>{url[:60]}</code>")
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
        await message.answer(
            "⚠️ That doesn't look like a valid URL. "
            "Please paste the full link (starting with https://)."
        )
        return

    site = detect_site(url)
    if site is None:
        await message.answer(
            "❌ <b>Unsupported website.</b>\n\n"
            f"Supported: {_SUPPORTED_SITES_TEXT}\n\n"
            "Please send a link from one of these sites.",
            parse_mode="HTML",
        )
        return

    ok, msg = add_product(user_id, name, url, site)
    await state.clear()

    if ok:
        await message.answer(
            f"🎉 <b>Product added!</b>\n\n"
            f"📌 <b>Name:</b> {name}\n"
            f"🛒 <b>Site:</b> {site.capitalize()}\n"
            f"🔗 <b>URL:</b> {url}\n\n"
            "I'll notify you as soon as it's back in stock!",
            parse_mode="HTML",
        )
    else:
        await message.answer(f"⚠️ {msg}")


# ---------------------------------------------------------------------------
# /list
# ---------------------------------------------------------------------------

@router.message(Command("list"))
async def cmd_list(message: Message):
    user_id = message.from_user.id
    products = list_products(user_id)

    if not products:
        await message.answer(
            "📭 You have no tracked products yet.\n"
            "Use /add to start tracking one!"
        )
        return

    lines = ["📋 <b>Your Tracked Products</b>\n"]
    for p in products:
        stock_emoji = "✅" if p["in_stock"] else "❌"
        checked = p["last_checked"] or "Never"
        lines.append(
            f"{stock_emoji} <b>{p['name']}</b> [{p['site'].capitalize()}]\n"
            f"   🆔 ID: <code>{p['id']}</code>\n"
            f"   🕒 Last checked: {checked}\n"
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
    products = list_products(user_id)

    if not products:
        await message.answer(
            "📭 You have no products to remove.\n"
            "Use /add to start tracking one!"
        )
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
        "🗑 <b>Select a product to remove:</b>",
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
    products = list_products(user_id)

    if not products:
        await message.answer(
            "📭 You have no tracked products yet.\n"
            "Use /add to start tracking one!"
        )
        return

    await message.answer(
        "🏪 <b>Filter by store</b>\n\n"
        "Pick a store to check, or check all at once:",
        parse_mode="HTML",
        reply_markup=_check_store_filter_keyboard(products),
    )


@router.callback_query(F.data == "check_all_now")
async def callback_check_all_now(call: CallbackQuery):
    """Check every tracked product in parallel."""
    products = list_products(call.from_user.id)
    if not products:
        await call.answer("No products to check!", show_alert=True)
        return

    await call.message.edit_text(
        f"⏳ Checking all <b>{len(products)}</b> product(s) in parallel…\n"
        "This may take a moment.",
        parse_mode="HTML",
    )
    await call.answer()

    results = await _parallel_check(products)
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

    in_stock = await check_stock(product["url"], product["site"])
    update_stock_status(product_id, in_stock)

    status_emoji = "✅" if in_stock else "❌"
    status_text = "IN STOCK" if in_stock else "OUT OF STOCK"
    await call.message.edit_text(
        f"{status_emoji} <b>{product['name']}</b>\n\n"
        f"Status: <b>{status_text}</b>\n"
        f"Site: {product['site'].capitalize()}\n"
        f"🔗 <a href=\"{product['url']}\">View product</a>",
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
        await message.answer(
            "📭 You have no tracked products yet.\n"
            "Use /add to start tracking one!"
        )
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
    products = list_products(call.from_user.id)
    if not products:
        await call.answer("No products to check!", show_alert=True)
        return
    await call.message.edit_text(
        f"⏳ Checking all <b>{len(products)}</b> product(s) in parallel…\n"
        "This may take a moment.",
        parse_mode="HTML",
    )
    await call.answer()
    results = await _parallel_check(products)
    await call.message.edit_text(
        _format_check_results(results),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
    await state.clear()


@router.callback_query(F.data == "sel_check_selected", SelectStates.selecting)
async def callback_sel_check_selected(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    selected = set(data.get("selected_ids", []))
    if not selected:
        await call.answer("No items selected! Tap ⬜ to select items first.", show_alert=True)
        return
    products = [p for p in list_products(call.from_user.id) if p["id"] in selected]
    if not products:
        await call.answer("Selected products not found.", show_alert=True)
        return
    await call.message.edit_text(
        f"⏳ Checking <b>{len(products)}</b> selected product(s) in parallel…\n"
        "This may take a moment.",
        parse_mode="HTML",
    )
    await call.answer()
    results = await _parallel_check(products)
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
    lines = ["🏪 <b>Supported Stores</b>\n\n"
             "We currently support tracking on these stores:\n"]
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
