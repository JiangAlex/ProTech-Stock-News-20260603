"""Yahoo Stock (tw.stock.yahoo.com) news scraper service."""

import logging
import re
from bs4 import BeautifulSoup
from src.core.scraper import BaseScraper
from src.core.database import insert_news

logger = logging.getLogger(__name__)

BASE_URL = "https://tw.stock.yahoo.com"


def fetch_hot_stocks():
    """Fetch community hot stocks ranking from Yahoo Stock."""
    import requests
    resp = requests.get(
        f"{BASE_URL}/community/rank/active",
        headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"},
        timeout=10,
    )
    if resp.status_code != 200:
        return []

    soup = BeautifulSoup(resp.text, "lxml")
    code_spans = soup.find_all("span", string=re.compile(r"^\d{4}\.TW$"))

    results = []
    for rank, span in enumerate(code_spans, 1):
        code = span.get_text(strip=True).replace(".TW", "")
        parent_div = span.parent
        name = ""
        if parent_div and parent_div.parent:
            all_text = parent_div.parent.get_text(" ", strip=True)
            name_match = re.search(r"([\u4e00-\u9fff\w*]+)\s+" + code, all_text)
            name = name_match.group(1) if name_match else ""
        results.append({"rank": rank, "code": code, "name": name})

    return results


class YahooScraper(BaseScraper):
    """Scraper for Yahoo Stock news."""

    def __init__(self):
        super().__init__(delay=1.5)

    def fetch_news_list(self):
        """Fetch news list from Yahoo Stock news page. Returns list of dicts."""
        resp = self.fetch(f"{BASE_URL}/news")
        if not resp:
            return []

        soup = BeautifulSoup(resp.text, "lxml")
        articles = []
        seen_urls = set()

        for link in soup.select('a[href*="/news/"]'):
            href = link.get("href", "")
            if not href.startswith("/news/") or len(href) < 15:
                continue

            full_url = BASE_URL + href
            if full_url in seen_urls:
                continue

            title = link.get_text(strip=True)
            if len(title) < 8:
                continue

            seen_urls.add(full_url)
            articles.append({"title": title, "url": full_url})

        return articles

    def fetch_article(self, url):
        """Fetch full article content from Yahoo Stock article page."""
        resp = self.fetch(url)
        if not resp:
            return None, None, None

        soup = BeautifulSoup(resp.text, "lxml")

        # Title
        h1 = soup.find("h1")
        title = h1.get_text(strip=True) if h1 else None

        # Body content
        content = ""
        article = soup.find("article")
        if article:
            paragraphs = article.find_all("p")
            content = "\n".join(
                p.get_text(strip=True) for p in paragraphs
                if p.get_text(strip=True) and len(p.get_text(strip=True)) > 10
            )

        # Publish time
        published_at = None
        time_el = soup.find("time")
        if time_el:
            published_at = time_el.get("datetime", time_el.get_text(strip=True))

        return content, published_at, title

    def scrape_all(self, max_articles=30):
        """Scrape Yahoo Stock news and store to database. Returns count of new articles."""
        logger.info("Scraping Yahoo Stock news")
        articles = self.fetch_news_list()
        total_new = 0

        for article in articles[:max_articles]:
            content, published_at, full_title = self.fetch_article(article["url"])
            title = full_title or article["title"]

            news_id = insert_news(
                title=title,
                url=article["url"],
                source="yahoo",
                category="股市",
                content=content,
                published_at=published_at,
            )
            if news_id:
                total_new += 1
                logger.debug(f"  New: {title[:40]}")

        logger.info(f"Yahoo scrape done. New articles: {total_new}")
        return total_new


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    scraper = YahooScraper()
    count = scraper.scrape_all(max_articles=5)
    print(f"Scraped {count} new articles from Yahoo Stock")
