"""SQLite database for watchlist storage."""

import sqlite3
import os

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DB_PATH = os.path.join(PROJECT_ROOT, "data", "app.db")


def get_connection():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_connection()
    conn.execute("""
    CREATE TABLE IF NOT EXISTS watchlist (
        stock_code TEXT PRIMARY KEY,
        stock_name TEXT,
        created_at TEXT DEFAULT (datetime('now', 'localtime'))
    )
    """)
    conn.commit()
    conn.close()


def get_watchlist():
    conn = get_connection()
    try:
        rows = conn.execute("SELECT * FROM watchlist ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def add_watchlist(stock_code, stock_name=""):
    conn = get_connection()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO watchlist (stock_code, stock_name) VALUES (?, ?)",
            (stock_code, stock_name)
        )
        conn.commit()
        return conn.total_changes > 0
    finally:
        conn.close()


def remove_watchlist(stock_code):
    conn = get_connection()
    try:
        conn.execute("DELETE FROM watchlist WHERE stock_code = ?", (stock_code,))
        conn.commit()
    finally:
        conn.close()
