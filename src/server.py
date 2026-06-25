"""FastAPI server for ProTech Stock Dashboard."""

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse
from pathlib import Path

from src.core.database import init_db, get_watchlist, add_watchlist, remove_watchlist, save_rank, get_rank_history
from src.core.pg_client import (
    get_all_stocks, get_daily_kline, get_weekly_kline, get_monthly_kline,
)
from src.services.yahoo_service import fetch_hot_stocks, fetch_revenue, fetch_dividend, fetch_rank

app = FastAPI(title="ProTech Stock Dashboard", version="2.0.0")
TEMPLATES_DIR = Path(__file__).parent / "templates"


@app.on_event("startup")
def startup():
    init_db()
    import asyncio
    asyncio.get_event_loop().create_task(_daily_rank_job())


async def _daily_rank_job():
    """Run rank collection daily at 17:00."""
    import asyncio
    from datetime import datetime, timedelta

    while True:
        now = datetime.now()
        target = now.replace(hour=17, minute=0, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())

        from datetime import date
        for direction in ("up", "down"):
            for market in ("tse", "otc"):
                data = fetch_rank(direction, market)
                if data:
                    save_rank(date.today().isoformat(), direction, market, data)


@app.get("/", response_class=HTMLResponse)
def index():
    return (TEMPLATES_DIR / "index.html").read_text(encoding="utf-8")


# --- Stock data ---

@app.get("/api/stocks")
def api_stocks():
    return get_all_stocks()


@app.get("/api/stock/{code}/kline")
def api_kline(code: str, period: str = Query("daily"), days: int = Query(120, le=500)):
    if period == "weekly":
        return get_weekly_kline(code, days)
    elif period == "monthly":
        return get_monthly_kline(code, days)
    return get_daily_kline(code, days)


@app.get("/api/stock/{code}/revenue")
def api_revenue(code: str):
    return fetch_revenue(code)


@app.get("/api/stock/{code}/dividend")
def api_dividend(code: str):
    return fetch_dividend(code)


# --- Hot stocks ---

@app.get("/api/hot-stocks")
def api_hot_stocks(type: str = Query("active")):
    return fetch_hot_stocks(type)


@app.get("/api/rank")
def api_rank(direction: str = Query("up"), market: str = Query("all")):
    from datetime import date
    data = fetch_rank(direction, market)
    if data:
        save_rank(date.today().isoformat(), direction, market, data)
    return data


@app.get("/api/rank/history")
def api_rank_history(date: str = Query(...), direction: str = Query("up"), market: str = Query("all")):
    return get_rank_history(date, direction, market)


# --- Watchlist ---

@app.get("/api/watchlist")
def api_get_watchlist():
    return get_watchlist()


@app.post("/api/watchlist/{code}")
def api_add_watchlist(code: str, name: str = Query("")):
    add_watchlist(code, name)
    return {"ok": True}


@app.delete("/api/watchlist/{code}")
def api_remove_watchlist(code: str):
    remove_watchlist(code)
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8020)
