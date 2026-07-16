"""Alert engine — checks alert conditions against latest market data."""

import logging
from src.core.database import get_enabled_alerts, mark_alert_triggered, disable_alert
from src.core.pg_client import get_daily_kline

logger = logging.getLogger(__name__)


def compute_ma(closes, period):
    """Compute simple moving average for the last data point."""
    if len(closes) < period:
        return None
    return sum(closes[-period:]) / period


def check_alert(alert, kline_data):
    """
    Check if an alert condition is triggered.
    Returns (triggered: bool, message: str)
    """
    if not kline_data or len(kline_data) < 2:
        return False, ""

    params = alert["params"] or {}
    alert_type = alert["alert_type"]
    latest = kline_data[-1]
    prev = kline_data[-2]
    close = latest["close"]
    code = alert["stock_code"]

    if alert_type == "price_above":
        threshold = params.get("threshold", 0)
        if close >= threshold:
            return True, f"📈 {code} 股價 {close} ≥ {threshold}"

    elif alert_type == "price_below":
        threshold = params.get("threshold", 0)
        if close <= threshold:
            return True, f"📉 {code} 股價 {close} ≤ {threshold}"

    elif alert_type == "change_pct_up":
        threshold = params.get("threshold", 0)
        if prev["close"] > 0:
            pct = (close - prev["close"]) / prev["close"] * 100
            if pct >= threshold:
                return True, f"🔺 {code} 漲幅 {pct:.1f}% ≥ {threshold}%"

    elif alert_type == "change_pct_down":
        threshold = params.get("threshold", 0)
        if prev["close"] > 0:
            pct = (prev["close"] - close) / prev["close"] * 100
            if pct >= threshold:
                return True, f"🔻 {code} 跌幅 {pct:.1f}% ≥ {threshold}%"

    elif alert_type == "ma_cross_up":
        short_p = params.get("short_period", 5)
        long_p = params.get("long_period", 20)
        closes = [d["close"] for d in kline_data]
        if len(closes) >= long_p + 1:
            ma_s_now = compute_ma(closes, short_p)
            ma_l_now = compute_ma(closes, long_p)
            ma_s_prev = compute_ma(closes[:-1], short_p)
            ma_l_prev = compute_ma(closes[:-1], long_p)
            if ma_s_prev and ma_l_prev and ma_s_now and ma_l_now:
                if ma_s_prev <= ma_l_prev and ma_s_now > ma_l_now:
                    return True, f"✨ {code} MA{short_p}上穿MA{long_p}（金叉）"

    elif alert_type == "ma_cross_down":
        short_p = params.get("short_period", 5)
        long_p = params.get("long_period", 20)
        closes = [d["close"] for d in kline_data]
        if len(closes) >= long_p + 1:
            ma_s_now = compute_ma(closes, short_p)
            ma_l_now = compute_ma(closes, long_p)
            ma_s_prev = compute_ma(closes[:-1], short_p)
            ma_l_prev = compute_ma(closes[:-1], long_p)
            if ma_s_prev and ma_l_prev and ma_s_now and ma_l_now:
                if ma_s_prev >= ma_l_prev and ma_s_now < ma_l_now:
                    return True, f"💀 {code} MA{short_p}下穿MA{long_p}（死叉）"

    elif alert_type == "volume":
        multiplier = params.get("multiplier", 2)
        volumes = [d["volume"] for d in kline_data[:-1]]  # exclude today
        if len(volumes) >= 20:
            avg_vol = sum(volumes[-20:]) / 20
            if avg_vol > 0 and latest["volume"] >= avg_vol * multiplier:
                return True, f"🔊 {code} 成交量 {latest['volume']:,} ≥ {multiplier}倍均量({int(avg_vol):,})"

    return False, ""


def run_alert_check():
    """Run all enabled alerts (daily K-line based), return list of triggered messages."""
    alerts = get_enabled_alerts()
    triggered_messages = []
    kline_cache = {}

    for alert in alerts:
        # Skip realtime-type alerts (handled by run_realtime_alert_check)
        if alert["alert_type"] in REALTIME_ALERT_TYPES:
            continue
        code = alert["stock_code"]
        # Cache kline data per stock
        if code not in kline_cache:
            kline_cache[code] = get_daily_kline(code, days=60)

        triggered, msg = check_alert(alert, kline_cache[code])
        if triggered:
            triggered_messages.append({"alert_id": alert["id"], "user_id": alert["user_id"], "message": msg})
            mark_alert_triggered(alert["id"])
            if alert["repeat_mode"] == "once":
                disable_alert(alert["id"])
            logger.info(f"Alert triggered: {msg}")

    return triggered_messages


# --- Realtime alert check (intraday) ---

# Alert types that should be checked with realtime quotes
REALTIME_ALERT_TYPES = {"price_above", "price_below", "change_pct_up", "change_pct_down"}


def check_realtime_alert(alert, quote):
    """
    Check if a realtime alert condition is triggered.
    Args:
        alert: alert dict from DB
        quote: realtime quote dict {price, yesterday_close, ...}
    Returns (triggered: bool, message: str)
    """
    price = quote.get("price")
    if price is None:
        return False, ""

    params = alert["params"] or {}
    alert_type = alert["alert_type"]
    code = alert["stock_code"]
    yesterday_close = quote.get("yesterday_close")

    if alert_type == "price_above":
        threshold = params.get("threshold", 0)
        if price >= threshold:
            return True, f"📈 {code} 即時價 {price} ≥ {threshold}"

    elif alert_type == "price_below":
        threshold = params.get("threshold", 0)
        if price <= threshold:
            return True, f"📉 {code} 即時價 {price} ≤ {threshold}"

    elif alert_type == "change_pct_up":
        threshold = params.get("threshold", 0)
        if yesterday_close and yesterday_close > 0:
            pct = (price - yesterday_close) / yesterday_close * 100
            if pct >= threshold:
                return True, f"🔺 {code} 即時漲幅 {pct:.1f}% ≥ {threshold}%"

    elif alert_type == "change_pct_down":
        threshold = params.get("threshold", 0)
        if yesterday_close and yesterday_close > 0:
            pct = (yesterday_close - price) / yesterday_close * 100
            if pct >= threshold:
                return True, f"🔻 {code} 即時跌幅 {pct:.1f}% ≥ {threshold}%"

    return False, ""


def run_realtime_alert_check(quotes):
    """
    Run realtime alert check with fetched quotes.
    Args:
        quotes: dict {stock_code: {price, yesterday_close, ...}} from MIS API
    Returns:
        list of triggered messages
    """
    alerts = get_enabled_alerts()
    triggered_messages = []

    for alert in alerts:
        if alert["alert_type"] not in REALTIME_ALERT_TYPES:
            continue

        code = alert["stock_code"]
        quote = quotes.get(code)
        if not quote:
            continue

        triggered, msg = check_realtime_alert(alert, quote)
        if triggered:
            triggered_messages.append({"alert_id": alert["id"], "user_id": alert["user_id"], "message": msg})
            mark_alert_triggered(alert["id"])
            if alert["repeat_mode"] == "once":
                disable_alert(alert["id"])
            logger.info(f"Realtime alert triggered: {msg}")

    return triggered_messages
