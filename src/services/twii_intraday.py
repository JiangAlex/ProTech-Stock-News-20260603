"""TWII Intraday 60-min K-line AI Feedback Learning System.

PLL (Phase-Locked Loop) architecture:
- Signal source: TWSE MIS API realtime TWII quotes (every 60s)
- VCO output: AI prediction (direction + support/resistance)
- Phase detector: Immediate verification (next bar completes)
- Loop filter: Bias analysis + prompt correction
- Integration: 17:00 daily + weekly line analysis

Time slots:
  Bar 1: 09:00~10:00
  Bar 2: 10:00~11:00
  Bar 3: 11:00~12:00
  Bar 4: 12:00~13:00
  Bar 5: 13:00~13:30
"""

import json
import logging
import os
import re
import urllib.request
from src.services.http_retry import retry_urlopen
from datetime import datetime, date, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

# --- Constants ---
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "data")
KLINE_FILE = os.path.join(DATA_DIR, "twii_60min_kline.json")
PREDICTION_FILE = os.path.join(DATA_DIR, "twii_prediction_history.json")
KLINE_RETAIN_DAYS = 60  # Need 200+ bars for MA200 (5 bars/day * 60 days = 300 bars)
PREDICTION_RETAIN_DAYS = 30

MIS_API_URL = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"
TWII_EX_CH = "tse_t00.tw"  # 加權指數

YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/%5ETWII"
MINIMAX_API_URL = os.getenv("OPENROUTER_API_URL", "https://openrouter.ai/api/v1/chat/completions")

# 60-min bar time slots (start_time, end_time)
BAR_SLOTS = [
    ("09:00", "10:00"),
    ("10:00", "11:00"),
    ("11:00", "12:00"),
    ("12:00", "13:00"),
    ("13:00", "13:30"),
]

# --- Persistence path ---
_STATS_FILE = os.path.join(DATA_DIR, "today_stats.json")

# --- In-memory state ---
_current_bar: dict = {}       # Current bar being assembled {open, high, low, close, volume_start, volume}
_today_bars: list = []        # Completed bars for today
_predictions_today: list = [] # Predictions made today
_today_stats: dict = {        # Today's prediction accuracy stats
    "total": 0,
    "correct": 0,
    "incorrect": 0,
    "accuracy": 0.0,
    "consecutive_failures": 0,
}

# --- Helpers (defined before use) ---

def _ensure_data_dir():
    """Create data directory if not exists."""
    os.makedirs(DATA_DIR, exist_ok=True)


# Try to restore persisted stats on module load
_ensure_data_dir()
try:
    load_today_stats()
except Exception:
    pass  # graceful fallback if file missing or corrupt


def save_today_stats():
    """Persist _today_stats to JSON file (includes date key)."""
    try:
        payload = dict(_today_stats, date=date.today().isoformat())
        with open(_STATS_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
    except Exception as e:
        logger.warning(f"Failed to save today_stats: {e}")


def load_today_stats():
    """Load _today_stats from JSON file; reset if not today."""
    if not os.path.exists(_STATS_FILE):
        return
    try:
        with open(_STATS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        today = date.today().isoformat()
        if data.get("date") == today:
            _today_stats.update(data)
            logger.info(f"Restored today_stats from file: {data}")
        else:
            logger.info(f"Stats file is from a different date ({data.get('date')}), resetting.")
            _today_stats.update({
                "total": 0, "correct": 0, "incorrect": 0,
                "accuracy": 0.0, "consecutive_failures": 0,
            })
    except Exception as e:
        logger.warning(f"Failed to load today_stats: {e}")


# =============================================================================
# 1. JSON File I/O (K-line history + prediction history)
# =============================================================================

def _load_kline_history() -> dict:
    """Load 60-min kline history from JSON file.
    Returns: {date_str: [bars]}
    """
    _ensure_data_dir()
    if not os.path.exists(KLINE_FILE):
        return {}
    try:
        with open(KLINE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        logger.error(f"Failed to load kline history: {e}")
        return {}


def _save_kline_history(data: dict):
    """Save 60-min kline history to JSON file, pruning old dates."""
    _ensure_data_dir()
    # Prune: keep only recent N days
    cutoff = (date.today() - timedelta(days=KLINE_RETAIN_DAYS)).isoformat()
    pruned = {k: v for k, v in data.items() if k >= cutoff}
    try:
        with open(KLINE_FILE, "w", encoding="utf-8") as f:
            json.dump(pruned, f, ensure_ascii=False, indent=2)
    except IOError as e:
        logger.error(f"Failed to save kline history: {e}")


def _load_prediction_history() -> dict:
    """Load prediction history from JSON file.
    Returns: {date_str: {"total": int, "correct": int, "accuracy": float, "details": [...]}}
    """
    _ensure_data_dir()
    if not os.path.exists(PREDICTION_FILE):
        return {}
    try:
        with open(PREDICTION_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        logger.error(f"Failed to load prediction history: {e}")
        return {}


def _save_prediction_history(data: dict):
    """Save prediction history to JSON file, pruning old dates."""
    _ensure_data_dir()
    cutoff = (date.today() - timedelta(days=PREDICTION_RETAIN_DAYS)).isoformat()
    pruned = {k: v for k, v in data.items() if k >= cutoff}
    try:
        with open(PREDICTION_FILE, "w", encoding="utf-8") as f:
            json.dump(pruned, f, ensure_ascii=False, indent=2)
    except IOError as e:
        logger.error(f"Failed to save prediction history: {e}")


# =============================================================================
# 2. MIS API — Fetch TWII Realtime Quote
# =============================================================================

def fetch_twii_realtime() -> Optional[dict]:
    """Fetch TWII realtime quote from TWSE MIS API.

    Returns:
        {"price": float, "high": float, "low": float, "volume": int, "time": str}
        or None on failure.
    """
    url = f"{MIS_API_URL}?ex_ch={TWII_EX_CH}&json=1&delay=0"
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"
        })
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        if data.get("rtcode") != "0000":
            return None

        items = data.get("msgArray", [])
        if not items:
            return None

        item = items[0]
        # z = latest price, h = high, l = low, v = volume (lots)
        price = _parse_float(item.get("z"))
        if price is None:
            # Fallback to yesterday close if not yet traded
            price = _parse_float(item.get("y"))
        if price is None:
            return None

        return {
            "price": price,
            "high": _parse_float(item.get("h")) or price,
            "low": _parse_float(item.get("l")) or price,
            "volume": _parse_int(item.get("v")),
            "time": item.get("t", ""),
        }
    except Exception as e:
        logger.error(f"TWII MIS API fetch error: {e}")
        return None


def _parse_float(val) -> Optional[float]:
    """Parse string to float, return None if invalid."""
    if not val or val == "-":
        return None
    try:
        return float(str(val).replace(",", ""))
    except (ValueError, TypeError):
        return None


def _parse_int(val) -> int:
    """Parse string to int, return 0 if invalid."""
    if not val or val == "-":
        return 0
    try:
        return int(str(val).replace(",", ""))
    except (ValueError, TypeError):
        return 0


# =============================================================================
# 3. 60-min Bar Assembly (RAM)
# =============================================================================

def get_current_bar_slot(now: datetime = None) -> Optional[tuple]:
    """Determine which bar slot the current time belongs to.
    Returns: (slot_index, start_time, end_time) or None if outside market hours.
    """
    if now is None:
        now = datetime.now()
    current_time = now.strftime("%H:%M")

    for i, (start, end) in enumerate(BAR_SLOTS):
        if start <= current_time < end:
            return (i, start, end)
    return None


def update_current_bar(quote: dict, now: datetime = None):
    """Update the current bar with a new quote tick.

    Args:
        quote: {"price": float, "high": float, "low": float, "volume": int}
    """
    global _current_bar

    if now is None:
        now = datetime.now()

    slot = get_current_bar_slot(now)
    if slot is None:
        return  # Outside market hours

    slot_idx, start_time, end_time = slot
    price = quote["price"]

    # Check if we need to start a new bar
    if not _current_bar or _current_bar.get("time_slot") != start_time:
        # New bar starts
        _current_bar = {
            "time_slot": start_time,
            "slot_index": slot_idx,
            "open": price,
            "high": price,
            "low": price,
            "close": price,
            "volume_start": quote["volume"],
            "volume": 0,
        }
    else:
        # Update existing bar
        _current_bar["close"] = price
        if price > _current_bar["high"]:
            _current_bar["high"] = price
        if price < _current_bar["low"]:
            _current_bar["low"] = price
        # Volume is cumulative from MIS API; bar volume = current - start
        _current_bar["volume"] = quote["volume"] - _current_bar["volume_start"]


def finalize_current_bar(now: datetime = None) -> Optional[dict]:
    """Finalize the current bar and add it to today's completed bars.

    Called when bar end time is reached.
    Returns the finalized bar dict, or None.
    """
    global _current_bar, _today_bars

    if not _current_bar:
        return None

    bar = {
        "time": _current_bar["time_slot"],
        "open": _current_bar["open"],
        "high": _current_bar["high"],
        "low": _current_bar["low"],
        "close": _current_bar["close"],
        "volume": max(0, _current_bar.get("volume", 0)),
    }
    _today_bars.append(bar)
    _current_bar = {}

    logger.info(
        f"TWII 60min bar finalized: {bar['time']} "
        f"O={bar['open']:.2f} H={bar['high']:.2f} L={bar['low']:.2f} C={bar['close']:.2f} V={bar['volume']}"
    )
    return bar


def save_today_bars_to_file():
    """Save today's completed bars to JSON file."""
    if not _today_bars:
        return
    today_str = date.today().isoformat()
    history = _load_kline_history()
    history[today_str] = _today_bars
    _save_kline_history(history)
    logger.info(f"Saved {len(_today_bars)} bars for {today_str}")


def reset_daily_state():
    """Reset all in-memory state for a new trading day."""
    global _current_bar, _today_bars, _predictions_today, _today_stats
    _current_bar = {}
    _today_bars = []
    _predictions_today = []
    _today_stats = {
        "total": 0,
        "correct": 0,
        "incorrect": 0,
        "accuracy": 0.0,
        "consecutive_failures": 0,
    }


def get_recent_bars(days: int = 5) -> list[dict]:
    """Get recent N days of 60-min bars (flattened, chronological).
    Each bar includes a 'date' field.
    """
    history = _load_kline_history()
    sorted_dates = sorted(history.keys(), reverse=True)[:days]
    sorted_dates.reverse()  # chronological order

    all_bars = []
    for d in sorted_dates:
        for bar in history[d]:
            all_bars.append({**bar, "date": d})

    # Also include today's in-memory bars
    today_str = date.today().isoformat()
    for bar in _today_bars:
        all_bars.append({**bar, "date": today_str})

    return all_bars


# =============================================================================
# 4. Yahoo Finance — Historical 60-min K-line Initialization
# =============================================================================

def init_history_from_yahoo(range_str: str = "60d") -> int:
    """Fetch historical 60-min TWII kline from Yahoo Finance.

    Used on startup if local JSON file has insufficient data.
    Args:
        range_str: "5d", "7d", "60d" etc. (Yahoo allows up to 60d for 60min interval)
    Returns: number of bars stored
    """
    url = f"{YAHOO_CHART_URL}?interval=60m&range={range_str}"
    headers = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"}

    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        logger.error(f"Yahoo Finance 60min fetch failed: {e}")
        return 0

    result = data.get("chart", {}).get("result", [])
    if not result:
        logger.warning("Yahoo Finance returned no data for TWII 60min")
        return 0

    timestamps = result[0].get("timestamp", [])
    quote = result[0].get("indicators", {}).get("quote", [{}])[0]
    opens = quote.get("open", [])
    highs = quote.get("high", [])
    lows = quote.get("low", [])
    closes = quote.get("close", [])
    volumes = quote.get("volume", [])

    if not timestamps:
        return 0

    # Group by date and time slot
    history = _load_kline_history()
    count = 0

    for i, ts in enumerate(timestamps):
        if ts is None or closes[i] is None:
            continue

        dt = datetime.fromtimestamp(ts)
        date_str = dt.strftime("%Y-%m-%d")
        time_str = dt.strftime("%H:%M")

        # Only keep bars within market hours (09:00~13:30)
        if time_str < "09:00" or time_str >= "13:30":
            continue

        # Map to our bar slot
        bar_time = None
        for start, end in BAR_SLOTS:
            if start <= time_str < end:
                bar_time = start
                break
        if bar_time is None:
            continue

        bar = {
            "time": bar_time,
            "open": round(opens[i] or 0, 2),
            "high": round(highs[i] or 0, 2),
            "low": round(lows[i] or 0, 2),
            "close": round(closes[i] or 0, 2),
            "volume": int(volumes[i] or 0),
        }

        if date_str not in history:
            history[date_str] = []

        # Avoid duplicates (same date + same time slot)
        existing_times = {b["time"] for b in history[date_str]}
        if bar_time not in existing_times:
            history[date_str].append(bar)
            count += 1

    # Sort bars within each day by time
    for d in history:
        history[d].sort(key=lambda b: b["time"])

    _save_kline_history(history)
    logger.info(f"Yahoo Finance init: stored {count} bars for TWII 60min")
    return count


def ensure_history_available():
    """Check if we have enough history for MA200; if not, fetch from Yahoo Finance.

    MA200 needs 200 bars = ~40 trading days. We check total bars to be safe.
    """
    history = _load_kline_history()
    total_bars = sum(len(bars) for bars in history.values())
    # Need at least 200 bars for MA200; 3 days is far from enough (3*5=15 bars)
    if total_bars < 200:
        logger.info(f"Insufficient 60min history ({total_bars} bars), fetching 60d from Yahoo Finance...")
        init_history_from_yahoo("60d")


# =============================================================================
# 5. AI Prediction Engine (Phase 2 — triggered after each bar completes)
# =============================================================================

def _compute_60min_indicators(bars: list[dict]) -> dict:
    """Compute technical indicators for 60-min bars.

    Args:
        bars: list of {time, open, high, low, close, volume} (chronological)
    Returns:
        dict with MA, trend, volume info
    """
    if len(bars) < 5:
        return {"error": "insufficient bars"}

    closes = [b["close"] for b in bars]
    volumes = [b["volume"] for b in bars]

    # Simple MAs
    def ma(data, period):
        if len(data) < period:
            return None
        return round(sum(data[-period:]) / period, 2)

    ma5 = ma(closes, 5)
    ma10 = ma(closes, 10)
    ma20 = ma(closes, 20)
    ma35 = ma(closes, 35)
    ma200 = ma(closes, 200)

    # Current price vs MAs
    current = closes[-1]
    prev = closes[-2] if len(closes) >= 2 else current

    # Volume average
    avg_vol_5 = ma(volumes, 5)
    vol_ratio = round(volumes[-1] / avg_vol_5, 2) if avg_vol_5 and avg_vol_5 > 0 else 1.0

    # Price change
    change = round(current - prev, 2)
    change_pct = round(change / prev * 100, 2) if prev > 0 else 0

    # Trend: count of recent up/down bars
    recent_5 = closes[-5:] if len(closes) >= 5 else closes
    up_count = sum(1 for i in range(1, len(recent_5)) if recent_5[i] > recent_5[i-1])
    down_count = len(recent_5) - 1 - up_count

    # MA35/MA200 position analysis
    ma35_dist = round((current - ma35) / current * 100, 2) if ma35 else None
    ma200_dist = round((current - ma200) / current * 100, 2) if ma200 else None

    return {
        "current": current,
        "prev_close": prev,
        "change": change,
        "change_pct": change_pct,
        "ma5": ma5,
        "ma10": ma10,
        "ma20": ma20,
        "ma35": ma35,
        "ma200": ma200,
        "ma35_dist": ma35_dist,
        "ma200_dist": ma200_dist,
        "vol_ratio": vol_ratio,
        "recent_up": up_count,
        "recent_down": down_count,
        "total_bars": len(bars),
    }


def _build_prediction_prompt(bars: list[dict], indicators: dict, feedback: str = "") -> str:
    """Build AI prompt for analyzing next 60-min bar support/resistance levels."""

    # Format recent bars (last 20 or all)
    display_bars = bars[-20:]
    bars_text = "\n".join([
        f"  {b.get('date', '')} {b['time']} O:{b['open']:.2f} H:{b['high']:.2f} "
        f"L:{b['low']:.2f} C:{b['close']:.2f} V:{b['volume']}"
        for b in display_bars
    ])

    # MA35/MA200 info
    ma_extra = ""
    if indicators.get('ma35'):
        ma_extra += f"\n- MA35: {indicators['ma35']}（距離現價 {indicators['ma35_dist']:+.2f}%）"
    if indicators.get('ma200'):
        ma_extra += f"\n- MA200: {indicators['ma200']}（距離現價 {indicators['ma200_dist']:+.2f}%）"

    prompt = f"""# 角色
你是一位專精台股大盤的短線技術分析師，擅長分析 60 分鐘 K 線的支撐與壓力。

# 任務
根據以下台灣加權指數（TWII）60 分鐘 K 線資料與技術指標，分析下一根 60 分 K 線的關鍵支撐位與壓力位，並結合 MA35/MA200 判斷目前多空格局。

注意：不要預測方向，只需分析支撐、壓力與均線相對位置。

# 輸出格式（嚴格 JSON，不要加任何其他文字）
{{
    "support": 數字（下一根支撐位）,
    "resistance": 數字（下一根壓力位）,
    "ma35_analysis": "MA35 與現價關係的簡短描述（20字內）",
    "ma200_analysis": "MA200 與現價關係的簡短描述（20字內）",
    "reasoning": "支撐壓力判斷理由，包含均線位置、量能、K線型態等綜合分析（150字內）"
}}

# 技術指標
- 最新收盤：{indicators['current']:.2f}（較前根 {indicators['change']:+.2f}, {indicators['change_pct']:+.2f}%）
- MA5: {indicators['ma5']}  MA10: {indicators['ma10']}  MA20: {indicators['ma20']}{ma_extra}
- 量比（vs 5根均量）: {indicators['vol_ratio']}
- 近5根趨勢：{indicators['recent_up']} 漲 / {indicators['recent_down']} 跌

# 近期 60 分 K 線
{bars_text}
"""

    if feedback:
        prompt += f"\n{feedback}\n"

    prompt += "\n請直接輸出 JSON，不要有其他文字。"
    return prompt


def predict_next_bar() -> Optional[dict]:
    """Run AI prediction for the next 60-min bar.

    Returns:
        {"support": float, "resistance": float, "ma35_analysis": str, "ma200_analysis": str, "reasoning": str}
        or None on failure.
    """
    api_key = os.getenv("OPENROUTER_API_KEY", os.getenv("MINIMAX_API_KEY", ""))
    if not api_key:
        logger.warning("MINIMAX_API_KEY not set, skipping TWII prediction")
        return None

    # Get recent bars (history + today) — need 200+ bars for MA200 (5 bars/day * 45 days)
    all_bars = get_recent_bars(days=45)
    if len(all_bars) < 5:
        logger.warning(f"Insufficient bars for prediction ({len(all_bars)}), need at least 5")
        return None

    # Compute indicators
    indicators = _compute_60min_indicators(all_bars)
    if "error" in indicators:
        logger.warning(f"Indicator error: {indicators['error']}")
        return None

    # Build feedback from history (PLL loop filter)
    feedback = _build_pll_feedback()

    # Build prompt
    prompt = _build_prediction_prompt(all_bars, indicators, feedback)

    # Call MiniMax AI
    data = json.dumps({
        "model": "MiniMax-M2.7",
        "messages": [
            {"role": "system", "content": "你是台股大盤 60 分線短線分析專家，只輸出 JSON 格式。"},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": 2000,
        "temperature": 0.2,
    }).encode()

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    try:
        req = urllib.request.Request(MINIMAX_API_URL, data=data, method="POST", headers=headers)
        result = json.loads(retry_urlopen(req, timeout=30, max_retries=2))
        raw_content = result["choices"][0]["message"]["content"].strip()

        # Try to extract JSON from full response first (including think blocks)
        json_match = re.search(r'\{[^{}]*"support"[^{}]*\}', raw_content, re.DOTALL)
        if not json_match:
            # Fallback: try after removing think blocks and markdown code fences
            content = re.sub(r'<think>.*?</think>', '', raw_content, flags=re.DOTALL).strip()
            content = re.sub(r'```json\s*', '', content)
            content = re.sub(r'```\s*', '', content)
            json_match = re.search(r'\{.*\}', content, re.DOTALL)

        if not json_match:
            logger.warning(f"TWII prediction: no JSON in response (len={len(raw_content)})")
            return _parse_support_resistance_from_text(raw_content)

        prediction = json.loads(json_match.group())

        # Validate required fields (support/resistance are the core outputs now)
        if "support" not in prediction and "resistance" not in prediction:
            return _parse_support_resistance_from_text(raw_content)

        prediction.setdefault("support", 0)
        prediction.setdefault("resistance", 0)
        prediction.setdefault("ma35_analysis", "")
        prediction.setdefault("ma200_analysis", "")
        prediction.setdefault("reasoning", "")

        logger.info(
            f"TWII prediction: "
            f"S={prediction['support']} R={prediction['resistance']} "
            f"MA35={prediction['ma35_analysis']} MA200={prediction['ma200_analysis']}"
        )
        return prediction

    except Exception as e:
        logger.error(f"TWII AI prediction failed: {e}")
        return None


def _parse_support_resistance_from_text(text: str) -> Optional[dict]:
    """Fallback: extract support/resistance from AI's text when JSON output fails."""
    support = _extract_price_from_text(text, ["支撐", "下檔", "低點"])
    resistance = _extract_price_from_text(text, ["壓力", "上檔", "高點"])

    if not support and not resistance:
        return None

    return {
        "support": support or 0,
        "resistance": resistance or 0,
        "ma35_analysis": "",
        "ma200_analysis": "",
        "reasoning": "(text-parsed)",
    }


def record_prediction(bar_time: str, prediction: dict):
    """Record a prediction to today's buffer."""
    _predictions_today.append({
        "bar_time": bar_time,
        "predicted_at": datetime.now().strftime("%H:%M"),
        "support": prediction.get("support", 0),
        "resistance": prediction.get("resistance", 0),
        "ma35_analysis": prediction.get("ma35_analysis", ""),
        "ma200_analysis": prediction.get("ma200_analysis", ""),
        "reasoning": prediction.get("reasoning", ""),
        "actual_close": None,
        "is_within_range": None,
    })


def _push_intraday_telegram(completed_bar: dict, verification: Optional[dict],
                            prediction: Optional[dict], next_slot: str):
    """Push hourly TWII 60-min prediction update to Telegram.

    Includes: completed bar info, verification result, and new prediction.
    """
    try:
        from src.services.telegram_service import send_telegram_message
        from src.core.database import get_alert_settings

        settings = get_alert_settings("default")
        bot_token = settings.get("telegram_bot_token", "")
        chat_id = settings.get("telegram_chat_id", "")
        if not bot_token or not chat_id:
            return

        bar_time = completed_bar["time"]
        bar_close = completed_bar["close"]
        bar_change = completed_bar["close"] - completed_bar["open"]
        bar_change_pct = round(bar_change / completed_bar["open"] * 100, 2) if completed_bar["open"] > 0 else 0

        lines = [f"🕐 <b>大盤 60 分線</b>（{bar_time} bar 收盤）"]
        lines.append(f"加權：{bar_close:.2f}（{bar_change:+.2f}, {bar_change_pct:+.2f}%）")
        lines.append(f"高/低：{completed_bar['high']:.2f} / {completed_bar['low']:.2f}")

        # First bar of the day (09:00): add background info with prev day close + MA positions
        if bar_time == "09:00" and not verification:
            all_bars = get_recent_bars(days=45)
            if len(all_bars) >= 5:
                indicators = _compute_60min_indicators(all_bars)
                if "error" not in indicators:
                    lines.append(f"\n📋 <b>背景</b>")
                    # Previous day's last close (prev_close from indicators = the bar before today's first)
                    lines.append(f"昨收：{indicators['prev_close']:.2f}")
                    # Gap info
                    gap = bar_close - indicators['prev_close']
                    gap_pct = round(gap / indicators['prev_close'] * 100, 2) if indicators['prev_close'] > 0 else 0
                    if abs(gap_pct) >= 0.1:
                        gap_dir = "跳空上漲" if gap > 0 else "跳空下跌"
                        lines.append(f"{gap_dir}：{gap:+.2f}（{gap_pct:+.2f}%）")
                    # MA positions
                    ma_parts = []
                    if indicators.get("ma5"):
                        ma_parts.append(f"MA5:{indicators['ma5']:.0f}")
                    if indicators.get("ma10"):
                        ma_parts.append(f"MA10:{indicators['ma10']:.0f}")
                    if indicators.get("ma20"):
                        ma_parts.append(f"MA20:{indicators['ma20']:.0f}")
                    if ma_parts:
                        lines.append(" | ".join(ma_parts))
                    ma_long_parts = []
                    if indicators.get("ma35"):
                        pos = "上方" if bar_close > indicators['ma35'] else "下方"
                        ma_long_parts.append(f"MA35:{indicators['ma35']:.0f}（{pos}{abs(indicators['ma35_dist']):.1f}%）")
                    if indicators.get("ma200"):
                        pos = "上方" if bar_close > indicators['ma200'] else "下方"
                        ma_long_parts.append(f"MA200:{indicators['ma200']:.0f}（{pos}{abs(indicators['ma200_dist']):.1f}%）")
                    if ma_long_parts:
                        lines.append(" | ".join(ma_long_parts))

        # Verification of previous prediction
        if verification:
            prev_pred = None
            for p in reversed(_predictions_today):
                if p.get("is_within_range") is not None:
                    prev_pred = p
                    break
            if prev_pred:
                mark = "✅" if prev_pred["is_within_range"] else "❌"
                lines.append(f"\n{mark} 上一分析：S={prev_pred['support']:.0f} R={prev_pred['resistance']:.0f} → 實際{prev_pred['actual_close']:.2f}")

            # Running stats
            total = _today_stats["total"]
            correct = _today_stats["correct"]
            if total > 0:
                lines.append(f"📊 今日命中（落在S~R範圍）：{correct}/{total}（{_today_stats['accuracy']}%）")

        # New prediction (support/resistance analysis)
        if prediction:
            lines.append(f"\n🔮 <b>下一時段分析（{next_slot}~）</b>")
            if prediction.get("support"):
                lines.append(f"支撐：{prediction['support']:.0f}｜壓力：{prediction.get('resistance', 0):.0f}")
            if prediction.get("ma35_analysis"):
                lines.append(f"MA35：{prediction['ma35_analysis']}")
            if prediction.get("ma200_analysis"):
                lines.append(f"MA200：{prediction['ma200_analysis']}")
            if prediction.get("reasoning"):
                lines.append(f"理由：{prediction['reasoning']}")

        # Last bar (13:00, slot_idx=4): no prediction, append daily summary instead
        if not prediction and bar_time == "13:00":
            summary = generate_intraday_summary()
            if summary:
                lines.append(f"\n📋 <b>當日總結</b>")
                # Skip the first line (title) since we already have bar info
                summary_lines = summary.split("\n")
                for sl in summary_lines[1:]:
                    if sl.strip():
                        lines.append(sl)

        msg = "\n".join(lines)
        send_telegram_message(bot_token, chat_id, msg)
        logger.info(f"TWII intraday Telegram push sent ({bar_time})")
    except Exception as e:
        logger.error(f"TWII intraday Telegram push failed: {e}")


def _push_market_close_telegram(summary: str):
    """Push market close brief to Telegram at 13:30.

    Includes: daily summary, final stats, and MA position overview.
    """
    try:
        from src.services.telegram_service import send_telegram_message
        from src.core.database import get_alert_settings

        settings = get_alert_settings("default")
        bot_token = settings.get("telegram_bot_token", "")
        chat_id = settings.get("telegram_chat_id", "")
        if not bot_token or not chat_id:
            return

        today_str = date.today().strftime("%m/%d")
        lines = [f"🔔 <b>大盤 60 分線收盤簡報</b>（{today_str}）"]

        # Intraday summary (OHLC + prediction stats)
        if summary:
            # Skip the title line from generate_intraday_summary (we have our own)
            summary_lines = summary.split("\n")
            for sl in summary_lines[1:]:
                if sl.strip():
                    lines.append(sl)
        else:
            lines.append("今日無 60 分線資料")

        # MA position overview from historical bars
        all_bars = get_recent_bars(days=45)
        if len(all_bars) >= 5:
            indicators = _compute_60min_indicators(all_bars)
            if "error" not in indicators:
                lines.append(f"\n📐 <b>均線位置</b>")
                current = indicators["current"]
                ma_info = []
                if indicators.get("ma5"):
                    rel = "▲" if current > indicators["ma5"] else "▼"
                    ma_info.append(f"MA5:{indicators['ma5']:.0f}{rel}")
                if indicators.get("ma10"):
                    rel = "▲" if current > indicators["ma10"] else "▼"
                    ma_info.append(f"MA10:{indicators['ma10']:.0f}{rel}")
                if indicators.get("ma20"):
                    rel = "▲" if current > indicators["ma20"] else "▼"
                    ma_info.append(f"MA20:{indicators['ma20']:.0f}{rel}")
                if ma_info:
                    lines.append(" | ".join(ma_info))
                if indicators.get("ma35"):
                    pos = "站上" if current > indicators["ma35"] else "跌破"
                    lines.append(f"MA35:{indicators['ma35']:.0f}（{pos}，距{abs(indicators['ma35_dist']):.1f}%）")
                if indicators.get("ma200"):
                    pos = "站上" if current > indicators["ma200"] else "跌破"
                    lines.append(f"MA200:{indicators['ma200']:.0f}（{pos}，距{abs(indicators['ma200_dist']):.1f}%）")

        # Cumulative accuracy
        history = _load_prediction_history()
        cum_total = sum(d.get("total", 0) for d in history.values())
        cum_correct = sum(d.get("correct", 0) for d in history.values())
        cum_accuracy = round(cum_correct / cum_total * 100, 1) if cum_total > 0 else 0
        if cum_total > 0:
            lines.append(f"\n🎯 累計命中率：{cum_accuracy}%（{cum_correct}/{cum_total}）")

        lines.append("\n⏰ 17:05 將推送日線/週線綜合分析")

        msg = "\n".join(lines)
        send_telegram_message(bot_token, chat_id, msg)
        logger.info("TWII market close Telegram brief sent")
    except Exception as e:
        logger.error(f"TWII market close Telegram brief failed: {e}")


# =============================================================================
# 6. Immediate Verification (Phase 3 — PLL Phase Detector)
# =============================================================================

def verify_last_prediction(completed_bar: dict) -> Optional[dict]:
    """Verify the last prediction against the just-completed bar.

    Checks if actual close is within support~resistance range.

    Args:
        completed_bar: The bar that just closed {time, open, high, low, close, volume}

    Returns:
        Verification result dict, or None if no pending prediction.
    """
    global _today_stats

    if not _predictions_today:
        return None

    # Find the last unverified prediction
    last_pred = None
    for p in reversed(_predictions_today):
        if p["is_within_range"] is None:
            last_pred = p
            break

    if last_pred is None:
        return None

    actual_close = completed_bar["close"]
    support = last_pred.get("support", 0)
    resistance = last_pred.get("resistance", 0)

    # Check if actual close is within support~resistance range
    if support > 0 and resistance > 0:
        is_within = support <= actual_close <= resistance
    elif support > 0:
        is_within = actual_close >= support
    elif resistance > 0:
        is_within = actual_close <= resistance
    else:
        is_within = False

    # Update prediction record
    last_pred["actual_close"] = actual_close
    last_pred["is_within_range"] = is_within

    # Update today's stats
    _today_stats["total"] += 1
    if is_within:
        _today_stats["correct"] += 1
        _today_stats["consecutive_failures"] = 0
    else:
        _today_stats["incorrect"] += 1
        _today_stats["consecutive_failures"] += 1

    _today_stats["accuracy"] = round(
        _today_stats["correct"] / _today_stats["total"] * 100, 1
    ) if _today_stats["total"] > 0 else 0

    save_today_stats()  # persist after every update

    mark = "✓" if is_within else "✗"
    logger.info(
        f"TWII verify: {last_pred['bar_time']} "
        f"S={support:.0f} R={resistance:.0f} actual={actual_close:.2f} "
        f"{mark} ({'in range' if is_within else 'out of range'})"
    )

    return {
        "bar_time": last_pred["bar_time"],
        "support": support,
        "resistance": resistance,
        "actual_close": actual_close,
        "is_within_range": is_within,
    }


def save_today_prediction_stats():
    """Save today's prediction stats to history file (called at end of day)."""
    if _today_stats["total"] == 0:
        return

    today_str = date.today().isoformat()
    history = _load_prediction_history()
    history[today_str] = {
        "total": _today_stats["total"],
        "correct": _today_stats["correct"],
        "incorrect": _today_stats["incorrect"],
        "accuracy": _today_stats["accuracy"],
        "details": [
            {
                "bar_time": p["bar_time"],
                "support": p.get("support"),
                "resistance": p.get("resistance"),
                "actual_close": p.get("actual_close"),
                "is_within_range": p.get("is_within_range"),
            }
            for p in _predictions_today if p.get("is_within_range") is not None
        ],
    }
    _save_prediction_history(history)
    logger.info(
        f"TWII prediction stats saved: {_today_stats['correct']}/{_today_stats['total']} "
        f"({_today_stats['accuracy']}%)"
    )


# =============================================================================
# 7. PLL Loop Filter — Bias Analysis & Prompt Feedback (Phase 5)
# =============================================================================

def _build_pll_feedback() -> str:
    """Build PLL feedback string for AI prompt injection.

    Analyzes recent prediction history to identify support/resistance accuracy.
    """
    history = _load_prediction_history()
    if not history:
        return ""

    # Get recent 10 days of stats
    sorted_dates = sorted(history.keys(), reverse=True)[:10]
    if not sorted_dates:
        return ""

    total_all = 0
    correct_all = 0
    range_too_wide = 0
    range_too_narrow = 0

    for d in sorted_dates:
        day_data = history[d]
        total_all += day_data.get("total", 0)
        correct_all += day_data.get("correct", 0)
        for detail in day_data.get("details", []):
            if not detail.get("is_within_range") and detail.get("support") and detail.get("resistance"):
                actual = detail.get("actual_close", 0)
                if actual < detail["support"]:
                    range_too_narrow += 1  # Support was too high
                elif actual > detail["resistance"]:
                    range_too_narrow += 1  # Resistance was too low

    if total_all == 0:
        return ""

    accuracy = round(correct_all / total_all * 100, 1)
    lines = ["\n【PLL 回饋修正】"]
    lines.append(f"近 {len(sorted_dates)} 天支撐壓力命中率：{accuracy}%（{correct_all}/{total_all}）")

    # Range accuracy feedback
    total_errors = total_all - correct_all
    if total_errors >= 3 and range_too_narrow >= 2:
        lines.append("⚠️ 支撐壓力範圍偏窄，實際收盤常超出範圍。建議適當擴大支撐壓力區間。")

    # Consecutive failure warning
    if _today_stats.get("consecutive_failures", 0) >= 2:
        lines.append(f"⚠️ 當日已連續失敗 {_today_stats['consecutive_failures']} 次，請重新評估支撐壓力區間")

    return "\n".join(lines) if len(lines) > 1 else ""


# =============================================================================
# 8. Post-Market Summary + 17:00 Daily/Weekly Integration (Phase 4)
# =============================================================================

def generate_intraday_summary() -> str:
    """Generate end-of-day 60-min line summary (called at 13:30).

    Returns: Summary text for inclusion in 17:00 report.
    """
    if not _today_bars:
        return ""

    # Today's OHLC from bars
    day_open = _today_bars[0]["open"]
    day_high = max(b["high"] for b in _today_bars)
    day_low = min(b["low"] for b in _today_bars)
    day_close = _today_bars[-1]["close"]
    day_change = day_close - day_open
    day_change_pct = round(day_change / day_open * 100, 2) if day_open > 0 else 0

    # Prediction stats
    total = _today_stats["total"]
    correct = _today_stats["correct"]
    accuracy = _today_stats["accuracy"]

    lines = [f"📊 60分線盤中回顧（{date.today().strftime('%m/%d')}）"]
    lines.append(f"加權指數：{day_open:.2f} → {day_close:.2f}（{day_change:+.2f}, {day_change_pct:+.2f}%）")
    lines.append(f"日內高低：{day_high:.2f} / {day_low:.2f}（振幅 {round((day_high-day_low)/day_open*100, 2)}%）")

    if total > 0:
        lines.append(f"AI 支撐壓力命中：{correct}/{total}（{accuracy}%）")

        # Detail each prediction
        for p in _predictions_today:
            if p.get("is_within_range") is not None:
                mark = "✓" if p["is_within_range"] else "✗"
                lines.append(
                    f"  {p['bar_time']}: S={p.get('support', 0):.0f} R={p.get('resistance', 0):.0f} "
                    f"實際={p.get('actual_close', 0):.2f} {mark}"
                )

    return "\n".join(lines)


def run_daily_integration() -> Optional[dict]:
    """17:00 integration: 60-min summary + daily analysis + weekly prediction.

    This is the main entry point called by the 17:00 scheduler.

    Returns:
        {
            "intraday_summary": str,
            "daily_analysis": str,
            "weekly_prediction": dict,
            "report_text": str,        # Full report for Telegram
            "should_expand": bool,      # Whether to use detailed format
        }
        or None on failure.
    """
    api_key = os.getenv("OPENROUTER_API_KEY", os.getenv("MINIMAX_API_KEY", ""))
    if not api_key:
        logger.warning("MINIMAX_API_KEY not set, skipping TWII daily integration")
        return None

    # 1. Intraday summary
    intraday_summary = generate_intraday_summary()

    # 2. Get TWII daily kline from DB
    try:
        from src.services.us_index_service import get_us_index_kline, get_us_index_weekly
        daily_kline = get_us_index_kline("TWII", 60)
        weekly_kline = get_us_index_weekly("TWII", 20)
    except Exception as e:
        logger.error(f"Failed to get TWII kline data: {e}")
        daily_kline = []
        weekly_kline = []

    if not daily_kline or len(daily_kline) < 10:
        logger.warning("Insufficient TWII daily kline for integration")
        return None

    # 3. Compute daily indicators
    from src.services.kline_analysis import compute_all_indicators
    daily_indicators = compute_all_indicators(daily_kline)

    # 4. Build integration prompt
    prompt = _build_integration_prompt(intraday_summary, daily_kline, daily_indicators, weekly_kline)

    # 5. Call AI
    data = json.dumps({
        "model": "MiniMax-M2.7",
        "messages": [
            {"role": "system", "content": "你是資深台股大盤分析師，負責整合盤中 60 分線、日線、週線做出綜合判斷。繁體中文回答。"},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": 1500,
        "temperature": 0.3,
    }).encode()

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    daily_analysis = ""
    try:
        req = urllib.request.Request(MINIMAX_API_URL, data=data, method="POST", headers=headers)
        result = json.loads(retry_urlopen(req, timeout=60, max_retries=2))
        content = result["choices"][0]["message"]["content"].strip()
        daily_analysis = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL).strip()
    except Exception as e:
        logger.error(f"TWII daily integration AI failed: {e}")
        return None

    if not daily_analysis:
        return None

    # 6. Extract weekly prediction from analysis
    weekly_prediction = _extract_weekly_prediction(daily_analysis)

    # 7. Save prediction for 5-day verification
    _save_daily_prediction(daily_kline, weekly_prediction, daily_analysis)

    # 8. Determine report format (adaptive)
    should_expand = _should_expand_report()

    # 9. Build final report
    report_text = _build_telegram_report(intraday_summary, daily_analysis, should_expand)

    return {
        "intraday_summary": intraday_summary,
        "daily_analysis": daily_analysis,
        "weekly_prediction": weekly_prediction,
        "report_text": report_text,
        "should_expand": should_expand,
    }


def _build_integration_prompt(intraday_summary: str, daily_kline: list,
                               daily_indicators: dict, weekly_kline: list) -> str:
    """Build the 17:00 integration analysis prompt."""

    # Format daily kline (last 10 days)
    daily_text = "\n".join([
        f"  {d['date']} O:{d['open']:.2f} H:{d['high']:.2f} L:{d['low']:.2f} C:{d['close']:.2f} V:{d['volume']}"
        for d in daily_kline[-10:]
    ])

    # Weekly kline (last 5 weeks)
    weekly_text = ""
    if weekly_kline and len(weekly_kline) >= 3:
        weekly_text = "\n".join([
            f"  {w['date']} O:{w['open']:.2f} H:{w['high']:.2f} L:{w['low']:.2f} C:{w['close']:.2f} V:{w['volume']}"
            for w in weekly_kline[-5:]
        ])

    # Daily indicators
    price = daily_indicators.get("price", {})
    ma = daily_indicators.get("ma", {})
    macd = daily_indicators.get("macd", {})

    indicator_text = f"""【日線指標】
- 收盤：{price.get('current')} 漲跌：{price.get('change')}（{price.get('change_pct')}%）
- MA5: {ma.get('ma5', {}).get('value')}  MA10: {ma.get('ma10', {}).get('value')}  MA20: {ma.get('ma20', {}).get('value')}  MA60: {ma.get('ma60', {}).get('value')}
- MACD DIF: {macd.get('dif')}  MACD: {macd.get('macd')}  柱狀: {macd.get('histogram')}"""

    # PLL feedback
    pll_feedback = _build_pll_feedback()

    # Daily prediction history (for weekly prediction accuracy)
    daily_pred_feedback = _build_daily_prediction_feedback()

    prompt = f"""# 任務
根據今日 60 分線盤中分析結果、日線技術指標、週線走勢，產出台灣加權指數綜合分析報告。

# 分析框架
1. 【60分線盤中回顧】：摘要今日走勢與 AI 命中率
2. 【日線技術分析】：MA/MACD 狀態、趨勢判斷
3. 【週線趨勢預測】：根據日線推導未來 5 個交易日方向、支撐位、壓力位
4. 【明日策略】：明日開盤觀察重點

# 格式要求
- 繁體中文，條列式
- 週線預測需明確給出：方向（偏多/偏空/中性）、支撐位、壓力位
- 結尾加上「⚠️ 以上為技術面分析，僅供研究參考。」

---

{intraday_summary}

{indicator_text}

【近10日 K 線】
{daily_text}
"""

    if weekly_text:
        prompt += f"""
【近5週週 K 線】
{weekly_text}
"""

    if pll_feedback:
        prompt += pll_feedback

    if daily_pred_feedback:
        prompt += daily_pred_feedback

    return prompt


def _build_daily_prediction_feedback() -> str:
    """Build feedback from recent daily predictions (5-day verification results)."""
    try:
        import psycopg2
        from psycopg2.extras import RealDictCursor
        from src.core.pg_client import DB_CONFIG

        conn = psycopg2.connect(**DB_CONFIG)
        try:
            cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute("""
                SELECT prediction_date, direction, price_at_prediction,
                       price_after_5d, actual_return_5d, is_correct_5d
                FROM ai_predictions
                WHERE stock_code = 'TWII' AND source = 'twii_daily'
                  AND is_correct_5d IS NOT NULL
                ORDER BY prediction_date DESC LIMIT 5
            """)
            records = [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()

        if not records:
            return ""

        total = len(records)
        correct = sum(1 for r in records if r["is_correct_5d"])
        accuracy = round(correct / total * 100, 1)

        lines = [f"\n【日線預測歷史回顧】"]
        lines.append(f"近 {total} 次日線→週線預測準確率：{accuracy}%")

        for r in records[:3]:
            dir_label = "偏多" if r["direction"] == "bullish" else "偏空" if r["direction"] == "bearish" else "中性"
            mark = "✓" if r["is_correct_5d"] else "✗"
            ret = r["actual_return_5d"] or 0
            lines.append(
                f"- {r['prediction_date']}：判斷「{dir_label}」"
                f"（{r['price_at_prediction']}→{r['price_after_5d']}，{ret:+.2f}%）{mark}"
            )

        failures = [r for r in records if not r["is_correct_5d"]]
        if failures:
            lines.append("⚠️ 請反思上述錯誤判斷，避免重複相同錯誤。")

        return "\n".join(lines)
    except Exception as e:
        logger.error(f"Failed to build daily prediction feedback: {e}")
        return ""


# =============================================================================
# 9. Weekly Prediction + Adaptive Report (Phase 5 + 6)
# =============================================================================

def _extract_weekly_prediction(analysis: str) -> dict:
    """Extract weekly direction prediction from AI analysis text."""
    text = analysis[:800]

    # Direction
    bullish_kw = ["偏多", "看多", "多頭", "看漲", "上攻"]
    bearish_kw = ["偏空", "看空", "空頭", "看跌", "下跌"]
    bull_count = sum(1 for kw in bullish_kw if kw in text)
    bear_count = sum(1 for kw in bearish_kw if kw in text)

    if bull_count > bear_count:
        direction = "bullish"
    elif bear_count > bull_count:
        direction = "bearish"
    else:
        direction = "neutral"

    # Extract support/resistance numbers near keywords
    support = _extract_price_from_text(analysis, ["支撐", "下檔"])
    resistance = _extract_price_from_text(analysis, ["壓力", "上檔", "目標"])

    return {
        "direction": direction,
        "support": support,
        "resistance": resistance,
    }


def _extract_price_from_text(text: str, keywords: list) -> Optional[float]:
    """Extract a price number near keywords in text."""
    for kw in keywords:
        idx = text.find(kw)
        if idx >= 0:
            segment = text[idx:idx + 50]
            match = re.search(r'(\d{4,6}(?:\.\d+)?)', segment)
            if match:
                val = float(match.group(1))
                if 5000 < val < 50000:  # reasonable TWII range
                    return val
    return None


def _save_daily_prediction(daily_kline: list, weekly_prediction: dict, daily_analysis: str = ""):
    """Save TWII daily prediction to ai_predictions table for 5-day verification."""
    try:
        from src.core.database import save_ai_prediction

        if not daily_kline:
            return

        current_price = daily_kline[-1]["close"]
        pred_date = date.today().isoformat()

        # Build meaningful key_reasoning from AI analysis (first 300 chars)
        intraday_acc = _today_stats.get('accuracy', 0)
        intraday_total = _today_stats.get('total', 0)
        reasoning_parts = []
        if intraday_total > 0:
            reasoning_parts.append(f"60min {_today_stats.get('correct',0)}/{intraday_total} 命中({intraday_acc}%)")
        if daily_analysis:
            # Store full AI analysis text
            reasoning_parts.append(daily_analysis)
        key_reasoning = " | ".join(reasoning_parts) if reasoning_parts else "daily integration"

        save_ai_prediction(
            stock_code="TWII",
            user_id="system",
            prediction_date=pred_date,
            price_at_prediction=current_price,
            direction=weekly_prediction.get("direction", "neutral"),
            target_price=weekly_prediction.get("resistance"),
            stop_loss=weekly_prediction.get("support"),
            key_reasoning=key_reasoning,
            source="twii_daily",
        )
        logger.info(f"TWII daily prediction saved: {weekly_prediction['direction']} at {current_price}")
    except Exception as e:
        logger.error(f"Failed to save TWII daily prediction: {e}")


def _should_expand_report() -> bool:
    """Determine whether to use expanded (detailed) Telegram report format.

    Triggers:
    - Consecutive failures >= 3
    - Today's accuracy < 30% or > 90% (with enough samples)
    - Daily prediction consecutive correct >= 5
    """
    # Consecutive intraday failures
    if _today_stats.get("consecutive_failures", 0) >= 3:
        return True

    # Extreme accuracy today
    total = _today_stats.get("total", 0)
    if total >= 3:
        acc = _today_stats.get("accuracy", 50)
        if acc < 30 or acc > 90:
            return True

    # Check daily prediction streak
    try:
        history = _load_prediction_history()
        sorted_dates = sorted(history.keys(), reverse=True)[:5]
        consecutive_correct = 0
        for d in sorted_dates:
            if history[d].get("accuracy", 0) >= 75:
                consecutive_correct += 1
            else:
                break
        if consecutive_correct >= 5:
            return True
    except Exception:
        pass

    return False


def _build_telegram_report(intraday_summary: str, daily_analysis: str, expand: bool) -> str:
    """Build Telegram report text (adaptive format).

    Args:
        intraday_summary: 60-min summary text
        daily_analysis: AI daily+weekly analysis text
        expand: Whether to use detailed format
    """
    today_str = date.today().strftime("%m/%d")
    total = _today_stats.get("total", 0)
    correct = _today_stats.get("correct", 0)
    accuracy = _today_stats.get("accuracy", 0)

    # Get cumulative stats
    history = _load_prediction_history()
    cum_total = sum(d.get("total", 0) for d in history.values())
    cum_correct = sum(d.get("correct", 0) for d in history.values())
    cum_accuracy = round(cum_correct / cum_total * 100, 1) if cum_total > 0 else 0

    if expand:
        # Detailed format
        header = f"🔄 <b>AI 大盤回饋日報</b>（{today_str}）"
        if _today_stats.get("consecutive_failures", 0) >= 3:
            header += " ⚠️ 連續失敗警示"
        elif accuracy > 90 and total >= 3:
            header += " 🎯 高準確率"

        report = f"{header}\n\n{intraday_summary}\n\n"
        report += f"📈 <b>日線/週線綜合分析</b>\n{daily_analysis}\n\n"
        report += f"🎯 累計準確率：<b>{cum_accuracy}%</b>（{cum_correct}/{cum_total}）"
    else:
        # Concise format
        # Extract key points from daily_analysis (first 2 lines or 150 chars)
        analysis_brief = daily_analysis.split("\n")
        brief_text = "\n".join(analysis_brief[:4]) if analysis_brief else daily_analysis[:150]

        report = f"🔄 <b>AI 大盤回饋日報</b>（{today_str}）\n\n"
        if total > 0:
            report += f"📊 60分線：{correct}/{total} 命中（{accuracy}%）\n"
        report += f"{brief_text}\n\n"
        report += f"🎯 累計準確率：<b>{cum_accuracy}%</b>（{cum_correct}/{cum_total}）"

    return report


# =============================================================================
# 10. Main Orchestration (called by server.py background task)
# =============================================================================

def on_tick(now: datetime = None):
    """Called every 60 seconds during market hours (09:00~13:30).

    Handles:
    1. Fetch TWII realtime quote
    2. Update current bar
    3. Detect bar completion → finalize → verify → predict → Telegram push
    """
    if now is None:
        now = datetime.now()

    # Fetch realtime quote
    quote = fetch_twii_realtime()
    if not quote:
        logger.debug(f"on_tick: fetch_twii_realtime returned None at {now.strftime('%H:%M:%S')}")
        return

    # Determine current slot
    slot = get_current_bar_slot(now)
    if slot is None:
        logger.debug(f"on_tick: outside market hours at {now.strftime('%H:%M:%S')}")
        return

    slot_idx, start_time, end_time = slot

    # Check if previous bar just completed (first tick of new slot)
    if _current_bar and _current_bar.get("time_slot") != start_time:
        logger.info(f"on_tick: bar slot changed {_current_bar.get('time_slot')} → {start_time}, finalizing...")
        # Previous bar ended — finalize it
        completed_bar = finalize_current_bar(now)
        if completed_bar:
            # Step 1: Verify last prediction
            verification = verify_last_prediction(completed_bar)
            logger.info(f"on_tick: verification={'correct' if verification and verification.get('is_within_range') else 'incorrect' if verification else 'none'}")

            # Step 2: Save bars to file
            save_today_bars_to_file()

            # Step 3: Make new prediction (except after last bar)
            prediction = None
            if slot_idx < len(BAR_SLOTS) - 1:  # Not the last slot
                logger.info(f"on_tick: calling predict_next_bar (recent_bars={len(get_recent_bars(5))})")
                prediction = predict_next_bar()
                if prediction:
                    record_prediction(start_time, prediction)
                    logger.info(f"on_tick: prediction recorded: S={prediction.get('support')} R={prediction.get('resistance')}")
                else:
                    logger.warning("on_tick: predict_next_bar returned None — no prediction made")
            else:
                logger.info(f"on_tick: last bar slot, skipping prediction")

            # Step 4: Push Telegram notification
            _push_intraday_telegram(completed_bar, verification, prediction, start_time)
        else:
            logger.warning("on_tick: finalize_current_bar returned None")

    # Update current bar with new tick
    update_current_bar(quote, now)


def on_market_close(now: datetime = None):
    """Called at 13:30 when market closes.

    Handles:
    1. Finalize last bar
    2. Verify last prediction
    3. Generate intraday summary
    4. Save all stats
    """
    if now is None:
        now = datetime.now()

    # Finalize last bar if still open
    if _current_bar:
        completed_bar = finalize_current_bar(now)
        if completed_bar:
            verify_last_prediction(completed_bar)

    # Save bars and stats
    save_today_bars_to_file()
    save_today_prediction_stats()

    summary = generate_intraday_summary()
    logger.info(f"TWII market close summary:\n{summary}")

    # Push closing brief to Telegram
    _push_market_close_telegram(summary)


def on_daily_integration() -> Optional[str]:
    """Called at 17:00 for full integration analysis.

    Returns: Telegram report text, or None.
    """
    result = run_daily_integration()
    if not result:
        return None

    # Save to notes
    try:
        from src.core.database import add_note
        today_str = date.today().isoformat()
        today_display = date.today().strftime("%Y/%m/%d")
        title = f"🔄 AI 大盤回饋學習日報 — {today_display}"
        content = (
            f"{result['intraday_summary']}\n\n"
            f"{'='*40}\n\n"
            f"{result['daily_analysis']}"
        )
        add_note(
            stock_code="TWII",
            content=content,
            user_id="system",
            news_date=today_str,
            title=title,
        )
        logger.info("TWII daily integration note saved")
    except Exception as e:
        logger.error(f"Failed to save TWII integration note: {e}")

    # Reset daily state for tomorrow
    reset_daily_state()

    return result.get("report_text")


def get_intraday_feedback_for_news_ai() -> str:
    """Get 60-min feedback text to inject into the daily news AI analysis prompt.

    Called by finance_news.run_daily_ai_analysis() at 17:00.
    """
    if not _today_bars and not _today_stats.get("total"):
        return ""

    lines = ["\n# 大盤 60 分線 AI 回饋"]
    summary = generate_intraday_summary()
    if summary:
        lines.append(summary)

    # Add PLL feedback
    pll = _build_pll_feedback()
    if pll:
        lines.append(pll)

    return "\n".join(lines) if len(lines) > 1 else ""
