# 每日新聞關聯排程 — 失敗通知 SOP

**對應 Redmine**: #42 / 方向 2-3
**對應排程**: `daily_news_correlation`
**作者**: RD (kanban t_d3a2cf0d)
**最後更新**: 2026-09-10

本文件規範 `daily_news_correlation` cron 失敗時的偵測、通報與復原流程,
與公司現有 cron pipeline (`office-status-sync`、`warehouse-stock-alert` 等)
採取**一致**的失敗處理策略。

---

## 1. 失敗偵測層級

| 層級 | 條件 | 工具 | 寫入位置 |
| --- | --- | --- | --- |
| L1 即時 | cron 主流程 exception | `_notify_admin_error()` 直接推 Telegram | Telegram ADMIN_CHAT_ID |
| L1 即時 | LLM 抽取某則失敗 | `_logger.warning(...)` 記錄後繼續 | `logs/daily_news_correlation.log` |
| L2 觀察 | 整輪 status='failure' | `cron_run_log` 表 | PostgreSQL `cron_run_log` 表 |
| L3 監控 | 超過 1 輪沒成功 | `stale_task_scanner` (CEO cron) | Telegram 紅字 |

---

## 2. L1 即時通知: `_notify_admin_error()`

定義於 `src/services/daily_news_correlation_cron.py`,
呼叫 `src.services.telegram_service.send_telegram_message`
推送到 `DAILY_NEWS_CORRELATION_ADMIN_CHAT_ID` (預設 = `TELEGRAM_CHAT_ID`)。

訊息格式:

```
🚨 [daily_news_correlation] 失敗
時間: 2026-09-10 18:01:23
步驟: extract
訊息: OpenRouter 503: upstream unavailable
──────────────────
完整 traceback 見 logs/daily_news_correlation.log
或 psql: SELECT * FROM cron_run_log WHERE status='failure' ORDER BY started_at DESC LIMIT 1;
```

訊息**強制帶主機 hostname**,方便多機部署時定位是哪一台出包。

---

## 3. L2 觀察: `cron_run_log` 表

每次 cron 啟動寫一筆 `status='running'`,
結束寫 `success` / `failure` / `skipped`。
失敗時 `error` 欄位含完整 traceback 第一行,
`last_step` 欄位標示是哪個步驟炸 (`fetch` / `extract` / `correlate` / `persist` / `notify`)。

快速查詢:

```sql
-- 看最近 7 天失敗紀錄
SELECT started_at, last_step, substring(error from 1 for 200)
FROM cron_run_log
WHERE job_name = 'daily_news_correlation' AND status = 'failure'
  AND started_at > NOW() - INTERVAL '7 days'
ORDER BY started_at DESC;

-- 看今日跑了幾輪 + 狀態分佈
SELECT status, count(*) FROM cron_run_log
WHERE job_name = 'daily_news_correlation'
  AND started_at::date = CURRENT_DATE
GROUP BY status;
```

---

## 4. L3 監控: stale_task_scanner

CEO 已有 `stale_task_scanner` cron (`*/30 * * * *`)
掃所有 running 任務是否卡住。
本 cron 寫入 `cron_run_log` 之後,
scanner 可加一條規則 (見 follow-up ticket):

> 若 `cron_run_log` 最新一筆 `daily_news_correlation` 距今 > 26 小時
> 且 status IN ('running', 'skipped'), 發 Telegram 紅字給 CEO。

(此規則不在本次 scope, 留待 #44 對接時一併加; 本 SOP 先記規格。)

---

## 5. 復原 SOP (Runbook)

### 5.1 個別新聞 LLM 抽取失敗
**症狀**: log 出現 `[extract] <news_id> 失敗: ...`。
**處理**: 自動跳過該則, **不中斷整輪**。手動不需要介入。
**若大量失敗** (>50%): 查 `OPENROUTER_API_KEY` 是否過期或 quota 用完。

### 5.2 整輪 fetch 失敗
**症狀**: log 出現 `[fetch] 0 則`, Telegram 收到紅字, `cron_run_log.status='failure'`。
**處理**:
1. 先看 `cron_run_log.error` 第一行, 確認是哪個 source 炸
2. 確認 RSS URL 還活著 (`curl -I <url>`)
3. 若單一 RSS 持續失敗, 從 `SOURCES` list 暫時移除 (見 §6)
4. 重跑: `python -m src.services.daily_news_correlation_cron --trigger manual`

### 5.3 整輪 persist 失敗 (DB 寫入)
**症狀**: `cron_run_log.status='failure'` 且 `last_step IN ('raw', 'correlate', 'persist')`。
**處理**:
1. 確認 DB 連線: `PGHOST/PGPORT/PGUSER/PGPASSWORD` 是否仍正確
2. 確認 migration 有跑: `psql -c "\d news_raw"`、`\d cron_run_log`
3. 若 migration 沒跑: `psql -f migrations/2026_09_10_redmine_41_create_entity_news_schema.sql` 然後
   `psql -f migrations/2026_09_10_redmine_42_daily_correlation.sql`
4. 重跑: `python -m src.services.daily_news_correlation_cron --trigger manual`

### 5.4 Telegram 推送失敗
**症狀**: `stats.telegram_sent=false`, 但 `cron_run_log.status='success'`。
**處理**: 屬於「pipeline 完成, 只是通知失敗」, **不視為 cron 失敗**。
但若連續 3 天推送失敗, 視為 Telegram bot 設定異常, 改發 CEO 通知。

---

## 6. 設定與調整

### 6.1 暫時移除壞掉的 RSS source
編輯 `src/services/daily_news_correlation_cron.py` 的 `SOURCES` list, 註解掉壞掉的 URL。
**注意**: 不要直接刪掉 (這樣未來復原困難), 用 `# 暫時停用 2026-09-XX 起 <原因>`
的格式註解, 同時在本 SOP §7「變更紀錄」加一筆。

### 6.2 調整排程時間
編輯 `~/.hermes/profiles/rd/cron/jobs.json` 內 `daily_news_correlation` 的 `schedule.expr`,
預設 `0 18 * * *` (每日 18:00 跑)。變更後重新觸發 dispatcher:

```bash
hermes cron reload
```

或重啟 hermes daemon。

### 6.3 啟用 / 停用整個 cron
設 `enabled=false` 即可停用, 不用刪除 jobs.json 條目。

---

## 7. 變更紀錄

| 日期 | 變更 | 原因 | 變更者 |
| --- | --- | --- | --- |
| 2026-09-10 | 初版建立 | Redmine #42 上線 | RD (t_d3a2cf0d) |
