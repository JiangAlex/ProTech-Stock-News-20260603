"""Weekly Watchlist Batch AI Analysis — 每週日 18:00 自選股全面 AI 分析。

每支自選股都會：
1. 取得最新日線 K 線資料
2. 查詢該股過去的 AI 分析記錄（作為上下文）
3. 進行新的 AI 技術分析（包含歷史分析對比）
4. 結果存入備註 & 彙整發送 Telegram
"""

import json
import logging
import os
import re
import time
import urllib.request
from datetime import date

import psycopg2
from psycopg2.extras import RealDictCursor

from src.core.pg_client import DB_CONFIG, get_daily_kline, get_all_stocks
from src.services.kline_analysis import compute_all_indicators, detect_patterns
from src.services.telegram_service import send_telegram_message
from src.core.database import add_note

logger = logging.getLogger(__name__)

MINIMAX_API_URL = "https://api.minimax.io/v1/chat/completions"


def get_all_watchlist_stocks() -> list[dict]:
    """取得所有使用者的自選股（去重）。
    Returns: [{"code": str, "name": str, "user_id": str}]
    """
    conn = psycopg2.connect(**DB_CONFIG)
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT DISTINCT stock_code, stock_name, user_id
            FROM watchlist
            WHERE stock_code != '' AND stock_code NOT IN ('TWII', 'DJI', 'IXIC', 'SOX')
            ORDER BY stock_code
        """)
        rows = cur.fetchall()
        return [{"code": r["stock_code"], "name": r["stock_name"] or r["stock_code"], "user_id": r["user_id"]}
                for r in rows]
    finally:
        conn.close()


def get_past_analyses(stock_code: str, limit: int = 3) -> list[dict]:
    """取得某檔股票過去的 AI 分析記錄。
    Returns: [{"date": str, "content": str}]
    """
    conn = psycopg2.connect(**DB_CONFIG)
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT news_date, content
            FROM watchlist_notes
            WHERE stock_code = %s AND title LIKE '%%AI 技術分析%%'
            ORDER BY created_at DESC
            LIMIT %s
        """, (stock_code, limit))
        return [{"date": str(r["news_date"]), "content": r["content"]} for r in cur.fetchall()]
    finally:
        conn.close()


def _build_weekly_prompt(stock_code: str, stock_name: str,
                         indicators: dict, patterns: list,
                         past_analyses: list[dict]) -> str:
    """建構週報 AI 分析 prompt（含歷史分析比較）。"""
    price = indicators.get("price", {})
    ma = indicators.get("ma", {})
    macd = indicators.get("macd", {})
    rsi = indicators.get("rsi")
    vol = indicators.get("volume", {})
    ma_arr = indicators.get("ma_arrangement", "")

    prompt = f"""# {stock_code} {stock_name} — 週度技術分析

## 最新技術指標（日線）
- 收盤：{price.get('close', 'N/A')} | 漲跌：{price.get('change', 'N/A')} ({price.get('change_pct', 'N/A')}%)
- MA5={ma.get('ma5', 'N/A')} | MA10={ma.get('ma10', 'N/A')} | MA20={ma.get('ma20', 'N/A')} | MA60={ma.get('ma60', 'N/A')}
- 均線排列：{ma_arr}
- MACD：DIF={macd.get('dif', 'N/A')} | MACD={macd.get('macd', 'N/A')} | 柱狀={macd.get('histogram', 'N/A')}
- RSI(14)：{rsi}
- 成交量比（vs 20日均量）：{vol.get('volume_ratio', 'N/A')}
"""

    if patterns:
        pattern_text = "、".join([f"{p['name']}({p['signal']})" for p in patterns[:5]])
        prompt += f"- K線型態：{pattern_text}\n"

    # 加入歷史分析
    if past_analyses:
        prompt += "\n## 過去 AI 分析紀錄（供比較）\n"
        for pa in past_analyses[:3]:
            # 取摘要（核心結論部分）
            content = pa["content"]
            # 嘗試擷取核心結論
            conclusion = ""
            for marker in ["核心結論", "**核心結論**", "綜合判斷", "操作建議"]:
                idx = content.find(marker)
                if idx >= 0:
                    conclusion = content[idx:idx+300]
                    break
            if not conclusion:
                conclusion = content[:300]
            prompt += f"\n### [{pa['date']}] 分析摘要\n{conclusion}\n"

    prompt += f"""
## 請執行以下分析

1. **趨勢追蹤**：與前次分析對比，趨勢是否有變化？方向有無轉折？
2. **關鍵價位**：目前支撐位與壓力位
3. **指標變化**：MACD/RSI/量能與上次分析相比的變化
4. **操作建議**：基於趨勢延續或轉折，給出短線(1-5日)與中線(5-20日)方向
5. **風險提示**：需注意的風險因子

請用繁體中文，精簡回答（300字以內）。"""

    # Inject prediction history feedback if available
    try:
        from src.services.kline_analysis import _build_prediction_feedback
        feedback = _build_prediction_feedback(stock_code, "shared")
        if feedback:
            prompt += feedback
    except Exception:
        pass

    return prompt


def analyze_single_stock(stock_code: str, stock_name: str) -> str | None:
    """對單一股票執行 AI 分析（含歷史對比）。"""
    api_key = os.getenv("MINIMAX_API_KEY", "")
    if not api_key:
        return None

    # 1. Get K-line data
    US_INDICES = {"TWII", "DJI", "IXIC", "SOX"}
    if stock_code in US_INDICES:
        from src.services.us_index_service import get_us_index_kline
        kline_data = get_us_index_kline(stock_code, 120)
    else:
        kline_data = get_daily_kline(stock_code, 120)
    if not kline_data or len(kline_data) < 5:
        logger.warning(f"[{stock_code}] K線資料不足，跳過")
        return None

    # 2. Compute indicators
    indicators = compute_all_indicators(kline_data)
    if "error" in indicators:
        return None

    # 3. Detect patterns
    patterns = detect_patterns(kline_data)

    # 4. Get past analyses
    past_analyses = get_past_analyses(stock_code, limit=3)

    # 5. Build prompt
    prompt = _build_weekly_prompt(stock_code, stock_name, indicators, patterns, past_analyses)

    # 6. Call AI
    data = json.dumps({
        "model": "MiniMax-M2.7",
        "messages": [
            {"role": "system", "content": "你是資深台股技術分析師，負責每週自選股覆盤。根據技術指標與過去分析紀錄，提供精簡的趨勢追蹤報告。繁體中文回答。"},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": 1500,
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
            content = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL).strip()
            return content if content else None
    except Exception as e:
        logger.error(f"[{stock_code}] AI analysis failed: {e}")
        return None


def run_weekly_watchlist_analysis() -> dict:
    """
    執行每週自選股全面 AI 分析：
    1. 取得所有自選股
    2. 逐一分析（含歷史對比）
    3. 存入備註
    4. 彙整發送 Telegram

    Returns: {"total": int, "analyzed": int, "telegram_sent": bool}
    """
    result = {"total": 0, "analyzed": 0, "telegram_sent": False}

    # Get all watchlist stocks (deduplicated by code)
    all_stocks = get_all_watchlist_stocks()
    seen_codes = set()
    unique_stocks = []
    for s in all_stocks:
        if s["code"] not in seen_codes:
            seen_codes.add(s["code"])
            unique_stocks.append(s)

    # Always include major indices
    INDEX_LIST = [
        {"code": "TWII", "name": "台灣加權指數", "user_id": "shared"},
        {"code": "DJI", "name": "道瓊工業指數", "user_id": "shared"},
        {"code": "IXIC", "name": "那斯達克綜合指數", "user_id": "shared"},
        {"code": "SOX", "name": "費城半導體指數", "user_id": "shared"},
    ]
    for idx in INDEX_LIST:
        if idx["code"] not in seen_codes:
            seen_codes.add(idx["code"])
            unique_stocks.insert(0, idx)  # indices first

    result["total"] = len(unique_stocks)
    if not unique_stocks:
        logger.info("No watchlist stocks to analyze")
        return result

    logger.info(f"Weekly watchlist analysis: {len(unique_stocks)} stocks to analyze")

    # Analyze each stock
    analysis_results = []
    today_str = date.today().isoformat()

    for stock in unique_stocks:
        code = stock["code"]
        name = stock["name"]

        logger.info(f"Analyzing {code} {name}...")
        analysis = analyze_single_stock(code, name)

        if analysis:
            # Save to notes
            title = f"🤖 AI 技術分析 (週報)"
            content = f"【{code} {name} 週度技術分析】\n\n{analysis}"
            add_note(
                stock_code=code,
                content=content,
                user_id="shared",
                title=title,
                news_date=today_str,
            )
            analysis_results.append({"code": code, "name": name, "analysis": analysis})
            result["analyzed"] += 1

            # Save AI prediction if ai_feedback alert is enabled
            _try_save_weekly_prediction(code, stock["user_id"], analysis)

        # Rate limiting: wait between API calls
        time.sleep(2)

    logger.info(f"Weekly analysis done: {result['analyzed']}/{result['total']} stocks analyzed")

    # Send Telegram summary
    if analysis_results:
        bot_token = ""
        chat_id = ""
        try:
            from src.core.database import get_alert_settings
            _tg_settings = get_alert_settings("default")
            bot_token = _tg_settings.get("telegram_bot_token", "")
            chat_id = _tg_settings.get("telegram_chat_id", "")
        except Exception:
            bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
            chat_id = os.getenv("TELEGRAM_CHAT_ID", "")

        # Build summary message
        today_display = date.today().strftime("%Y/%m/%d")
        lines = [f"📊 <b>每週自選股 AI 分析</b> — {today_display}\n"]
        lines.append(f"共分析 {result['analyzed']} 檔自選股：\n")

        for item in analysis_results:
            # Extract first line or conclusion as summary
            analysis_text = item["analysis"]
            summary = ""
            for marker in ["核心結論", "**核心結論**", "趨勢追蹤"]:
                idx = analysis_text.find(marker)
                if idx >= 0:
                    end = analysis_text.find("\n", idx + len(marker) + 3)
                    if end > idx:
                        summary = analysis_text[idx:end].strip()
                    break
            if not summary:
                # Take first meaningful line
                for line in analysis_text.split("\n"):
                    line = line.strip()
                    if len(line) > 10 and not line.startswith("#"):
                        summary = line[:100]
                        break

            lines.append(f"<b>{item['code']} {item['name']}</b>\n{summary}\n")

        message = "\n".join(lines)
        # Telegram has 4096 char limit
        if len(message) > 4000:
            message = message[:3950] + "\n\n... (詳細分析請查看 WEB 備註)"

        if send_telegram_message(bot_token, chat_id, message):
            result["telegram_sent"] = True

    return result


def _try_save_weekly_prediction(stock_code: str, user_id: str, analysis: str):
    """Check if ai_feedback alert is enabled for this stock, and save prediction snapshot."""
    try:
        from src.core.database import has_ai_feedback_alert, save_ai_prediction
        if not has_ai_feedback_alert(stock_code, user_id):
            return

        from src.services.kline_analysis import _extract_direction, _extract_price

        direction = _extract_direction(analysis)
        target_price = _extract_price(analysis, ["目標", "壓力", "上看"])
        stop_loss = _extract_price(analysis, ["停損", "支撐", "停利"])
        key_reasoning = analysis[:300]

        # Get current price
        US_INDICES = {"TWII", "DJI", "IXIC", "SOX"}
        if stock_code in US_INDICES:
            from src.services.us_index_service import get_us_index_kline
            kline_data = get_us_index_kline(stock_code, 5)
        else:
            kline_data = get_daily_kline(stock_code, 5)
        price = float(kline_data[-1]["close"]) if kline_data else None

        save_ai_prediction(
            stock_code=stock_code,
            user_id=user_id,
            prediction_date=date.today().isoformat(),
            price_at_prediction=price,
            direction=direction,
            target_price=target_price,
            stop_loss=stop_loss,
            key_reasoning=key_reasoning,
            source="weekly_report",
        )
        logger.info(f"Weekly prediction saved: {stock_code} direction={direction}")
    except Exception as e:
        logger.error(f"Failed to save weekly prediction for {stock_code}: {e}")
