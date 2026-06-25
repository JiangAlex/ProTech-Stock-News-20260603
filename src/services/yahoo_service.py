"""Yahoo Stock: hot stocks, revenue, dividend data."""

import re
import requests
from bs4 import BeautifulSoup

BASE_URL = "https://tw.stock.yahoo.com"
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"}


def fetch_hot_stocks(rank_type="active"):
    """Fetch community hot stocks. rank_type: active, trending, search."""
    resp = requests.get(f"{BASE_URL}/community/rank/{rank_type}", headers=HEADERS, timeout=10)
    if resp.status_code != 200:
        return []

    soup = BeautifulSoup(resp.text, "lxml")
    code_spans = soup.find_all("span", string=re.compile(r"^\d{4}\.TW$"))

    results = []
    for rank, span in enumerate(code_spans, 1):
        code = span.get_text(strip=True).replace(".TW", "")
        name = ""
        parent_div = span.parent
        if parent_div and parent_div.parent:
            all_text = parent_div.parent.get_text(" ", strip=True)
            name_match = re.search(r"([\u4e00-\u9fff\w*]+)\s+" + code, all_text)
            name = name_match.group(1) if name_match else ""
        results.append({"rank": rank, "code": code, "name": name})

    return results


def fetch_revenue(stock_code):
    """Fetch monthly revenue from Yahoo Stock, grouped by year for multi-line chart."""
    resp = requests.get(f"{BASE_URL}/quote/{stock_code}.TW/revenue", headers=HEADERS, timeout=10)
    if resp.status_code != 200:
        return []

    soup = BeautifulSoup(resp.text, "lxml")
    data = []
    for li in soup.find_all("li"):
        m = re.match(r"(20\d{2})/(\d{2})\s+([\d,]+)", li.get_text(" ", strip=True))
        if m:
            data.append({"year": m.group(1), "month": int(m.group(2)), "revenue": int(m.group(3).replace(",", ""))})

    return data


def fetch_dividend(stock_code):
    """Fetch dividend history from Yahoo Stock /dividend page."""
    resp = requests.get(f"{BASE_URL}/quote/{stock_code}.TW/dividend", headers=HEADERS, timeout=10)
    if resp.status_code != 200:
        return []

    soup = BeautifulSoup(resp.text, "lxml")
    results = []
    for li in soup.find_all("li"):
        text = li.get_text(" ", strip=True)
        # Pattern: 發放年 所屬年 現金股利 股票股利(or -) ...
        m = re.match(r"(20\d{2})\s+(20\d{2})\s+([\d.]+|-)\s+([\d.]+|-)", text)
        if m:
            cash = float(m.group(3)) if m.group(3) != '-' else 0
            stock = float(m.group(4)) if m.group(4) != '-' else 0
            results.append({"year": m.group(1), "belonging": m.group(2), "cash": cash, "stock": stock})

    return results


def fetch_rank(direction="up", market="all"):
    """Fetch 漲幅/跌幅排行. direction: up/down, market: all/tse/otc."""
    exchange_map = {"tse": "TAI", "otc": "TWO"}
    params = f"?exchange={exchange_map[market]}" if market in exchange_map else ""
    resp = requests.get(f"{BASE_URL}/rank/change-{direction}{params}", headers=HEADERS, timeout=10)
    if resp.status_code != 200:
        return []

    soup = BeautifulSoup(resp.text, "lxml")
    results = []
    for li in soup.find_all("li"):
        text = li.get_text(" ", strip=True)
        m = re.match(
            r"(?:(\d+)\s+)?([\u4e00-\u9fff\w*\-]+)\s+(\d{4,5})\.(TW|TWO)\s+"
            r"([\d,.]+)\s+([\d.]+)\s+([\d.]+%)",
            text,
        )
        if m:
            suffix = m.group(4)
            results.append({
                "rank": int(m.group(1)) if m.group(1) else len(results) + 1,
                "name": m.group(2),
                "code": m.group(3),
                "market": "上市" if suffix == "TW" else "上櫃",
                "price": float(m.group(5).replace(",", "")),
                "change": float(m.group(6)),
                "change_pct": m.group(7),
            })

    return results
