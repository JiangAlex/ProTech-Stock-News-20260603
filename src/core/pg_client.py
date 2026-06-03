"""PostgreSQL client for QuantStockDB (read-only access)."""

import os
import logging
import psycopg2
from psycopg2.extras import RealDictCursor

logger = logging.getLogger(__name__)

DB_CONFIG = {
    "host": os.getenv("PGHOST", "blog.softsnail.com"),
    "port": int(os.getenv("PGPORT", 2432)),
    "user": os.getenv("PGUSER", "reef"),
    "password": os.getenv("PGPASSWORD", "accton123"),
    "database": os.getenv("PGDATABASE", "twsestock"),
}


def get_pg_connection():
    """Returns a PostgreSQL connection."""
    return psycopg2.connect(**DB_CONFIG)


def get_all_stocks():
    """Fetch all stock codes and names from stock_basic."""
    conn = get_pg_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT stock_code, stock_name FROM stock_basic ORDER BY stock_code")
        return cur.fetchall()
    finally:
        conn.close()


def get_daily_kline(stock_code, days=90):
    """Fetch recent daily kline data for a stock."""
    conn = get_pg_connection()
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            "SELECT trade_date, open, high, low, close, volume "
            "FROM daily_kline WHERE stock_code = %s "
            "ORDER BY trade_date DESC LIMIT %s",
            (stock_code, days),
        )
        rows = cur.fetchall()
        # Return in chronological order
        return [
            {
                "date": r["trade_date"].isoformat(),
                "open": float(r["open"]) if r["open"] else 0,
                "high": float(r["high"]) if r["high"] else 0,
                "low": float(r["low"]) if r["low"] else 0,
                "close": float(r["close"]) if r["close"] else 0,
                "volume": int(r["volume"]) if r["volume"] else 0,
            }
            for r in reversed(rows)
        ]
    finally:
        conn.close()
