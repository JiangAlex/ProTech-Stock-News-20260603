-- Migration: ticker ↔ entity ↔ news mapping schema
-- Redmine: #41 (方向 2-2) — semantic_search.py 串接新聞與個股關聯
-- Source of truth: docs/projects/ProTech-Stock-News/schema/ticker-entity-mapping.md
-- Author: RD (kanban t_04a02caa)
-- Date:   2026-09-10
--
-- 範圍:
--   * entities              統一表: 公司/集團/主題/產業/個股 (kind 區分)
--   * entity_aliases        別名/同義詞 (含 LLM 抽取的來源)
--   * ticker_entity_map     ticker ↔ entity 多對多 + 關係類型
--   * news_raw              新聞原始快取 (RSS / 鉅亨 / Yahoo 全進這裡)
--   * news_entity_map       新聞 ↔ entity 多對多
--
-- 不重建 stock_basic / sectors / stock_concepts / watchlist_notes,
-- 用 FK / VIEW 整合 (見 schema doc §1)。

BEGIN;

CREATE EXTENSION IF NOT EXISTS pg_trgm;

------------------------------------------------------------------------
-- 1. entities
------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS entities (
    entity_id      SERIAL PRIMARY KEY,
    entity_kind    TEXT NOT NULL,                 -- 'TICKER' | 'COMPANY' | 'GROUP' | 'THEME' | 'INDUSTRY' | 'PERSON'
    canonical_name TEXT NOT NULL,
    ticker_code    TEXT,                          -- 僅 TICKER kind, 對應 stock_basic.stock_code
    industry       TEXT,                          -- 主分類 (與 stock_basic.industry 同 vocabulary)
    metadata       JSONB DEFAULT '{}'::jsonb,
    created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_entity UNIQUE (entity_kind, canonical_name),
    CONSTRAINT chk_ticker CHECK (
        (entity_kind <> 'TICKER') OR (ticker_code IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_entities_kind   ON entities (entity_kind);
CREATE INDEX IF NOT EXISTS idx_entities_ticker ON entities (ticker_code) WHERE ticker_code IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_entities_name_trgm ON entities USING gin (canonical_name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_entities_metadata_gin ON entities USING gin (metadata);

------------------------------------------------------------------------
-- 2. entity_aliases
------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS entity_aliases (
    alias_id     SERIAL PRIMARY KEY,
    entity_id    INTEGER NOT NULL REFERENCES entities(entity_id) ON DELETE CASCADE,
    alias_text   TEXT NOT NULL,
    alias_norm   TEXT NOT NULL,                   -- 正規化 (lowercase, 去空白/標點)
    source       TEXT DEFAULT 'manual',           -- 'manual' | 'llm' | 'import'
    confidence   NUMERIC(3,2) DEFAULT 1.0,
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_alias UNIQUE (alias_norm)
);

CREATE INDEX IF NOT EXISTS idx_alias_norm_trgm ON entity_aliases USING gin (alias_norm gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_alias_entity ON entity_aliases (entity_id);

------------------------------------------------------------------------
-- 3. ticker_entity_map
------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ticker_entity_map (
    map_id         SERIAL PRIMARY KEY,
    ticker_code    TEXT NOT NULL,                 -- stock_basic.stock_code
    entity_id      INTEGER NOT NULL REFERENCES entities(entity_id) ON DELETE CASCADE,
    relation_type  TEXT NOT NULL,
    confidence     NUMERIC(3,2) DEFAULT 1.0,
    source         TEXT DEFAULT 'manual',         -- 'manual' | 'llm' | 'import' | 'concept_table'
    effective_from DATE DEFAULT CURRENT_DATE,
    effective_to   DATE,
    created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_ticker_entity UNIQUE (ticker_code, entity_id, relation_type, effective_from)
);

CREATE INDEX IF NOT EXISTS idx_tem_ticker ON ticker_entity_map (ticker_code);
CREATE INDEX IF NOT EXISTS idx_tem_entity ON ticker_entity_map (entity_id);
CREATE INDEX IF NOT EXISTS idx_tem_active ON ticker_entity_map (ticker_code, effective_to) WHERE effective_to IS NULL;

------------------------------------------------------------------------
-- 4. news_raw
------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS news_raw (
    news_id      TEXT PRIMARY KEY,                 -- source + url_hash 或原生 ID
    source       TEXT NOT NULL,                   -- 'cnyes_headline' / 'moneydj_rss' / ...
    url          TEXT NOT NULL,
    title        TEXT NOT NULL,
    summary      TEXT,                            -- RSS description; NULL = 需後續抓全文
    published_at TIMESTAMP,
    fetched_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    raw_payload  JSONB DEFAULT '{}'::jsonb,
    UNIQUE (source, url)
);

CREATE INDEX IF NOT EXISTS idx_news_pub      ON news_raw (published_at DESC);
CREATE INDEX IF NOT EXISTS idx_news_src_pub  ON news_raw (source, published_at DESC);

------------------------------------------------------------------------
-- 5. news_entity_map
------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS news_entity_map (
    map_id             SERIAL PRIMARY KEY,
    news_id            TEXT NOT NULL REFERENCES news_raw(news_id) ON DELETE CASCADE,
    entity_id          INTEGER NOT NULL REFERENCES entities(entity_id) ON DELETE CASCADE,
    ticker_code        TEXT,                      -- denorm: 加速 ticker 熱度查詢
    mention_type       TEXT,                      -- 'TITLE' | 'SUMMARY' | 'BODY'
    confidence         NUMERIC(3,2) DEFAULT 1.0,
    extracted_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    extractor          TEXT,                      -- 'llm:gpt-4o-mini' / 'manual' / 'rule'
    raw_text_excerpt   TEXT,                      -- 抽取原文 100 字片段供審核
    CONSTRAINT uq_news_entity UNIQUE (news_id, entity_id, mention_type)
);

CREATE INDEX IF NOT EXISTS idx_nem_news      ON news_entity_map (news_id);
CREATE INDEX IF NOT EXISTS idx_nem_ticker    ON news_entity_map (ticker_code) WHERE ticker_code IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_nem_entity    ON news_entity_map (entity_id);
CREATE INDEX IF NOT EXISTS idx_nem_extracted ON news_entity_map (extracted_at);

------------------------------------------------------------------------
-- 6. 種子回填 (從既有 stock_basic / stock_concepts)
--    - 為每檔 stock_basic 建一個 TICKER entity
--    - 為每個 DISTINCT concept_name 建一個 THEME entity
--    - 同步 stock_concepts 對應關係 (HAS_THEME)
--    - 為每個 DISTINCT industry 建一個 INDUSTRY entity
------------------------------------------------------------------------

-- 6.1 TICKER entities from stock_basic (column is stock_name, not name)
INSERT INTO entities (entity_kind, canonical_name, ticker_code, industry)
SELECT 'TICKER', stock_name, stock_code, industry
FROM stock_basic
ON CONFLICT (entity_kind, canonical_name) DO NOTHING;

-- 6.2 alias: ticker code ↔ entity (the most fundamental form)
INSERT INTO entity_aliases (entity_id, alias_text, alias_norm, source, confidence)
SELECT entity_id, ticker_code, lower(ticker_code), 'import', 1.0
FROM entities WHERE entity_kind = 'TICKER' AND ticker_code IS NOT NULL
ON CONFLICT (alias_norm) DO NOTHING;

-- 6.3 INDUSTRY entities
INSERT INTO entities (entity_kind, canonical_name)
SELECT DISTINCT 'INDUSTRY', industry
FROM stock_basic
WHERE industry IS NOT NULL AND industry <> ''
ON CONFLICT (entity_kind, canonical_name) DO NOTHING;

-- 6.4 BELONGS_TO_INDUSTRY map (ticker → industry)
INSERT INTO ticker_entity_map (ticker_code, entity_id, relation_type, source)
SELECT sb.stock_code, e.entity_id, 'BELONGS_TO_INDUSTRY', 'concept_table'
FROM stock_basic sb
JOIN entities e ON e.entity_kind = 'INDUSTRY' AND e.canonical_name = sb.industry
WHERE sb.industry IS NOT NULL AND sb.industry <> ''
ON CONFLICT (ticker_code, entity_id, relation_type, effective_from) DO NOTHING;

-- 6.5 THEME entities from stock_concepts
INSERT INTO entities (entity_kind, canonical_name)
SELECT DISTINCT 'THEME', concept_name
FROM stock_concepts
WHERE concept_name IS NOT NULL AND concept_name <> ''
ON CONFLICT (entity_kind, canonical_name) DO NOTHING;

-- 6.6 HAS_THEME map
INSERT INTO ticker_entity_map (ticker_code, entity_id, relation_type, source)
SELECT sc.stock_code, e.entity_id, 'HAS_THEME', 'concept_table'
FROM stock_concepts sc
JOIN entities e ON e.entity_kind = 'THEME' AND e.canonical_name = sc.concept_name
WHERE sc.concept_name IS NOT NULL AND sc.concept_name <> ''
ON CONFLICT (ticker_code, entity_id, relation_type, effective_from) DO NOTHING;

-- 6.7 IS_TICKER self-link (ticker_code itself is the primary entity)
INSERT INTO ticker_entity_map (ticker_code, entity_id, relation_type, source)
SELECT e.ticker_code, e.entity_id, 'IS_TICKER', 'manual'
FROM entities e
WHERE e.entity_kind = 'TICKER' AND e.ticker_code IS NOT NULL
ON CONFLICT (ticker_code, entity_id, relation_type, effective_from) DO NOTHING;

COMMIT;
