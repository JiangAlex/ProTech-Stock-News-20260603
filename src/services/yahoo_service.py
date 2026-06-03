"""Yahoo Stock community hot stocks ranking."""

import re
import requests
from bs4 import BeautifulSoup

BASE_URL = "https://tw.stock.yahoo.com"


def fetch_hot_stocks(rank_type="active"):
    """Fetch community hot stocks. rank_type: active, trending, search."""
    resp = requests.get(
        f"{BASE_URL}/community/rank/{rank_type}",
        headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"},
        timeout=10,
    )
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
