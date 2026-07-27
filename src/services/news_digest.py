"""News Weekly Digest Service — Generate weekly news summary using MiniMax AI."""

import os
import json
import re
import urllib.request
import logging
from datetime import date, timedelta

logger = logging.getLogger(__name__)

MINIMAX_API_URL = "https://api.minimax.io/v1/chat/completions"


def generate_weekly_digest(target_date: date = None) -> dict | None:
    """
    Generate a weekly news digest for the week containing target_date.
    target_date defaults to today (should be called on Sunday).
    Returns {"id": int, "title": str} on success, None on failure.
    """
    from src.core.database import get_week_news_notes, save_news_digest

    if target_date is None:
        target_date = date.today()

    # Calculate week range (Mon-Sun)
    weekday = target_date.weekday()  # 0=Mon, 6=Sun
    week_start = target_date - timedelta(days=weekday)
    week_end = week_start + timedelta(days=6)

    # Fetch this week's news notes
    notes = get_week_news_notes(week_start.isoformat(), week_end.isoformat())

    if not notes:
        logger.info(f"No news notes found for week {week_start} ~ {week_end}")
        return None

    # Build context from notes
    context_parts = []
    for n in notes:
        content = (n.get("content") or "").strip()
        if content and content != "(圖片)" and content != "⏳ 分析中...":
            news_date = n.get("news_date", "")
            context_parts.append(f"[{news_date}] {content[:500]}")

    if not context_parts:
        logger.info(f"No text content in news notes for week {week_start} ~ {week_end}")
        return None

    context_text = "\n\n---\n".join(context_parts[:30])  # Limit to 30 notes

    # Call MiniMax AI to generate digest
    api_key = os.getenv("MINIMAX_API_KEY", "")
    if not api_key:
        logger.error("MINIMAX_API_KEY not set, cannot generate digest")
        return None

    title = f"{week_start.strftime('%Y/%m/%d')} ~ {week_end.strftime('%m/%d')} 本週重點新聞"

    prompt = f"""# 角色
你是一位資深台股新聞編輯，負責整理本週重點新聞摘要。

# 任務
根據下方【本週新聞備註】，產出一份結構清晰的「本週重點新聞 Markdown 摘要」。

# 格式要求
- 使用繁體中文
- 使用 Markdown 格式
- 先用 2-3 句話寫「本週總覽」
- 然後分類整理重點新聞（可依產業、主題分類）
- 每條新聞用條列式，包含日期和重點
- 標註相關個股代號（如有）
- 最後加上「本週關注焦點」小結

# 約束
- 只根據提供的新聞備註整理，不可編造
- 保持客觀中立
- 篇幅適中，重點突出

---
【本週新聞備註】（{week_start.strftime('%Y/%m/%d')} ~ {week_end.strftime('%m/%d')}）

{context_text}
"""

    data = json.dumps({
        "model": "MiniMax-M2.7",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 4000,
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
            # Remove <think>...</think> blocks if present
            content = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL).strip()

        if not content:
            logger.error("MiniMax returned empty content for weekly digest")
            return None

        # Save to database
        digest_id = save_news_digest(
            week_start.isoformat(),
            week_end.isoformat(),
            title,
            content
        )
        logger.info(f"Weekly digest saved: id={digest_id}, title={title}")
        return {"id": digest_id, "title": title}

    except Exception as e:
        logger.error(f"Failed to generate weekly digest: {e}")
        return None
