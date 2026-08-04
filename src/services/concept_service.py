"""Concept Stock Service — 概念股分類抓取與管理。

從 Goodinfo 台灣股市資訊網抓取概念股分類對照表，
存入 stock_concepts 資料表供 AI 盤後分析使用。
"""

import logging
import time
import urllib.request
from datetime import date

import psycopg2
from psycopg2.extras import execute_values

from src.core.pg_client import DB_CONFIG

logger = logging.getLogger(__name__)

# Goodinfo 概念股列表頁
GOODINFO_CONCEPT_LIST_URL = "https://goodinfo.tw/tw/StockList.asp?MARKET_CAT=%E6%A6%82%E5%BF%B5%E8%82%A1"
# 單一概念股成分頁面
GOODINFO_CONCEPT_DETAIL_URL = "https://goodinfo.tw/tw/StockList.asp?MARKET_CAT=%E6%A6%82%E5%BF%B5%E8%82%A1&INDUSTRY_CAT={concept}"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
    "Referer": "https://goodinfo.tw/tw/StockList.asp",
}


def init_concept_table():
    """建立 stock_concepts 資料表。"""
    conn = psycopg2.connect(**DB_CONFIG)
    try:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS stock_concepts (
                id SERIAL PRIMARY KEY,
                concept_name TEXT NOT NULL,
                stock_code TEXT NOT NULL,
                stock_name TEXT DEFAULT '',
                updated_at DATE DEFAULT CURRENT_DATE,
                UNIQUE (concept_name, stock_code)
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_sc_concept ON stock_concepts (concept_name)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_sc_stock ON stock_concepts (stock_code)")
        conn.commit()
        logger.info("stock_concepts table ready")
    finally:
        conn.close()


def _fetch_page(url: str) -> str | None:
    """抓取網頁內容，含 retry。"""
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=20) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except Exception as e:
            logger.warning(f"Fetch attempt {attempt+1} failed [{url}]: {e}")
            time.sleep(2 * (attempt + 1))
    return None


def fetch_concept_list() -> list[str]:
    """從 Goodinfo 抓取所有概念股分類名稱。

    Returns: ["AI", "5G", "電動車", "元宇宙", ...]
    """
    from bs4 import BeautifulSoup

    html = _fetch_page(GOODINFO_CONCEPT_LIST_URL)
    if not html:
        logger.error("Failed to fetch concept list page")
        return []

    try:
        soup = BeautifulSoup(html, "lxml")
        concepts = []

        # Goodinfo 的概念股分類在 select option 或 table 中
        # 方法1: 從 select#INDUSTRY_CAT 的 option 取得
        select = soup.find("select", {"id": "INDUSTRY_CAT"})
        if select:
            for opt in select.find_all("option"):
                val = opt.get("value", "").strip()
                text = opt.text.strip()
                if val and text and val != "全部" and text != "全部":
                    concepts.append(text)

        if not concepts:
            # 方法2: 從 navbar 或其他 link 取得
            for link in soup.select("a[href*='INDUSTRY_CAT=']"):
                text = link.text.strip()
                if text and len(text) >= 2 and text not in ("全部", "概念股"):
                    if text not in concepts:
                        concepts.append(text)

        logger.info(f"Found {len(concepts)} concept categories")
        return concepts
    except Exception as e:
        logger.error(f"Parse concept list error: {e}")
        return []


def fetch_concept_stocks(concept_name: str) -> list[dict]:
    """從 Goodinfo 抓取指定概念股的成分個股。

    Args:
        concept_name: 概念名稱，如 "AI"

    Returns: [{"stock_code": "2330", "stock_name": "台積電"}, ...]
    """
    from bs4 import BeautifulSoup
    import urllib.parse

    encoded = urllib.parse.quote(concept_name)
    url = GOODINFO_CONCEPT_DETAIL_URL.format(concept=encoded)
    html = _fetch_page(url)
    if not html:
        return []

    try:
        soup = BeautifulSoup(html, "lxml")
        stocks = []

        # Goodinfo 股票列表通常在 table#tblStockList 或 class=b1 的 table
        table = soup.find("table", {"id": "tblStockList"})
        if not table:
            # 備用：找包含「代號」欄位的 table
            for t in soup.find_all("table"):
                headers = [th.text.strip() for th in t.find_all("th")]
                if "代號" in headers or "股票代號" in headers:
                    table = t
                    break

        if not table:
            return []

        rows = table.find_all("tr")
        for row in rows[1:]:  # skip header
            cells = row.find_all("td")
            if len(cells) >= 2:
                code = cells[0].text.strip()
                name = cells[1].text.strip()
                # 驗證股票代號格式（4碼數字）
                if code and code.isdigit() and len(code) == 4:
                    stocks.append({"stock_code": code, "stock_name": name})

        return stocks
    except Exception as e:
        logger.error(f"Parse concept stocks error [{concept_name}]: {e}")
        return []


def update_all_concepts(delay: float = 3.0) -> dict:
    """全量更新所有概念股分類。

    Args:
        delay: 每次請求的間隔秒數（避免被封鎖）

    Returns: {"concepts_count": int, "stocks_count": int}
    """
    init_concept_table()

    concepts = fetch_concept_list()
    if not concepts:
        logger.error("No concepts fetched, aborting")
        return {"concepts_count": 0, "stocks_count": 0}

    all_rows = []  # [(concept_name, stock_code, stock_name)]

    for i, concept in enumerate(concepts):
        stocks = fetch_concept_stocks(concept)
        for s in stocks:
            all_rows.append((concept, s["stock_code"], s["stock_name"]))

        if stocks:
            logger.info(f"[{i+1}/{len(concepts)}] {concept}: {len(stocks)} stocks")
        else:
            logger.warning(f"[{i+1}/{len(concepts)}] {concept}: no stocks found")

        # 避免請求過快被封
        if i < len(concepts) - 1:
            time.sleep(delay)

    if not all_rows:
        logger.error("No concept stock data fetched")
        return {"concepts_count": 0, "stocks_count": 0}

    # 全量替換
    conn = psycopg2.connect(**DB_CONFIG)
    try:
        cur = conn.cursor()
        # 清除舊資料，重新寫入
        cur.execute("DELETE FROM stock_concepts")
        execute_values(cur, """
            INSERT INTO stock_concepts (concept_name, stock_code, stock_name, updated_at)
            VALUES %s
            ON CONFLICT (concept_name, stock_code) DO UPDATE SET
                stock_name = EXCLUDED.stock_name,
                updated_at = EXCLUDED.updated_at
        """, [(c, code, name, date.today()) for c, code, name in all_rows])
        conn.commit()
        logger.info(f"Updated stock_concepts: {len(concepts)} concepts, {len(all_rows)} entries")
    finally:
        conn.close()

    return {"concepts_count": len(concepts), "stocks_count": len(all_rows)}


def get_concept_stocks_context(limit_concepts: int = 30) -> str:
    """
    從 DB 取得概念股分類資料，格式化為 AI prompt 用的文字。
    只取個股數量前 N 大的概念分類。

    Returns: 格式化的概念股-個股對照文字。
    """
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        cur = conn.cursor()

        # 取得概念分類及成分股（每個概念取前 8 檔）
        cur.execute("""
            WITH ranked AS (
                SELECT concept_name, stock_code, stock_name,
                       ROW_NUMBER() OVER (PARTITION BY concept_name ORDER BY stock_code) as rn,
                       COUNT(*) OVER (PARTITION BY concept_name) as concept_size
                FROM stock_concepts
            )
            SELECT concept_name, stock_code, stock_name, concept_size
            FROM ranked
            WHERE rn <= 8
            ORDER BY concept_size DESC, concept_name, rn
        """)
        rows = cur.fetchall()
        conn.close()

        if not rows:
            return ""

        # 整理成 {concept: [stocks]}
        from collections import OrderedDict
        concept_map = OrderedDict()
        seen_concepts = 0
        for concept_name, stock_code, stock_name, concept_size in rows:
            if concept_name not in concept_map:
                if seen_concepts >= limit_concepts:
                    continue
                concept_map[concept_name] = []
                seen_concepts += 1
            concept_map[concept_name].append(f"{stock_code} {stock_name}")

        # 格式化
        lines = []
        for concept, stocks in concept_map.items():
            lines.append(f"【{concept}】{', '.join(stocks)}")

        return "\n".join(lines)
    except Exception as e:
        logger.error(f"Failed to get concept stocks context: {e}")
        return ""


def get_stock_concepts(stock_code: str) -> list[str]:
    """取得某支股票所屬的概念股分類。"""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        cur = conn.cursor()
        cur.execute(
            "SELECT concept_name FROM stock_concepts WHERE stock_code = %s ORDER BY concept_name",
            (stock_code,)
        )
        result = [r[0] for r in cur.fetchall()]
        conn.close()
        return result
    except Exception as e:
        logger.error(f"Failed to get concepts for {stock_code}: {e}")
        return []
