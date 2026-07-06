"""
access.py
~~~~~~~~~
Access-control / monetization logic: trial + paid-plan status computation,
item-limit enforcement, and the aiogram middleware that gates every regular
(non-admin) command on the user's current access status.

Kept separate from database.py (pure read + a little math, no writes here
except the middleware's lazy user-row creation) and from handlers.py /
admin_handlers.py (both import from here) to avoid circular imports, mirroring
the existing notifications.py pattern in this codebase.
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Message, CallbackQuery

from config import ADMIN_USER_ID, GRACE_PERIOD_DAYS
from database import (
    IST,
    parse_ist,
    get_or_create_user,
    get_plan_by_id,
    get_user_lang,
    is_site_locked,
    list_products,
)
from translations import t

logger = logging.getLogger(__name__)

# Statuses shown in admin views. TRIAL and ACTIVE both mean "has access right
# now" — they're the same underlying condition (now <= access_until), split
# only by whether this access period originated from the free trial
# (is_trial=1) or a formal admin approval (is_trial=0, set by grant_access).
STATUS_TRIAL = "trial"
STATUS_ACTIVE = "active"
STATUS_EXPIRED_GRACE = "expired_grace"
STATUS_LOCKED = "locked"

# Commands regular (non-admin) users can always reach regardless of access
# status: /start so a brand-new user can create their trial row and so a
# locked user can see their status message, /cancel so nobody is ever
# trapped mid-FSM-flow by an access change, /freetrial since locked-out/
# expired users regaining access via a WhatsApp share is exactly this
# feature's point — gating it behind access they don't have would be
# circular — and /language so a brand-new, not-yet-approved user can pick
# their preferred language (see translations.LANGS for the full list)
# immediately during onboarding, before doing anything else (every message
# they'll see afterwards depends on it).
_ALWAYS_ALLOWED_COMMANDS = {"/start", "/cancel", "/freetrial", "/language"}

# Callback-data prefixes for button flows that must stay reachable for the
# same reason as the commands above — CallbackQuery events carry no `/command`
# text, so they aren't covered by the check above and need their own bypass.
# "setlang:" is the language-picker's callback (see _language_keyboard in
# handlers.py) — a locked user must be able to actually TAP a language, not
# just invoke /language, for the onboarding pick to work.
_ALWAYS_ALLOWED_CALLBACK_PREFIXES = ("freetrial:", "setlang:")


@dataclass
class AccessInfo:
    status: str            # one of the STATUS_* constants above
    has_access: bool        # True iff status in (trial, active)
    user_row: dict
    plan: dict | None
    days_remaining: float | None  # fractional days until access_until; None if never set
    grace_days_remaining: float | None  # only meaningful when status == expired_grace


def compute_access(user_row: dict, now: datetime | None = None) -> AccessInfo:
    """
    Pure function: derive the user's current access status from stored facts
    (access_until, blocked, is_trial) rather than a separately-stored status
    column, so status can never drift out of sync with the timestamps it's
    based on. `now` is injectable for testing.
    """
    now = now or datetime.now(IST)
    plan = get_plan_by_id(user_row["plan_id"]) if user_row.get("plan_id") else None

    if user_row.get("blocked"):
        return AccessInfo(STATUS_LOCKED, False, user_row, plan, None, None)

    access_until_raw = user_row.get("access_until")
    if not access_until_raw:
        return AccessInfo(STATUS_LOCKED, False, user_row, plan, None, None)

    access_until = parse_ist(access_until_raw)
    delta_seconds = (access_until - now).total_seconds()

    if delta_seconds > 0:
        status = STATUS_TRIAL if user_row.get("is_trial") else STATUS_ACTIVE
        return AccessInfo(status, True, user_row, plan, delta_seconds / 86400, None)

    grace_end = access_until.timestamp() + GRACE_PERIOD_DAYS * 86400
    grace_seconds_left = grace_end - now.timestamp()
    if grace_seconds_left > 0:
        return AccessInfo(
            STATUS_EXPIRED_GRACE, False, user_row, plan,
            delta_seconds / 86400, grace_seconds_left / 86400,
        )

    return AccessInfo(STATUS_LOCKED, False, user_row, plan, delta_seconds / 86400, None)


def get_access_info(
    user_id: int, username: str | None = None, first_name: str | None = None
) -> AccessInfo:
    """Fetch-or-create the user row and compute its current access status.
    Optional username/first_name are forwarded to get_or_create_user so the
    stored Telegram profile refreshes on every interaction (get_or_create_user
    preserves existing values when these are None). This is what lets usernames
    that were previously wiped to NULL recover the next time a user interacts."""
    user_row = get_or_create_user(user_id, username=username, first_name=first_name)
    return compute_access(user_row)


# ---------------------------------------------------------------------------
# Item-limit / site-restriction enforcement (used at /add time)
# ---------------------------------------------------------------------------

#: reason codes returned by check_can_add_item, so callers (e.g. bulk /add)
#: can react differently per failure type without parsing message text —
#: "item_limit" applies to every remaining item in a batch (stop entirely),
#: "site_not_allowed" only rules out this one item (skip and keep going).
REASON_NO_ACCESS = "no_access"
REASON_NO_PLAN = "no_plan"
REASON_ITEM_LIMIT = "item_limit"
REASON_SITE_NOT_ALLOWED = "site_not_allowed"
#: admin locked this store — globally or for this specific user. Like
#: site_not_allowed, it only rules out this one item (skip and keep going),
#: not the whole batch.
REASON_SITE_LOCKED = "site_locked"


def check_can_add_item(user_id: int, site: str) -> tuple[bool, str | None, str | None]:
    """
    Returns (allowed, reason_code, message). reason_code/message are None
    when allowed. Enforces the user's current plan's max_items and site
    restriction.

    The admin is exempt (mirrors AccessControlMiddleware's admin bypass) —
    without this, the admin's own /add would call get_access_info ->
    get_or_create_user and silently spin up a trial row + plan-limit
    enforcement for the admin, which makes no sense since they're not a
    monetized user and the middleware already lets them through unconditionally.
    """
    if user_id == ADMIN_USER_ID:
        return True, None, None

    info = get_access_info(user_id)
    lang = info.user_row.get("lang") or "en"
    if not info.has_access:
        return False, REASON_NO_ACCESS, access_denied_text(info)

    # Admin store lock (global or per-user) — checked before plan limits so a
    # locked store is refused regardless of the user's plan or remaining slots.
    if is_site_locked(site, user_id):
        return False, REASON_SITE_LOCKED, t("store_locked", lang, site=site.capitalize())

    plan = info.plan
    if plan is None:
        return False, REASON_NO_PLAN, t("no_active_plan", lang)

    current_count = len(list_products(user_id))
    if current_count >= plan["max_items"]:
        return False, REASON_ITEM_LIMIT, t(
            "item_limit", lang, plan=plan["name"], max=plan["max_items"], count=current_count)

    allowed_sites = plan["sites"]
    if allowed_sites != "all":
        allowed_list = {s.strip().lower() for s in allowed_sites.split(",") if s.strip()}
        if site.lower() not in allowed_list:
            return False, REASON_SITE_NOT_ALLOWED, t(
                "store_not_in_plan", lang, plan=plan["name"], sites=", ".join(sorted(allowed_list)))

    return True, None, None


# ---------------------------------------------------------------------------
# User-facing status/lockout messages
# ---------------------------------------------------------------------------

def access_denied_text(info: AccessInfo) -> str:
    lang = info.user_row.get("lang") or "en"
    if info.status == STATUS_LOCKED and info.user_row.get("blocked"):
        return t("access_blocked", lang)
    if info.status == STATUS_LOCKED and info.days_remaining is None:
        # access_until was never set at all — the normal state for a brand-new
        # user, since get_or_create_user no longer auto-grants a trial.
        return t("access_no_trial", lang)
    if info.status == STATUS_EXPIRED_GRACE:
        grace_left = int(info.grace_days_remaining) if info.grace_days_remaining else 0
        return t(
            "access_expired_grace", lang,
            grace=grace_left, s=("" if grace_left == 1 else "s"),
            payment=t("payment_instructions", lang),
        )
    return t("access_trial_ended", lang, payment=t("payment_instructions", lang))


# ---------------------------------------------------------------------------
# aiogram middleware — gates every event on the main (non-admin) router
# ---------------------------------------------------------------------------

class AccessControlMiddleware(BaseMiddleware):
    """
    Runs before every message/callback. The admin (ADMIN_USER_ID) always
    bypasses this entirely — their own access row, if any, is irrelevant.
    Regular users must have status in (trial, active) or the event is
    swallowed here with a status/payment message instead of reaching the
    handler. /start and /cancel are always allowed through so a locked user
    can see their status and nobody gets stuck mid-flow; /freetrial (and its
    "freetrial:"-prefixed callback buttons) is always allowed through too, so
    a locked-out/expired user can actually use the WhatsApp-share trial to
    regain access in the first place.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = None
        if isinstance(event, Message) and event.from_user:
            user = event.from_user
        elif isinstance(event, CallbackQuery) and event.from_user:
            user = event.from_user

        if user is None:
            return await handler(event, data)

        if user.id == ADMIN_USER_ID:
            return await handler(event, data)

        if isinstance(event, Message) and event.text:
            first_token = event.text.split()[0].split("@")[0]
            if first_token in _ALWAYS_ALLOWED_COMMANDS:
                return await handler(event, data)
        elif isinstance(event, CallbackQuery) and event.data:
            if event.data.startswith(_ALWAYS_ALLOWED_CALLBACK_PREFIXES):
                return await handler(event, data)

        info = get_access_info(user.id, username=user.username, first_name=user.first_name)
        if info.has_access:
            return await handler(event, data)

        logger.info(f"[access] blocking user {user.id} (status={info.status})")
        text = access_denied_text(info)
        try:
            if isinstance(event, Message):
                await event.answer(text, parse_mode="HTML")
            elif isinstance(event, CallbackQuery):
                await event.answer("Access required — see /start for details.", show_alert=True)
        except Exception as exc:
            logger.error(f"[access] failed to send lockout message to {user.id}: {exc}")
        return None
