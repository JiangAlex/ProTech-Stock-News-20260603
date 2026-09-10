"""PostgreSQL client for QuantStockDB."""

import os
import psycopg2
from psycopg2.extras import RealDictCursor

DB_CONFIG = {
    "host": os.getenv("PGHOST", "blog.softsnail.com"),
    "port": int(os.getenv("PGPORT", 1432)),
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
        cur.execute("SELECT stock_code, stock_name, market FROM stock_basic ORDER BY stock_code")
        return [{"code": r[0], "name": r[1], "market": r[2]} for r in cur.fetchall()]
    finally:
        conn.close()


def get_stock_market_map():
    """Return {stock_code: market} mapping. market is 'TSE' or 'TPEx'."""
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT stock_code, market FROM stock_basic WHERE market IS NOT NULL")
        return {r[0]: r[1] for r in cur.fetchall()}
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


def get_news_heat(stock_code: str, days: int = 7) -> dict:
    """Return news heat summary for a ticker over the past ``days`` days.

    Output shape::

        {
            "stock_code": "2330",
            "days": 7,
            "total_mentions": 12,         # distinct news rows mentioning the ticker
            "by_day":  [                   # intensity curve, one row per day
                {"date": "2026-09-04", "count": 3},
                ...
            ],
            "by_source": [                 # source distribution
                {"source": "moneydj_rss", "count": 5},
                ...
            ],
            "by_mention_type": [           # how the ticker was mentioned
                {"mention_type": "TITLE", "count": 4},
                ...
            ],
        }

    The query joins ``news_entity_map`` (denormalized ``ticker_code``) with
    ``news_raw`` so we can both count distinct news and group by source.

    If no news is found the response still includes the full ``days``-length
    zero-filled ``by_day`` array so the frontend can render an empty curve
    without special-casing.
    """
    if days is None or days <= 0:
        days = 7
    if days > 365:
        days = 365  # hard cap to keep response bounded

    conn = _conn()
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)

        # 1) Total distinct news count in window
        cur.execute(
            """
            SELECT COUNT(DISTINCT nem.news_id) AS total
            FROM news_entity_map nem
            JOIN news_raw n ON n.news_id = nem.news_id
            WHERE nem.ticker_code = %s
              AND n.published_at >= NOW() - (%s || ' days')::interval
            """,
            (stock_code, days),
        )
        total_row = cur.fetchone()
        total = int((total_row or {}).get("total") or 0)

        # 2) Per-day intensity curve — fill zero-days so frontend gets full window
        cur.execute(
            """
            SELECT (n.published_at AT TIME ZONE 'UTC')::date AS d,
                   COUNT(DISTINCT nem.news_id) AS cnt
            FROM news_entity_map nem
            JOIN news_raw n ON n.news_id = nem.news_id
            WHERE nem.ticker_code = %s
              AND n.published_at >= NOW() - (%s || ' days')::interval
            GROUP BY d
            ORDER BY d
            """,
            (stock_code, days),
        )
        by_day_rows = {r["d"].isoformat(): int(r["cnt"]) for r in cur.fetchall()}

        # build zero-filled series for the full window
        from datetime import date, timedelta
        today = date.today()
        by_day = []
        for offset in range(days - 1, -1, -1):
            d = (today - timedelta(days=offset)).isoformat()
            by_day.append({"date": d, "count": by_day_rows.get(d, 0)})

        # 3) Source distribution
        cur.execute(
            """
            SELECT n.source, COUNT(DISTINCT nem.news_id) AS cnt
            FROM news_entity_map nem
            JOIN news_raw n ON n.news_id = nem.news_id
            WHERE nem.ticker_code = %s
              AND n.published_at >= NOW() - (%s || ' days')::interval
            GROUP BY n.source
            ORDER BY cnt DESC
            """,
            (stock_code, days),
        )
        by_source = [
            {"source": r["source"], "count": int(r["cnt"])}
            for r in cur.fetchall()
        ]

        # 4) Mention-type distribution (TITLE / SUMMARY / BODY)
        cur.execute(
            """
            SELECT COALESCE(nem.mention_type, 'UNKNOWN') AS mention_type,
                   COUNT(*) AS cnt
            FROM news_entity_map nem
            JOIN news_raw n ON n.news_id = nem.news_id
            WHERE nem.ticker_code = %s
              AND n.published_at >= NOW() - (%s || ' days')::interval
            GROUP BY COALESCE(nem.mention_type, 'UNKNOWN')
            ORDER BY cnt DESC
            """,
            (stock_code, days),
        )
        by_mention_type = [
            {"mention_type": r["mention_type"], "count": int(r["cnt"])}
            for r in cur.fetchall()
        ]

        return {
            "stock_code": stock_code,
            "days": days,
            "total_mentions": total,
            "by_day": by_day,
            "by_source": by_source,
            "by_mention_type": by_mention_type,
        }
    finally:
        conn.close()
