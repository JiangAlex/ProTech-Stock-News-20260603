"""Yahoo Stock: hot stocks, revenue, EPS data."""

import re
from collections import defaultdict
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


def fetch_eps(stock_code):
    """Fetch quarterly EPS from Yahoo Stock, return cumulative by year."""
    resp = requests.get(f"{BASE_URL}/quote/{stock_code}.TW/eps", headers=HEADERS, timeout=10)
    if resp.status_code != 200:
        return []

    soup = BeautifulSoup(resp.text, "lxml")
    yearly = defaultdict(float)
    for li in soup.find_all("li"):
        m = re.match(r"(20\d{2})\s*Q[1-4]\s+([\d.]+)", li.get_text(" ", strip=True))
        if m:
            yearly[m.group(1)] += float(m.group(2))

    return [{"year": y, "eps": round(yearly[y], 2)} for y in sorted(yearly)]
