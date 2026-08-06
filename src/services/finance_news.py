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


def _get_industry_stocks_context() -> str:
    """
    從 DB 取得各產業類股及其代表個股（依成交量排序），
    作為 AI 分析時的參考資料。
    Returns: 格式化的產業-個股對照文字。
    """
    import psycopg2
    from psycopg2.extras import RealDictCursor
    from src.core.pg_client import DB_CONFIG

    try:
        conn = psycopg2.connect(**DB_CONFIG)
        cur = conn.cursor(cursor_factory=RealDictCursor)

        # 取得最近一天的 daily_indicators 日期
        cur.execute("SELECT MAX(date) as latest FROM daily_indicators")
        row = cur.fetchone()
        latest_date = row["latest"] if row else None

        if not latest_date:
            conn.close()
            return ""

        # 取得各產業前 5 檔個股（依成交量排序）
        cur.execute("""
            SELECT sb.industry, sb.stock_code, sb.stock_name,
                   di.close, di.change_pct, di.volume
            FROM daily_indicators di
            JOIN stock_basic sb ON sb.stock_code = di.stock_code
            WHERE di.date = %s AND sb.industry IS NOT NULL
            ORDER BY sb.industry, di.volume DESC
        """, (latest_date,))
        rows = cur.fetchall()
        conn.close()

        # 整理成 {industry: [stocks...]}
        from collections import defaultdict
        industry_map = defaultdict(list)
        for r in rows:
            if len(industry_map[r["industry"]]) < 5:
                industry_map[r["industry"]].append(r)

        # 格式化輸出
        lines = []
        for industry in sorted(industry_map.keys()):
            stocks = industry_map[industry]
            stock_strs = ", ".join(
                f"{s['stock_code']}{s['stock_name']}({s['change_pct']:+.1f}%)"
                if s.get('change_pct') else f"{s['stock_code']}{s['stock_name']}"
                for s in stocks
            )
            lines.append(f"【{industry}】{stock_strs}")

        return "\n".join(lines)
    except Exception as e:
        logger.error(f"Failed to get industry stocks context: {e}")
        return ""


def _get_industry_rotation_context() -> str:
    """
    分析全部產業類股的 K 線指標，觀察產業資金輪動。
    計算各產業近日平均漲跌幅、成交量變化、多頭比例等。
    Returns: 格式化的產業資金輪動分析文字。
    """
    import psycopg2
    from psycopg2.extras import RealDictCursor
    from src.core.pg_client import DB_CONFIG
    from collections import defaultdict

    try:
        conn = psycopg2.connect(**DB_CONFIG)
        cur = conn.cursor(cursor_factory=RealDictCursor)

        # 取得最近 5 個交易日
        cur.execute("""
            SELECT DISTINCT date FROM daily_indicators
            ORDER BY date DESC LIMIT 5
        """)
        dates = [r["date"] for r in cur.fetchall()]
        if not dates:
            conn.close()
            return ""

        latest_date = dates[0]

        # 取得最近 5 日各產業的聚合指標
        cur.execute("""
            SELECT sb.industry,
                   di.date,
                   AVG(di.change_pct) as avg_change,
                   SUM(di.volume) as total_volume,
                   COUNT(*) FILTER (WHERE di.ma_arrangement = '多頭排列') as bull_count,
                   COUNT(*) FILTER (WHERE di.ma_arrangement = '空頭排列') as bear_count,
                   COUNT(*) as total_count,
                   AVG(di.rsi14) as avg_rsi,
                   AVG(di.volume_ratio) as avg_vol_ratio
            FROM daily_indicators di
            JOIN stock_basic sb ON sb.stock_code = di.stock_code
            WHERE di.date = ANY(%s) AND sb.industry IS NOT NULL
            GROUP BY sb.industry, di.date
            ORDER BY sb.industry, di.date
        """, (dates,))
        rows = cur.fetchall()
        conn.close()

        if not rows:
            return ""

        # 整理：每個產業最近 5 日的趨勢
        industry_data = defaultdict(list)
        for r in rows:
            industry_data[r["industry"]].append(r)

        # 計算各產業指標
        results = []
        for industry, daily_rows in industry_data.items():
            latest_row = next((r for r in daily_rows if r["date"] == latest_date), None)
            if not latest_row:
                continue

            # 最新日指標
            avg_change_today = latest_row["avg_change"] or 0
            total_vol_today = latest_row["total_volume"] or 0
            bull_pct = (latest_row["bull_count"] / latest_row["total_count"] * 100) if latest_row["total_count"] else 0
            bear_pct = (latest_row["bear_count"] / latest_row["total_count"] * 100) if latest_row["total_count"] else 0
            avg_rsi = latest_row["avg_rsi"] or 50
            avg_vol_ratio = latest_row["avg_vol_ratio"] or 1.0

            # 近 5 日累計漲幅
            cumulative_change = sum(r["avg_change"] or 0 for r in daily_rows)

            # 成交量趨勢（最新日 vs 5日前）
            if len(daily_rows) >= 2:
                oldest_vol = daily_rows[0]["total_volume"] or 1
                vol_change_pct = ((total_vol_today - oldest_vol) / oldest_vol * 100) if oldest_vol else 0
            else:
                vol_change_pct = 0

            # 判斷資金流向
            if avg_change_today > 0.5 and avg_vol_ratio > 1.2:
                flow = "🔴 資金流入"
            elif avg_change_today < -0.5 and avg_vol_ratio > 1.2:
                flow = "🟢 資金流出"
            elif avg_vol_ratio > 1.5:
                flow = "⚡ 爆量關注"
            elif cumulative_change > 2:
                flow = "📈 持續走強"
            elif cumulative_change < -2:
                flow = "📉 持續走弱"
            else:
                flow = "➖ 平穩"

            results.append({
                "industry": industry,
                "avg_change_today": avg_change_today,
                "cumulative_5d": cumulative_change,
                "bull_pct": bull_pct,
                "bear_pct": bear_pct,
                "avg_rsi": avg_rsi,
                "avg_vol_ratio": avg_vol_ratio,
                "vol_change_pct": vol_change_pct,
                "flow": flow,
            })

        # 按今日漲幅排序
        results.sort(key=lambda x: x["avg_change_today"], reverse=True)

        # 格式化輸出
        lines = [f"（資料日期：{latest_date}，對比近 5 個交易日）\n"]

        # 前 5 強勢產業
        lines.append("▲ 強勢產業（今日漲幅前 5）：")
        for r in results[:5]:
            lines.append(
                f"  {r['flow']} {r['industry']}：今日{r['avg_change_today']:+.2f}%｜"
                f"5日{r['cumulative_5d']:+.2f}%｜多頭{r['bull_pct']:.0f}%｜"
                f"量比{r['avg_vol_ratio']:.1f}x｜RSI{r['avg_rsi']:.0f}"
            )

        # 後 5 弱勢產業
        lines.append("\n▼ 弱勢產業（今日跌幅前 5）：")
        for r in results[-5:]:
            lines.append(
                f"  {r['flow']} {r['industry']}：今日{r['avg_change_today']:+.2f}%｜"
                f"5日{r['cumulative_5d']:+.2f}%｜空頭{r['bear_pct']:.0f}%｜"
                f"量比{r['avg_vol_ratio']:.1f}x｜RSI{r['avg_rsi']:.0f}"
            )

        # 資金異動產業（量比 > 1.5）
        vol_alert = [r for r in results if r["avg_vol_ratio"] > 1.5]
        if vol_alert:
            lines.append("\n⚡ 成交量異動產業（量比 > 1.5x）：")
            for r in vol_alert[:5]:
                lines.append(
                    f"  {r['industry']}：量比{r['avg_vol_ratio']:.1f}x｜"
                    f"今日{r['avg_change_today']:+.2f}%｜RSI{r['avg_rsi']:.0f}"
                )

        return "\n".join(lines)
    except Exception as e:
        logger.error(f"Failed to get industry rotation context: {e}")
        return ""


def analyze_finance_news_ai(news_items: list[dict]) -> str | None:
    """
    使用 MiniMax AI 分析財經新聞重點摘要，包含概念股/族群/類股/個股/產業資金輪動分析。
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

    # 取得產業/個股參考資料
    industry_context = _get_industry_stocks_context()
    industry_section = ""
    if industry_context:
        industry_section = f"""
# 台股產業類股與代表個股（今日成交量前 5 名）
{industry_context}
"""

    # 取得概念股分類資料
    concept_section = ""
    try:
        from src.services.concept_service import get_concept_stocks_context
        concept_context = get_concept_stocks_context(limit_concepts=30)
        if concept_context:
            concept_section = f"""
# 概念股/題材分類與成分股
{concept_context}
"""
    except Exception as e:
        logger.warning(f"Failed to get concept stocks context: {e}")

    # 取得產業資金輪動分析
    rotation_context = _get_industry_rotation_context()
    rotation_section = ""
    if rotation_context:
        rotation_section = f"""
# 產業類股 K 線指標分析（資金輪動觀察）
{rotation_context}
"""

    prompt = f"""# 角色
你是一位資深台股分析師，負責每日盤後新聞彙整與趨勢分析。

# 任務
根據以下今日熱門財經新聞標題及產業類股 K 線指標數據，產出盤後分析摘要。
特別注意：
1. 你必須根據新聞內容，辨識出相關的「概念股/題材」、「族群」和「產業類股」，並列出每個概念/族群/類股中可能受影響的具體個股（代號＋名稱）。
2. 你必須根據產業 K 線指標數據，分析目前的「產業資金輪動」方向，指出資金正從哪些產業流出、流入哪些產業。

# 格式要求
- 使用繁體中文
- 先寫 2-3 句「今日重點」總覽
- 再列出「🔥 概念股/題材」段落：
  - 每個概念股/題材獨立一行，格式為：概念名稱：相關個股（代號＋名稱）
  - 例如：AI 伺服器概念：2382 廣達、2317 鴻海、3231 緯創
  - 至少列出 2-4 個今日相關的概念股題材
- 再列出「👥 族群動態」段落：
  - 列出今日新聞相關的股票族群（如：蘋果供應鏈、車用電子族群、重電族群等）
  - 格式為：族群名稱：個股代號＋名稱（3-5 檔）
- 再列出「📊 類股動態」段落：
  - 列出今日新聞相關的產業類股及其代表個股
  - 格式為：產業名稱（漲/跌/震盪）：個股代號＋名稱
- 再列出「🔄 產業資金輪動」段落：
  - 根據提供的產業 K 線指標數據（漲跌幅、量比、多頭比例、RSI）
  - 分析資金流向：哪些產業正在吸引資金（強勢）、哪些正在流出（弱勢）
  - 指出輪動方向（如：資金由電子轉向傳產、由大型股轉向中小型等）
  - 標註值得留意的爆量或轉折訊號
- 最後給出「📌 短線觀察」：短線操作方向建議
- 總字數控制在 800 字以內

# 今日熱門財經新聞（{date.today().strftime('%Y/%m/%d')}）
{news_text}
{industry_section}{concept_section}{rotation_section}"""

    # Inject TWII 60-min intraday feedback if available
    try:
        from src.services.twii_intraday import get_intraday_feedback_for_news_ai
        twii_feedback = get_intraday_feedback_for_news_ai()
        if twii_feedback:
            prompt += twii_feedback
    except Exception:
        pass

    data = json.dumps({
        "model": "MiniMax-M2.7",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 3000,
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
    4. Telegram 發送 AI 盤後分析

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

    # AI 分析
    analysis = analyze_finance_news_ai(news_items)
    if analysis:
        # 去重：檢查當天是否已存在同標題的 AI 盤後分析
        ai_title = f"🤖 AI 盤後分析 — {today_display}"
        from src.core.database import get_notes
        existing_notes = get_notes("NEWS", "shared")
        already_exists = any(
            n.get("title") == ai_title and n.get("news_date") == today_str
            for n in existing_notes
        )
        if already_exists:
            logger.info("Daily AI analysis already exists for today, skipping save")
            result["ai_saved"] = True
            return result

        add_note(
            stock_code="NEWS",
            content=analysis,
            user_id="shared",
            news_date=today_str,
            title=ai_title,
        )
        result["ai_saved"] = True
        logger.info("Daily AI analysis saved")

        # Telegram 發送 AI 分析
        ai_msg = f"🤖 <b>AI 盤後分析</b> — {today_display}\n\n{analysis}"
        if send_telegram_message(bot_token, chat_id, ai_msg):
            result["telegram_sent"] = True
    else:
        logger.warning("AI analysis returned empty")

    return result


def run_daily_finance_news() -> dict:
    """向後相容：手動觸發時同時執行抓取+分析。"""
    count = run_hourly_news_collect()
    result = run_daily_ai_analysis()
    result["news_count"] = count
    return result
