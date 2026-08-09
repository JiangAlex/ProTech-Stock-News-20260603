# Memory — 待排查 & 追蹤事項

## 2026-08-09：TWII 盤中 60 分線預測未產生結果

### 現象
- Production server (blog.softsnail.com:8020) 有在執行
- 17:05 daily integration 正常（有產出 TWII prediction）
- 但 `_today_stats` 在 17:05 時為 0 → 盤中 predict_next_bar() 從未成功
- `key_reasoning` 全部是 "60min accuracy=0.0% | daily integration"

### 已部署的診斷工具
- `GET /api/twii-intraday/status` — 查看盤中系統狀態
- `on_tick` 加入詳細 log（commit 25f38f1）

### 排查步驟（週一 2026-08-11 交易日）
1. 部署後立刻查 `/api/twii-intraday/status`：
   - `minimax_api_key_set` 是否 true
   - `kline_history_dates` 是否有 5+ 天資料
   - `recent_bars_count` 是否 >= 5
2. 週一 10:00 後再查一次：
   - `today_bars_count` 應 > 0（代表 bar 有累積）
   - `predictions_today` 應有記錄（代表 predict 成功）
   - Telegram 是否收到盤中推播
3. 如果 predict 失敗，查 server log 看：
   - `"Insufficient bars for prediction"` → kline 歷史不夠
   - `"MINIMAX_API_KEY not set"` → 環境變數問題
   - `"TWII AI prediction failed"` → API 呼叫錯誤（timeout/rate limit）
   - `"no JSON in response"` → AI 回覆格式異常

### 可能原因（待確認）
1. MiniMax API 盤中呼叫 timeout（盤後 17:05 成功可能因壓力低）
2. `get_recent_bars()` 返回 bars 不足（Yahoo init 失敗或 data file 路徑問題）
3. `_compute_60min_indicators()` 計算錯誤導致提前 return

### 相關 commits
- `c2dfa87` — 盤中 60 分線預測即時推播 Telegram
- `a6f072d` — /api/twii-intraday/status 診斷 endpoint
- `25f38f1` — on_tick 詳細 log

---

## 2026-08-09：每日 AI 回饋學習排程（新功能）

### 說明
- 新增 `_daily_ai_feedback_job`：每日 17:10 自動對所有啟用 ai_feedback alert 的個股做 AI 分析
- commit `73f1340`

### 確認事項（週一 2026-08-11）
- 17:10 後檢查 `/api/predictions` 是否有新增 2353、2354 的記錄
- 確認 source 為 "kline_analysis"、prediction_date 為當天
