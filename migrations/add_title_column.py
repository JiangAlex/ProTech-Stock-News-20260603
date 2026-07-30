"""Migration: Add 'title' column to watchlist_notes table."""
import psycopg2
from src.core.pg_client import DB_CONFIG


def migrate():
    conn = psycopg2.connect(**DB_CONFIG)
    try:
        cur = conn.cursor()
        # Add title column if not exists
        cur.execute("""
            ALTER TABLE watchlist_notes
            ADD COLUMN IF NOT EXISTS title TEXT DEFAULT NULL;
        """)
        conn.commit()
        print("✓ Migration complete: added 'title' column to watchlist_notes")
    finally:
        conn.close()


if __name__ == "__main__":
    migrate()
