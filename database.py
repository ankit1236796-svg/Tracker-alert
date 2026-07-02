import os
import sqlite3
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional
from config import DB_PATH, TRIAL_DAYS

logger = logging.getLogger(__name__)

# India Standard Time (UTC+5:30). SQLite's datetime('now') returns UTC, which
# was being stored and shown to users verbatim — 5:30 behind local time.
IST = timezone(timedelta(hours=5, minutes=30))


def now_ist_str() -> str:
    """Current time in IST as a 'YYYY-MM-DD HH:MM:SS' string for storage."""
    return datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")


def ist_str_plus(days: float = 0, hours: float = 0) -> str:
    """IST timestamp string offset by the given days/hours from now — used for
    trial/access expiry deadlines."""
    return (datetime.now(IST) + timedelta(days=days, hours=hours)).strftime("%Y-%m-%d %H:%M:%S")


def parse_ist(raw: str) -> datetime:
    """Parse a stored 'YYYY-MM-DD HH:MM:SS' IST string back into an aware datetime."""
    return datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST)


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Initialize the database schema, creating the data directory if needed."""
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

    with get_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS products (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id      INTEGER NOT NULL,
                name         TEXT    NOT NULL,
                url          TEXT    NOT NULL,
                site         TEXT    NOT NULL,
                in_stock     INTEGER NOT NULL DEFAULT 0,
                last_checked TEXT,
                created_at   TEXT    NOT NULL DEFAULT (datetime('now')),
                UNIQUE(user_id, url)
            )
        """)
        # Migration: add target_price column for Amazon price-gated alerts.
        # ALTER TABLE silently fails on older DBs that already have the column.
        try:
            conn.execute("ALTER TABLE products ADD COLUMN target_price REAL")
            conn.commit()
        except sqlite3.OperationalError:
            pass  # column already exists
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_products_user_id ON products(user_id)
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pin_codes (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL,
                pin_code   TEXT    NOT NULL,
                created_at TEXT    NOT NULL DEFAULT (datetime('now')),
                UNIQUE(user_id, pin_code)
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_pin_codes_user_id ON pin_codes(user_id)
        """)

        # ── Plans (admin-configurable, no code changes needed to adjust) ─────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS plans (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                name          TEXT    NOT NULL UNIQUE,
                price         REAL    NOT NULL,
                max_items     INTEGER NOT NULL,
                sites         TEXT    NOT NULL DEFAULT 'all',
                is_trial_plan INTEGER NOT NULL DEFAULT 0,
                is_active     INTEGER NOT NULL DEFAULT 1,
                created_at    TEXT    NOT NULL DEFAULT (datetime('now'))
            )
        """)
        # Seed default plan once; idempotent (INSERT OR IGNORE on UNIQUE name).
        # is_trial_plan=1 makes this the plan whose limits apply during the
        # free trial by default — admin can move that flag to a different
        # plan later via /editplan without any code change.
        existing_plan = conn.execute("SELECT 1 FROM plans WHERE name = 'Standard'").fetchone()
        if not existing_plan:
            conn.execute(
                """
                INSERT INTO plans (name, price, max_items, sites, is_trial_plan, created_at)
                VALUES ('Standard', 999, 20, 'all', 1, ?)
                """,
                (now_ist_str(),),
            )

        # ── Users (access/subscription state) ─────────────────────────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id             INTEGER PRIMARY KEY,
                username            TEXT,
                first_name          TEXT,
                plan_id             INTEGER,
                is_trial            INTEGER NOT NULL DEFAULT 1,
                access_until        TEXT,
                blocked             INTEGER NOT NULL DEFAULT 0,
                reminder_sent_until TEXT,
                created_at          TEXT    NOT NULL DEFAULT (datetime('now')),
                FOREIGN KEY (plan_id) REFERENCES plans(id)
            )
        """)

        # ── Approvals (full approve/reject/extend/block/unblock audit trail) ──
        conn.execute("""
            CREATE TABLE IF NOT EXISTS approvals (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL,
                plan_id    INTEGER,
                days       INTEGER,
                amount     REAL,
                action     TEXT    NOT NULL,
                reason     TEXT,
                admin_id   INTEGER NOT NULL,
                created_at TEXT    NOT NULL DEFAULT (datetime('now'))
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_approvals_user_id ON approvals(user_id)
        """)
        conn.commit()

        # Migration: every pre-existing user (found via products/pin_codes) who
        # doesn't yet have a `users` row is switched into the access-control
        # system "as if their trial just ended" — access_until = now, giving
        # the admin a full GRACE_PERIOD_DAYS window to review/approve each one
        # via /approve before their tracked items are purged. Nobody is
        # auto-grandfathered; is_trial=0 since they're past the trial framing.
        standard_row = conn.execute("SELECT id FROM plans WHERE name = 'Standard'").fetchone()
        standard_plan_id = standard_row["id"] if standard_row else None

        existing_user_ids = set()
        for row in conn.execute("SELECT DISTINCT user_id FROM products"):
            existing_user_ids.add(row["user_id"])
        for row in conn.execute("SELECT DISTINCT user_id FROM pin_codes"):
            existing_user_ids.add(row["user_id"])

        migrated = 0
        for uid in existing_user_ids:
            already = conn.execute("SELECT 1 FROM users WHERE user_id = ?", (uid,)).fetchone()
            if already:
                continue
            conn.execute(
                """
                INSERT INTO users (user_id, plan_id, is_trial, access_until, blocked, created_at)
                VALUES (?, ?, 0, ?, 0, ?)
                """,
                (uid, standard_plan_id, now_ist_str(), now_ist_str()),
            )
            migrated += 1
        conn.commit()
        if migrated:
            logger.info(
                f"Migrated {migrated} pre-existing user(s) into the access-control "
                f"system as expired/awaiting-approval — review via /pending or /approve"
            )
    logger.info(f"Database initialized at {DB_PATH}")


# ---------------------------------------------------------------------------
# Products
# ---------------------------------------------------------------------------

def add_product(
    user_id: int,
    name: str,
    url: str,
    site: str,
    target_price: float | None = None,
) -> tuple[bool, str]:
    try:
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO products (user_id, name, url, site, target_price) VALUES (?, ?, ?, ?, ?)",
                (user_id, name, url, site, target_price),
            )
            conn.commit()
        return True, "Product added successfully."
    except sqlite3.IntegrityError:
        return False, "You are already tracking this URL."
    except Exception as e:
        logger.error(f"add_product error: {e}")
        return False, "Database error while adding product."


def list_products(user_id: int) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM products WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def remove_product(user_id: int, product_id: int) -> bool:
    with get_connection() as conn:
        cursor = conn.execute(
            "DELETE FROM products WHERE id = ? AND user_id = ?",
            (product_id, user_id),
        )
        conn.commit()
    return cursor.rowcount > 0


def get_all_products() -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM products").fetchall()
    return [dict(row) for row in rows]


def update_stock_status(product_id: int, in_stock: bool):
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE products
            SET in_stock = ?, last_checked = ?
            WHERE id = ?
            """,
            (1 if in_stock else 0, now_ist_str(), product_id),
        )
        conn.commit()


def get_product_by_id(product_id: int) -> Optional[dict]:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM products WHERE id = ?", (product_id,)
        ).fetchone()
    return dict(row) if row else None


def search_products(user_id: int, keyword: str) -> list[dict]:
    """Case-insensitive partial match on product name, scoped to one user."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM products WHERE user_id = ? AND LOWER(name) LIKE ? ORDER BY created_at DESC",
            (user_id, f"%{keyword.lower()}%"),
        ).fetchall()
    return [dict(row) for row in rows]


def get_product_by_id_for_user(product_id: int, user_id: int) -> Optional[dict]:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM products WHERE id = ? AND user_id = ?",
            (product_id, user_id),
        ).fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Pin codes
# ---------------------------------------------------------------------------

def add_pin_code(user_id: int, pin_code: str) -> tuple[bool, str]:
    try:
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO pin_codes (user_id, pin_code) VALUES (?, ?)",
                (user_id, pin_code),
            )
            conn.commit()
        return True, "Pin code added."
    except sqlite3.IntegrityError:
        return False, "This pin code is already saved."
    except Exception as e:
        logger.error(f"add_pin_code error: {e}")
        return False, "Database error while adding pin code."


def remove_pin_code(user_id: int, pin_code: str) -> bool:
    with get_connection() as conn:
        cursor = conn.execute(
            "DELETE FROM pin_codes WHERE user_id = ? AND pin_code = ?",
            (user_id, pin_code),
        )
        conn.commit()
    return cursor.rowcount > 0


def list_pin_codes(user_id: int) -> list[str]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT pin_code FROM pin_codes WHERE user_id = ? ORDER BY created_at ASC",
            (user_id,),
        ).fetchall()
    return [row["pin_code"] for row in rows]


def get_user_primary_pincode(user_id: int) -> str | None:
    """Return the user's first saved pin code, or None if they have none."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT pin_code FROM pin_codes WHERE user_id = ? ORDER BY created_at ASC LIMIT 1",
            (user_id,),
        ).fetchone()
    return row["pin_code"] if row else None


# ---------------------------------------------------------------------------
# Plans
# ---------------------------------------------------------------------------

_EDITABLE_PLAN_FIELDS = {"name", "price", "max_items", "sites", "is_trial_plan", "is_active"}


def add_plan(name: str, price: float, max_items: int, sites: str) -> tuple[bool, str]:
    try:
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO plans (name, price, max_items, sites, created_at) VALUES (?, ?, ?, ?, ?)",
                (name, price, max_items, sites, now_ist_str()),
            )
            conn.commit()
        return True, "Plan created."
    except sqlite3.IntegrityError:
        return False, f"A plan named '{name}' already exists."
    except Exception as e:
        logger.error(f"add_plan error: {e}")
        return False, "Database error while creating plan."


def list_plans(active_only: bool = False) -> list[dict]:
    with get_connection() as conn:
        query = "SELECT * FROM plans"
        if active_only:
            query += " WHERE is_active = 1"
        query += " ORDER BY price ASC"
        rows = conn.execute(query).fetchall()
    return [dict(row) for row in rows]


def get_plan_by_id(plan_id: int) -> Optional[dict]:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()
    return dict(row) if row else None


def get_trial_plan() -> Optional[dict]:
    """The plan whose limits apply during the free trial (is_trial_plan=1).
    Falls back to the cheapest active plan if none is explicitly marked, so
    the system always has a usable trial plan even if misconfigured."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM plans WHERE is_trial_plan = 1 AND is_active = 1 LIMIT 1"
        ).fetchone()
        if row is None:
            row = conn.execute(
                "SELECT * FROM plans WHERE is_active = 1 ORDER BY price ASC LIMIT 1"
            ).fetchone()
    return dict(row) if row else None


def edit_plan(plan_id: int, field: str, value: str) -> tuple[bool, str]:
    if field not in _EDITABLE_PLAN_FIELDS:
        return False, f"Unknown field '{field}'. Editable: {', '.join(sorted(_EDITABLE_PLAN_FIELDS))}."
    try:
        with get_connection() as conn:
            # Only one plan may ever be flagged as the trial plan — setting
            # this one clears the flag on all others first.
            if field == "is_trial_plan" and str(value).lower() in ("1", "true", "yes"):
                conn.execute("UPDATE plans SET is_trial_plan = 0")
                value = 1
            cursor = conn.execute(f"UPDATE plans SET {field} = ? WHERE id = ?", (value, plan_id))
            conn.commit()
        if cursor.rowcount == 0:
            return False, f"No plan with id {plan_id}."
        return True, "Plan updated."
    except sqlite3.IntegrityError:
        return False, "That value conflicts with an existing plan (e.g. duplicate name)."
    except Exception as e:
        logger.error(f"edit_plan error: {e}")
        return False, "Database error while editing plan."


def delete_plan(plan_id: int) -> tuple[bool, str]:
    """Refuses to delete a plan that users are still assigned to, to avoid
    dangling references — reassign those users first via /setuserplan."""
    with get_connection() as conn:
        in_use = conn.execute(
            "SELECT COUNT(*) AS n FROM users WHERE plan_id = ?", (plan_id,)
        ).fetchone()["n"]
        if in_use:
            return False, f"{in_use} user(s) are on this plan — reassign them first with /setuserplan."
        cursor = conn.execute("DELETE FROM plans WHERE id = ?", (plan_id,))
        conn.commit()
    if cursor.rowcount == 0:
        return False, f"No plan with id {plan_id}."
    return True, "Plan deleted."


# ---------------------------------------------------------------------------
# Users / access control
# ---------------------------------------------------------------------------

def get_or_create_user(
    user_id: int, username: str | None = None, first_name: str | None = None
) -> dict:
    """
    Returns the user's access row, creating it with a fresh TRIAL_DAYS trial
    on first sight (called from /start). Keeps username/first_name current on
    every call since Telegram profiles can change.
    """
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
        if row is None:
            trial_plan = conn.execute(
                "SELECT id FROM plans WHERE is_trial_plan = 1 AND is_active = 1 LIMIT 1"
            ).fetchone()
            plan_id = trial_plan["id"] if trial_plan else None
            access_until = ist_str_plus(days=TRIAL_DAYS)
            conn.execute(
                """
                INSERT INTO users
                    (user_id, username, first_name, plan_id, is_trial, access_until, blocked, created_at)
                VALUES (?, ?, ?, ?, 1, ?, 0, ?)
                """,
                (user_id, username, first_name, plan_id, access_until, now_ist_str()),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
            logger.info(f"New user {user_id} started a {TRIAL_DAYS}-day trial until {access_until}")
        elif username != row["username"] or first_name != row["first_name"]:
            conn.execute(
                "UPDATE users SET username = ?, first_name = ? WHERE user_id = ?",
                (username, first_name, user_id),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
    return dict(row)


def get_user(user_id: int) -> Optional[dict]:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
    return dict(row) if row else None


def list_all_users() -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM users ORDER BY created_at DESC").fetchall()
    return [dict(row) for row in rows]


def set_user_plan(user_id: int, plan_id: int) -> bool:
    with get_connection() as conn:
        cursor = conn.execute("UPDATE users SET plan_id = ? WHERE user_id = ?", (plan_id, user_id))
        conn.commit()
    return cursor.rowcount > 0


def grant_access(user_id: int, plan_id: int | None, days: int, admin_id: int) -> dict:
    """
    Approve/extend a user's access. STACKS on remaining access rather than
    overwriting: if access_until is still in the future, `days` is added on
    top of it (so two 30-day gift cards correctly extend to 60 days from the
    current expiry); otherwise the new period starts from now. Clears
    `blocked`, sets is_trial=0 (formally approved, no longer just "trialing"),
    and resets reminder_sent_until so the expiry reminder can fire again for
    the new deadline. Records the approval and returns the updated user row.
    """
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
        now = datetime.now(IST)
        base = now
        if row and row["access_until"]:
            try:
                current_until = parse_ist(row["access_until"])
                if current_until > now:
                    base = current_until
            except ValueError:
                pass
        new_until = (base + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")

        if plan_id is None and row:
            plan_id = row["plan_id"]

        if row is None:
            conn.execute(
                """
                INSERT INTO users (user_id, plan_id, is_trial, access_until, blocked, created_at)
                VALUES (?, ?, 0, ?, 0, ?)
                """,
                (user_id, plan_id, new_until, now_ist_str()),
            )
        else:
            conn.execute(
                """
                UPDATE users
                SET plan_id = ?, is_trial = 0, access_until = ?, blocked = 0, reminder_sent_until = NULL
                WHERE user_id = ?
                """,
                (plan_id, new_until, user_id),
            )
        conn.commit()
        updated = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()

    plan = get_plan_by_id(plan_id) if plan_id else None
    amount = plan["price"] if plan else None
    _record_approval(user_id, plan_id, days, amount, "approve", None, admin_id)
    return dict(updated)


def reject_user(user_id: int, admin_id: int, reason: str | None = None) -> bool:
    row = get_user(user_id)
    if row is None:
        return False
    _record_approval(user_id, None, None, None, "reject", reason, admin_id)
    return True


def extend_access(user_id: int, days: int, admin_id: int) -> Optional[dict]:
    """Add days to the user's current access_until without changing plan."""
    row = get_user(user_id)
    if row is None:
        return None
    return grant_access(user_id, row["plan_id"], days, admin_id)


def set_blocked(user_id: int, blocked: bool, admin_id: int) -> bool:
    with get_connection() as conn:
        cursor = conn.execute(
            "UPDATE users SET blocked = ? WHERE user_id = ?", (1 if blocked else 0, user_id)
        )
        conn.commit()
    if cursor.rowcount == 0:
        return False
    _record_approval(user_id, None, None, None, "block" if blocked else "unblock", None, admin_id)
    return True


def mark_reminder_sent(user_id: int, access_until: str):
    with get_connection() as conn:
        conn.execute(
            "UPDATE users SET reminder_sent_until = ? WHERE user_id = ?",
            (access_until, user_id),
        )
        conn.commit()


def purge_user_data(user_id: int) -> int:
    """Permanently delete a user's tracked products (called once the grace
    period elapses with no renewal). Returns the number of products deleted."""
    with get_connection() as conn:
        cursor = conn.execute("DELETE FROM products WHERE user_id = ?", (user_id,))
        conn.commit()
    return cursor.rowcount


# ---------------------------------------------------------------------------
# Approvals (audit trail for approve/reject/extend/block/unblock)
# ---------------------------------------------------------------------------

def _record_approval(
    user_id: int,
    plan_id: int | None,
    days: int | None,
    amount: float | None,
    action: str,
    reason: str | None,
    admin_id: int,
):
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO approvals (user_id, plan_id, days, amount, action, reason, admin_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (user_id, plan_id, days, amount, action, reason, admin_id, now_ist_str()),
        )
        conn.commit()


def get_approval_history(user_id: int) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM approvals WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def get_approvals_since(since_str: str) -> list[dict]:
    """Approvals with action='approve' at/after the given IST timestamp string
    — used for /stats' revenue-this-month calculation."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM approvals WHERE action = 'approve' AND created_at >= ? ORDER BY created_at DESC",
            (since_str,),
        ).fetchall()
    return [dict(row) for row in rows]
