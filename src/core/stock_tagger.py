"""Stock tagger: identify stock codes/names in news text."""

import re
import logging
from src.core.pg_client import get_all_stocks
from src.core.database import get_connection, insert_stock_rel

logger = logging.getLogger(__name__)

# Cache for stock data
_stock_cache = None


def _load_stock_data():
    """Load stock code/name mapping from PostgreSQL (cached)."""
    global _stock_cache
    if _stock_cache is not None:
        return _stock_cache

    stocks = get_all_stocks()
    # Build lookup: code -> name, name -> code
    code_set = set()
    name_to_code = {}
    for code, name in stocks:
        code_set.add(code)
        # Only use names with 2+ chars to avoid false positives
        if len(name) >= 2:
            name_to_code[name] = code

    _stock_cache = (code_set, name_to_code)
    logger.info(f"Loaded {len(code_set)} stock codes, {len(name_to_code)} names")
    return _stock_cache


def tag_text(text):
    """Identify stock codes/names in text. Returns set of stock_codes."""
    if not text:
        return set()

    code_set, name_to_code = _load_stock_data()
    matched = set()

    # Match 4-digit stock codes (e.g., 2330, (2330), 「2330」)
    for m in re.finditer(r"(?<!\d)(\d{4})(?!\d)", text):
        code = m.group(1)
        if code in code_set:
            matched.add(code)

    # Match stock names (longer names first to avoid partial matches)
    # Sort by length descending so "聯發科" matches before "聯發"
    for name, code in sorted(name_to_code.items(), key=lambda x: len(x[0]), reverse=True):
        if name in text:
            # For 2-char names, require word boundary (not part of a longer name)
            if len(name) == 2:
                # Check if this 2-char name is actually part of a longer matched name
                skip = False
                for longer_name in name_to_code:
                    if len(longer_name) > 2 and name in longer_name and longer_name in text:
                        skip = True
                        break
                if skip:
                    continue
            matched.add(code)

    return matched


def tag_news(news_id, title, content=None):
    """Tag a single news article with stock codes."""
    text = title or ""
    if content:
        text += " " + content
    codes = tag_text(text)
    if codes:
        insert_stock_rel(news_id, codes)
    return codes


def tag_all_untagged():
    """Tag all news that don't have stock relations yet. Returns total tagged count."""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT n.id, n.title, n.content FROM news n "
            "WHERE n.id NOT IN (SELECT DISTINCT news_id FROM news_stock_rel)"
        ).fetchall()
    finally:
        conn.close()

    total = 0
    for row in rows:
        codes = tag_news(row["id"], row["title"], row["content"])
        if codes:
            total += 1

    logger.info(f"Tagged {total} news with stock relations (out of {len(rows)} untagged)")
    return total


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    count = tag_all_untagged()
    print(f"Tagged {count} news articles")
