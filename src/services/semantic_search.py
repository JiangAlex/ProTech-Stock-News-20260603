"""Semantic expansion service — use MiniMax M2.7 to expand search keywords."""

import os
import json
import urllib.request
from src.services.http_retry import retry_urlopen
import logging

logger = logging.getLogger(__name__)

MINIMAX_API_URL = "https://api.minimax.io/v1/chat/completions"

# Conversation history per user (in-memory)
_conversation_history: dict[str, list] = {}
MAX_HISTORY = 5


def _build_messages(user_id: str, current_prompt: str) -> list[dict]:
    """Build messages array with conversation history."""
    history = _conversation_history.get(user_id, [])
    messages = [{"role": "system", "content": "你是一位台股新聞分析助理，根據新聞備註資料回答問題。繁體中文回答。"}]
    messages.extend(history)
    messages.append({"role": "user", "content": current_prompt})
    return messages


def _save_history(user_id: str, question: str, answer: str):
    """Save Q&A to conversation history, keep last N rounds."""
    if user_id not in _conversation_history:
        _conversation_history[user_id] = []
    _conversation_history[user_id].append({"role": "user", "content": question})
    _conversation_history[user_id].append({"role": "assistant", "content": answer})
    # Keep only last MAX_HISTORY rounds
    while len(_conversation_history[user_id]) > MAX_HISTORY * 2:
        _conversation_history[user_id].pop(0)


def expand_keywords(query: str) -> list[str]:
    """
    Use MiniMax M2.7 to expand a search query into related keywords/synonyms.
    Returns a list of keywords (including the original query).
    Falls back to just the original query if API fails.
    """
    api_key = os.getenv("MINIMAX_API_KEY", "")
    if not api_key:
        return [query]

    prompt = f"""你是一個台股新聞搜尋關鍵字擴展助手。用戶的搜尋詞會用 ILIKE 去比對資料庫中的新聞備註內容。

規則：
1. 先將搜尋詞拆分成每個獨立的詞（例如「copos 概念股」→「CoPoS」「概念股」）
2. 保留每個詞的各種大小寫寫法（例如 copos → CoPoS, COPOS, Copos）
3. 再產生 3-5 個台股相關的同義詞或相關詞（繁體中文優先）
4. 每個關鍵詞必須是「單一詞彙」，不可包含空格
5. 只回傳關鍵詞列表，用逗號分隔，不要其他解釋

搜尋詞：{query}

關鍵詞："""

    data = json.dumps({
        "model": "MiniMax-M2.7",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 1000,
        "temperature": 0.3,
    }).encode()

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    try:
        req = urllib.request.Request(MINIMAX_API_URL, data=data, method="POST", headers=headers)
        result = json.loads(retry_urlopen(req, timeout=30, max_retries=2))
        content = result["choices"][0]["message"]["content"].strip()
        # Remove <think>...</think> blocks if present
        import re
        content = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL).strip()
        # Parse comma-separated keywords
        keywords = [k.strip() for k in content.replace("、", ",").replace("，", ",").split(",") if k.strip()]
        # Always include original query
        if query not in keywords:
            keywords.insert(0, query)
        logger.info(f"Expanded '{query}' -> {keywords}")
        return keywords
    except Exception as e:
        logger.error(f"Keyword expansion failed: {e}")
        return [query]


def search_notes(query: str, user_id: str = "default", use_ai: bool = True) -> list[dict]:
    """
    Search news notes using AI-expanded keywords + PostgreSQL ILIKE.
    If use_ai=True, expand keywords first via MiniMax.
    Falls back to simple ILIKE if AI is unavailable.
    """
    import psycopg2
    from psycopg2.extras import RealDictCursor
    from src.core.pg_client import DB_CONFIG

    # Step 1: Expand keywords (AI or just original)
    if use_ai:
        keywords = expand_keywords(query)
    else:
        keywords = [query]

    # Step 1.5: Programmatic token split as fallback — ensure individual words are searched
    import re as _re
    tokens = [t.strip() for t in _re.split(r'[\s,，、/]+', query) if t.strip() and len(t.strip()) >= 2]
    for token in tokens:
        # Add token and common case variants
        variants = {token, token.upper(), token.lower(), token.capitalize()}
        for v in variants:
            if v not in keywords:
                keywords.append(v)

    # Deduplicate while preserving order
    seen = set()
    deduped = []
    for kw in keywords:
        kw_lower = kw.lower()
        if kw_lower not in seen:
            seen.add(kw_lower)
            deduped.append(kw)
    keywords = deduped
    logger.info(f"Final search keywords for '{query}': {keywords}")

    # Step 2: Query DB with ILIKE for each keyword
    conn = psycopg2.connect(**DB_CONFIG)
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        # Build OR conditions for all keywords
        conditions = " OR ".join(["content ILIKE %s"] * len(keywords))
        params = [f"%{kw}%" for kw in keywords]
        sql = f"""
            SELECT id, stock_code, content, news_date, created_at,
                   (image_data IS NOT NULL) AS has_image
            FROM watchlist_notes
            WHERE stock_code = 'NEWS' AND user_id = %s
              AND ({conditions})
            ORDER BY news_date DESC NULLS LAST, created_at DESC
        """
        cur.execute(sql, [user_id] + params)
        results = [dict(r) for r in cur.fetchall()]
        return {"keywords": keywords, "results": results}
    finally:
        conn.close()


def ask_news(query: str, user_id: str = "default") -> dict:
    """
    AI Agent: search news notes + generate summary answer using MiniMax M2.7.
    Returns {"answer": str, "keywords": list, "sources": list}
    """
    import re

    # Step 0: Detect concept stock query (e.g. "AI概念股", "電動車 概念股")
    concept_match = re.match(r'^(.+?)\s*概念股[有哪些是什麼？?]*$', query.strip())
    if concept_match:
        concept_answer = _answer_from_concepts(concept_match.group(1).strip(), user_id)
        if concept_answer:
            return concept_answer

    # Step 1: Detect if query is a general/time-range question
    general_patterns = r'(最近|近\d|全部|所有|都有|有哪些|總結|摘要|整理|報導什麼|說什麼|講什麼|有什麼|一[周週]|本[周週]|這[周週]|上[周週]|重點|概況|概述|回顧|today|this week)'
    is_general = bool(re.search(general_patterns, query))

    # Extract time range (days) from query
    days_match = re.search(r'近(\d+)[日天]', query)
    week_match = re.search(r'(一|本|這|上)[周週]', query)
    if days_match:
        recent_days = int(days_match.group(1))
    elif week_match:
        recent_days = 7
    elif is_general:
        recent_days = 7
    else:
        recent_days = 0

    if is_general:
        # General query: fetch recent notes directly without keyword filtering
        results = _fetch_recent_notes(user_id, days=recent_days)
        keywords = [query]
    else:
        # Specific query: use keyword expansion + ILIKE
        search_result = search_notes(query, user_id, use_ai=True)
        keywords = search_result["keywords"]
        results = search_result["results"]

    if not results:
        # Check if this is a technical/market scan question
        tech_patterns = r'(RSI|rsi|均線|多頭|空頭|超買|超賣|量比|放量|縮量|型態|突破|支撐|壓力|哪些股票|掃描|篩選|排列|MACD|macd|布林)'
        if re.search(tech_patterns, query):
            # Try to answer from daily_indicators
            market_answer = _answer_from_indicators(query, user_id)
            if market_answer:
                return market_answer

        # Fallback: use LLM general knowledge to answer
        general_answer = _answer_general(query, user_id)
        if general_answer:
            return general_answer

        return {"answer": "❌ 找不到相關新聞備註。", "keywords": keywords, "sources": []}

    # Step 2: Build context from search results (top 15)
    top_results = results[:15]
    context_text = "\n\n---\n".join([
        f"[{r['news_date']}] {r['content'][:200]}" for r in top_results if r.get('content')
    ])

    if not context_text.strip():
        return {"answer": "❌ 搜尋到圖片但無文字內容可供分析。", "keywords": keywords, "sources": top_results}

    # Step 3: Call MiniMax M2.7 to generate answer
    api_key = os.getenv("MINIMAX_API_KEY", "")
    if not api_key:
        return {"answer": "⚠️ AI 未設定（缺少 MINIMAX_API_KEY）", "keywords": keywords, "sources": top_results}

    prompt = f"""# 角色
你是一位擁有 20 年經驗的「資深機構買方股票分析師」，同時也是台股新聞分析助理。
請根據下方提供的【新聞備註資料】，針對用戶問題產出專業、客觀、數據驅動的分析回答。

# 重要約束
- 只能根據【新聞備註資料】中的事實進行分析，不可編造資料中沒有的數據
- 若資料不足以支撐某個分析面向，直接跳過該段落，不要硬寫「資料不足」
- 繁體中文回答，使用條列式呈現，保持簡潔易讀
- 保持專業理性客觀的語氣

# 彈性分析框架（根據資料豐富程度選擇展開）
- 若資料包含個股/產業資訊 → 簡述商業模式或產業定位
- 若資料包含財務數據（營收、EPS、毛利率等）→ 列出關鍵數據並評估
- 若資料包含未來趨勢或催化劑 → 說明成長論點
- 若資料包含風險因子 → 列出關鍵風險
- 最後給出綜合觀點或操作方向參考

# 格式要求
- 先用 1-2 句總結核心結論
- 再條列重點分析
- 結尾加上：「⚠️ 以上僅供研究參考，不構成投資建議。」

---
【新聞備註資料】
{context_text}

---
【用戶問題】
{query}"""

    data = json.dumps({
        "model": "MiniMax-M2.7",
        "messages": _build_messages(user_id, prompt),
        "max_tokens": 2000,
        "temperature": 0.3,
    }).encode()

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    try:
        req = urllib.request.Request(MINIMAX_API_URL, data=data, method="POST", headers=headers)
        result = json.loads(retry_urlopen(req, timeout=60, max_retries=2))
        content = result["choices"][0]["message"]["content"].strip()
        # Remove <think>...</think> blocks
        import re as re2
        answer = re2.sub(r'<think>.*?</think>', '', content, flags=re2.DOTALL).strip()
        if not answer:
            answer = "⚠️ AI 回應為空，請重試。"
        else:
            # Save to conversation history
            _save_history(user_id, query, answer)
    except Exception as e:
        logger.error(f"AI QA failed: {e}")
        answer = f"⚠️ AI 回答失敗：{e}"

    sources = [{"id": r["id"], "news_date": r.get("news_date"), "content": (r.get("content") or "")[:80], "has_image": r.get("has_image", False)} for r in top_results]
    return {"answer": answer, "keywords": keywords, "sources": sources}


def _fetch_recent_notes(user_id: str, days: int = 7) -> list[dict]:
    """Fetch recent news notes without keyword filtering (for general questions)."""
    import psycopg2
    from psycopg2.extras import RealDictCursor
    from src.core.pg_client import DB_CONFIG

    conn = psycopg2.connect(**DB_CONFIG)
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        sql = """
            SELECT id, stock_code, content, news_date, created_at,
                   (image_data IS NOT NULL) AS has_image
            FROM watchlist_notes
            WHERE stock_code = 'NEWS' AND user_id = %s
              AND (news_date >= CURRENT_DATE - %s OR created_at >= CURRENT_DATE - %s)
            ORDER BY news_date DESC NULLS LAST, created_at DESC
            LIMIT 20
        """
        cur.execute(sql, [user_id, days, days])
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def _answer_from_concepts(concept_query: str, user_id: str = "default") -> dict | None:
    """回答概念股查詢，從 stock_concepts 表查詢並結合技術指標。"""
    import psycopg2
    from psycopg2.extras import RealDictCursor
    from src.core.pg_client import DB_CONFIG

    try:
        conn = psycopg2.connect(**DB_CONFIG)
        cur = conn.cursor(cursor_factory=RealDictCursor)

        # 模糊搜尋概念名稱
        cur.execute("""
            SELECT DISTINCT concept_name FROM stock_concepts
            WHERE concept_name ILIKE %s
            ORDER BY concept_name
        """, (f"%{concept_query}%",))
        matched_concepts = [r["concept_name"] for r in cur.fetchall()]

        if not matched_concepts:
            conn.close()
            return None

        # 取得成分股 + 最新技術指標
        cur.execute("""
            SELECT sc.concept_name, sc.stock_code, sc.stock_name,
                   di.close, di.change_pct, di.volume, di.ma_arrangement,
                   di.rsi14, di.volume_ratio
            FROM stock_concepts sc
            LEFT JOIN daily_indicators di ON di.stock_code = sc.stock_code
                AND di.date = (SELECT MAX(date) FROM daily_indicators)
            WHERE sc.concept_name = ANY(%s)
            ORDER BY sc.concept_name, di.volume DESC NULLS LAST
        """, (matched_concepts,))
        rows = cur.fetchall()
        conn.close()

        if not rows:
            return None

        # 格式化回答
        lines = []
        current_concept = None
        for r in rows:
            if r["concept_name"] != current_concept:
                current_concept = r["concept_name"]
                lines.append(f"\n🔥 **{current_concept}** 概念股：")
                lines.append("代號 | 名稱 | 收盤 | 漲跌% | 均線 | RSI | 量比")
                lines.append("---|---|---|---|---|---|---")

            price = f"{r['close']:.1f}" if r.get('close') else "-"
            change = f"{r['change_pct']:+.2f}%" if r.get('change_pct') else "-"
            ma = r.get('ma_arrangement') or "-"
            rsi = f"{r['rsi14']:.0f}" if r.get('rsi14') else "-"
            vol_ratio = f"{r['volume_ratio']:.1f}x" if r.get('volume_ratio') else "-"

            lines.append(f"{r['stock_code']} | {r['stock_name']} | {price} | {change} | {ma} | {rsi} | {vol_ratio}")

        answer = "\n".join(lines)
        answer += "\n\n⚠️ 以上僅供研究參考，不構成投資建議。"

        _save_history(user_id, f"{concept_query}概念股", answer)

        return {
            "answer": answer,
            "keywords": [concept_query, "概念股"] + matched_concepts,
            "sources": [],
        }
    except Exception as e:
        logger.error(f"Concept stock query failed: {e}")
        return None


def _answer_from_indicators(query: str, user_id: str = "default") -> dict | None:
    """Answer technical/market scan questions using daily_indicators table."""
    import re
    import psycopg2
    from psycopg2.extras import RealDictCursor
    from src.core.pg_client import DB_CONFIG

    # Build conditions from query
    conditions = []
    params = []

    # RSI conditions
    if re.search(r'RSI\s*[<＜]\s*30|超賣|rsi.*30', query, re.IGNORECASE):
        conditions.append("di.rsi14 < 30")
    elif re.search(r'RSI\s*[>＞]\s*70|超買|rsi.*70', query, re.IGNORECASE):
        conditions.append("di.rsi14 > 70")
    elif re.search(r'RSI\s*[<＜]\s*50|rsi.*50', query, re.IGNORECASE):
        conditions.append("di.rsi14 < 50")

    # MA arrangement
    if re.search(r'多頭排列', query):
        conditions.append("di.ma_arrangement = '多頭排列'")
    elif re.search(r'空頭排列', query):
        conditions.append("di.ma_arrangement = '空頭排列'")

    # Volume
    if re.search(r'放量|爆量', query):
        conditions.append("di.volume_trend = '放量'")
    elif re.search(r'縮量', query):
        conditions.append("di.volume_trend = '縮量'")

    # If no specific conditions detected, try a general scan
    if not conditions:
        conditions.append("1=1")

    conn = psycopg2.connect(**DB_CONFIG)
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT MAX(date) FROM daily_indicators")
        latest = cur.fetchone()["max"]
        if not latest:
            return None

        where = " AND ".join(["di.date = %s"] + conditions)
        params = [latest]

        sql = f"""
            SELECT di.stock_code, sb.stock_name, sb.industry,
                   di.close, di.change_pct, di.ma_arrangement,
                   di.rsi14, di.volume_ratio, di.volume_trend
            FROM daily_indicators di
            JOIN stock_basic sb ON sb.stock_code = di.stock_code
            WHERE {where}
            ORDER BY di.volume DESC
            LIMIT 20
        """
        cur.execute(sql, params)
        results = [dict(r) for r in cur.fetchall()]

        if not results:
            return None

        # Build answer text
        total_sql = f"SELECT COUNT(*) FROM daily_indicators di JOIN stock_basic sb ON sb.stock_code = di.stock_code WHERE {where}"
        cur.execute(total_sql, params)
        total = cur.fetchone()["count"]

        lines = [f"📡 全市場掃描結果（{latest}）— 共 {total} 檔符合條件：\n"]
        for r in results:
            chg = f"+{r['change_pct']}" if (r['change_pct'] or 0) >= 0 else str(r['change_pct'])
            lines.append(f"• {r['stock_code']} {r['stock_name']}（{r['industry'] or ''}）— {r['close']} ({chg}%) RSI={r['rsi14']:.1f} {r['ma_arrangement']} 量比={r['volume_ratio']}")

        if total > 20:
            lines.append(f"\n...還有 {total - 20} 檔，請使用「📡 掃描」功能查看完整列表。")

        answer = "\n".join(lines)
        return {"answer": answer, "keywords": [query], "sources": []}
    except Exception as e:
        logger.error(f"_answer_from_indicators failed: {e}")
        return None
    finally:
        conn.close()


def _answer_general(query: str, user_id: str = "default") -> dict | None:
    """Fallback: use LLM general knowledge to answer stock/finance related questions."""
    api_key = os.getenv("MINIMAX_API_KEY", "")
    if not api_key:
        return None

    prompt = f"""# 角色
你是一位擁有 20 年經驗的「資深台股分析師暨投資顧問」。

# 任務
用戶提出了一個問題，請根據你的專業知識回答。

# 規則
- 繁體中文回答
- 簡潔條列式，重點明確
- 若為投資相關問題，結尾加上「⚠️ 以上僅供研究參考，不構成投資建議。」
- 若問題與股市/金融/投資完全無關，請簡短回答並提示用戶本系統主要用途為台股分析

# 用戶問題
{query}"""

    data = json.dumps({
        "model": "MiniMax-M2.7",
        "messages": _build_messages(user_id, prompt),
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
            import re
            answer = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL).strip()
            if not answer:
                return None
            _save_history(user_id, query, answer)
            return {"answer": answer, "keywords": [query], "sources": []}
    except Exception as e:
        logger.error(f"_answer_general failed: {e}")
        return None
