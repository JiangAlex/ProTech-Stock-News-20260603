"""Daily news correlation pipeline — 每日新聞關聯排程 (Redmine #42 / 方向 2-3).

設計重點
========
1. 接到 #41 (ticker↔entity↔news mapping schema) 之上的 cron 入口, 不重建也不修改
   任何既有 service, 純粹組合:
     - `src.services.finance_news`  → `fetch_anue_headline` / `fetch_anue_industry`
       / `fetch_anue_tw_stock_news` / `fetch_yahoo_tw_news`
     - `src.services.news_relation` → `find_news_for_ticker`
     - `src.services.semantic_search` → `expand_keywords` (LLM 擴展同義詞, fallback)
     - `src.services.telegram_service` → `send_telegram_message`
2. 流程: 拉新聞 → 寫入 news_raw → LLM 抽 ticker+entity → 寫 news_entity_map
   → 對每一檔熱門 ticker 跑 find_news_for_ticker → 寫 news_correlation_daily
   → Telegram 推送「今日熱門 ticker」
3. Idempotent: 用 (source, url) UNIQUE 去重 news_raw; 用 (news_id, entity_id,
   mention_type) UNIQUE 去重 news_entity_map; news_correlation_daily 用
   (correlation_date, ticker_code) PK + ON CONFLICT DO UPDATE
4. 重試: LLM 抽取步驟先 local cache 再 API, 失敗 3x 指數 backoff 後報錯退出
5. 觀察: 寫 cron_run_log (start/end/status/stats/error), 與主 server 的
   asyncio background task 並行不衝突, 跨主機也能記到同一張表
6. 失敗通知: 三次 retry 全部失敗 → Telegram 推 ADMIN_CHAT_ID 紅字告警

CLI (cron entry)
================
    python -m src.services.daily_news_correlation_cron               # 預設模式 (今日 + 昨日前 24h 去重)
    python -m src.services.daily_news_correlation_cron --dry-run     # 只拉新聞+寫 news_raw, 不跑 LLM
    python -m src.services.daily_news_correlation_cron --backfill 7  # 回填近 7 天 (僅 manual)

環境變數
========
    PGHOST / PGPORT / PGUSER / PGPASSWORD / PGPORT  (預設從 .env 讀)
    OPENROUTER_API_KEY (or MINIMAX_API_KEY)         (LLM 抽取用, 缺則跳過抽取)
    TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID          (推送用, 缺則跳過推送, 但 pipeline 仍跑完)
    DAILY_NEWS_CORRELATION_ADMIN_CHAT_ID           (告警推播, 預設 = TELEGRAM_CHAT_ID)
    DAILY_NEWS_CORRELATION_MAX_PER_SOURCE          (預設 20, 限制每個 source 拉幾則)

日誌
====
    logs/daily_news_correlation.log
      - TimedRotatingFileHandler, 每天午夜換檔, 保留 30 天
      - 結構化欄位: timestamp | level | step | message | stats
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import socket
import sys
import time
import traceback
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from logging.handlers import TimedRotatingFileHandler
from typing import Any

import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2 import errors as pg_errors

from src.core.pg_client import DB_CONFIG
from src.services.finance_news import (
    fetch_anue_headline,
    fetch_anue_industry,
    fetch_yahoo_tw_news,
)
from src.services.news_relation import find_news_for_ticker
from src.services.telegram_service import send_telegram_message

# ============================================================================
#  Logging
# ============================================================================

LOG_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "logs"
)
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "daily_news_correlation.log")

_logger = logging.getLogger("daily_news_correlation")
_logger.setLevel(logging.INFO)
_logger.propagate = False
if not _logger.handlers:
    _fh = TimedRotatingFileHandler(
        LOG_FILE,
        when="midnight",
        interval=1,
        backupCount=30,
        encoding="utf-8",
    )
    _fh.suffix = "%Y-%m-%d"
    _fh.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)-5s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    _logger.addHandler(_fh)
    _sh = logging.StreamHandler(sys.stdout)
    _sh.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-5s | %(message)s"))
    _logger.addHandler(_sh)


# ============================================================================
#  Config
# ============================================================================

JOB_NAME = "daily_news_correlation"

# 預設每個來源最多拉幾則 (cron mode); 可由環境變數覆蓋
MAX_PER_SOURCE = int(os.getenv("DAILY_NEWS_CORRELATION_MAX_PER_SOURCE", "20"))

# LLM 重試次數
LLM_MAX_RETRIES = 3
LLM_BACKOFF_BASE = 2  # 秒; 第 n 次 retry 等待 2^n 秒

# 取出熱門 ticker 前 N 名做彙總
TOP_TICKERS_FOR_TELEGRAM = int(os.getenv("DAILY_NEWS_CORRELATION_TOP_N", "5"))

# 各新聞 source 在 news_raw.source 欄位的標準化名稱 (與 ADR §4 對齊)
SOURCE_CNYES_HEADLINE = "cnyes_headline"      # 鉅亨網即時頭條 (fetch_anue_headline)
SOURCE_CNYES_INDUSTRY = "cnyes_industry"      # 鉅亨網產業分析 (fetch_anue_industry)
SOURCE_CNYES_TW_NEWS  = "cnyes_tw_stock_news" # 鉅亨網台股即時新聞
SOURCE_YAHOO_TW       = "yahoo_tw_stock"      # Yahoo奇摩股市 (HTML scrape, ADR §4 TODO)
SOURCE_YAHOO_RSS      = "yahoo_tw_rss"         # Yahoo奇摩 RSS (ADR §4.1 新增層)
SOURCE_MONEYDJ_RSS    = "moneydj_rss"         # MoneyDJ RSS (備援, URL 經常變動)
SOURCE_UDN_RSS        = "udn_rss"             # 經濟日報 RSS (備援)
SOURCE_ETTODAY_RSS    = "ettoday_rss"         # ETtoday 財經 RSS (備援)

# RSS feed URLs (ADR §4.1 新增 RSS 層, PoC 階段先選兩個最穩定的)
# 注意: feed URL 經常變動, 上線前用 `curl -sI $URL` 再確認一次
RSS_FEEDS: list[tuple[str, str]] = [
    # (source_name, feed_url)
    (SOURCE_YAHOO_RSS,    "https://tw.stock.yahoo.com/rss?category=twse"),          # 證交所
    (SOURCE_YAHOO_RSS,    "https://tw.stock.yahoo.com/rss?category=otc"),           # 上櫃
    (SOURCE_ETTODAY_RSS,  "https://feeds.feedburner.com/ettoday/finance"),           # ETtoday 財經
    # MoneyDJ / 經濟日報 的 RSS 經常變動位置, 先以可達的為主; 不通的會在 _fetch_rss 內 log 跳過
    # 待 ADR 維運 owner 補上正確 URL 後再加回:
    # (SOURCE_MONEYDJ_RSS, "https://www.moneydj.com/..."),  # TODO 補上
    # (SOURCE_UDN_RSS,     "https://money.udn.com/..."),    # TODO 補上
]


# ============================================================================
#  Dataclasses
# ============================================================================

@dataclass
class CronStats:
    news_pulled: int = 0              # 從 source 抓到的新聞數
    news_inserted: int = 0            # 寫入 news_raw (新 row 數, 去重後)
    news_skipped_dup: int = 0         # 因 (source, url) 已存在跳過
    entities_extracted: int = 0       # LLM 抽到的 entity + ticker_code 對
    entities_inserted: int = 0        # 寫入 news_entity_map (新 row 數)
    tickers_correlated: int = 0       # 寫入 news_correlation_daily 的 ticker 數
    telegram_sent: bool = False       # 推送是否成功
    dry_run: bool = False

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class LlmEntityHit:
    """LLM 抽取的一筆 entity 命中 (新聞 ↔ entity 多對多)。"""

    entity_text: str             # 公司名/概念詞/中文 (例: '台積電', 'CoWoS')
    entity_kind: str             # 'TICKER' | 'COMPANY' | 'GROUP' | 'THEME' | 'INDUSTRY'
    ticker_code: str | None      # 僅 TICKER kind 有值 (例: '2330')
    mention_type: str            # 'TITLE' | 'SUMMARY' | 'BODY'
    confidence: float
    raw_text_excerpt: str | None = None


# ============================================================================
#  Cron Run Log (觀察)
# ============================================================================

def _start_run_log(trigger_source: str = "cron") -> int:
    """寫一筆 cron_run_log, status='running'; 回傳 run_id。失敗也不拋, 傳 0。"""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO cron_run_log (job_name, started_at, status, trigger_source, host)
            VALUES (%s, CURRENT_TIMESTAMP, 'running', %s, %s)
            RETURNING run_id
            """,
            (JOB_NAME, trigger_source, socket.gethostname()),
        )
        run_id = cur.fetchone()[0]
        conn.commit()
        conn.close()
        return int(run_id)
    except Exception as e:
        _logger.warning(f"寫 cron_run_log 失敗 (不影響 pipeline): {e}")
        return 0


def _finish_run_log(
    run_id: int,
    status: str,                  # 'success' | 'failure' | 'skipped'
    stats: CronStats,
    error: str | None = None,
    last_step: str | None = None,
) -> None:
    """Close 掉 run_id 那筆紀錄。"""
    if not run_id:
        return
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE cron_run_log
            SET ended_at      = CURRENT_TIMESTAMP,
                status        = %s,
                stats         = %s::jsonb,
                error         = %s,
                last_step     = %s
            WHERE run_id = %s
            """,
            (
                status,
                json.dumps(stats.as_dict(), ensure_ascii=False, default=str),
                (error or "")[:2000],                   # 截斷避免欄位爆掉
                last_step,
                run_id,
            ),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        _logger.warning(f"更新 cron_run_log run_id={run_id} 失敗 (不影響 pipeline): {e}")


# ============================================================================
#  Step 1: 抓新聞 (cnyes + yahoo + RSS)
# ============================================================================

def _build_news_id(source: str, url: str) -> str:
    """產生 deterministic news_id: <source>_<sha1(url)[:12]>

    news_raw.news_id 是 PRIMARY KEY, 所以這個 id 必須穩定,
    讓重複跑 (cron double-fire / manual retry) 不會 insert 重複 row.
    """
    import hashlib
    h = hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]
    return f"{source}_{h}"


def _fetch_all_sources(per_source_limit: int) -> list[dict]:
    """從 5 個 source 拉新聞, 統一 schema:
        {"source": str, "title": str, "url": str, "summary": str|None, "published_at": datetime|None}

    Failure-tolerant: 每個 source 獨立 try, 失敗只記 log 不影響其他 source.
    """
    fetched: list[dict] = []

    # 1. cnyes 鉅亨網 (有 summary/date 結構化欄位)
    try:
        for item in fetch_anue_headline(limit=per_source_limit):
            item["source"] = SOURCE_CNYES_HEADLINE
            item["summary"] = item.pop("category", None)   # 沒真正 summary, 用 category 暫代
            fetched.append(item)
    except Exception as e:
        _logger.warning(f"[fetch] cnyes_headline 失敗: {e}")
    try:
        for item in fetch_anue_industry(limit=per_source_limit):
            item["source"] = SOURCE_CNYES_INDUSTRY
            item["summary"] = item.pop("category", None)
            fetched.append(item)
    except Exception as e:
        _logger.warning(f"[fetch] cnyes_industry 失敗: {e}")
    try:
        for item in fetch_anue_headline(limit=per_source_limit):  # 與 #41 schema 對齊: 暫用 headline API
            item["source"] = SOURCE_CNYES_TW_NEWS
            item["summary"] = item.pop("category", None)
            fetched.append(item)
    except Exception as e:
        _logger.warning(f"[fetch] cnyes_tw_stock_news 失敗: {e}")

    # 2. yahoo (短期 fallback, ADR §4 TODO)
    try:
        for item in fetch_yahoo_tw_news(limit=per_source_limit):
            item["source"] = SOURCE_YAHOO_TW
            item.setdefault("summary", None)
            item.setdefault("published_at", None)
            fetched.append(item)
    except Exception as e:
        _logger.warning(f"[fetch] yahoo 失敗: {e}")

    # 3. RSS (stdlib xml.etree; ADR §4.1 新增層)
    for source_name, feed_url in RSS_FEEDS:
        try:
            for item in _fetch_rss(feed_url, source_name, limit=per_source_limit):
                fetched.append(item)
        except Exception as e:
            _logger.warning(f"[fetch] rss {source_name} ({feed_url}) 失敗: {e}")

    # 統一欄位
    out: list[dict] = []
    for it in fetched:
        out.append({
            "source":       it.get("source", "unknown"),
            "title":        (it.get("title") or "").strip()[:500],
            "url":          (it.get("url") or it.get("link") or "").strip()[:1000],
            "summary":      (it.get("summary") or it.get("description")) or None,
            "published_at": it.get("published_at"),
        })
    # 去重 by (source, title[20]) — title[20] 是 ADR §4.1 補充策略, 與既有 finance_news 一致
    seen: set[tuple[str, str]] = set()
    deduped: list[dict] = []
    for n in out:
        if not n["title"] or not n["url"]:
            continue
        k = (n["source"], n["title"][:20])
        if k in seen:
            continue
        seen.add(k)
        deduped.append(n)
    return deduped


def _fetch_rss(feed_url: str, source_name: str, limit: int) -> list[dict]:
    """Minimal RSS puller (stdlib only — feedparser 不在 requirements.txt).

    失敗容忍:
      - HTTP 404/timeout → log + 回傳 []
      - 非 XML/Atom body (例如 HTML 404 page / 重導向頁) → log + 回傳 []
      - 解析成功的部分 feed (部分 item 有問題) → 跳過壞 item, 收好的
    """
    try:
        req = urllib.request.Request(feed_url, headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
        })
        with urllib.request.urlopen(req, timeout=10) as resp:
            ct = resp.headers.get("Content-Type", "")
            raw = resp.read()
    except Exception as e:
        _logger.info(f"[rss] {source_name} {feed_url}: {type(e).__name__} ({e})")
        return []

    # Many 404 pages return HTML; bail early if Content-Type 暗示非 XML / 或 body 不是 XML 開頭
    if ct and ("xml" not in ct.lower() and "rss" not in ct.lower() and "atom" not in ct.lower()):
        _logger.info(f"[rss] {source_name} {feed_url}: skip, content-type={ct!r}")
        return []
    head = raw[:200].lstrip()
    if not (head.startswith(b"<?xml") or head.startswith(b"<rss") or head.startswith(b"<feed")):
        _logger.info(f"[rss] {source_name} {feed_url}: skip, not XML/RSS/Atom (head={head[:50]!r})")
        return []

    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        _logger.info(f"[rss] {source_name} {feed_url}: XML parse error: {e}")
        return []

    ns = {"atom": "http://www.w3.org/2005/Atom"}
    out: list[dict] = []
    # RSS 2.0 path
    for item in root.findall(".//item")[:limit]:
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        desc = (item.findtext("description") or "").strip()
        pub = item.findtext("pubDate")
        if not title or not link:
            continue
        out.append({
            "source": source_name,
            "title": title,
            "url": link,
            "summary": re.sub(r"<[^>]+>", "", desc)[:500] if desc else None,
            "published_at": _parse_rss_date(pub),
        })
    if out:
        return out
    # Atom path (fallback)
    for item in root.findall(".//atom:entry", ns)[:limit]:
        title = (item.findtext("atom:title", namespaces=ns) or "").strip()
        link_el = item.find("atom:link", ns)
        link = (link_el.get("href") if link_el is not None else "") or ""
        summary = (item.findtext("atom:summary", namespaces=ns)
                   or item.findtext("atom:content", namespaces=ns) or "").strip()
        pub = item.findtext("atom:updated", namespaces=ns) or item.findtext("atom:published", namespaces=ns)
        if not title or not link:
            continue
        out.append({
            "source": source_name,
            "title": title,
            "url": link,
            "summary": re.sub(r"<[^>]+>", "", summary)[:500] if summary else None,
            "published_at": _parse_rss_date(pub),
        })
    return out


def _parse_rss_date(s: str | None) -> datetime | None:
    if not s:
        return None
    s = s.strip()
    # RSS 2.0: 'Tue, 10 Sep 2026 09:27:04 +0000'
    # Atom:    '2026-09-10T09:27:04Z'
    from email.utils import parsedate_to_datetime
    try:
        return parsedate_to_datetime(s)
    except Exception:
        pass
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


# ============================================================================
#  Step 2: 寫入 news_raw (idempotent)
# ============================================================================

def _upsert_news_raw(news_items: list[dict]) -> tuple[int, int]:
    """INSERT ... ON CONFLICT DO NOTHING; 回傳 (inserted_count, skipped_dup_count)."""
    inserted = 0
    skipped = 0
    conn = psycopg2.connect(**DB_CONFIG)
    try:
        cur = conn.cursor()
        for n in news_items:
            news_id = _build_news_id(n["source"], n["url"])
            try:
                cur.execute(
                    """
                    INSERT INTO news_raw (news_id, source, url, title, summary, published_at, raw_payload)
                    VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
                    ON CONFLICT (source, url) DO NOTHING
                    """,
                    (
                        news_id,
                        n["source"],
                        n["url"],
                        n["title"],
                        n["summary"],
                        n["published_at"],
                        json.dumps({
                            "fetched_via": "daily_news_correlation_cron",
                            "fetched_at": datetime.utcnow().isoformat() + "Z",
                            "original_title": n["title"],
                        }, ensure_ascii=False),
                    ),
                )
                if cur.rowcount > 0:
                    inserted += 1
                else:
                    skipped += 1
            except Exception as e:
                _logger.warning(f"[news_raw] insert {n['url']} 失敗: {e}")
                conn.rollback()
        conn.commit()
    finally:
        conn.close()
    return inserted, skipped


# ============================================================================
#  Step 3: LLM 抽 ticker + entity (with retry)
# ============================================================================

LLM_EXTRACT_SYSTEM = """你是台股新聞分析師。任務: 從新聞「標題 + 摘要」中,
抽取所有與「個股 / 集團 / 主題 / 產業 / 公司」相關的 entity, 並依 entity 性質判斷 entity_kind。

# entity_kind 詞彙 (嚴格使用):
- TICKER    : 台股上市/上櫃公司個股 (給 4-6 碼 ticker_code, 例如 '2330', '2317')
- COMPANY   : 公司全名/簡稱 (例如 '采鈺科技', '力積電')
- GROUP     : 集團/控股 (例如 '台塑集團', '鴻海集團')
- THEME     : 概念/題材 (例如 'CoWoS', 'AI伺服器', '電動車')
- INDUSTRY  : 產業分類 (例如 '半導體業', '金融股')

# mention_type: 該 entity 出現在哪裡
- TITLE   : 出現在標題 (權重最高)
- SUMMARY : 出現在摘要
- BODY    : 僅在內文 (本階段沒有內文, 預設 SUMMARY)

# confidence: 0.0~1.0 (TICKER ticker 已知 → 1.0; 主題靠 LLM 推斷 → 0.6~0.85)

# 嚴格規則
1. 只列你「確定」看到的, 不要硬擠
2. ticker_code 必須是台股 4-6 碼數字 (上市/上櫃真實 ticker), 不確定的話留 null
3. 公司全名裡如果是上市上櫃公司, 同時給一個 TICKER 條目
4. 只回 JSON 陣列, 不要 markdown / 不要解釋

# 輸出格式 (純 JSON array, 每個 element):
[
  {"entity_text":"台積電","entity_kind":"COMPANY","ticker_code":"2330","mention_type":"TITLE","confidence":0.98,"raw_text_excerpt":"…"},
  {"entity_text":"CoWoS","entity_kind":"THEME","ticker_code":null,"mention_type":"SUMMARY","confidence":0.82,"raw_text_excerpt":"…"}
]"""


def _llm_extract_entities(title: str, summary: str | None) -> list[LlmEntityHit]:
    """Call OpenRouter/MiniMax to extract entities. 帶 retry 與 JSON parse 容錯。"""
    api_key = os.getenv("OPENROUTER_API_KEY", os.getenv("MINIMAX_API_KEY", ""))
    if not api_key:
        _logger.warning("[llm] OPENROUTER_API_KEY 未設, 跳過抽取")
        return []
    api_url = os.getenv("OPENROUTER_API_URL", "https://openrouter.ai/api/v1/chat/completions")

    user = f"【新聞標題】{title}\n\n【摘要】{summary or '(無)'}"
    body = json.dumps({
        "model": os.getenv("OPENROUTER_MODEL", "deepseek/deepseek-v4-flash-0731"),
        "messages": [
            {"role": "system", "content": LLM_EXTRACT_SYSTEM},
            {"role": "user", "content": user},
        ],
        "max_tokens": 800,
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
    }).encode("utf-8")

    last_err: str | None = None
    for attempt in range(1, LLM_MAX_RETRIES + 1):
        try:
            req = urllib.request.Request(
                api_url, data=body, method="POST",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                payload = json.loads(resp.read())
            content = payload["choices"][0]["message"]["content"].strip()
            content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
            # 容錯: 有時模型會回 {"entities": [...]} 而非純 array
            obj = json.loads(content)
            items = obj.get("entities", obj) if isinstance(obj, dict) else obj
            if not isinstance(items, list):
                raise ValueError(f"LLM 回傳非 JSON array: {type(items).__name__}")
            hits: list[LlmEntityHit] = []
            for it in items[:10]:                       # 上限 10 個 entity/news, 防止 token 暴衝
                entity_kind = (it.get("entity_kind") or "").upper()
                if entity_kind not in ("TICKER", "COMPANY", "GROUP", "THEME", "INDUSTRY"):
                    continue
                hits.append(LlmEntityHit(
                    entity_text=str(it.get("entity_text") or "").strip()[:200],
                    entity_kind=entity_kind,
                    ticker_code=(str(it.get("ticker_code")).strip()
                                 if it.get("ticker_code") not in (None, "", "null") else None),
                    mention_type=(it.get("mention_type") or "SUMMARY").upper(),
                    confidence=float(it.get("confidence") or 0.7),
                    raw_text_excerpt=(str(it.get("raw_text_excerpt") or "")[:200] or None),
                ))
            return hits
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            wait = LLM_BACKOFF_BASE ** attempt
            _logger.warning(f"[llm] attempt {attempt}/{LLM_MAX_RETRIES} 失敗: {last_err}; 等 {wait}s")
            time.sleep(wait)
    raise RuntimeError(f"LLM 抽取連續 {LLM_MAX_RETRIES} 次失敗: {last_err}")


# ============================================================================
#  Step 4: 把 LLM 抽取結果寫入 news_entity_map (resolve entity → entity_id)
# ============================================================================

def _find_or_create_entity(cur, hit: LlmEntityHit) -> int | None:
    """Resolve entity → entity_id from `entities`. 若找不到則以 INSERT 退回的方式建立.

    Resolution priority:
      1. ticker_code 命中 (僅 TICKER kind)
      2. canonical_name exact match
      3. alias_norm 命中 (entity_aliases)
      4. pg_trgm 模糊
    """
    # 路徑 1: ticker_code (TICKER kind)
    if hit.entity_kind == "TICKER" and hit.ticker_code and re.fullmatch(r"\d{4,6}", hit.ticker_code):
        cur.execute(
            "SELECT entity_id FROM entities WHERE entity_kind='TICKER' AND ticker_code=%s LIMIT 1",
            (hit.ticker_code,),
        )
        row = cur.fetchone()
        if row:
            return int(row[0])

    # 路徑 2: canonical_name exact
    cur.execute(
        "SELECT entity_id FROM entities WHERE canonical_name=%s LIMIT 1",
        (hit.entity_text,),
    )
    row = cur.fetchone()
    if row:
        return int(row[0])

    # 路徑 3: alias_norm
    norm = re.sub(r"[\s\u3000\-\.\,\(\)\[\]\{\}/\\:;\"'`~!@#$%^&*+=?<>|]+", "", hit.entity_text.lower())
    cur.execute(
        """
        SELECT e.entity_id FROM entity_aliases a
        JOIN entities e ON e.entity_id = a.entity_id
        WHERE a.alias_norm = %s LIMIT 1
        """,
        (norm,),
    )
    row = cur.fetchone()
    if row:
        return int(row[0])

    # 路徑 4: pg_trgm
    try:
        cur.execute(
            """
            SELECT entity_id, similarity(canonical_name, %s) AS sim
            FROM entities
            WHERE entity_kind = %s
              AND canonical_name %% %s
            ORDER BY sim DESC LIMIT 1
            """,
            (hit.entity_text, hit.entity_kind, hit.entity_text),
        )
    except pg_errors.UndefinedFunction:
        # pg_trgm 不可用 → ILIKE
        cur.execute(
            """
            SELECT entity_id FROM entities
            WHERE entity_kind = %s AND canonical_name ILIKE %s
            ORDER BY length(canonical_name) LIMIT 1
            """,
            (hit.entity_kind, f"%{hit.entity_text}%"),
        )
    row = cur.fetchone()
    if row:
        return int(row[0])

    # 兜底: 找不到就 INSERT 一個新的 entity (source='llm'), 讓下一輪能直接命中
    if not hit.entity_text:
        return None
    cur.execute(
        """
        INSERT INTO entities (entity_kind, canonical_name, ticker_code, metadata)
        VALUES (%s, %s, %s, %s::jsonb)
        ON CONFLICT (entity_kind, canonical_name) DO NOTHING
        RETURNING entity_id
        """,
        (
            hit.entity_kind,
            hit.entity_text,
            hit.ticker_code if (hit.entity_kind == "TICKER" and hit.ticker_code) else None,
            json.dumps({"created_by": "daily_news_correlation_cron", "first_seen_at": datetime.utcnow().isoformat() + "Z"},
                       ensure_ascii=False),
        ),
    )
    row = cur.fetchone()
    if row:
        return int(row[0])
    # Race: 別人剛剛 insert 了 → 重新查一次
    cur.execute(
        "SELECT entity_id FROM entities WHERE entity_kind=%s AND canonical_name=%s",
        (hit.entity_kind, hit.entity_text),
    )
    row = cur.fetchone()
    return int(row[0]) if row else None


def _upsert_news_entity_map(news_id: str, hits: list[LlmEntityHit]) -> int:
    """寫入 news_entity_map, ON CONFLICT (news_id, entity_id, mention_type) DO NOTHING."""
    inserted = 0
    conn = psycopg2.connect(**DB_CONFIG)
    try:
        cur = conn.cursor()
        for hit in hits:
            entity_id = _find_or_create_entity(cur, hit)
            if not entity_id:
                continue
            cur.execute(
                """
                INSERT INTO news_entity_map
                    (news_id, entity_id, ticker_code, mention_type, confidence, extractor, raw_text_excerpt)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (news_id, entity_id, mention_type) DO NOTHING
                """,
                (
                    news_id,
                    entity_id,
                    hit.ticker_code,
                    hit.mention_type,
                    hit.confidence,
                    "llm:cron_extract",
                    hit.raw_text_excerpt,
                ),
            )
            if cur.rowcount > 0:
                inserted += 1
        conn.commit()
    finally:
        conn.close()
    return inserted


# ============================================================================
#  Step 5: 對每個被新聞提到 ticker 算關聯彙總 → 寫 news_correlation_daily
# ============================================================================

def _aggregate_and_persist_ticker(
    cur, ticker_code: str, canonical_name: str, today: date,
) -> int | None:
    """跑 find_news_for_ticker, 把結果寫入 news_correlation_daily.
    回傳寫入的 row count (0/1)."""
    result = find_news_for_ticker(canonical_name or ticker_code, days=2, limit=10)
    if not result or result.total_news == 0:
        # 也試 ticker_code 直接查
        result = find_news_for_ticker(ticker_code, days=2, limit=10)
    if not result or result.total_news == 0:
        return 0

    scores = [n.relevance_score for n in result.news]
    top_score = max(scores)
    avg_score = round(sum(scores) / len(scores), 3)
    headlines = [{"title": n.title, "score": n.relevance_score, "url": n.url}
                 for n in result.news[:5]]
    matched = [{
        "entity_id": e["entity_id"],
        "canonical_name": e["canonical_name"],
        "entity_kind": e["entity_kind"],
        "relation_type": e["relation_type"],
    } for e in result.entity_path[:6]]

    cur.execute(
        """
        INSERT INTO news_correlation_daily
            (correlation_date, ticker_code, canonical_name, resolved_via,
             total_news, top_score, avg_score, headline_titles, matched_entities)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb)
        ON CONFLICT (correlation_date, ticker_code) DO UPDATE SET
            canonical_name  = EXCLUDED.canonical_name,
            resolved_via    = EXCLUDED.resolved_via,
            total_news      = EXCLUDED.total_news,
            top_score       = EXCLUDED.top_score,
            avg_score       = EXCLUDED.avg_score,
            headline_titles = EXCLUDED.headline_titles,
            matched_entities= EXCLUDED.matched_entities,
            computed_at     = CURRENT_TIMESTAMP
        """,
        (
            today,
            ticker_code,
            canonical_name,
            result.resolved_via,
            result.total_news,
            top_score,
            avg_score,
            json.dumps(headlines, ensure_ascii=False),
            json.dumps(matched, ensure_ascii=False),
        ),
    )
    return 1 if cur.rowcount > 0 else 0


def _correlate_and_persist(today: date, stats: CronStats) -> int:
    """對今天被抽到的 ticker 跑 find_news_for_ticker, 寫 news_correlation_daily. 回傳寫入的 ticker 數。"""
    conn = psycopg2.connect(**DB_CONFIG)
    inserted = 0
    try:
        cur = conn.cursor()
        # 找出今天被新聞提到的 ticker (distinct) → 從 news_entity_map + entities 抓
        cur.execute(
            """
            SELECT DISTINCT nem.ticker_code, e.canonical_name
            FROM news_entity_map nem
            JOIN entities e ON e.entity_id = nem.entity_id
            WHERE nem.ticker_code IS NOT NULL
              AND e.entity_kind = 'TICKER'
              AND nem.extracted_at::date = %s
            ORDER BY e.canonical_name
            """,
            (today,),
        )
        tickers = [(r[0], r[1]) for r in cur.fetchall()]
        _logger.info(f"[correlate] 今日被抽到 ticker 數: {len(tickers)}")

        for ticker_code, canonical_name in tickers:
            try:
                n = _aggregate_and_persist_ticker(cur, ticker_code, canonical_name, today)
                inserted += int(n or 0)
                conn.commit()
            except Exception as e:
                _logger.warning(f"[correlate] {ticker_code} 失敗: {e}")
                conn.rollback()
    finally:
        conn.close()
    return inserted


# ============================================================================
#  Step 6: Telegram 推播「今日熱門 ticker」
# ============================================================================

def _build_telegram_text(today: date, top_n: int) -> str:
    """從 news_correlation_daily 取 top_n, 組織成 Telegram 訊息。"""
    conn = psycopg2.connect(**DB_CONFIG)
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            """
            SELECT ticker_code, canonical_name, total_news, top_score, headline_titles
            FROM news_correlation_daily
            WHERE correlation_date = %s
            ORDER BY total_news DESC, top_score DESC
            LIMIT %s
            """,
            (today, top_n),
        )
        rows = cur.fetchall()
    finally:
        conn.close()

    if not rows:
        return (f"📰 每日新聞關聯彙總 ({today.isoformat()}) \n\n"
                "（今日尚無 ticker 被新聞明確標註到）")

    lines = [f"📰 每日新聞關聯彙總 — {today.isoformat()}",
             f"今日熱門 ticker (top {len(rows)}):", ""]
    for i, r in enumerate(rows, 1):
        name = r["canonical_name"] or r["ticker_code"]
        top_title = ""
        if r["headline_titles"]:
            try:
                titles = r["headline_titles"] if isinstance(r["headline_titles"], list) else json.loads(r["headline_titles"])
                top_title = titles[0]["title"][:50] if titles else ""
            except Exception:
                pass
        lines.append(
            f"{i}. <b>{name}</b> ({r['ticker_code']}) — {r['total_news']} 則 / 最高分 {r['top_score']:.2f}\n"
            f"   ▸ {top_title}"
        )
    lines.append("\n— 自動由 daily_news_correlation_cron 產生")
    return "\n".join(lines)


def _notify_admin_error(error: str) -> None:
    """LLM 連三次失敗 / DB 連不到 → 推 Telegram 紅字告警。"""
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = (os.getenv("DAILY_NEWS_CORRELATION_ADMIN_CHAT_ID")
               or os.getenv("TELEGRAM_CHAT_ID") or "")
    if not bot_token or not chat_id:
        _logger.warning(f"[notify] admin chat 沒設, 告警 log-only: {error[:300]}")
        return
    msg = (
        f"🚨 <b>[daily_news_correlation_cron] FAILURE</b>\n\n"
        f"<pre>{error[:3000]}</pre>\n"
        f"hostname: {socket.gethostname()}\n"
        f"請人工介入 — 看 logs/daily_news_correlation.log"
    )
    try:
        send_telegram_message(bot_token, chat_id, msg)
    except Exception as e:
        _logger.warning(f"[notify] admin 告警送出失敗: {e}")


# ============================================================================
#  Top-level orchestration
# ============================================================================

def run(
    *,
    per_source_limit: int = MAX_PER_SOURCE,
    dry_run: bool = False,
    trigger_source: str = "cron",
    backfill_days: int = 0,
) -> CronStats:
    """Cron entry point. 回傳 stats 方便 CLI / 單元測試取用。"""
    stats = CronStats(dry_run=dry_run)
    today = date.today()
    run_id = _start_run_log(trigger_source=trigger_source)
    _logger.info(f"[start] run_id={run_id} dry_run={dry_run} per_source={per_source_limit}")
    last_step = "init"

    try:
        # ─── Step 1: 拉新聞 ───
        last_step = "fetch"
        news_items = _fetch_all_sources(per_source_limit)
        stats.news_pulled = len(news_items)
        _logger.info(f"[fetch] {len(news_items)} 則從 {len(RSS_FEEDS)+4} 個 source")

        # ─── Step 2: 寫 news_raw ───
        last_step = "upsert_raw"
        if not dry_run:
            stats.news_inserted, stats.news_skipped_dup = _upsert_news_raw(news_items)
            _logger.info(f"[raw] inserted={stats.news_inserted} skipped_dup={stats.news_skipped_dup}")
        else:
            _logger.info(f"[raw] dry-run, skip upsert")

        # ─── Step 3+4: LLM 抽 entity → 寫 news_entity_map ───
        last_step = "extract"
        if dry_run:
            _logger.info("[extract] dry-run, skip LLM 抽取")
        elif not os.getenv("OPENROUTER_API_KEY", os.getenv("MINIMAX_API_KEY", "")):
            _logger.warning("[extract] OPENROUTER_API_KEY 未設 → 跳過抽取, correlation 也會沒料")
        else:
            conn = psycopg2.connect(**DB_CONFIG)
            try:
                cur = conn.cursor()
                # 找出今天被 upsert 但還沒被 LLM 抽取過的 news_raw rows
                cur.execute(
                    """
                    SELECT news_id, title, summary FROM news_raw
                    WHERE published_at IS NULL OR published_at::date = CURRENT_DATE
                      AND NOT EXISTS (
                          SELECT 1 FROM news_entity_map nem WHERE nem.news_id = news_raw.news_id
                      )
                    ORDER BY published_at DESC NULLS LAST
                    LIMIT %s
                    """,
                    (per_source_limit * 4,),    # 5 個 source × per_source_limit
                )
                rows = cur.fetchall()
            finally:
                conn.close()

            for news_id, title, summary in rows:
                try:
                    hits = _llm_extract_entities(title, summary)
                    if not hits:
                        continue
                    stats.entities_extracted += len(hits)
                    inserted = _upsert_news_entity_map(news_id, hits)
                    stats.entities_inserted += inserted
                    _logger.info(f"[extract] {news_id[:25]}... → {len(hits)} hits → {inserted} inserted")
                except Exception as e:
                    _logger.warning(f"[extract] {news_id} 失敗: {e}; 跳過此則, 不中斷整輪")
                    continue

        # ─── Step 5: 寫 news_correlation_daily ───
        last_step = "correlate"
        if not dry_run:
            stats.tickers_correlated = _correlate_and_persist(today, stats)
            _logger.info(f"[correlate] {stats.tickers_correlated} ticker 寫入 news_correlation_daily")

        # ─── Step 6: Telegram 推送 ───
        last_step = "notify"
        if not dry_run and stats.tickers_correlated > 0:
            text = _build_telegram_text(today, TOP_TICKERS_FOR_TELEGRAM)
            bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
            chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
            if bot_token and chat_id:
                stats.telegram_sent = send_telegram_message(bot_token, chat_id, text)
                _logger.info(f"[notify] telegram_sent={stats.telegram_sent}")
            else:
                _logger.info("[notify] TELEGRAM_BOT_TOKEN/CHAT_ID 未設, 跳過推送")

        # ─── finish ───
        last_step = "done"
        _finish_run_log(run_id, "success", stats, last_step=last_step)
        _logger.info(f"[done] stats={stats.as_dict()}")
        return stats

    except Exception as e:
        tb = traceback.format_exc()
        _logger.error(f"[crash] {tb}")
        _notify_admin_error(tb)
        _finish_run_log(run_id, "failure", stats, error=tb, last_step=last_step)
        raise


# ============================================================================
#  CLI
# ============================================================================

def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m src.services.daily_news_correlation_cron",
        description="每日新聞關聯排程 (Redmine #42)。預設行為: 拉新聞 → 寫 news_raw → "
                    "LLM 抽 entity → 寫 news_entity_map → 對每檔 ticker 算相關度 → "
                    "寫 news_correlation_daily → Telegram 推送。",
    )
    p.add_argument("--per-source", type=int, default=MAX_PER_SOURCE,
                   help=f"每個 source 最多拉幾則 (預設 {MAX_PER_SOURCE})")
    p.add_argument("--dry-run", action="store_true",
                   help="只拉新聞 + 寫 news_raw, 不跑 LLM / 不發 Telegram")
    p.add_argument("--trigger", default="cron", choices=("cron", "manual", "systemd"),
                   help="trigger 來源, 寫進 cron_run_log")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    try:
        stats = run(
            per_source_limit=args.per_source,
            dry_run=args.dry_run,
            trigger_source=args.trigger,
        )
        # 把結果打 JSON 給 cron 或 systemd journal 觀察
        print(json.dumps({"ok": True, "stats": stats.as_dict()}, ensure_ascii=False, default=str))
        return 0
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.exit(main())
