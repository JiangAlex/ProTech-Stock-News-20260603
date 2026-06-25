# ProTech-Stock-News-20260603

個股技術分析 Dashboard — K 線圖（日/週/月）+ MA/VOL/MACD 指標 + 基本面圖表 + 自選股。

## 功能

- **K 線圖**：日線/週線/月線切換，天數可調
- **技術指標**：MA 均線（最多 5 條自訂）、VOL/MACD 子圖互斥切換
- **基本面**：年度營收走勢折線圖、歷年股利柱狀圖
- **自選股**：加入/移除，持久化儲存
- **社群爆紅榜**：Yahoo 最多瀏覽/瀏覽激增/熱門搜尋
- **漲跌幅排行**：上市/上櫃 漲幅排行、跌幅排行
- **個股新聞**：外連 Yahoo News 搜尋

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

## 資料來源

- PostgreSQL (ProTech-QuantStockDB) — K 線、營收、獲利
- Yahoo Stock — 社群爆紅榜
- SQLite — 自選股
