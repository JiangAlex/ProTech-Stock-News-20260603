"""SQLite database with FTS5 for news storage and full-text search."""

import sqlite3
import os
from datetime import datetime

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DB_PATH = os.path.join(PROJECT_ROOT, "data", "news.db")


def get_connection():
    """Returns a connection to the SQLite database."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    """Initialize database tables and FTS5 virtual table."""
    conn = get_connection()
    c = conn.cursor()

    c.execute("""
    CREATE TABLE IF NOT EXISTS news (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        url TEXT UNIQUE NOT NULL,
        source TEXT NOT NULL,
        category TEXT,
        content TEXT,
        published_at TEXT,
        created_at TEXT DEFAULT (datetime('now', 'localtime'))
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS news_stock_rel (
        news_id INTEGER NOT NULL,
        stock_code TEXT NOT NULL,
        PRIMARY KEY (news_id, stock_code),
        FOREIGN KEY (news_id) REFERENCES news(id) ON DELETE CASCADE
    )
    """)

    c.execute("""
    CREATE INDEX IF NOT EXISTS idx_news_stock_rel_code
    ON news_stock_rel(stock_code)
    """)

    c.execute("""
    CREATE INDEX IF NOT EXISTS idx_news_source ON news(source)
    """)

    c.execute("""
    CREATE INDEX IF NOT EXISTS idx_news_published ON news(published_at DESC)
    """)

    # FTS5 virtual table
    c.execute("""
    CREATE VIRTUAL TABLE IF NOT EXISTS news_fts USING fts5(
        title,
        content,
        content=news,
        content_rowid=id
    )
    """)

    # Triggers to keep FTS5 in sync
    c.execute("""
    CREATE TRIGGER IF NOT EXISTS news_ai AFTER INSERT ON news BEGIN
        INSERT INTO news_fts(rowid, title, content)
        VALUES (new.id, new.title, new.content);
    END
    """)

    c.execute("""
    CREATE TRIGGER IF NOT EXISTS news_ad AFTER DELETE ON news BEGIN
        INSERT INTO news_fts(news_fts, rowid, title, content)
        VALUES ('delete', old.id, old.title, old.content);
    END
    """)

    c.execute("""
    CREATE TRIGGER IF NOT EXISTS news_au AFTER UPDATE ON news BEGIN
        INSERT INTO news_fts(news_fts, rowid, title, content)
        VALUES ('delete', old.id, old.title, old.content);
        INSERT INTO news_fts(rowid, title, content)
        VALUES (new.id, new.title, new.content);
    END
    """)

    # Scrape log table
    c.execute("""
    CREATE TABLE IF NOT EXISTS scrape_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source TEXT NOT NULL,
        status TEXT NOT NULL,
        news_count INTEGER DEFAULT 0,
        error_message TEXT,
        created_at TEXT DEFAULT (datetime('now', 'localtime'))
    )
    """)

    conn.commit()
    conn.close()


def insert_news(title, url, source, category=None, content=None, published_at=None):
    """Insert a news article. Returns id or None if duplicate."""
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute(
            "INSERT OR IGNORE INTO news (title, url, source, category, content, published_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (title, url, source, category, content, published_at)
        )
        conn.commit()
        return c.lastrowid if c.rowcount > 0 else None
    finally:
        conn.close()


def search_news_fts(query, limit=20):
    """Search news using FTS5 full-text search + LIKE fallback."""
    import re
    conn = get_connection()
    try:
        results = []
        seen_ids = set()

        # FTS5 search
        words = re.findall(r"[\u4e00-\u9fffA-Za-z0-9]+", query)
        if words:
            fts_query = " OR ".join([f"{w}*" for w in words])
            try:
                rows = conn.execute(
                    "SELECT n.* FROM news n JOIN news_fts ON n.id = news_fts.rowid "
                    "WHERE news_fts MATCH ? ORDER BY rank LIMIT ?",
                    (fts_query, limit)
                ).fetchall()
                for r in rows:
                    results.append(dict(r))
                    seen_ids.add(r["id"])
            except Exception:
                pass

        # LIKE fallback for content that FTS5 might miss
        like_pattern = f"%{query}%"
        rows2 = conn.execute(
            "SELECT * FROM news WHERE (title LIKE ? OR content LIKE ?) "
            "AND id NOT IN ({}) ORDER BY published_at DESC LIMIT ?".format(
                ",".join(str(i) for i in seen_ids) if seen_ids else "0"
            ),
            (like_pattern, like_pattern, limit - len(results))
        ).fetchall()
        for r in rows2:
            results.append(dict(r))

        return results[:limit]
    finally:
        conn.close()


def get_latest_news(limit=50):
    """Get latest news ordered by published_at."""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM news ORDER BY published_at DESC, id DESC LIMIT ?",
            (limit,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_news_by_stock(stock_code, limit=30):
    """Get news related to a specific stock code."""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT n.* FROM news n "
            "JOIN news_stock_rel r ON n.id = r.news_id "
            "WHERE r.stock_code = ? ORDER BY n.published_at DESC LIMIT ?",
            (stock_code, limit)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def insert_stock_rel(news_id, stock_codes):
    """Insert news-stock relationships."""
    if not stock_codes:
        return
    conn = get_connection()
    try:
        conn.executemany(
            "INSERT OR IGNORE INTO news_stock_rel (news_id, stock_code) VALUES (?, ?)",
            [(news_id, code) for code in stock_codes]
        )
        conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    init_db()
    print(f"Database initialized at {DB_PATH}")
