# ProTech-Stock-News-20260603 開發記錄

## 專案概述
個股技術分析 Dashboard — K 線圖 + 技術指標 + 基本面 + 自選股 + 社群爆紅榜 + 漲跌幅排行

## 技術架構
- **後端**: FastAPI (Python 3.12), uvicorn, port 8020
- **資料庫**: PostgreSQL (K線/營收/股票清單) + SQLite (自選股/notes/trades/balance/rank_history)
- **前端**: 單一 HTML (src/templates/index.html), Lightweight Charts v4.1, html2canvas
- **資料來源**: Yahoo Stock (爬蟲: 爆紅榜/營收/股利/排行)

## 已完成功能 (截至 2026-06-30)

### 核心功能
- K 線圖：日/週/月線切換，天數可調
- 技術指標：MA 均線（最多 5 條）、VOL/MACD 子圖互斥切換
- 基本面：年度營收走勢、歷年股利柱狀圖
- 社群爆紅榜：Yahoo 最多瀏覽/瀏覽激增/熱門搜尋
- 漲跌幅排行：上市/上櫃，可查歷史日期
- 個股新聞：底部外連 Yahoo News

### 自選股系統
- 分組管理（新增/重命名/刪除分組）
- 多使用者隔離（簡易帳號，前端 localStorage，user_id 區隔）
- 未輸入使用者名稱時無法操作自選股
- user 名稱不分大小寫（統一轉 lowercase）

### 交易/損益追蹤
- **多筆交易記錄** (watchlist_trades 表)：同一檔股票可多次買入
- 個股顯示：現價、均價、總張數、損益金額、報酬率%
- 面板頂端：總市值、總成本、總損益、總報酬率
- 賣出功能：FIFO 方式扣減持股，加回餘額

### 銀行餘額
- user_balance 表，每人獨立
- 入金功能（手動加餘額）
- 買入自選股自動扣款
- 賣出加回餘額
- 總資產 = 餘額 + 股票市值

### Notes 備註
- 多筆 notes（文字 + 圖檔上傳）
- 圖檔存 data/uploads/，靜態服務掛載 /uploads
- 截圖功能：K 線圖截圖自動存入個股 notes

### K 線圖畫線工具
- 畫線模式（✏）：拖曳畫線段，跟隨 K 線移動（用邏輯座標）
- 文字模式（T）：點擊加入文字標記
- 截圖（📷）：截圖存入個股 notes
- 清除功能

## 已修復的重要 Bug
1. **FastAPI 路由順序衝突**：靜態路徑必須定義在動態 `{code}` 路徑之前
2. **JS 語法錯誤**：重複 `const` 宣告導致整個前端崩潰
3. **watchlist PK 問題**：舊表 PK 只有 stock_code，需 migration 重建為 (stock_code, user_id)
4. **Lightweight Charts v4.1 API**：不支援 `timeScale().coordinateToTime()`，改用 `coordinateToLogical` + `series.coordinateToPrice`

## 路由順序規則 (重要)
FastAPI 路由按定義順序匹配，靜態路徑必須在動態路徑前：
```
/api/watchlist                    (GET)
/api/watchlist/groups             (GET)
/api/watchlist/group              (POST)
/api/watchlist/group/rename       (PUT)
/api/watchlist/group/{group_name} (DELETE)
/api/watchlist/notes/{note_id}    (DELETE)
/api/watchlist/balance            (GET/POST)
/api/watchlist/trades             (GET)
/api/watchlist/trades/{trade_id}  (DELETE)
--- 動態路徑在後 ---
/api/watchlist/{code}             (POST/DELETE)
/api/watchlist/{code}/move        (PUT)
/api/watchlist/{code}/cost        (PUT)
/api/watchlist/{code}/sell        (POST)
/api/watchlist/{code}/trades      (GET/POST)
/api/watchlist/{code}/notes       (GET/POST)
```

## 資料庫狀態
- K線 (PostgreSQL): 最新 2026-06-30
- 排行榜 (SQLite rank_history): 最新 2026-06-29
- 依賴: python-multipart (for file upload)

## Git
- Remote: https://github.com/JiangAlex/ProTech-Stock-News-20260603.git
- Branch: main
- Latest commit: 1ca706b (2026-06-30)
