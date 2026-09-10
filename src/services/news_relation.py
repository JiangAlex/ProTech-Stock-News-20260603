"""News-Ticker association (方向 2-2 / Redmine #41)

接收 ticker entity (代號 / 中文名 / 英文名 / 別名),輸出近 N 天被該 ticker
/集團 /主題 /產業 提及的新聞清單,並給每則新聞打分 (相關度)。

設計重點:
- 走 `ticker_entity_map` 展開「一檔 ticker → 多個 entity」,再 JOIN `news_entity_map`
  → `news_raw`, 一次拿到 ticker 直接相關 + 主題/產業間接相關的新聞
- 相關度分數 `relevance_score` = SUM(confidence × weight(relation_type)),
  規範化到 0~1 後輸出 (避免天數越多分母膨脹)
- 不重建 watchlist_notes, 而是與既有 `search_notes()` 並存; 提供
  `find_news_for_ticker()` 作為主入口, 既有路徑用於 user private note

CLI:
    python -m src.services.news_relation --ticker 2330 --days 7 --limit 20
    python -m src.services.news_relation --ticker 台積電 --format json

Python API:
    from src.services.news_relation import find_news_for_ticker, search_by_entity_name
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from dataclasses import dataclass, field, asdict
from typing import Any

import psycopg2
from psycopg2.extras import RealDictCursor

from src.core.pg_client import DB_CONFIG

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
#  Scoring weights (按 relation_type 給不同權重)
#  - IS_TICKER:        ticker 直接出現 → 權重最高
#  - BELONGS_TO_GROUP: 集團層 (有擴散效益但非個股直指)
#  - BELONGS_TO_INDUSTRY: 產業層 (最弱, 但累積起來有意義)
#  - HAS_THEME:        主題/概念 (隨報導密度有顯著差異)
#  - SUPPLIES_TO / CUSTOMER_OF / RELATED_TO: 目前 schema 未大量填,
#    但保留欄位機制 (見 schema/ticker-entity-mapping.md §3.3)
# ---------------------------------------------------------------------------
RELATION_WEIGHTS: dict[str, float] = {
    "IS_TICKER": 1.0,
    "BELONGS_TO_GROUP": 0.7,
    "BELONGS_TO_INDUSTRY": 0.4,
    "HAS_THEME": 0.6,
    "SUPPLIES_TO": 0.5,
    "CUSTOMER_OF": 0.5,
    "RELATED_TO": 0.3,
}

# mention_type 權重: 同樣是 HAS_THEME 出現在標題比 summary 中相關度更高
MENTION_WEIGHTS: dict[str, float] = {
    "TITLE": 1.0,
    "SUMMARY": 0.7,
    "BODY": 0.5,
}


@dataclass
class ScoredNews:
    """一則新聞 + 與目標 ticker 的相關度證據"""

    news_id: str
    title: str
    source: str
    url: str
    published_at: str | None
    summary: str | None
    relevance_score: float           # 0~1
    matched_entities: list[dict] = field(default_factory=list)
    """每筆匹配證據, 結構:
        {entity_id, canonical_name, entity_kind, relation_type, mention_type, confidence}
    """

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class TickerNewsResult:
    """整體結果: ticker + 找到的新聞 + 使用的 entity 證據"""

    ticker_code: str
    canonical_name: str | None
    resolved_via: str                # 'ticker_code' | 'alias' | 'canonical_name' | 'concept'
    resolution_details: dict         # 解析過程的證據
    days: int
    total_news: int
    news: list[ScoredNews]
    entity_path: list[dict] = field(default_factory=list)
    """從 ticker → 各 entity 的展開路徑 (方便審核)"""

    def to_dict(self) -> dict:
        return {
            "ticker_code": self.ticker_code,
            "canonical_name": self.canonical_name,
            "resolved_via": self.resolved_via,
            "resolution_details": self.resolution_details,
            "days": self.days,
            "total_news": self.total_news,
            "news": [n.to_dict() for n in self.news],
            "entity_path": self.entity_path,
        }


# ===========================================================================
#  Step 1: 解析輸入 (ticker code / 中文 / 英文 / 別名 → entity)
# ===========================================================================

def _normalize(text: str) -> str:
    """低正規化: lowercase + 去空白/標點。用於 alias_norm 比對。"""
    if not text:
        return ""
    t = text.strip().lower()
    t = re.sub(r"[\s\u3000\-\.\,\(\)\[\]\{\}/\\:;\"'`~!@#$%^&*+=?<>|]+", "", t)
    return t


def resolve_ticker(query: str) -> dict:
    """輸入「2330 / 台積電 / TSMC / 台積」之類字串, 解析到 ticker_code + entity 集合。

    Returns:
        {
            "ticker_code": "2330",
            "canonical_name": "台積電",
            "resolved_via": "ticker_code" | "alias" | "canonical_name" | "concept",
            "entities": [         # 此 ticker 直接/間接相關的所有 entity
                {"entity_id": 1, "canonical_name": "台積電", "entity_kind": "TICKER",
                 "relation_type": "IS_TICKER", "confidence": 1.0},
                ...
            ],
        }
    或 None (找不到)。
    """
    if not query or not query.strip():
        return None
    raw = query.strip()
    norm = _normalize(raw)

    conn = psycopg2.connect(**DB_CONFIG)
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)

        # 路徑 A: ticker_code 直接命中 (從 stock_basic 或 entities.ticker_code)
        if re.fullmatch(r"[0-9]{4,6}", raw):
            cur.execute("""
                SELECT e.entity_id, e.canonical_name, e.entity_kind, e.ticker_code,
                       'IS_TICKER' AS relation_type, 1.0 AS confidence
                FROM entities e
                WHERE e.entity_kind = 'TICKER' AND e.ticker_code = %s
                LIMIT 1
            """, (raw,))
            row = cur.fetchone()
            if row:
                return _expand_from_ticker(cur, row, resolved_via="ticker_code")

        # 路徑 B: 別名命中 (alias_norm)
        cur.execute("""
            SELECT e.entity_id, e.canonical_name, e.entity_kind, e.ticker_code,
                   'IS_TICKER' AS relation_type, 1.0 AS confidence
            FROM entity_aliases a
            JOIN entities e ON e.entity_id = a.entity_id
            WHERE a.alias_norm = %s AND e.entity_kind = 'TICKER'
            ORDER BY a.confidence DESC, a.source = 'manual' DESC
            LIMIT 1
        """, (norm,))
        row = cur.fetchone()
        if row:
            return _expand_from_ticker(cur, row, resolved_via="alias")

        # 路徑 C: canonical_name 含匹配 (trigram 模糊)
        cur.execute("""
            SELECT entity_id, canonical_name, entity_kind, ticker_code, 1.0 AS confidence,
                   similarity(canonical_name, %s) AS sim
            FROM entities
            WHERE entity_kind = 'TICKER'
              AND canonical_name %% %s
            ORDER BY sim DESC
            LIMIT 1
        """, (raw, raw))
        row = cur.fetchone()
        if row:
            row["relation_type"] = "IS_TICKER"
            return _expand_from_ticker(cur, row, resolved_via="canonical_name")

        # 路徑 D: 主題 / 概念命中 (CoWoS, AI伺服器, 電動車...)
        cur.execute("""
            SELECT e.entity_id, e.canonical_name, e.entity_kind, NULL AS ticker_code,
                   'HAS_THEME' AS relation_type, 1.0 AS confidence,
                   similarity(e.canonical_name, %s) AS sim
            FROM entities e
            WHERE e.entity_kind = 'THEME'
              AND e.canonical_name %% %s
            ORDER BY sim DESC
            LIMIT 1
        """, (raw, raw))
        row = cur.fetchone()
        if row:
            return _expand_from_theme(cur, row, resolved_via="concept")
    except psycopg2.errors.UndefinedFunction:
        # pg_trgm 不可用時退化路徑 C/D 為 ILIKE
        logger.warning("pg_trgm 不可用, fallback to ILIKE")
        return _resolve_ticker_ilike(raw, norm)
    finally:
        conn.close()

    return None


def _resolve_ticker_ilike(raw: str, norm: str) -> dict | None:
    """pg_trgm 不可用時的退化路徑 (向後相容用)。"""
    conn = psycopg2.connect(**DB_CONFIG)
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        if re.fullmatch(r"[0-9]{4,6}", raw):
            cur.execute("""
                SELECT e.entity_id, e.canonical_name, e.entity_kind, e.ticker_code,
                       'IS_TICKER' AS relation_type, 1.0 AS confidence
                FROM entities e WHERE e.entity_kind='TICKER' AND e.ticker_code=%s LIMIT 1
            """, (raw,))
            row = cur.fetchone()
            if row: return _expand_from_ticker(cur, row, resolved_via="ticker_code")

        cur.execute("""
            SELECT e.entity_id, e.canonical_name, e.entity_kind, e.ticker_code,
                   'IS_TICKER' AS relation_type, a.confidence
            FROM entity_aliases a JOIN entities e ON e.entity_id=a.entity_id
            WHERE a.alias_norm = %s AND e.entity_kind='TICKER' LIMIT 1
        """, (norm,))
        row = cur.fetchone()
        if row: return _expand_from_ticker(cur, row, resolved_via="alias")

        cur.execute("""
            SELECT entity_id, canonical_name, entity_kind, ticker_code, 1.0 AS confidence
            FROM entities WHERE entity_kind='TICKER' AND canonical_name ILIKE %s
            ORDER BY length(canonical_name) LIMIT 1
        """, (f"%{raw}%",))
        row = cur.fetchone()
        if row:
            row["relation_type"] = "IS_TICKER"
            return _expand_from_ticker(cur, row, resolved_via="canonical_name")

        cur.execute("""
            SELECT entity_id, canonical_name, entity_kind, NULL AS ticker_code,
                   'HAS_THEME' AS relation_type, 1.0 AS confidence
            FROM entities WHERE entity_kind='THEME' AND canonical_name ILIKE %s
            ORDER BY length(canonical_name) LIMIT 1
        """, (f"%{raw}%",))
        row = cur.fetchone()
        if row: return _expand_from_theme(cur, row, resolved_via="concept")
    finally:
        conn.close()
    return None


def _expand_from_ticker(cur, row: dict, resolved_via: str) -> dict:
    """從 IS_TICKER 命中展開: 找 ticker 的所有 entity (IS_TICKER / HAS_THEME / BELONGS_TO_*)"""
    ticker_code = row["ticker_code"]
    cur.execute("""
        SELECT m.relation_type, m.confidence, m.source,
               e.entity_id, e.canonical_name, e.entity_kind, e.industry
        FROM ticker_entity_map m
        JOIN entities e ON e.entity_id = m.entity_id
        WHERE m.ticker_code = %s
          AND (m.effective_to IS NULL OR m.effective_to >= CURRENT_DATE)
        ORDER BY m.relation_type, e.canonical_name
    """, (ticker_code,))
    rels = cur.fetchall()
    return {
        "ticker_code": ticker_code,
        "canonical_name": row["canonical_name"],
        "resolved_via": resolved_via,
        "entities": [
            {
                "entity_id": r["entity_id"],
                "canonical_name": r["canonical_name"],
                "entity_kind": r["entity_kind"],
                "relation_type": r["relation_type"],
                "confidence": float(r["confidence"]) if r["confidence"] is not None else 1.0,
            }
            for r in rels
        ],
    }


def _expand_from_theme(cur, row: dict, resolved_via: str) -> dict:
    """從 THEME 命中展開: 找出所有 HAS_THEME 該主題的 ticker (M:N)"""
    concept_entity_id = row["entity_id"]
    cur.execute("""
        SELECT m.ticker_code, m.confidence, m.relation_type,
               e_stock.entity_id AS stock_entity_id, e_stock.canonical_name AS stock_canonical_name
        FROM ticker_entity_map m
        JOIN entities e_stock ON e_stock.entity_id = (
            SELECT entity_id FROM entities
            WHERE entity_kind='TICKER' AND ticker_code=m.ticker_code LIMIT 1
        )
        WHERE m.entity_id = %s AND m.relation_type = 'HAS_THEME'
          AND (m.effective_to IS NULL OR m.effective_to >= CURRENT_DATE)
        ORDER BY m.ticker_code
    """, (concept_entity_id,))
    members = cur.fetchall()
    entities = [
        {
            "entity_id": row["entity_id"],
            "canonical_name": row["canonical_name"],
            "entity_kind": row["entity_kind"],
            "relation_type": "HAS_THEME",
            "confidence": 1.0,
        }
    ]
    entities.extend([
        {
            "entity_id": m["stock_entity_id"],
            "canonical_name": m["stock_canonical_name"],
            "entity_kind": "TICKER",
            "relation_type": "HAS_THEME_MEMBER",
            "confidence": float(m["confidence"]) if m["confidence"] is not None else 1.0,
        }
        for m in members
    ])
    # 主題命中沒有單一 ticker_code, 用概念名代表
    synthetic_code = f"@{row['canonical_name']}"
    return {
        "ticker_code": synthetic_code,
        "canonical_name": row["canonical_name"],
        "resolved_via": resolved_via,
        "entities": entities,
    }


# ===========================================================================
#  Step 2: 從 entity 集合查 news_entity_map, 算 relevance_score
# ===========================================================================

def _fetch_news_for_entities(
    cur,
    entity_ids: list[int],
    days: int,
    limit: int = 100,
) -> list[dict]:
    """JOIN news_entity_map + news_raw, 回傳每則新聞與其 evidence rows."""
    if not entity_ids:
        return []
    cur.execute("""
        WITH evidence AS (
            SELECT nem.news_id,
                   nem.entity_id,
                   nem.ticker_code,
                   nem.mention_type,
                   nem.confidence,
                   nem.raw_text_excerpt
            FROM news_entity_map nem
            WHERE nem.entity_id = ANY(%s)
        )
        SELECT nr.news_id, nr.title, nr.source, nr.url, nr.published_at, nr.summary,
               json_agg(json_build_object(
                   'entity_id', e.entity_id,
                   'ticker_code', e.ticker_code,
                   'mention_type', e.mention_type,
                   'confidence', e.confidence,
                   'raw_text_excerpt', e.raw_text_excerpt
               )) AS evidence
        FROM evidence e
        JOIN news_raw nr ON nr.news_id = e.news_id
        WHERE nr.published_at IS NULL
           OR nr.published_at >= CURRENT_TIMESTAMP - make_interval(days => %s)
        GROUP BY nr.news_id, nr.title, nr.source, nr.url, nr.published_at, nr.summary
        ORDER BY nr.published_at DESC NULLS LAST
        LIMIT %s
    """, (entity_ids, days, limit))
    return cur.fetchall()


def _score_news(news_row: dict, resolved_entities: list[dict]) -> tuple[float, list[dict]]:
    """計算一則新聞的 relevance_score 與 matched_entities 證據清單。

    公式: score = Σ(relation_weight × mention_weight × confidence) / norm
    norm 採 Σ 該則新聞 evidence 中 relation_weight 最大值 × mention_weight 最大值
    (即「最直觀」的命中視為滿分, 其他 evidence 為加分項), 上限 1.0。
    """
    # 建 entity_id → resolved relation/canonical 對照表
    by_id: dict[int, dict] = {e["entity_id"]: e for e in resolved_entities}

    raw_weighted = 0.0
    norm_weight = 0.0
    matched: dict[int, dict] = {}

    for ev in news_row.get("evidence", []):
        ent = by_id.get(ev["entity_id"])
        if not ent:
            continue
        rel_w = RELATION_WEIGHTS.get(ent["relation_type"], 0.3)
        men_w = MENTION_WEIGHTS.get(ev["mention_type"] or "SUMMARY", 0.6)
        conf = float(ev["confidence"]) if ev["confidence"] is not None else 1.0
        contrib = rel_w * men_w * conf
        raw_weighted += contrib
        # 用最大一次 (relation=IS_TICKER, mention=TITLE) 為滿分基準
        if rel_w * men_w > norm_weight:
            norm_weight = rel_w * men_w
        matched[ev["entity_id"]] = {
            "entity_id": ev["entity_id"],
            "canonical_name": ent["canonical_name"],
            "entity_kind": ent["entity_kind"],
            "relation_type": ent["relation_type"],
            "mention_type": ev["mention_type"],
            "confidence": conf,
            "raw_text_excerpt": ev.get("raw_text_excerpt"),
        }

    score = (raw_weighted / norm_weight) if norm_weight > 0 else 0.0
    score = min(1.0, max(0.0, round(score, 4)))
    return score, list(matched.values())


# ===========================================================================
#  Public API
# ===========================================================================

def find_news_for_ticker(
    query: str,
    days: int = 7,
    limit: int = 20,
    min_score: float = 0.0,
) -> TickerNewsResult | None:
    """主入口: 輸入 ticker / 概念 / 中文名, 輸出相關度新聞清單。"""
    if days <= 0:
        days = 7
    if limit <= 0:
        limit = 20

    resolved = resolve_ticker(query)
    if not resolved:
        return None

    entities = resolved["entities"]
    ticker_code = resolved["ticker_code"]
    entity_ids = [e["entity_id"] for e in entities]

    conn = psycopg2.connect(**DB_CONFIG)
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        rows = _fetch_news_for_entities(cur, entity_ids, days, limit=limit * 4)
    finally:
        conn.close()

    scored: list[ScoredNews] = []
    for row in rows:
        score, matched = _score_news(row, entities)
        if score < min_score:
            continue
        scored.append(ScoredNews(
            news_id=row["news_id"],
            title=row["title"],
            source=row["source"],
            url=row["url"],
            published_at=row["published_at"].isoformat() if row.get("published_at") else None,
            summary=row.get("summary"),
            relevance_score=score,
            matched_entities=matched,
        ))

    # 排序: relevance_score desc → published_at desc
    scored.sort(key=lambda n: (n.relevance_score, n.published_at or ""), reverse=True)
    scored = scored[:limit]

    return TickerNewsResult(
        ticker_code=ticker_code,
        canonical_name=resolved["canonical_name"],
        resolved_via=resolved["resolved_via"],
        resolution_details={"query": query, "entity_count": len(entities)},
        days=days,
        total_news=len(scored),
        news=scored,
        entity_path=entities,
    )


def search_by_entity_name(entity_name: str, days: int = 7, limit: int = 20) -> TickerNewsResult | None:
    """Alias 給既有 semantic_search 的入口, 名稱更語意化一點。"""
    return find_news_for_ticker(entity_name, days=days, limit=limit)


def format_result_text(result: TickerNewsResult) -> str:
    """人類可讀的摘要文字, 給 CLI 或 Telegram bot 用。"""
    lines = [
        f"📰 {result.canonical_name or result.ticker_code} ({result.ticker_code}) "
        f"— 近 {result.days} 天共 {result.total_news} 則相關新聞",
        f"   (解析路徑: {result.resolved_via}, 展開 {len(result.entity_path)} 個 entity)\n",
    ]
    if not result.news:
        lines.append("   (尚無被 entity 標註的新聞 — 需先跑 LLM 抽取)")
        return "\n".join(lines)

    for i, n in enumerate(result.news, 1):
        date_str = (n.published_at or "")[:10] or "-"
        top_rel = (n.matched_entities[0]["relation_type"] if n.matched_entities else "-")
        top_match = (n.matched_entities[0]["canonical_name"] if n.matched_entities else "-")
        lines.append(
            f"  {i:>2}. [{date_str}] score={n.relevance_score:.2f} {n.source} | "
            f"{top_rel}→{top_match}\n"
            f"     {n.title}"
        )
    return "\n".join(lines)


# ===========================================================================
#  CLI
# ===========================================================================

def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m src.services.news_relation",
        description="輸入 ticker / 概念股 / 別名, 回傳近 N 天關聯新聞 (含相關度分數)。",
    )
    p.add_argument("--ticker", required=True, help="ticker code (2330) / 中文名 (台積電) / 別名 (TSMC)")
    p.add_argument("--days", type=int, default=7, help="回溯天數 (預設 7)")
    p.add_argument("--limit", type=int, default=20, help="最多回傳幾則 (預設 20)")
    p.add_argument("--min-score", type=float, default=0.0, help="過濾掉低於此分數的新聞 (0~1)")
    p.add_argument("--format", choices=("text", "json"), default="text")
    return p


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _build_arg_parser().parse_args(argv)
    try:
        result = find_news_for_ticker(args.ticker, days=args.days, limit=args.limit, min_score=args.min_score)
    except Exception as e:
        logger.error(f"查詢失敗: {e}")
        return 1
    if not result:
        print(f"❌ 找不到與「{args.ticker}」對應的 ticker / entity", file=sys.stderr)
        return 2
    if args.format == "json":
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(format_result_text(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
