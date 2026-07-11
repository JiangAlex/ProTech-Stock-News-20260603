"""US Index data service — fetch and store DJI, IXIC, SOX via yfinance."""

import logging
from datetime import datetime, timedelta
import psycopg2
from src.core.pg_client import DB_CONFIG

logger = logging.getLogger(__name__)

# Symbol mapping: display code -> Yahoo Finance ticker
US_INDICES = {
    "DJI": "^DJI",      # Dow Jones
    "IXIC": "^IXIC",    # NASDAQ Composite
    "SOX": "^SOX",      # Philadelphia Semiconductor
}


def _conn():
    return psycopg2.connect(**DB_CONFIG)


def fetch_and_store_us_index(symbol_key, period="2y"):
    """
    Fetch historical daily OHLCV for a US index and store in PostgreSQL.
    Uses Yahoo Finance chart API directly via urllib.
    symbol_key: "DJI", "IXIC", or "SOX"
    period: "1y", "2y", "5y", "max"
    """
    import urllib.request
    import json
    from datetime import datetime

    ticker = US_INDICES.get(symbol_key)
    if not ticker:
        logger.error(f"Unknown symbol key: {symbol_key}")
        return 0

    # URL encode the ^ symbol
    encoded_ticker = ticker.replace("^", "%5E")
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{encoded_ticker}?interval=1d&range={period}"
    headers = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"}

    logger.info(f"Fetching {symbol_key} ({ticker}) data for {period}...")
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        logger.error(f"Failed to fetch {symbol_key}: {e}")
        return 0

    result = data.get("chart", {}).get("result", [])
    if not result:
        logger.warning(f"No data returned for {symbol_key}")
        return 0

    timestamps = result[0].get("timestamp", [])
    quote = result[0].get("indicators", {}).get("quote", [{}])[0]
    opens = quote.get("open", [])
    highs = quote.get("high", [])
    lows = quote.get("low", [])
    closes = quote.get("close", [])
    volumes = quote.get("volume", [])

    if not timestamps:
        logger.warning(f"Empty timestamps for {symbol_key}")
        return 0

    conn = _conn()
    try:
        cur = conn.cursor()
        count = 0
        for i, ts in enumerate(timestamps):
            if ts is None or closes[i] is None:
                continue
            trade_date = datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d")
            cur.execute(
                """INSERT INTO us_index_kline (symbol, trade_date, open, high, low, close, volume)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (symbol, trade_date) DO UPDATE SET
                   open=EXCLUDED.open, high=EXCLUDED.high, low=EXCLUDED.low,
                   close=EXCLUDED.close, volume=EXCLUDED.volume""",
                (symbol_key, trade_date,
                 round(opens[i] or 0, 2), round(highs[i] or 0, 2),
                 round(lows[i] or 0, 2), round(closes[i] or 0, 2),
                 int(volumes[i] or 0))
            )
            count += 1
        conn.commit()
        logger.info(f"Stored {count} records for {symbol_key}")
        return count
    finally:
        conn.close()


def fetch_all_us_indices(period="2y"):
    """Fetch and store all US indices."""
    total = 0
    for key in US_INDICES:
        try:
            n = fetch_and_store_us_index(key, period)
            total += n
        except Exception as e:
            logger.error(f"Error fetching {key}: {e}")
    return total


def get_us_index_kline(symbol_key, days=120):
    """Get daily kline data for a US index from DB."""
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT trade_date, open, high, low, close, volume "
            "FROM us_index_kline WHERE symbol = %s "
            "ORDER BY trade_date DESC LIMIT %s",
            (symbol_key, days)
        )
        rows = cur.fetchall()
        return [
            {"date": r[0].isoformat(), "open": float(r[1] or 0),
             "high": float(r[2] or 0), "low": float(r[3] or 0),
             "close": float(r[4] or 0), "volume": int(r[5] or 0)}
            for r in reversed(rows)
        ]
    finally:
        conn.close()


def get_us_index_weekly(symbol_key, weeks=104):
    """Aggregate daily into weekly OHLCV."""
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT date_trunc('week', trade_date)::date AS week_start,
                   (array_agg(open ORDER BY trade_date))[1] AS open,
                   max(high) AS high, min(low) AS low,
                   (array_agg(close ORDER BY trade_date DESC))[1] AS close,
                   sum(volume) AS volume
            FROM us_index_kline WHERE symbol = %s
            GROUP BY week_start ORDER BY week_start DESC LIMIT %s
        """, (symbol_key, weeks))
        rows = cur.fetchall()
        return [
            {"date": r[0].isoformat(), "open": float(r[1] or 0),
             "high": float(r[2] or 0), "low": float(r[3] or 0),
             "close": float(r[4] or 0), "volume": int(r[5] or 0)}
            for r in reversed(rows)
        ]
    finally:
        conn.close()


def get_us_index_monthly(symbol_key, months=60):
    """Aggregate daily into monthly OHLCV."""
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT date_trunc('month', trade_date)::date AS month_start,
                   (array_agg(open ORDER BY trade_date))[1] AS open,
                   max(high) AS high, min(low) AS low,
                   (array_agg(close ORDER BY trade_date DESC))[1] AS close,
                   sum(volume) AS volume
            FROM us_index_kline WHERE symbol = %s
            GROUP BY month_start ORDER BY month_start DESC LIMIT %s
        """, (symbol_key, months))
        rows = cur.fetchall()
        return [
            {"date": r[0].isoformat(), "open": float(r[1] or 0),
             "high": float(r[2] or 0), "low": float(r[3] or 0),
             "close": float(r[4] or 0), "volume": int(r[5] or 0)}
            for r in reversed(rows)
        ]
    finally:
        conn.close()
