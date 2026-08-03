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

## 資料來源

- PostgreSQL (ProTech-QuantStockDB) — K 線、營收、獲利、自選股、交易記錄、備註、餘額、排行榜、警示、美股指數
- Yahoo Stock — 社群爆紅榜
- Yahoo Finance API — 美股指數歷史資料
