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
import json
import logging
import re
import time
from calendar import monthrange
from collections import Counter
from datetime import datetime
from urllib.parse import urlparse

from aiogram import Router, F
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from bs4 import BeautifulSoup

import stock_checker
from access import compute_access, STATUS_TRIAL, STATUS_ACTIVE, STATUS_EXPIRED_GRACE, STATUS_LOCKED
from checkers import (
    fetch_page, fetch_with_502_retry, shopatsc, unicornstore, inventstore, reliancedigital, apple,
    sangeethamobiles, vijaysales, tataneu, iqoo, vivo, croma,
    CHECKER_MAP,
)
from config import (
    ADMIN_USER_ID, REMINDER_HOURS_BEFORE_EXPIRY, get_site_label, SCRAPING_PROVIDER,
    APPLE_PICKUP_PINCODES, APPLE_PICKUP_STORE_LABELS, APPLE_OFFICIAL_PICKUP_ALERTS_ENABLED,
)
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
    list_users_with_products_summary,
    bulk_stop_tracking,
    bulk_cancel_plan,
    list_tracked_links_by_store,
    get_zyte_usage_summary,
    get_service_pause_info,
    set_service_paused,
    list_paused_user_ids,
    set_users_checks_paused,
)
import whatsapp_client
import zyte_client
from states import AdminBulkStates
from notifications import (
    send_approval_notice,
    send_rejection_notice,
    send_block_notice,
    send_unblock_notice,
    send_plan_cancelled_notice,
    send_items_removed_notice,
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


def _notify_prompt_keyboard(action: str, user_id: int) -> InlineKeyboardMarkup:
    """'Notify user?' Yes/No keyboard shown after /block or /unblock runs with
    no silent/notify flag. `action` is 'block' or 'unblock', encoded into the
    callback_data alongside the user_id so a single handler (callback_
    blocknotify below) covers both. Only used by /block and /unblock — the
    action (locking/unlocking the user) has ALREADY executed by the time this
    is shown; tapping a button here only decides whether to also message the
    user, never whether to lock/unlock them."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Yes", callback_data=f"blocknotify:{action}:{user_id}:yes"),
        InlineKeyboardButton(text="❌ No", callback_data=f"blocknotify:{action}:{user_id}:no"),
    ]])


_BLOCK_USAGE = "Usage: <code>/block &lt;user_id&gt; [silent|notify]</code>"
_UNBLOCK_USAGE = "Usage: <code>/unblock &lt;user_id&gt; [silent|notify]</code>"


@router.message(Command("block"))
async def cmd_block(message: Message, command: CommandObject):
    if not command.args:
        await message.answer(_BLOCK_USAGE, parse_mode="HTML")
        return
    parts = command.args.split()
    flag = parts[1].lower() if len(parts) > 1 else None
    if not parts[0].lstrip("-").isdigit() or len(parts) > 2 or flag not in (None, "silent", "notify"):
        await message.answer(_BLOCK_USAGE, parse_mode="HTML")
        return
    user_id = int(parts[0])

    # The lock itself ALWAYS executes, regardless of the notify choice below —
    # only whether a message is sent to the user is conditional.
    ok = set_blocked(user_id, True, admin_id=message.from_user.id)
    if not ok:
        await message.answer(f"⚠️ No user with id {user_id} has interacted with the bot yet.")
        return

    if flag == "silent":
        await message.answer(f"🚫 Blocked user <code>{user_id}</code> (not notified).", parse_mode="HTML")
        return
    if flag == "notify":
        await message.answer(f"🚫 Blocked user <code>{user_id}</code>.", parse_mode="HTML")
        await send_block_notice(message.bot, user_id)
        return
    await message.answer(
        f"🚫 Blocked user <code>{user_id}</code>.\n\nNotify them?",
        parse_mode="HTML",
        reply_markup=_notify_prompt_keyboard("block", user_id),
    )


@router.message(Command("unblock"))
async def cmd_unblock(message: Message, command: CommandObject):
    if not command.args:
        await message.answer(_UNBLOCK_USAGE, parse_mode="HTML")
        return
    parts = command.args.split()
    flag = parts[1].lower() if len(parts) > 1 else None
    if not parts[0].lstrip("-").isdigit() or len(parts) > 2 or flag not in (None, "silent", "notify"):
        await message.answer(_UNBLOCK_USAGE, parse_mode="HTML")
        return
    user_id = int(parts[0])

    ok = set_blocked(user_id, False, admin_id=message.from_user.id)
    if not ok:
        await message.answer(f"⚠️ No user with id {user_id} has interacted with the bot yet.")
        return

    if flag == "silent":
        await message.answer(f"✅ Unblocked user <code>{user_id}</code> (not notified).", parse_mode="HTML")
        return
    if flag == "notify":
        await message.answer(f"✅ Unblocked user <code>{user_id}</code>.", parse_mode="HTML")
        await send_unblock_notice(message.bot, user_id)
        return
    await message.answer(
        f"✅ Unblocked user <code>{user_id}</code>.\n\nNotify them?",
        parse_mode="HTML",
        reply_markup=_notify_prompt_keyboard("unblock", user_id),
    )


@router.callback_query(F.data.startswith("blocknotify:"))
async def callback_blocknotify(call: CallbackQuery):
    _, action, user_id_raw, choice = call.data.split(":", 3)
    user_id = int(user_id_raw)
    if choice == "yes":
        if action == "block":
            await send_block_notice(call.bot, user_id)
        else:
            await send_unblock_notice(call.bot, user_id)
        suffix = "✅ Notified."
    else:
        suffix = "🔕 Not notified."
    await call.message.edit_text(
        f"{call.message.text}\n\n{suffix}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[]),
    )
    await call.answer()


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
            lines.append(f"  • {p['name']} [{get_site_label(p['site'])}]")
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
# /managetracking — bulk "Stop Tracking" + "Stop Plan" panel.
#
# Lists every user who currently has at least one tracked product as a
# checkbox keyboard (mirroring handlers.py's /select checkbox pattern:
# sel_toggle: callbacks + a ✅/⬜ mark per row, driven by an FSM state so the
# selection survives across button taps). Two bulk actions:
#   • Stop Tracking (selected) — deletes ALL tracked products for each
#     selected user (database.bulk_stop_tracking), notifying each affected
#     user with the exact items removed (reusing the same
#     items_removed_text the dashboard's own item-removal flow sends).
#   • Stop Plan (selected) — cancels each selected user's current access
#     period (database.bulk_cancel_plan / cancel_user_plan), which ties into
#     the existing tiered plan system by simply expiring access_until now —
#     the user then flows through the exact same grace-period/locked states
#     a normal plan expiry would, rather than a separate punitive "blocked"
#     flag (see /block for that).
# Both actions show a confirmation summary (affected users + product counts
# or plan names) before anything is executed — mirroring /select's
# sel_delete_selected -> sel_confirm_delete two-step flow.
# ---------------------------------------------------------------------------

def _managetracking_keyboard(rows: list[dict], selected: set[int]) -> InlineKeyboardMarkup:
    buttons = []
    for r in rows:
        mark = "✅" if r["user_id"] in selected else "⬜"
        plan = get_plan_by_id(r["plan_id"]) if r.get("plan_id") else None
        plan_label = plan["name"] if plan else "no plan"
        buttons.append([
            InlineKeyboardButton(
                text=f"{mark} {_display_name(r)} — {r['product_count']} item(s) [{plan_label}]",
                callback_data=f"mt_toggle:{r['user_id']}",
            )
        ])
    buttons.append([
        InlineKeyboardButton(text="🗑 Stop Tracking (Selected)", callback_data="mt_stoptracking"),
    ])
    buttons.append([
        InlineKeyboardButton(text="🚫 Stop Plan (Selected)", callback_data="mt_stopplan"),
    ])
    buttons.append([
        InlineKeyboardButton(text="❌ Cancel", callback_data="mt_cancel"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


@router.message(Command("managetracking"))
async def cmd_managetracking(message: Message, state: FSMContext):
    rows = list_users_with_products_summary()
    if not rows:
        await message.answer("📭 No users currently have any tracked products.")
        return
    await state.set_state(AdminBulkStates.managing)
    await state.update_data(selected_ids=[])
    await message.answer(
        f"☑️ <b>Manage tracking ({len(rows)} user(s) with tracked items)</b>\n\n"
        "Tap to toggle ✅/⬜, then choose a bulk action:",
        parse_mode="HTML",
        reply_markup=_managetracking_keyboard(rows, set()),
    )


@router.callback_query(F.data.startswith("mt_toggle:"), AdminBulkStates.managing)
async def callback_mt_toggle(call: CallbackQuery, state: FSMContext):
    user_id = int(call.data.split(":", 1)[1])
    data = await state.get_data()
    selected = set(data.get("selected_ids", []))
    if user_id in selected:
        selected.discard(user_id)
    else:
        selected.add(user_id)
    await state.update_data(selected_ids=list(selected))
    rows = list_users_with_products_summary()
    await call.message.edit_reply_markup(reply_markup=_managetracking_keyboard(rows, selected))
    await call.answer()


@router.callback_query(F.data == "mt_stoptracking", AdminBulkStates.managing)
async def callback_mt_stoptracking(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    selected = set(data.get("selected_ids", []))
    if not selected:
        await call.answer("No users selected! Tap ⬜ to select users first.", show_alert=True)
        return
    rows = {r["user_id"]: r for r in list_users_with_products_summary()}
    total_items = sum(rows[uid]["product_count"] for uid in selected if uid in rows)
    await call.message.edit_text(
        f"⚠️ <b>Stop tracking for {len(selected)} selected user(s)?</b>\n\n"
        f"This will permanently delete {total_items} tracked item(s) total across "
        f"them and notify each affected user. This cannot be undone.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Yes, stop tracking", callback_data="mt_confirm_stoptracking"),
                InlineKeyboardButton(text="↩️ Go back", callback_data="mt_back"),
            ]
        ]),
    )
    await call.answer()


@router.callback_query(F.data == "mt_stopplan", AdminBulkStates.managing)
async def callback_mt_stopplan(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    selected = set(data.get("selected_ids", []))
    if not selected:
        await call.answer("No users selected! Tap ⬜ to select users first.", show_alert=True)
        return
    rows = {r["user_id"]: r for r in list_users_with_products_summary()}
    plan_lines = []
    for uid in selected:
        r = rows.get(uid)
        if r is None:
            continue
        plan = get_plan_by_id(r["plan_id"]) if r.get("plan_id") else None
        plan_lines.append(f"  • {_display_name(r)} — {plan['name'] if plan else 'no plan'}")
    await call.message.edit_text(
        f"⚠️ <b>Stop the plan for {len(selected)} selected user(s)?</b>\n\n"
        + "\n".join(plan_lines) +
        "\n\nTheir access will expire immediately (same grace-period flow as a "
        "normal plan expiry) and each will be notified. Their tracked items are "
        "NOT deleted by this action.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Yes, stop plan", callback_data="mt_confirm_stopplan"),
                InlineKeyboardButton(text="↩️ Go back", callback_data="mt_back"),
            ]
        ]),
    )
    await call.answer()


@router.callback_query(F.data == "mt_confirm_stoptracking", AdminBulkStates.managing)
async def callback_mt_confirm_stoptracking(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    selected = list(set(data.get("selected_ids", [])))
    removed = bulk_stop_tracking(selected)
    for uid, names in removed.items():
        await send_items_removed_notice(call.bot, uid, names)
    total_items = sum(len(v) for v in removed.values())
    await call.message.edit_text(
        f"✅ Stopped tracking for <b>{len(removed)}</b> user(s) "
        f"({total_items} item(s) deleted total) and notified them.",
        parse_mode="HTML",
    )
    await state.clear()
    await call.answer()


@router.callback_query(F.data == "mt_confirm_stopplan", AdminBulkStates.managing)
async def callback_mt_confirm_stopplan(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    selected = list(set(data.get("selected_ids", [])))
    cancelled = bulk_cancel_plan(selected, admin_id=call.from_user.id)
    for uid in cancelled:
        await send_plan_cancelled_notice(call.bot, uid)
    await call.message.edit_text(
        f"✅ Stopped the plan for <b>{len(cancelled)}</b> user(s) and notified them.",
        parse_mode="HTML",
    )
    await state.clear()
    await call.answer()


@router.callback_query(F.data == "mt_back", AdminBulkStates.managing)
async def callback_mt_back(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    selected = set(data.get("selected_ids", []))
    rows = list_users_with_products_summary()
    if not rows:
        await call.message.edit_text("📭 No users with tracked items left to manage.")
        await state.clear()
        await call.answer()
        return
    await call.message.edit_text(
        f"☑️ <b>Manage tracking ({len(rows)} user(s) with tracked items)</b>\n\n"
        "Tap to toggle ✅/⬜, then choose a bulk action:",
        parse_mode="HTML",
        reply_markup=_managetracking_keyboard(rows, selected),
    )
    await call.answer()


@router.callback_query(F.data == "mt_cancel", AdminBulkStates.managing)
async def callback_mt_cancel(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text("❌ Cancelled.")
    await call.answer()


# ---------------------------------------------------------------------------
# /pauseservice + /resumeservice — Pause/Resume Service.
#
# Two independent pause mechanisms, both checked by bot.py's
# run_stock_check_cycle:
#   • GLOBAL (database.service_status, a singleton row) — an on/off switch
#     for the ENTIRE background check cycle. When on, the cycle returns
#     immediately: no get_all_products() call, no grouping, no Scrape.do/
#     Zyte requests at all — an efficient global stop, not "iterate every
#     user and skip each one", per the feature's explicit design.
#   • PER-USER (users.checks_paused) — a secondary, separate mechanism:
#     selected users' tracked products are excluded from the cycle before
#     grouping, but every other user's items still get checked normally.
#
# Neither mode touches a paused user's saved tracked items, and NEITHER
# sends any notification to affected users — silent, admin-only visibility,
# exactly as specified. Per-user selection reuses the exact same checkbox
# pattern as /managetracking (AdminBulkStates, ✅/⬜ toggle callbacks, an
# FSM-tracked selected_ids set) — just a different FSM state
# (AdminBulkStates.pausing) and a different pair of terminal actions
# (Pause Selected / Resume Selected instead of Stop Tracking / Stop Plan).
# No confirmation step for pause/resume specifically — unlike
# stop-tracking/stop-plan, both are fully, instantly reversible, so the
# extra step would only add friction without a corresponding safety benefit.
# ---------------------------------------------------------------------------

def _service_status_text() -> str:
    info = get_service_pause_info()
    paused_user_count = len(list_paused_user_ids())
    if info["paused"]:
        status_line = f"🔴 <b>Service: PAUSED</b> (globally, since {info['paused_at']})"
    else:
        status_line = "🟢 <b>Service: RUNNING</b>"
    paused_users_line = (
        f"👤 {paused_user_count} user(s) individually paused"
        if paused_user_count else "👤 No users individually paused"
    )
    return f"{status_line}\n{paused_users_line}"


def _service_menu_keyboard(global_paused: bool) -> InlineKeyboardMarkup:
    toggle_button = (
        InlineKeyboardButton(text="▶️ Resume ALL (global)", callback_data="ps_resume_all")
        if global_paused else
        InlineKeyboardButton(text="⏸ Pause ALL (global)", callback_data="ps_pause_all")
    )
    return InlineKeyboardMarkup(inline_keyboard=[
        [toggle_button],
        [InlineKeyboardButton(text="☑️ Pause/Resume Selected Users…", callback_data="ps_select_users")],
    ])


async def _send_service_menu(message: Message) -> None:
    info = get_service_pause_info()
    await message.answer(
        _service_status_text(), parse_mode="HTML",
        reply_markup=_service_menu_keyboard(info["paused"]),
    )


@router.message(Command("pauseservice"))
async def cmd_pauseservice(message: Message):
    await _send_service_menu(message)


@router.message(Command("resumeservice"))
async def cmd_resumeservice(message: Message):
    # /resumeservice always resumes the GLOBAL switch immediately if it's
    # currently on (a dedicated one-tap "get me back to running" command),
    # then shows the same status+menu either way — including the
    # Pause/Resume Selected Users option, for resuming specific
    # individually-paused users too.
    info = get_service_pause_info()
    if info["paused"]:
        set_service_paused(False)
        await message.answer("✅ Service resumed globally.", parse_mode="HTML")
    await _send_service_menu(message)


@router.callback_query(F.data == "ps_pause_all")
async def callback_ps_pause_all(call: CallbackQuery):
    set_service_paused(True)
    await call.message.edit_text(
        _service_status_text(), parse_mode="HTML",
        reply_markup=_service_menu_keyboard(True),
    )
    await call.answer("Service paused globally.")


@router.callback_query(F.data == "ps_resume_all")
async def callback_ps_resume_all(call: CallbackQuery):
    set_service_paused(False)
    await call.message.edit_text(
        _service_status_text(), parse_mode="HTML",
        reply_markup=_service_menu_keyboard(False),
    )
    await call.answer("Service resumed globally.")


def _pauseselect_keyboard(rows: list[dict], selected: set[int]) -> InlineKeyboardMarkup:
    buttons = []
    for r in rows:
        mark = "✅" if r["user_id"] in selected else "⬜"
        pause_indicator = "🔴 PAUSED" if r.get("checks_paused") else "🟢 running"
        buttons.append([
            InlineKeyboardButton(
                text=f"{mark} {_display_name(r)} — {pause_indicator} ({r['product_count']} item(s))",
                callback_data=f"ps_toggle:{r['user_id']}",
            )
        ])
    buttons.append([
        InlineKeyboardButton(text="⏸ Pause Selected", callback_data="ps_pause_selected"),
        InlineKeyboardButton(text="▶️ Resume Selected", callback_data="ps_resume_selected"),
    ])
    buttons.append([
        InlineKeyboardButton(text="↩️ Back", callback_data="ps_back_to_menu"),
        InlineKeyboardButton(text="❌ Cancel", callback_data="ps_cancel"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(F.data == "ps_select_users")
async def callback_ps_select_users(call: CallbackQuery, state: FSMContext):
    rows = list_users_with_products_summary()
    if not rows:
        await call.answer("No users currently have any tracked products.", show_alert=True)
        return
    await state.set_state(AdminBulkStates.pausing)
    await state.update_data(selected_ids=[])
    await call.message.edit_text(
        f"☑️ <b>Pause/Resume selected users ({len(rows)} user(s) with tracked items)</b>\n\n"
        "Tap to toggle ✅/⬜, then Pause or Resume the selected users. "
        "No notification is ever sent to affected users.",
        parse_mode="HTML",
        reply_markup=_pauseselect_keyboard(rows, set()),
    )
    await call.answer()


@router.callback_query(F.data.startswith("ps_toggle:"), AdminBulkStates.pausing)
async def callback_ps_toggle(call: CallbackQuery, state: FSMContext):
    user_id = int(call.data.split(":", 1)[1])
    data = await state.get_data()
    selected = set(data.get("selected_ids", []))
    if user_id in selected:
        selected.discard(user_id)
    else:
        selected.add(user_id)
    await state.update_data(selected_ids=list(selected))
    rows = list_users_with_products_summary()
    await call.message.edit_reply_markup(reply_markup=_pauseselect_keyboard(rows, selected))
    await call.answer()


@router.callback_query(F.data == "ps_pause_selected", AdminBulkStates.pausing)
async def callback_ps_pause_selected(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    selected = list(set(data.get("selected_ids", [])))
    if not selected:
        await call.answer("No users selected! Tap ⬜ to select users first.", show_alert=True)
        return
    count = set_users_checks_paused(selected, True)
    await call.message.edit_text(
        f"⏸ Paused checking for <b>{count}</b> user(s). Their tracked items stay saved — "
        f"just not checked until resumed. No notification was sent.",
        parse_mode="HTML",
    )
    await state.clear()
    await call.answer()


@router.callback_query(F.data == "ps_resume_selected", AdminBulkStates.pausing)
async def callback_ps_resume_selected(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    selected = list(set(data.get("selected_ids", [])))
    if not selected:
        await call.answer("No users selected! Tap ⬜ to select users first.", show_alert=True)
        return
    count = set_users_checks_paused(selected, False)
    await call.message.edit_text(
        f"▶️ Resumed checking for <b>{count}</b> user(s). No notification was sent.",
        parse_mode="HTML",
    )
    await state.clear()
    await call.answer()


@router.callback_query(F.data == "ps_back_to_menu", AdminBulkStates.pausing)
async def callback_ps_back_to_menu(call: CallbackQuery, state: FSMContext):
    await state.clear()
    info = get_service_pause_info()
    await call.message.edit_text(
        _service_status_text(), parse_mode="HTML",
        reply_markup=_service_menu_keyboard(info["paused"]),
    )
    await call.answer()


@router.callback_query(F.data == "ps_cancel", AdminBulkStates.pausing)
async def callback_ps_cancel(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text("❌ Cancelled.")
    await call.answer()


# ---------------------------------------------------------------------------
# /linksbystore — read-only view of every currently-tracked product link,
# grouped by marketplace (site), with each link's display name and how many
# distinct users are tracking it. Iterates checkers.CHECKER_MAP's key order
# so every currently-supported store is represented in the same canonical
# order used everywhere else in the codebase; a site with zero tracked links
# is simply skipped (nothing to show).
# ---------------------------------------------------------------------------

@router.message(Command("linksbystore"))
async def cmd_linksbystore(message: Message):
    grouped = list_tracked_links_by_store()
    if not grouped:
        await message.answer("📭 No tracked links yet.")
        return

    lines = ["🔗 <b>Tracked links by store</b>\n"]
    # CHECKER_MAP's order first (canonical), then any leftover site not in
    # CHECKER_MAP (e.g. a retired store like Croma with lingering rows) so
    # nothing tracked is ever silently hidden.
    ordered_sites = list(CHECKER_MAP.keys()) + [
        s for s in grouped.keys() if s not in CHECKER_MAP
    ]
    for site in ordered_sites:
        links = grouped.get(site)
        if not links:
            continue
        lines.append(f"\n🏪 <b>{get_site_label(site)}</b> ({len(links)} link(s))")
        for link in links:
            lines.append(f"  • {link['name']} — {link['tracker_count']} user(s)\n    {link['url']}")

    text = "\n".join(lines)
    # Telegram messages cap at 4096 chars — chunk if the list is large.
    for i in range(0, len(text), 3800):
        await message.answer(text[i:i + 3800], parse_mode="HTML", disable_web_page_preview=True)


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
        resp = await fetch_page(
            url,
            render_js=True,
            wait_until=_DEBUG_ONEPLUS_WAIT_UNTIL,
            custom_wait_ms=_DEBUG_ONEPLUS_CUSTOM_WAIT_MS,
            timeout=60.0,
        )
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
# Previously ran two isolated trials to compare render=true alone against
# super=true (premium proxy): render=true alone consistently failed to get
# past RelianceDigital's anti-bot/stale-cache wall, so super=true is now
# the default (and only) fetch this command makes going forward.
# _run_debug_reliance_trial still takes arbitrary build_scraper_url()
# kwargs and reports under a label, in case another comparison is ever
# needed again.
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

# Substrings (case-insensitive) of JSON key names worth surfacing from any
# embedded state blob — covers the common variants sites use for a stock
# flag without needing to guess the exact key name up front.
_JSON_STATE_KEYWORDS = ("stock", "availab", "instock", "sellable", "buyable")


def _extract_balanced_json(text: str, start_idx: int) -> str | None:
    """Starting from start_idx, skip to the first '{' or '[' and return the
    substring up to its matching close, respecting string literals (so a
    brace/bracket inside a quoted string doesn't throw off the depth
    count). Returns None if no balanced blob is found."""
    i = start_idx
    while i < len(text) and text[i] not in "{[":
        i += 1
    if i >= len(text):
        return None
    open_ch, close_ch = ("{", "}") if text[i] == "{" else ("[", "]")
    depth = 0
    in_string = False
    escape = False
    start = i
    for j in range(i, len(text)):
        ch = text[j]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return text[start:j + 1]
    return None


def _find_embedded_json_blobs(html: str) -> list[tuple[str, str]]:
    """Locate embedded JSON/state blobs in the RAW HTML (script/style
    content included — this deliberately does NOT use the script-stripped
    visible text): a Next.js __NEXT_DATA__ script tag, any
    <script type="application/json"> block, and a window.__INITIAL_STATE__
    assignment inside a plain <script> tag. Returns (source_label, blob)
    pairs; a page with none of these returns an empty list, which the
    caller reports explicitly rather than treating as "no signal"."""
    blobs: list[tuple[str, str]] = []
    soup = BeautifulSoup(html, "html.parser")

    next_data_tag = soup.find("script", id="__NEXT_DATA__")
    if next_data_tag is not None and next_data_tag.string:
        blobs.append(("__NEXT_DATA__", next_data_tag.string))

    for i, tag in enumerate(soup.find_all("script", type="application/json")):
        if tag is next_data_tag or not tag.string:
            continue
        label = f'<script type="application/json"> #{i}' + (f' id={tag.get("id")!r}' if tag.get("id") else "")
        blobs.append((label, tag.string))

    for tag in soup.find_all("script"):
        text = tag.string
        if not text:
            continue
        idx = text.find("__INITIAL_STATE__")
        if idx == -1:
            continue
        eq_idx = text.find("=", idx)
        if eq_idx == -1:
            continue
        json_text = _extract_balanced_json(text, eq_idx + 1)
        if json_text:
            blobs.append(("window.__INITIAL_STATE__", json_text))

    return blobs


def _extract_candidate_product_ids(url: str) -> list[str]:
    """Best-effort candidate product ID/SKU values pulled from the URL —
    the exact convention isn't confirmed for RelianceDigital's real product
    URLs from this sandbox, so this returns several candidates rather than
    committing to one: the path segment right after a literal "/p/" (a
    common e-commerce convention), the last non-empty path segment, and any
    standalone alphanumeric token of 6+ characters containing a digit found
    anywhere in the path. Query string is ignored (rarely carries product
    identity). De-duplicated, order preserved."""
    path = urlparse(url).path
    segments = [s for s in path.split("/") if s]
    candidates = []

    for i, seg in enumerate(segments):
        if seg.lower() == "p" and i + 1 < len(segments):
            candidates.append(segments[i + 1])
    if segments:
        candidates.append(segments[-1])
    for seg in segments:
        for token in re.findall(r"[A-Za-z0-9]+", seg):
            if len(token) >= 6 and any(c.isdigit() for c in token):
                candidates.append(token)

    seen = set()
    out = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _find_product_object(data, candidate_ids: list[str], path: str = "", substring_ok: bool = False):
    """Recursively walk a parsed JSON value looking for a dict that has one
    of candidate_ids as a direct VALUE (not key) among its own key:value
    pairs — i.e. that dict IS the product's object, with the matching ID as
    a sibling to fields like sellable/is_available. A dict's own values are
    checked before recursing into its children, so the shallowest/most
    specific containing object wins (depth-first). With substring_ok=False
    (the default, tried first by the caller) only an exact string match
    counts, to avoid matching an unrelated object whose ID merely overlaps
    a candidate — e.g. a "related products" list. Returns
    (path_to_dict, the_dict) for the first match, or None."""
    if isinstance(data, dict):
        for value in data.values():
            if isinstance(value, bool) or not isinstance(value, (str, int)):
                continue
            value_str = str(value)
            for cid in candidate_ids:
                if value_str == cid:
                    return path, data
                if substring_ok and len(cid) >= 6 and (cid in value_str or value_str in cid):
                    return path, data
        for key, value in data.items():
            child_path = f"{path}.{key}" if path else key
            result = _find_product_object(value, candidate_ids, child_path, substring_ok)
            if result:
                return result
    elif isinstance(data, list):
        for i, item in enumerate(data):
            result = _find_product_object(item, candidate_ids, f"{path}[{i}]", substring_ok)
            if result:
                return result
    return None


def _find_stock_keys_within(data, base_path: str) -> list[tuple[str, object]]:
    """Recursively search WITHIN data (already scoped to just the matched
    product's own object/subtree — NOT the whole JSON document) for any key
    matching _JSON_STATE_KEYWORDS, returning (full_path, value) pairs
    rooted at base_path — e.g. base_path="data.product" plus a nested
    "variants[0].sellable" match yields "data.product.variants[0].sellable"."""
    results: list[tuple[str, object]] = []

    def walk(node, path):
        if isinstance(node, dict):
            for k, v in node.items():
                child_path = f"{path}.{k}" if path else k
                if any(kw in k.lower() for kw in _JSON_STATE_KEYWORDS):
                    results.append((child_path, v))
                walk(v, child_path)
        elif isinstance(node, list):
            for i, item in enumerate(node):
                walk(item, f"{path}[{i}]")

    walk(data, base_path)
    return results


def _dump_json_structure_preview(data, max_keys: int = 20) -> str:
    """Fallback diagnostic when the product's object can't be located: the
    first-level keys of the JSON root plus one level of nesting under each,
    so the correct path can be identified manually."""
    lines = []
    if isinstance(data, dict):
        for k in list(data.keys())[:max_keys]:
            v = data[k]
            if isinstance(v, dict):
                nested = list(v.keys())[:max_keys]
                suffix = ", ..." if len(v) > max_keys else ""
                lines.append(f"{k}: {{{', '.join(nested)}{suffix}}}")
            elif isinstance(v, list):
                extra = ""
                if v and isinstance(v[0], dict):
                    extra = f" — first item keys: {list(v[0].keys())[:max_keys]}"
                lines.append(f"{k}: [list of {len(v)} item(s)]{extra}")
            else:
                lines.append(f"{k}: {v!r}")
    elif isinstance(data, list):
        lines.append(f"(root is a list of {len(data)} item(s))")
        if data and isinstance(data[0], dict):
            lines.append(f"first item keys: {list(data[0].keys())[:max_keys]}")
    else:
        lines.append(f"(root is a {type(data).__name__}, not an object)")
    return "\n".join(lines)


async def _report_embedded_json_signals(message: Message, label: str, html: str, url: str) -> None:
    """For each embedded JSON/state blob in the raw HTML, parse it fully
    (not a regex grep) and locate the MAIN PRODUCT's own object — the dict
    holding the page's product ID/SKU (extracted from url) as a direct
    sibling value — then report the full path and value of every
    sellable/is_available-like key found WITHIN that specific object, not
    just the first such key anywhere in the blob (which could belong to an
    unrelated related-products list, a different variant, etc.). Falls back
    to a root-plus-one-level structure dump per blob when the product
    object can't be located, so the correct path can be found manually."""
    blobs = _find_embedded_json_blobs(html)
    if not blobs:
        await _debug_send(
            message,
            f"[{label}] embedded JSON state: no __NEXT_DATA__, "
            f"window.__INITIAL_STATE__, or <script type=\"application/json\"> "
            f"blocks found in the raw HTML.",
        )
        return

    candidate_ids = _extract_candidate_product_ids(url)

    for source_label, json_text in blobs:
        try:
            parsed = json.loads(json_text)
        except Exception as exc:
            await _debug_send(
                message,
                f"[{label}] {source_label}: found ({len(json_text)} chars) but failed "
                f"to parse as JSON: {exc}",
            )
            continue

        match = None
        if candidate_ids:
            match = _find_product_object(parsed, candidate_ids, substring_ok=False)
            if match is None:
                match = _find_product_object(parsed, candidate_ids, substring_ok=True)

        if match is None:
            preview = _dump_json_structure_preview(parsed)
            reason = (
                f"no candidate product ID could be extracted from the URL"
                if not candidate_ids
                else f"none of {candidate_ids!r} (from the URL) matched anywhere in this blob"
            )
            await _debug_send(
                message,
                f"[{label}] {source_label}: couldn't locate the product's object — {reason}.\n"
                f"Root structure (first-level keys + one level of nesting):\n{preview}",
            )
            continue

        product_path, product_obj = match
        stock_keys = _find_stock_keys_within(product_obj, product_path)
        if not stock_keys:
            await _debug_send(
                message,
                f"[{label}] {source_label}: product object located at "
                f"{product_path or '(root)'!r} but no sellable/is_available-like "
                f"key inside it.",
            )
            continue

        lines = [
            f"[{label}] {source_label}: product object located at "
            f"{product_path or '(root)'!r} — stock-related field(s):"
        ]
        for full_path, value in stock_keys:
            lines.append(f"  {full_path} = {value!r}")
        await _debug_send(message, "\n".join(lines))


async def _run_debug_reliance_trial(message: Message, label: str, url: str, **scraper_kwargs) -> None:
    """Fetch `url` via the active scraping provider (checkers.fetch_page)
    with the given kwargs and send a labeled diagnostic report: HTTP
    status, raw HTML length, where _RELIANCE_ANTIBOT_PHRASE was (or
    wasn't) found, then the FULL visible text chunked under Telegram's
    4096-char limit (not truncated — unlike /debugoneplus, which still
    only sends the first 3000 chars). Any failure here is reported under
    this trial's own label and does not raise — safe to call multiple
    times/labels from the same command."""
    await _debug_send(message, f"— {label} —")

    try:
        resp = await fetch_page(url, timeout=60.0, **scraper_kwargs)
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

    await _report_embedded_json_signals(message, label, html, url)

    await _debug_send(message, f"[{label}] visible text: {len(visible_text)} chars total (sending in full).")
    _CHUNK_SIZE = 4000
    for i in range(0, len(visible_text), _CHUNK_SIZE):
        await _debug_send(message, visible_text[i:i + _CHUNK_SIZE])


@router.message(Command("debugreliance"))
async def cmd_debugreliance(message: Message, command: CommandObject):
    if message.from_user.id != _DEBUG_RELIANCE_ADMIN_ID:
        return
    if not command.args:
        await message.answer(
            "Usage: <code>/debugreliance &lt;url&gt; [pincode]</code>", parse_mode="HTML"
        )
        return

    parts = command.args.strip().rsplit(maxsplit=1)
    # A pincode is a bare 6-digit number; anything else in the trailing
    # token (or no second token at all) means the whole args string is
    # just the URL and no pincode was supplied.
    if len(parts) == 2 and parts[1].isdigit() and len(parts[1]) == 6:
        url, pincode = parts
    else:
        url, pincode = command.args.strip(), None

    if pincode:
        label = "super=true + setCookies=pincode (premium proxy, default)"
        await _debug_send(
            message,
            f"🔍 Fetching (super=true, premium proxy, pincode cookie={pincode!r}): {url}",
        )
        await _run_debug_reliance_trial(
            message, label, url,
            super_proxy=True, set_cookies=f"pincode={pincode}",
        )
    else:
        await _debug_send(message, f"🔍 Fetching (super=true, premium proxy — default for RelianceDigital): {url}")
        await _run_debug_reliance_trial(
            message, "super=true (premium proxy, default)", url,
            super_proxy=True,
        )


# ---------------------------------------------------------------------------
# TEMPORARY debug command for verifying Scrape.do's "playWithBrowser"
# browser-interaction actions (Click/Fill/Wait — confirmed to exist via
# Scrape.do's own documentation, see checkers/common.py's build_scraper_url)
# against RelianceDigital's pincode-entry widget: click the pincode input,
# fill it with a pincode, click the submit/check button, wait, then capture
# the resulting HTML. Same admin restriction as /debugreliance above. NOT
# wired into CHECKER_MAP or the regular check cycle — calls
# checkers.reliancedigital.fetch_with_pincode_interaction(), a debug-only
# function checkers/reliancedigital.py's own check()/production path never
# touches. The CSS selectors used are BEST-GUESS, not verified against the
# real site (no live network access from this sandbox) — this command's
# entire purpose is to reveal whether they actually work. Safe to delete
# once no longer needed.
# ---------------------------------------------------------------------------
_DEBUG_RELIANCE2_ADMIN_ID = 5004721766  # same hardcoded restriction as
# every other /debug* command above, on top of the router's own
# ADMIN_USER_ID filter — this fetches an arbitrary caller-supplied URL via
# Scrape.do (spends credits, and render=true+super=true+playWithBrowser is
# likely a more expensive combination than the plain fetches other /debug*
# commands make).


@router.message(Command("debugreliance2"))
async def cmd_debugreliance2(message: Message, command: CommandObject):
    if message.from_user.id != _DEBUG_RELIANCE2_ADMIN_ID:
        return
    if not command.args:
        await message.answer(
            "Usage: <code>/debugreliance2 &lt;url&gt; [pincode]</code>", parse_mode="HTML"
        )
        return

    parts = command.args.strip().rsplit(maxsplit=1)
    if len(parts) == 2 and parts[1].isdigit() and len(parts[1]) == 6:
        url, pincode = parts
    else:
        url, pincode = command.args.strip(), "110001"

    await _debug_send(
        message,
        f"🔍 Simulating pincode entry (click → fill {pincode!r} → click submit → "
        f"wait) via Scrape.do playWithBrowser, render=true + super=true: {url}\n"
        f"Selectors used (best-guess, unverified): input={reliancedigital._PINCODE_INPUT_SELECTOR!r} "
        f"submit={reliancedigital._PINCODE_SUBMIT_SELECTOR!r}",
    )

    try:
        html = await reliancedigital.fetch_with_pincode_interaction(url, pincode=pincode)
    except Exception as exc:
        await _debug_send(message, f"⚠️ playWithBrowser fetch failed: {exc}")
        return

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    visible_text = soup.get_text(" ", strip=True)

    await _debug_send(
        message,
        f"Raw HTML length: {len(html)} chars | visible text: {len(visible_text)} chars",
    )

    await _report_embedded_json_signals(message, "debugreliance2", html, url)

    await _debug_send(message, f"[debugreliance2] visible text: {len(visible_text)} chars total (sending in full).")
    _CHUNK_SIZE = 4000
    for i in range(0, len(visible_text), _CHUNK_SIZE):
        await _debug_send(message, visible_text[i:i + _CHUNK_SIZE])


# ---------------------------------------------------------------------------
# TEMPORARY debug command for tuning checkers/shopatsc.py's three-tier
# Scrape.do escalation (render=false, then render=true, then super=true —
# the .js Shopify JSON endpoint's "available" field was confirmed
# unreliable for this store and is no longer used at all) — same admin
# restriction as /debugoneplus and /debugreliance above. NOT wired into
# CHECKER_MAP or the regular check cycle — shopatsc's live check_stock
# fetch (stock_checker.py) skips straight to super=true (render=false and
# render=true were both confirmed failing for this site — HTTP 502 and
# timeout respectively) and is completely untouched by this command. This
# command instead calls checkers.shopatsc.debug_check(), a
# diagnostics-only sibling that still exercises all three tiers in order
# and returns rich per-tier detail (status codes, errors, visible-text
# length, timing) instead of collapsing to a bool, so it stays useful for
# monitoring whether render=false/render=true ever recover. Safe to
# delete once no longer needed.
# ---------------------------------------------------------------------------
_DEBUG_SONYOFFICIAL_ADMIN_ID = 5004721766  # same hardcoded restriction as
# /debugoneplus and /debugreliance, on top of the router's own
# ADMIN_USER_ID filter — this fetches an arbitrary caller-supplied URL via
# Scrape.do (spends credits).


@router.message(Command("debugsonyofficial"))
async def cmd_debugsonyofficial(message: Message, command: CommandObject):
    if message.from_user.id != _DEBUG_SONYOFFICIAL_ADMIN_ID:
        return
    if not command.args:
        await message.answer(
            "Usage: <code>/debugsonyofficial &lt;url&gt;</code>", parse_mode="HTML"
        )
        return

    url = command.args.strip()
    await _debug_send(message, f"🔍 Running shopatsc three-tier check: {url}")

    try:
        result = await shopatsc.debug_check(url)
    except Exception as exc:
        await _debug_send(message, f"⚠️ debug_check crashed: {exc}")
        return

    lines = ["— Tier 1: render=false —"]
    lines.append(
        f"Status: HTTP {result['render_false_status_code']}"
        if result["render_false_status_code"] is not None else "Status: (no response)"
    )
    if result["render_false_error"]:
        lines.append(f"❌ Error: {result['render_false_error']}")
    lines.append(f"Visible text length: {result['render_false_visible_text_length']} chars")
    lines.append(f"Looked incomplete: {'yes' if result['render_false_looked_incomplete'] else 'no'}")
    lines.append(f"⏱ Time: {result['render_false_elapsed_seconds']:.2f}s")

    lines.append("")
    if result["used_render_true"]:
        lines.append("— Tier 2: render=true (tier 1 looked incomplete/failed) —")
        lines.append(
            f"Status: HTTP {result['render_true_status_code']}"
            if result["render_true_status_code"] is not None else "Status: (no response)"
        )
        if result["render_true_error"]:
            lines.append(f"❌ Error: {result['render_true_error']}")
        if result["render_true_visible_text_length"] is not None:
            lines.append(f"Visible text length: {result['render_true_visible_text_length']} chars")
        if result["render_true_looked_incomplete"] is not None:
            lines.append(f"Looked incomplete: {'yes' if result['render_true_looked_incomplete'] else 'no'}")
        lines.append(f"⏱ Time: {result['render_true_elapsed_seconds']:.2f}s")
    else:
        lines.append("— Tier 2: render=true — NOT used (tier 1 was sufficient) —")

    lines.append("")
    if result["used_super_proxy"]:
        lines.append("— Tier 3: super=true premium proxy (tiers 1 & 2 both looked incomplete/failed) —")
        lines.append(
            f"Status: HTTP {result['super_proxy_status_code']}"
            if result["super_proxy_status_code"] is not None else "Status: (no response)"
        )
        if result["super_proxy_error"]:
            lines.append(f"❌ Error: {result['super_proxy_error']}")
        if result["super_proxy_visible_text_length"] is not None:
            lines.append(f"Visible text length: {result['super_proxy_visible_text_length']} chars")
        lines.append(f"⏱ Time: {result['super_proxy_elapsed_seconds']:.2f}s")
    else:
        lines.append("— Tier 3: super=true — NOT used —")

    lines.append("")
    lines.append(f"Signal used: {result['signal']}")
    if result["in_stock"] is None:
        lines.append("Verdict: ⚠️ INCONCLUSIVE (all attempted tiers failed)")
    else:
        lines.append(f"Verdict: {'✅ IN STOCK' if result['in_stock'] else '❌ OUT OF STOCK'}")

    lines.append("")
    lines.append(f"⏱ Total time: {result['total_elapsed_seconds']:.2f}s")
    lines.append("")
    lines.append(
        "Note: live check_stock() skips straight to Tier 3 (super=true) for this "
        "site — tiers 1 & 2 are known-failing there and are only run here for "
        "monitoring purposes."
    )

    await _debug_send(message, "\n".join(lines))


# ---------------------------------------------------------------------------
# TEMPORARY debug command for tuning checkers/unicornstore.py against real
# product pages — same admin restriction as the other /debug* commands
# above. NOT wired into CHECKER_MAP or the regular check cycle — Unicorn
# Store's live check_stock fetch (stock_checker.py) is completely untouched
# by this. Mirrors the EXACT fetch escalation stock_checker.py actually
# uses for "unicornstore" today (render=true first via _JS_SITES, then
# render=true+super=true if the first attempt looks blocked/incomplete via
# _SUPER_PROXY_FALLBACK_SITES), INCLUDING the waitUntil="networkidle0" +
# customWait=6000ms JS-settle fix (stock_checker._SITE_WAIT_UNTIL /
# _SITE_CUSTOM_WAIT_MS) — confirmed necessary via an earlier run of this
# exact command, which showed render=true alone capturing the page before
# its SPA finished loading (visible text was just boilerplate/footer plus
# the literal "Please enable JavaScript to continue using this
# application" fallback text, no real product content at all — the same
# symptom OnePlus hit, fixed the same way). The blocked/incomplete
# heuristic below is a deliberate local copy of stock_checker._looks_
# blocked_or_incomplete's logic (same constants), not an import, matching
# this file's existing convention of keeping every /debug* command
# self-contained. Safe to delete once no longer needed.
# ---------------------------------------------------------------------------
_DEBUG_UNICORN_ADMIN_ID = 5004721766  # same hardcoded restriction as every
# other /debug* command above, on top of the router's own ADMIN_USER_ID
# filter — this fetches an arbitrary caller-supplied URL via Scrape.do
# (spends credits).

# Mirrors stock_checker._SITE_WAIT_UNTIL["unicornstore"] /
# _SITE_CUSTOM_WAIT_MS["unicornstore"] exactly, so this command's fetches
# match production for real.
_UNICORN_WAIT_UNTIL = "networkidle0"
_UNICORN_CUSTOM_WAIT_MS = 6000

# Mirrors stock_checker._BLOCKED_PAGE_PHRASES / _MIN_PLAUSIBLE_HTML_LENGTH /
# _looks_blocked_or_incomplete exactly, so this command's escalation
# decision matches production's for real.
_UNICORN_BLOCKED_PAGE_PHRASES = (
    "access denied", "attention required", "are you a human",
    "captcha", "just a moment", "checking your browser",
    "please enable javascript and cookies", "bot detection",
    "request unsuccessful",
)
_UNICORN_MIN_PLAUSIBLE_HTML_LENGTH = 2000


def _unicorn_looks_blocked_or_incomplete(html: str) -> bool:
    if len(html) < _UNICORN_MIN_PLAUSIBLE_HTML_LENGTH:
        return True
    html_lower = html.lower()
    return any(phrase in html_lower for phrase in _UNICORN_BLOCKED_PAGE_PHRASES)


@router.message(Command("debugunicorn"))
async def cmd_debugunicorn(message: Message, command: CommandObject):
    if message.from_user.id != _DEBUG_UNICORN_ADMIN_ID:
        return
    if not command.args:
        await message.answer(
            "Usage: <code>/debugunicorn &lt;url&gt;</code>", parse_mode="HTML"
        )
        return

    url = command.args.strip()
    start = time.monotonic()

    # Tier 1: render=true + waitUntil=networkidle0 + customWait=6000ms —
    # the current production default for "unicornstore" (_JS_SITES plus
    # the JS-settle fix in _SITE_WAIT_UNTIL/_SITE_CUSTOM_WAIT_MS).
    method_used = f"render=true, waitUntil={_UNICORN_WAIT_UNTIL!r}, customWait={_UNICORN_CUSTOM_WAIT_MS}ms"
    try:
        stage_start = time.monotonic()
        resp = await fetch_page(
            url, render_js=True, wait_until=_UNICORN_WAIT_UNTIL,
            custom_wait_ms=_UNICORN_CUSTOM_WAIT_MS, timeout=60.0,
        )
        status_code = resp.status_code
        html = resp.text
        elapsed_stage1 = time.monotonic() - stage_start
    except Exception as exc:
        await _debug_send(message, f"⚠️ render=true fetch failed: {exc}")
        return

    await _debug_send(
        message,
        f"— Tier 1: render=true, waitUntil={_UNICORN_WAIT_UNTIL!r}, "
        f"customWait={_UNICORN_CUSTOM_WAIT_MS}ms —\nStatus: HTTP {status_code}\n"
        f"Visible text length so far: {len(html)} raw chars\n⏱ Time: {elapsed_stage1:.2f}s",
    )

    # Tier 2: render=true + super=true (same wait params), with automatic
    # retry on HTTP 502 (up to 3 total attempts, ~4s apart — Scrape.do's
    # proxy-rotation-failure symptom, sometimes carrying an ErrorType:
    # "ROTATION_FAILED" header) — only if tier 1 still looks blocked/
    # incomplete, exactly matching stock_checker.py's escalation for this
    # site (_SUPER_PROXY_FALLBACK_SITES + _RETRY_502_SITES).
    if _unicorn_looks_blocked_or_incomplete(html):
        stage_start = time.monotonic()
        resp2, attempts = await fetch_with_502_retry(
            url, render_js=True, super_proxy=True,
            wait_until=_UNICORN_WAIT_UNTIL, custom_wait_ms=_UNICORN_CUSTOM_WAIT_MS,
        )
        elapsed_stage2 = time.monotonic() - stage_start

        attempt_lines = []
        for a in attempts:
            outcome = f"HTTP {a['status_code']}" if a["error"] is None else (a["error"] or "unknown error")
            attempt_lines.append(f"  Attempt {a['attempt']}/{len(attempts)}: {outcome}")

        if resp2 is not None:
            status_code = resp2.status_code
            html = resp2.text
            method_used = (
                f"render=true + super=true, waitUntil={_UNICORN_WAIT_UNTIL!r}, "
                f"customWait={_UNICORN_CUSTOM_WAIT_MS}ms (premium proxy, escalated — "
                f"tier 1 looked blocked/incomplete; {len(attempts)} attempt(s) made)"
            )
            await _debug_send(
                message,
                f"— Tier 2: render=true + super=true, retrying on HTTP 502 "
                f"(tier 1 looked blocked/incomplete) —\n"
                + "\n".join(attempt_lines)
                + f"\nFinal status: HTTP {status_code}\n"
                f"Visible text length so far: {len(html)} raw chars\n"
                f"⏱ Time: {elapsed_stage2:.2f}s",
            )
        else:
            # Every attempt failed outright (non-502 exception, e.g. a
            # timeout/connection error) — no response at all. Per the
            # "don't crash or hang" requirement, keep the earlier
            # (tier 1) HTML and still send a reply rather than aborting
            # the command silently.
            method_used = (
                f"render=true only, waitUntil={_UNICORN_WAIT_UNTIL!r}, "
                f"customWait={_UNICORN_CUSTOM_WAIT_MS}ms (super=true retry attempted "
                f"{len(attempts)} time(s) but failed outright each time — kept tier 1's HTML)"
            )
            await _debug_send(
                message,
                f"— Tier 2: render=true + super=true, retrying on HTTP 502 "
                f"(tier 1 looked blocked/incomplete) —\n"
                + "\n".join(attempt_lines)
                + f"\n⚠️ All {len(attempts)} attempt(s) failed outright — no response received. "
                f"Falling back to tier 1's HTML rather than crashing.\n⏱ Time: {elapsed_stage2:.2f}s",
            )
    else:
        await _debug_send(message, "— Tier 2: render=true + super=true — NOT used (tier 1 was sufficient) —")

    soup = BeautifulSoup(html, "html.parser")
    text_soup = BeautifulSoup(html, "html.parser")
    for tag in text_soup(["script", "style"]):
        tag.decompose()
    visible_text = text_soup.get_text(" ", strip=True)

    await _debug_send(
        message,
        f"🔍 Fetch method used: {method_used}\n"
        f"Final HTTP status: {status_code}\n"
        f"Visible text length: {len(visible_text)} chars\n"
        f"Visible text preview (first 500 chars): {visible_text[:500]!r}",
    )

    await _report_embedded_json_signals(message, "unicornstore", html, url)

    verdict = unicornstore.check(soup, html)
    total_elapsed = time.monotonic() - start
    await _debug_send(
        message,
        f"Signal/logic used: checkers.common.generic_marketplace_check() with "
        f"unicornstore's own ADD patterns {unicornstore._ADD_PATTERNS!r} and "
        f"OOS patterns {unicornstore._OOS_PATTERNS!r} (JSON-LD availability -> "
        f"embedded-JSON stock key -> OOS text -> active add-to-cart button/attr "
        f"-> default False; see checkers/common.py for the full waterfall).\n"
        f"Verdict: {'✅ IN STOCK' if verdict else '❌ OUT OF STOCK'}\n"
        f"⏱ Total time: {total_elapsed:.2f}s",
    )

    await _debug_send(message, f"📄 Full visible text ({len(visible_text)} chars, sending in full):")
    _CHUNK_SIZE = 4000
    for i in range(0, len(visible_text), _CHUNK_SIZE):
        await _debug_send(message, visible_text[i:i + _CHUNK_SIZE])


# ---------------------------------------------------------------------------
# TEMPORARY debug command for tuning checkers/inventstore.py against real
# product pages — same admin restriction as the other /debug* commands
# above. NOT wired into CHECKER_MAP or the regular check cycle —
# InventStore's live check_stock fetch (stock_checker.py) is completely
# untouched by this. Mirrors the EXACT fetch escalation stock_checker.py
# actually uses for "inventstore" today (render=true first via _JS_SITES,
# then render=true+super=true if that looks blocked/incomplete via
# _SUPER_PROXY_FALLBACK_SITES — inventstore has neither the JS-settle wait
# params nor the 502-retry logic added for unicornstore, so this command
# deliberately doesn't add them either). The blocked/incomplete heuristic
# below is a deliberate local copy of stock_checker._looks_blocked_or_
# incomplete's logic (same constants), not an import, matching this file's
# existing convention of keeping every /debug* command self-contained.
# Safe to delete once no longer needed.
# ---------------------------------------------------------------------------
_DEBUG_INVENTSTORE_ADMIN_ID = 5004721766  # same hardcoded restriction as
# every other /debug* command above, on top of the router's own
# ADMIN_USER_ID filter — this fetches an arbitrary caller-supplied URL via
# Scrape.do (spends credits).

_INVENTSTORE_BLOCKED_PAGE_PHRASES = (
    "access denied", "attention required", "are you a human",
    "captcha", "just a moment", "checking your browser",
    "please enable javascript and cookies", "bot detection",
    "request unsuccessful",
)
_INVENTSTORE_MIN_PLAUSIBLE_HTML_LENGTH = 2000

# ~50 chars of context on either side of each match — enough to tell
# whether a hit is describing the main product or an unrelated section
# (a "related products" list, a filter label, a policy blurb, etc.).
_INVENTSTORE_STOCK_PHRASES = ("out of stock", "in stock")
_INVENTSTORE_CONTEXT_CHARS = 50


def _inventstore_looks_blocked_or_incomplete(html: str) -> bool:
    if len(html) < _INVENTSTORE_MIN_PLAUSIBLE_HTML_LENGTH:
        return True
    html_lower = html.lower()
    return any(phrase in html_lower for phrase in _INVENTSTORE_BLOCKED_PAGE_PHRASES)


def _find_stock_phrase_occurrences(visible_text: str) -> list[tuple[str, str, int]]:
    """Find every case-insensitive occurrence of "out of stock" or "in
    stock" in visible_text. Returns (phrase, context, index) triples,
    sorted by position in the text, where context is up to
    _INVENTSTORE_CONTEXT_CHARS characters before and after the match
    (clipped at the text's edges). "in stock" is never a substring of
    "out of stock" (the word before "stock" there is "of", not "in"), so
    the two phrases can't double-count the same occurrence."""
    results: list[tuple[str, str, int]] = []
    lower_text = visible_text.lower()
    for phrase in _INVENTSTORE_STOCK_PHRASES:
        start = 0
        while True:
            idx = lower_text.find(phrase, start)
            if idx == -1:
                break
            ctx_start = max(0, idx - _INVENTSTORE_CONTEXT_CHARS)
            ctx_end = min(len(visible_text), idx + len(phrase) + _INVENTSTORE_CONTEXT_CHARS)
            context = visible_text[ctx_start:ctx_end]
            results.append((phrase, context, idx))
            start = idx + len(phrase)
    results.sort(key=lambda r: r[2])
    return results


# ~100 chars of context for the raw-HTML searches below — wider than the
# visible-text search's 50, since raw markup (a JSON blob's keys, an
# attribute name/value pair) needs more surrounding characters to be
# legible than plain rendered sentences do.
_INVENTSTORE_RAW_CONTEXT_CHARS = 100
# "stock in-stock" is deliberately NOT searched here — confirmed via real
# /debuginventstore results that WooCommerce never emits that marker at
# all; checkers/inventstore.py's own detection now only ever looks for
# the plain visible-text phrase "In Stock" (see checkers/inventstore.py),
# so only "out of stock" remains a real signal to search for here.
_INVENTSTORE_RAW_PHRASES = ("out of stock",)


def _find_raw_html_occurrences(html: str, phrase: str) -> list[tuple[str, int]]:
    """Find every case-insensitive occurrence of `phrase` in the RAW
    HTML — <script> tag contents, HTML attributes (e.g.
    data-stock-status="Out of Stock"), and any other raw markup included,
    unlike _find_stock_phrase_occurrences above which only searches the
    visible/rendered text. Returns (context, index) pairs sorted by
    position, where context is up to _INVENTSTORE_RAW_CONTEXT_CHARS
    characters before and after the match (clipped at the string's
    edges) — this surfaces a match sitting inside an embedded JSON state
    blob or a data attribute that never becomes visible text, which the
    visible-text-only search would miss entirely."""
    results: list[tuple[str, int]] = []
    lower_html = html.lower()
    lower_phrase = phrase.lower()
    start = 0
    while True:
        idx = lower_html.find(lower_phrase, start)
        if idx == -1:
            break
        ctx_start = max(0, idx - _INVENTSTORE_RAW_CONTEXT_CHARS)
        ctx_end = min(len(html), idx + len(lower_phrase) + _INVENTSTORE_RAW_CONTEXT_CHARS)
        context = html[ctx_start:ctx_end]
        results.append((context, idx))
        start = idx + len(lower_phrase)
    return results


# ---------------------------------------------------------------------------
# Race-condition investigation (not a fix — see cmd_debuginventstore's
# final section below): real /debuginventstore runs showed the SAME URL
# sometimes has "In Stock" in its visible text and sometimes doesn't,
# despite an identical HTTP 200 + full page length each time. The leading
# hypothesis is that WooCommerce loads the selected variation's stock
# status via a delayed AJAX call (wc-ajax=get_variation, WooCommerce's
# standard endpoint for this) rather than baking it into the initial HTML
# — meaning a fixed customWait may or may not be long enough, and a
# request-timing race, not a detection-logic bug, could be the real cause.
# ---------------------------------------------------------------------------
_INVENTSTORE_DIAGNOSTIC_WAIT_UNTIL = "networkidle0"
_INVENTSTORE_DIAGNOSTIC_CUSTOM_WAIT_MS = 10000  # 10s, vs. no explicit wait today

_INVENTSTORE_BAKED_IN_ATTR = "data-product_variations"


def _detect_ajax_variation_endpoint(html: str) -> list[str]:
    """Search the raw HTML for any reference to WooCommerce's AJAX
    variation-lookup endpoint convention (?wc-ajax=get_variation, or a
    generic wc-ajax= call) — evidence that stock status for a selected
    variation is fetched via a delayed AJAX request rather than being
    present in the initial page load at all. Returns the matched hint(s)
    found (at most one, the most specific one available)."""
    lower = html.lower()
    if "wc-ajax=get_variation" in lower:
        return ["wc-ajax=get_variation (WooCommerce's standard variation-lookup AJAX action)"]
    if "wc-ajax" in lower:
        return ["wc-ajax (a WooCommerce AJAX call is present, but not specifically get_variation)"]
    return []


def _detect_baked_in_variations_attr(html: str) -> tuple[bool, int]:
    """Check for WooCommerce's standard data-product_variations HTML
    attribute — if present, its JSON value carries per-variation data
    (including each variation's availability_html) directly in the
    initial page load, with no AJAX round-trip needed to learn stock
    status. Returns (attribute_present, "availability_html"
    occurrence_count) — the count is a rough proxy for how many
    variations have some availability_html content baked in (blank for
    in-stock, non-blank for out-of-stock, per the WooCommerce convention
    confirmed in earlier rounds), regardless of whether it's inside this
    specific attribute or elsewhere in the page."""
    present = _INVENTSTORE_BAKED_IN_ATTR in html.lower()
    availability_count = html.lower().count("availability_html")
    return present, availability_count


async def _inventstore_diagnostic_fetch(url: str) -> dict:
    """One controlled render=true + customWait=10000ms fetch — no tier
    escalation, deliberately kept simple/isolated so any difference
    between repeated calls is attributable only to genuine fetch-to-fetch
    nondeterminism (e.g. AJAX-timing luck), not to different fetch
    configurations being used. Returns the signals observed: whether the
    visible-text "In Stock" phrase was found (the checker's current
    signal), and whether a baked-in variations JSON attribute / an AJAX
    variation-lookup call was detected in the raw HTML (candidate
    alternative signals)."""
    resp = await fetch_page(
        url, render_js=True,
        wait_until=_INVENTSTORE_DIAGNOSTIC_WAIT_UNTIL,
        custom_wait_ms=_INVENTSTORE_DIAGNOSTIC_CUSTOM_WAIT_MS,
        timeout=60.0,
    )
    html = resp.text
    text_soup = BeautifulSoup(html, "html.parser")
    for tag in text_soup(["script", "style"]):
        tag.decompose()
    visible_text = text_soup.get_text(" ", strip=True)
    baked_in_present, availability_count = _detect_baked_in_variations_attr(html)
    return {
        "status_code": resp.status_code,
        "html_length": len(html),
        "visible_text_length": len(visible_text),
        "in_stock_found": inventstore._IN_STOCK_PHRASE in visible_text.lower(),
        "ajax_hints": _detect_ajax_variation_endpoint(html),
        "baked_in_present": baked_in_present,
        "availability_html_count": availability_count,
    }


@router.message(Command("debuginventstore"))
async def cmd_debuginventstore(message: Message, command: CommandObject):
    if message.from_user.id != _DEBUG_INVENTSTORE_ADMIN_ID:
        return
    if not command.args:
        await message.answer(
            "Usage: <code>/debuginventstore &lt;url&gt;</code>", parse_mode="HTML"
        )
        return

    url = command.args.strip()

    # Tier 1: render=true — the current production default for
    # "inventstore" (stock_checker._JS_SITES membership).
    method_used = "render=true"
    try:
        resp = await fetch_page(url, render_js=True, timeout=60.0)
        status_code = resp.status_code
        html = resp.text
    except Exception as exc:
        await _debug_send(message, f"⚠️ render=true fetch failed: {exc}")
        return

    await _debug_send(message, f"— Tier 1: render=true —\nStatus: HTTP {status_code}\nRaw length: {len(html)} chars")

    # Tier 2: render=true + super=true — only if tier 1 looks
    # blocked/incomplete, exactly matching stock_checker.py's
    # _SUPER_PROXY_FALLBACK_SITES escalation for this site.
    if _inventstore_looks_blocked_or_incomplete(html):
        method_used = "render=true + super=true (premium proxy, escalated — tier 1 looked blocked/incomplete)"
        try:
            resp2 = await fetch_page(url, render_js=True, super_proxy=True, timeout=60.0)
            status_code = resp2.status_code
            html = resp2.text
            await _debug_send(
                message,
                f"— Tier 2: render=true + super=true (tier 1 looked blocked/incomplete) —\n"
                f"Status: HTTP {status_code}\nRaw length: {len(html)} chars",
            )
        except Exception as exc:
            await _debug_send(message, f"⚠️ super=true fallback fetch failed: {exc}")
            return
    else:
        await _debug_send(message, "— Tier 2: render=true + super=true — NOT used (tier 1 was sufficient) —")

    text_soup = BeautifulSoup(html, "html.parser")
    for tag in text_soup(["script", "style"]):
        tag.decompose()
    visible_text = text_soup.get_text(" ", strip=True)

    await _debug_send(
        message,
        f"🔍 Fetch method used: {method_used}\n"
        f"Final HTTP status: {status_code}\n"
        f"Visible text length: {len(visible_text)} chars",
    )

    occurrences = _find_stock_phrase_occurrences(visible_text)
    if not occurrences:
        await _debug_send(message, "No occurrences of 'out of stock' or 'in stock' found in the visible text.")
    else:
        occ_lines = [f"Found {len(occurrences)} occurrence(s) of 'out of stock'/'in stock':"]
        for i, (phrase, context, idx) in enumerate(occurrences, 1):
            occ_lines.append(f"{i}. {phrase!r} @ char {idx}: …{context}…")
        occ_text = "\n".join(occ_lines)
        _CHUNK_SIZE = 4000
        for i in range(0, len(occ_text), _CHUNK_SIZE):
            await _debug_send(message, occ_text[i:i + _CHUNK_SIZE])

    found_in_stock_phrase = inventstore._IN_STOCK_PHRASE in visible_text.lower()
    verdict = inventstore.check(BeautifulSoup(html, "html.parser"), html)
    await _debug_send(
        message,
        f"🧩 checkers/inventstore.py's sole detection signal: literal phrase "
        f"{inventstore._IN_STOCK_PHRASE!r} in visible text (case-insensitive)\n"
        f"Found: {'yes' if found_in_stock_phrase else 'no'}\n"
        f"Verdict: {'✅ IN STOCK' if verdict else '❌ OUT OF STOCK'}",
    )

    for phrase in _INVENTSTORE_RAW_PHRASES:
        raw_occurrences = _find_raw_html_occurrences(html, phrase)
        if not raw_occurrences:
            await _debug_send(
                message,
                f"No occurrences of {phrase!r} found in the RAW HTML "
                f"(script tags and attributes included).",
            )
        else:
            raw_lines = [
                f"Found {len(raw_occurrences)} occurrence(s) of {phrase!r} in the "
                f"RAW HTML (script tags/attributes included, {len(html)} raw chars total):"
            ]
            for i, (context, idx) in enumerate(raw_occurrences, 1):
                raw_lines.append(f"{i}. @ char {idx}: …{context}…")
            raw_text = "\n".join(raw_lines)
            _CHUNK_SIZE = 4000
            for i in range(0, len(raw_text), _CHUNK_SIZE):
                await _debug_send(message, raw_text[i:i + _CHUNK_SIZE])

    await _debug_send(message, f"📄 Full visible text ({len(visible_text)} chars, sending in full):")
    _CHUNK_SIZE = 4000
    for i in range(0, len(visible_text), _CHUNK_SIZE):
        await _debug_send(message, visible_text[i:i + _CHUNK_SIZE])

    # ── Race-condition investigation: 2 back-to-back controlled fetches ──
    # (customWait=10000ms, no tier escalation) — see _inventstore_
    # diagnostic_fetch's docstring. NOT a fix, purely diagnostic; nothing
    # here feeds back into checkers/inventstore.py's actual detection.
    await _debug_send(
        message,
        f"🔬 Race-condition investigation: 2 back-to-back fetches with "
        f"customWait={_INVENTSTORE_DIAGNOSTIC_CUSTOM_WAIT_MS}ms "
        f"(waitUntil={_INVENTSTORE_DIAGNOSTIC_WAIT_UNTIL!r}), no tier escalation:",
    )
    try:
        run1 = await _inventstore_diagnostic_fetch(url)
        run2 = await _inventstore_diagnostic_fetch(url)
    except Exception as exc:
        await _debug_send(message, f"⚠️ Diagnostic fetch failed: {exc}")
        return

    for label, run in (("Run 1", run1), ("Run 2", run2)):
        ajax_line = (
            "; ".join(run["ajax_hints"]) if run["ajax_hints"]
            else "no wc-ajax reference found in raw HTML"
        )
        await _debug_send(
            message,
            f"— {label} —\n"
            f"HTTP {run['status_code']} | raw HTML {run['html_length']} chars | "
            f"visible text {run['visible_text_length']} chars\n"
            f"'In Stock' in visible text (waited-text signal): "
            f"{'✅ found' if run['in_stock_found'] else '❌ not found'}\n"
            f"AJAX variation endpoint reference: {ajax_line}\n"
            f"{_INVENTSTORE_BAKED_IN_ATTR!r} attribute present (baked-in JSON signal): "
            f"{'✅ yes' if run['baked_in_present'] else '❌ no'}\n"
            f"'availability_html' occurrences in raw HTML: {run['availability_html_count']}",
        )

    waited_text_consistent = run1["in_stock_found"] == run2["in_stock_found"]
    baked_in_consistent = (
        run1["baked_in_present"] == run2["baked_in_present"]
        and run1["availability_html_count"] == run2["availability_html_count"]
    )
    summary_lines = [
        "— Consistency summary —",
        "Waited visible-text signal ('In Stock' present): "
        + ("✅ consistent across both runs" if waited_text_consistent
           else f"❌ INCONSISTENT — differed between runs (run1={run1['in_stock_found']} vs run2={run2['in_stock_found']})"),
        f"Baked-in JSON signal ({_INVENTSTORE_BAKED_IN_ATTR!r} + availability_html count): "
        + ("✅ consistent across both runs" if baked_in_consistent else "❌ INCONSISTENT — differed between runs"),
    ]
    if waited_text_consistent and not baked_in_consistent:
        summary_lines.append("→ The waited-text signal was MORE consistent this run.")
    elif baked_in_consistent and not waited_text_consistent:
        summary_lines.append(
            "→ The baked-in JSON signal was MORE consistent this run — a stronger "
            "candidate to switch detection to if this holds up across more runs."
        )
    elif waited_text_consistent and baked_in_consistent:
        summary_lines.append(
            "→ Both signals were consistent this run — inconclusive on which is "
            "more reliable; try more back-to-back runs, ideally against a URL "
            "known to flip between states."
        )
    else:
        summary_lines.append(
            "→ BOTH signals were inconsistent this run — the race condition may "
            "affect more than just the visible-text timing; worth investigating "
            "further before picking a replacement signal."
        )
    await _debug_send(message, "\n".join(summary_lines))


# ---------------------------------------------------------------------------
# TEMPORARY debug command for the new Apple Store pickup-availability
# tracker feature. This is step 1 of that feature ONLY — verifying the SKU
# extraction and the fulfillment-messages API call/response before anything
# else is built. Explicitly NOT included in this round: /trackpickup, a new
# tracked-item-type DB schema, polling-cycle wiring, or notification-on-
# transition logic — all deferred until this command's real-world output is
# reviewed.
#
# Reuses checkers/apple.py's existing, already-in-production functions
# (_extract_sku, _build_fulfillment_target, _fetch_pickup_availability,
# _evaluate_pickup_availability — all used today by refine_with_pincode())
# rather than reimplementing SKU extraction or the API call. The one thing
# this command does that production code doesn't: report every store's raw
# pickupDisplay value and the full JSON response, not just the collapsed
# True/None verdict production only needs.
#
# NOT wired into CHECKER_MAP or the regular check cycle. Safe to delete once
# no longer needed.
# ---------------------------------------------------------------------------
_DEBUG_PICKUP_ADMIN_ID = 5004721766  # same hardcoded restriction as every
# other /debug* command above, on top of the router's own ADMIN_USER_ID
# filter — this fetches an arbitrary caller-supplied Apple URL plus calls
# Apple's fulfillment-messages API via Scrape.do (spends credits).


@router.message(Command("debugpickup"))
async def cmd_debugpickup(message: Message, command: CommandObject):
    if message.from_user.id != _DEBUG_PICKUP_ADMIN_ID:
        return
    if not command.args:
        await message.answer(
            "Usage: <code>/debugpickup &lt;apple_url&gt; &lt;pincode&gt;</code>", parse_mode="HTML"
        )
        return

    parts = command.args.strip().split()
    if len(parts) < 2:
        await message.answer(
            "Usage: <code>/debugpickup &lt;apple_url&gt; &lt;pincode&gt;</code>", parse_mode="HTML"
        )
        return
    url, pincode = parts[0], parts[1]

    await _debug_send(message, f"🔍 Fetching product page (render={apple.NEEDS_JS}): {url}")
    try:
        resp = await fetch_page(url, render_js=apple.NEEDS_JS, timeout=30.0)
        resp.raise_for_status()
        html = resp.text
    except Exception as exc:
        await _debug_send(message, f"⚠️ Product page fetch failed: {exc}")
        return

    soup = BeautifulSoup(html, "html.parser")
    sku = apple._extract_sku(soup, html)
    if not sku:
        await _debug_send(
            message,
            "⚠️ Could not extract a SKU/part number from this page (checked JSON-LD "
            "sku/offers.sku, inline partNumber, inline sku) — cannot call the "
            "fulfillment-messages API without one.",
        )
        return
    await _debug_send(message, f"✅ Extracted SKU: {sku!r}")

    target = apple._build_fulfillment_target(sku, pincode)
    await _debug_send(
        message,
        f"🔍 Calling fulfillment-messages API for pincode {pincode} — navigates "
        f"to the product page first (render_js=True), then triggers the API "
        f"call as an in-page fetch() from WITHIN that browser session (real "
        f"referrer/cookies), escalating to super_proxy=True if that fails "
        f"(see checkers/apple.py's _fetch_pickup_availability):\n{target}",
    )

    data, method, diagnostics = await apple._fetch_pickup_availability(sku, pincode, url)

    diag_lines = ["Per-tier diagnostics:"]
    for method_label, err in diagnostics:
        diag_lines.append(f"  • {method_label}: {'✅ succeeded' if err is None else f'❌ {err}'}")
    await _debug_send(message, "\n".join(diag_lines))

    if data is None:
        await _debug_send(
            message,
            "⚠️ fulfillment-messages call failed on every tier tried above — "
            "see the per-tier reasons and Railway logs for the exact "
            "exception/status/body.",
        )
        return

    await _debug_send(message, f"✅ Succeeded via method={method!r}")

    raw_json = json.dumps(data, indent=2)
    await _debug_send(message, f"Raw JSON response ({len(raw_json)} chars, sending in full):")
    _CHUNK_SIZE = 4000
    for i in range(0, len(raw_json), _CHUNK_SIZE):
        await _debug_send(message, raw_json[i:i + _CHUNK_SIZE])

    stores = (data.get("body") or {}).get("content", {}).get("pickupMessage", {}).get("stores", [])
    if not stores:
        await _debug_send(
            message,
            f"No stores returned for pincode {pincode} — common for most Indian "
            f"pincodes given Apple's sparse retail network (see checkers/apple.py's "
            f"design note); not necessarily a bug.",
        )
        return

    lines = [f"— {len(stores)} store(s) found for pincode {pincode} —"]
    for store in stores:
        part_info = (store.get("partsAvailability") or {}).get(sku, {})
        pickup_display = part_info.get("pickupDisplay", "(missing)")
        store_name = store.get("storeName", "(unknown store)")
        lines.append(f"{store_name}: pickupDisplay={pickup_display!r}")
    await _debug_send(message, "\n".join(lines))

    verdict = apple._evaluate_pickup_availability(data, sku)
    await _debug_send(
        message,
        f"Verdict via the existing _evaluate_pickup_availability (True = confirmed "
        f"pickup-available somewhere, None = inconclusive/unavailable): {verdict!r}",
    )


# ---------------------------------------------------------------------------
# /debugzyte — verify a single fetch through the active scraping provider
# (checkers.fetch_page / config.SCRAPING_PROVIDER) before trusting the Zyte
# API switchover for the live check cycle. Built specifically for the
# Scrape.do -> Zyte API cutover (Scrape.do's credits ran out): reports which
# provider actually served the fetch, the HTTP status, raw HTML length, and
# a chunk of visible text, so a human can eyeball a real Zyte response
# before it's relied on for every checker. NOT wired into CHECKER_MAP or
# the regular check cycle. Safe to delete once no longer needed.
# ---------------------------------------------------------------------------
_DEBUG_ZYTE_ADMIN_ID = 5004721766  # same hardcoded restriction as every
# other /debug* command above, on top of the router's own ADMIN_USER_ID
# filter — this fetches an arbitrary caller-supplied URL via whichever
# scraping provider is currently active (spends Zyte/Scrape.do credits).


@router.message(Command("debugzyte"))
async def cmd_debugzyte(message: Message, command: CommandObject):
    if message.from_user.id != _DEBUG_ZYTE_ADMIN_ID:
        return
    if not command.args:
        await message.answer(
            "Usage: <code>/debugzyte &lt;url&gt; [render] [raw]</code>\n"
            "Add <code>render</code> to fetch with render_js=True instead "
            "of the default False.\n"
            "Add <code>raw</code> to dump Zyte API's COMPLETE raw response "
            "(all HTTP headers + the full parsed JSON body, minus the huge "
            "HTML payload itself) instead of the usual visible-text report "
            "— specifically to check whether Zyte includes a real "
            "per-request cost/billing field (see database.py's "
            "zyte_usage_log for why this matters). Always goes straight to "
            "Zyte regardless of SCRAPING_PROVIDER, since the raw dump only "
            "makes sense for Zyte's own response shape.",
            parse_mode="HTML",
        )
        return

    parts = command.args.strip().split()
    flags = {p.lower() for p in parts[1:]}
    render_js = "render" in flags
    raw_mode = "raw" in flags
    url = parts[0]

    if raw_mode:
        await _debug_send(message, f"🔍 Fetching RAW via Zyte API (render_js={render_js}): {url}")
        try:
            raw = await zyte_client.fetch_raw(url, render_js=render_js, timeout=60.0)
        except Exception as exc:
            await _debug_send(message, f"⚠️ Fetch failed: {exc}")
            return

        await _debug_send(message, f"Zyte HTTP status: {raw['status_code']}")

        header_lines = "\n".join(f"  {k}: {v}" for k, v in sorted(raw["headers"].items()))
        await _debug_send(message, f"— Response headers —\n{header_lines or '(none)'}")

        if raw["cost_like_fields"]:
            await _debug_send(
                message,
                "💰 POSSIBLE COST/BILLING FIELD(S) FOUND:\n" + "\n".join(raw["cost_like_fields"]),
            )
        else:
            await _debug_send(
                message,
                "💸 No cost/billing/price/credit-like field found anywhere in the "
                "response body — matches this codebase's documentation research "
                "(no such field in Zyte's documented /v1/extract response schema).",
            )

        body_json = json.dumps(raw["body"], indent=2, default=str)
        await _debug_send(message, f"— Full JSON body ({len(body_json)} chars, HTML payload omitted) —")
        _CHUNK_SIZE = 3800
        for i in range(0, len(body_json), _CHUNK_SIZE):
            await _debug_send(message, body_json[i:i + _CHUNK_SIZE])
        return

    await _debug_send(
        message,
        f"🔍 Fetching via SCRAPING_PROVIDER={SCRAPING_PROVIDER!r} "
        f"(render_js={render_js}): {url}",
    )

    try:
        resp = await fetch_page(url, render_js=render_js, timeout=60.0)
    except Exception as exc:
        await _debug_send(message, f"⚠️ Fetch failed: {exc}")
        return

    html = resp.text
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    visible_text = soup.get_text(" ", strip=True)

    await _debug_send(
        message,
        f"Status: HTTP {resp.status_code}\n"
        f"Raw HTML length: {len(html)} chars | visible text: {len(visible_text)} chars",
    )

    snippet = visible_text[:3000]
    await _debug_send(
        message, f"📄 Visible text (first {len(snippet)} of {len(visible_text)} chars):",
    )
    _CHUNK_SIZE = 4000
    for i in range(0, len(snippet), _CHUNK_SIZE):
        await _debug_send(message, snippet[i:i + _CHUNK_SIZE])


# ---------------------------------------------------------------------------
# /creditusage — per-store Zyte API usage breakdown: request count, total
# data fetched, browser-rendered/actions-used split, sorted by highest
# usage first — a proxy for cost even without exact per-request Zyte
# pricing, since request count + browser-rendering directly correlates
# with what Zyte actually bills (browser-rendered requests cost roughly
# 5-15x more than plain HTTP ones per Zyte's own published pricing — see
# database.py's ZYTE_COST_PER_REQUEST_HTTP/BROWSER). The estimated $ range
# shown alongside is exactly that — an ESTIMATE, not real billing: Zyte's
# own /v1/extract response has no per-request cost/billing field
# (confirmed via documentation research and /debugzyte's "raw" mode — see
# database.py's zyte_usage_log schema comment for the full reasoning).
#
# Defaults to the CURRENT MONTH (IST) — a running counter that resets
# automatically at each month boundary without ever deleting the
# underlying log (get_zyte_usage_summary's since="__month__" default).
# /creditusage all shows the all-time total instead. Only counts requests
# actually served by Zyte (SCRAPING_PROVIDER="zyte") — Scrape.do usage has
# its own separate dashboard the admin already monitors directly.
# ---------------------------------------------------------------------------

@router.message(Command("creditusage"))
async def cmd_creditusage(message: Message, command: CommandObject):
    all_time = bool(command.args and command.args.strip().lower() == "all")
    summary = get_zyte_usage_summary(since=None if all_time else "__month__")
    period_label = "all-time" if all_time else "this month"

    if not summary:
        await message.answer(
            f"📭 No Zyte API usage logged yet ({period_label}). "
            f"Try <code>/creditusage all</code> for the all-time total.",
            parse_mode="HTML",
        )
        return

    total_requests = sum(r["request_count"] for r in summary)
    total_low = sum(r["estimated_cost_low"] for r in summary)
    total_high = sum(r["estimated_cost_high"] for r in summary)

    lines = [
        f"📊 <b>Zyte API credit usage — {period_label} — {total_requests} request(s) total</b>\n"
        f"💰 Estimated cost: ${total_low:,.4f} – ${total_high:,.4f} "
        f"(ESTIMATE from request count/bytes/browser-rendering — see "
        f"/debugzyte &lt;url&gt; raw for why no exact figure is available)\n",
    ]
    for r in summary:
        mb = r["total_bytes"] / (1024 * 1024)
        label = r["site"] if r["site"] == "(debug/other)" else get_site_label(r["site"])
        lines.append(
            f"🏪 <b>{label}</b>\n"
            f"   {r['request_count']} request(s) · {mb:.2f} MB total\n"
            f"   {r['browser_count']} browser-rendered · {r['http_count']} plain HTTP"
            + (f" · {r['actions_count']} used actions" if r["actions_count"] else "") + "\n"
            f"   Est. cost: ${r['estimated_cost_low']:,.4f} – ${r['estimated_cost_high']:,.4f}"
        )
    if not all_time:
        lines.append("\nUse <code>/creditusage all</code> for the all-time total.")
    text = "\n".join(lines)
    for i in range(0, len(text), 3800):
        await message.answer(text[i:i + 3800], parse_mode="HTML")


# ---------------------------------------------------------------------------
# /debugrenderfalse — tests whether render_js=False (plain HTTP, no
# headless browser) still returns usable/complete product data for the six
# checkers currently in stock_checker._JS_SITES on a "safe default, never
# individually verified" basis: inventstore, sangeethamobiles, vijaysales,
# tataneu, iqoo, vivo. OnePlus, RelianceDigital, and ShopAtSC are
# deliberately NOT covered here — those were already confirmed (via
# /debugoneplus and real diagnostics — see stock_checker.py's comments) to
# actually need JS rendering / a premium proxy, so there's nothing to
# re-test.
#
# Fetches the given URL with render_js=False, then runs that SITE'S OWN
# real check() function against the result (so the boolean verdict is
# exactly what production would compute), plus a per-site signal-presence
# report built from each checker's own real constants/JSON-LD scan (not
# hand-duplicated guesses) — so a page-structure difference between
# render_js=True and False is directly visible, not inferred. Also reports
# stock_checker._looks_blocked_or_incomplete's verdict — the same
# blocked/challenge-page heuristic the live check cycle uses to decide
# whether to escalate to super=true — since a render_js=False fetch that
# LOOKS fine but is secretly a bot-block page would be a false "it works"
# reading otherwise.
#
# Deliberately does NOT change any checker's NEEDS_JS/render default by
# itself — this sandbox has no live network access to actually fetch real
# product pages for these sites, so there is no real result to act on yet.
# The intended workflow: admin runs this against real product URLs (ideally
# one confirmed in-stock, one confirmed out-of-stock per site) and reports
# the output back; only THEN does switching stock_checker._JS_SITES happen,
# backed by real evidence, not a guess — matching this codebase's standing
# "never guess target-site behavior, verify via a debug command and real
# results first" principle.
# ---------------------------------------------------------------------------

_RENDER_FALSE_TEST_SITES = {
    "inventstore": inventstore,
    "sangeethamobiles": sangeethamobiles,
    "vijaysales": vijaysales,
    "tataneu": tataneu,
    "iqoo": iqoo,
    "vivo": vivo,
}

_DEBUG_RENDERFALSE_ADMIN_ID = 5004721766  # same hardcoded restriction as
# every other /debug* command above, on top of the router's own
# ADMIN_USER_ID filter — this fetches an arbitrary caller-supplied URL via
# whichever scraping provider is currently active (spends credits).


def _jsonld_offers_report(soup: BeautifulSoup) -> list[str]:
    """Shared JSON-LD offers.availability scan — the same pattern iqoo.py/
    vivo.py's own _log_diagnostics already runs, reused here so the debug
    report reflects the exact same extraction, not a re-typed guess."""
    lines = []
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except Exception:
            continue
        for item in (data if isinstance(data, list) else [data]):
            if not isinstance(item, dict):
                continue
            offers = item.get("offers")
            if offers is None:
                continue
            avail = ""
            if isinstance(offers, dict):
                avail = offers.get("availability", "")
                nested = offers.get("offers", [])
                if not avail and isinstance(nested, list):
                    for o in nested:
                        if isinstance(o, dict) and o.get("availability"):
                            avail = o["availability"]
                            break
            elif isinstance(offers, list):
                for o in offers:
                    if isinstance(o, dict) and o.get("availability"):
                        avail = o["availability"]
                        break
            lines.append(f"JSON-LD offers.availability = {avail!r}")
    if not lines:
        lines.append("No JSON-LD block with an 'offers' field found.")
    return lines


def _render_false_signal_report(site: str, soup: BeautifulSoup, html: str) -> list[str]:
    """Per-site signal-presence report, built from each checker module's
    OWN real constants (_IN_STOCK_PHRASE, _OOS_PHRASE, _OOS_PATTERNS,
    _ADD_PATTERNS) rather than hand-duplicated copies, so this can never
    silently drift from what check() actually looks for."""
    html_lower = html.lower()
    lines = []

    if site == "inventstore":
        visible_lower = inventstore._visible_text(html).lower()
        present = inventstore._IN_STOCK_PHRASE in visible_lower
        lines.append(
            f"'{inventstore._IN_STOCK_PHRASE}' phrase in visible text: "
            f"{'✅ present' if present else '❌ absent'}"
        )

    elif site == "tataneu":
        present = tataneu._OOS_PHRASE in html_lower
        lines.append(
            f"OOS phrase ({tataneu._OOS_PHRASE!r}) in raw HTML: "
            + ("✅ present (→ this checker reads it as OUT OF STOCK)" if present
               else "❌ absent (→ this checker reads it as IN STOCK — NEGATIVE "
                    "signal: a broken/incomplete fetch would also read as "
                    "IN STOCK, the riskier failure direction for this "
                    "specific checker)")
        )

    elif site in ("iqoo", "vivo"):
        module = iqoo if site == "iqoo" else vivo
        lines.extend(_jsonld_offers_report(soup))
        oos_hits = [p for p in module._OOS_PATTERNS if p in html_lower]
        lines.append(f"OOS text patterns present: {oos_hits or 'none'}")

    elif site in ("sangeethamobiles", "vijaysales"):
        module = sangeethamobiles if site == "sangeethamobiles" else vijaysales
        lines.extend(_jsonld_offers_report(soup))
        oos_hits = [p for p in module._OOS_PATTERNS if p in html_lower]
        add_hits = [p for p in module._ADD_PATTERNS if p in html_lower]
        lines.append(f"OOS text patterns present: {oos_hits or 'none'}")
        lines.append(f"Add-to-cart text patterns present: {add_hits or 'none'}")
        if site == "vijaysales":
            lines.append(
                "Note: vijaysales.check() INVERTS the generic waterfall's "
                "raw result (see checkers/vijaysales.py's bugfix comment) "
                "— the 'checker(soup, html) result' line above already "
                "reflects that inversion."
            )

    return lines


@router.message(Command("debugrenderfalse"))
async def cmd_debugrenderfalse(message: Message, command: CommandObject):
    if message.from_user.id != _DEBUG_RENDERFALSE_ADMIN_ID:
        return
    if not command.args:
        await message.answer(
            "Usage: <code>/debugrenderfalse &lt;site&gt; &lt;url&gt;</code>\n"
            f"site must be one of: {', '.join(sorted(_RENDER_FALSE_TEST_SITES))}",
            parse_mode="HTML",
        )
        return

    parts = command.args.strip().split(maxsplit=1)
    if len(parts) != 2:
        await message.answer(
            "Usage: <code>/debugrenderfalse &lt;site&gt; &lt;url&gt;</code>", parse_mode="HTML"
        )
        return
    site, url = parts[0].lower(), parts[1].strip()
    if site not in _RENDER_FALSE_TEST_SITES:
        await message.answer(
            f"⚠️ Unknown site {site!r}. Must be one of: "
            f"{', '.join(sorted(_RENDER_FALSE_TEST_SITES))}"
        )
        return

    await _debug_send(
        message, f"🔍 Fetching with render_js=False (plain HTTP, no browser) for {site}: {url}",
    )

    try:
        resp = await fetch_page(url, render_js=False, timeout=60.0)
    except Exception as exc:
        await _debug_send(message, f"⚠️ Fetch failed: {exc}")
        return

    html = resp.text
    soup = BeautifulSoup(html, "html.parser")
    text_soup = BeautifulSoup(html, "html.parser")
    for tag in text_soup(["script", "style"]):
        tag.decompose()
    visible_text = text_soup.get_text(" ", strip=True)

    try:
        checker_result = CHECKER_MAP[site](soup, html)
        checker_error = None
    except Exception as exc:
        checker_result = None
        checker_error = str(exc)

    blocked = stock_checker._looks_blocked_or_incomplete(html)

    lines = [
        f"— render_js=False result for {site} —",
        f"HTTP status: {resp.status_code}",
        f"Raw HTML length: {len(html)} chars",
        f"Visible text length: {len(visible_text)} chars",
        f"Looks blocked/incomplete (same heuristic the live check cycle uses): "
        f"{'⚠️ YES' if blocked else '✅ no'}",
    ]
    if checker_error:
        lines.append(f"⚠️ check(soup, html) CRASHED on this HTML: {checker_error}")
    else:
        lines.append(
            f"checker(soup, html) result: {'✅ IN STOCK' if checker_result else '❌ OUT OF STOCK'}"
        )
    lines.extend(_render_false_signal_report(site, soup, html))

    await _debug_send(message, "\n".join(lines))

    snippet = visible_text[:2000]
    await _debug_send(
        message, f"📄 Visible text (first {len(snippet)} of {len(visible_text)} chars):",
    )
    _CHUNK_SIZE = 3800
    for i in range(0, len(snippet), _CHUNK_SIZE):
        await _debug_send(message, snippet[i:i + _CHUNK_SIZE])


# ---------------------------------------------------------------------------
# /debugcroma — verify checkers/croma.py's new internal-inventory-API
# checker (itemID extraction from the tracked URL, the raw request sent,
# and Croma's raw response) against a real tracked Croma URL + pincode.
# The itemID-from-URL extraction specifically is a BEST GUESS (see
# checkers/croma.py's module docstring) — this command exists so that can
# be confirmed/corrected against real URLs before being trusted in
# production. NOT wired into CHECKER_MAP or the regular check cycle
# (check_via_api already IS the production path, called directly from
# stock_checker.check_stock()'s "croma" special case — this command is
# purely for manual verification, same purpose every other /debug* command
# in this file serves). Safe to delete once no longer needed.
# ---------------------------------------------------------------------------
_DEBUG_CROMA_ADMIN_ID = 5004721766  # same hardcoded restriction as every
# other /debug* command above, on top of the router's own ADMIN_USER_ID
# filter — this calls Croma's own inventory API for an arbitrary
# caller-supplied URL/pincode.


@router.message(Command("debugcroma"))
async def cmd_debugcroma(message: Message, command: CommandObject):
    if message.from_user.id != _DEBUG_CROMA_ADMIN_ID:
        return
    if not command.args:
        await message.answer(
            "Usage: <code>/debugcroma &lt;croma_url&gt; &lt;pincode&gt;</code>", parse_mode="HTML"
        )
        return

    parts = command.args.strip().split()
    if len(parts) < 2:
        await message.answer(
            "Usage: <code>/debugcroma &lt;croma_url&gt; &lt;pincode&gt;</code>", parse_mode="HTML"
        )
        return
    url, pincode = parts[0], parts[1]

    item_id = croma.extract_item_id(url)
    if not item_id:
        await _debug_send(
            message,
            "⚠️ Could not extract an itemID from this URL via the current "
            "'/p/<id>' pattern (see checkers/croma.py's module docstring — "
            "this extraction is a BEST GUESS, not verified against a real "
            "croma.com URL). Paste the real URL structure back so "
            "extract_item_id() can be corrected.",
        )
        return
    await _debug_send(message, f"✅ Extracted itemID: {item_id!r}")

    aff_key = croma._apim_key()
    await _debug_send(
        message,
        f"oms-apim-subscription-key: {'set via CROMA_APIM_KEY env var' if aff_key != croma._DEFAULT_APIM_KEY else 'using the built-in default (CROMA_APIM_KEY not set)'}",
    )

    await _debug_send(message, f"🔍 Calling Croma's inventory API for itemID={item_id!r} pincode={pincode!r}…")
    try:
        result = await croma.check_via_api(url, pincode)
    except Exception as exc:
        await _debug_send(message, f"⚠️ check_via_api crashed unexpectedly: {exc}")
        return

    if result is None:
        await _debug_send(
            message,
            "⚠️ Inconclusive (None) — the API call failed, the key was "
            "rejected (401/403 — check Railway logs for a clear 'key "
            "expired' message), or the response was malformed/non-JSON. "
            "See Railway logs for the exact reason.",
        )
        return

    await _debug_send(
        message,
        f"✅ Result: {'IN STOCK' if result else 'OUT OF STOCK'} "
        f"(promise.suggestedOption.option.promiseLines.promiseLine "
        f"{'non-empty' if result else 'empty/missing'} for pincode {pincode!r})",
    )


# ---------------------------------------------------------------------------
# /debugapplestores — verify checkers/apple.py's official-store pickup
# checker (checkers.apple.check_pickup_at_official_stores) against a real
# tracked Apple product URL, across all 6 fixed config.APPLE_PICKUP_PINCODES
# — no pincode argument needed, unlike /debugpickup. This is a THIRD,
# separate Apple signal from /debugpickup (the opt-in /trackpickup system's
# checker) — see checkers/apple.py's "official-store pickup checker"
# module note for how the three don't overlap. Exists specifically so
# real-world accuracy can be verified before
# config.APPLE_OFFICIAL_PICKUP_ALERTS_ENABLED is turned on. NOT wired into
# CHECKER_MAP or gated by CHECKER_MAP at all. Safe to delete once no
# longer needed.
# ---------------------------------------------------------------------------
_DEBUG_APPLE_STORES_ADMIN_ID = 5004721766  # same hardcoded restriction as
# every other /debug* command above, on top of the router's own
# ADMIN_USER_ID filter — this calls Apple's fulfillment-messages API 6
# times (once per official store) for an arbitrary caller-supplied URL.


@router.message(Command("debugapplestores"))
async def cmd_debugapplestores(message: Message, command: CommandObject):
    if message.from_user.id != _DEBUG_APPLE_STORES_ADMIN_ID:
        return
    if not command.args:
        await message.answer(
            "Usage: <code>/debugapplestores &lt;apple_url&gt;</code>", parse_mode="HTML"
        )
        return

    url = command.args.strip().split()[0]

    await _debug_send(message, f"🔍 Fetching product page (render={apple.NEEDS_JS}): {url}")
    try:
        resp = await fetch_page(url, render_js=apple.NEEDS_JS, timeout=30.0)
        resp.raise_for_status()
        html = resp.text
    except Exception as exc:
        await _debug_send(message, f"⚠️ Product page fetch failed: {exc}")
        return

    soup = BeautifulSoup(html, "html.parser")
    sku = apple._extract_sku(soup, html)
    if not sku:
        await _debug_send(
            message,
            "⚠️ Could not extract a SKU/part number from this page — cannot "
            "call the fulfillment-messages API without one.",
        )
        return
    await _debug_send(message, f"✅ Extracted SKU: {sku!r}")

    await _debug_send(
        message,
        f"🔍 Checking pickup availability across all {len(APPLE_PICKUP_PINCODES)} "
        f"official-store pincodes: {', '.join(APPLE_PICKUP_PINCODES)}\n"
        f"(alerts currently {'ENABLED' if APPLE_OFFICIAL_PICKUP_ALERTS_ENABLED else 'DISABLED'} "
        f"via config.APPLE_OFFICIAL_PICKUP_ALERTS_ENABLED)",
    )

    try:
        results = await apple.check_pickup_at_official_stores(sku, APPLE_PICKUP_PINCODES)
    except Exception as exc:
        await _debug_send(message, f"⚠️ check_pickup_at_official_stores crashed unexpectedly: {exc}")
        return

    lines = []
    for pincode in APPLE_PICKUP_PINCODES:
        label = APPLE_PICKUP_STORE_LABELS.get(pincode, pincode)
        if pincode not in results:
            lines.append(f"⚠️ {pincode} ({label}): request failed — see Railway logs")
            continue
        stores = results[pincode]
        if stores:
            store_desc = "; ".join(
                f"{s['store_name']}" + (f" ({s['location']})" if s.get("location") else "")
                for s in stores
            )
            lines.append(f"✅ {pincode} ({label}): AVAILABLE — {store_desc}")
        else:
            lines.append(f"❌ {pincode} ({label}): not available")
    await _debug_send(message, "\n".join(lines))
