"""FastAPI server for ProTech Stock Dashboard."""

from fastapi import FastAPI, Query, UploadFile, File, Form
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path

from src.core.database import (
    init_db, get_watchlist, add_watchlist, remove_watchlist, move_watchlist,
    rename_group, delete_group, save_rank, get_rank_history, create_group,
    get_all_groups, get_notes, add_note, delete_note, update_cost, sell_stock,
    get_balance, update_balance, get_trades, get_all_trades, add_trade, delete_trade,
)
from src.core.pg_client import (
    get_all_stocks, get_daily_kline, get_weekly_kline, get_monthly_kline,
)
from src.services.yahoo_service import fetch_hot_stocks, fetch_revenue, fetch_dividend, fetch_rank

app = FastAPI(title="ProTech Stock Dashboard", version="2.0.0")
TEMPLATES_DIR = Path(__file__).parent / "templates"
UPLOADS_DIR = Path(__file__).parent.parent / "data" / "uploads"
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=str(UPLOADS_DIR)), name="uploads")


@app.on_event("startup")
def startup():
    init_db()
    import asyncio
    asyncio.get_event_loop().create_task(_daily_rank_job())


async def _daily_rank_job():
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


# --- Watchlist (static paths first) ---

@app.get("/api/watchlist")
def api_get_watchlist(user: str = Query("default")):
    return get_watchlist(user)


@app.get("/api/watchlist/groups")
def api_get_groups(user: str = Query("default")):
    return get_all_groups(user)


@app.post("/api/watchlist/group")
def api_create_group(name: str = Query(...), user: str = Query("default")):
    create_group(name, user)
    return {"ok": True}


@app.put("/api/watchlist/group/rename")
def api_rename_group(old: str = Query(...), new: str = Query(...), user: str = Query("default")):
    rename_group(old, new, user)
    return {"ok": True}


@app.delete("/api/watchlist/group/{group_name}")
def api_delete_group(group_name: str, user: str = Query("default")):
    delete_group(group_name, user)
    return {"ok": True}


@app.delete("/api/watchlist/notes/{note_id}")
def api_delete_note(note_id: int, user: str = Query("default")):
    img = delete_note(note_id, user)
    if img:
        p = UPLOADS_DIR / img.split("/")[-1]
        if p.exists():
            p.unlink()
    return {"ok": True}


# --- Balance ---

@app.get("/api/watchlist/balance")
def api_get_balance(user: str = Query("default")):
    return {"balance": get_balance(user)}


@app.post("/api/watchlist/balance")
def api_update_balance(amount: float = Query(...), user: str = Query("default")):
    update_balance(user, amount)
    return {"ok": True, "balance": get_balance(user)}


# --- Trades ---

@app.get("/api/watchlist/trades")
def api_get_all_trades(user: str = Query("default")):
    return get_all_trades(user)


@app.delete("/api/watchlist/trades/{trade_id}")
def api_delete_trade(trade_id: int, user: str = Query("default")):
    delete_trade(trade_id, user)
    return {"ok": True}


# --- Watchlist (dynamic paths) ---

@app.post("/api/watchlist/{code}")
def api_add_watchlist(code: str, name: str = Query(""), group: str = Query("預設"),
                      user: str = Query("default"), buy_price: float = Query(0),
                      buy_shares: int = Query(0), buy_date: str = Query("")):
    add_watchlist(code, name, group, user, buy_price, buy_shares, buy_date)
    # Add trade record and deduct from balance
    if buy_price > 0 and buy_shares > 0:
        add_trade(code, buy_price, buy_shares, buy_date, user)
        cost = buy_price * buy_shares * 1000
        update_balance(user, -cost)
    return {"ok": True}


@app.delete("/api/watchlist/{code}")
def api_remove_watchlist(code: str, user: str = Query("default")):
    remove_watchlist(code, user)
    return {"ok": True}


@app.put("/api/watchlist/{code}/move")
def api_move_watchlist(code: str, group: str = Query(...), user: str = Query("default")):
    move_watchlist(code, group, user)
    return {"ok": True}


@app.put("/api/watchlist/{code}/cost")
def api_update_cost(code: str, price: float = Query(...), shares: int = Query(...),
                    date: str = Query(""), user: str = Query("default")):
    update_cost(code, price, shares, date, user)
    return {"ok": True}


@app.post("/api/watchlist/{code}/sell")
def api_sell_stock(code: str, price: float = Query(...), shares: int = Query(...),
                   user: str = Query("default")):
    sell_stock(code, shares, price, user)
    return {"ok": True}


@app.get("/api/watchlist/{code}/trades")
def api_get_trades(code: str, user: str = Query("default")):
    return get_trades(code, user)


@app.post("/api/watchlist/{code}/trades")
def api_add_trade(code: str, price: float = Query(...), shares: int = Query(...),
                  date: str = Query(""), user: str = Query("default")):
    add_trade(code, price, shares, date, user)
    # Deduct from balance
    cost = price * shares * 1000
    update_balance(user, -cost)
    return {"ok": True}


@app.get("/api/watchlist/{code}/notes")
def api_get_notes(code: str, user: str = Query("default")):
    return get_notes(code, user)


@app.post("/api/watchlist/{code}/notes")
async def api_add_note(code: str, user: str = Query("default"),
                       content: str = Form(""), image: UploadFile = File(None)):
    image_path = ""
    if image and image.filename:
        import uuid
        ext = Path(image.filename).suffix
        fname = f"{code}_{uuid.uuid4().hex[:8]}{ext}"
        dest = UPLOADS_DIR / fname
        dest.write_bytes(await image.read())
        image_path = f"/uploads/{fname}"
    add_note(code, content, image_path, user)
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8020)
