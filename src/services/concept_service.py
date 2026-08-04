"""Concept Stock Service — 概念股分類抓取與管理。

提供概念股/題材分類對照表，存入 stock_concepts 資料表供 AI 盤後分析使用。
資料來源：內建常見概念股對照表（Goodinfo 因 Cloudflare 保護無法直接抓取）。
"""

import logging
import time
import urllib.request
from datetime import date

import psycopg2
from psycopg2.extras import execute_values

from src.core.pg_client import DB_CONFIG

logger = logging.getLogger(__name__)

# --- 內建概念股對照表（台股常見 30+ 種概念/題材） ---
# 格式：{概念名稱: [股票代號列表]}
BUILTIN_CONCEPTS = {
    "AI 伺服器": [
        "2317", "2382", "3231", "2324", "2353", "3013", "2376", "6669", "3661", "2308",
    ],
    "AI 晶片/IC設計": [
        "2330", "3443", "2454", "3034", "2379", "6547", "3529", "2388", "6415", "5274",
    ],
    "CoWoS/先進封裝": [
        "2330", "3711", "6770", "2449", "3037", "8150", "6239", "3661",
    ],
    "散熱/熱管理": [
        "3017", "6274", "3017", "2059", "3653", "5765", "6230", "3552",
    ],
    "PCB/載板": [
        "3037", "8150", "6153", "2313", "3189", "5765", "8046", "4999",
    ],
    "光通訊/矽光子": [
        "2345", "6209", "3149", "2455", "5309", "4746", "3704", "6244",
    ],
    "電動車": [
        "2308", "2382", "3443", "2049", "1513", "2327", "6121", "3665", "2231", "6803",
    ],
    "車用電子": [
        "2379", "3044", "3035", "2330", "2454", "6239", "5483", "2449", "3443",
    ],
    "蘋果供應鏈": [
        "2317", "3711", "2354", "4938", "3008", "2474", "6176", "3661", "2327", "6285",
    ],
    "半導體設備": [
        "3443", "2379", "3532", "5765", "6691", "3556", "6488", "2404",
    ],
    "重電/電網": [
        "1503", "1504", "8150", "1519", "3018", "2308", "6409", "9933",
    ],
    "綠能/太陽能": [
        "6244", "3576", "6443", "6464", "3691", "6469", "3561", "6147",
    ],
    "風電": [
        "2208", "2634", "2023", "9933", "6409", "1503", "8150", "3708",
    ],
    "儲能/充電樁": [
        "6409", "3527", "6591", "8150", "6443", "1513", "6121", "3665",
    ],
    "5G/通訊": [
        "2454", "3443", "3034", "4904", "3045", "2049", "6285", "3596",
    ],
    "低軌衛星": [
        "3558", "6285", "2345", "4904", "3596", "3443", "5388", "2455",
    ],
    "元宇宙/XR": [
        "3443", "2454", "3034", "3231", "2382", "3532", "5765", "6674",
    ],
    "機器人": [
        "2317", "4739", "2308", "3443", "2454", "6452", "2049", "1590", "4306",
    ],
    "航運/貨櫃": [
        "2615", "2609", "2603", "2618", "2606", "2637", "5765",
    ],
    "航空": [
        "2610", "2618", "6288", "2634",
    ],
    "金融/升息": [
        "2881", "2882", "2884", "2886", "2887", "2891", "2892", "2880", "5880", "2885",
    ],
    "生技醫療": [
        "6547", "4743", "6446", "1707", "4744", "6472", "4726", "1760", "4968", "6589",
    ],
    "記憶體/DRAM": [
        "2408", "3450", "8150", "3037", "2337", "6770",
    ],
    "ABF載板": [
        "3037", "8150", "6153", "2313",
    ],
    "IP矽智財": [
        "5765", "6770", "3529", "6547", "5765", "3611",
    ],
    "網通/交換器": [
        "3231", "3045", "2345", "6285", "4904", "3596", "4999", "2332",
    ],
    "ASIC/客製化晶片": [
        "3661", "2379", "5765", "6770", "3443", "3034",
    ],
    "軍工/國防": [
        "2634", "2208", "2023", "1503", "1504", "2014", "1476", "9933",
    ],
    "觀光/旅遊": [
        "2712", "2707", "2706", "5765", "2739", "6202", "2731", "2722",
    ],
    "營建/都更": [
        "2504", "2505", "2511", "2520", "2530", "2534", "2538", "2547", "2548",
    ],
    "鋼鐵/原物料": [
        "2002", "2006", "2014", "2015", "2017", "2020", "2024", "2027",
    ],
    "食品/民生消費": [
        "1201", "1203", "1210", "1215", "1216", "1217", "1218", "1219", "1220",
    ],
    "紡織/成衣": [
        "1402", "1434", "1440", "1441", "1444", "1451", "1454", "1459", "1460",
    ],
    "CXL/高速傳輸": [
        "3443", "6770", "5765", "3711", "2379", "6285",
    ],
    "邊緣運算/Edge AI": [
        "3443", "2454", "3034", "2382", "3231", "3013", "2376",
    ],
    "銅箔基板/CCL": [
        "6213", "2313", "3189", "6153", "8046",
    ],
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


def _get_stock_name_map() -> dict[str, str]:
    """從 stock_basic 取得 code -> name 對照。"""
    conn = psycopg2.connect(**DB_CONFIG)
    try:
        cur = conn.cursor()
        cur.execute("SELECT stock_code, stock_name FROM stock_basic")
        return {r[0]: r[1] for r in cur.fetchall()}
    finally:
        conn.close()


def update_all_concepts(delay: float = 0) -> dict:
    """更新所有概念股分類（使用內建對照表）。

    Args:
        delay: 保留參數，相容介面（內建資料不需延遲）

    Returns: {"concepts_count": int, "stocks_count": int}
    """
    init_concept_table()

    # 取得股票名稱對照
    name_map = _get_stock_name_map()

    all_rows = []  # [(concept_name, stock_code, stock_name)]
    for concept, codes in BUILTIN_CONCEPTS.items():
        seen_codes = set()
        for code in codes:
            if code not in seen_codes:
                seen_codes.add(code)
                name = name_map.get(code, "")
                all_rows.append((concept, code, name))

    if not all_rows:
        logger.error("No concept stock data to insert")
        return {"concepts_count": 0, "stocks_count": 0}

    # 全量替換
    conn = psycopg2.connect(**DB_CONFIG)
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM stock_concepts")
        execute_values(cur, """
            INSERT INTO stock_concepts (concept_name, stock_code, stock_name, updated_at)
            VALUES %s
            ON CONFLICT (concept_name, stock_code) DO UPDATE SET
                stock_name = EXCLUDED.stock_name,
                updated_at = EXCLUDED.updated_at
        """, [(c, code, name, date.today()) for c, code, name in all_rows])
        conn.commit()
        logger.info(f"Updated stock_concepts: {len(BUILTIN_CONCEPTS)} concepts, {len(all_rows)} entries")
    finally:
        conn.close()

    return {"concepts_count": len(BUILTIN_CONCEPTS), "stocks_count": len(all_rows)}


def add_concept(concept_name: str, stock_codes: list[str]) -> int:
    """手動新增或更新一個概念股分類。

    Args:
        concept_name: 概念名稱
        stock_codes: 股票代號列表

    Returns: 新增的筆數
    """
    name_map = _get_stock_name_map()
    rows = [(concept_name, code, name_map.get(code, ""), date.today()) for code in stock_codes]

    conn = psycopg2.connect(**DB_CONFIG)
    try:
        cur = conn.cursor()
        execute_values(cur, """
            INSERT INTO stock_concepts (concept_name, stock_code, stock_name, updated_at)
            VALUES %s
            ON CONFLICT (concept_name, stock_code) DO UPDATE SET
                stock_name = EXCLUDED.stock_name,
                updated_at = EXCLUDED.updated_at
        """, rows)
        conn.commit()
        return len(rows)
    finally:
        conn.close()


def remove_concept(concept_name: str) -> int:
    """刪除一個概念股分類。"""
    conn = psycopg2.connect(**DB_CONFIG)
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM stock_concepts WHERE concept_name = %s", (concept_name,))
        count = cur.rowcount
        conn.commit()
        return count
    finally:
        conn.close()


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
