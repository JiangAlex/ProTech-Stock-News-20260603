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
    conn.execute("""
    CREATE TABLE IF NOT EXISTS rank_history (
        date TEXT NOT NULL,
        direction TEXT NOT NULL,
        market TEXT NOT NULL,
        rank INTEGER NOT NULL,
        code TEXT NOT NULL,
        name TEXT,
        price REAL,
        change_val REAL,
        change_pct TEXT,
        PRIMARY KEY (date, direction, market, rank)
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


def save_rank(date, direction, market, items):
    """Save daily rank data. items: list of dicts from fetch_rank."""
    conn = get_connection()
    try:
        conn.execute("DELETE FROM rank_history WHERE date=? AND direction=? AND market=?", (date, direction, market))
        conn.executemany(
            "INSERT INTO rank_history (date,direction,market,rank,code,name,price,change_val,change_pct) VALUES (?,?,?,?,?,?,?,?,?)",
            [(date, direction, market, i+1, s["code"], s["name"], s["price"], s["change"], s["change_pct"]) for i, s in enumerate(items)]
        )
        conn.commit()
    finally:
        conn.close()


def get_rank_history(date, direction, market="all"):
    conn = get_connection()
    try:
        if market == "all":
            rows = conn.execute(
                "SELECT * FROM rank_history WHERE date=? AND direction=? ORDER BY rank",
                (date, direction)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM rank_history WHERE date=? AND direction=? AND market=? ORDER BY rank",
                (date, direction, market)
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
