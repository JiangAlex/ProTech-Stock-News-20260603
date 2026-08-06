"""AI Prediction Verifier — 每日收盤後驗證已過期預測。

比對實際價格，計算：
1. 方向是否正確（bullish 且漲 > 0% = correct）
2. 模擬損益（假設預測日買進，N日後賣出）
"""

import logging
from datetime import date, timedelta

import psycopg2
from psycopg2.extras import RealDictCursor

from src.core.pg_client import DB_CONFIG

logger = logging.getLogger(__name__)


def get_close_price_on_date(stock_code: str, target_date: date) -> float | None:
    """Get the close price for a stock on a specific date (or nearest prior trading day)."""
    conn = psycopg2.connect(**DB_CONFIG)
    try:
        cur = conn.cursor()
        # Try exact date first, then look back up to 5 days for holidays
        cur.execute("""
            SELECT close FROM daily_kline
            WHERE stock_code = %s AND trade_date <= %s
            ORDER BY trade_date DESC LIMIT 1
        """, (stock_code, target_date))
        row = cur.fetchone()
        if row:
            return float(row[0])

        # Try US index table for indices
        cur.execute("""
            SELECT close FROM us_index_kline
            WHERE symbol = %s AND trade_date <= %s
            ORDER BY trade_date DESC LIMIT 1
        """, (stock_code, target_date))
        row = cur.fetchone()
        if row:
            return float(row[0])

        return None
    finally:
        conn.close()


def verify_predictions(verify_after_days: int = 5) -> dict:
    """
    Verify all unverified predictions that are at least N days old.

    For each prediction:
    1. Get the close price N days after prediction_date
    2. Calculate actual return
    3. Determine if direction was correct
    4. Calculate simulated P&L (buy at prediction price, sell at N-day price)

    Args:
        verify_after_days: Number of days to wait before verifying (5 or 20)

    Returns:
        {"verified": int, "correct": int, "incorrect": int, "skipped": int}
    """
    result = {"verified": 0, "correct": 0, "incorrect": 0, "skipped": 0}

    conn = psycopg2.connect(**DB_CONFIG)
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)

        # Find predictions that need verification
        cutoff_date = date.today() - timedelta(days=verify_after_days)

        if verify_after_days <= 5:
            # Verify 5-day predictions
            cur.execute("""
                SELECT * FROM ai_predictions
                WHERE price_after_5d IS NULL
                  AND prediction_date <= %s
                ORDER BY prediction_date
            """, (cutoff_date,))
        else:
            # Verify 20-day predictions
            cur.execute("""
                SELECT * FROM ai_predictions
                WHERE price_after_20d IS NULL
                  AND prediction_date <= %s
                ORDER BY prediction_date
            """, (cutoff_date,))

        predictions = [dict(r) for r in cur.fetchall()]
        logger.info(f"Prediction verifier: {len(predictions)} predictions to verify ({verify_after_days}d)")

        for pred in predictions:
            stock_code = pred["stock_code"]
            pred_date = pred["prediction_date"]
            pred_price = float(pred["price_at_prediction"]) if pred["price_at_prediction"] else None

            if not pred_price or pred_price <= 0:
                result["skipped"] += 1
                continue

            # Get price N days after prediction
            target_date = pred_date + timedelta(days=verify_after_days)
            actual_price = get_close_price_on_date(stock_code, target_date)

            if actual_price is None:
                result["skipped"] += 1
                continue

            # Calculate return
            actual_return = round((actual_price - pred_price) / pred_price * 100, 2)

            # Determine if direction was correct
            direction = pred["direction"]
            if direction == "bullish":
                is_correct = actual_return > 0
            elif direction == "bearish":
                is_correct = actual_return < 0
            else:
                # neutral: correct if abs(return) < 2%
                is_correct = abs(actual_return) < 2.0

            # Simulated P&L (assume buy 1000 shares at prediction price)
            simulated_shares = 1000
            simulated_pnl = round((actual_price - pred_price) * simulated_shares, 0)

            # Update DB
            update_cur = conn.cursor()
            if verify_after_days <= 5:
                update_cur.execute("""
                    UPDATE ai_predictions
                    SET price_after_5d = %s,
                        actual_return_5d = %s,
                        is_correct_5d = %s,
                        verified_at = NOW()
                    WHERE id = %s
                """, (actual_price, actual_return, is_correct, pred["id"]))
            else:
                update_cur.execute("""
                    UPDATE ai_predictions
                    SET price_after_20d = %s,
                        actual_return_20d = %s,
                        is_correct_20d = %s,
                        verified_at = NOW()
                    WHERE id = %s
                """, (actual_price, actual_return, is_correct, pred["id"]))

            result["verified"] += 1
            if is_correct:
                result["correct"] += 1
            else:
                result["incorrect"] += 1

            logger.info(
                f"  [{stock_code}] {pred_date} dir={direction} "
                f"pred_price={pred_price} actual={actual_price} "
                f"return={actual_return}% {'✓' if is_correct else '✗'} "
                f"sim_pnl={simulated_pnl:+.0f}"
            )

        conn.commit()
    finally:
        conn.close()

    logger.info(
        f"Prediction verifier done ({verify_after_days}d): "
        f"verified={result['verified']} correct={result['correct']} "
        f"incorrect={result['incorrect']} skipped={result['skipped']}"
    )
    return result


def run_daily_verification() -> dict:
    """Run both 5-day and 20-day verification. Called by daily scheduler."""
    result_5d = verify_predictions(verify_after_days=5)
    result_20d = verify_predictions(verify_after_days=20)
    return {
        "5d": result_5d,
        "20d": result_20d,
    }


def weekly_prediction_review() -> str | None:
    """Generate weekly AI prediction review report for Telegram.

    Summarizes last week's predictions: total, correct, accuracy,
    biggest failure, and stocks to watch this week.

    Returns: formatted message string, or None if no data.
    """
    from datetime import date, timedelta

    conn = psycopg2.connect(**DB_CONFIG)
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)

        # Last week range (Mon-Sun)
        today = date.today()
        last_monday = today - timedelta(days=today.weekday() + 7)
        last_sunday = last_monday + timedelta(days=6)

        # Get predictions made last week that have been verified
        cur.execute("""
            SELECT stock_code, prediction_date, direction,
                   price_at_prediction, price_after_5d, actual_return_5d, is_correct_5d
            FROM ai_predictions
            WHERE prediction_date BETWEEN %s AND %s
              AND is_correct_5d IS NOT NULL
            ORDER BY prediction_date
        """, (last_monday, last_sunday))
        verified = [dict(r) for r in cur.fetchall()]

        # Get predictions made last week still pending
        cur.execute("""
            SELECT stock_code, prediction_date, direction, price_at_prediction
            FROM ai_predictions
            WHERE prediction_date BETWEEN %s AND %s
              AND is_correct_5d IS NULL
            ORDER BY prediction_date
        """, (last_monday, last_sunday))
        pending = [dict(r) for r in cur.fetchall()]

        # Get overall stats
        cur.execute("""
            SELECT COUNT(*) as total,
                   COUNT(*) FILTER (WHERE is_correct_5d = true) as correct,
                   COUNT(*) FILTER (WHERE is_correct_5d IS NOT NULL) as verified_count
            FROM ai_predictions
        """)
        overall = dict(cur.fetchone())

        conn.close()

        if not verified and not pending:
            return None

        # Build report
        total_week = len(verified)
        correct_week = sum(1 for v in verified if v["is_correct_5d"])
        accuracy_week = round(correct_week / total_week * 100, 1) if total_week > 0 else 0

        lines = [f"📊 <b>AI 預測週回顧</b>（{last_monday} ~ {last_sunday}）\n"]

        if total_week > 0:
            lines.append(f"上週預測：<b>{total_week}</b> 筆｜命中：<b>{correct_week}</b> 筆｜準確率：<b>{accuracy_week}%</b>\n")

            # Biggest failure
            failures = [v for v in verified if not v["is_correct_5d"]]
            if failures:
                worst = min(failures, key=lambda x: abs(x["actual_return_5d"] or 0) * (-1 if x["direction"] == "bullish" else 1))
                dir_label = "偏多" if worst["direction"] == "bullish" else "偏空"
                lines.append(
                    f"❌ 最大失誤：{worst['stock_code']} "
                    f"判斷「{dir_label}」{worst['price_at_prediction']} → "
                    f"{worst['price_after_5d']}（{worst['actual_return_5d']:+.1f}%）"
                )

            # Successes
            successes = [v for v in verified if v["is_correct_5d"]]
            if successes:
                best = max(successes, key=lambda x: abs(x["actual_return_5d"] or 0))
                dir_label = "偏多" if best["direction"] == "bullish" else "偏空"
                lines.append(
                    f"✅ 最佳預測：{best['stock_code']} "
                    f"判斷「{dir_label}」{best['price_at_prediction']} → "
                    f"{best['price_after_5d']}（{best['actual_return_5d']:+.1f}%）"
                )
        else:
            lines.append("上週無已驗證的預測。\n")

        # Pending predictions (this week's watchlist)
        if pending:
            lines.append(f"\n⏳ 本週待驗證：{len(pending)} 筆")
            for p in pending[:5]:
                dir_label = "📈" if p["direction"] == "bullish" else "📉" if p["direction"] == "bearish" else "➖"
                lines.append(f"  {dir_label} {p['stock_code']} {p['price_at_prediction']}（{p['prediction_date']}）")
            if len(pending) > 5:
                lines.append(f"  ... 及其他 {len(pending)-5} 筆")

        # Overall cumulative stats
        if overall["verified_count"] > 0:
            overall_acc = round(overall["correct"] / overall["verified_count"] * 100, 1)
            lines.append(f"\n📈 累計準確率：<b>{overall_acc}%</b>（{overall['correct']}/{overall['verified_count']}）")

        return "\n".join(lines)
    except Exception as e:
        logger.error(f"Weekly prediction review failed: {e}")
        return None
