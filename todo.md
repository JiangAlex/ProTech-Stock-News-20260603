# TODO

## ~~新增新聞備註區塊 + 上市加權指數(TWII)~~ ✅ done (30a30b4)

- [x] 抓取 TWII 歷史資料存入 us_index_kline 表
- [x] 前端加入 TWII 快捷按鈕（跟美股指數一起）
- [x] Dashboard 最下方新增新聞備註區塊（stock_code='NEWS'）
- [x] 驗證 + push

## ~~新聞備註 OCR 自動標題 + 搜尋~~ ✅ done (feature/news-ocr-search)

- [x] 安裝 pytesseract + tesseract-ocr (chi_tra+eng)
- [x] 上傳圖片自動 OCR 擷取文字存入 content（前 200 字）
- [x] 搜尋框即時過濾備註內容
- [x] 驗證 + push

## 暫緩

- [ ] 上櫃指數 — Yahoo Finance 無對應 ticker，待找其他資料來源
