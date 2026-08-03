"""Finance News Scraper — 鉅亨網 (Anue) / Yahoo奇摩股市 / CMoney(替代:鉅亨產業分析)

每日 PM 5:00 抓取即時新聞 & 產業分析 熱門話題 10 條，
透過 Telegram 發送，並記錄至 AI 分析（MiniMax）。
"""

import json
import logging
import os
import re
import urllib.request
import urllib.parse
from datetime import datetime, date

logger = logging.getLogger(__name__)

# --- Anue (鉅亨網) API ---
ANUE_API_BASE = "https://api.cnyes.com/media/api/v1/newslist/category"
ANUE_NEWS_URL = "https://news.cnyes.com/news/id"

# --- MiniMax AI ---
MINIMAX_API_URL = "https://api.minimax.io/v1/chat/completions"


def _fetch_url(url: str, timeout: int = 15) -> str | None:
    """Fetch URL content with error handling."""
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"
        })
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        logger.error(f"Fetch failed [{url}]: {e}")
        return None


def fetch_anue_headline(limit: int = 10) -> list[dict]:
    """
    抓取鉅亨網即時頭條新聞。
    Returns: [{"title": str, "url": str, "source": "鉅亨網", "category": "即時新聞"}]
    """
    url = f"{ANUE_API_BASE}/headline?limit={limit}"
    data = _fetch_url(url)
    if not data:
        return []

    try:
        result = json.loads(data)
        items = result.get("items", {}).get("data", [])
        news = []
        for item in items[:limit]:
            news_id = item.get("newsId", "")
            title = item.get("title", "").strip()
            if title:
                news.append({
                    "title": title,
                    "url": f"{ANUE_NEWS_URL}/{news_id}",
                    "source": "鉅亨網",
                    "category": "即時新聞",
                })
        return news
    except (json.JSONDecodeError, KeyError) as e:
        logger.error(f"Anue headline parse error: {e}")
        return []


def fetch_anue_industry(limit: int = 10) -> list[dict]:
    """
    抓取鉅亨網台股產業新聞 (產業分析)。
    Returns: [{"title": str, "url": str, "source": "鉅亨網", "category": "產業分析"}]
    """
    url = f"{ANUE_API_BASE}/tw_stock?limit={limit}"
    data = _fetch_url(url)
    if not data:
        return []

    try:
        result = json.loads(data)
        items = result.get("items", {}).get("data", [])
        news = []
        for item in items[:limit]:
            news_id = item.get("newsId", "")
            title = item.get("title", "").strip()
            if title:
                news.append({
                    "title": title,
                    "url": f"{ANUE_NEWS_URL}/{news_id}",
                    "source": "鉅亨網",
                    "category": "產業分析",
                })
        return news
    except (json.JSONDecodeError, KeyError) as e:
        logger.error(f"Anue industry parse error: {e}")
        return []


def fetch_yahoo_tw_news(limit: int = 10) -> list[dict]:
    """
    抓取 Yahoo奇摩股市 即時新聞。
    Returns: [{"title": str, "url": str, "source": "Yahoo奇摩股市", "category": "即時新聞"}]
    """
    from bs4 import BeautifulSoup

    url = "https://tw.stock.yahoo.com/news"
    html = _fetch_url(url)
    if not html:
        return []

    try:
        soup = BeautifulSoup(html, "lxml")
        links = soup.select('a')
        seen = set()
        news = []

        for link in links:
            if len(news) >= limit:
                break
            href = link.get("href", "")
            title = link.text.strip()

            # Filter: must be a news article link with meaningful title
            if not ((".html" in href or "/news/" in href) and
                    len(title) > 10 and title not in seen):
                continue
            # Skip navigation items
            if any(skip in title for skip in ["登入", "首頁", "新聞", "Yahoo"]):
                continue

            seen.add(title)
            # Normalize URL
            if href.startswith("/"):
                href = f"https://tw.stock.yahoo.com{href}"
            news.append({
                "title": title[:80],
                "url": href,
                "source": "Yahoo奇摩股市",
                "category": "即時新聞",
            })

        return news
    except Exception as e:
        logger.error(f"Yahoo TW news parse error: {e}")
        return []


def fetch_cmoney_news(limit: int = 10) -> list[dict]:
    """
    抓取 CMoney 股市爆料同學會熱門討論 (公開頁面)。
    NOTE: CMoney 需登入才能取得完整內容，此處改用鉅亨網台股即時新聞作替代。
    Returns: [{"title": str, "url": str, "source": "CMoney", "category": "產業分析"}]
    """
    # CMoney 需登入，使用鉅亨網 tw_stock_news 作為替代來源
    url = f"{ANUE_API_BASE}/tw_stock_news?limit={limit}"
    data = _fetch_url(url)
    if not data:
        return []

    try:
        result = json.loads(data)
        items = result.get("items", {}).get("data", [])
        news = []
        for item in items[:limit]:
            news_id = item.get("newsId", "")
            title = item.get("title", "").strip()
            if title:
                news.append({
                    "title": title,
                    "url": f"{ANUE_NEWS_URL}/{news_id}",
                    "source": "CMoney(鉅亨)",
                    "category": "產業分析",
                })
        return news
    except (json.JSONDecodeError, KeyError) as e:
        logger.error(f"CMoney/Anue news parse error: {e}")
        return []


def collect_finance_news(max_items: int = 10) -> list[dict]:
    """
    整合三大來源，去重後取最熱門 10 條話題。
    優先順序：鉅亨即時 > Yahoo奇摩 > 鉅亨產業 > CMoney替代
    """
    all_news = []
    seen_titles = set()

    # 1. 鉅亨網即時頭條
    for item in fetch_anue_headline(limit=5):
        key = item["title"][:20]
        if key not in seen_titles:
            seen_titles.add(key)
            all_news.append(item)

    # 2. Yahoo奇摩股市
    for item in fetch_yahoo_tw_news(limit=5):
        key = item["title"][:20]
        if key not in seen_titles:
            seen_titles.add(key)
            all_news.append(item)

    # 3. 鉅亨網產業分析
    for item in fetch_anue_industry(limit=5):
        key = item["title"][:20]
        if key not in seen_titles:
            seen_titles.add(key)
            all_news.append(item)

    # 4. CMoney 替代 (鉅亨台股即時)
    for item in fetch_cmoney_news(limit=5):
        key = item["title"][:20]
        if key not in seen_titles:
            seen_titles.add(key)
            all_news.append(item)

    return all_news[:max_items]


def format_telegram_message(news_items: list[dict]) -> str:
    """格式化 Telegram 訊息 (HTML parse_mode)。"""
    today_str = date.today().strftime("%Y/%m/%d")
    lines = [f"📰 <b>每日財經熱門話題</b> — {today_str}\n"]

    for i, item in enumerate(news_items, 1):
        title = item["title"]
        url = item["url"]
        source = item["source"]
        category = item["category"]
        lines.append(f'{i}. <a href="{url}">{title}</a>\n   <i>[{source} · {category}]</i>')

    lines.append(f"\n🔗 來源：鉅亨網 / Yahoo奇摩股市 / CMoney")
    return "\n".join(lines)


def analyze_finance_news_ai(news_items: list[dict]) -> str | None:
    """
    使用 MiniMax AI 分析財經新聞重點摘要。
    Returns: AI 分析結果文字，或 None。
    """
    api_key = os.getenv("MINIMAX_API_KEY", "")
    if not api_key:
        logger.error("MINIMAX_API_KEY not set, cannot analyze finance news")
        return None

    # Build news context
    news_text = "\n".join(
        f"{i}. [{item['source']}] {item['title']}"
        for i, item in enumerate(news_items, 1)
    )

    prompt = f"""# 角色
你是一位資深台股分析師，負責每日盤後新聞彙整與趨勢分析。

# 任務
根據以下今日熱門財經新聞標題，產出簡短的盤後分析摘要。

# 格式要求
- 使用繁體中文
- 先寫 2-3 句「今日重點」總覽
- 分類列出關鍵主題（如：AI/半導體、金融、傳產等）
- 標註可能影響的個股（如有明顯相關者）
- 給出短線觀察方向
- 總字數控制在 300 字以內

# 今日熱門財經新聞（{date.today().strftime('%Y/%m/%d')}）
{news_text}
"""

    data = json.dumps({
        "model": "MiniMax-M2.7",
        "messages": [{"role": "user", "content": prompt}],
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
            # Remove <think>...</think> blocks if present
            content = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL).strip()
            return content if content else None
    except Exception as e:
        logger.error(f"Finance news AI analysis failed: {e}")
        return None


def run_daily_finance_news() -> dict:
    """
    執行每日財經新聞排程任務：
    1. 抓取熱門新聞 10 條
    2. 發送 Telegram
    3. AI 分析並記錄到資料庫

    Returns: {"news_count": int, "telegram_sent": bool, "ai_saved": bool}
    """
    from src.services.telegram_service import send_telegram_message
    from src.core.database import add_note

    result = {"news_count": 0, "telegram_sent": False, "ai_saved": False}

    # Step 1: 抓取新聞
    news_items = collect_finance_news(max_items=10)
    result["news_count"] = len(news_items)

    if not news_items:
        logger.warning("No finance news collected, skipping")
        return result

    logger.info(f"Collected {len(news_items)} finance news items")

    # Step 2: 發送 Telegram
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    message = format_telegram_message(news_items)

    if send_telegram_message(bot_token, chat_id, message):
        result["telegram_sent"] = True
        logger.info("Finance news Telegram message sent")
    else:
        logger.error("Failed to send finance news Telegram message")

    # Step 3: AI 分析 & 記錄
    analysis = analyze_finance_news_ai(news_items)
    if analysis:
        today_str = date.today().isoformat()
        title = f"📰 每日財經新聞分析 — {date.today().strftime('%Y/%m/%d')}"
        # 組合新聞列表 + AI 分析
        full_content = "【今日熱門新聞】\n"
        full_content += "\n".join(f"{i}. {item['title']}" for i, item in enumerate(news_items, 1))
        full_content += f"\n\n【AI 分析】\n{analysis}"

        note_id = add_note(
            stock_code="NEWS",
            content=full_content,
            user_id="shared",
            news_date=today_str,
            title=title,
        )
        result["ai_saved"] = True
        logger.info(f"Finance news AI analysis saved: note_id={note_id}")

        # 也發送 AI 分析到 Telegram
        ai_msg = f"🤖 <b>AI 盤後分析</b> — {date.today().strftime('%Y/%m/%d')}\n\n{analysis}"
        send_telegram_message(bot_token, chat_id, ai_msg)
    else:
        logger.warning("AI analysis returned empty, skipped saving")

    return result
