import sqlite3
from config import DB_NAME

def get_connection():
    return sqlite3.connect(DB_NAME)

def init_db():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            name TEXT,
            url TEXT,
            platform TEXT,
            in_stock BOOLEAN DEFAULT 0
        )
    ''')
    conn.commit()
    conn.close()

def add_product(user_id: int, name: str, url: str, platform: str):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO products (user_id, name, url, platform) 
        VALUES (?, ?, ?, ?)
    ''', (user_id, name, url, platform))
    conn.commit()
    conn.close()

def get_user_products(user_id: int):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT id, name, url, platform, in_stock 
        FROM products WHERE user_id = ?
    ''', (user_id,))
    res = cursor.fetchall()
    conn.close()
    return res

def remove_product(user_id: int, product_id: int):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('DELETE FROM products WHERE id = ? AND user_id = ?', (product_id, user_id))
    conn.commit()
    conn.close()

def get_all_products():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT id, user_id, name, url, platform, in_stock FROM products')
    res = cursor.fetchall()
    conn.close()
    return res

def update_stock_status(product_id: int, in_stock: int):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('UPDATE products SET in_stock = ? WHERE id = ?', (in_stock, product_id))
    conn.commit()
    conn.close()

