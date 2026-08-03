"""PostgreSQL database for watchlist/trades/notes/balance/rank storage."""

import psycopg2
from psycopg2.extras import RealDictCursor
from src.core.pg_client import DB_CONFIG


def _conn():
    return psycopg2.connect(**DB_CONFIG)


def init_db():
    """Ensure schema is up-to-date (add missing columns)."""
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("ALTER TABLE watchlist_notes ADD COLUMN IF NOT EXISTS title TEXT DEFAULT NULL")
        cur.execute("ALTER TABLE watchlist_notes ADD COLUMN IF NOT EXISTS verification TEXT DEFAULT NULL")
        cur.execute("ALTER TABLE watchlist_notes ADD COLUMN IF NOT EXISTS verified_at TIMESTAMP DEFAULT NULL")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS analysis_preferences (
                user_id TEXT PRIMARY KEY,
                trading_style TEXT DEFAULT '',
                preferred_indicators TEXT DEFAULT '',
                risk_tolerance TEXT DEFAULT '',
                custom_prompt TEXT DEFAULT '',
                analysis_framework TEXT DEFAULT ''
            )
        """)
        cur.execute("ALTER TABLE analysis_preferences ADD COLUMN IF NOT EXISTS analysis_framework TEXT DEFAULT ''")
        cur.execute("ALTER TABLE analysis_preferences ADD COLUMN IF NOT EXISTS ma_tangle_threshold NUMERIC DEFAULT 3.0")
        conn.commit()
    finally:
        conn.close()


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
        cur.execute("SELECT id, stock_code, content, image_path, user_id, created_at, news_date, title, verification, (image_data IS NOT NULL) AS has_image FROM watchlist_notes WHERE stock_code = %s AND user_id = %s ORDER BY news_date DESC NULLS LAST, created_at DESC", (stock_code, user_id))
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def add_note(stock_code, content="", image_path="", user_id="default", image_data=None, news_date=None, image_filename="", title=""):
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO watchlist_notes (stock_code, content, image_path, user_id, image_data, news_date, image_filename, title) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (stock_code, content, image_path, user_id, psycopg2.Binary(image_data) if image_data else None, news_date, image_filename or None, title or None))
        note_id = cur.fetchone()[0]
        conn.commit()
        return note_id
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


def get_note_image(note_id):
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT image_data FROM watchlist_notes WHERE id = %s", (note_id,))
        row = cur.fetchone()
        return bytes(row[0]) if row and row[0] else None
    finally:
        conn.close()


def update_note_content(note_id, content, user_id="default", title=None):
    conn = _conn()
    try:
        cur = conn.cursor()
        if title is not None:
            cur.execute("UPDATE watchlist_notes SET content = %s, title = %s WHERE id = %s AND user_id = %s", (content, title, note_id, user_id))
        else:
            cur.execute("UPDATE watchlist_notes SET content = %s WHERE id = %s AND user_id = %s", (content, note_id, user_id))
        conn.commit()
    finally:
        conn.close()


def verify_note(note_id, verification, user_id="default"):
    """Mark a note as correct/incorrect. verification: 'correct' or 'incorrect' or None (clear)."""
    conn = _conn()
    try:
        cur = conn.cursor()
        if verification:
            cur.execute(
                "UPDATE watchlist_notes SET verification = %s, verified_at = NOW() WHERE id = %s AND user_id = %s",
                (verification, note_id, user_id))
        else:
            cur.execute(
                "UPDATE watchlist_notes SET verification = NULL, verified_at = NULL WHERE id = %s AND user_id = %s",
                (note_id, user_id))
        conn.commit()
    finally:
        conn.close()


def get_analysis_accuracy(stock_code, user_id="default"):
    """Get historical analysis accuracy for a stock (only AI analysis notes with verification)."""
    conn = _conn()
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT
                COUNT(*) FILTER (WHERE verification IS NOT NULL) AS total_verified,
                COUNT(*) FILTER (WHERE verification = 'correct') AS correct_count,
                COUNT(*) FILTER (WHERE verification = 'incorrect') AS incorrect_count
            FROM watchlist_notes
            WHERE stock_code = %s AND user_id = %s
              AND title LIKE '%%AI 技術分析%%'
              AND verification IS NOT NULL
        """, (stock_code, user_id))
        r = cur.fetchone()
        total = r["total_verified"] or 0
        correct = r["correct_count"] or 0
        if total == 0:
            return {"total": 0, "correct": 0, "incorrect": 0, "accuracy": None}
        return {
            "total": total,
            "correct": correct,
            "incorrect": r["incorrect_count"] or 0,
            "accuracy": round(correct / total * 100, 1)
        }
    finally:
        conn.close()


# --- Analysis Preferences ---

def get_analysis_preferences(user_id="default"):
    """Get user's AI analysis preferences."""
    conn = _conn()
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM analysis_preferences WHERE user_id = %s", (user_id,))
        row = cur.fetchone()
        if row:
            return dict(row)
        return {
            "user_id": user_id,
            "trading_style": "",
            "preferred_indicators": "",
            "risk_tolerance": "",
            "custom_prompt": "",
            "analysis_framework": "",
            "ma_tangle_threshold": 3.0,
        }
    finally:
        conn.close()


def update_analysis_preferences(user_id="default", trading_style="", preferred_indicators="",
                                risk_tolerance="", custom_prompt="", analysis_framework="",
                                ma_tangle_threshold=3.0):
    """Update user's AI analysis preferences."""
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO analysis_preferences (user_id, trading_style, preferred_indicators, risk_tolerance, custom_prompt, analysis_framework, ma_tangle_threshold)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (user_id) DO UPDATE SET
                trading_style=EXCLUDED.trading_style,
                preferred_indicators=EXCLUDED.preferred_indicators,
                risk_tolerance=EXCLUDED.risk_tolerance,
                custom_prompt=EXCLUDED.custom_prompt,
                analysis_framework=EXCLUDED.analysis_framework,
                ma_tangle_threshold=EXCLUDED.ma_tangle_threshold
        """, (user_id, trading_style, preferred_indicators, risk_tolerance, custom_prompt, analysis_framework, ma_tangle_threshold))
        conn.commit()
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


# --- Alerts ---

def get_alerts(user_id="default", stock_code=None):
    conn = _conn()
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        if stock_code:
            cur.execute("SELECT * FROM stock_alerts WHERE user_id = %s AND stock_code = %s ORDER BY created_at DESC", (user_id, stock_code))
        else:
            cur.execute("SELECT * FROM stock_alerts WHERE user_id = %s ORDER BY created_at DESC", (user_id,))
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def add_alert(stock_code, alert_type, params, repeat_mode="once", user_id="default"):
    import json
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO stock_alerts (stock_code, user_id, alert_type, params, repeat_mode) VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (stock_code, user_id, alert_type, json.dumps(params), repeat_mode))
        new_id = cur.fetchone()[0]
        conn.commit()
        return new_id
    finally:
        conn.close()


def update_alert(alert_id, user_id="default", **kwargs):
    import json
    conn = _conn()
    try:
        cur = conn.cursor()
        sets = []
        vals = []
        for k, v in kwargs.items():
            if k in ('alert_type', 'repeat_mode', 'enabled'):
                sets.append(f"{k} = %s")
                vals.append(v)
            elif k == 'params':
                sets.append("params = %s")
                vals.append(json.dumps(v))
        if not sets:
            return
        vals.extend([alert_id, user_id])
        cur.execute(f"UPDATE stock_alerts SET {', '.join(sets)} WHERE id = %s AND user_id = %s", vals)
        conn.commit()
    finally:
        conn.close()


def delete_alert(alert_id, user_id="default"):
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM stock_alerts WHERE id = %s AND user_id = %s", (alert_id, user_id))
        conn.commit()
    finally:
        conn.close()


def get_enabled_alerts():
    """Get all enabled alerts (for alert engine)."""
    conn = _conn()
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM stock_alerts WHERE enabled = true")
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def mark_alert_triggered(alert_id):
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE stock_alerts SET last_triggered = now() WHERE id = %s", (alert_id,))
        conn.commit()
    finally:
        conn.close()


def disable_alert(alert_id):
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE stock_alerts SET enabled = false WHERE id = %s", (alert_id,))
        conn.commit()
    finally:
        conn.close()


# --- Alert Settings ---

def get_alert_settings(user_id="default"):
    conn = _conn()
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM alert_settings WHERE user_id = %s", (user_id,))
        row = cur.fetchone()
        if row:
            result = dict(row)
        else:
            result = {"user_id": user_id, "run_time": "18:00", "telegram_chat_id": "", "telegram_bot_token": ""}
        # Fallback to env vars if DB values are empty
        import os
        if not result.get("telegram_bot_token"):
            result["telegram_bot_token"] = os.getenv("TELEGRAM_BOT_TOKEN", "")
        if not result.get("telegram_chat_id"):
            result["telegram_chat_id"] = os.getenv("TELEGRAM_CHAT_ID", "")
        return result
    finally:
        conn.close()


def update_alert_settings(user_id="default", run_time="18:00", telegram_chat_id="", telegram_bot_token=""):
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO alert_settings (user_id, run_time, telegram_chat_id, telegram_bot_token) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (user_id) DO UPDATE SET run_time=EXCLUDED.run_time, telegram_chat_id=EXCLUDED.telegram_chat_id, telegram_bot_token=EXCLUDED.telegram_bot_token",
            (user_id, run_time, telegram_chat_id, telegram_bot_token))
        conn.commit()
    finally:
        conn.close()


# --- News Weekly Digest ---

def init_news_digest_table():
    """Create news_weekly_digest table if not exists."""
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS news_weekly_digest (
                id SERIAL PRIMARY KEY,
                week_start DATE NOT NULL UNIQUE,
                week_end DATE NOT NULL,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        conn.commit()
    finally:
        conn.close()


def get_news_digests():
    """List all weekly digests (without full content)."""
    conn = _conn()
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT id, week_start, week_end, title, created_at
            FROM news_weekly_digest
            ORDER BY week_start DESC
        """)
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def get_news_digest_by_id(digest_id):
    """Get a single digest with full content."""
    conn = _conn()
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM news_weekly_digest WHERE id = %s", (digest_id,))
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def save_news_digest(week_start, week_end, title, content):
    """Save a weekly digest. Upsert by week_start."""
    conn = _conn()
    try:
        cur = conn.cursor()
        # Try upsert first
        cur.execute("""
            INSERT INTO news_weekly_digest (week_start, week_end, title, content)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (week_start) DO UPDATE SET
                week_end = EXCLUDED.week_end,
                title = EXCLUDED.title,
                content = EXCLUDED.content,
                created_at = NOW()
            RETURNING id
        """, (week_start, week_end, title, content))
        new_id = cur.fetchone()[0]
        conn.commit()
        return new_id
    finally:
        conn.close()


def get_week_news_notes(start_date, end_date, user_id=None):
    """Fetch news notes between start_date and end_date. If user_id is None, fetch all users."""
    conn = _conn()
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        if user_id:
            cur.execute("""
                SELECT id, content, news_date, title, created_at
                FROM watchlist_notes
                WHERE stock_code = 'NEWS' AND user_id = %s
                  AND (news_date >= %s AND news_date <= %s)
                ORDER BY news_date ASC, created_at ASC
            """, (user_id, start_date, end_date))
        else:
            cur.execute("""
                SELECT id, content, news_date, title, created_at
                FROM watchlist_notes
                WHERE stock_code = 'NEWS'
                  AND (news_date >= %s AND news_date <= %s)
                ORDER BY news_date ASC, created_at ASC
            """, (start_date, end_date))
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()
