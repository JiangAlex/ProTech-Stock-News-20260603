"""PostgreSQL client for QuantStockDB."""

import os
import psycopg2
from psycopg2.extras import RealDictCursor

DB_CONFIG = {
    "host": os.getenv("PGHOST", "blog.softsnail.com"),
    "port": int(os.getenv("PGPORT", 2432)),
    "user": os.getenv("PGUSER", "reef"),
    "password": os.getenv("PGPASSWORD", "accton123"),
    "database": os.getenv("PGDATABASE", "twsestock"),
}


def _conn():
    return psycopg2.connect(**DB_CONFIG)


def get_all_stocks():
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT stock_code, stock_name FROM stock_basic ORDER BY stock_code")
        return [{"code": r[0], "name": r[1]} for r in cur.fetchall()]
    finally:
        conn.close()


def get_daily_kline(stock_code, days=120):
    conn = _conn()
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            "SELECT trade_date, open, high, low, close, volume "
            "FROM daily_kline WHERE stock_code = %s "
            "ORDER BY trade_date DESC LIMIT %s",
            (stock_code, days),
        )
        return [
            {"date": r["trade_date"].isoformat(), "open": float(r["open"] or 0),
             "high": float(r["high"] or 0), "low": float(r["low"] or 0),
             "close": float(r["close"] or 0), "volume": int(r["volume"] or 0)}
            for r in reversed(cur.fetchall())
        ]
    finally:
        conn.close()


def get_weekly_kline(stock_code, weeks=52):
    """Aggregate daily kline into weekly OHLCV."""
    conn = _conn()
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT date_trunc('week', trade_date)::date AS week_start,
                   (array_agg(open ORDER BY trade_date))[1] AS open,
                   max(high) AS high, min(low) AS low,
                   (array_agg(close ORDER BY trade_date DESC))[1] AS close,
                   sum(volume) AS volume
            FROM daily_kline WHERE stock_code = %s
            GROUP BY week_start ORDER BY week_start DESC LIMIT %s
        """, (stock_code, weeks))
        return [
            {"date": r["week_start"].isoformat(), "open": float(r["open"] or 0),
             "high": float(r["high"] or 0), "low": float(r["low"] or 0),
             "close": float(r["close"] or 0), "volume": int(r["volume"] or 0)}
            for r in reversed(cur.fetchall())
        ]
    finally:
        conn.close()


def get_monthly_kline(stock_code, months=36):
    """Aggregate daily kline into monthly OHLCV."""
    conn = _conn()
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT date_trunc('month', trade_date)::date AS month_start,
                   (array_agg(open ORDER BY trade_date))[1] AS open,
                   max(high) AS high, min(low) AS low,
                   (array_agg(close ORDER BY trade_date DESC))[1] AS close,
                   sum(volume) AS volume
            FROM daily_kline WHERE stock_code = %s
            GROUP BY month_start ORDER BY month_start DESC LIMIT %s
        """, (stock_code, months))
        return [
            {"date": r["month_start"].isoformat(), "open": float(r["open"] or 0),
             "high": float(r["high"] or 0), "low": float(r["low"] or 0),
             "close": float(r["close"] or 0), "volume": int(r["volume"] or 0)}
            for r in reversed(cur.fetchall())
        ]
    finally:
        conn.close()


def get_annual_revenue(stock_code):
    """Get annual revenue trend (sum of monthly_revenue by year)."""
    conn = _conn()
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        # Try both TWSE and TPEx tables
        cur.execute("""
            SELECT left(year_month, 4) AS year, sum(revenue) AS revenue
            FROM (
                SELECT year_month, revenue FROM monthly_revenue WHERE stock_code = %s
                UNION ALL
                SELECT year_month, revenue FROM monthly_revenue_tpex WHERE stock_code = %s
            ) t
            GROUP BY year ORDER BY year
        """, (stock_code, stock_code))
        return [{"year": r["year"], "revenue": float(r["revenue"] or 0)} for r in cur.fetchall()]
    finally:
        conn.close()


def get_cumulative_eps(stock_code):
    """Get annual net profit (revenue * aftertax_margin) from quarterly_profit."""
    conn = _conn()
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT year, sum(revenue * aftertax_margin / 100) AS net_profit
            FROM quarterly_profit WHERE stock_code = %s
            GROUP BY year ORDER BY year
        """, (stock_code,))
        return [{"year": str(r["year"]), "eps": round(float(r["net_profit"] or 0), 2)} for r in cur.fetchall()]
    finally:
        conn.close()
