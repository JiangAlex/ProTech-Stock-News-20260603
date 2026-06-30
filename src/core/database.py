"""PostgreSQL database for watchlist/trades/notes/balance/rank storage."""

import psycopg2
from psycopg2.extras import RealDictCursor
from src.core.pg_client import DB_CONFIG


def _conn():
    return psycopg2.connect(**DB_CONFIG)


def init_db():
    """Tables are pre-created in PostgreSQL. This is a no-op now."""
    pass


# --- Watchlist ---

def get_watchlist(user_id="default"):
    conn = _conn()
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM watchlist WHERE user_id = %s ORDER BY created_at DESC", (user_id,))
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def add_watchlist(stock_code, stock_name="", group_name="預設", user_id="default", buy_price=0, buy_shares=0, buy_date=""):
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO watchlist (stock_code, stock_name, group_name, user_id, buy_price, buy_shares, buy_date) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (stock_code, user_id) DO NOTHING",
            (stock_code, stock_name, group_name, user_id, buy_price, buy_shares, buy_date))
        conn.commit()
    finally:
        conn.close()


def remove_watchlist(stock_code, user_id="default"):
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM watchlist WHERE stock_code = %s AND user_id = %s", (stock_code, user_id))
        conn.commit()
    finally:
        conn.close()


def move_watchlist(stock_code, group_name, user_id="default"):
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE watchlist SET group_name = %s WHERE stock_code = %s AND user_id = %s", (group_name, stock_code, user_id))
        conn.commit()
    finally:
        conn.close()


def update_cost(stock_code, buy_price, buy_shares, buy_date, user_id="default"):
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE watchlist SET buy_price=%s, buy_shares=%s, buy_date=%s WHERE stock_code=%s AND user_id=%s",
                    (buy_price, buy_shares, buy_date, stock_code, user_id))
        conn.commit()
    finally:
        conn.close()


# --- Trades ---

def get_trades(stock_code, user_id="default"):
    conn = _conn()
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM watchlist_trades WHERE stock_code=%s AND user_id=%s ORDER BY buy_date DESC, id DESC",
                    (stock_code, user_id))
        rows = cur.fetchall()
        return [{**r, 'buy_price': float(r['buy_price'])} for r in rows]
    finally:
        conn.close()


def get_all_trades(user_id="default"):
    conn = _conn()
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM watchlist_trades WHERE user_id=%s ORDER BY stock_code, buy_date DESC", (user_id,))
        rows = cur.fetchall()
        return [{**r, 'buy_price': float(r['buy_price'])} for r in rows]
    finally:
        conn.close()


def add_trade(stock_code, buy_price, buy_shares, buy_date="", user_id="default"):
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("INSERT INTO watchlist_trades (stock_code, user_id, buy_price, buy_shares, buy_date) VALUES (%s,%s,%s,%s,%s)",
                    (stock_code, user_id, buy_price, buy_shares, buy_date))
        conn.commit()
    finally:
        conn.close()


def delete_trade(trade_id, user_id="default"):
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM watchlist_trades WHERE id=%s AND user_id=%s", (trade_id, user_id))
        conn.commit()
    finally:
        conn.close()


def sell_stock(stock_code, sell_shares, sell_price, user_id="default"):
    """Sell shares: remove from trades (FIFO), add proceeds to balance."""
    conn = _conn()
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT id, buy_shares FROM watchlist_trades WHERE stock_code=%s AND user_id=%s ORDER BY buy_date ASC, id ASC",
                    (stock_code, user_id))
        rows = cur.fetchall()
        remaining = sell_shares
        for row in rows:
            if remaining <= 0:
                break
            if row["buy_shares"] <= remaining:
                remaining -= row["buy_shares"]
                cur.execute("DELETE FROM watchlist_trades WHERE id=%s", (row["id"],))
            else:
                cur.execute("UPDATE watchlist_trades SET buy_shares=%s WHERE id=%s", (row["buy_shares"] - remaining, row["id"]))
                remaining = 0
        proceeds = sell_price * sell_shares * 1000
        cur.execute("INSERT INTO user_balance (user_id, balance) VALUES (%s, 0) ON CONFLICT (user_id) DO NOTHING", (user_id,))
        cur.execute("UPDATE user_balance SET balance = balance + %s WHERE user_id = %s", (proceeds, user_id))
        conn.commit()
    finally:
        conn.close()


# --- Notes ---

def get_notes(stock_code, user_id="default"):
    conn = _conn()
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM watchlist_notes WHERE stock_code = %s AND user_id = %s ORDER BY created_at DESC", (stock_code, user_id))
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def add_note(stock_code, content="", image_path="", user_id="default"):
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("INSERT INTO watchlist_notes (stock_code, content, image_path, user_id) VALUES (%s, %s, %s, %s)",
                    (stock_code, content, image_path, user_id))
        conn.commit()
    finally:
        conn.close()


def delete_note(note_id, user_id="default"):
    conn = _conn()
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT image_path FROM watchlist_notes WHERE id = %s AND user_id = %s", (note_id, user_id))
        row = cur.fetchone()
        cur.execute("DELETE FROM watchlist_notes WHERE id = %s AND user_id = %s", (note_id, user_id))
        conn.commit()
        return row["image_path"] if row and row["image_path"] else ""
    finally:
        conn.close()


# --- Groups ---

def rename_group(old_name, new_name, user_id="default"):
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE watchlist SET group_name = %s WHERE group_name = %s AND user_id = %s", (new_name, old_name, user_id))
        cur.execute("UPDATE watchlist_groups SET group_name = %s WHERE group_name = %s AND user_id = %s", (new_name, old_name, user_id))
        conn.commit()
    finally:
        conn.close()


def delete_group(group_name, user_id="default"):
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM watchlist WHERE group_name = %s AND user_id = %s", (group_name, user_id))
        cur.execute("DELETE FROM watchlist_groups WHERE group_name = %s AND user_id = %s", (group_name, user_id))
        conn.commit()
    finally:
        conn.close()


def create_group(group_name, user_id="default"):
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("INSERT INTO watchlist_groups (group_name, user_id) VALUES (%s, %s) ON CONFLICT (group_name, user_id) DO NOTHING",
                    (group_name, user_id))
        conn.commit()
    finally:
        conn.close()


def get_all_groups(user_id="default"):
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT group_name FROM watchlist_groups WHERE user_id = %s", (user_id,))
        return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


# --- Balance ---

def get_balance(user_id="default"):
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("INSERT INTO user_balance (user_id, balance) VALUES (%s, 0) ON CONFLICT (user_id) DO NOTHING", (user_id,))
        conn.commit()
        cur.execute("SELECT balance FROM user_balance WHERE user_id = %s", (user_id,))
        row = cur.fetchone()
        return float(row[0]) if row else 0
    finally:
        conn.close()


def update_balance(user_id, amount):
    """Add amount to balance (positive=deposit, negative=withdraw)."""
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("INSERT INTO user_balance (user_id, balance) VALUES (%s, 0) ON CONFLICT (user_id) DO NOTHING", (user_id,))
        cur.execute("UPDATE user_balance SET balance = balance + %s WHERE user_id = %s", (amount, user_id))
        conn.commit()
    finally:
        conn.close()


# --- Rank ---

def save_rank(date, direction, market, items):
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM rank_history WHERE date=%s AND direction=%s AND market=%s", (date, direction, market))
        for i, s in enumerate(items):
            cur.execute(
                "INSERT INTO rank_history (date,direction,market,rank,code,name,price,change_val,change_pct) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (date, direction, market, i+1, s["code"], s["name"], s["price"], s["change"], s["change_pct"]))
        conn.commit()
    finally:
        conn.close()


def get_rank_history(date, direction, market="all"):
    conn = _conn()
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        if market == "all":
            cur.execute("SELECT * FROM rank_history WHERE date=%s AND direction=%s ORDER BY rank", (date, direction))
        else:
            cur.execute("SELECT * FROM rank_history WHERE date=%s AND direction=%s AND market=%s ORDER BY rank", (date, direction, market))
        rows = cur.fetchall()
        return [{**r, 'price': float(r['price']) if r['price'] else 0, 'change_val': float(r['change_val']) if r['change_val'] else 0} for r in rows]
    finally:
        conn.close()
