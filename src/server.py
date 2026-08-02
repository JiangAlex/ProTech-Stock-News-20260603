"""FastAPI server for ProTech Stock Dashboard."""

from dotenv import load_dotenv
load_dotenv()

import asyncio
import functools
import os

from fastapi import FastAPI, Query, UploadFile, File, Form
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pathlib import Path

from src.core.database import (
    init_db, get_watchlist, add_watchlist, remove_watchlist, move_watchlist,
    rename_group, delete_group, save_rank, get_rank_history, create_group,
    get_all_groups, get_notes, add_note, delete_note, get_note_image, update_cost, sell_stock,
    get_balance, update_balance, get_trades, get_all_trades, add_trade, delete_trade,
    get_alerts, add_alert, update_alert, delete_alert, get_alert_settings, update_alert_settings,
    update_note_content, verify_note, get_analysis_accuracy,
    get_analysis_preferences, update_analysis_preferences,
)
from src.core.pg_client import (
    get_all_stocks, get_daily_kline, get_weekly_kline, get_monthly_kline,
)
from src.services.yahoo_service import fetch_hot_stocks, fetch_revenue, fetch_dividend, fetch_rank
from src.services.kline_analysis import analyze_kline, get_analysis_history

app = FastAPI(title="ProTech Stock Dashboard", version="2.0.0")
TEMPLATES_DIR = Path(__file__).parent / "templates"
UPLOADS_DIR = Path(__file__).parent.parent / "data" / "uploads"
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=str(UPLOADS_DIR)), name="uploads")

# Limit concurrent OCR processes to 1 to prevent CPU/memory spikes
_ocr_semaphore = asyncio.Semaphore(1)

# Limit concurrent AI image analysis to 2
_ai_semaphore = asyncio.Semaphore(2)

# Max image dimension for OCR (downscale large images to save memory)
_OCR_MAX_PIXELS = 1600


def _run_ocr(image_data: bytes) -> str:
    """Synchronous OCR with image downscaling. Runs in thread pool."""
    import pytesseract
    from PIL import Image
    import io

    img = Image.open(io.BytesIO(image_data))
    # Downscale if image is too large
    w, h = img.size
    if max(w, h) > _OCR_MAX_PIXELS:
        ratio = _OCR_MAX_PIXELS / max(w, h)
        img = img.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)
    ocr_text = pytesseract.image_to_string(img, lang='chi_tra+eng').strip()
    return ocr_text[:200] if ocr_text else ""


@app.on_event("startup")
def startup():
    init_db()
    from src.core.database import init_news_digest_table
    init_news_digest_table()
    from src.services.market_scan import init_daily_indicators_table
    init_daily_indicators_table()
    import asyncio
    asyncio.get_event_loop().create_task(_daily_rank_job())
    asyncio.get_event_loop().create_task(_daily_alert_job())
    asyncio.get_event_loop().create_task(_daily_us_index_job())
    asyncio.get_event_loop().create_task(_daily_twii_job())
    asyncio.get_event_loop().create_task(_realtime_alert_job())
    asyncio.get_event_loop().create_task(_weekly_news_digest_job())
    asyncio.get_event_loop().create_task(_daily_market_scan_job())


async def _daily_rank_job():
    import asyncio
    from datetime import datetime, timedelta
    while True:
        now = datetime.now()
        target = now.replace(hour=17, minute=0, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())
        from datetime import date
        today = date.today().isoformat()
        for direction in ("up", "down"):
            for market in ("tse", "otc"):
                data = fetch_rank(direction, market)
                if data:
                    save_rank(today, direction, market, data)
        # Ensure at least 100 entries per direction by supplementing from daily_indicators
        try:
            _supplement_rank_from_indicators(today)
        except Exception as e:
            import logging
            logging.getLogger(__name__).error(f"Rank supplement error: {e}")


def _supplement_rank_from_indicators(today_str: str):
    """Ensure rank_history has at least 100 entries per direction+market by
    supplementing from daily_indicators if Yahoo didn't provide enough."""
    import psycopg2
    from psycopg2.extras import RealDictCursor
    from src.core.pg_client import DB_CONFIG

    conn = psycopg2.connect(**DB_CONFIG)
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        market_map = {"tse": "TSE", "otc": "TPEx"}

        for direction in ("up", "down"):
            for market_key, market_val in market_map.items():
                # Count existing entries
                cur.execute(
                    "SELECT COUNT(*) as cnt FROM rank_history WHERE date=%s AND direction=%s AND market=%s",
                    (today_str, direction, market_key))
                existing = cur.fetchone()["cnt"]

                if existing >= 100:
                    continue

                # Get existing codes to avoid duplicates
                cur.execute(
                    "SELECT code FROM rank_history WHERE date=%s AND direction=%s AND market=%s",
                    (today_str, direction, market_key))
                existing_codes = {r["code"] for r in cur.fetchall()}

                # Fetch from daily_indicators, ordered by change_pct
                order = "DESC" if direction == "up" else "ASC"
                cur.execute(f"""
                    SELECT di.stock_code, sb.stock_name, di.close, di.change_pct
                    FROM daily_indicators di
                    JOIN stock_basic sb ON sb.stock_code = di.stock_code
                    WHERE di.date = %s AND sb.market = %s
                      AND di.change_pct IS NOT NULL
                    ORDER BY di.change_pct {order}
                    LIMIT 100
                """, (today_str, market_val))
                candidates = cur.fetchall()

                # Insert missing entries up to 100
                rank_num = existing
                for c in candidates:
                    if rank_num >= 100:
                        break
                    if c["stock_code"] in existing_codes:
                        continue
                    rank_num += 1
                    cur.execute(
                        "INSERT INTO rank_history (date,direction,market,rank,code,name,price,change_val,change_pct) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                        (today_str, direction, market_key, rank_num,
                         c["stock_code"], c["stock_name"], c["close"],
                         round(c["change_pct"] * c["close"] / 100, 2) if c["close"] else 0,
                         f"{c['change_pct']:.2f}%"))
                    existing_codes.add(c["stock_code"])

        conn.commit()
    finally:
        conn.close()


# In-memory store for triggered alerts (for frontend polling)
_triggered_alerts = []


async def _daily_alert_job():
    import asyncio
    from datetime import datetime, timedelta
    while True:
        # Read run_time from settings (default 18:00)
        settings = get_alert_settings("default")
        run_time = settings.get("run_time", "18:00")
        try:
            h, m = map(int, run_time.split(":"))
        except:
            h, m = 18, 0
        now = datetime.now()
        target = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())
        # Run alert engine
        try:
            from src.services.alert_engine import run_alert_check
            from src.services.telegram_service import send_telegram_message
            results = run_alert_check()
            if results:
                # Group by user for Telegram
                user_msgs = {}
                for r in results:
                    _triggered_alerts.append(r)
                    uid = r["user_id"]
                    if uid not in user_msgs:
                        user_msgs[uid] = []
                    user_msgs[uid].append(r["message"])
                # Send Telegram per user
                for uid, msgs in user_msgs.items():
                    s = get_alert_settings(uid)
                    if s.get("telegram_bot_token") and s.get("telegram_chat_id"):
                        text = "🔔 <b>ProTech 警示通知</b>\n\n" + "\n".join(msgs)
                        send_telegram_message(s["telegram_bot_token"], s["telegram_chat_id"], text)
        except Exception as e:
            import logging
            logging.getLogger(__name__).error(f"Alert job error: {e}")


@app.get("/api/alerts/triggered")
def api_get_triggered(user: str = Query("default")):
    """Frontend polls this for toast/browser notifications."""
    results = [a for a in _triggered_alerts if a["user_id"] == user]
    # Clear after read
    for r in results:
        _triggered_alerts.remove(r)
    return results


async def _daily_us_index_job():
    """Fetch US index (DJI/IXIC/SOX) daily at 07:00 (after US market close)."""
    import asyncio
    from datetime import datetime, timedelta
    while True:
        now = datetime.now()
        target = now.replace(hour=7, minute=0, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())
        try:
            from src.services.us_index_service import fetch_and_store_us_index
            for sym in ("DJI", "IXIC", "SOX"):
                fetch_and_store_us_index(sym, "5d")
        except Exception as e:
            import logging
            logging.getLogger(__name__).error(f"US index job error: {e}")


async def _daily_twii_job():
    """Fetch TWII (台灣加權指數) daily at 18:00 (after TSE market close)."""
    import asyncio
    from datetime import datetime, timedelta
    while True:
        now = datetime.now()
        target = now.replace(hour=18, minute=0, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())
        try:
            from src.services.us_index_service import fetch_and_store_us_index
            fetch_and_store_us_index("TWII", "5d")
        except Exception as e:
            import logging
            logging.getLogger(__name__).error(f"TWII index job error: {e}")


async def _daily_market_scan_job():
    """Compute market indicators daily at 18:00 (only on trading days)."""
    import asyncio
    from datetime import datetime, timedelta
    import logging as _logging
    _logger = _logging.getLogger(__name__)
    while True:
        now = datetime.now()
        target = now.replace(hour=18, minute=0, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())
        try:
            from src.services.market_scan import run_daily_scan
            count = run_daily_scan()
            if count > 0:
                _logger.info(f"Market scan completed: {count} stocks")
            else:
                _logger.info("Market scan skipped (not a trading day or no data)")
        except Exception as e:
            _logger.error(f"Market scan job error: {e}")


async def _realtime_alert_job():
    """Intraday realtime alert check: 09:00-13:30, every 60 seconds."""
    import asyncio
    from datetime import datetime, timedelta
    import logging as _logging
    _logger = _logging.getLogger(__name__)

    # Cache market map (refreshed daily)
    market_map = {}
    last_map_date = None

    while True:
        now = datetime.now()
        # Only run on weekdays (Mon-Fri)
        if now.weekday() >= 5:
            # Wait until next Monday 09:00
            days_until_monday = 7 - now.weekday()
            next_open = now.replace(hour=9, minute=0, second=0, microsecond=0) + timedelta(days=days_until_monday)
            await asyncio.sleep((next_open - now).total_seconds())
            continue

        market_open = now.replace(hour=9, minute=0, second=0, microsecond=0)
        market_close = now.replace(hour=13, minute=30, second=0, microsecond=0)

        if now < market_open:
            # Wait until 09:00
            await asyncio.sleep((market_open - now).total_seconds())
            continue
        elif now > market_close:
            # Wait until next day 09:00
            next_open = market_open + timedelta(days=1)
            await asyncio.sleep((next_open - now).total_seconds())
            continue

        # Refresh market map once per day
        today_str = now.strftime("%Y-%m-%d")
        if last_map_date != today_str:
            try:
                from src.core.pg_client import get_stock_market_map
                market_map = get_stock_market_map()
                last_map_date = today_str
                _logger.info(f"Realtime alert: refreshed market map ({len(market_map)} stocks)")
            except Exception as e:
                _logger.error(f"Realtime alert: failed to load market map: {e}")

        # Get stock codes that have realtime-type alerts enabled
        try:
            from src.core.database import get_enabled_alerts
            from src.services.alert_engine import REALTIME_ALERT_TYPES, run_realtime_alert_check
            from src.services.realtime_quote import fetch_realtime_quotes_batch
            from src.services.telegram_service import send_telegram_message

            alerts = get_enabled_alerts()
            realtime_codes = list({a["stock_code"] for a in alerts if a["alert_type"] in REALTIME_ALERT_TYPES})

            if realtime_codes:
                quotes = fetch_realtime_quotes_batch(realtime_codes, market_map)
                if quotes:
                    results = run_realtime_alert_check(quotes)
                    if results:
                        # Push to in-memory store for frontend polling
                        user_msgs = {}
                        for r in results:
                            _triggered_alerts.append(r)
                            uid = r["user_id"]
                            if uid not in user_msgs:
                                user_msgs[uid] = []
                            user_msgs[uid].append(r["message"])
                        # Send Telegram per user
                        for uid, msgs in user_msgs.items():
                            s = get_alert_settings(uid)
                            if s.get("telegram_bot_token") and s.get("telegram_chat_id"):
                                text = "🔔 <b>ProTech 盤中即時警示</b>\n\n" + "\n".join(msgs)
                                send_telegram_message(s["telegram_bot_token"], s["telegram_chat_id"], text)
        except Exception as e:
            _logger.error(f"Realtime alert job error: {e}")

        # Sleep 60 seconds
        await asyncio.sleep(60)


async def _weekly_news_digest_job():
    """Generate weekly news digest every Sunday at 18:00."""
    import asyncio
    from datetime import datetime, timedelta
    import logging as _logging
    _logger = _logging.getLogger(__name__)

    while True:
        now = datetime.now()
        # Find next Sunday 18:00
        days_until_sunday = (6 - now.weekday()) % 7
        if days_until_sunday == 0 and now.hour >= 18:
            days_until_sunday = 7
        target = (now + timedelta(days=days_until_sunday)).replace(
            hour=18, minute=0, second=0, microsecond=0
        )
        wait_seconds = (target - now).total_seconds()
        _logger.info(f"Weekly digest job: next run at {target} (wait {wait_seconds:.0f}s)")
        await asyncio.sleep(wait_seconds)

        try:
            from src.services.news_digest import generate_weekly_digest
            result = generate_weekly_digest()
            if result:
                _logger.info(f"Weekly digest generated: {result['title']}")
            else:
                _logger.info("Weekly digest: no news to summarize this week")
        except Exception as e:
            _logger.error(f"Weekly digest job error: {e}")


# --- US Index API ---

@app.get("/api/usindex/{symbol}/kline")
def api_us_index_kline(symbol: str, period: str = Query("daily"), days: int = Query(120)):
    from src.services.us_index_service import get_us_index_kline, get_us_index_weekly, get_us_index_monthly
    sym = symbol.upper().replace("^", "")
    if period == "weekly":
        return get_us_index_weekly(sym, days)
    elif period == "monthly":
        return get_us_index_monthly(sym, days)
    return get_us_index_kline(sym, days)


@app.get("/", response_class=HTMLResponse)
def index():
    return (TEMPLATES_DIR / "index.html").read_text(encoding="utf-8")


# --- Stock data ---

@app.get("/api/stocks")
def api_stocks():
    return get_all_stocks()


@app.get("/api/stock/{code}/kline")
def api_kline(code: str, period: str = Query("daily"), days: int = Query(120, le=500)):
    if period == "weekly":
        return get_weekly_kline(code, days)
    elif period == "monthly":
        return get_monthly_kline(code, days)
    return get_daily_kline(code, days)


@app.post("/api/stock/{code}/ai-analysis")
def api_ai_analysis(code: str, period: str = Query("daily"), days: int = Query(120, le=500),
                    user: str = Query("default")):
    """AI technical analysis for a stock's K-line data."""
    # 1. Get K-line data
    if period == "weekly":
        kline_data = get_weekly_kline(code, days)
    elif period == "monthly":
        kline_data = get_monthly_kline(code, days)
    else:
        kline_data = get_daily_kline(code, days)

    if not kline_data or len(kline_data) < 5:
        return {"error": "K線資料不足，無法分析"}

    # 2. Get stock name
    stock_name = ""
    if not hasattr(api_ai_analysis, '_stock_map'):
        try:
            stocks = get_all_stocks()
            api_ai_analysis._stock_map = {s["code"]: s["name"] for s in stocks}
        except Exception:
            api_ai_analysis._stock_map = {}
    stock_name = api_ai_analysis._stock_map.get(code, code)

    # 3. Run analysis
    result = analyze_kline(code, stock_name, kline_data, period, user)

    # 4. Auto-save to notes
    note_id = None
    if result.get("analysis") and not result.get("error"):
        try:
            period_name = {"daily": "日線", "weekly": "週線", "monthly": "月線"}.get(period, period)
            title = f"🤖 AI 技術分析 ({period_name})"
            content = f"【{code} {stock_name} {period_name}技術分析】\n\n{result['analysis']}"
            from datetime import date
            note_id = add_note(
                stock_code=code,
                content=content,
                user_id=user,
                title=title,
                news_date=date.today().isoformat(),
            )
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(f"Failed to save AI analysis note: {e}")
    result["note_id"] = note_id

    # 5. Also inject into semantic_search conversation history for follow-up
    try:
        from src.services.semantic_search import _conversation_history
        if user not in _conversation_history:
            _conversation_history[user] = []
        summary = f"[{code} {stock_name} 技術分析] {result.get('analysis', '')[:800]}"
        _conversation_history[user].append({"role": "assistant", "content": summary})
        # Trim history
        while len(_conversation_history[user]) > 10:
            _conversation_history[user].pop(0)
    except Exception:
        pass

    return result


@app.put("/api/notes/{note_id}/verify")
def api_verify_note(note_id: int, body: dict):
    """Mark an AI analysis note as correct or incorrect."""
    verification = body.get("verification")  # 'correct', 'incorrect', or None
    user = body.get("user", "default")
    verify_note(note_id, verification, user)
    return {"ok": True}


@app.get("/api/stock/{code}/analysis-accuracy")
def api_analysis_accuracy(code: str, user: str = Query("default")):
    """Get historical AI analysis accuracy for a stock."""
    return get_analysis_accuracy(code, user)


@app.get("/api/analysis-preferences")
def api_get_analysis_preferences(user: str = Query("default")):
    """Get user's AI analysis preferences."""
    return get_analysis_preferences(user)


@app.put("/api/analysis-preferences")
def api_update_analysis_preferences(body: dict):
    """Update user's AI analysis preferences."""
    user = body.get("user", "default")
    update_analysis_preferences(
        user_id=user,
        trading_style=body.get("trading_style", ""),
        preferred_indicators=body.get("preferred_indicators", ""),
        risk_tolerance=body.get("risk_tolerance", ""),
        custom_prompt=body.get("custom_prompt", ""),
        analysis_framework=body.get("analysis_framework", ""),
        ma_tangle_threshold=float(body.get("ma_tangle_threshold", 3.0)),
    )
    return {"ok": True}


@app.get("/api/market-scan")
def api_market_scan(
    rsi_min: float = Query(None), rsi_max: float = Query(None),
    ma_arrangement: str = Query(None),
    volume_ratio_min: float = Query(None),
    change_pct_min: float = Query(None), change_pct_max: float = Query(None),
    volume_trend: str = Query(None),
    industry: str = Query(None),
    market: str = Query(None),
    price_above_ma: str = Query(None),
    price_below_ma: str = Query(None),
    ma_dir: str = Query(None),
    ma_cross: str = Query(None),
    change_rank_max: int = Query(None),
    scan_date: str = Query(None),
    limit: int = Query(50, le=200),
):
    """Scan market with conditions from daily_indicators."""
    import psycopg2
    from psycopg2.extras import RealDictCursor
    from src.core.pg_client import DB_CONFIG

    conn = psycopg2.connect(**DB_CONFIG)
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        # Use specified date or latest date in daily_indicators
        if scan_date:
            latest = scan_date
            # Verify this date exists
            cur.execute("SELECT date FROM daily_indicators WHERE date = %s LIMIT 1", (scan_date,))
            if not cur.fetchone():
                # Find closest available date <= scan_date
                cur.execute("SELECT MAX(date) FROM daily_indicators WHERE date <= %s", (scan_date,))
                row = cur.fetchone()
                latest = row["max"] if row and row["max"] else None
        else:
            cur.execute("SELECT MAX(date) FROM daily_indicators")
            latest = cur.fetchone()["max"]
        if not latest:
            return {"date": None, "results": [], "total": 0}

        # Build dynamic WHERE
        conditions = ["di.date = %s"]
        params = [latest]

        if rsi_min is not None:
            conditions.append("di.rsi14 >= %s")
            params.append(rsi_min)
        if rsi_max is not None:
            conditions.append("di.rsi14 <= %s")
            params.append(rsi_max)
        if ma_arrangement:
            conditions.append("di.ma_arrangement = %s")
            params.append(ma_arrangement)
        if volume_ratio_min is not None:
            conditions.append("di.volume_ratio >= %s")
            params.append(volume_ratio_min)
        if change_pct_min is not None:
            conditions.append("di.change_pct >= %s")
            params.append(change_pct_min)
        if change_pct_max is not None:
            conditions.append("di.change_pct <= %s")
            params.append(change_pct_max)
        if volume_trend:
            conditions.append("di.volume_trend = %s")
            params.append(volume_trend)
        if industry:
            conditions.append("sb.industry = %s")
            params.append(industry)
        if market:
            conditions.append("sb.market = %s")
            params.append(market)
        # Price above MA (e.g. "20" means close > MA20)
        if price_above_ma:
            ma_col = f"di.ma{price_above_ma}"
            conditions.append(f"di.close > {ma_col}")
            conditions.append(f"{ma_col} IS NOT NULL")
        # Price below MA
        if price_below_ma:
            ma_col = f"di.ma{price_below_ma}"
            conditions.append(f"di.close < {ma_col}")
            conditions.append(f"{ma_col} IS NOT NULL")
        # MA direction (e.g. "20_up" means MA20 trending up)
        if ma_dir:
            parts = ma_dir.split("_")
            if len(parts) == 2:
                ma_period, direction = parts
                dir_col = f"di.ma{ma_period}_dir"
                dir_val = "↑" if direction == "up" else "↓" if direction == "down" else "→"
                conditions.append(f"{dir_col} = %s")
                params.append(dir_val)
        # MA cross (e.g. "5_20_up" means MA5 crossed above MA20)
        if ma_cross:
            parts = ma_cross.split("_")
            if len(parts) == 3:
                fast, slow, cross_dir = parts
                fast_col = f"di.ma{fast}"
                slow_col = f"di.ma{slow}"
                if cross_dir == "up":
                    # Golden cross: MA_fast > MA_slow and they are close (within 1%)
                    conditions.append(f"{fast_col} > {slow_col}")
                    conditions.append(f"({fast_col} - {slow_col}) / {slow_col} < 0.01")
                    conditions.append(f"{fast_col} IS NOT NULL AND {slow_col} IS NOT NULL")
                else:
                    # Death cross: MA_fast < MA_slow and they are close
                    conditions.append(f"{fast_col} < {slow_col}")
                    conditions.append(f"({slow_col} - {fast_col}) / {fast_col} < 0.01")
                    conditions.append(f"{fast_col} IS NOT NULL AND {slow_col} IS NOT NULL")

        # Change rank (漲幅排名)
        if change_rank_max is not None:
            conditions.append("di.change_rank <= %s")
            params.append(change_rank_max)

        where_clause = " AND ".join(conditions)
        params.append(limit)

        sql = f"""
            SELECT di.stock_code, sb.stock_name, sb.industry, sb.market,
                   di.close, di.change_pct, di.ma_arrangement,
                   di.rsi14, di.volume_ratio, di.volume_trend,
                   di.macd_dif, di.macd_histogram, di.patterns
            FROM daily_indicators di
            JOIN stock_basic sb ON sb.stock_code = di.stock_code
            WHERE {where_clause}
            ORDER BY di.volume DESC
            LIMIT %s
        """
        cur.execute(sql, params)
        results = [dict(r) for r in cur.fetchall()]

        # Get total count
        count_sql = f"SELECT COUNT(*) FROM daily_indicators di JOIN stock_basic sb ON sb.stock_code = di.stock_code WHERE {where_clause}"
        cur.execute(count_sql, params[:-1])  # exclude limit
        total = cur.fetchone()["count"]

        return {"date": latest.isoformat(), "results": results, "total": total}
    finally:
        conn.close()


@app.get("/api/stock/{code}/related")
def api_related_stocks(code: str, user: str = Query("default")):
    """Get related stocks (same industry) with their indicators."""
    import psycopg2
    from psycopg2.extras import RealDictCursor
    from src.core.pg_client import DB_CONFIG

    conn = psycopg2.connect(**DB_CONFIG)
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        # Get this stock's industry
        cur.execute("SELECT industry FROM stock_basic WHERE stock_code = %s", (code,))
        row = cur.fetchone()
        if not row or not row["industry"]:
            return {"industry": None, "stocks": []}

        industry = row["industry"]

        # Get latest indicators for same industry
        cur.execute("SELECT MAX(date) FROM daily_indicators")
        latest = cur.fetchone()["max"]
        if not latest:
            return {"industry": industry, "stocks": []}

        cur.execute("""
            SELECT di.stock_code, sb.stock_name,
                   di.close, di.change_pct, di.ma_arrangement,
                   di.rsi14, di.volume_ratio, di.volume_trend,
                   di.macd_histogram, di.patterns
            FROM daily_indicators di
            JOIN stock_basic sb ON sb.stock_code = di.stock_code
            WHERE sb.industry = %s AND di.date = %s AND di.stock_code != %s
            ORDER BY di.volume DESC
            LIMIT 10
        """, (industry, latest, code))
        stocks = [dict(r) for r in cur.fetchall()]

        return {"industry": industry, "date": latest.isoformat(), "stocks": stocks}
    finally:
        conn.close()


@app.get("/api/industries")
def api_industries():
    """Get list of all industries."""
    import psycopg2
    from src.core.pg_client import DB_CONFIG
    conn = psycopg2.connect(**DB_CONFIG)
    try:
        cur = conn.cursor()
        cur.execute("SELECT DISTINCT industry FROM stock_basic WHERE industry IS NOT NULL ORDER BY industry")
        return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


@app.get("/api/stock/{code}/revenue")
def api_revenue(code: str):
    return fetch_revenue(code)


@app.get("/api/stock/{code}/dividend")
def api_dividend(code: str):
    return fetch_dividend(code)


# --- Hot stocks ---

@app.get("/api/hot-stocks")
def api_hot_stocks(type: str = Query("active")):
    return fetch_hot_stocks(type)


@app.get("/api/rank")
def api_rank(direction: str = Query("up"), market: str = Query("all")):
    from datetime import date
    data = fetch_rank(direction, market)
    if data:
        save_rank(date.today().isoformat(), direction, market, data)
    return data


@app.get("/api/rank/history")
def api_rank_history(date: str = Query(...), direction: str = Query("up"), market: str = Query("all")):
    return get_rank_history(date, direction, market)


# --- Watchlist (static paths first) ---

@app.get("/api/watchlist")
def api_get_watchlist(user: str = Query("default")):
    return get_watchlist(user)


@app.get("/api/watchlist/groups")
def api_get_groups(user: str = Query("default")):
    return get_all_groups(user)


@app.post("/api/watchlist/group")
def api_create_group(name: str = Query(...), user: str = Query("default")):
    create_group(name, user)
    return {"ok": True}


@app.put("/api/watchlist/group/rename")
def api_rename_group(old: str = Query(...), new: str = Query(...), user: str = Query("default")):
    rename_group(old, new, user)
    return {"ok": True}


@app.delete("/api/watchlist/group/{group_name}")
def api_delete_group(group_name: str, user: str = Query("default")):
    delete_group(group_name, user)
    return {"ok": True}


@app.delete("/api/watchlist/notes/{note_id}")
def api_delete_note(note_id: int, user: str = Query("default")):
    img = delete_note(note_id, user)
    if img:
        p = UPLOADS_DIR / img.split("/")[-1]
        if p.exists():
            p.unlink()
    return {"ok": True}


@app.get("/api/watchlist/notes/{note_id}/image")
def api_get_note_image(note_id: int):
    data = get_note_image(note_id)
    if not data:
        return Response(status_code=404)
    return Response(content=data, media_type="image/png")


@app.put("/api/watchlist/notes/{note_id}/content")
def api_update_note_content(note_id: int, body: dict):
    user = body.get("user", "default")
    content = body.get("content", "")
    title = body.get("title", None)
    from src.core.database import update_note_content
    update_note_content(note_id, content, user, title)
    return {"ok": True}


# --- Alerts ---

@app.get("/api/alerts")
def api_get_alerts(user: str = Query("default"), stock_code: str = Query(None)):
    return get_alerts(user, stock_code)


@app.post("/api/alerts")
def api_add_alert(body: dict):
    new_id = add_alert(
        stock_code=body["stock_code"],
        alert_type=body["alert_type"],
        params=body.get("params", {}),
        repeat_mode=body.get("repeat_mode", "once"),
        user_id=body.get("user", "default"),
    )
    return {"ok": True, "id": new_id}


@app.get("/api/alerts/settings")
def api_get_alert_settings(user: str = Query("default")):
    return get_alert_settings(user)


@app.put("/api/alerts/settings")
def api_update_alert_settings(body: dict):
    update_alert_settings(
        user_id=body.get("user", "default"),
        run_time=body.get("run_time", "18:00"),
        telegram_chat_id=body.get("telegram_chat_id", ""),
        telegram_bot_token=body.get("telegram_bot_token", ""),
    )
    return {"ok": True}


@app.put("/api/alerts/{alert_id}")
def api_update_alert(alert_id: int, body: dict):
    user = body.pop("user", "default")
    update_alert(alert_id, user, **body)
    return {"ok": True}


@app.delete("/api/alerts/{alert_id}")
def api_delete_alert(alert_id: int, user: str = Query("default")):
    delete_alert(alert_id, user)
    return {"ok": True}


# --- Semantic Search ---

@app.get("/api/notes/search")
def api_search_notes(q: str = Query(""), user: str = Query("default"), ai: bool = Query(True)):
    from src.services.semantic_search import search_notes
    return search_notes(q, user, use_ai=ai)


@app.get("/api/notes/ask")
def api_ask_notes(q: str = Query(""), user: str = Query("default")):
    from src.services.semantic_search import ask_news
    return ask_news(q, user)


# --- News Weekly Digest ---

@app.get("/api/news-digest")
def api_get_news_digests():
    """List all weekly news digests."""
    from src.core.database import get_news_digests
    return get_news_digests()


@app.get("/api/news-digest/{digest_id}")
def api_get_news_digest(digest_id: int):
    """Get a single weekly digest with full markdown content."""
    from src.core.database import get_news_digest_by_id
    result = get_news_digest_by_id(digest_id)
    if not result:
        return Response(status_code=404)
    return result


@app.post("/api/news-digest/generate")
def api_generate_news_digest():
    """Manually trigger weekly digest generation (for testing)."""
    from src.services.news_digest import generate_weekly_digest
    result = generate_weekly_digest()
    if result:
        return {"ok": True, **result}
    return {"ok": False, "message": "本週無新聞可整理"}


# --- Backtest ---

@app.post("/api/backtest")
def api_backtest(body: dict):
    from src.services.backtest_engine import run_backtest
    result = run_backtest(
        stock_code=body["stock_code"],
        buy_conditions=body.get("buy_conditions", []),
        sell_conditions=body.get("sell_conditions", []),
        buy_logic=body.get("buy_logic", "and"),
        sell_logic=body.get("sell_logic", "and"),
        shares=body.get("shares", 1),
        fee_rate=body.get("fee_rate", 0.1425),
        tax_rate=body.get("tax_rate", 0.3),
        days=body.get("days", 500),
    )
    return result


# --- Balance ---

@app.get("/api/watchlist/balance")
def api_get_balance(user: str = Query("default")):
    return {"balance": get_balance(user)}


@app.post("/api/watchlist/balance")
def api_update_balance(amount: float = Query(...), user: str = Query("default")):
    update_balance(user, amount)
    return {"ok": True, "balance": get_balance(user)}


# --- Trades ---

@app.get("/api/watchlist/trades")
def api_get_all_trades(user: str = Query("default")):
    return get_all_trades(user)


@app.delete("/api/watchlist/trades/{trade_id}")
def api_delete_trade(trade_id: int, user: str = Query("default")):
    delete_trade(trade_id, user)
    return {"ok": True}


# --- Watchlist (dynamic paths) ---

@app.post("/api/watchlist/{code}")
def api_add_watchlist(code: str, name: str = Query(""), group: str = Query("預設"),
                      user: str = Query("default"), buy_price: float = Query(0),
                      buy_shares: int = Query(0), buy_date: str = Query("")):
    add_watchlist(code, name, group, user, buy_price, buy_shares, buy_date)
    # Add trade record and deduct from balance
    if buy_price > 0 and buy_shares > 0:
        add_trade(code, buy_price, buy_shares, buy_date, user)
        cost = buy_price * buy_shares * 1000
        update_balance(user, -cost)
    return {"ok": True}


@app.delete("/api/watchlist/{code}")
def api_remove_watchlist(code: str, user: str = Query("default")):
    remove_watchlist(code, user)
    return {"ok": True}


@app.put("/api/watchlist/{code}/move")
def api_move_watchlist(code: str, group: str = Query(...), user: str = Query("default")):
    move_watchlist(code, group, user)
    return {"ok": True}


@app.put("/api/watchlist/{code}/cost")
def api_update_cost(code: str, price: float = Query(...), shares: int = Query(...),
                    date: str = Query(""), user: str = Query("default")):
    update_cost(code, price, shares, date, user)
    return {"ok": True}


@app.post("/api/watchlist/{code}/sell")
def api_sell_stock(code: str, price: float = Query(...), shares: int = Query(...),
                   user: str = Query("default")):
    sell_stock(code, shares, price, user)
    return {"ok": True}


@app.get("/api/watchlist/{code}/trades")
def api_get_trades(code: str, user: str = Query("default")):
    return get_trades(code, user)


@app.post("/api/watchlist/{code}/trades")
def api_add_trade(code: str, price: float = Query(...), shares: int = Query(...),
                  date: str = Query(""), user: str = Query("default")):
    add_trade(code, price, shares, date, user)
    # Deduct from balance
    cost = price * shares * 1000
    update_balance(user, -cost)
    return {"ok": True}


@app.get("/api/watchlist/{code}/notes")
def api_get_notes(code: str, user: str = Query("default")):
    return get_notes(code, user)


@app.post("/api/watchlist/{code}/notes")
async def api_add_note(code: str, user: str = Query("default"),
                       content: str = Form(""), image: UploadFile = File(None),
                       news_date: str = Form(""), title: str = Form("")):
    image_data = None
    image_filename = ""
    if image and image.filename:
        image_filename = image.filename
        # Remove existing note with same filename (allow re-upload after delete)
        import psycopg2
        from src.core.pg_client import DB_CONFIG
        conn = psycopg2.connect(**DB_CONFIG)
        cur = conn.cursor()
        cur.execute("DELETE FROM watchlist_notes WHERE stock_code=%s AND user_id=%s AND image_filename=%s",
                    (code, user, image_filename))
        conn.commit()
        conn.close()
        image_data = await image.read()

    # Save note immediately (with placeholder if image needs processing)
    pending = bool(image_data and not content)
    save_content = "⏳ 分析中..." if pending else content
    note_id = add_note(code, save_content, "", user, image_data, news_date if news_date else None, image_filename, title)

    # Dispatch background image analysis if needed
    if pending:
        from fastapi.concurrency import run_in_threadpool
        asyncio.ensure_future(
            _process_image_background(note_id, image_data, image_filename, user)
        )

    return {"ok": True, "id": note_id, "pending": pending}


async def _process_image_background(note_id: int, image_data: bytes, filename: str, user_id: str):
    """Background task: try AI analysis first, fallback to OCR."""
    import logging
    logger = logging.getLogger(__name__)
    from src.services.image_analysis import analyze_image_ai, analyze_image_ocr

    loop = asyncio.get_running_loop()
    content = ""

    try:
        # Try AI vision analysis first (limited concurrency)
        async with _ai_semaphore:
            content = await loop.run_in_executor(
                None, functools.partial(analyze_image_ai, image_data, filename)
            ) or ""
    except Exception as e:
        logger.error(f"Background AI analysis failed for note {note_id}: {e}")
        content = ""

    # Fallback to OCR if AI returned nothing (controlled by ENABLE_OCR env var)
    if not content and os.getenv("ENABLE_OCR", "false").lower() == "true":
        try:
            async with _ocr_semaphore:
                content = await loop.run_in_executor(
                    None, functools.partial(_run_ocr, image_data)
                )
        except Exception as e:
            logger.error(f"Background OCR failed for note {note_id}: {e}")
            content = ""

    # Update the note with the result
    final_content = content if content else "(圖片)"
    try:
        update_note_content(note_id, final_content, user_id)
        logger.info(f"Note {note_id} updated: {final_content[:50]}...")
    except Exception as e:
        logger.error(f"Failed to update note {note_id}: {e}")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8020)
