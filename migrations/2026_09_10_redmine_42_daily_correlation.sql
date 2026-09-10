-- Migration: 每日新聞關聯排程 (cron) — 配套表 (Redmine #42 / 方向 2-3)
-- 對應 source of truth: docs/projects/ProTech-Stock-News/cron/daily-news-correlation-SOP.md
-- 在 #41 (entities / entity_aliases / ticker_entity_map / news_raw / news_entity_map)
-- 之上新增兩個 lightweight 表:
--   1. cron_run_log: SOP-grade observability, 每一輪 cron 跑一次寫入一筆
--      (記錄 run_id / start/end / status / stats / error, 與 main server 的
--      asyncio background task 模式互補 — 此處是 daily cron, 跨主機也能寫)
--   2. news_correlation_daily: 每日跑完後, 對每檔被新聞提及的 ticker 寫一筆
--      「今日相關新聞彙總」, 給 #43 (近 N 天熱度 API) 與方向 1 (#44) 直接取用
--
-- 此 migration 不修改 #41 的五張表, 完全向下相容。回滾指令見尾段。

BEGIN;

------------------------------------------------------------------------
-- 1. cron_run_log — 跨 profile / 跨主機的 cron 觀察表
------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS cron_run_log (
    run_id           BIGSERIAL PRIMARY KEY,
    job_name         TEXT NOT NULL,                -- 'daily_news_correlation'
    started_at       TIMESTAMP NOT NULL,
    ended_at         TIMESTAMP,
    status           TEXT NOT NULL                 -- 'running' | 'success' | 'failure' | 'skipped'
        CHECK (status IN ('running', 'success', 'failure', 'skipped')),
    trigger_source   TEXT DEFAULT 'cron',          -- 'cron' | 'systemd' | 'manual'
    stats            JSONB DEFAULT '{}'::jsonb,    -- {news_pulled, news_inserted, entities_extracted, tickers_correlated, telegram_sent}
    error            TEXT,                         -- exception 摘要 (含 traceback 第一行)
    last_step        TEXT,                         -- 跑到哪一步: 'fetch' | 'extract' | 'correlate' | 'persist' | 'notify'
    attempts         INTEGER DEFAULT 1,            -- 第幾次重試
    host             TEXT                          -- hostname / container id, 方便多機部署時觀察
);
CREATE INDEX IF NOT EXISTS idx_cron_job_started   ON cron_run_log (job_name, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_cron_status        ON cron_run_log (status, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_cron_host          ON cron_run_log (host, started_at DESC);

------------------------------------------------------------------------
-- 2. news_correlation_daily — 每日 ticker × 相關新聞彙總
--    Key = (correlation_date, ticker_code), partial unique (可能一個 ticker 一天多次寫入,
--    但業務上只留最新一筆 — 用 ON CONFLICT DO UPDATE)
--    用途:
--      - #43 近 N 天熱度查詢直接 SELECT SUM(...) GROUP BY ticker
--      - 給 Telegram 每日推送一個「今日熱門 ticker」一覽表
--      - #44 方向 1 對接: 把這張表 join trades 看「新聞 → 隔日漲跌」
------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS news_correlation_daily (
    correlation_date  DATE NOT NULL,
    ticker_code       TEXT NOT NULL,
    canonical_name    TEXT,
    resolved_via      TEXT,                        -- 'ticker_code' | 'alias' | 'canonical_name' | 'concept'
    total_news        INTEGER DEFAULT 0,
    top_score         NUMERIC(4,3),                -- 最高 relevance_score
    avg_score         NUMERIC(4,3),                -- 平均 relevance_score
    headline_titles   JSONB DEFAULT '[]'::jsonb,   -- top 5 title JSON array, 給 Telegram / Web 直接用
    matched_entities  JSONB DEFAULT '[]'::jsonb,   -- 含 entity_kind / relation_type 證據
    computed_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (correlation_date, ticker_code)
);
CREATE INDEX IF NOT EXISTS idx_correlation_date    ON news_correlation_daily (correlation_date DESC);
CREATE INDEX IF NOT EXISTS idx_correlation_ticker  ON news_correlation_daily (ticker_code, correlation_date DESC);
CREATE INDEX IF NOT EXISTS idx_correlation_total   ON news_correlation_daily (correlation_date, total_news DESC);

------------------------------------------------------------------------
-- 3. SELECT 視圖 — 給 #43「近 N 天熱度」直接用
------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_news_correlation_recent AS
SELECT correlation_date,
       ticker_code,
       canonical_name,
       total_news,
       top_score,
       avg_score,
       headline_titles
FROM news_correlation_daily
WHERE correlation_date >= CURRENT_DATE - INTERVAL '30 days';

COMMIT;

-- ============================================================================
-- ROLLBACK 範本 (down-migration, 不會自動跑, 留給 ops):
--   DROP VIEW IF EXISTS v_news_correlation_recent;
--   DROP TABLE IF EXISTS news_correlation_daily;
--   DROP TABLE IF EXISTS cron_run_log;
-- ============================================================================
