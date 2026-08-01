"""K-Line Technical Analysis Service.

Provides:
1. Technical indicator calculations (MA, MACD, RSI, Bollinger Bands, Volume)
2. Candlestick pattern recognition
3. LLM-powered comprehensive analysis
"""

import os
import json
import urllib.request
import logging
from typing import Optional

logger = logging.getLogger(__name__)

MINIMAX_API_URL = "https://api.minimax.io/v1/chat/completions"


# =============================================================================
# 1. Technical Indicator Calculations
# =============================================================================

def compute_ma(closes: list[float], period: int) -> list[Optional[float]]:
    """Compute Simple Moving Average."""
    result = [None] * len(closes)
    for i in range(period - 1, len(closes)):
        result[i] = sum(closes[i - period + 1:i + 1]) / period
    return result


def compute_ema(closes: list[float], period: int) -> list[Optional[float]]:
    """Compute Exponential Moving Average."""
    result = [None] * len(closes)
    if len(closes) < period:
        return result
    # First EMA = SMA
    sma = sum(closes[:period]) / period
    result[period - 1] = sma
    multiplier = 2 / (period + 1)
    for i in range(period, len(closes)):
        result[i] = (closes[i] - result[i - 1]) * multiplier + result[i - 1]
    return result


def compute_macd(closes: list[float], fast=12, slow=26, signal=9) -> dict:
    """Compute MACD (DIF, MACD signal, histogram)."""
    ema_fast = compute_ema(closes, fast)
    ema_slow = compute_ema(closes, slow)

    # DIF = EMA(fast) - EMA(slow)
    dif = [None] * len(closes)
    for i in range(len(closes)):
        if ema_fast[i] is not None and ema_slow[i] is not None:
            dif[i] = ema_fast[i] - ema_slow[i]

    # MACD signal = EMA(DIF, signal)
    dif_values = [v for v in dif if v is not None]
    if len(dif_values) < signal:
        return {"dif": None, "macd": None, "histogram": None}

    dif_ema = compute_ema(dif_values, signal)
    macd_signal = dif_ema[-1] if dif_ema else None
    last_dif = dif_values[-1] if dif_values else None
    histogram = (last_dif - macd_signal) if (last_dif is not None and macd_signal is not None) else None

    return {
        "dif": round(last_dif, 3) if last_dif else None,
        "macd": round(macd_signal, 3) if macd_signal else None,
        "histogram": round(histogram, 3) if histogram else None,
    }


def compute_rsi(closes: list[float], period=14) -> Optional[float]:
    """Compute RSI (Relative Strength Index)."""
    if len(closes) < period + 1:
        return None
    gains = []
    losses = []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))

    # Use last `period` values with smoothing
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)


def compute_bollinger(closes: list[float], period=20, num_std=2) -> dict:
    """Compute Bollinger Bands."""
    if len(closes) < period:
        return {"upper": None, "middle": None, "lower": None, "bandwidth": None}

    recent = closes[-period:]
    middle = sum(recent) / period
    variance = sum((x - middle) ** 2 for x in recent) / period
    std = variance ** 0.5

    upper = middle + num_std * std
    lower = middle - num_std * std
    bandwidth = ((upper - lower) / middle) * 100 if middle != 0 else 0

    return {
        "upper": round(upper, 2),
        "middle": round(middle, 2),
        "lower": round(lower, 2),
        "bandwidth": round(bandwidth, 2),
    }


def compute_volume_analysis(volumes: list[int], period=5) -> dict:
    """Analyze volume trends."""
    if len(volumes) < period + 1:
        return {"avg_volume": None, "volume_ratio": None, "trend": "不明"}

    recent_avg = sum(volumes[-period:]) / period
    prev_avg = sum(volumes[-period * 2:-period]) / period if len(volumes) >= period * 2 else recent_avg
    current = volumes[-1]

    volume_ratio = current / recent_avg if recent_avg > 0 else 0
    trend_ratio = recent_avg / prev_avg if prev_avg > 0 else 1

    if trend_ratio > 1.3:
        trend = "放量"
    elif trend_ratio < 0.7:
        trend = "縮量"
    else:
        trend = "平量"

    return {
        "current_volume": current,
        "avg_volume_5d": int(recent_avg),
        "volume_ratio": round(volume_ratio, 2),
        "trend": trend,
    }


def compute_all_indicators(kline_data: list[dict]) -> dict:
    """Compute all technical indicators from K-line data."""
    if not kline_data or len(kline_data) < 5:
        return {"error": "資料不足，無法計算指標"}

    closes = [d["close"] for d in kline_data]
    volumes = [d["volume"] for d in kline_data]
    current_price = closes[-1]
    prev_price = closes[-2] if len(closes) >= 2 else current_price

    # MA
    ma5 = compute_ma(closes, 5)
    ma10 = compute_ma(closes, 10)
    ma20 = compute_ma(closes, 20)
    ma60 = compute_ma(closes, 60)

    # MA direction (last 3 values)
    def ma_direction(ma_list):
        vals = [v for v in ma_list[-3:] if v is not None]
        if len(vals) < 2:
            return "—"
        if vals[-1] > vals[-2]:
            return "↑"
        elif vals[-1] < vals[-2]:
            return "↓"
        return "→"

    # Price vs MA position
    def price_vs_ma(ma_val):
        if ma_val is None:
            return "—"
        pct = ((current_price - ma_val) / ma_val) * 100
        return f"{'+' if pct >= 0 else ''}{pct:.1f}%"

    indicators = {
        "price": {
            "current": current_price,
            "change": round(current_price - prev_price, 2),
            "change_pct": round(((current_price - prev_price) / prev_price) * 100, 2) if prev_price else 0,
            "date": kline_data[-1]["date"],
        },
        "ma": {
            "ma5": {"value": round(ma5[-1], 2) if ma5[-1] else None, "direction": ma_direction(ma5), "distance": price_vs_ma(ma5[-1])},
            "ma10": {"value": round(ma10[-1], 2) if ma10[-1] else None, "direction": ma_direction(ma10), "distance": price_vs_ma(ma10[-1])},
            "ma20": {"value": round(ma20[-1], 2) if ma20[-1] else None, "direction": ma_direction(ma20), "distance": price_vs_ma(ma20[-1])},
            "ma60": {"value": round(ma60[-1], 2) if ma60[-1] else None, "direction": ma_direction(ma60), "distance": price_vs_ma(ma60[-1])},
        },
        "macd": compute_macd(closes),
        "rsi": compute_rsi(closes),
        "bollinger": compute_bollinger(closes),
        "volume": compute_volume_analysis(volumes),
    }

    # MA arrangement
    ma_vals = [v for v in [ma5[-1], ma10[-1], ma20[-1], ma60[-1]] if v is not None]
    if len(ma_vals) >= 3:
        if ma_vals == sorted(ma_vals, reverse=True):
            indicators["ma_arrangement"] = "多頭排列"
        elif ma_vals == sorted(ma_vals):
            indicators["ma_arrangement"] = "空頭排列"
        else:
            indicators["ma_arrangement"] = "糾結"
    else:
        indicators["ma_arrangement"] = "資料不足"

    return indicators


# =============================================================================
# 2. Candlestick Pattern Recognition
# =============================================================================

def _body(candle: dict) -> float:
    """Real body size (absolute)."""
    return abs(candle["close"] - candle["open"])


def _upper_shadow(candle: dict) -> float:
    """Upper shadow length."""
    return candle["high"] - max(candle["close"], candle["open"])


def _lower_shadow(candle: dict) -> float:
    """Lower shadow length."""
    return min(candle["close"], candle["open"]) - candle["low"]


def _is_bullish(candle: dict) -> bool:
    return candle["close"] > candle["open"]


def _is_bearish(candle: dict) -> bool:
    return candle["close"] < candle["open"]


def _candle_range(candle: dict) -> float:
    return candle["high"] - candle["low"]


def detect_patterns(kline_data: list[dict]) -> list[dict]:
    """Detect candlestick patterns from K-line data.

    Returns list of detected patterns with name, signal, and description.
    """
    if len(kline_data) < 3:
        return []

    patterns = []
    # Analyze last 5 candles for patterns
    lookback = min(5, len(kline_data))
    candles = kline_data[-lookback:]

    # Current and previous candles
    c = candles[-1]  # current
    p = candles[-2] if len(candles) >= 2 else None  # previous
    pp = candles[-3] if len(candles) >= 3 else None  # 2 bars ago

    cr = _candle_range(c)
    body = _body(c)
    upper = _upper_shadow(c)
    lower = _lower_shadow(c)

    # Avoid division by zero
    if cr == 0:
        cr = 0.001

    # --- Single candle patterns ---

    # Doji (十字線)
    if body <= cr * 0.1:
        patterns.append({
            "name": "十字線 (Doji)",
            "signal": "中性/反轉",
            "description": "開盤=收盤，多空力道均衡，可能反轉信號"
        })

    # Hammer (錘子線) — small body at top, long lower shadow
    if (lower >= body * 2 and upper <= body * 0.5 and body > 0
            and lower >= cr * 0.6):
        signal = "看多" if _is_bullish(c) else "看多（需確認）"
        patterns.append({
            "name": "錘子線 (Hammer)",
            "signal": signal,
            "description": "下影線長，出現在下跌趨勢底部為反轉信號"
        })

    # Hanging Man (上吊線) — same shape as hammer but in uptrend
    if (lower >= body * 2 and upper <= body * 0.5 and body > 0
            and lower >= cr * 0.6):
        # Check if in uptrend (previous 3 candles rising)
        if p and pp and p["close"] > pp["close"]:
            patterns.append({
                "name": "上吊線 (Hanging Man)",
                "signal": "看空",
                "description": "形似錘子但出現在上漲趨勢頂部，警示反轉"
            })

    # Shooting Star (流星線) — long upper shadow at top
    if (upper >= body * 2 and lower <= body * 0.5 and body > 0
            and upper >= cr * 0.6):
        patterns.append({
            "name": "流星線 (Shooting Star)",
            "signal": "看空",
            "description": "上影線長，出現在上漲趨勢頂部為反轉信號"
        })

    # Inverted Hammer (倒錘子)
    if (upper >= body * 2 and lower <= body * 0.5 and body > 0
            and upper >= cr * 0.6):
        if p and _is_bearish(p):
            patterns.append({
                "name": "倒錘子 (Inverted Hammer)",
                "signal": "看多（需確認）",
                "description": "出現在下跌趨勢，隔日若高開則確認反轉"
            })

    # Marubozu (光頭光腳)
    if body >= cr * 0.9:
        if _is_bullish(c):
            patterns.append({
                "name": "大陽線 (Bullish Marubozu)",
                "signal": "強烈看多",
                "description": "實體長無影線，買方強勢主導"
            })
        else:
            patterns.append({
                "name": "大陰線 (Bearish Marubozu)",
                "signal": "強烈看空",
                "description": "實體長無影線，賣方強勢主導"
            })

    # --- Two candle patterns ---
    if p:
        p_body = _body(p)
        p_range = _candle_range(p)

        # Bullish Engulfing (多頭吞噬)
        if (_is_bearish(p) and _is_bullish(c)
                and c["open"] <= p["close"] and c["close"] >= p["open"]
                and body > p_body):
            patterns.append({
                "name": "多頭吞噬 (Bullish Engulfing)",
                "signal": "看多",
                "description": "陽線完全包覆前一根陰線，反轉向上信號"
            })

        # Bearish Engulfing (空頭吞噬)
        if (_is_bullish(p) and _is_bearish(c)
                and c["open"] >= p["close"] and c["close"] <= p["open"]
                and body > p_body):
            patterns.append({
                "name": "空頭吞噬 (Bearish Engulfing)",
                "signal": "看空",
                "description": "陰線完全包覆前一根陽線，反轉向下信號"
            })

        # Piercing Line (貫穿線)
        if (_is_bearish(p) and _is_bullish(c)
                and c["open"] < p["low"]
                and c["close"] > (p["open"] + p["close"]) / 2
                and c["close"] < p["open"]):
            patterns.append({
                "name": "貫穿線 (Piercing Line)",
                "signal": "看多",
                "description": "跳空低開後收至前一根實體50%以上"
            })

        # Dark Cloud Cover (烏雲蓋頂)
        if (_is_bullish(p) and _is_bearish(c)
                and c["open"] > p["high"]
                and c["close"] < (p["open"] + p["close"]) / 2
                and c["close"] > p["open"]):
            patterns.append({
                "name": "烏雲蓋頂 (Dark Cloud Cover)",
                "signal": "看空",
                "description": "跳空高開後收至前一根實體50%以下"
            })

    # --- Three candle patterns ---
    if p and pp:
        # Morning Star (晨星)
        if (_is_bearish(pp) and _body(pp) > _candle_range(pp) * 0.5
                and _body(p) <= _candle_range(p) * 0.3
                and _is_bullish(c) and body > _candle_range(c) * 0.5
                and c["close"] > (pp["open"] + pp["close"]) / 2):
            patterns.append({
                "name": "晨星 (Morning Star)",
                "signal": "看多",
                "description": "大陰線+小實體+大陽線，底部反轉信號"
            })

        # Evening Star (暮星)
        if (_is_bullish(pp) and _body(pp) > _candle_range(pp) * 0.5
                and _body(p) <= _candle_range(p) * 0.3
                and _is_bearish(c) and body > _candle_range(c) * 0.5
                and c["close"] < (pp["open"] + pp["close"]) / 2):
            patterns.append({
                "name": "暮星 (Evening Star)",
                "signal": "看空",
                "description": "大陽線+小實體+大陰線，頂部反轉信號"
            })

        # Three White Soldiers (紅三兵)
        if (all(_is_bullish(x) for x in [pp, p, c])
                and p["close"] > pp["close"] and c["close"] > p["close"]
                and p["open"] > pp["open"] and c["open"] > p["open"]):
            patterns.append({
                "name": "紅三兵 (Three White Soldiers)",
                "signal": "強烈看多",
                "description": "連續三根陽線，開盤價逐步墊高，強勢上攻"
            })

        # Three Black Crows (黑三兵)
        if (all(_is_bearish(x) for x in [pp, p, c])
                and p["close"] < pp["close"] and c["close"] < p["close"]
                and p["open"] < pp["open"] and c["open"] < p["open"]):
            patterns.append({
                "name": "黑三兵 (Three Black Crows)",
                "signal": "強烈看空",
                "description": "連續三根陰線，開盤價逐步降低，弱勢下殺"
            })

    return patterns


# =============================================================================
# 3. LLM Comprehensive Analysis
# =============================================================================

# Conversation history for AI analysis (shared with semantic_search)
_analysis_history: dict[str, list] = {}
MAX_ANALYSIS_HISTORY = 5


def _build_analysis_prompt(stock_code: str, stock_name: str, period: str,
                           indicators: dict, patterns: list[dict],
                           recent_kline: list[dict], user_id: str = "default") -> str:
    """Build the analysis prompt for LLM."""

    # Format indicators
    price = indicators.get("price", {})
    ma = indicators.get("ma", {})
    macd = indicators.get("macd", {})
    rsi = indicators.get("rsi")
    boll = indicators.get("bollinger", {})
    vol = indicators.get("volume", {})
    ma_arr = indicators.get("ma_arrangement", "")

    # Load user-defined framework first (needed to decide which indicators to include)
    default_framework = """1. **趨勢判斷**：{根據均線排列、價格位置判斷上升/下降/盤整}
2. **關鍵價位**：{找出支撐位與壓力位}
3. **指標解讀**：{解讀 MACD/RSI/布林帶/量能}
4. **型態分析**：{分析偵測到的K線型態意義}
5. **綜合判斷**：{給出短線1-5日與中線5-20日操作方向}"""

    try:
        from src.core.database import get_analysis_preferences
        prefs = get_analysis_preferences(user_id)
        custom_framework = prefs.get("analysis_framework", "").strip()
        if custom_framework:
            framework_text = custom_framework
        else:
            framework_text = default_framework
    except Exception:
        framework_text = default_framework

    # Strip comments: everything after # on each line is a comment (not sent to AI)
    framework_lines = []
    for line in framework_text.split('\n'):
        # Find # that is not inside {} brackets
        in_bracket = 0
        comment_pos = -1
        for i, ch in enumerate(line):
            if ch == '{':
                in_bracket += 1
            elif ch == '}':
                in_bracket -= 1
            elif ch == '#' and in_bracket == 0:
                comment_pos = i
                break
        if comment_pos >= 0:
            framework_lines.append(line[:comment_pos].rstrip())
        else:
            framework_lines.append(line)
    framework_for_ai = '\n'.join(framework_lines)

    # Use stripped version for indicator detection
    framework_check = framework_for_ai.lower()

    indicator_text = f"""【價格】
- 最新收盤：{price.get('current')}  漲跌：{price.get('change')}（{price.get('change_pct')}%）
- 日期：{price.get('date')}

【均線】排列：{ma_arr}
- MA5:  {ma.get('ma5', {}).get('value')} {ma.get('ma5', {}).get('direction')} 距離:{ma.get('ma5', {}).get('distance')}
- MA10: {ma.get('ma10', {}).get('value')} {ma.get('ma10', {}).get('direction')} 距離:{ma.get('ma10', {}).get('distance')}
- MA20: {ma.get('ma20', {}).get('value')} {ma.get('ma20', {}).get('direction')} 距離:{ma.get('ma20', {}).get('distance')}
- MA60: {ma.get('ma60', {}).get('value')} {ma.get('ma60', {}).get('direction')} 距離:{ma.get('ma60', {}).get('distance')}

【MACD】
- DIF: {macd.get('dif')}  MACD: {macd.get('macd')}  柱狀: {macd.get('histogram')}"""

    # Conditionally include RSI and Bollinger based on framework content (comments stripped)
    if 'rsi' in framework_check:
        indicator_text += f"""

【RSI(14)】{rsi}"""

    if '布林' in framework_check or 'bollinger' in framework_check:
        indicator_text += f"""

【布林帶(20,2)】
- 上軌: {boll.get('upper')}  中軌: {boll.get('middle')}  下軌: {boll.get('lower')}
- 帶寬: {boll.get('bandwidth')}%"""

    indicator_text += f"""

【量能】
- 今量: {vol.get('current_volume')}  5日均量: {vol.get('avg_volume_5d')}
- 量比: {vol.get('volume_ratio')}  趨勢: {vol.get('trend')}"""

    # Format patterns
    if patterns:
        pattern_text = "\n".join([
            f"- {p['name']}（{p['signal']}）：{p['description']}"
            for p in patterns
        ])
    else:
        pattern_text = "- 無明顯型態"

    # Format recent K-line (last 10 bars)
    kline_text = "\n".join([
        f"  {d['date']} O:{d['open']} H:{d['high']} L:{d['low']} C:{d['close']} V:{d['volume']}"
        for d in recent_kline[-10:]
    ])

    period_name = {"daily": "日線", "weekly": "週線", "monthly": "月線"}.get(period, period)

    prompt = f"""# 角色
你是一位擁有 20 年經驗的「資深技術分析師」，專精台股技術面分析。

# 任務
根據以下 {stock_code} {stock_name} 的{period_name}技術指標、K線型態、近期走勢，產出專業的技術分析報告。

# 分析框架
{framework_for_ai}

# 格式要求
- 繁體中文，條列式
- 先用 1-2 句給出核心結論
- 嚴格依照上方「分析框架」的段落結構輸出，不要自行增加額外段落
- 結尾加上「⚠️ 以上為技術面分析，僅供研究參考，不構成投資建議。」

---
{indicator_text}

【K線型態偵測】
{pattern_text}

【近10日K線】
{kline_text}
"""
    return prompt


def analyze_kline(stock_code: str, stock_name: str, kline_data: list[dict],
                  period: str = "daily", user_id: str = "default") -> dict:
    """Run full K-line technical analysis with LLM.

    Returns:
        {
            "indicators": dict,
            "patterns": list,
            "analysis": str,  # LLM generated text
            "error": str | None
        }
    """
    # Step 1: Compute indicators
    indicators = compute_all_indicators(kline_data)
    if "error" in indicators:
        return {"indicators": {}, "patterns": [], "analysis": "", "error": indicators["error"]}

    # Step 2: Detect patterns
    patterns = detect_patterns(kline_data)

    # Step 3: Call LLM
    api_key = os.getenv("MINIMAX_API_KEY", "")
    if not api_key:
        return {
            "indicators": indicators,
            "patterns": patterns,
            "analysis": "⚠️ AI 未設定（缺少 MINIMAX_API_KEY），僅顯示技術指標數據。",
            "error": None
        }

    prompt = _build_analysis_prompt(stock_code, stock_name, period, indicators, patterns, kline_data, user_id)

    # Inject historical accuracy into prompt if available
    try:
        from src.core.database import get_analysis_accuracy
        accuracy = get_analysis_accuracy(stock_code, user_id)
        if accuracy["total"] > 0:
            accuracy_note = f"\n\n【歷史分析紀錄】此股票過去 {accuracy['total']} 次 AI 分析中，{accuracy['correct']} 次判斷正確，{accuracy['incorrect']} 次判斷錯誤，正確率 {accuracy['accuracy']}%。請參考歷史表現調整信心度。"
            prompt += accuracy_note
    except Exception:
        pass

    # Inject user preferences into prompt
    try:
        from src.core.database import get_analysis_preferences
        prefs = get_analysis_preferences(user_id)
        pref_parts = []
        if prefs.get("trading_style"):
            pref_parts.append(f"操作風格：{prefs['trading_style']}")
        if prefs.get("preferred_indicators"):
            pref_parts.append(f"重視指標：{prefs['preferred_indicators']}")
        if prefs.get("risk_tolerance"):
            pref_parts.append(f"風險偏好：{prefs['risk_tolerance']}")
        if prefs.get("custom_prompt"):
            pref_parts.append(f"其他要求：{prefs['custom_prompt']}")
        if pref_parts:
            prompt += "\n\n【使用者分析偏好】\n" + "\n".join(pref_parts) + "\n請根據以上偏好調整分析方向和建議。"
    except Exception:
        pass

    # Inject related stocks (same industry) comparison
    try:
        import psycopg2
        from psycopg2.extras import RealDictCursor
        from src.core.pg_client import DB_CONFIG
        conn = psycopg2.connect(**DB_CONFIG)
        try:
            cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute("SELECT industry FROM stock_basic WHERE stock_code = %s", (stock_code,))
            row = cur.fetchone()
            if row and row["industry"]:
                industry = row["industry"]
                cur.execute("""
                    SELECT di.stock_code, sb.stock_name, di.close, di.change_pct,
                           di.ma_arrangement, di.rsi14, di.volume_ratio
                    FROM daily_indicators di
                    JOIN stock_basic sb ON sb.stock_code = di.stock_code
                    WHERE sb.industry = %s AND di.date = (SELECT MAX(date) FROM daily_indicators)
                      AND di.stock_code != %s
                    ORDER BY di.volume DESC LIMIT 5
                """, (industry, stock_code))
                related = cur.fetchall()
                if related:
                    related_text = "\n".join([
                        f"  {r['stock_code']} {r['stock_name']}: {r['close']} ({'+' if (r['change_pct'] or 0)>=0 else ''}{r['change_pct']}%) RSI={r['rsi14']} {r['ma_arrangement']} 量比={r['volume_ratio']}"
                        for r in related
                    ])
                    prompt += f"\n\n【同產業（{industry}）比較】\n{related_text}\n請參考同業表現判斷個股相對強弱。"
        finally:
            conn.close()
    except Exception:
        pass

    # Build messages with history
    history = _analysis_history.get(user_id, [])
    messages = [{"role": "system", "content": "你是一位資深台股技術分析師，根據技術指標和K線型態提供專業分析。繁體中文回答。"}]
    messages.extend(history)
    messages.append({"role": "user", "content": prompt})

    data = json.dumps({
        "model": "MiniMax-M2.7",
        "messages": messages,
        "max_tokens": 2500,
        "temperature": 0.3,
    }).encode()

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    try:
        req = urllib.request.Request(MINIMAX_API_URL, data=data, method="POST", headers=headers)
        with urllib.request.urlopen(req, timeout=60) as r:
            result = json.loads(r.read())
            content = result["choices"][0]["message"]["content"].strip()
            # Remove <think>...</think> blocks
            import re
            analysis = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL).strip()
            if not analysis:
                analysis = "⚠️ AI 回應為空，請重試。"
            else:
                # Save to analysis history
                _save_analysis_history(user_id, stock_code, analysis)
    except Exception as e:
        logger.error(f"AI kline analysis failed: {e}")
        analysis = f"⚠️ AI 分析失敗：{e}"

    return {
        "indicators": indicators,
        "patterns": patterns,
        "analysis": analysis,
        "error": None
    }


def _save_analysis_history(user_id: str, stock_code: str, analysis: str):
    """Save analysis to conversation history for follow-up questions."""
    if user_id not in _analysis_history:
        _analysis_history[user_id] = []

    summary = f"[{stock_code} 技術分析結果] {analysis[:500]}"
    _analysis_history[user_id].append({"role": "assistant", "content": summary})

    # Keep only last N rounds
    while len(_analysis_history[user_id]) > MAX_ANALYSIS_HISTORY * 2:
        _analysis_history[user_id].pop(0)


def get_analysis_history(user_id: str = "default") -> list[dict]:
    """Get analysis history for a user (used by semantic_search for context)."""
    return _analysis_history.get(user_id, [])
