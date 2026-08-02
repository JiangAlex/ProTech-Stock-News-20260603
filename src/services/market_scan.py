"""Market Scan Service — compute indicators for all stocks and store in daily_indicators."""

import json
import logging
from datetime import date
from collections import defaultdict

import psycopg2
from psycopg2.extras import execute_values, RealDictCursor

from src.core.pg_client import DB_CONFIG
from src.services.kline_analysis import compute_all_indicators, detect_patterns

logger = logging.getLogger(__name__)


def init_daily_indicators_table():
    """Create daily_indicators table if not exists."""
    conn = psycopg2.connect(**DB_CONFIG)
    try:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS daily_indicators (
                stock_code TEXT NOT NULL,
                date DATE NOT NULL,
                close FLOAT,
                change_pct FLOAT,
                ma5 FLOAT, ma10 FLOAT, ma20 FLOAT, ma60 FLOAT,
                ma5_dir TEXT, ma10_dir TEXT, ma20_dir TEXT, ma60_dir TEXT,
                ma_arrangement TEXT,
                macd_dif FLOAT, macd_signal FLOAT, macd_histogram FLOAT,
                rsi14 FLOAT,
                boll_upper FLOAT, boll_middle FLOAT, boll_lower FLOAT, boll_bandwidth FLOAT,
                volume BIGINT, volume_ratio FLOAT, volume_trend TEXT,
                patterns JSONB DEFAULT '[]',
                change_rank INT,
                PRIMARY KEY (stock_code, date)
            )
        """)
        cur.execute("ALTER TABLE daily_indicators ADD COLUMN IF NOT EXISTS change_rank INT")
        # Index for common queries
        cur.execute("CREATE INDEX IF NOT EXISTS idx_di_date ON daily_indicators (date)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_di_rsi ON daily_indicators (date, rsi14)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_di_ma_arr ON daily_indicators (date, ma_arrangement)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_di_change_rank ON daily_indicators (date, change_rank)")
        conn.commit()
        logger.info("daily_indicators table ready")
    finally:
        conn.close()


def is_trading_day(target_date=None) -> bool:
    """Check if a date is a trading day by looking for K-line data in DB."""
    if target_date is None:
        target_date = date.today()
    # Weekend check
    if target_date.weekday() >= 5:
        return False
    conn = psycopg2.connect(**DB_CONFIG)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT 1 FROM daily_kline WHERE trade_date = %s LIMIT 1",
            (target_date,))
        return cur.fetchone() is not None
    finally:
        conn.close()


def compute_market_indicators(target_date=None) -> int:
    """Compute and store indicators for all stocks.

    Args:
        target_date: Date to compute for (default: today)

    Returns:
        Number of stocks processed successfully
    """
    if target_date is None:
        target_date = date.today()

    # Ensure table exists
    init_daily_indicators_table()

    # Step 1: Batch fetch all K-line data (last 180 days to have enough for MA60)
    conn = psycopg2.connect(**DB_CONFIG)
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT stock_code, trade_date, open, high, low, close, volume
            FROM daily_kline
            WHERE trade_date >= %s - INTERVAL '180 days'
              AND trade_date <= %s
            ORDER BY stock_code, trade_date
        """, (target_date, target_date))
        all_rows = cur.fetchall()
    finally:
        conn.close()

    if not all_rows:
        logger.warning(f"No K-line data found for {target_date}")
        return 0

    # Step 2: Group by stock_code
    grouped = defaultdict(list)
    for r in all_rows:
        grouped[r["stock_code"]].append({
            "date": r["trade_date"].isoformat(),
            "open": float(r["open"] or 0),
            "high": float(r["high"] or 0),
            "low": float(r["low"] or 0),
            "close": float(r["close"] or 0),
            "volume": int(r["volume"] or 0),
        })

    # Step 3: Compute indicators and prepare rows
    rows_to_insert = []
    for code, kline in grouped.items():
        if len(kline) < 20:
            continue
        # Only keep last 120 for computation
        kline = kline[-120:]

        # Check if last date matches target
        if kline[-1]["date"] != target_date.isoformat():
            continue

        try:
            indicators = compute_all_indicators(kline)
            if "error" in indicators:
                continue
            patterns = detect_patterns(kline)

            price = indicators.get("price", {})
            ma = indicators.get("ma", {})
            macd = indicators.get("macd", {})
            boll = indicators.get("bollinger", {})
            vol = indicators.get("volume", {})

            row = (
                code,
                target_date,
                price.get("current"),
                price.get("change_pct"),
                ma.get("ma5", {}).get("value"),
                ma.get("ma10", {}).get("value"),
                ma.get("ma20", {}).get("value"),
                ma.get("ma60", {}).get("value"),
                ma.get("ma5", {}).get("direction"),
                ma.get("ma10", {}).get("direction"),
                ma.get("ma20", {}).get("direction"),
                ma.get("ma60", {}).get("direction"),
                indicators.get("ma_arrangement"),
                macd.get("dif"),
                macd.get("macd"),
                macd.get("histogram"),
                indicators.get("rsi"),
                boll.get("upper"),
                boll.get("middle"),
                boll.get("lower"),
                boll.get("bandwidth"),
                vol.get("current_volume"),
                vol.get("volume_ratio"),
                vol.get("trend"),
                json.dumps([{"name": p["name"], "signal": p["signal"]} for p in patterns], ensure_ascii=False),
            )
            rows_to_insert.append(row)
        except Exception as e:
            logger.debug(f"Failed to compute indicators for {code}: {e}")

    if not rows_to_insert:
        logger.warning(f"No indicators computed for {target_date}")
        return 0

    # Step 4: Bulk insert with upsert
    conn = psycopg2.connect(**DB_CONFIG)
    try:
        cur = conn.cursor()
        execute_values(cur, """
            INSERT INTO daily_indicators (
                stock_code, date, close, change_pct,
                ma5, ma10, ma20, ma60,
                ma5_dir, ma10_dir, ma20_dir, ma60_dir,
                ma_arrangement,
                macd_dif, macd_signal, macd_histogram,
                rsi14,
                boll_upper, boll_middle, boll_lower, boll_bandwidth,
                volume, volume_ratio, volume_trend,
                patterns
            ) VALUES %s
            ON CONFLICT (stock_code, date) DO UPDATE SET
                close=EXCLUDED.close, change_pct=EXCLUDED.change_pct,
                ma5=EXCLUDED.ma5, ma10=EXCLUDED.ma10, ma20=EXCLUDED.ma20, ma60=EXCLUDED.ma60,
                ma5_dir=EXCLUDED.ma5_dir, ma10_dir=EXCLUDED.ma10_dir, ma20_dir=EXCLUDED.ma20_dir, ma60_dir=EXCLUDED.ma60_dir,
                ma_arrangement=EXCLUDED.ma_arrangement,
                macd_dif=EXCLUDED.macd_dif, macd_signal=EXCLUDED.macd_signal, macd_histogram=EXCLUDED.macd_histogram,
                rsi14=EXCLUDED.rsi14,
                boll_upper=EXCLUDED.boll_upper, boll_middle=EXCLUDED.boll_middle, boll_lower=EXCLUDED.boll_lower, boll_bandwidth=EXCLUDED.boll_bandwidth,
                volume=EXCLUDED.volume, volume_ratio=EXCLUDED.volume_ratio, volume_trend=EXCLUDED.volume_trend,
                patterns=EXCLUDED.patterns
        """, rows_to_insert)
        conn.commit()
        logger.info(f"Stored indicators for {len(rows_to_insert)} stocks on {target_date}")

        # Step 5: Compute change_rank (漲幅排名, 1=漲最多)
        cur.execute("""
            UPDATE daily_indicators di SET change_rank = sub.rn
            FROM (
                SELECT stock_code, ROW_NUMBER() OVER (ORDER BY change_pct DESC NULLS LAST) AS rn
                FROM daily_indicators WHERE date = %s AND change_pct IS NOT NULL
            ) sub
            WHERE di.stock_code = sub.stock_code AND di.date = %s
        """, (target_date, target_date))
        conn.commit()
        logger.info(f"Computed change_rank for {target_date}")
    finally:
        conn.close()

    return len(rows_to_insert)


def run_daily_scan():
    """Main entry point for daily scheduled scan."""
    today = date.today()

    if not is_trading_day(today):
        logger.info(f"{today} is not a trading day, skipping market scan")
        return 0

    logger.info(f"Starting market scan for {today}")
    count = compute_market_indicators(today)
    logger.info(f"Market scan complete: {count} stocks processed")
    return count
