"""FastAPI server for ProTech-Stock-News."""

import logging
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path

from src.core.database import init_db, get_latest_news
from src.services.news_service import search_news, get_stock_news, get_stock_kline, get_stock_list
from src.services.udn_service import UdnScraper
from src.services.ctee_service import CteeScraper
from src.services.yahoo_service import YahooScraper
from src.core.stock_tagger import tag_all_untagged
from src.core.database import get_connection
from src.services.scheduler import start_scheduler, stop_scheduler, get_scheduler_status

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="ProTech Stock News", version="1.0.0")

TEMPLATES_DIR = Path(__file__).parent / "templates"


@app.on_event("startup")
def startup():
    init_db()
    start_scheduler()
    logger.info("Database initialized, scheduler started")


@app.on_event("shutdown")
def shutdown():
    stop_scheduler()


@app.get("/", response_class=HTMLResponse)
def index():
    """Serve the dashboard HTML."""
    html_path = TEMPLATES_DIR / "index.html"
    return html_path.read_text(encoding="utf-8")


@app.get("/api/news/latest")
def api_latest_news(limit: int = Query(50, le=200)):
    """Get latest news."""
    return get_latest_news(limit)


@app.get("/api/news/search")
def api_search_news(q: str = Query(..., min_length=1), limit: int = Query(20, le=100)):
    """Search news using FTS5."""
    return search_news(q, limit)


@app.get("/api/stock/{code}/news")
def api_stock_news(code: str, limit: int = Query(30, le=100)):
    """Get news for a specific stock."""
    return get_stock_news(code, limit)


@app.get("/api/stock/{code}/kline")
def api_stock_kline(code: str, days: int = Query(90, le=365)):
    """Get K-line data for a stock."""
    return get_stock_kline(code, days)


@app.get("/api/stocks")
def api_stock_list():
    """Get all stock codes/names for autocomplete."""
    return get_stock_list()


@app.post("/api/scrape")
def api_scrape(source: str = Query("all")):
    """Manually trigger scraping. source: udn, ctee, yahoo, all."""
    results = {}

    if source in ("all", "udn"):
        try:
            count = UdnScraper().scrape_all(max_per_category=10)
            results["udn"] = count
        except Exception as e:
            results["udn"] = f"error: {e}"

    if source in ("all", "ctee"):
        try:
            count = CteeScraper().scrape_all()
            results["ctee"] = count
        except Exception as e:
            results["ctee"] = f"error: {e}"

    if source in ("all", "yahoo"):
        try:
            count = YahooScraper().scrape_all()
            results["yahoo"] = count
        except Exception as e:
            results["yahoo"] = f"error: {e}"

    # Auto-tag after scraping
    tagged = tag_all_untagged()
    results["tagged"] = tagged

    # Log results
    conn = get_connection()
    try:
        for src, val in results.items():
            if src == "tagged":
                continue
            status = "success" if isinstance(val, int) else "error"
            count = val if isinstance(val, int) else 0
            error = str(val) if not isinstance(val, int) else None
            conn.execute(
                "INSERT INTO scrape_log (source, status, news_count, error_message) VALUES (?, ?, ?, ?)",
                (src, status, count, error),
            )
        conn.commit()
    finally:
        conn.close()

    return results


@app.get("/api/scrape/log")
def api_scrape_log(limit: int = Query(20, le=100)):
    """Get recent scrape logs."""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM scrape_log ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@app.get("/api/scheduler/status")
def api_scheduler_status():
    """Get scheduler status and next run times."""
    return get_scheduler_status()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8020)
