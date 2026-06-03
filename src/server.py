"""FastAPI server for ProTech Stock Dashboard."""

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse
from pathlib import Path

from src.core.database import init_db, get_watchlist, add_watchlist, remove_watchlist
from src.core.pg_client import (
    get_all_stocks, get_daily_kline, get_weekly_kline, get_monthly_kline,
    get_annual_revenue, get_cumulative_eps,
)
from src.services.yahoo_service import fetch_hot_stocks

app = FastAPI(title="ProTech Stock Dashboard", version="2.0.0")
TEMPLATES_DIR = Path(__file__).parent / "templates"


@app.on_event("startup")
def startup():
    init_db()


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
    return get_annual_revenue(code)


@app.get("/api/stock/{code}/eps")
def api_eps(code: str):
    return get_cumulative_eps(code)


# --- Hot stocks ---

@app.get("/api/hot-stocks")
def api_hot_stocks(type: str = Query("active")):
    return fetch_hot_stocks(type)


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
