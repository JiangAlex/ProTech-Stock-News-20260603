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
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]

    # --- watchlist ---
    if "watchlist" in tables:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(watchlist)").fetchall()]
        if "user_id" not in cols:
            conn.execute("ALTER TABLE watchlist ADD COLUMN user_id TEXT DEFAULT 'default'")
        if "buy_price" not in cols:
            conn.execute("ALTER TABLE watchlist ADD COLUMN buy_price REAL DEFAULT 0")
        if "buy_shares" not in cols:
            conn.execute("ALTER TABLE watchlist ADD COLUMN buy_shares INTEGER DEFAULT 0")
        if "buy_date" not in cols:
            conn.execute("ALTER TABLE watchlist ADD COLUMN buy_date TEXT DEFAULT ''")
        if "group_name" not in cols:
            conn.execute("ALTER TABLE watchlist ADD COLUMN group_name TEXT DEFAULT '預設'")
        if "note" not in cols:
            conn.execute("ALTER TABLE watchlist ADD COLUMN note TEXT DEFAULT ''")
        conn.commit()
        # Check if PK needs to be updated to composite (stock_code, user_id)
        pk_sql = conn.execute("SELECT sql FROM sqlite_master WHERE name='watchlist'").fetchone()[0]
        if "PRIMARY KEY (stock_code, user_id)" not in pk_sql:
            conn.execute("""CREATE TABLE IF NOT EXISTS watchlist_new (
                stock_code TEXT, stock_name TEXT, group_name TEXT DEFAULT '預設',
                note TEXT DEFAULT '', user_id TEXT DEFAULT 'default',
                buy_price REAL DEFAULT 0, buy_shares INTEGER DEFAULT 0, buy_date TEXT DEFAULT '',
                created_at TEXT DEFAULT (datetime('now', 'localtime')),
                PRIMARY KEY (stock_code, user_id))""")
            # Dynamically build SELECT based on existing cols
            cols = [r[1] for r in conn.execute("PRAGMA table_info(watchlist)").fetchall()]
            col_map = {
                'stock_code': 'stock_code',
                'stock_name': 'stock_name',
                'group_name': "COALESCE(group_name,'預設')" if 'group_name' in cols else "'預設'",
                'note': "COALESCE(note,'')" if 'note' in cols else "''",
                'user_id': "COALESCE(user_id,'default')" if 'user_id' in cols else "'default'",
                'buy_price': "COALESCE(buy_price,0)" if 'buy_price' in cols else "0",
                'buy_shares': "COALESCE(buy_shares,0)" if 'buy_shares' in cols else "0",
                'buy_date': "COALESCE(buy_date,'')" if 'buy_date' in cols else "''",
                'created_at': 'created_at' if 'created_at' in cols else "datetime('now','localtime')",
            }
            insert_cols = ', '.join(col_map.keys())
            select_cols = ', '.join(col_map.values())
            conn.execute(f"INSERT OR IGNORE INTO watchlist_new ({insert_cols}) SELECT {select_cols} FROM watchlist")
            conn.execute("DROP TABLE watchlist")
            conn.execute("ALTER TABLE watchlist_new RENAME TO watchlist")
            conn.commit()
    else:
        conn.execute("""CREATE TABLE watchlist (
            stock_code TEXT, stock_name TEXT, group_name TEXT DEFAULT '預設',
            note TEXT DEFAULT '', user_id TEXT DEFAULT 'default',
            buy_price REAL DEFAULT 0, buy_shares INTEGER DEFAULT 0, buy_date TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now', 'localtime')),
            PRIMARY KEY (stock_code, user_id))""")
        conn.commit()

    # --- watchlist_groups ---
    if "watchlist_groups" in tables:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(watchlist_groups)").fetchall()]
        if "user_id" not in cols:
            conn.execute("CREATE TABLE watchlist_groups_new (group_name TEXT, user_id TEXT DEFAULT 'default', PRIMARY KEY (group_name, user_id))")
            conn.execute("INSERT OR IGNORE INTO watchlist_groups_new SELECT group_name, 'default' FROM watchlist_groups")
            conn.execute("DROP TABLE watchlist_groups")
            conn.execute("ALTER TABLE watchlist_groups_new RENAME TO watchlist_groups")
            conn.commit()
    else:
        conn.execute("CREATE TABLE watchlist_groups (group_name TEXT, user_id TEXT DEFAULT 'default', PRIMARY KEY (group_name, user_id))")
        conn.commit()

    # --- watchlist_notes ---
    if "watchlist_notes" in tables:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(watchlist_notes)").fetchall()]
        if "user_id" not in cols:
            conn.execute("ALTER TABLE watchlist_notes ADD COLUMN user_id TEXT DEFAULT 'default'")
            conn.commit()
    else:
        conn.execute("""CREATE TABLE watchlist_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT, stock_code TEXT NOT NULL,
            content TEXT DEFAULT '', image_path TEXT DEFAULT '',
            user_id TEXT DEFAULT 'default',
            created_at TEXT DEFAULT (datetime('now', 'localtime')))""")
        conn.commit()

    # --- user_balance ---
    conn.execute("CREATE TABLE IF NOT EXISTS user_balance (user_id TEXT PRIMARY KEY, balance REAL DEFAULT 0)")
    conn.commit()

    # --- watchlist_trades ---
    conn.execute("""CREATE TABLE IF NOT EXISTS watchlist_trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        stock_code TEXT NOT NULL,
        user_id TEXT DEFAULT 'default',
        buy_price REAL NOT NULL,
        buy_shares INTEGER NOT NULL,
        buy_date TEXT DEFAULT '',
        created_at TEXT DEFAULT (datetime('now', 'localtime')))""")
    conn.commit()

    # --- rank_history ---
    conn.execute("""CREATE TABLE IF NOT EXISTS rank_history (
        date TEXT NOT NULL, direction TEXT NOT NULL, market TEXT NOT NULL,
        rank INTEGER NOT NULL, code TEXT NOT NULL, name TEXT,
        price REAL, change_val REAL, change_pct TEXT,
        PRIMARY KEY (date, direction, market, rank))""")
    conn.commit()
    conn.close()


# --- Watchlist ---

def get_watchlist(user_id="default"):
    conn = get_connection()
    try:
        rows = conn.execute("SELECT * FROM watchlist WHERE user_id = ? ORDER BY created_at DESC", (user_id,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def add_watchlist(stock_code, stock_name="", group_name="預設", user_id="default", buy_price=0, buy_shares=0, buy_date=""):
    conn = get_connection()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO watchlist (stock_code, stock_name, group_name, user_id, buy_price, buy_shares, buy_date) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (stock_code, stock_name, group_name, user_id, buy_price, buy_shares, buy_date))
        conn.commit()
    finally:
        conn.close()


def remove_watchlist(stock_code, user_id="default"):
    conn = get_connection()
    try:
        conn.execute("DELETE FROM watchlist WHERE stock_code = ? AND user_id = ?", (stock_code, user_id))
        conn.commit()
    finally:
        conn.close()


def move_watchlist(stock_code, group_name, user_id="default"):
    conn = get_connection()
    try:
        conn.execute("UPDATE watchlist SET group_name = ? WHERE stock_code = ? AND user_id = ?", (group_name, stock_code, user_id))
        conn.commit()
    finally:
        conn.close()


def update_cost(stock_code, buy_price, buy_shares, buy_date, user_id="default"):
    conn = get_connection()
    try:
        conn.execute("UPDATE watchlist SET buy_price=?, buy_shares=?, buy_date=? WHERE stock_code=? AND user_id=?",
                     (buy_price, buy_shares, buy_date, stock_code, user_id))
        conn.commit()
    finally:
        conn.close()


def sell_stock(stock_code, sell_shares, sell_price, user_id="default"):
    """Sell shares: remove from trades (FIFO), add proceeds to balance."""
    conn = get_connection()
    try:
        rows = conn.execute("SELECT id, buy_shares FROM watchlist_trades WHERE stock_code=? AND user_id=? ORDER BY buy_date ASC, id ASC",
                            (stock_code, user_id)).fetchall()
        remaining = sell_shares
        for row in rows:
            if remaining <= 0:
                break
            if row["buy_shares"] <= remaining:
                remaining -= row["buy_shares"]
                conn.execute("DELETE FROM watchlist_trades WHERE id=?", (row["id"],))
            else:
                conn.execute("UPDATE watchlist_trades SET buy_shares=? WHERE id=?", (row["buy_shares"] - remaining, row["id"]))
                remaining = 0
        proceeds = sell_price * sell_shares * 1000
        conn.execute("INSERT OR IGNORE INTO user_balance (user_id, balance) VALUES (?, 0)", (user_id,))
        conn.execute("UPDATE user_balance SET balance = balance + ? WHERE user_id = ?", (proceeds, user_id))
        conn.commit()
    finally:
        conn.close()


# --- Trades ---

def get_trades(stock_code, user_id="default"):
    conn = get_connection()
    try:
        rows = conn.execute("SELECT * FROM watchlist_trades WHERE stock_code=? AND user_id=? ORDER BY buy_date DESC, id DESC",
                            (stock_code, user_id)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_all_trades(user_id="default"):
    conn = get_connection()
    try:
        rows = conn.execute("SELECT * FROM watchlist_trades WHERE user_id=? ORDER BY stock_code, buy_date DESC",
                            (user_id,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def add_trade(stock_code, buy_price, buy_shares, buy_date="", user_id="default"):
    conn = get_connection()
    try:
        conn.execute("INSERT INTO watchlist_trades (stock_code, user_id, buy_price, buy_shares, buy_date) VALUES (?,?,?,?,?)",
                     (stock_code, user_id, buy_price, buy_shares, buy_date))
        conn.commit()
    finally:
        conn.close()


def delete_trade(trade_id, user_id="default"):
    conn = get_connection()
    try:
        conn.execute("DELETE FROM watchlist_trades WHERE id=? AND user_id=?", (trade_id, user_id))
        conn.commit()
    finally:
        conn.close()


# --- Notes ---

def get_notes(stock_code, user_id="default"):
    conn = get_connection()
    try:
        rows = conn.execute("SELECT * FROM watchlist_notes WHERE stock_code = ? AND user_id = ? ORDER BY created_at DESC", (stock_code, user_id)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def add_note(stock_code, content="", image_path="", user_id="default"):
    conn = get_connection()
    try:
        conn.execute("INSERT INTO watchlist_notes (stock_code, content, image_path, user_id) VALUES (?, ?, ?, ?)", (stock_code, content, image_path, user_id))
        conn.commit()
    finally:
        conn.close()


def delete_note(note_id, user_id="default"):
    conn = get_connection()
    try:
        row = conn.execute("SELECT image_path FROM watchlist_notes WHERE id = ? AND user_id = ?", (note_id, user_id)).fetchone()
        conn.execute("DELETE FROM watchlist_notes WHERE id = ? AND user_id = ?", (note_id, user_id))
        conn.commit()
        return row["image_path"] if row and row["image_path"] else ""
    finally:
        conn.close()


# --- Groups ---

def rename_group(old_name, new_name, user_id="default"):
    conn = get_connection()
    try:
        conn.execute("UPDATE watchlist SET group_name = ? WHERE group_name = ? AND user_id = ?", (new_name, old_name, user_id))
        conn.execute("UPDATE watchlist_groups SET group_name = ? WHERE group_name = ? AND user_id = ?", (new_name, old_name, user_id))
        conn.commit()
    finally:
        conn.close()


def delete_group(group_name, user_id="default"):
    conn = get_connection()
    try:
        conn.execute("DELETE FROM watchlist WHERE group_name = ? AND user_id = ?", (group_name, user_id))
        conn.execute("DELETE FROM watchlist_groups WHERE group_name = ? AND user_id = ?", (group_name, user_id))
        conn.commit()
    finally:
        conn.close()


def create_group(group_name, user_id="default"):
    conn = get_connection()
    try:
        conn.execute("INSERT OR IGNORE INTO watchlist_groups (group_name, user_id) VALUES (?, ?)", (group_name, user_id))
        conn.commit()
    finally:
        conn.close()


def get_all_groups(user_id="default"):
    conn = get_connection()
    try:
        rows = conn.execute("SELECT group_name FROM watchlist_groups WHERE user_id = ?", (user_id,)).fetchall()
        return [r[0] for r in rows]
    finally:
        conn.close()


# --- Balance ---

def get_balance(user_id="default"):
    conn = get_connection()
    try:
        conn.execute("INSERT OR IGNORE INTO user_balance (user_id, balance) VALUES (?, 0)", (user_id,))
        conn.commit()
        row = conn.execute("SELECT balance FROM user_balance WHERE user_id = ?", (user_id,)).fetchone()
        return row["balance"] if row else 0
    finally:
        conn.close()


def update_balance(user_id, amount):
    """Add amount to balance (positive=deposit, negative=withdraw)."""
    conn = get_connection()
    try:
        conn.execute("INSERT OR IGNORE INTO user_balance (user_id, balance) VALUES (?, 0)", (user_id,))
        conn.execute("UPDATE user_balance SET balance = balance + ? WHERE user_id = ?", (amount, user_id))
        conn.commit()
    finally:
        conn.close()


# --- Rank ---

def save_rank(date, direction, market, items):
    conn = get_connection()
    try:
        conn.execute("DELETE FROM rank_history WHERE date=? AND direction=? AND market=?", (date, direction, market))
        conn.executemany(
            "INSERT INTO rank_history (date,direction,market,rank,code,name,price,change_val,change_pct) VALUES (?,?,?,?,?,?,?,?,?)",
            [(date, direction, market, i+1, s["code"], s["name"], s["price"], s["change"], s["change_pct"]) for i, s in enumerate(items)])
        conn.commit()
    finally:
        conn.close()


def get_rank_history(date, direction, market="all"):
    conn = get_connection()
    try:
        if market == "all":
            rows = conn.execute("SELECT * FROM rank_history WHERE date=? AND direction=? ORDER BY rank", (date, direction)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM rank_history WHERE date=? AND direction=? AND market=? ORDER BY rank", (date, direction, market)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
