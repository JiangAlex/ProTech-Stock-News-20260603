"""Semantic expansion service — use MiniMax M2.7 to expand search keywords."""

import os
import json
import urllib.request
import logging

logger = logging.getLogger(__name__)

MINIMAX_API_URL = "https://api.minimax.io/v1/chat/completions"


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
