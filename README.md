# ProTech-Stock-News-20260603

個股技術分析 Dashboard — K 線圖（日/週/月）+ MA/VOL/MACD 指標 + 基本面圖表 + 自選股。

## 功能

- **K 線圖**：日線/週線/月線切換，天數可調
- **技術指標**：MA 均線（最多 5 條自訂）、VOL/MACD 子圖互斥切換、MA 扣抵標記
- **基本面**：年度營收走勢折線圖、歷年股利柱狀圖
- **自選股**：加入/移除、分組管理、▲▼ 快速切換
- **備註**：獨立浮動視窗，文字+圖檔+截圖，可拖曳
- **警示通知**：多條件（股價/漲跌幅/MA交叉/爆量）、toast+Browser+Telegram 通知
- **回測**：多條件策略模擬、K 線標記買賣點、損益統計
- **美股指數**：道瓊/那斯達克/費城半導體 K 線圖
- **社群爆紅榜**：Yahoo 最多瀏覽/瀏覽激增/熱門搜尋
- **漲跌幅排行**：上市/上櫃 漲幅排行、跌幅排行
- **個股新聞**：外連 Yahoo News 搜尋
- **每日財經新聞**：每日 PM 5:00 自動抓取鉅亨網/Yahoo奇摩股市/CMoney 熱門即時新聞&產業分析 10 條 → Telegram 推播 + AI 盤後分析
- **Telegram Bot**：雙向互動（查股票/自選股/掃描/持股分析/新聞），WEB 設定頁面填寫 Bot Token + Chat ID
- **群組討論面板**：Telegram 群組訊息存檔，WEB 端即時查看討論紀錄

## 快速啟動

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

python -m src.server
# 瀏覽器開啟 http://localhost:8020
```

## API

| Method | Path | 說明 |
|--------|------|------|
| GET | `/api/stocks` | 股票清單 |
| GET | `/api/stock/{code}/kline?period=&days=` | K 線資料 |
| GET | `/api/stock/{code}/revenue` | 年度營收 |
| GET | `/api/stock/{code}/dividend` | 歷年股利 |
| GET | `/api/hot-stocks?type=` | 社群爆紅榜 |
| GET | `/api/rank?direction=&market=` | 漲跌幅排行 |
| GET | `/api/watchlist` | 自選股清單 |
| POST | `/api/watchlist/{code}?name=` | 加入自選 |
| DELETE | `/api/watchlist/{code}` | 移除自選 |
| GET | `/api/alerts` | 警示條件列表 |
| POST | `/api/alerts` | 新增警示 |
| PUT | `/api/alerts/{id}` | 修改警示 |
| DELETE | `/api/alerts/{id}` | 刪除警示 |
| GET | `/api/alerts/settings` | 警示全域設定 |
| PUT | `/api/alerts/settings` | 更新警示設定 |
| POST | `/api/backtest` | 執行回測 |
| GET | `/api/usindex/{symbol}/kline?period=&days=` | 美股指數 K 線 |
| GET | `/api/telegram/settings` | Telegram 設定 |
| PUT | `/api/telegram/settings` | 更新 Telegram 設定 |
| POST | `/api/telegram/test` | 測試 Telegram 發送 |

## Telegram Bot 功能

### 設定方式

1. 在 WEB 介面「⚙ 設定」頁面填入 **Bot Token** 與 **Chat ID**
2. 或透過環境變數設定：`TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID`
3. 使用 `POST /api/telegram/test` 驗證連線

### Bot 指令

| 指令 | 說明 |
|------|------|
| `/start` `/menu` | 顯示主選單（Persistent Keyboard） |
| `/bind <帳號>` | 綁定 Telegram 帳號到 WEB 使用者，同步自選股/備註/警示 |

### 互動功能選單

| 功能 | 說明 |
|------|------|
| 📊 查股票 | 輸入代號 → MA 均線圖 + 加自選/AI 分析按鈕 |
| 📋 自選股 | 分組瀏覽 → 個股操作（備註/看圖/AI 分析） |
| 📡 掃描 | 多條件組合篩選（RSI/均線排列/站上MA/跌破MA/MA方向/MA交叉/量比/量趨勢/漲跌%/漲幅排名/產業/市場） |
| 💼 持股分析 | AI 投資組合分析 |
| 📰 新聞 | 新聞備註（按日期）/ 本週重點（週報） |
| ❓ 幫助 | 使用說明 |

### AI 分析功能

- **個股 AI 技術分析**：查股票或自選股點選「🤖 AI 分析」，分段顯示（趨勢判斷/關鍵價位/指標解讀/型態分析/綜合判斷），支援按鈕切換段落
- **持股分析**：一鍵執行投資組合 AI 分析

### 自動排程推播

| 排程 | 功能 |
|------|------|
| 每小時 | 抓取鉅亨網/Yahoo奇摩股市/CMoney 熱門新聞，疊加去重存入備註 |
| 每日 17:00 | AI 盤後分析（MiniMax）：概念股/族群/類股/產業資金輪動 → Telegram 推播 |
| 每日 17:05 | AI 大盤回饋學習：60分K線盤中預測回顧 + 日線/週線整合分析 → Telegram 推播 |
| 每週日 18:00 | 全自選股 AI 週度技術分析（含歷史對比）→ 彙整 Telegram 推播 |
| 警示觸發時 | 股價/漲跌幅/MA交叉/爆量 條件達標 → Telegram 即時通知 |

### 其他互動

- **@Bot 提問**：AI 語意搜尋新聞/備註，自動回覆
- **一般訊息**：自動存檔為討論紀錄

### Telegram 相關 API

| Method | Path | 說明 |
|--------|------|------|
| GET | `/api/telegram/settings` | 取得 Telegram 設定 |
| PUT | `/api/telegram/settings` | 更新 Bot Token / Chat ID |
| POST | `/api/telegram/test` | 發送測試訊息驗證設定 |

### 環境變數

| 變數 | 說明 |
|------|------|
| `TELEGRAM_BOT_TOKEN` | Telegram Bot Token（也可在 WEB 設定） |
| `TELEGRAM_CHAT_ID` | Telegram Chat/Group ID（也可在 WEB 設定） |
| `MINIMAX_API_KEY` | MiniMax AI API Key（供盤後分析/週報使用） |

---

## 資料來源

- PostgreSQL (ProTech-QuantStockDB) — K 線、營收、獲利、自選股、交易記錄、備註、餘額、排行榜、警示、美股指數
- Yahoo Stock — 社群爆紅榜
- Yahoo Finance API — 美股指數歷史資料

## Communication

- **技術解釋**使用「繁體中文」
- **變數名稱**、**函數名稱**與**代碼註釋**必須保持英文
