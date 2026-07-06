"""
dashboard.py
~~~~~~~~~~~~
Password-protected web admin dashboard — an ADDITIONAL, form-based way to do
what the Telegram admin commands already do. The bot and its /approve,
/block, etc. commands are untouched and keep working.

Runs in a background thread inside the bot's own process (see
start_dashboard_in_background, called from bot.py), so it shares the exact
same SQLite database file — no duplicated data, no separate service, no
Railway volume-sharing problem. Every DB operation calls the SAME
database.py / access.py functions the bot uses, so the two can never drift.

Write actions (approve/reject/block/extend, plan CRUD, broadcast) record the
audit trail under config.ADMIN_USER_ID (the dashboard operator is the admin)
and notify affected users via the Telegram HTTP API using the SAME message
text builders in notifications.py that the bot's own commands use — so an
action taken from the web reaches the user identically to one via Telegram.

Env vars:
    ADMIN_DASHBOARD_PASSWORD  required — dashboard won't start without it
    SECRET_KEY                recommended — stable Flask session signing key
    BOT_TOKEN                 used to send Telegram notifications/broadcasts
    PORT                      injected by Railway; the web server binds here
"""

import hmac
import logging
import os
import secrets
from collections import Counter
from datetime import datetime
from functools import wraps

import httpx
from flask import (
    Flask, abort, flash, get_flashed_messages, redirect, render_template,
    request, session, url_for,
)

from config import ADMIN_USER_ID, BOT_TOKEN, SUPPORTED_SITES
from access import (
    compute_access,
    STATUS_TRIAL,
    STATUS_ACTIVE,
    STATUS_EXPIRED_GRACE,
    STATUS_LOCKED,
)
from database import (
    IST,
    list_all_users,
    get_user,
    list_plans,
    list_products,
    list_pin_codes,
    get_all_products,
    get_approvals_since,
    get_approval_history,
    get_product_by_id,
    get_user_lang,
    remove_product,
    grant_access,
    reject_user,
    extend_access,
    set_blocked,
    add_plan,
    edit_plan,
    delete_plan,
    get_plan_by_id,
    list_global_site_locks,
    list_user_site_locks,
    set_global_site_lock,
    set_user_site_lock,
)
from notifications import (
    approval_notice_text,
    rejection_notice_text,
    block_notice_text,
    unblock_notice_text,
    items_removed_text,
)

logger = logging.getLogger(__name__)

STATUS_LABEL = {
    STATUS_TRIAL: "Trial",
    STATUS_ACTIVE: "Active",
    STATUS_EXPIRED_GRACE: "Expired (grace)",
    STATUS_LOCKED: "Locked",
}
STATUS_CLASS = {
    STATUS_TRIAL: "ok",
    STATUS_ACTIVE: "ok",
    STATUS_EXPIRED_GRACE: "warn",
    STATUS_LOCKED: "bad",
}

_EDITABLE_PLAN_FIELDS = (
    "name", "price", "max_items", "sites", "is_trial_plan", "is_active",
    "default_duration_days",
)


def _fmt_days(days):
    if days is None:
        return "—"
    if days < 0:
        return "expired"
    if days < 1:
        return f"{days * 24:.1f}h left"
    return f"{days:.1f}d left"


def _display_name(u: dict) -> str:
    # Identity precedence: @username → first name → bare ID. Point-1 fallback:
    # users who never set a public Telegram username show their first name
    # rather than just a numeric ID.
    if u.get("username"):
        return f"@{u['username']}"
    if u.get("first_name"):
        return u["first_name"]
    return str(u["user_id"])


def _tg_send(user_id: int, text: str) -> bool:
    """
    Send an HTML Telegram message via the Bot API directly (not the aiogram
    Bot object, which lives on the bot's event loop in another thread). Logs
    and swallows failures — a notification that fails (e.g. the user blocked
    the bot) must not fail the admin action that triggered it. Returns success.
    """
    if not BOT_TOKEN or BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        logger.warning("[dashboard] BOT_TOKEN not set — cannot send Telegram notification")
        return False
    try:
        resp = httpx.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": user_id, "text": text, "parse_mode": "HTML"},
            timeout=15.0,
        )
        if resp.status_code != 200:
            logger.error(f"[dashboard] Telegram send to {user_id} failed: {resp.status_code} {resp.text[:200]}")
            return False
        return True
    except Exception as exc:
        logger.error(f"[dashboard] Telegram send to {user_id} errored: {exc}")
        return False


def create_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
    if not os.environ.get("SECRET_KEY"):
        logger.warning(
            "[dashboard] SECRET_KEY not set — using a random per-start key; "
            "you'll be logged out on every restart. Set SECRET_KEY to persist sessions."
        )

    def _password() -> str:
        return os.environ.get("ADMIN_DASHBOARD_PASSWORD", "")

    def login_required(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not session.get("authed"):
                return redirect(url_for("login", next=request.path))
            return view(*args, **kwargs)
        return wrapped

    # ── CSRF: a per-session token embedded in every form, checked on POST ─────
    def _csrf_token() -> str:
        tok = session.get("csrf")
        if not tok:
            tok = secrets.token_hex(16)
            session["csrf"] = tok
        return tok

    @app.context_processor
    def _inject():
        return {"csrf_token": _csrf_token, "messages": get_flashed_messages(with_categories=True)}

    @app.before_request
    def _csrf_protect():
        if request.method == "POST" and request.endpoint != "login":
            sent = request.form.get("csrf")
            if not sent or not hmac.compare_digest(sent, session.get("csrf", "")):
                abort(400, "CSRF token missing or invalid")

    # ── Auth ─────────────────────────────────────────────────────────────────
    @app.route("/login", methods=["GET", "POST"])
    def login():
        error = None
        if request.method == "POST":
            supplied = request.form.get("password", "")
            expected = _password()
            if expected and hmac.compare_digest(supplied, expected):
                session["authed"] = True
                dest = request.args.get("next") or url_for("home")
                if not dest.startswith("/"):
                    dest = url_for("home")
                return redirect(dest)
            error = "Incorrect password."
            logger.warning("[dashboard] failed login attempt")
        return render_template("login.html", error=error)

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    # ── Read-only views ──────────────────────────────────────────────────────
    @app.route("/")
    @login_required
    def home():
        users = list_all_users()
        counts = Counter(compute_access(u).status for u in users)
        now = datetime.now(IST)
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        revenue = sum(a["amount"] or 0 for a in get_approvals_since(month_start))
        all_products = get_all_products()
        site_counts = Counter(p["site"] for p in all_products)
        top_store = site_counts.most_common(1)
        stats = {
            "total_users": len(users),
            "active": counts.get(STATUS_ACTIVE, 0),
            "trial": counts.get(STATUS_TRIAL, 0),
            "grace": counts.get(STATUS_EXPIRED_GRACE, 0),
            "locked": counts.get(STATUS_LOCKED, 0),
            "blocked": sum(1 for u in users if u.get("blocked")),
            "revenue": revenue,
            "total_products": len(all_products),
            "top_store": f"{top_store[0][0]} ({top_store[0][1]})" if top_store else "—",
            "active_plans": len(list_plans(active_only=True)),
        }
        return render_template("dashboard.html", stats=stats)

    # Maps a ?filter= value to a predicate over (AccessInfo, user_row). Powers
    # the clickable stats cards (point 3): each card links to /users?filter=X.
    _USER_FILTERS = {
        "active": lambda info, u: info.status == STATUS_ACTIVE,
        "trial": lambda info, u: info.status == STATUS_TRIAL,
        "grace": lambda info, u: info.status == STATUS_EXPIRED_GRACE,
        "locked": lambda info, u: info.status == STATUS_LOCKED,
        "blocked": lambda info, u: bool(u.get("blocked")),
    }
    _FILTER_LABEL = {
        "active": "Active (paid)", "trial": "In trial", "grace": "Expired (grace)",
        "locked": "Locked", "blocked": "Blocked",
    }

    @app.route("/users")
    @login_required
    def users():
        q = (request.args.get("q") or "").strip().lower()
        flt = request.args.get("filter") or ""
        predicate = _USER_FILTERS.get(flt)
        rows = []
        for u in list_all_users():
            info = compute_access(u)
            if predicate and not predicate(info, u):
                continue
            name = _display_name(u)
            uname = f"@{u['username']}" if u.get("username") else "—"
            if q and q not in str(u["user_id"]) and q not in name.lower() and q not in uname.lower():
                continue
            rows.append({
                "user_id": u["user_id"],
                "name": name,
                "username": uname,
                "status": STATUS_LABEL.get(info.status, info.status),
                "status_class": STATUS_CLASS.get(info.status, ""),
                "plan": info.plan["name"] if info.plan else "—",
                "days": _fmt_days(info.days_remaining),
                "blocked": bool(u.get("blocked")),
                "items": len(list_products(u["user_id"])),
            })
        return render_template(
            "users.html", rows=rows, q=request.args.get("q") or "", plans=list_plans(),
            filter=flt, filter_label=_FILTER_LABEL.get(flt),
        )

    @app.route("/user/<int:uid>")
    @login_required
    def user_detail(uid):
        u = get_user(uid)
        if u is None:
            flash(f"No user with id {uid}.", "bad")
            return redirect(url_for("users"))
        info = compute_access(u)
        products = list_products(uid)
        for p in products:
            p["status_label"] = "In stock" if p["in_stock"] else "Out of stock"
        detail = {
            "user_id": uid,
            "name": _display_name(u),
            "username": f"@{u['username']}" if u.get("username") else "—",
            "first_name": u.get("first_name") or "—",
            "joined": u.get("created_at") or "—",
            "plan": info.plan["name"] if info.plan else "—",
            "status": STATUS_LABEL.get(info.status, info.status),
            "status_class": STATUS_CLASS.get(info.status, ""),
            "access_until": u.get("access_until") or "—",
            "days": _fmt_days(info.days_remaining),
            "blocked": bool(u.get("blocked")),
            "pins": list_pin_codes(uid),
        }
        user_locked = list_user_site_locks(uid)
        global_locked = list_global_site_locks()
        site_locks_rows = [{
            "site": s,
            "user_locked": s in user_locked,
            "global_locked": s in global_locked,
        } for s in sorted(SUPPORTED_SITES.keys())]
        return render_template(
            "user_detail.html", d=detail, products=products,
            history=get_approval_history(uid), plans=list_plans(active_only=True),
            site_locks_rows=site_locks_rows,
        )

    @app.route("/pending")
    @login_required
    def pending():
        rows = []
        for u in list_all_users():
            if u.get("blocked"):
                continue
            info = compute_access(u)
            if info.status in (STATUS_TRIAL, STATUS_EXPIRED_GRACE):
                rows.append({
                    "user_id": u["user_id"],
                    "name": _display_name(u),
                    "kind": "Trial" if info.status == STATUS_TRIAL else "Awaiting approval (grace)",
                    "days": _fmt_days(info.days_remaining),
                    "grace_days": _fmt_days(info.grace_days_remaining) if info.grace_days_remaining else "—",
                })
            elif u.get("share_trial_requested"):
                # Completed the 5-round WhatsApp-share cycle but has no
                # access yet — surfaced here (distinct "kind" label) so the
                # admin can /approve or reject it like any other request.
                rows.append({
                    "user_id": u["user_id"],
                    "name": _display_name(u),
                    "kind": "Trial requested (via share)",
                    "days": _fmt_days(info.days_remaining),
                    "grace_days": "—",
                })
        return render_template("pending.html", rows=rows, plans=list_plans(active_only=True))

    @app.route("/plans")
    @login_required
    def plans():
        return render_template(
            "plans.html", plans=list_plans(), fields=_EDITABLE_PLAN_FIELDS,
            all_sites=sorted(SUPPORTED_SITES.keys()),
        )

    def _valid_uids(raw_list) -> list[int]:
        out = []
        for v in raw_list:
            try:
                out.append(int(v))
            except (TypeError, ValueError):
                continue
        return out

    @app.route("/broadcast")
    @login_required
    def broadcast():
        active = [u for u in list_all_users() if compute_access(u).has_access]
        return render_template("broadcast.html", active_count=len(active), target_uids=[], targets=None)

    @app.route("/broadcast/compose", methods=["POST"])
    @login_required
    def broadcast_compose():
        # Reached from the Users table's "Message selected" button — carries the
        # checked user_ids into the compose form as the target set.
        uids = _valid_uids(request.form.getlist("uids"))
        if not uids:
            flash("No users selected.", "bad")
            return redirect(url_for("users"))
        targets = [_display_name(u) for u in (get_user(i) for i in uids) if u]
        active = [u for u in list_all_users() if compute_access(u).has_access]
        return render_template(
            "broadcast.html", active_count=len(active), target_uids=uids, targets=targets,
        )

    # ── Write actions ────────────────────────────────────────────────────────
    def _require_user(uid: int) -> dict:
        u = get_user(uid)
        if u is None:
            flash(f"No user with id {uid}.", "bad")
        return u

    @app.route("/users/<int:uid>/block", methods=["POST"])
    @login_required
    def block_user(uid):
        if _require_user(uid) is None:
            return redirect(url_for("users"))
        if set_blocked(uid, True, ADMIN_USER_ID):
            _tg_send(uid, block_notice_text(get_user_lang(uid)))
            flash(f"User {uid} blocked.", "ok")
        else:
            flash(f"Could not block user {uid}.", "bad")
        return redirect(request.referrer or url_for("users"))

    @app.route("/users/<int:uid>/unblock", methods=["POST"])
    @login_required
    def unblock_user(uid):
        if _require_user(uid) is None:
            return redirect(url_for("users"))
        if set_blocked(uid, False, ADMIN_USER_ID):
            _tg_send(uid, unblock_notice_text(get_user_lang(uid)))
            flash(f"User {uid} unblocked.", "ok")
        else:
            flash(f"Could not unblock user {uid}.", "bad")
        return redirect(request.referrer or url_for("users"))

    @app.route("/users/<int:uid>/extend", methods=["POST"])
    @login_required
    def extend_user(uid):
        if _require_user(uid) is None:
            return redirect(url_for("users"))
        try:
            days = int(request.form.get("days", "").strip())
            if days <= 0:
                raise ValueError
        except ValueError:
            flash("Days must be a positive whole number.", "bad")
            return redirect(request.referrer or url_for("users"))
        updated = extend_access(uid, days, ADMIN_USER_ID)
        if updated:
            plan = get_plan_by_id(updated["plan_id"]) if updated.get("plan_id") else None
            _tg_send(uid, approval_notice_text(
                plan["name"] if plan else "your plan", days, updated["access_until"], get_user_lang(uid)))
            flash(f"Extended user {uid} by {days} day(s).", "ok")
        else:
            flash(f"Could not extend user {uid}.", "bad")
        return redirect(request.referrer or url_for("users"))

    @app.route("/pending/<int:uid>/approve", methods=["POST"])
    @login_required
    def approve_user(uid):
        if _require_user(uid) is None:
            return redirect(url_for("pending"))
        try:
            plan_id = int(request.form.get("plan_id", ""))
            days = int(request.form.get("days", "").strip())
            if days <= 0:
                raise ValueError
        except ValueError:
            flash("Pick a plan and a positive number of days.", "bad")
            return redirect(url_for("pending"))
        plan = get_plan_by_id(plan_id)
        if plan is None:
            flash("That plan no longer exists.", "bad")
            return redirect(url_for("pending"))
        updated = grant_access(uid, plan_id, days, ADMIN_USER_ID)
        _tg_send(uid, approval_notice_text(plan["name"], days, updated["access_until"], get_user_lang(uid)))
        flash(f"Approved user {uid} on {plan['name']} for {days} day(s).", "ok")
        return redirect(request.referrer or url_for("pending"))

    @app.route("/pending/<int:uid>/reject", methods=["POST"])
    @login_required
    def reject_user_route(uid):
        if _require_user(uid) is None:
            return redirect(url_for("pending"))
        reason = (request.form.get("reason") or "").strip() or None
        if reject_user(uid, ADMIN_USER_ID, reason):
            _tg_send(uid, rejection_notice_text(reason, get_user_lang(uid)))
            flash(f"Rejected user {uid}.", "ok")
        else:
            flash(f"Could not reject user {uid}.", "bad")
        return redirect(request.referrer or url_for("pending"))

    @app.route("/plans/add", methods=["POST"])
    @login_required
    def plan_add():
        name = (request.form.get("name") or "").strip()
        # Sites: "All stores" checkbox wins; otherwise the checked store list.
        # An empty selection with "all" unchecked is treated as "all" too
        # (a plan that allows no stores would be useless).
        if request.form.get("all_sites") or not request.form.getlist("sites"):
            sites = "all"
        else:
            sites = ",".join(request.form.getlist("sites"))
        is_trial_plan = bool(request.form.get("is_trial_plan"))
        is_active = bool(request.form.get("is_active"))
        try:
            price = float(request.form.get("price", "").strip())
            max_items = int(request.form.get("max_items", "").strip())
            duration = int(request.form.get("default_duration_days", "").strip())
            if not name or price < 0 or max_items <= 0 or duration <= 0:
                raise ValueError
        except ValueError:
            flash("Name, a non-negative price, a positive max-items, and a positive duration are required.", "bad")
            return redirect(url_for("plans"))
        ok, msg = add_plan(
            name, price, max_items, sites,
            default_duration_days=duration,
            is_trial_plan=is_trial_plan,
            is_active=is_active,
        )
        flash(msg, "ok" if ok else "bad")
        return redirect(url_for("plans"))

    @app.route("/plans/<int:pid>/edit", methods=["POST"])
    @login_required
    def plan_edit(pid):
        field = request.form.get("field", "")
        value = (request.form.get("value") or "").strip()
        if field not in _EDITABLE_PLAN_FIELDS:
            flash(f"Field '{field}' is not editable.", "bad")
            return redirect(url_for("plans"))
        ok, msg = edit_plan(pid, field, value)
        flash(msg, "ok" if ok else "bad")
        return redirect(url_for("plans"))

    @app.route("/plans/<int:pid>/delete", methods=["POST"])
    @login_required
    def plan_delete(pid):
        ok, msg = delete_plan(pid)
        flash(msg, "ok" if ok else "bad")
        return redirect(url_for("plans"))

    @app.route("/broadcast/send", methods=["POST"])
    @login_required
    def broadcast_send():
        text = (request.form.get("text") or "").strip()
        if not text:
            flash("Message can't be empty.", "bad")
            return redirect(request.referrer or url_for("broadcast"))
        # If explicit target uids are supplied (selective broadcast from the
        # Users table or a user's detail page), send only to those. Otherwise
        # send to all users with active access.
        uids = _valid_uids(request.form.getlist("uids"))
        if uids:
            recipients = [i for i in uids if get_user(i) is not None]
            scope = f"{len(recipients)} selected user(s)"
        else:
            recipients = [u["user_id"] for u in list_all_users() if compute_access(u).has_access]
            scope = f"{len(recipients)} active user(s)"
        sent = sum(1 for i in recipients if _tg_send(i, f"📣 {text}"))
        flash(f"Broadcast sent to {sent}/{len(recipients)} recipient(s) ({scope}).", "ok")
        return redirect(url_for("broadcast"))

    @app.route("/users/bulk-block", methods=["POST"])
    @login_required
    def users_bulk_block():
        uids = _valid_uids(request.form.getlist("uids"))
        if not uids:
            flash("No users selected.", "bad")
            return redirect(url_for("users"))
        done = 0
        for uid in uids:
            if get_user(uid) and set_blocked(uid, True, ADMIN_USER_ID):
                _tg_send(uid, block_notice_text(get_user_lang(uid)))
                done += 1
        flash(f"Blocked {done} user(s).", "ok")
        return redirect(request.referrer or url_for("users"))

    @app.route("/users/bulk-unblock", methods=["POST"])
    @login_required
    def users_bulk_unblock():
        uids = _valid_uids(request.form.getlist("uids"))
        if not uids:
            flash("No users selected.", "bad")
            return redirect(url_for("users"))
        done = 0
        for uid in uids:
            if get_user(uid) and set_blocked(uid, False, ADMIN_USER_ID):
                _tg_send(uid, unblock_notice_text(get_user_lang(uid)))
                done += 1
        flash(f"Unblocked {done} user(s).", "ok")
        return redirect(request.referrer or url_for("users"))

    @app.route("/pending/bulk-approve", methods=["POST"])
    @login_required
    def pending_bulk_approve():
        uids = _valid_uids(request.form.getlist("uids"))
        if not uids:
            flash("No users selected.", "bad")
            return redirect(url_for("pending"))
        try:
            plan_id = int(request.form.get("plan_id", ""))
            days = int(request.form.get("days", "").strip())
            if days <= 0:
                raise ValueError
        except ValueError:
            flash("Pick a plan and a positive number of days for the bulk approval.", "bad")
            return redirect(url_for("pending"))
        plan = get_plan_by_id(plan_id)
        if plan is None:
            flash("That plan no longer exists.", "bad")
            return redirect(url_for("pending"))
        done = 0
        for uid in uids:
            if get_user(uid) is None:
                continue
            updated = grant_access(uid, plan_id, days, ADMIN_USER_ID)
            _tg_send(uid, approval_notice_text(plan["name"], days, updated["access_until"], get_user_lang(uid)))
            done += 1
        flash(f"Approved {done} user(s) on {plan['name']} for {days} day(s).", "ok")
        return redirect(url_for("pending"))

    @app.route("/pending/bulk-reject", methods=["POST"])
    @login_required
    def pending_bulk_reject():
        uids = _valid_uids(request.form.getlist("uids"))
        if not uids:
            flash("No users selected.", "bad")
            return redirect(url_for("pending"))
        reason = (request.form.get("reason") or "").strip() or None
        done = 0
        for uid in uids:
            if get_user(uid) and reject_user(uid, ADMIN_USER_ID, reason):
                _tg_send(uid, rejection_notice_text(reason, get_user_lang(uid)))
                done += 1
        flash(f"Rejected {done} user(s).", "ok")
        return redirect(url_for("pending"))

    def _bulk_remove(pids, notify: bool, custom_message: str,
                     scope_uid: int | None = None, scope_site: str | None = None):
        """
        Remove the given product ids, grouped by owning user. scope_uid /
        scope_site restrict what may be removed (defence against a crafted
        request targeting products outside the page the button came from).
        When notify is set, each affected user gets ONE message: the admin's
        custom_message if provided (sent as-is), else the default items-removed
        notice listing that user's removed items. Returns (removed_count,
        notified_user_count).
        """
        by_user: dict[int, list[str]] = {}
        for pid in pids:
            prod = get_product_by_id(pid)
            if prod is None:
                continue
            if scope_uid is not None and prod["user_id"] != scope_uid:
                continue
            if scope_site is not None and prod["site"] != scope_site:
                continue
            if remove_product(prod["user_id"], pid):
                by_user.setdefault(prod["user_id"], []).append(prod["name"])
        removed = sum(len(v) for v in by_user.values())
        notified = 0
        if notify:
            msg = custom_message.strip() if custom_message and custom_message.strip() else None
            for owner, names in by_user.items():
                if _tg_send(owner, msg or items_removed_text(names, get_user_lang(owner))):
                    notified += 1
        return removed, notified

    @app.route("/user/<int:uid>/products/remove", methods=["POST"])
    @login_required
    def remove_user_products(uid):
        pids = _valid_uids(request.form.getlist("pids"))
        if not pids:
            flash("No items selected.", "bad")
            return redirect(url_for("user_detail", uid=uid))
        notify = "notify" in request.form
        removed, notified = _bulk_remove(
            pids, notify, request.form.get("message", ""), scope_uid=uid)
        if removed:
            note = f" and notified the user" if notify and notified else ""
            flash(f"Removed {removed} item(s){note}.", "ok")
        else:
            flash("Nothing was removed.", "bad")
        return redirect(url_for("user_detail", uid=uid))

    @app.route("/stores/<site>/remove", methods=["POST"])
    @login_required
    def remove_store_products(site):
        pids = _valid_uids(request.form.getlist("pids"))
        if not pids:
            flash("No items selected.", "bad")
            return redirect(url_for("store_detail", site=site))
        notify = "notify" in request.form
        removed, notified = _bulk_remove(
            pids, notify, request.form.get("message", ""), scope_site=site)
        if removed:
            note = f", notified {notified} user(s)" if notify else ""
            flash(f"Removed {removed} item(s) from {site.capitalize()}{note}.", "ok")
        else:
            flash("Nothing was removed.", "bad")
        return redirect(url_for("store_detail", site=site))

    # ── Store-wise breakdown ─────────────────────────────────────────────────
    @app.route("/stores")
    @login_required
    def stores():
        products = get_all_products()
        counts = Counter(p["site"] for p in products)
        # Union of officially-supported stores and any site actually present in
        # the DB (e.g. Croma items linger after Croma was pulled from
        # SUPPORTED_SITES), so nothing is hidden. Supported-but-empty stores
        # still appear with a 0.
        all_sites = sorted(set(SUPPORTED_SITES.keys()) | set(counts.keys()))
        rows = [{
            "site": s,
            "count": counts.get(s, 0),
            "supported": s in SUPPORTED_SITES,
        } for s in all_sites]
        rows.sort(key=lambda r: r["count"], reverse=True)
        return render_template("stores.html", rows=rows, total=len(products))

    @app.route("/stores/<site>")
    @login_required
    def store_detail(site):
        # display-name map built once to avoid a query per product
        name_by_id = {u["user_id"]: _display_name(u) for u in list_all_users()}
        items = []
        for p in get_all_products():
            if p["site"] != site:
                continue
            items.append({
                "id": p["id"],
                "name": p["name"],
                "url": p["url"],
                "in_stock": bool(p["in_stock"]),
                "user_id": p["user_id"],
                "user_name": name_by_id.get(p["user_id"], str(p["user_id"])),
            })
        items.sort(key=lambda i: i["name"].lower())
        return render_template("store_detail.html", site=site, items=items)

    # ── Site locks (global store restriction) ────────────────────────────────
    @app.route("/site-locks")
    @login_required
    def site_locks():
        locked = list_global_site_locks()
        counts = Counter(p["site"] for p in get_all_products())
        # Every supported store, plus any store already locked even if it's no
        # longer in SUPPORTED_SITES, so a lock is never hidden/orphaned.
        names = sorted(set(SUPPORTED_SITES.keys()) | locked)
        rows = [{
            "site": s,
            "locked": s in locked,
            "supported": s in SUPPORTED_SITES,
            "tracked": counts.get(s, 0),
        } for s in names]
        return render_template("site_locks.html", rows=rows)

    @app.route("/site-locks/toggle", methods=["POST"])
    @login_required
    def site_locks_toggle():
        site = (request.form.get("site") or "").strip().lower()
        if not site:
            flash("No store specified.", "bad")
            return redirect(url_for("site_locks"))
        lock = request.form.get("action") == "lock"
        set_global_site_lock(site, lock)
        flash(
            f"{site.capitalize()} is now {'locked for all users' if lock else 'unlocked'}.",
            "ok",
        )
        return redirect(url_for("site_locks"))

    @app.route("/user/<int:uid>/site-locks/toggle", methods=["POST"])
    @login_required
    def user_site_lock_toggle(uid):
        if _require_user(uid) is None:
            return redirect(url_for("users"))
        site = (request.form.get("site") or "").strip().lower()
        if not site:
            flash("No store specified.", "bad")
            return redirect(url_for("user_detail", uid=uid))
        lock = request.form.get("action") == "lock"
        set_user_site_lock(uid, site, lock)
        flash(
            f"{site.capitalize()} is now {'locked' if lock else 'unlocked'} for this user.",
            "ok",
        )
        return redirect(url_for("user_detail", uid=uid))

    return app


def start_dashboard_in_background() -> None:
    """
    Launch the dashboard on a daemon thread using waitress, bound to Railway's
    $PORT. No-ops (with a warning) if ADMIN_DASHBOARD_PASSWORD isn't set, so an
    existing deploy that hasn't configured the dashboard keeps behaving exactly
    as before. Never raises into the caller — a dashboard failure must not take
    the bot down with it.
    """
    if not os.environ.get("ADMIN_DASHBOARD_PASSWORD"):
        logger.warning(
            "[dashboard] ADMIN_DASHBOARD_PASSWORD not set — admin dashboard disabled. "
            "Set it (and ideally SECRET_KEY) to enable the web dashboard."
        )
        return

    import threading

    def _run():
        try:
            from waitress import serve
            port = int(os.environ.get("PORT", "8080"))
            app = create_app()
            logger.info(f"[dashboard] starting on 0.0.0.0:{port}")
            serve(app, host="0.0.0.0", port=port, threads=4)
        except Exception as exc:
            logger.error(f"[dashboard] failed to start (bot continues without it): {exc}")

    threading.Thread(target=_run, name="dashboard", daemon=True).start()
