"""Backtest engine — run trading strategy simulation on historical data."""

from src.core.pg_client import get_daily_kline


def compute_ma(closes, period, idx):
    """Compute MA at index idx (0-based)."""
    if idx < period - 1:
        return None
    return sum(closes[idx - period + 1: idx + 1]) / period


def check_condition(condition, kline, idx, closes, volumes):
    """
    Check if a single condition is met at index idx.
    condition: {"type": str, "params": dict}
    Returns bool.
    """
    ctype = condition["type"]
    params = condition.get("params", {})

    if idx < 1:
        return False

    if ctype == "price_above":
        return closes[idx] >= params.get("threshold", 0)

    elif ctype == "price_below":
        return closes[idx] <= params.get("threshold", 0)

    elif ctype == "change_pct_up":
        if closes[idx - 1] > 0:
            pct = (closes[idx] - closes[idx - 1]) / closes[idx - 1] * 100
            return pct >= params.get("threshold", 0)

    elif ctype == "change_pct_down":
        if closes[idx - 1] > 0:
            pct = (closes[idx - 1] - closes[idx]) / closes[idx - 1] * 100
            return pct >= params.get("threshold", 0)

    elif ctype == "ma_cross_up":
        short_p = params.get("short_period", 5)
        long_p = params.get("long_period", 20)
        if idx < long_p:
            return False
        ms_now = compute_ma(closes, short_p, idx)
        ml_now = compute_ma(closes, long_p, idx)
        ms_prev = compute_ma(closes, short_p, idx - 1)
        ml_prev = compute_ma(closes, long_p, idx - 1)
        if ms_now and ml_now and ms_prev and ml_prev:
            return ms_prev <= ml_prev and ms_now > ml_now

    elif ctype == "ma_cross_down":
        short_p = params.get("short_period", 5)
        long_p = params.get("long_period", 20)
        if idx < long_p:
            return False
        ms_now = compute_ma(closes, short_p, idx)
        ml_now = compute_ma(closes, long_p, idx)
        ms_prev = compute_ma(closes, short_p, idx - 1)
        ml_prev = compute_ma(closes, long_p, idx - 1)
        if ms_now and ml_now and ms_prev and ml_prev:
            return ms_prev >= ml_prev and ms_now < ml_now

    elif ctype == "volume":
        multiplier = params.get("multiplier", 2)
        if idx >= 20:
            avg_vol = sum(volumes[idx - 20: idx]) / 20
            return avg_vol > 0 and volumes[idx] >= avg_vol * multiplier

    elif ctype == "ma_above":
        period = params.get("period", 20)
        ma = compute_ma(closes, period, idx)
        if ma:
            return closes[idx] > ma

    elif ctype == "ma_below":
        period = params.get("period", 20)
        ma = compute_ma(closes, period, idx)
        if ma:
            return closes[idx] < ma

    return False


def check_conditions(conditions, logic, kline, idx, closes, volumes):
    """
    Check multiple conditions with AND/OR logic.
    logic: "and" or "or"
    """
    if not conditions:
        return False
    results = [check_condition(c, kline, idx, closes, volumes) for c in conditions]
    if logic == "or":
        return any(results)
    return all(results)


def run_backtest(stock_code, buy_conditions, sell_conditions,
                 buy_logic="and", sell_logic="and",
                 shares=1, fee_rate=0.1425, tax_rate=0.3, days=500):
    """
    Run backtest simulation.

    Args:
        stock_code: stock code
        buy_conditions: list of {"type": str, "params": dict}
        sell_conditions: list of {"type": str, "params": dict}
        buy_logic: "and" or "or"
        sell_logic: "and" or "or"
        shares: shares per trade (張)
        fee_rate: broker fee rate (%)  default 0.1425%
        tax_rate: transaction tax rate (%) default 0.3%
        days: lookback days for kline data

    Returns:
        {
            "trades": [{"date", "action", "price", "shares", "cost", "pnl"}],
            "summary": {"total_return", "total_pnl", "win_rate", "num_trades", "max_drawdown"},
            "markers": [{"date", "action", "price"}]
        }
    """
    kline = get_daily_kline(stock_code, days=days)
    if not kline or len(kline) < 30:
        return {"trades": [], "summary": {}, "markers": []}

    closes = [d["close"] for d in kline]
    volumes = [d["volume"] for d in kline]

    trades = []
    markers = []
    holding = False
    buy_price = 0
    total_cost = 0  # accumulated cost basis

    for i in range(1, len(kline)):
        if not holding:
            # Check buy
            if check_conditions(buy_conditions, buy_logic, kline, i, closes, volumes):
                price = closes[i]
                cost = price * shares * 1000
                fee = cost * fee_rate / 100
                buy_price = price
                total_cost = cost + fee
                holding = True
                trades.append({
                    "date": kline[i]["date"],
                    "action": "buy",
                    "price": price,
                    "shares": shares,
                    "cost": round(total_cost),
                    "pnl": 0,
                })
                markers.append({"date": kline[i]["date"], "action": "buy", "price": price})
        else:
            # Check sell
            if check_conditions(sell_conditions, sell_logic, kline, i, closes, volumes):
                price = closes[i]
                revenue = price * shares * 1000
                fee = revenue * fee_rate / 100
                tax = revenue * tax_rate / 100
                net = revenue - fee - tax
                pnl = net - total_cost
                holding = False
                trades.append({
                    "date": kline[i]["date"],
                    "action": "sell",
                    "price": price,
                    "shares": shares,
                    "cost": round(fee + tax),
                    "pnl": round(pnl),
                })
                markers.append({"date": kline[i]["date"], "action": "sell", "price": price})

    # Summary
    sell_trades = [t for t in trades if t["action"] == "sell"]
    num_trades = len(sell_trades)
    total_pnl = sum(t["pnl"] for t in sell_trades)
    wins = len([t for t in sell_trades if t["pnl"] > 0])
    win_rate = round(wins / num_trades * 100, 1) if num_trades > 0 else 0

    # Total invested (sum of buy costs)
    buy_trades = [t for t in trades if t["action"] == "buy"]
    total_invested = sum(t["cost"] for t in buy_trades) if buy_trades else 1
    total_return = round(total_pnl / total_invested * 100, 2) if total_invested > 0 else 0

    # Max drawdown (from equity curve)
    equity = 0
    peak = 0
    max_dd = 0
    for t in sell_trades:
        equity += t["pnl"]
        if equity > peak:
            peak = equity
        dd = peak - equity
        if dd > max_dd:
            max_dd = dd

    summary = {
        "total_return": total_return,
        "total_pnl": round(total_pnl),
        "win_rate": win_rate,
        "num_trades": num_trades,
        "max_drawdown": round(max_dd),
        "total_invested": round(total_invested),
    }

    return {"trades": trades, "summary": summary, "markers": markers}
