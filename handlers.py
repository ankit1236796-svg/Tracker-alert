import logging

from aiogram import Router, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
)

from states import AddProductStates
from database import add_product, list_products, remove_product
from stock_checker import detect_site

logger = logging.getLogger(__name__)
router = Router()


# ---------------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------------

@router.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "👋 <b>Welcome to Stock Alert Bot!</b>\n\n"
        "I monitor products on Amazon, Flipkart, Zepto, and BigBasket "
        "and alert you the moment they come back in stock.\n\n"
        "<b>Commands:</b>\n"
        "  /add    – Track a new product\n"
        "  /list   – View your tracked products\n"
        "  /remove – Stop tracking a product\n\n"
        "Use /add to get started!",
        parse_mode="HTML",
    )


# ---------------------------------------------------------------------------
# /add  – FSM: name → link → save
# ---------------------------------------------------------------------------

@router.message(Command("add"))
async def cmd_add(message: Message, state: FSMContext):
    await state.set_state(AddProductStates.waiting_for_name)
    await message.answer(
        "📦 <b>Add a new product</b>\n\n"
        "Step 1 of 2 — Send me the <b>product name</b> (so you recognise it later).\n\n"
        "Type /cancel at any time to stop.",
        parse_mode="HTML",
    )


@router.message(Command("cancel"), AddProductStates.waiting_for_name)
@router.message(Command("cancel"), AddProductStates.waiting_for_link)
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("❌ Cancelled. Nothing was saved.")


@router.message(AddProductStates.waiting_for_name)
async def receive_name(message: Message, state: FSMContext):
    name = message.text.strip()
    if not name:
        await message.answer("Product name cannot be empty. Please try again.")
        return

    await state.update_data(product_name=name)
    await state.set_state(AddProductStates.waiting_for_link)
    await message.answer(
        f"✅ Name saved: <b>{name}</b>\n\n"
        "Step 2 of 2 — Now send me the <b>product URL</b>.\n"
        "Supported sites: Amazon · Flipkart · Zepto · BigBasket",
        parse_mode="HTML",
    )


@router.message(AddProductStates.waiting_for_link)
async def receive_link(message: Message, state: FSMContext):
    url = message.text.strip()

    # Basic URL validation
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
            "I only support:\n"
            "• amazon.in / amazon.com\n"
            "• flipkart.com\n"
            "• zeptonow.com\n"
            "• bigbasket.com\n\n"
            "Please send a link from one of these sites.",
            parse_mode="HTML",
        )
        return

    data = await state.get_data()
    name = data["product_name"]
    user_id = message.from_user.id

    success, msg = add_product(user_id, name, url, site)
    await state.clear()

    if success:
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
