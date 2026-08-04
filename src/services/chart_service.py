"""Chart Service — Generate MA line charts using matplotlib.

Produces PNG bytes for Telegram sendPhoto.
Only draws close price + MA lines (no candlestick).
"""

import io
import logging
from typing import List, Optional

import matplotlib
matplotlib.use("Agg")  # Non-interactive backend
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from datetime import datetime

logger = logging.getLogger(__name__)

# Dark theme colors matching the frontend
BG_COLOR = "#0f1117"
PANEL_COLOR = "#1a1d28"
GRID_COLOR = "#2a2d38"
TEXT_COLOR = "#e1e5ea"
CLOSE_COLOR = "#e1e5ea"
MA_COLORS = {
    5: "#ff6b6b",   # Red
    20: "#6bcb77",  # Green
    60: "#4d96ff",  # Blue
}


def generate_ma_chart(kline_data: List[dict], stock_code: str, stock_name: str = "",
                      ma_periods: List[int] = None) -> Optional[bytes]:
    """Generate MA line chart from kline data.

    Args:
        kline_data: List of {"date": "YYYY-MM-DD", "close": float, ...}
        stock_code: Stock code for title
        stock_name: Stock name for title
        ma_periods: MA periods to draw (default: [5, 20, 60])

    Returns:
        PNG image bytes, or None on failure
    """
    if not kline_data or len(kline_data) < 5:
        return None

    if ma_periods is None:
        ma_periods = [5, 20, 60]

    try:
        # Parse data
        dates = [datetime.strptime(d["date"], "%Y-%m-%d") for d in kline_data]
        closes = [d["close"] for d in kline_data]

        # Compute MAs
        ma_data = {}
        for period in ma_periods:
            if len(closes) >= period:
                ma_values = _compute_ma(closes, period)
                ma_data[period] = ma_values

        # Create figure
        fig, ax = plt.subplots(1, 1, figsize=(10, 5))
        fig.patch.set_facecolor(BG_COLOR)
        ax.set_facecolor(PANEL_COLOR)

        # Plot MA lines
        for period, values in ma_data.items():
            color = MA_COLORS.get(period, "#ffffff")
            # Only plot non-None values
            valid_dates = []
            valid_values = []
            for i, v in enumerate(values):
                if v is not None:
                    valid_dates.append(dates[i])
                    valid_values.append(v)
            if valid_dates:
                ax.plot(valid_dates, valid_values, color=color, linewidth=1.5,
                        label=f"MA{period}")

        # Title
        title = f"{stock_code}"
        if stock_name:
            title += f" {stock_name}"
        # Add current price info
        last_close = closes[-1]
        prev_close = closes[-2] if len(closes) > 1 else last_close
        change = last_close - prev_close
        change_pct = (change / prev_close * 100) if prev_close else 0
        sign = "+" if change >= 0 else ""
        title += f"  {last_close:.2f} ({sign}{change_pct:.2f}%)"

        ax.set_title(title, color=TEXT_COLOR, fontsize=13, fontweight="bold",
                     fontfamily="sans-serif", pad=10)

        # Style
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)
        ax.spines["left"].set_color(GRID_COLOR)
        ax.spines["bottom"].set_color(GRID_COLOR)
        ax.tick_params(colors=TEXT_COLOR, labelsize=9)
        ax.yaxis.label.set_color(TEXT_COLOR)
        ax.xaxis.label.set_color(TEXT_COLOR)
        ax.grid(True, alpha=0.3, color=GRID_COLOR)

        # Date format
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d"))
        ax.xaxis.set_major_locator(mdates.AutoDateLocator())
        fig.autofmt_xdate(rotation=30)

        # Legend
        legend = ax.legend(loc="upper left", fontsize=9, framealpha=0.7,
                          facecolor=PANEL_COLOR, edgecolor=GRID_COLOR)
        for text in legend.get_texts():
            text.set_color(TEXT_COLOR)

        # Add latest MA values annotation on the right
        last_date = dates[-1]
        for period, values in ma_data.items():
            last_ma = values[-1]
            if last_ma is not None:
                color = MA_COLORS.get(period, "#ffffff")
                ax.annotate(f"{last_ma:.1f}",
                           xy=(last_date, last_ma),
                           xytext=(5, 0), textcoords="offset points",
                           fontsize=8, color=color, va="center")

        plt.tight_layout()

        # Export to PNG bytes
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=120, facecolor=BG_COLOR,
                    bbox_inches="tight", pad_inches=0.1)
        plt.close(fig)
        buf.seek(0)
        return buf.getvalue()

    except Exception as e:
        logger.error(f"Chart generation failed for {stock_code}: {e}")
        return None


def _compute_ma(closes: List[float], period: int) -> List[Optional[float]]:
    """Compute Simple Moving Average."""
    result = [None] * len(closes)
    for i in range(period - 1, len(closes)):
        result[i] = sum(closes[i - period + 1:i + 1]) / period
    return result
