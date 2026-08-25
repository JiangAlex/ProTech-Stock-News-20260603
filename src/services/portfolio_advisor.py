"""Portfolio AI Analysis Service — 整體持股組合 AI 分析。

收集使用者全部持股資料（交易記錄 + 最新價格 + 技術指標 + 產業分布），
組裝 prompt 送 MiniMax LLM，產出投資組合層級的分析報告。
"""

import json
import logging
import os
import re
import urllib.request
from src.services.http_retry import retry_urlopen
from datetime import date

import psycopg2
from psycopg2.extras import RealDictCursor

from src.core.pg_client import DB_CONFIG
from src.core.database import get_analysis_preferences, get_all_trades

logger = logging.getLogger(__name__)

MINIMAX_API_URL = os.getenv("OPENROUTER_API_URL", "https://openrouter.ai/api/v1/chat/completions")

# Conversation history for portfolio analysis (in-memory, per user)
_portfolio_history: dict[str, list] = {}
MAX_PORTFOLIO_HISTORY = 5

DEFAULT_PORTFOLIO_FRAMEWORK = """1. **部位總覽**：{總成本/現值/未實現損益/報酬率}
2. **產業集中度**：{各產業占比，是否過度集中某一產業}
3. **個股強弱排序**：{依技術面排序，哪些轉強/轉弱}
4. **風險提示**：{超買/跌破均線/虧損過大/量能異常的個股}
5. **操作建議**：{加碼/減碼/停損/換股方向}"""


def gather_portfolio_data(user_id: str = "default") -> dict:
    """收集使用者全部持股資料。

    Returns:
        {
            "holdings": [...],
            "summary": {
                "total_cost", "total_value", "total_pnl", "total_pnl_pct",
                "industry_distribution", "risk_flags"
            }
        }
    """
    # 1. 取得交易記錄（持股）
    trades = get_all_trades(user_id)
    if not trades:
        return {"holdings": [], "summary": {}}

    # Group trades by stock_code → compute avg cost & total shares
    from collections import defaultdict
    stock_trades = defaultdict(list)
    for t in trades:
        stock_trades[t["stock_code"]].append(t)

    stock_codes = list(stock_trades.keys())
    if not stock_codes:
        return {"holdings": [], "summary": {}}

    # 2. 從 DB 取最新價格 + 技術指標 + 產業別
    conn = psycopg2.connect(**DB_CONFIG)
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)

        # 最新收盤價
        placeholders = ",".join(["%s"] * len(stock_codes))
        cur.execute(f"""
            SELECT DISTINCT ON (stock_code) stock_code, close, trade_date
            FROM daily_kline
            WHERE stock_code IN ({placeholders})
            ORDER BY stock_code, trade_date DESC
        """, stock_codes)
        price_map = {r["stock_code"]: {"close": float(r["close"]), "date": r["trade_date"].isoformat()} for r in cur.fetchall()}

        # 最新技術指標
        cur.execute(f"""
            SELECT DISTINCT ON (stock_code) stock_code, close, change_pct,
                   ma5, ma10, ma20, ma60, ma_arrangement,
                   rsi14, volume_ratio, volume_trend,
                   macd_dif, macd_signal, macd_histogram
            FROM daily_indicators
            WHERE stock_code IN ({placeholders})
            ORDER BY stock_code, date DESC
        """, stock_codes)
        indicator_map = {r["stock_code"]: dict(r) for r in cur.fetchall()}

        # 產業別 + 股票名稱
        cur.execute(f"""
            SELECT stock_code, stock_name, industry
            FROM stock_basic
            WHERE stock_code IN ({placeholders})
        """, stock_codes)
        info_map = {r["stock_code"]: {"name": r["stock_name"], "industry": r["industry"] or "未分類"} for r in cur.fetchall()}

    finally:
        conn.close()

    # 3. 組裝每支股票的持股數據
    holdings = []
    total_cost = 0
    total_value = 0
    industry_values = defaultdict(float)
    risk_flags = []

    for code in stock_codes:
        trade_list = stock_trades[code]
        total_shares = sum(t["buy_shares"] for t in trade_list)
        if total_shares <= 0:
            continue

        # 加權平均成本
        total_spent = sum(float(t["buy_price"]) * t["buy_shares"] for t in trade_list)
        avg_cost = total_spent / total_shares

        current_price = price_map.get(code, {}).get("close", 0)
        info = info_map.get(code, {"name": code, "industry": "未分類"})
        indicators = indicator_map.get(code, {})

        # 損益計算（以張為單位，1張=1000股）
        cost_value = avg_cost * total_shares * 1000
        market_value = current_price * total_shares * 1000
        pnl = market_value - cost_value
        pnl_pct = ((current_price - avg_cost) / avg_cost * 100) if avg_cost > 0 else 0

        total_cost += cost_value
        total_value += market_value
        industry_values[info["industry"]] += market_value

        holding = {
            "code": code,
            "name": info["name"],
            "industry": info["industry"],
            "shares": total_shares,
            "avg_cost": round(avg_cost, 2),
            "current_price": current_price,
            "pnl": round(pnl),
            "pnl_pct": round(pnl_pct, 1),
            "ma_arrangement": indicators.get("ma_arrangement", ""),
            "rsi14": indicators.get("rsi14"),
            "volume_ratio": indicators.get("volume_ratio"),
            "change_pct": indicators.get("change_pct"),
            "macd_histogram": indicators.get("macd_histogram"),
        }
        holdings.append(holding)

        # 風險偵測
        rsi = indicators.get("rsi14")
        if rsi and rsi > 80:
            risk_flags.append(f"{code} {info['name']} RSI={rsi:.0f} 超買")
        if rsi and rsi < 20:
            risk_flags.append(f"{code} {info['name']} RSI={rsi:.0f} 超賣")
        if indicators.get("ma_arrangement") == "空頭排列":
            risk_flags.append(f"{code} {info['name']} 均線空頭排列")
        if pnl_pct < -15:
            risk_flags.append(f"{code} {info['name']} 虧損 {pnl_pct:.1f}%")

    # 產業分布（百分比）
    industry_distribution = {}
    if total_value > 0:
        for ind, val in sorted(industry_values.items(), key=lambda x: -x[1]):
            pct = val / total_value * 100
            industry_distribution[ind] = round(pct, 1)
            if pct > 40:
                risk_flags.insert(0, f"{ind} 產業占比 {pct:.0f}%（偏高）")

    # 依損益排序
    holdings.sort(key=lambda h: h["pnl_pct"], reverse=True)

    total_pnl = total_value - total_cost
    total_pnl_pct = (total_pnl / total_cost * 100) if total_cost > 0 else 0

    return {
        "holdings": holdings,
        "summary": {
            "total_cost": round(total_cost),
            "total_value": round(total_value),
            "total_pnl": round(total_pnl),
            "total_pnl_pct": round(total_pnl_pct, 1),
            "industry_distribution": industry_distribution,
            "risk_flags": risk_flags,
            "stock_count": len(holdings),
            "date": price_map.get(stock_codes[0], {}).get("date", str(date.today())),
        }
    }


def _build_portfolio_prompt(portfolio: dict, user_id: str = "default") -> str:
    """組裝持股分析 prompt。"""
    prefs = get_analysis_preferences(user_id)
    holdings = portfolio["holdings"]
    summary = portfolio["summary"]

    # 使用者自訂框架 or 預設
    custom_framework = (prefs.get("portfolio_framework") or "").strip()
    framework_text = custom_framework if custom_framework else DEFAULT_PORTFOLIO_FRAMEWORK

    # Strip comments (# 後面的內容)
    framework_lines = []
    for line in framework_text.split('\n'):
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

    # 持股明細表
    holdings_table = "| 股票 | 產業 | 張數 | 成本 | 現價 | 損益% | MA排列 | RSI | 量比 | MACD柱 |\n"
    holdings_table += "|------|------|------|------|------|-------|--------|-----|------|--------|\n"
    for h in holdings:
        rsi_str = f"{h['rsi14']:.0f}" if h['rsi14'] else "-"
        vr_str = f"{h['volume_ratio']:.1f}" if h['volume_ratio'] else "-"
        macd_str = f"{h['macd_histogram']:.2f}" if h['macd_histogram'] else "-"
        pnl_sign = "+" if h['pnl_pct'] >= 0 else ""
        holdings_table += (
            f"| {h['code']} {h['name']} | {h['industry']} | {h['shares']} | "
            f"{h['avg_cost']} | {h['current_price']} | {pnl_sign}{h['pnl_pct']}% | "
            f"{h['ma_arrangement']} | {rsi_str} | {vr_str} | {macd_str} |\n"
        )

    # 產業分布
    ind_text = "、".join([f"{k} {v}%" for k, v in summary.get("industry_distribution", {}).items()])

    # 風險提示
    risk_text = "\n".join([f"- {f}" for f in summary.get("risk_flags", [])]) or "- 無明顯風險"

    # 使用者偏好
    pref_parts = []
    if prefs.get("trading_style"):
        pref_parts.append(f"操作風格：{prefs['trading_style']}")
    if prefs.get("risk_tolerance"):
        pref_parts.append(f"風險偏好：{prefs['risk_tolerance']}")
    if prefs.get("custom_prompt"):
        pref_parts.append(f"其他要求：{prefs['custom_prompt']}")
    pref_text = "\n".join(pref_parts) if pref_parts else "（未設定）"

    total_pnl_sign = "+" if summary.get("total_pnl", 0) >= 0 else ""

    prompt = f"""# 角色
你是一位擁有 CFA 資格的「投資組合分析師」，專精台股投資組合管理。

# 任務
根據使用者的持股部位、技術面數據、產業分布，產出專業的投資組合分析報告。

# 分析框架
{framework_for_ai}

# 格式要求
- 繁體中文，條列式
- 先用 1-2 句給出核心結論
- 嚴格依照上方「分析框架」的段落結構輸出
- 具體到個股，附數據佐證
- 結尾加上「⚠️ 以上為 AI 分析，僅供研究參考，不構成投資建議。」

---

【部位總覽】
- 持股檔數：{summary.get('stock_count', 0)}
- 總成本：{summary.get('total_cost', 0):,.0f} 元
- 現值：{summary.get('total_value', 0):,.0f} 元
- 未實現損益：{total_pnl_sign}{summary.get('total_pnl', 0):,.0f} 元（{total_pnl_sign}{summary.get('total_pnl_pct', 0)}%）
- 資料日期：{summary.get('date', '')}

【產業分布】
{ind_text}

【持股明細】
{holdings_table}

【風險偵測】
{risk_text}

【使用者偏好】
{pref_text}
"""
    return prompt


def analyze_portfolio(user_id: str = "default") -> dict:
    """執行整體持股 AI 分析。

    Returns:
        {
            "portfolio": dict,  # 持股數據
            "analysis": str,    # AI 分析文字
            "error": str | None
        }
    """
    # Step 1: 收集資料
    portfolio = gather_portfolio_data(user_id)
    if not portfolio["holdings"]:
        return {
            "portfolio": portfolio,
            "analysis": "⚠️ 目前沒有持股記錄（交易記錄為空）。請先在自選股中新增買進記錄。",
            "error": None
        }

    # Step 2: 檢查 API key
    api_key = os.getenv("OPENROUTER_API_KEY", os.getenv("MINIMAX_API_KEY", ""))
    if not api_key:
        return {
            "portfolio": portfolio,
            "analysis": "⚠️ AI 未設定（缺少 MINIMAX_API_KEY），僅顯示持股數據。",
            "error": None
        }

    # Step 3: 組裝 prompt
    prompt = _build_portfolio_prompt(portfolio, user_id)

    # Step 4: 加入歷史對話
    history = _portfolio_history.get(user_id, [])
    messages = [{"role": "system", "content": "你是一位專業台股投資組合分析師，根據持股數據和技術指標提供組合層級的分析建議。繁體中文回答。"}]
    messages.extend(history)
    messages.append({"role": "user", "content": prompt})

    # Step 5: 呼叫 LLM
    data = json.dumps({
        "model": "MiniMax-M2.7",
        "messages": messages,
        "max_tokens": 3000,
        "temperature": 0.3,
    }).encode()

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    try:
        req = urllib.request.Request(MINIMAX_API_URL, data=data, method="POST", headers=headers)
        result = json.loads(retry_urlopen(req, timeout=90, max_retries=2))
        content = result["choices"][0]["message"]["content"].strip()
        # Remove <think>...</think> blocks
        analysis = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL).strip()
        if not analysis:
            analysis = "⚠️ AI 回應為空，請重試。"
        else:
            # Save to history
            _save_portfolio_history(user_id, analysis)
    except Exception as e:
        logger.error(f"Portfolio AI analysis failed: {e}")
        analysis = f"⚠️ AI 持股分析失敗：{e}"

    return {
        "portfolio": portfolio,
        "analysis": analysis,
        "error": None
    }


def _save_portfolio_history(user_id: str, analysis: str):
    """Save portfolio analysis to conversation history."""
    if user_id not in _portfolio_history:
        _portfolio_history[user_id] = []

    summary = f"[持股組合分析] {analysis[:600]}"
    _portfolio_history[user_id].append({"role": "assistant", "content": summary})

    while len(_portfolio_history[user_id]) > MAX_PORTFOLIO_HISTORY * 2:
        _portfolio_history[user_id].pop(0)
