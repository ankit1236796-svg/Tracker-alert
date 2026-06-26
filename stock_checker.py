import asyncio
from playwright.async_api import async_playwright
from database import get_all_products, update_stock_status
from config import CHECK_INTERVAL

def detect_platform(url: str) -> str:
    url = url.lower()
    if 'amazon' in url: return 'amazon'
    if 'flipkart' in url: return 'flipkart'
    if 'zepto' in url: return 'zepto'
    if 'bigbasket' in url: return 'bigbasket'
    return 'unknown'

async def check_amazon(page, url):
    await page.goto(url, timeout=60000)
    await page.wait_for_timeout(5000)
    content = await page.content()
    # Amazon US IP issue fix: Checking strict out of stock keywords
    return "Currently unavailable" not in content and "Out of stock" not in content

async def check_flipkart(page, url):
    await page.goto(url, timeout=60000)
    await page.wait_for_timeout(5000)
    content = await page.content()
    return "Sold Out" not in content and "Currently Unavailable" not in content

async def check_zepto(page, url):
    await page.goto(url, timeout=60000)
    await page.wait_for_timeout(5000)
    content = await page.content()
    return "Out of Stock" not in content

async def check_bigbasket(page, url):
    await page.goto(url, timeout=60000)
    await page.wait_for_timeout(5000)
    content = await page.content()
    return "Out of Stock" not in content and "Notify Me" not in content

async def check_stock(page, url: str, platform: str) -> bool:
    try:
        if platform == 'amazon': return await check_amazon(page, url)
        if platform == 'flipkart': return await check_flipkart(page, url)
        if platform == 'zepto': return await check_zepto(page, url)
        if platform == 'bigbasket': return await check_bigbasket(page, url)
    except Exception as e:
        print(f"Error checking {url}: {e}")
    return False

async def stock_checker_loop(bot):
    async with async_playwright() as p:
        # Browser ko bina sandbox ke aur normal size mein launch karna
        browser = await p.chromium.launch(headless=True, args=[
            "--no-sandbox", 
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled" # Anti-bot trick 1
        ])
        
        while True:
            products = get_all_products()
            if products:
                # --- SMART STEALTH CONTEXT START ---
                context = await browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                    viewport={"width": 1920, "height": 1080},
                    locale="en-IN", # India Locale
                    timezone_id="Asia/Kolkata", # Indian Standard Time
                    geolocation={"longitude": 77.2090, "latitude": 28.6139}, # Delhi Location
                    permissions=["geolocation"]
                )
                
                # Anti-bot trick 2: Removing 'webdriver' flag so sites think it's a real human
                await context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
                # --- SMART STEALTH CONTEXT END ---
                
                page = await context.new_page()
                
                for prod in products:
                    p_id, user_id, name, url, platform, currently_in_stock = prod
                    is_in_stock = await check_stock(page, url, platform)
                    
                    if is_in_stock and not currently_in_stock:
                        msg = f"🚨 **STOCK ALERT** 🚨\n\nYour product **{name}** is now IN STOCK on {platform.title()}!\n🔗 {url}"
                        await bot.send_message(user_id, msg)
                        update_stock_status(p_id, 1)
                        
                    elif not is_in_stock and currently_in_stock:
                        update_stock_status(p_id, 0)
                        
                await context.close()
            
            await asyncio.sleep(CHECK_INTERVAL)
