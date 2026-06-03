"""News query service - wraps database queries for the API layer."""

from src.core.database import search_news_fts, get_latest_news, get_news_by_stock
from src.core.pg_client import get_daily_kline, get_all_stocks


def search_news(keyword, limit=20):
    """Search news using FTS5."""
    return search_news_fts(keyword, limit)


def get_stock_news(stock_code, limit=30):
    """Get news related to a specific stock."""
    return get_news_by_stock(stock_code, limit)


def get_stock_kline(stock_code, days=90):
    """Get K-line data from PostgreSQL."""
    return get_daily_kline(stock_code, days)


def get_stock_list():
    """Get all stocks for autocomplete."""
    stocks = get_all_stocks()
    return [{"code": code, "name": name} for code, name in stocks]
