from aiogram import Router, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from states import AddProduct
from database import add_product, get_user_products, remove_product
from stock_checker import detect_platform

router = Router()

@router.message(Command("start"))
async def cmd_start(message: types.Message):
    welcome_text = (
        "Welcome to Stock Alert Bot! 🛒\n\n"
        "**Available Commands:**\n"
        "/add - Add a new product to track\n"
        "/list - View your tracked products\n"
        "/remove <id> - Remove a product from tracking"
    )
    await message.answer(welcome_text)

@router.message(Command("add"))
async def cmd_add(message: types.Message, state: FSMContext):
    await message.answer("What is the name of the product you want to track?")
    await state.set_state(AddProduct.waiting_for_name)

@router.message(AddProduct.waiting_for_name)
async def process_name(message: types.Message, state: FSMContext):
    await state.update_data(name=message.text)
    await message.answer("Great! Now send me the product link (Supports Amazon, Flipkart, Zepto, or BigBasket).")
    await state.set_state(AddProduct.waiting_for_link)

@router.message(AddProduct.waiting_for_link)
async def process_link(message: types.Message, state: FSMContext):
    url = message.text
    platform = detect_platform(url)
    
    if platform == 'unknown':
        await message.answer("❌ Unsupported link! Please provide a link from Amazon, Flipkart, Zepto, or BigBasket.")
        return

    user_data = await state.get_data()
    name = user_data['name']
    
    add_product(message.from_user.id, name, url, platform)
    await message.answer(f"✅ Product **{name}** added successfully! I will notify you when it's in stock on {platform.title()}.")
    await state.clear()

@router.message(Command("list"))
async def cmd_list(message: types.Message):
    products = get_user_products(message.from_user.id)
    if not products:
        await message.answer("You are not tracking any products right now. Use /add to get started.")
        return
    
    text = "📦 **Your Tracked Products:**\n\n"
    for p in products:
        status = "🟢 In Stock" if p[4] else "🔴 Out of Stock / Waiting"
        text += f"**ID:** `{p[0]}`\n**Name:** {p[1]}\n**Platform:** {p[3].title()}\n**Status:** {status}\n🔗 [Product Link]({p[2]})\n\n"
    
    await message.answer(text, disable_web_page_preview=True)

@router.message(Command("remove"))
async def cmd_remove(message: types.Message):
    args = message.text.split()
    if len(args) != 2 or not args[1].isdigit():
        await message.answer("⚠️ Please provide the Product ID to remove.\nExample: `/remove 1`\n\nUse `/list` to find your product IDs.")
        return
    
    product_id = int(args[1])
    remove_product(message.from_user.id, product_id)
    await message.answer(f"🗑️ Product ID `{product_id}` removed (if it existed in your list).")
