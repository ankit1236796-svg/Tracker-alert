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
from calendar import monthrange
from collections import Counter
from datetime import datetime
from urllib.parse import urlparse

import httpx
from aiogram import Router, F
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from bs4 import BeautifulSoup

from access import compute_access, STATUS_TRIAL, STATUS_ACTIVE, STATUS_EXPIRED_GRACE, STATUS_LOCKED
from checkers import build_scraper_url, HEADERS
from config import ADMIN_USER_ID, REMINDER_HOURS_BEFORE_EXPIRY, get_site_label
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
    """Fetch `url` via Scrape.do with the given build_scraper_url() kwargs
    and send a labeled diagnostic report: HTTP status, raw HTML length,
    where _RELIANCE_ANTIBOT_PHRASE was (or wasn't) found, then the FULL
    visible text chunked under Telegram's 4096-char limit (not truncated —
    unlike /debugoneplus, which still only sends the first 3000 chars).
    Any failure here is reported under this trial's own label and does not
    raise — safe to call multiple times/labels from the same command."""
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
