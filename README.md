# ProTech-Stock-News-20260603

個股新聞爬蟲與整合系統 — 爬取經濟日報/工商時報/Yahoo股市新聞，結合 K 線圖 Dashboard。

## 功能

- **新聞爬取**：經濟日報 (UDN)、工商時報 (CTEE via Google News)、Yahoo 股市
- **全文搜尋**：SQLite FTS5 中文全文搜尋
- **個股辨識**：自動辨識新聞中提到的股票代號/名稱，建立關聯
- **K 線圖**：整合 ProTech-QuantStockDB (PostgreSQL) 歷史日 K 線資料
- **排程爬取**：每日 08:00 / 12:00 / 18:00 自動爬取
- **Dashboard**：單頁 Web UI，搜尋個股顯示新聞 + K 線圖

## 快速啟動

```bash
# 建立虛擬環境
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 啟動 server
python -m src.server
# 瀏覽器開啟 http://localhost:8020
```

## API Endpoints

| Method | Path | 說明 |
|--------|------|------|
| GET | `/api/news/latest` | 最新新聞 |
| GET | `/api/news/search?q=` | FTS5 全文搜尋 |
| GET | `/api/stock/{code}/news` | 個股相關新聞 |
| GET | `/api/stock/{code}/kline?days=` | K 線資料 |
| GET | `/api/stocks` | 股票清單（autocomplete） |
| POST | `/api/scrape?source=all` | 手動觸發爬取 |
| GET | `/api/scheduler/status` | 排程狀態 |
| GET | `/api/scrape/log` | 爬取歷史 |

## 專案結構

```
src/
├── core/
│   ├── database.py      # SQLite FTS5
│   ├── scraper.py       # Base HTTP scraper
│   ├── stock_tagger.py  # 個股辨識
│   └── pg_client.py     # PostgreSQL 連線
├── services/
│   ├── udn_service.py   # 經濟日報
│   ├── ctee_service.py  # 工商時報
│   ├── yahoo_service.py # Yahoo 股市
│   ├── news_service.py  # 查詢服務
│   └── scheduler.py     # APScheduler
├── server.py            # FastAPI
└── templates/
    └── index.html       # Dashboard
```
