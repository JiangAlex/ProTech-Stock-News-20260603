"""Realtime quote service — TWSE/TPEx MIS API."""

import json
import logging
import urllib.request
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

MIS_API_URL = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"

# Market mapping: DB value -> MIS API prefix
MARKET_PREFIX = {
    "TSE": "tse",
    "TPEx": "otc",
}


def build_ex_ch(stock_codes: List[str], market_map: Dict[str, str]) -> str:
    """
    Build ex_ch query parameter for MIS API.
    e.g. "tse_2330.tw|otc_5483.tw"
    """
    parts = []
    for code in stock_codes:
        market = market_map.get(code)
        if not market:
            continue
        prefix = MARKET_PREFIX.get(market)
        if not prefix:
            continue
        parts.append(f"{prefix}_{code}.tw")
    return "|".join(parts)


def fetch_realtime_quotes(stock_codes: List[str], market_map: Dict[str, str]) -> Dict[str, dict]:
    """
    Fetch realtime quotes from TWSE MIS API.

    Args:
        stock_codes: List of stock codes to query.
        market_map: {stock_code: market} mapping from DB.

    Returns:
        Dict of {stock_code: quote_info} where quote_info contains:
        - price: current price (float or None if not yet traded)
        - yesterday_close: yesterday's close price
        - open: today's open
        - high: today's high
        - low: today's low
        - volume: accumulated volume (lots)
        - name: stock name
        - time: last trade time
    """
    if not stock_codes:
        return {}

    ex_ch = build_ex_ch(stock_codes, market_map)
    if not ex_ch:
        return {}

    url = f"{MIS_API_URL}?ex_ch={ex_ch}&json=1&delay=0"
    results = {}

    try:
        req = urllib.request.Request(url)
        req.add_header("User-Agent", "Mozilla/5.0")
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        if data.get("rtcode") != "0000":
            logger.warning(f"MIS API returned rtcode={data.get('rtcode')}: {data.get('rtmessage')}")
            return {}

        for item in data.get("msgArray", []):
            code = item.get("c", "")
            if not code:
                continue

            # z = latest trade price, y = yesterday close
            z = item.get("z", "-")
            y = item.get("y", "-")

            price = _parse_price(z)
            # Fallback: if z is '-', use best bid (b first value) as approximate price
            if price is None:
                b = item.get("b", "")
                if b and b != "-":
                    price = _parse_price(b.split("_")[0])

            yesterday_close = _parse_price(y)

            results[code] = {
                "price": price,
                "yesterday_close": yesterday_close,
                "open": _parse_price(item.get("o", "-")),
                "high": _parse_price(item.get("h", "-")),
                "low": _parse_price(item.get("l", "-")),
                "volume": _parse_int(item.get("v", "0")),
                "name": item.get("n", ""),
                "time": item.get("t", ""),
            }

    except Exception as e:
        logger.error(f"MIS API fetch error: {e}")

    return results


def fetch_realtime_quotes_batch(stock_codes: List[str], market_map: Dict[str, str],
                                batch_size: int = 20) -> Dict[str, dict]:
    """
    Fetch realtime quotes in batches to avoid overly long URLs.
    MIS API supports multiple stocks per request via '|' separator,
    but we limit batch size for safety.
    """
    all_results = {}
    for i in range(0, len(stock_codes), batch_size):
        batch = stock_codes[i:i + batch_size]
        results = fetch_realtime_quotes(batch, market_map)
        all_results.update(results)
    return all_results


def _parse_price(val: str) -> Optional[float]:
    """Parse price string. Returns None if '-' or invalid."""
    if not val or val == "-":
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _parse_int(val: str) -> int:
    """Parse integer string. Returns 0 if invalid."""
    try:
        return int(val)
    except (ValueError, TypeError):
        return 0
