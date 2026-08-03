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


def _get_today_news_note() -> tuple[int | None, list[str]]:
    """取得今天的「每日財經熱門話題」備註 id 和已有的標題列表。"""
    import psycopg2
    from psycopg2.extras import RealDictCursor
    from src.core.pg_client import DB_CONFIG

    today_str = date.today().isoformat()
    today_display = date.today().strftime('%Y/%m/%d')
    target_title = f"📰 每日財經熱門話題 — {today_display}"

    conn = psycopg2.connect(**DB_CONFIG)
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            "SELECT id, content FROM watchlist_notes WHERE stock_code='NEWS' AND user_id='shared' AND title=%s AND news_date=%s",
            (target_title, today_str)
        )
        row = cur.fetchone()
        if row:
            existing_titles = [line.split(". ", 1)[1].rsplit("  [", 1)[0] if ". " in line else line
                               for line in (row["content"] or "").strip().split("\n") if line.strip()]
            return row["id"], existing_titles
        return None, []
    finally:
        conn.close()


def run_hourly_news_collect() -> int:
    """
    每小時抓取新聞，疊加至今天的「每日財經熱門話題」備註，重複的跳過。
    Returns: 新增的新聞條數。
    """
    import psycopg2
    from src.core.pg_client import DB_CONFIG
    from src.core.database import add_note

    # 抓取最新新聞
    news_items = collect_finance_news(max_items=10)
    if not news_items:
        return 0

    today_str = date.today().isoformat()
    today_display = date.today().strftime('%Y/%m/%d')
    target_title = f"📰 每日財經熱門話題 — {today_display}"

    # 取得已有的備註
    note_id, existing_titles = _get_today_news_note()

    # 去重：只加入新的
    new_items = []
    for item in news_items:
        # 用前 15 字做模糊比對避免重複
        title_prefix = item["title"][:15]
        if not any(title_prefix in t for t in existing_titles):
            new_items.append(item)

    if not new_items:
        logger.info("Hourly news: no new items to add")
        return 0

    # 組合新內容
    all_titles = existing_titles.copy()
    for item in new_items:
        all_titles.append(item["title"])

    # 重新編號
    new_content = "\n".join(f"{i}. {t}  " for i, t in enumerate(all_titles, 1))

    if note_id:
        # 更新既有備註
        conn = psycopg2.connect(**DB_CONFIG)
        try:
            cur = conn.cursor()
            cur.execute("UPDATE watchlist_notes SET content=%s WHERE id=%s", (new_content, note_id))
            conn.commit()
        finally:
            conn.close()
    else:
        # 建立新備註
        add_note(
            stock_code="NEWS",
            content=new_content,
            user_id="shared",
            news_date=today_str,
            title=target_title,
        )

    logger.info(f"Hourly news: added {len(new_items)} new items (total: {len(all_titles)})")
    return len(new_items)


def run_daily_ai_analysis() -> dict:
    """
    每日 17:00 執行：
    1. 讀取今天累積的所有新聞標題
    2. AI 盤後分析
    3. 存入備註
    4. Telegram 發送（新聞列表 + AI 分析）

    Returns: {"telegram_sent": bool, "ai_saved": bool}
    """
    from src.services.telegram_service import send_telegram_message
    from src.core.database import add_note

    result = {"telegram_sent": False, "ai_saved": False}

    today_str = date.today().isoformat()
    today_display = date.today().strftime('%Y/%m/%d')

    # 讀取今天累積的新聞
    note_id, existing_titles = _get_today_news_note()
    if not existing_titles:
        logger.warning("No news collected today, skipping AI analysis")
        return result

    # 組成 news_items 格式給 AI 分析
    news_items = [{"title": t, "source": "", "url": "", "category": ""} for t in existing_titles]

    # 發送 Telegram — 新聞列表
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    message = format_telegram_message(news_items)
    if send_telegram_message(bot_token, chat_id, message):
        result["telegram_sent"] = True

    # AI 分析
    analysis = analyze_finance_news_ai(news_items)
    if analysis:
        add_note(
            stock_code="NEWS",
            content=analysis,
            user_id="shared",
            news_date=today_str,
            title=f"🤖 AI 盤後分析 — {today_display}",
        )
        result["ai_saved"] = True
        logger.info("Daily AI analysis saved")

        # Telegram 發送 AI 分析
        ai_msg = f"🤖 <b>AI 盤後分析</b> — {today_display}\n\n{analysis}"
        send_telegram_message(bot_token, chat_id, ai_msg)
    else:
        logger.warning("AI analysis returned empty")

    return result


def run_daily_finance_news() -> dict:
    """向後相容：手動觸發時同時執行抓取+分析。"""
    count = run_hourly_news_collect()
    result = run_daily_ai_analysis()
    result["news_count"] = count
    return result
