# Telegram Bot 雙向互動功能 — Implementation Plan

## Problem Statement
目前 Telegram 只有單向推播（警示/盤後分析），需加入雙向互動：透過 Inline Keyboard 按鈕選單操作系統功能，並將群組討論存檔供前端查看。

## Requirements
1. Telegram Group 中以 Inline Keyboard 方式操作
2. 支援：查股票（MA圖+加自選+AI分析）、自選股（含備註）、掃描（多條件組合）、持股分析、新聞（有資料才顯示按鈕）
3. `@Bot 問題` 觸發 AI 語意搜尋
4. 一般訊息存檔為討論紀錄
5. 前端新增獨立「💬 群組討論」浮動面板

## Background
- 已有 `send_telegram_message()` 函數
- 已有完整的 service 層：realtime_quote、kline_analysis、market_scan、portfolio_advisor、semantic_search、finance_news
- K 線資料存在 PostgreSQL，MA 計算邏輯已有
- Server 用 FastAPI + asyncio background tasks
- 依賴需新增：matplotlib==3.9.0

## 設計

### 主選單按鈕
```
[📊 查股票] [📋 自選股]
[📡 掃描]   [💼 持股分析]
[📰 新聞]   [❓ 幫助]
```

### 各功能流程
- 📊 查股票：輸入代號 → MA 均線圖（matplotlib, 只有 MA 線無 K 線蠟燭）→ 按鈕 [➕ 加自選] [🤖 AI分析]
- 📋 自選股：分組按鈕 → 股票列表 → [📝 備註] [📊 看圖] [🤖 AI分析]
- 📡 掃描：二級條件分類按鈕 → 三級多選選項（✅ 標記）→ 可組合多條件 → [🔍 執行掃描]
  - 條件：RSI / 均線排列 / 站上MA / 跌破MA / MA方向 / MA交叉 / 量比 / 量趨勢 / 漲跌% / 漲幅排名 / 產業 / 市場
- 💼 持股分析：直接回傳 AI 持股分析
- 📰 新聞：二級按鈕有資料才顯示 [📋 每日財經熱門話題] [🤖 AI盤後分析]
- @Bot 問題：AI 語意搜尋回覆
- 一般訊息：存檔到 telegram_discussions 表（不回覆）

### 前端
新增「💬 群組討論」浮動面板

---

## Task Breakdown

### Task 1: 建立 Telegram Bot long polling 基礎架構
- 新增 `src/services/telegram_bot.py`，包含 `getUpdates` loop、message dispatch、`answerCallbackQuery`
- 在 `server.py` startup 中啟動 `asyncio.create_task(_telegram_bot_polling())`
- 封裝 `sendMessage` with `reply_markup`（InlineKeyboardMarkup）、`sendPhoto`
- Demo：Bot 上線，群組發訊息可在 log 看到

### Task 2: 實作主選單與按鈕路由
- `/menu` 或 `/start` 發出主選單
- callback_data 路由設計（menu、stock、watchlist、scan、portfolio、news、help）
- Demo：點按鈕 Bot 正確回應各功能入口

### Task 3: 📊 查股票 — MA 均線圖 + 加自選 + AI分析
- 新增 `src/services/chart_service.py`，用 matplotlib 畫收盤價 + MA5/10/20/60 線圖，輸出 PNG bytes
- Telegram `sendPhoto` API
- 「➕ 加自選」callback → `add_watchlist`
- 「🤖 AI分析」callback → `analyze_kline` 回傳文字
- 需新增依賴：matplotlib==3.9.0 到 requirements.txt
- Demo：查 2330 收到 MA 圖；點加自選/AI分析成功

### Task 4: 📋 自選股 — 分組 + 備註
- 讀取 `get_all_groups` + `get_watchlist` 產生分組按鈕
- 點個股 → [📝 備註] [📊 看圖] [🤖 AI分析]
- 「📝 備註」→ 顯示現有備註 + 等待下一則訊息作為新備註 → `add_note`
- Demo：完整操作流程跑通

### Task 5: 📡 掃描 — 多條件組合
- 二級按鈕：條件分類
- 三級：該分類下選項（多選，選中加 ✅）
- per-user state dict 暫存已選條件
- 「🔍 執行掃描」→ 組裝 params 呼叫 market_scan 邏輯 → 回傳結果
- Demo：多條件組合掃描完整

### Task 6: 💼 持股分析 + 📰 新聞
- 💼 → `analyze_portfolio` → 回傳文字
- 📰 → 查 DB 當天是否有資料 → 有的才顯示按鈕
- 「📋 每日財經熱門話題」→ 新聞標題列表
- 「🤖 AI盤後分析」→ 分析內容
- Demo：有資料時按鈕出現、無資料不顯示

### Task 7: @Bot 語意搜尋 + 一般訊息存檔
- mention Bot → `ask_news` → 回傳結果
- 其他訊息 → 存入 `telegram_discussions` 表
- DB schema: `CREATE TABLE telegram_discussions (id SERIAL PRIMARY KEY, user_name TEXT, user_id BIGINT, message TEXT, created_at TIMESTAMP DEFAULT NOW())`
- Demo：@Bot 問問題有回覆；DB 有紀錄

### Task 8: 前端「💬 群組討論」浮動面板
- 新增 API：`GET /api/discussions` 回傳討論紀錄（分頁）
- 前端浮動面板（類似 float-notes 樣式）
- 按時間倒序：使用者名稱 + 時間 + 訊息內容
- Header 加「💬」按鈕切換
- Demo：前端面板顯示群組討論紀錄

### Task 9: 測試整合與 push
- 完整測試所有功能
- Push to git

---

## 技術細節

### callback_data 命名規則
- `menu` — 返回主選單
- `stock` — 查股票入口
- `stock_add_{code}` — 加入自選
- `stock_ai_{code}` — AI分析
- `wl` — 自選股入口
- `wl_group_{name}` — 點選分組
- `wl_stock_{code}` — 點選個股
- `wl_note_{code}` — 查看/新增備註
- `wl_chart_{code}` — 看 MA 圖
- `wl_ai_{code}` — AI 分析
- `scan` — 掃描入口
- `scan_cat_{category}` — 選條件分類
- `scan_opt_{category}_{value}` — 選/取消選項
- `scan_run` — 執行掃描
- `scan_clear` — 清除條件
- `portfolio` — 持股分析
- `news` — 新聞入口
- `news_titles` — 每日財經熱門話題
- `news_ai` — AI盤後分析
- `help` — 幫助

### 使用者狀態管理
```python
# Per-user state for multi-step interactions
user_states = {}
# Example: user_states[user_id] = {"action": "waiting_stock_code"}
# Example: user_states[user_id] = {"action": "waiting_note", "stock_code": "2330"}
# Example: user_states[user_id] = {"action": "scan", "conditions": {"rsi_max": 30, "ma_arrangement": "多頭排列"}}
```

### 新增檔案
- `src/services/telegram_bot.py` — Bot 核心邏輯
- `src/services/chart_service.py` — MA 均線圖生成

### 修改檔案
- `src/server.py` — startup 加入 bot polling task + 新增 /api/discussions API + 新增 /api/telegram/settings、/api/telegram/test API
- `src/core/database.py` — 新增 telegram_discussions 表 CRUD
- `src/templates/index.html` — 新增浮動面板 + 設定面板 Telegram 區塊（Bot Token / Chat ID 輸入 + 測試按鈕）
- `requirements.txt` — 新增 matplotlib==3.9.0

### Telegram Bot 設定
- Bot Token / Chat ID 改為 WEB 設定頁面填寫（個人使用模式）
- 儲存於 `alert_settings` 表（per user_id）
- `telegram_bot.py` 透過 `_ensure_config()` lazy-load from DB
- 保留環境變數 fallback（向後相容）
