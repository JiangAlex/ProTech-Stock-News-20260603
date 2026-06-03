"""CTEE (工商時報) news scraper service.

Uses Google News RSS as data source since ctee.com.tw is behind strict
Cloudflare protection that blocks automated requests.
"""

import logging
import re
from datetime import datetime
from email.utils import parsedate_to_datetime

from bs4 import BeautifulSoup
from src.core.scraper import BaseScraper
from src.core.database import insert_news

logger = logging.getLogger(__name__)

GOOGLE_NEWS_RSS = (
    "https://news.google.com/rss/search?"
    "q=site:ctee.com.tw&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
)


class CteeScraper(BaseScraper):
    """Scraper for CTEE (工商時報) news via Google News RSS."""

    def __init__(self):
        super().__init__(delay=1.0)

    def fetch_news_list(self):
        """Fetch CTEE news list from Google News RSS. Returns list of dicts."""
        resp = self.fetch(GOOGLE_NEWS_RSS)
        if not resp:
            return []

        soup = BeautifulSoup(resp.text, "xml")
        articles = []

        for item in soup.find_all("item"):
            title_el = item.find("title")
            pub_el = item.find("pubDate")
            link_el = item.find("link")
            source_el = item.find("source")

            if not title_el:
                continue

            # Clean title: remove trailing " - 工商時報" or category markers
            title = title_el.get_text(strip=True)
            title = re.sub(r"\s*-\s*(證券|產業|國際|金融|科技|政經|房產)\s*-\s*工商時報$", "", title)
            title = re.sub(r"\s*-\s*工商時報$", "", title)

            # Extract category from original title
            cat_match = re.search(r"-\s*(證券|產業|國際|金融|科技|政經|房產)\s*-\s*工商時報", title_el.get_text())
            category = cat_match.group(1) if cat_match else ""

            # Parse publish date
            published_at = None
            if pub_el:
                try:
                    dt = parsedate_to_datetime(pub_el.get_text())
                    published_at = dt.strftime("%Y/%m/%d %H:%M:%S")
                except Exception:
                    pass

            # Use Google News link as URL (original ctee URL not accessible)
            url = link_el.get_text(strip=True) if link_el else ""

            articles.append({
                "title": title,
                "url": url,
                "category": category,
                "published_at": published_at,
            })

        return articles

    def scrape_all(self, max_articles=50):
        """Scrape CTEE news and store to database. Returns count of new articles."""
        logger.info("Scraping CTEE via Google News RSS")
        articles = self.fetch_news_list()
        total_new = 0

        for article in articles[:max_articles]:
            news_id = insert_news(
                title=article["title"],
                url=article["url"],
                source="ctee",
                category=article["category"],
                content=article["title"],  # Only title available without JS rendering
                published_at=article["published_at"],
            )
            if news_id:
                total_new += 1

        logger.info(f"CTEE scrape done. New articles: {total_new}")
        return total_new


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    scraper = CteeScraper()
    count = scraper.scrape_all()
    print(f"Scraped {count} new articles from CTEE")
