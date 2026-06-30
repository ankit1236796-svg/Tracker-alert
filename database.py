import os
import sqlite3
import logging
from typing import Optional
from config import DB_PATH

logger = logging.getLogger(__name__)


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
        conn.commit()
    logger.info(f"Database initialized at {DB_PATH}")


# ---------------------------------------------------------------------------
# Products
# ---------------------------------------------------------------------------

def add_product(user_id: int, name: str, url: str, site: str) -> tuple[bool, str]:
    try:
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO products (user_id, name, url, site) VALUES (?, ?, ?, ?)",
                (user_id, name, url, site),
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
            SET in_stock = ?, last_checked = datetime('now')
            WHERE id = ?
            """,
            (1 if in_stock else 0, product_id),
        )
        conn.commit()


def get_product_by_id(product_id: int) -> Optional[dict]:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM products WHERE id = ?", (product_id,)
        ).fetchone()
    return dict(row) if row else None


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
