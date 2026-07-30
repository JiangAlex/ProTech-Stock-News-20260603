"""Industry Classification Service — fetch from TWSE/TPEx and update DB."""

import json
import urllib.request
import logging

logger = logging.getLogger(__name__)

# TWSE 產業代碼對照表
TWSE_INDUSTRY_MAP = {
    "01": "水泥", "02": "食品", "03": "塑膠", "04": "紡織纖維",
    "05": "電機機械", "06": "電器電纜", "08": "玻璃陶瓷", "09": "造紙",
    "10": "鋼鐵", "11": "橡膠", "12": "汽車", "14": "建材營造",
    "15": "航運", "16": "觀光餐旅", "17": "金融保險", "18": "貿易百貨",
    "20": "其他", "21": "化學", "22": "生技醫療", "23": "油電燃氣",
    "24": "半導體", "25": "電腦及週邊", "26": "光電", "27": "通信網路",
    "28": "電子零組件", "29": "電子通路", "30": "資訊服務", "31": "其他電子",
    "35": "綠能環保", "36": "數位雲端", "37": "運動休閒", "38": "居家生活",
    "91": "存託憑證",
}

# TPEx 上櫃產業代碼對照表（與 TWSE 部分重疊，部分不同）
TPEX_INDUSTRY_MAP = {
    "01": "水泥", "02": "食品", "03": "塑膠", "04": "紡織纖維",
    "05": "電機機械", "06": "電器電纜", "08": "玻璃陶瓷", "09": "造紙",
    "10": "鋼鐵", "11": "橡膠", "12": "汽車", "14": "建材營造",
    "15": "航運", "16": "觀光餐旅", "17": "金融保險", "18": "貿易百貨",
    "20": "其他", "21": "化學", "22": "生技醫療", "23": "油電燃氣",
    "24": "半導體", "25": "電腦及週邊", "26": "光電", "27": "通信網路",
    "28": "電子零組件", "29": "電子通路", "30": "資訊服務", "31": "其他電子",
    "32": "文化創意", "33": "農業科技", "34": "電子商務",
    "35": "綠能環保", "36": "數位雲端", "37": "運動休閒", "38": "居家生活",
}


def fetch_twse_industry() -> list[dict]:
    """Fetch industry classification for TWSE (上市) stocks.

    Returns: [{"code": "2330", "name": "台積電", "industry_code": "24", "industry": "半導體"}]
    """
    url = "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read())
        results = []
        for d in data:
            code = d.get("公司代號", "").strip()
            name = d.get("公司簡稱", "").strip()
            ind_code = d.get("產業別", "").strip()
            industry = TWSE_INDUSTRY_MAP.get(ind_code, f"未分類({ind_code})")
            if code:
                results.append({
                    "code": code, "name": name,
                    "industry_code": ind_code, "industry": industry,
                })
        logger.info(f"TWSE industry: fetched {len(results)} stocks")
        return results
    except Exception as e:
        logger.error(f"Failed to fetch TWSE industry: {e}")
        return []


def fetch_tpex_industry() -> list[dict]:
    """Fetch industry classification for TPEx (上櫃) stocks.

    Returns: [{"code": "6547", "name": "高端疫苗", "industry_code": "22", "industry": "生技醫療"}]
    """
    url = "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read())
        results = []
        for d in data:
            code = d.get("SecuritiesCompanyCode", "").strip()
            name = d.get("CompanyAbbreviation", "").strip()
            ind_code = d.get("SecuritiesIndustryCode", "").strip()
            industry = TPEX_INDUSTRY_MAP.get(ind_code, f"未分類({ind_code})")
            if code:
                results.append({
                    "code": code, "name": name,
                    "industry_code": ind_code, "industry": industry,
                })
        logger.info(f"TPEx industry: fetched {len(results)} stocks")
        return results
    except Exception as e:
        logger.error(f"Failed to fetch TPEx industry: {e}")
        return []


def update_stock_industry():
    """Fetch industry classification from TWSE/TPEx and update stock_basic table."""
    import psycopg2
    from src.core.pg_client import DB_CONFIG

    # Ensure column exists
    conn = psycopg2.connect(**DB_CONFIG)
    try:
        cur = conn.cursor()
        cur.execute("ALTER TABLE stock_basic ADD COLUMN IF NOT EXISTS industry TEXT DEFAULT NULL")
        conn.commit()
    finally:
        conn.close()

    # Fetch data
    twse = fetch_twse_industry()
    tpex = fetch_tpex_industry()
    all_stocks = twse + tpex

    if not all_stocks:
        logger.error("No industry data fetched, aborting update")
        return 0

    # Update DB
    conn = psycopg2.connect(**DB_CONFIG)
    updated = 0
    try:
        cur = conn.cursor()
        for s in all_stocks:
            cur.execute(
                "UPDATE stock_basic SET industry = %s WHERE stock_code = %s",
                (s["industry"], s["code"]))
            if cur.rowcount > 0:
                updated += 1
        conn.commit()
        logger.info(f"Updated industry for {updated}/{len(all_stocks)} stocks")
    finally:
        conn.close()

    return updated
