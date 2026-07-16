"""Semantic expansion service — use MiniMax M2.7 to expand search keywords."""

import os
import json
import urllib.request
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

    prompt = f"""你是一個搜尋關鍵字擴展助手。用戶要搜尋股市新聞備註。
請根據輸入的搜尋詞，產生 5-8 個相關的同義詞、相關詞、相關公司名、相關概念。
只回傳關鍵詞列表，用逗號分隔，不要其他解釋。

搜尋詞：{query}

相關關鍵詞："""

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
        with urllib.request.urlopen(req, timeout=30) as r:
            result = json.loads(r.read())
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

    prompt = f"""你是一位台股新聞分析助理。請根據以下新聞備註資料，回答用戶的問題。
若資料不足以回答，請據實說明。

【新聞備註資料】
{context_text}

【用戶問題】
{query}

要求：繁體中文回答，條列重點，最後一句總結。"""

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
        with urllib.request.urlopen(req, timeout=60) as r:
            result = json.loads(r.read())
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
