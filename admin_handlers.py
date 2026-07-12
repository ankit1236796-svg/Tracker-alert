"""
admin_handlers.py
~~~~~~~~~~~~~~~~~~
Admin-only commands: plan management, manual payment approval, user
visibility, and moderation. Registered as a separate Router filtered to
ADMIN_USER_ID only — non-admin users' messages never match any handler here,
so these commands are functionally invisible to them (and additionally kept
out of Telegram's own "/" menu for everyone but the admin — see
register_admin_commands in bot.py).
"""

import asyncio
import logging
from calendar import monthrange
from collections import Counter
from datetime import datetime

import httpx
from aiogram import Router, F
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from bs4 import BeautifulSoup

from access import compute_access, STATUS_TRIAL, STATUS_ACTIVE, STATUS_EXPIRED_GRACE, STATUS_LOCKED
from checkers import build_scraper_url, HEADERS
from config import ADMIN_USER_ID, REMINDER_HOURS_BEFORE_EXPIRY
from database import (
    IST,
    now_ist_str,
    add_plan,
    list_plans,
    get_plan_by_id,
    edit_plan,
    delete_plan,
    get_user,
    list_all_users,
    set_user_plan,
    grant_access,
    reject_user,
    extend_access,
    set_blocked,
    get_approval_history,
    get_approvals_since,
    list_products,
    get_all_products,
    list_pending_whatsapp_channels,
    approve_whatsapp_channel,
    disable_whatsapp_channel,
    get_whatsapp_channel,
    set_whatsapp_group_name,
)
import whatsapp_client
from notifications import (
    send_approval_notice,
    send_rejection_notice,
    send_block_notice,
    send_unblock_notice,
)

logger = logging.getLogger(__name__)
router = Router()
router.message.filter(F.from_user.id == ADMIN_USER_ID)

_STATUS_LABEL = {
    STATUS_TRIAL: "🟢 Trial",
    STATUS_ACTIVE: "🟢 Active",
    STATUS_EXPIRED_GRACE: "🟡 Expired (grace)",
    STATUS_LOCKED: "🔴 Locked",
}


def _fmt_days(days: float | None) -> str:
    if days is None:
        return "n/a"
    if days >= 0:
        return f"{days:.1f}d left"
    return f"{abs(days):.1f}d ago"


def _display_name(user_row: dict) -> str:
    uname = user_row.get("username")
    fname = user_row.get("first_name")
    if uname:
        return f"@{uname}"
    if fname:
        return fname
    return str(user_row["user_id"])


# ---------------------------------------------------------------------------
# Plans: /addplan /editplan /listplans /deleteplan
# ---------------------------------------------------------------------------

@router.message(Command("addplan"))
async def cmd_addplan(message: Message, command: CommandObject):
    if not command.args:
        await message.answer(
            "Usage: <code>/addplan &lt;name&gt; &lt;price&gt; &lt;max_items&gt; &lt;sites&gt;</code>\n"
            "sites = <code>all</code> or a comma-separated list, e.g. <code>amazon,flipkart</code>\n"
            "(name must be a single word — no spaces)",
            parse_mode="HTML",
        )
        return
    parts = command.args.split(maxsplit=3)
    if len(parts) != 4:
        await message.answer("⚠️ Need exactly 4 args: name price max_items sites.")
        return
    name, price_raw, max_items_raw, sites = parts
    try:
        price = float(price_raw)
        max_items = int(max_items_raw)
    except ValueError:
        await message.answer("⚠️ price must be a number and max_items must be an integer.")
        return

    ok, msg = add_plan(name, price, max_items, sites)
    await message.answer(("✅ " if ok else "⚠️ ") + msg)


@router.message(Command("editplan"))
async def cmd_editplan(message: Message, command: CommandObject):
    if not command.args:
        await message.answer(
            "Usage: <code>/editplan &lt;plan_id&gt; &lt;field&gt; &lt;value&gt;</code>\n"
            "Fields: name, price, max_items, sites, is_trial_plan, is_active",
            parse_mode="HTML",
        )
        return
    parts = command.args.split(maxsplit=2)
    if len(parts) != 3:
        await message.answer("⚠️ Need exactly 3 args: plan_id field value.")
        return
    plan_id_raw, field, value = parts
    try:
        plan_id = int(plan_id_raw)
    except ValueError:
        await message.answer("⚠️ plan_id must be an integer.")
        return

    ok, msg = edit_plan(plan_id, field, value)
    await message.answer(("✅ " if ok else "⚠️ ") + msg)


@router.message(Command("listplans"))
async def cmd_listplans(message: Message):
    plans = list_plans()
    if not plans:
        await message.answer("No plans configured yet. Use /addplan to create one.")
        return
    lines = ["📋 <b>Plans</b>\n"]
    for p in plans:
        flags = []
        if p["is_trial_plan"]:
            flags.append("TRIAL DEFAULT")
        if not p["is_active"]:
            flags.append("INACTIVE")
        flag_str = f" [{', '.join(flags)}]" if flags else ""
        lines.append(
            f"🆔 <code>{p['id']}</code> — <b>{p['name']}</b>{flag_str}\n"
            f"   ₹{p['price']:,.0f}/mo · {p['max_items']} items · sites: {p['sites']}\n"
        )
    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(Command("deleteplan"))
async def cmd_deleteplan(message: Message, command: CommandObject):
    if not command.args or not command.args.strip().isdigit():
        await message.answer("Usage: <code>/deleteplan &lt;plan_id&gt;</code>", parse_mode="HTML")
        return
    plan_id = int(command.args.strip())
    ok, msg = delete_plan(plan_id)
    await message.answer(("✅ " if ok else "⚠️ ") + msg)


@router.message(Command("setuserplan"))
async def cmd_setuserplan(message: Message, command: CommandObject):
    if not command.args:
        await message.answer(
            "Usage: <code>/setuserplan &lt;user_id&gt; &lt;plan_id&gt;</code>", parse_mode="HTML"
        )
        return
    parts = command.args.split()
    if len(parts) != 2 or not all(p.lstrip("-").isdigit() for p in parts):
        await message.answer("⚠️ Both user_id and plan_id must be integers.")
        return
    user_id, plan_id = int(parts[0]), int(parts[1])

    if get_plan_by_id(plan_id) is None:
        await message.answer(f"⚠️ No plan with id {plan_id}. Check /listplans.")
        return
    if get_user(user_id) is None:
        await message.answer(f"⚠️ No user with id {user_id} has interacted with the bot yet.")
        return

    ok = set_user_plan(user_id, plan_id)
    await message.answer("✅ Plan updated." if ok else "⚠️ Could not update plan.")


# ---------------------------------------------------------------------------
# Manual payment approval: /approve /reject /extend /block /unblock
# ---------------------------------------------------------------------------

@router.message(Command("approve"))
async def cmd_approve(message: Message, command: CommandObject):
    if not command.args:
        await message.answer(
            "Usage: <code>/approve &lt;user_id&gt; &lt;plan_id&gt; &lt;days&gt;</code>\n"
            "Running this again on an already-active user ADDS days to their "
            "current expiry (stacks), it doesn't reset it.",
            parse_mode="HTML",
        )
        return
    parts = command.args.split()
    if len(parts) != 3 or not all(p.lstrip("-").isdigit() for p in parts):
        await message.answer("⚠️ user_id, plan_id, and days must all be integers.")
        return
    user_id, plan_id, days = int(parts[0]), int(parts[1]), int(parts[2])
    if days <= 0:
        await message.answer("⚠️ days must be positive.")
        return

    plan = get_plan_by_id(plan_id)
    if plan is None:
        await message.answer(f"⚠️ No plan with id {plan_id}. Check /listplans.")
        return

    updated = grant_access(user_id, plan_id, days, admin_id=message.from_user.id)
    await message.answer(
        f"✅ Approved user <code>{user_id}</code> on <b>{plan['name']}</b> "
        f"(+{days} day(s)) → access until <b>{updated['access_until']}</b>",
        parse_mode="HTML",
    )
    await send_approval_notice(message.bot, user_id, plan["name"], days, updated["access_until"])


@router.message(Command("reject"))
async def cmd_reject(message: Message, command: CommandObject):
    if not command.args or not command.args.split()[0].lstrip("-").isdigit():
        await message.answer(
            "Usage: <code>/reject &lt;user_id&gt; [reason]</code>", parse_mode="HTML"
        )
        return
    parts = command.args.split(maxsplit=1)
    user_id = int(parts[0])
    reason = parts[1] if len(parts) > 1 else None

    ok = reject_user(user_id, admin_id=message.from_user.id, reason=reason)
    if not ok:
        await message.answer(f"⚠️ No user with id {user_id} has interacted with the bot yet.")
        return
    await message.answer(f"❌ Rejected user <code>{user_id}</code>.", parse_mode="HTML")
    await send_rejection_notice(message.bot, user_id, reason)


@router.message(Command("extend"))
async def cmd_extend(message: Message, command: CommandObject):
    if not command.args:
        await message.answer("Usage: <code>/extend &lt;user_id&gt; &lt;days&gt;</code>", parse_mode="HTML")
        return
    parts = command.args.split()
    if len(parts) != 2 or not all(p.lstrip("-").isdigit() for p in parts):
        await message.answer("⚠️ user_id and days must both be integers.")
        return
    user_id, days = int(parts[0]), int(parts[1])
    if days <= 0:
        await message.answer("⚠️ days must be positive.")
        return

    updated = extend_access(user_id, days, admin_id=message.from_user.id)
    if updated is None:
        await message.answer(f"⚠️ No user with id {user_id} has interacted with the bot yet.")
        return
    plan = get_plan_by_id(updated["plan_id"]) if updated["plan_id"] else None
    plan_name = plan["name"] if plan else "(no plan)"
    await message.answer(
        f"✅ Extended user <code>{user_id}</code> by {days} day(s) → "
        f"access until <b>{updated['access_until']}</b>",
        parse_mode="HTML",
    )
    await send_approval_notice(message.bot, user_id, plan_name, days, updated["access_until"])


@router.message(Command("block"))
async def cmd_block(message: Message, command: CommandObject):
    if not command.args or not command.args.strip().lstrip("-").isdigit():
        await message.answer("Usage: <code>/block &lt;user_id&gt;</code>", parse_mode="HTML")
        return
    user_id = int(command.args.strip())
    ok = set_blocked(user_id, True, admin_id=message.from_user.id)
    if not ok:
        await message.answer(f"⚠️ No user with id {user_id} has interacted with the bot yet.")
        return
    await message.answer(f"🚫 Blocked user <code>{user_id}</code>.", parse_mode="HTML")
    await send_block_notice(message.bot, user_id)


@router.message(Command("unblock"))
async def cmd_unblock(message: Message, command: CommandObject):
    if not command.args or not command.args.strip().lstrip("-").isdigit():
        await message.answer("Usage: <code>/unblock &lt;user_id&gt;</code>", parse_mode="HTML")
        return
    user_id = int(command.args.strip())
    ok = set_blocked(user_id, False, admin_id=message.from_user.id)
    if not ok:
        await message.answer(f"⚠️ No user with id {user_id} has interacted with the bot yet.")
        return
    await message.answer(f"✅ Unblocked user <code>{user_id}</code>.", parse_mode="HTML")
    await send_unblock_notice(message.bot, user_id)


# ---------------------------------------------------------------------------
# Visibility: /pending /users /finduser /stats
# ---------------------------------------------------------------------------

@router.message(Command("pending"))
async def cmd_pending(message: Message):
    users = list_all_users()
    rows = []
    for u in users:
        if u["blocked"]:
            continue  # deliberately blocked, not "pending"
        info = compute_access(u)
        if info.status in (STATUS_TRIAL, STATUS_EXPIRED_GRACE):
            rows.append((u, info, False))
        elif u.get("share_trial_requested"):
            # Completed the 5-round WhatsApp-share cycle but has no access yet
            # (status is usually LOCKED — they never had access before) —
            # surfaced here so the admin can /approve or /reject it like any
            # other pending request.
            rows.append((u, info, True))

    if not rows:
        await message.answer("📭 No users currently in trial or awaiting approval.")
        return

    lines = [f"⏳ <b>Pending ({len(rows)})</b>\n"]
    for u, info, via_share in rows:
        if via_share:
            label = "Trial requested (via share)"
        else:
            label = "Trial" if info.status == STATUS_TRIAL else "Awaiting approval (in grace)"
        lines.append(
            f"👤 <code>{u['user_id']}</code> {_display_name(u)} — {label}\n"
            f"   {_fmt_days(info.days_remaining)}"
            + (f", grace {_fmt_days(info.grace_days_remaining)}" if info.grace_days_remaining else "")
        )
    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(Command("users"))
async def cmd_users(message: Message):
    users = list_all_users()
    if not users:
        await message.answer("📭 No users yet.")
        return

    lines = [f"👥 <b>All Users ({len(users)})</b>\n"]
    for u in users:
        info = compute_access(u)
        item_count = len(list_products(u["user_id"]))
        plan_name = info.plan["name"] if info.plan else "—"
        lines.append(
            f"{_STATUS_LABEL[info.status]} <code>{u['user_id']}</code> {_display_name(u)}\n"
            f"   Plan: {plan_name} · {_fmt_days(info.days_remaining)} · {item_count} item(s)"
        )
    text = "\n".join(lines)
    # Telegram messages cap at 4096 chars — chunk if the user base is large.
    for i in range(0, len(text), 3800):
        await message.answer(text[i:i + 3800], parse_mode="HTML")


@router.message(Command("finduser"))
async def cmd_finduser(message: Message, command: CommandObject):
    if not command.args or not command.args.strip().lstrip("-").isdigit():
        await message.answer("Usage: <code>/finduser &lt;telegram_id&gt;</code>", parse_mode="HTML")
        return
    user_id = int(command.args.strip())
    u = get_user(user_id)
    if u is None:
        await message.answer(f"⚠️ No user with id {user_id} has interacted with the bot yet.")
        return

    info = compute_access(u)
    products = list_products(user_id)
    history = get_approval_history(user_id)

    lines = [
        f"👤 <b>User {user_id}</b> {_display_name(u)}\n",
        f"📅 Joined: {u['created_at']}",
        f"📦 Plan: {info.plan['name'] if info.plan else '—'}",
        f"📊 Status: {_STATUS_LABEL[info.status]} ({_fmt_days(info.days_remaining)})",
        f"🚫 Blocked: {'yes' if u['blocked'] else 'no'}",
        f"🎁 WhatsApp-share trial used: {'yes' if u.get('share_trial_used') else 'no'}",
        f"\n🛒 <b>Tracked items ({len(products)}):</b>",
    ]
    if products:
        for p in products[:20]:
            lines.append(f"  • {p['name']} [{p['site'].capitalize()}]")
        if len(products) > 20:
            lines.append(f"  … and {len(products) - 20} more")
    else:
        lines.append("  (none)")

    lines.append(f"\n📜 <b>History ({len(history)}):</b>")
    if history:
        for h in history[:15]:
            detail = f"{h['days']}d" if h["days"] else ""
            if h["amount"]:
                detail += f" ₹{h['amount']:,.0f}"
            if h["reason"]:
                detail += f" — {h['reason']}"
            lines.append(f"  • {h['created_at']} — {h['action']}{(' ' + detail) if detail else ''}")
        if len(history) > 15:
            lines.append(f"  … and {len(history) - 15} more")
    else:
        lines.append("  (none)")

    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(Command("stats"))
async def cmd_stats(message: Message):
    users = list_all_users()
    counts = Counter()
    for u in users:
        info = compute_access(u)
        counts[info.status] += 1

    now = datetime.now(IST)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).strftime("%Y-%m-%d %H:%M:%S")
    revenue = sum(a["amount"] or 0 for a in get_approvals_since(month_start))

    all_products = get_all_products()
    site_counts = Counter(p["site"] for p in all_products)
    top_store = site_counts.most_common(1)
    top_store_str = f"{top_store[0][0]} ({top_store[0][1]} items)" if top_store else "n/a"

    await message.answer(
        "📊 <b>Stats</b>\n\n"
        f"👥 Total users: <b>{len(users)}</b>\n"
        f"🟢 Active (paid): <b>{counts[STATUS_ACTIVE]}</b>\n"
        f"🟢 In trial: <b>{counts[STATUS_TRIAL]}</b>\n"
        f"🟡 Expired (grace): <b>{counts[STATUS_EXPIRED_GRACE]}</b>\n"
        f"🔴 Locked: <b>{counts[STATUS_LOCKED]}</b>\n\n"
        f"💰 Revenue this month: <b>₹{revenue:,.0f}</b>\n"
        f"🏪 Most-tracked store: <b>{top_store_str}</b>\n"
        f"📦 Total tracked items: <b>{len(all_products)}</b>",
        parse_mode="HTML",
    )


# ---------------------------------------------------------------------------
# /broadcast
# ---------------------------------------------------------------------------

@router.message(Command("broadcast"))
async def cmd_broadcast(message: Message, command: CommandObject):
    if not command.args:
        await message.answer("Usage: <code>/broadcast &lt;message&gt;</code>", parse_mode="HTML")
        return
    text = command.args.strip()

    users = list_all_users()
    targets = [u["user_id"] for u in users if compute_access(u).has_access]
    if not targets:
        await message.answer("📭 No active users to broadcast to.")
        return

    await message.answer(f"📣 Broadcasting to {len(targets)} active user(s)…")
    sent, failed = 0, 0
    for uid in targets:
        try:
            await message.bot.send_message(chat_id=uid, text=f"📣 {text}", parse_mode="HTML")
            sent += 1
        except Exception as exc:
            logger.error(f"[broadcast] failed to message {uid}: {exc}")
            failed += 1
        await asyncio.sleep(0.05)  # stay well under Telegram's rate limits

    await message.answer(f"✅ Broadcast done — sent {sent}, failed {failed}.")


# ---------------------------------------------------------------------------
# WhatsApp channel forwarding: /whatsapppending /whatsappapprove /whatsappdisable
# Per-user, admin-approved (see database.whatsapp_channels + handlers.py's
# /setwhatsapp). The admin must manually join a user's Channel/Community with
# their own phone BEFORE approving here — approval here is purely a DB status
# flip, it does not join anything on the admin's behalf.
# ---------------------------------------------------------------------------

@router.message(Command("whatsapppending"))
async def cmd_whatsapppending(message: Message):
    rows = list_pending_whatsapp_channels()
    if not rows:
        await message.answer("📭 No WhatsApp channel registrations awaiting approval.")
        return

    lines = [f"⏳ <b>Pending WhatsApp channels ({len(rows)})</b>\n"]
    for row in rows:
        u = get_user(row["user_id"])
        lines.append(
            f"👤 <code>{row['user_id']}</code> {_display_name(u) if u else ''}\n"
            f"   {row['invite_link']}\n"
            f"   registered {row['registered_at']}"
        )
    lines.append(
        "\nUse <code>/whatsappapprove &lt;user_id&gt;</code> after joining their "
        "Channel/Community, or <code>/whatsappdisable &lt;user_id&gt;</code> to reject."
    )
    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(Command("whatsappapprove"))
async def cmd_whatsappapprove(message: Message, command: CommandObject):
    if not command.args or not command.args.strip().lstrip("-").isdigit():
        await message.answer("Usage: <code>/whatsappapprove &lt;user_id&gt;</code>", parse_mode="HTML")
        return
    user_id = int(command.args.strip())
    ok = approve_whatsapp_channel(user_id, message.from_user.id)
    if not ok:
        await message.answer(f"⚠️ No WhatsApp channel registration found for user {user_id}.")
        return

    reply = f"✅ WhatsApp channel for user {user_id} approved — alerts will now forward there."
    channel = get_whatsapp_channel(user_id)
    # Best-effort: fetch the group/channel's display name (once) so future
    # forwards can open it via WhatsApp Web's sidebar search instead of
    # always navigating the invite link fresh — see whatsapp_client.py and
    # whatsapp_forwarder/main.py. This involves a real page load on the
    # forwarder side and can take up to ~35s; approval has already
    # succeeded above regardless of how this turns out.
    if channel and not channel.get("group_name"):
        name = await whatsapp_client.resolve_group_name(channel["invite_link"])
        if name:
            set_whatsapp_group_name(user_id, name)
            reply += f"\n📛 Group name resolved: {name!r} (enables faster sidebar-based delivery)."
        else:
            reply += (
                "\n⚠️ Couldn't auto-resolve the group name (forwarder unreachable, "
                "not logged in, or the page didn't load) — forwarding still works "
                "via the invite link, just without the sidebar-search shortcut."
            )
    await message.answer(reply)


@router.message(Command("whatsappdisable"))
async def cmd_whatsappdisable(message: Message, command: CommandObject):
    if not command.args or not command.args.strip().lstrip("-").isdigit():
        await message.answer("Usage: <code>/whatsappdisable &lt;user_id&gt;</code>", parse_mode="HTML")
        return
    user_id = int(command.args.strip())
    ok = disable_whatsapp_channel(user_id)
    if ok:
        await message.answer(f"🚫 WhatsApp channel forwarding for user {user_id} disabled.")
    else:
        await message.answer(f"⚠️ No WhatsApp channel registration found for user {user_id}.")


# ---------------------------------------------------------------------------
# TEMPORARY debug commands (oneplus + reliancedigital) for tuning checker
# logic against real product pages. NOT wired into CHECKER_MAP or the
# regular check cycle.
# ---------------------------------------------------------------------------


async def _debug_send(message: Message, text: str) -> None:
    """Send debug-command output as plain text (parse_mode=None), never the
    bot's default HTML parse mode — the extracted page text and the URLs
    these commands echo back can contain <, >, & (e.g. any query string
    with an "&", or literal "<script" in scraped text), which Telegram's
    HTML entity parser rejects with TelegramBadRequest: can't parse
    entities, previously failing the whole command with no visible error.
    Wrapped in try/except so a send failure for any OTHER reason (message
    too long despite chunking, a transient API error, etc.) is reported to
    the admin instead of the command just going silent."""
    try:
        await message.answer(text, parse_mode=None)
    except Exception as exc:
        logger.error(f"[debug] failed to send a debug output message: {exc}")
        try:
            await message.answer(f"⚠️ Failed to send a debug output message: {exc}", parse_mode=None)
        except Exception:
            pass  # nothing more we can do — already logged above


# /debugoneplus: fetches on demand via the same render=true Scrape.do path
# stock_checker.py uses, dumps the resulting visible text back to the
# admin, and does nothing else. Safe to delete once no longer needed.
_DEBUG_ONEPLUS_ADMIN_ID = 5004721766  # hardcoded on top of the router's own
# ADMIN_USER_ID filter — this fetches an arbitrary caller-supplied URL via
# Scrape.do (spends credits) and is meant for one specific admin's own
# debugging, not general admin use.

# OnePlus product pages render incompletely with a plain render=true —
# earlier /debugoneplus output showed the "Priority Delivery" text missing
# even on pages that should have it. waitUntil="networkidle0" (Scrape.do's
# Puppeteer-backed wait condition) waits for the page's JS/XHR activity to
# settle before capturing content; customWait is a fixed extra buffer (ms)
# on top of that, for any DOM mutation that keeps happening briefly after
# the network itself goes idle. Scoped to this debug command only —
# build_scraper_url's new wait_until/custom_wait_ms params are opt-in and
# untouched by every other call site (the live check cycle's OnePlus
# fetches are unaffected).
_DEBUG_ONEPLUS_WAIT_UNTIL = "networkidle0"
_DEBUG_ONEPLUS_CUSTOM_WAIT_MS = 4000


@router.message(Command("debugoneplus"))
async def cmd_debugoneplus(message: Message, command: CommandObject):
    if message.from_user.id != _DEBUG_ONEPLUS_ADMIN_ID:
        return
    if not command.args:
        await message.answer("Usage: <code>/debugoneplus &lt;url&gt;</code>", parse_mode="HTML")
        return

    url = command.args.strip()
    await _debug_send(
        message,
        f"🔍 Fetching (render=true, waitUntil={_DEBUG_ONEPLUS_WAIT_UNTIL}, "
        f"customWait={_DEBUG_ONEPLUS_CUSTOM_WAIT_MS}ms): {url}",
    )

    try:
        scraper_url = build_scraper_url(
            url,
            render_js=True,
            wait_until=_DEBUG_ONEPLUS_WAIT_UNTIL,
            custom_wait_ms=_DEBUG_ONEPLUS_CUSTOM_WAIT_MS,
        )
        async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=60.0) as client:
            resp = await client.get(scraper_url)
            resp.raise_for_status()
        html = resp.text
    except Exception as exc:
        await _debug_send(message, f"⚠️ Fetch failed: {exc}")
        return

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    visible_text = soup.get_text(" ", strip=True)
    snippet = visible_text[:3000]

    await _debug_send(
        message, f"📄 Visible text: {len(visible_text)} chars total, showing first {len(snippet)}."
    )
    # 3000 chars always fits in one Telegram message (limit 4096), but chunk
    # defensively anyway in case the snippet length above is ever changed.
    _CHUNK_SIZE = 4000
    for i in range(0, len(snippet), _CHUNK_SIZE):
        await _debug_send(message, snippet[i:i + _CHUNK_SIZE])


# ---------------------------------------------------------------------------
# TEMPORARY debug command for tuning checkers/reliancedigital.py against
# real product pages — same admin restriction as /debugoneplus above. NOT
# wired into CHECKER_MAP or the regular check cycle — RelianceDigital's live
# check_stock fetch is completely untouched by this. Safe to delete once no
# longer needed.
#
# Runs TWO isolated trials (each its own Scrape.do request) to separate two
# competing hypotheses for why RelianceDigital pages show an anti-bot/stale-
# cache wall: a rendering-timing issue (fixed by waiting longer) vs. a
# proxy/IP-reputation issue (fixed by a better proxy pool). Each trial is
# reported independently — see _run_debug_reliance_trial — so one failing
# (e.g. super=true not being available on the current plan) never prevents
# the other from running or being reported.
# ---------------------------------------------------------------------------
_DEBUG_RELIANCE_ADMIN_ID = 5004721766  # same hardcoded restriction as
# /debugoneplus, on top of the router's own ADMIN_USER_ID filter — this
# fetches an arbitrary caller-supplied URL via Scrape.do (spends credits).

# The literal string RelianceDigital shows on what looks like an anti-bot/
# stale-cache interstitial. Checked against both the raw HTML (full
# response text, script/style included) and the visible text (script/style
# stripped) so the report can distinguish "present in the actual
# rendered/static page" from "only exists inside a <script>/<style> tag" —
# the latter means it's client-side-JS-injected content, not something
# that's actually shown/rendered on the page as fetched.
_RELIANCE_ANTIBOT_PHRASE = "Please Update the Page in Theme"


async def _run_debug_reliance_trial(message: Message, label: str, url: str, **scraper_kwargs) -> None:
    """Fetch `url` via Scrape.do with the given build_scraper_url() kwargs
    and send a labeled diagnostic report: HTTP status, raw HTML length,
    where _RELIANCE_ANTIBOT_PHRASE was (or wasn't) found, then the first
    3000 chars of visible text chunked under Telegram's 4096-char limit.
    Any failure here is reported under this trial's own label and does not
    raise — the caller runs each trial independently."""
    await _debug_send(message, f"— {label} —")

    try:
        scraper_url = build_scraper_url(url, **scraper_kwargs)
        async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=60.0) as client:
            resp = await client.get(scraper_url)
        status_code = resp.status_code
        html = resp.text
    except Exception as exc:
        await _debug_send(message, f"⚠️ [{label}] Fetch failed: {exc}")
        return

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    visible_text = soup.get_text(" ", strip=True)

    phrase_lower = _RELIANCE_ANTIBOT_PHRASE.lower()
    in_raw = phrase_lower in html.lower()
    in_visible = phrase_lower in visible_text.lower()
    if not in_raw:
        phrase_diag = f"{_RELIANCE_ANTIBOT_PHRASE!r} not found anywhere in the raw HTML."
    elif in_visible:
        phrase_diag = (
            f"{_RELIANCE_ANTIBOT_PHRASE!r} found in the VISIBLE text — present in the "
            f"actual rendered/static page, not just inside a <script>/<style> tag."
        )
    else:
        phrase_diag = (
            f"{_RELIANCE_ANTIBOT_PHRASE!r} found in the raw HTML but ONLY inside a "
            f"stripped <script>/<style> tag — likely injected/rendered by client-side "
            f"JS, not present as static/visible text as fetched."
        )

    await _debug_send(
        message, f"[{label}] HTTP {status_code} | raw HTML length: {len(html)} chars\n{phrase_diag}"
    )

    snippet = visible_text[:3000]
    await _debug_send(
        message, f"[{label}] visible text: {len(visible_text)} chars total, showing first {len(snippet)}."
    )
    _CHUNK_SIZE = 4000
    for i in range(0, len(snippet), _CHUNK_SIZE):
        await _debug_send(message, snippet[i:i + _CHUNK_SIZE])


@router.message(Command("debugreliance"))
async def cmd_debugreliance(message: Message, command: CommandObject):
    if message.from_user.id != _DEBUG_RELIANCE_ADMIN_ID:
        return
    if not command.args:
        await message.answer("Usage: <code>/debugreliance &lt;url&gt;</code>", parse_mode="HTML")
        return

    url = command.args.strip()
    await _debug_send(message, f"🔍 Running 2 diagnostic trials for: {url}")

    await _run_debug_reliance_trial(
        message, "Trial A: render=true only (no waitUntil/customWait)", url,
        render_js=True,
    )
    await _run_debug_reliance_trial(
        message, "Trial B: super=true (premium proxy)", url,
        super_proxy=True,
    )
