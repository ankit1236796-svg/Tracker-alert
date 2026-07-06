"""
translations.py
~~~~~~~~~~~~~~~
Single source of truth for user-facing message text in the three supported
languages: English ('en'), Hindi/Devanagari formal-आप ('hi'), and balanced
Hinglish/Roman ('hinglish').

Every in-scope string is a key in _T mapping lang -> template. Templates use
str.format named fields (e.g. {name}, {price}) and keep HTML tags + command
tokens (/add) + emoji identical across languages — only the words change.

Resolve text with t(key, lang, **vars). Callers get the recipient's lang from
the DB (database.get_user_lang) — the aiogram handlers, the background stock
alert loop, and the web dashboard's notifier all funnel through here so the
three surfaces can't drift.

NOTE: admin-only commands, rare/technical errors, store names, product names,
URLs and numbers are intentionally NOT translated (English/literal).
"""

import logging

logger = logging.getLogger(__name__)

LANGS = ("en", "hi", "hinglish")
DEFAULT_LANG = "en"

LANG_LABEL = {"en": "English", "hi": "हिंदी", "hinglish": "Hinglish"}


def t(key: str, lang: str = DEFAULT_LANG, **kwargs) -> str:
    """Return the translated, formatted string for `key` in `lang`. Falls back
    to English for an unknown lang/key, and to the raw template if formatting
    fails (missing var) — never raises into a message-send path."""
    table = _T.get(key)
    if table is None:
        logger.warning(f"[i18n] missing translation key: {key!r}")
        return key
    template = table.get(lang) or table.get(DEFAULT_LANG) or ""
    if not kwargs:
        return template
    try:
        return template.format(**kwargs)
    except Exception as exc:
        logger.error(f"[i18n] format failed for key={key!r} lang={lang!r}: {exc}")
        return table.get(DEFAULT_LANG, template)


_T: dict[str, dict[str, str]] = {
    # ── Onboarding / /start ──────────────────────────────────────────────────
    "welcome_body": {
        "en": (
            "👋 <b>Welcome to Ullu Alert!</b>\n\n{trial_line}"
            "I monitor products on multiple online shopping sites "
            "and alert you the moment they come back in stock.\n\n"
        ),
        "hi": (
            "👋 <b>Ullu Alert में आपका स्वागत है!</b>\n\n{trial_line}"
            "मैं कई online shopping sites पर products को monitor करता हूँ और "
            "जैसे ही वो stock में वापस आते हैं, आपको तुरंत alert भेजता हूँ।\n\n"
        ),
        "hinglish": (
            "👋 <b>Ullu Alert me aapka welcome hai!</b>\n\n{trial_line}"
            "Main bohot saari online shopping sites pe products track karta hoon "
            "aur jaise hi wo wapas stock me aate hain, turant alert bhej deta hoon.\n\n"
        ),
    },
    "welcome_commands": {
        "en": (
            "<b>Commands:</b>\n"
            "  /add     – Track product(s); bulk format: <code>Name | URL</code> one per line\n"
            "  /list    – View your tracked products\n"
            "  /remove  – Stop tracking a product\n"
            "  /check   – Check stock (filter by store, or check all at once)\n"
            "  /select  – Select items to bulk-check or delete\n"
            "  /search  – Search your tracked products by name\n"
            "  /stores  – List all supported stores\n"
            "  /pins    – Manage your delivery pin codes\n"
            "  /language – Change language (English / हिंदी / Hinglish)\n"
            "  /freetrial – Get a bonus free trial by sharing on WhatsApp\n\n"
            "Use /add to get started!"
        ),
        "hi": (
            "<b>Commands:</b>\n"
            "  /add     – Product(s) track करें; bulk format: <code>Name | URL</code> एक लाइन में एक\n"
            "  /list    – अपने tracked products देखें\n"
            "  /remove  – किसी product को track करना बंद करें\n"
            "  /check   – Stock check करें (store चुनें, या सब एक साथ)\n"
            "  /select  – Bulk check या delete के लिए items चुनें\n"
            "  /search  – अपने products नाम से खोजें\n"
            "  /stores  – सभी supported stores देखें\n"
            "  /pins    – अपने delivery pin codes manage करें\n"
            "  /language – भाषा बदलें (English / हिंदी / Hinglish)\n"
            "  /freetrial – WhatsApp पर share करके bonus free trial पाएं\n\n"
            "शुरू करने के लिए /add इस्तेमाल करें!"
        ),
        "hinglish": (
            "<b>Commands:</b>\n"
            "  /add     – Product(s) track karo; bulk format: <code>Name | URL</code> ek line me ek\n"
            "  /list    – Apne tracked products dekho\n"
            "  /remove  – Kisi product ko track karna band karo\n"
            "  /check   – Stock check karo (store choose karo, ya sab ek saath)\n"
            "  /select  – Bulk check ya delete ke liye items choose karo\n"
            "  /search  – Apne products naam se search karo\n"
            "  /stores  – Saare supported stores dekho\n"
            "  /pins    – Apne delivery pin codes manage karo\n"
            "  /language – Language badlo (English / हिंदी / Hinglish)\n"
            "  /freetrial – WhatsApp pe share karke bonus free trial pao\n\n"
            "Shuru karne ke liye /add use karo!"
        ),
    },
    "status_trial": {
        "en": "🎁 <b>Free trial active</b> — {days} day(s) left (started with a {trial_days}-day trial).\n\n",
        "hi": "🎁 <b>Free trial चालू है</b> — {days} दिन बाकी ({trial_days}-दिन के trial से शुरू)।\n\n",
        "hinglish": "🎁 <b>Free trial active hai</b> — {days} din bache ({trial_days}-din ke trial se shuru).\n\n",
    },
    "status_plan": {
        "en": "✅ <b>{plan}</b> active — {days} day(s) left.\n\n",
        "hi": "✅ <b>{plan}</b> चालू है — {days} दिन बाकी।\n\n",
        "hinglish": "✅ <b>{plan}</b> active hai — {days} din bache.\n\n",
    },

    # ── Access / lockout ─────────────────────────────────────────────────────
    "access_blocked": {
        "en": ("🚫 <b>Your access has been blocked by the admin.</b>\n\n"
               "If you believe this is a mistake, please contact the admin."),
        "hi": ("🚫 <b>आपका access admin द्वारा block कर दिया गया है।</b>\n\n"
               "अगर आपको लगता है कि ये गलती है, तो admin से संपर्क करें।"),
        "hinglish": ("🚫 <b>Aapka access admin ne block kar diya hai.</b>\n\n"
                     "Agar aapko lagta hai ye galti hai, to admin se contact karo."),
    },
    "access_no_trial": {
        "en": ("👋 <b>You don't have an active trial yet.</b>\n\n"
               "Use /freetrial to get a free trial by sharing Ullu Alert on "
               "WhatsApp, or contact the admin for manual approval."),
        "hi": ("👋 <b>अभी आपके पास कोई active trial नहीं है।</b>\n\n"
               "WhatsApp पर Ullu Alert share करके free trial पाने के लिए /freetrial "
               "इस्तेमाल करें, या manual approval के लिए admin से संपर्क करें।"),
        "hinglish": ("👋 <b>Abhi aapke paas koi active trial nahi hai.</b>\n\n"
                     "WhatsApp pe Ullu Alert share karke free trial paane ke liye /freetrial "
                     "use karo, ya manual approval ke liye admin se contact karo."),
    },
    "access_expired_grace": {
        "en": ("⏰ <b>Your access has expired.</b>\n\n"
               "Your tracked items are safely kept for <b>{grace} more day{s}</b> — "
               "renew within that window and your full list is restored automatically. "
               "After that, they're permanently deleted.\n\n{payment}"),
        "hi": ("⏰ <b>आपका access expire हो गया है।</b>\n\n"
               "आपके tracked items <b>{grace} और दिन</b> तक safe रखे जाते हैं — इस दौरान "
               "renew करें और आपकी पूरी list अपने आप वापस आ जाएगी। उसके बाद वो हमेशा के लिए "
               "delete हो जाते हैं।\n\n{payment}"),
        "hinglish": ("⏰ <b>Aapka access expire ho gaya hai.</b>\n\n"
                     "Aapke tracked items <b>{grace} aur din</b> tak safe rahenge — is beech "
                     "renew karo aur aapki poori list apne aap wapas aa jayegi. Uske baad wo "
                     "hamesha ke liye delete ho jaate hain.\n\n{payment}"),
    },
    "access_trial_ended": {
        "en": ("⏰ <b>Your trial has ended.</b>\n\n"
               "You need an active plan to keep using this bot.\n\n{payment}"),
        "hi": ("⏰ <b>आपका trial खत्म हो गया है।</b>\n\n"
               "इस bot को इस्तेमाल करते रहने के लिए आपको active plan चाहिए।\n\n{payment}"),
        "hinglish": ("⏰ <b>Aapka trial khatam ho gaya hai.</b>\n\n"
                     "Is bot ko use karte rehne ke liye aapko active plan chahiye.\n\n{payment}"),
    },
    "payment_instructions": {
        "en": ("💳 <b>To get access:</b>\n"
               "Send an Amazon Gift Card to the admin (details to be shared) and "
               "include your Telegram user ID in the message.\n\n"
               "📩 Contact: the admin will review and approve your access shortly "
               "after payment — use /start any time to check your status."),
        "hi": ("💳 <b>Access पाने के लिए:</b>\n"
               "Admin को Amazon Gift Card भेजें (details बाद में share होंगी) और message में "
               "अपना Telegram user ID ज़रूर लिखें।\n\n"
               "📩 Contact: payment के बाद admin आपके access को review करके जल्दी approve कर "
               "देंगे — अपना status देखने के लिए कभी भी /start इस्तेमाल करें।"),
        "hinglish": ("💳 <b>Access paane ke liye:</b>\n"
                     "Admin ko Amazon Gift Card bhejo (details baad me share hongi) aur message me "
                     "apna Telegram user ID zaroor likho.\n\n"
                     "📩 Contact: payment ke baad admin aapke access ko review karke jaldi approve "
                     "kar denge — apna status dekhne ke liye kabhi bhi /start use karo."),
    },
    "no_active_plan": {
        "en": "⚠️ You don't have an active plan assigned. Contact the admin to get set up.",
        "hi": "⚠️ आपको कोई active plan assign नहीं है। Setup के लिए admin से संपर्क करें।",
        "hinglish": "⚠️ Aapko koi active plan assign nahi hai. Setup ke liye admin se contact karo.",
    },
    "item_limit": {
        "en": ("🚫 <b>Item limit reached.</b>\n\n"
               "Your <b>{plan}</b> plan allows up to <b>{max}</b> tracked items, and "
               "you're currently tracking <b>{count}</b>.\n\n"
               "Remove an item with /remove, or contact the admin to upgrade your plan."),
        "hi": ("🚫 <b>Item limit पूरी हो गई।</b>\n\n"
               "आपका <b>{plan}</b> plan ज़्यादा से ज़्यादा <b>{max}</b> items track करने देता है, "
               "और अभी आप <b>{count}</b> track कर रहे हैं।\n\n"
               "/remove से कोई item हटाएं, या plan upgrade करने के लिए admin से संपर्क करें।"),
        "hinglish": ("🚫 <b>Item limit poori ho gayi.</b>\n\n"
                     "Aapka <b>{plan}</b> plan max <b>{max}</b> items track karne deta hai, aur "
                     "abhi aap <b>{count}</b> track kar rahe ho.\n\n"
                     "/remove se koi item hatao, ya plan upgrade karne ke liye admin se contact karo."),
    },
    "store_not_in_plan": {
        "en": ("🚫 <b>Store not included in your plan.</b>\n\n"
               "Your <b>{plan}</b> plan only allows: <b>{sites}</b>.\n\n"
               "Contact the admin to upgrade to a plan that includes this store."),
        "hi": ("🚫 <b>ये store आपके plan में नहीं है।</b>\n\n"
               "आपका <b>{plan}</b> plan सिर्फ़ ये allow करता है: <b>{sites}</b>.\n\n"
               "इस store वाले plan के लिए admin से upgrade करवाएं।"),
        "hinglish": ("🚫 <b>Ye store aapke plan me nahi hai.</b>\n\n"
                     "Aapka <b>{plan}</b> plan sirf ye allow karta hai: <b>{sites}</b>.\n\n"
                     "Is store wale plan ke liye admin se upgrade karwao."),
    },
    "store_locked": {
        "en": ("🔒 <b>{site} tracking is currently unavailable.</b>\n\n"
               "This store has been temporarily disabled. Please try another "
               "store, or check back later."),
        "hi": ("🔒 <b>{site} tracking अभी उपलब्ध नहीं है।</b>\n\n"
               "ये store फ़िलहाल disable किया गया है। कृपया कोई और store आज़माएं, "
               "या बाद में दोबारा देखें।"),
        "hinglish": ("🔒 <b>{site} tracking abhi available nahi hai.</b>\n\n"
                     "Ye store filhaal disable kiya gaya hai. Koi aur store try karo, "
                     "ya baad me dobara check karo."),
    },

    # ── Notifications ────────────────────────────────────────────────────────
    "stock_alert": {
        "en": ("🚨 <b>Back in Stock!</b>\n\n"
               "📦 <b>{name}</b> is now available on <b>{site}</b>!{price_line}\n\n"
               "🛒 <a href=\"{url}\">Buy it now →</a>"),
        "hi": ("🚨 <b>वापस स्टॉक में!</b>\n\n"
               "📦 <b>{name}</b> अब <b>{site}</b> पर उपलब्ध है!{price_line}\n\n"
               "🛒 <a href=\"{url}\">अभी खरीदें →</a>"),
        "hinglish": ("🚨 <b>Wapas Stock me aa gaya!</b>\n\n"
                     "📦 <b>{name}</b> ab <b>{site}</b> pe available hai!{price_line}\n\n"
                     "🛒 <a href=\"{url}\">Abhi kharido →</a>"),
    },
    "stock_alert_price_line": {
        "en": "\n💰 <b>Current price: ₹{price}</b>",
        "hi": "\n💰 <b>अभी कीमत: ₹{price}</b>",
        "hinglish": "\n💰 <b>Abhi ka price: ₹{price}</b>",
    },
    "item_removed_tail": {
        "en": ("To keep the bot fast, accurate, and running smoothly for everyone, "
               "some items get cleared. Re-add anytime with /add!"),
        "hi": ("Bot को सबके लिए fast, सही और smooth रखने के लिए कुछ items हटा दिए जाते हैं। "
               "कभी भी /add से दोबारा जोड़ सकते हैं!"),
        "hinglish": ("Bot ko sabke liye fast, accurate aur smooth rakhne ke liye kuch items "
                     "clear ho jaate hain. Kabhi bhi /add se dobara add kar sakte ho!"),
    },
    "item_removed_single": {
        "en": "🦉 Ullu removed: {name}",
        "hi": "🦉 Ullu ने हटाया: {name}",
        "hinglish": "🦉 Ullu ne hataya: {name}",
    },
    "item_removed_multi_header": {
        "en": "🦉 Ullu removed the following items:",
        "hi": "🦉 Ullu ने ये items हटाए:",
        "hinglish": "🦉 Ullu ne ye items hataye:",
    },
    "approval_notice": {
        "en": ("✅ <b>Access approved!</b>\n\n"
               "📦 Plan: <b>{plan}</b>\n➕ Days added: <b>{days}</b>\n"
               "📅 Access until: <b>{until}</b>\n\n"
               "Thanks for your payment — you're all set. Use /list to see your tracked items."),
        "hi": ("✅ <b>Access approve हो गया!</b>\n\n"
               "📦 Plan: <b>{plan}</b>\n➕ जोड़े गए दिन: <b>{days}</b>\n"
               "📅 Access तब तक: <b>{until}</b>\n\n"
               "आपके payment के लिए धन्यवाद — अब सब तैयार है। अपने tracked items देखने के लिए /list इस्तेमाल करें।"),
        "hinglish": ("✅ <b>Access approve ho gaya!</b>\n\n"
                     "📦 Plan: <b>{plan}</b>\n➕ Din add hue: <b>{days}</b>\n"
                     "📅 Access kab tak: <b>{until}</b>\n\n"
                     "Payment ke liye thanks — ab sab set hai. Apne tracked items dekhne ke liye /list use karo."),
    },
    "rejection_notice": {
        "en": "❌ <b>Your access request was not approved.</b>{reason}\n\nContact the admin if you have questions.",
        "hi": "❌ <b>आपका access request approve नहीं हुआ।</b>{reason}\n\nकोई सवाल हो तो admin से संपर्क करें।",
        "hinglish": "❌ <b>Aapka access request approve nahi hua.</b>{reason}\n\nKoi sawaal ho to admin se contact karo.",
    },
    "rejection_reason": {
        "en": "\n\nReason: {reason}",
        "hi": "\n\nकारण: {reason}",
        "hinglish": "\n\nReason: {reason}",
    },
    "block_notice": {
        "en": ("🚫 <b>Your access has been blocked by the admin.</b>\n\n"
               "Contact the admin if you believe this is a mistake."),
        "hi": ("🚫 <b>आपका access admin द्वारा block कर दिया गया है।</b>\n\n"
               "अगर आपको लगता है कि ये गलती है, तो admin से संपर्क करें।"),
        "hinglish": ("🚫 <b>Aapka access admin ne block kar diya hai.</b>\n\n"
                     "Agar aapko lagta hai ye galti hai, to admin se contact karo."),
    },
    "unblock_notice": {
        "en": "✅ <b>Your access has been restored.</b> Welcome back!",
        "hi": "✅ <b>आपका access वापस चालू कर दिया गया है।</b> वापसी पर स्वागत है!",
        "hinglish": "✅ <b>Aapka access wapas chalu kar diya gaya hai.</b> Welcome back!",
    },
    "expiry_reminder": {
        "en": ("⏰ <b>Your {kind} expires in about {hours} hour(s).</b>\n\n"
               "💳 To keep your alerts running, send an Amazon Gift Card to the admin "
               "(details to be shared) and include your Telegram user ID.\n\n"
               "📩 The admin will review and extend your access after payment."),
        "hi": ("⏰ <b>आपका {kind} करीब {hours} घंटे में expire हो जाएगा।</b>\n\n"
               "💳 अपने alerts चालू रखने के लिए admin को Amazon Gift Card भेजें "
               "(details बाद में) और अपना Telegram user ID ज़रूर लिखें।\n\n"
               "📩 Payment के बाद admin आपका access review करके बढ़ा देंगे।"),
        "hinglish": ("⏰ <b>Aapka {kind} karib {hours} ghante me expire ho jayega.</b>\n\n"
                     "💳 Apne alerts chalu rakhne ke liye admin ko Amazon Gift Card bhejo "
                     "(details baad me) aur apna Telegram user ID zaroor likho.\n\n"
                     "📩 Payment ke baad admin aapka access review karke badha denge."),
    },
    "expiry_kind_trial": {"en": "trial", "hi": "trial", "hinglish": "trial"},
    "expiry_kind_paid": {"en": "paid access", "hi": "paid access", "hinglish": "paid access"},
    "data_purged": {
        "en": ("🗑 Your <b>{count}</b> tracked item(s) have been permanently deleted "
               "after your access grace period expired without renewal.\n\n"
               "You can start fresh any time once your access is restored."),
        "hi": ("🗑 आपके <b>{count}</b> tracked item(s) हमेशा के लिए delete कर दिए गए हैं "
               "क्योंकि grace period में access renew नहीं हुआ।\n\n"
               "Access वापस मिलने पर आप कभी भी नए सिरे से शुरू कर सकते हैं।"),
        "hinglish": ("🗑 Aapke <b>{count}</b> tracked item(s) hamesha ke liye delete kar diye gaye hain "
                     "kyunki grace period me access renew nahi hua.\n\n"
                     "Access wapas milne pe aap kabhi bhi naye sire se shuru kar sakte ho."),
    },

    # ── Free trial ───────────────────────────────────────────────────────────
    "ft_header": {
        "en": "🎁 <b>Get a free trial!</b> (Round {n} of {total})\n\n",
        "hi": "🎁 <b>Free trial पाएं!</b> (Round {n} / {total})\n\n",
        "hinglish": "🎁 <b>Free trial pao!</b> (Round {n} / {total})\n\n",
    },
    "ft_progress_first": {
        "en": ("Share Ullu Alert with a friend or group on WhatsApp, then confirm below — "
               "do this {total} times to unlock your free trial.\n\n"),
        "hi": ("Ullu Alert को WhatsApp पर किसी दोस्त या group के साथ share करें, फिर नीचे confirm करें — "
               "free trial unlock करने के लिए ये {total} बार करें।\n\n"),
        "hinglish": ("Ullu Alert ko WhatsApp pe kisi dost ya group ko share karo, phir niche confirm karo — "
                     "free trial unlock karne ke liye ye {total} baar karo.\n\n"),
    },
    "ft_progress_more": {
        "en": "✅ {done}/{total} shares done — keep going!\n\n",
        "hi": "✅ {done}/{total} shares हो गए — बढ़ते रहें!\n\n",
        "hinglish": "✅ {done}/{total} shares ho gaye — lage raho!\n\n",
    },
    "ft_waiting": {
        "en": ("⏳ Please wait {secs} seconds while you share...\n\n"
               "Tap <b>Share on WhatsApp</b> below — open the app, pick a contact or group, and send it."),
        "hi": ("⏳ Share करते समय {secs} seconds रुकें...\n\n"
               "नीचे <b>Share on WhatsApp</b> दबाएं — app खोलें, कोई contact या group चुनें, और भेज दें।"),
        "hinglish": ("⏳ Share karte waqt {secs} second ruko...\n\n"
                     "Niche <b>Share on WhatsApp</b> dabao — app kholo, koi contact ya group choose karo, aur bhej do."),
    },
    "ft_ready": {
        "en": "✅ Shared? Tap <b>Done</b> below to continue.",
        "hi": "✅ Share कर दिया? आगे बढ़ने के लिए नीचे <b>Done</b> दबाएं।",
        "hinglish": "✅ Share kar diya? Aage badhne ke liye niche <b>Done</b> dabao.",
    },
    "ft_confirm": {
        "en": ("⚠️ <b>Are you sure you shared this in {total} WhatsApp groups/contacts?</b>\n\n"
               "Cheating will result in your free trial being denied and you may be "
               "permanently banned from future free trials.\n\nDo you still want to confirm?"),
        "hi": ("⚠️ <b>क्या आपने सच में इसे {total} WhatsApp groups/contacts पर share किया है?</b>\n\n"
               "धोखाधड़ी करने पर आपका free trial रद्द कर दिया जाएगा और आपको आगे के free trials से "
               "हमेशा के लिए ban किया जा सकता है।\n\nक्या आप फिर भी confirm करना चाहते हैं?"),
        "hinglish": ("⚠️ <b>Kya sach me aapne ise {total} WhatsApp groups/contacts pe share kiya hai?</b>\n\n"
                     "Cheating karne pe aapka free trial cancel ho jayega aur future free trials se "
                     "hamesha ke liye ban ho sakte ho.\n\nPhir bhi confirm karna hai?"),
    },
    "ft_already_used": {
        "en": ("🚫 <b>You've already used this offer.</b>\n\n"
               "The WhatsApp-share free trial can only be claimed once per account."),
        "hi": ("🚫 <b>आप ये offer पहले ही इस्तेमाल कर चुके हैं।</b>\n\n"
               "WhatsApp-share free trial हर account पर सिर्फ़ एक बार मिल सकता है।"),
        "hinglish": ("🚫 <b>Aap ye offer pehle hi use kar chuke ho.</b>\n\n"
                     "WhatsApp-share free trial har account pe sirf ek baar mil sakta hai."),
    },
    "ft_request_pending": {
        "en": ("✅ <b>Thanks for sharing!</b>\n\n"
               "Your free trial request is pending admin approval. "
               "You'll be notified once approved."),
        "hi": ("✅ <b>Share करने के लिए धन्यवाद!</b>\n\n"
               "आपका free trial request admin approval के लिए pending है। "
               "Approve होते ही आपको बता दिया जाएगा।"),
        "hinglish": ("✅ <b>Share karne ke liye thanks!</b>\n\n"
                     "Aapka free trial request admin approval ke liye pending hai. "
                     "Approve hote hi aapko bata diya jayega."),
    },
    "ft_wa_share_text": {
        "en": ("🚨 PS5 restock? New iPhone drop? Don't miss it again!\n\n"
               "I use Ullu Alert (100% FREE) — it watches products 24/7 and pings me "
               "the SECOND they're back in stock, so I never miss a restock. 🔥\n\n"
               "Try it free: {link}"),
        "hi": ("🚨 PS5 restock? नया iPhone drop? अब मत चूको!\n\n"
               "मैं Ullu Alert use करता हूँ (100% FREE) — ये products को 24/7 watch करता है और stock "
               "आते ही तुरंत ping कर देता है, तो कोई restock miss नहीं होता। 🔥\n\n"
               "Free try करो: {link}"),
        "hinglish": ("🚨 PS5 restock? Naya iPhone drop? Ab mat chuko!\n\n"
                     "Main Ullu Alert use karta hoon (100% FREE) — ye products ko 24/7 watch karta hai aur "
                     "stock aate hi turant ping kar deta hai, to koi restock miss nahi hota. 🔥\n\n"
                     "Free try karo: {link}"),
    },
    "ft_admin_no_need": {
        "en": "The admin account doesn't need a trial.",
        "hi": "Admin account को trial की ज़रूरत नहीं है।",
        "hinglish": "Admin account ko trial ki zaroorat nahi hai.",
    },

    # ── Add flow ─────────────────────────────────────────────────────────────
    "add_instructions": {
        "en": ("📦 <b>Add product(s)</b>\n\n"
               "<b>Option A — Bulk (one per line):</b>\n"
               "<code>Watch | https://amazon.in/…\nShirt | https://flipkart.com/…</code>\n\n"
               "<b>Option B — Single:</b> just send the product name, then the URL in the next step.\n\n"
               "Type /cancel to abort."),
        "hi": ("📦 <b>Product(s) जोड़ें</b>\n\n"
               "<b>Option A — Bulk (एक लाइन में एक):</b>\n"
               "<code>Watch | https://amazon.in/…\nShirt | https://flipkart.com/…</code>\n\n"
               "<b>Option B — Single:</b> बस product का नाम भेजें, फिर अगले step में URL।\n\n"
               "रद्द करने के लिए /cancel लिखें।"),
        "hinglish": ("📦 <b>Product(s) add karo</b>\n\n"
                     "<b>Option A — Bulk (ek line me ek):</b>\n"
                     "<code>Watch | https://amazon.in/…\nShirt | https://flipkart.com/…</code>\n\n"
                     "<b>Option B — Single:</b> bas product ka naam bhejo, phir agle step me URL.\n\n"
                     "Cancel karne ke liye /cancel likho."),
    },
    "add_name_saved": {
        "en": ("✅ Name saved: <b>{name}</b>\n\n"
               "Step 2 of 2 — Send me the <b>product URL</b>.\n"
               "Paste <b>multiple URLs (one per line)</b> to add several products at once.\n"
               "Supported: {sites}"),
        "hi": ("✅ नाम save हुआ: <b>{name}</b>\n\n"
               "Step 2 of 2 — मुझे <b>product URL</b> भेजें।\n"
               "एक साथ कई products जोड़ने के लिए <b>कई URLs (एक लाइन में एक)</b> paste करें।\n"
               "Supported: {sites}"),
        "hinglish": ("✅ Naam save hua: <b>{name}</b>\n\n"
                     "Step 2 of 2 — Mujhe <b>product URL</b> bhejo.\n"
                     "Ek saath kai products add karne ke liye <b>kai URLs (ek line me ek)</b> paste karo.\n"
                     "Supported: {sites}"),
    },
    "add_empty_input": {
        "en": "Input cannot be empty. Please try again.",
        "hi": "Input खाली नहीं हो सकता। कृपया दोबारा try करें।",
        "hinglish": "Input khaali nahi ho sakta. Dobara try karo.",
    },
    "add_invalid_url": {
        "en": "⚠️ That doesn't look like a valid URL. Please paste the full link (starting with https://).",
        "hi": "⚠️ ये सही URL नहीं लग रहा। कृपया पूरा link paste करें (https:// से शुरू)।",
        "hinglish": "⚠️ Ye sahi URL nahi lag raha. Poora link paste karo (https:// se shuru hone wala).",
    },
    "add_unsupported": {
        "en": ("❌ <b>Unsupported website.</b>\n\nSupported: {sites}\n\n"
               "Please send a link from one of these sites."),
        "hi": ("❌ <b>ये website supported नहीं है।</b>\n\nSupported: {sites}\n\n"
               "कृपया इनमें से किसी site का link भेजें।"),
        "hinglish": ("❌ <b>Ye website supported nahi hai.</b>\n\nSupported: {sites}\n\n"
                     "Inme se kisi site ka link bhejo."),
    },
    "amazon_target_prompt": {
        "en": ("💰 <b>Set a target price (optional)</b>\n\nTracking: <b>{name}</b>\n\n"
               "Send a target price (e.g. <code>1299</code> or <code>1299.99</code>) to only get "
               "alerted when the price drops to or below that amount.\n\n"
               "Or send /skip to get alerted at any price."),
        "hi": ("💰 <b>Target price set करें (optional)</b>\n\nTracking: <b>{name}</b>\n\n"
               "Target price भेजें (जैसे <code>1299</code> या <code>1299.99</code>) ताकि alert तभी मिले "
               "जब price उतनी या उससे कम हो जाए।\n\n"
               "या किसी भी price पर alert पाने के लिए /skip भेजें।"),
        "hinglish": ("💰 <b>Target price set karo (optional)</b>\n\nTracking: <b>{name}</b>\n\n"
                     "Target price bhejo (jaise <code>1299</code> ya <code>1299.99</code>) taki alert tabhi mile "
                     "jab price utni ya usse kam ho jaye.\n\n"
                     "Ya kisi bhi price pe alert paane ke liye /skip bhejo."),
    },
    "target_invalid": {
        "en": ("⚠️ That doesn't look like a valid price. Send a number like <code>1299</code> or "
               "<code>1299.99</code>, or /skip to track at any price."),
        "hi": ("⚠️ ये सही price नहीं लग रही। <code>1299</code> या <code>1299.99</code> जैसा number भेजें, "
               "या किसी भी price पर track करने के लिए /skip भेजें।"),
        "hinglish": ("⚠️ Ye sahi price nahi lag rahi. <code>1299</code> ya <code>1299.99</code> jaisa number bhejo, "
                     "ya kisi bhi price pe track karne ke liye /skip bhejo."),
    },
    "product_added": {
        "en": ("🎉 <b>Product added!</b>\n\n📌 <b>Name:</b> {name}\n🛒 <b>Site:</b> {site}\n"
               "🔗 <b>URL:</b> {url}\n\nI'll notify you as soon as it's back in stock!"),
        "hi": ("🎉 <b>Product जोड़ा गया!</b>\n\n📌 <b>Name:</b> {name}\n🛒 <b>Site:</b> {site}\n"
               "🔗 <b>URL:</b> {url}\n\nStock में वापस आते ही मैं आपको बता दूँगा!"),
        "hinglish": ("🎉 <b>Product add ho gaya!</b>\n\n📌 <b>Name:</b> {name}\n🛒 <b>Site:</b> {site}\n"
                     "🔗 <b>URL:</b> {url}\n\nStock me wapas aate hi main aapko bata dunga!"),
    },
    "cancelled": {
        "en": "❌ Cancelled.",
        "hi": "❌ रद्द कर दिया।",
        "hinglish": "❌ Cancel kar diya.",
    },

    # ── List / remove / check / search / stores empties+headers ──────────────
    "list_empty": {
        "en": "📭 You have no tracked products yet.\nUse /add to start tracking one!",
        "hi": "📭 अभी आपके कोई tracked products नहीं हैं।\nTrack करना शुरू करने के लिए /add इस्तेमाल करें!",
        "hinglish": "📭 Abhi aapke koi tracked products nahi hain.\nTrack karna shuru karne ke liye /add use karo!",
    },
    "list_header": {
        "en": "📋 <b>Your Tracked Products</b>\n",
        "hi": "📋 <b>आपके Tracked Products</b>\n",
        "hinglish": "📋 <b>Aapke Tracked Products</b>\n",
    },
    "remove_empty": {
        "en": "📭 You have no products to remove.\nUse /add to start tracking one!",
        "hi": "📭 हटाने के लिए आपके कोई products नहीं हैं।\nTrack करने के लिए /add इस्तेमाल करें!",
        "hinglish": "📭 Hataane ke liye aapke koi products nahi hain.\nTrack karne ke liye /add use karo!",
    },
    "remove_prompt": {
        "en": "🗑 <b>Select a product to remove:</b>",
        "hi": "🗑 <b>हटाने के लिए कोई product चुनें:</b>",
        "hinglish": "🗑 <b>Hataane ke liye koi product choose karo:</b>",
    },
    "check_empty": {
        "en": "📭 You have no tracked products yet.\nUse /add to start tracking one!",
        "hi": "📭 अभी आपके कोई tracked products नहीं हैं।\nTrack करने के लिए /add इस्तेमाल करें!",
        "hinglish": "📭 Abhi aapke koi tracked products nahi hain.\nTrack karne ke liye /add use karo!",
    },
    "check_filter_prompt": {
        "en": "🏪 <b>Filter by store</b>\n\nPick a store to check, or check all at once:",
        "hi": "🏪 <b>Store से filter करें</b>\n\nCheck करने के लिए कोई store चुनें, या सब एक साथ check करें:",
        "hinglish": "🏪 <b>Store se filter karo</b>\n\nCheck karne ke liye koi store choose karo, ya sab ek saath check karo:",
    },
    "search_empty": {
        "en": "📭 You have no tracked products to search.\nUse /add to start tracking one!",
        "hi": "📭 खोजने के लिए आपके कोई tracked products नहीं हैं।\nTrack करने के लिए /add इस्तेमाल करें!",
        "hinglish": "📭 Search karne ke liye aapke koi tracked products nahi hain.\nTrack karne ke liye /add use karo!",
    },
    "stores_intro": {
        "en": "🏪 <b>Supported Stores</b>\n\nWe currently support tracking on these stores:\n",
        "hi": "🏪 <b>Supported Stores</b>\n\nअभी हम इन stores पर tracking support करते हैं:\n",
        "hinglish": "🏪 <b>Supported Stores</b>\n\nAbhi hum in stores pe tracking support karte hain:\n",
    },
    "coming_soon_croma": {
        "en": ("🚧 <b>Croma tracking is temporarily unavailable.</b>\n\n"
               "We found reliability issues with Croma stock detection and pulled it "
               "while we fix them, rather than risk sending you wrong alerts. "
               "Check back soon, or track this product on another supported store in the meantime."),
        "hi": ("🚧 <b>Croma tracking अभी temporarily unavailable है।</b>\n\n"
               "Croma के stock detection में कुछ reliability issues मिले, इसलिए गलत alerts भेजने के risk "
               "से बचने के लिए हमने इसे फिलहाल हटा दिया है। जल्द वापस check करें, या तब तक किसी और "
               "supported store पर ये product track करें।"),
        "hinglish": ("🚧 <b>Croma tracking abhi temporarily unavailable hai.</b>\n\n"
                     "Croma ke stock detection me kuch reliability issues mile, isliye galat alerts bhejne ke "
                     "risk se bachne ke liye humne ise filhaal hata diya hai. Jald wapas check karo, ya tab tak "
                     "kisi aur supported store pe ye product track karo."),
    },

    # ── /language ────────────────────────────────────────────────────────────
    "language_prompt": {
        "en": "🌐 <b>Choose your language</b>\nAffects the main messages you see — commands stay the same.",
        "hi": "🌐 <b>अपनी भाषा चुनें</b>\nइससे आपके main messages बदलेंगे — commands वही रहेंगे।",
        "hinglish": "🌐 <b>Apni language choose karo</b>\nIsse aapke main messages badlenge — commands wahi rahenge.",
    },
    "language_set": {
        "en": "✅ Language set to <b>English</b>.",
        "hi": "✅ भाषा <b>हिंदी</b> set कर दी गई है।",
        "hinglish": "✅ Language <b>Hinglish</b> set kar di gayi hai.",
    },
    "language_welcome_prompt": {
        "en": "👋 Welcome! First, choose your language:",
        "hi": "👋 स्वागत है! पहले, अपनी भाषा चुनें:",
        "hinglish": "👋 Welcome! Pehle, apni language choose karo:",
    },
}
