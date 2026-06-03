"""UDN Money (經濟日報) news scraper service."""

import logging
from bs4 import BeautifulSoup
from src.core.scraper import BaseScraper
from src.core.database import insert_news

logger = logging.getLogger(__name__)

# Key categories on money.udn.com
CATEGORIES = {
    "5590": "證券",
    "5591": "產業",
    "5588": "國際",
    "12017": "金融",
    "10846": "要聞",
}

BASE_URL = "https://money.udn.com"


class UdnScraper(BaseScraper):
    """Scraper for UDN Money (經濟日報) news."""

    def __init__(self):
        super().__init__(delay=1.5)

    def fetch_news_list(self, cate_id):
        """Fetch news list from a category page. Returns list of dicts."""
        url = f"{BASE_URL}/money/cate/{cate_id}"
        resp = self.fetch(url)
        if not resp:
            return []

        soup = BeautifulSoup(resp.text, "lxml")
        articles = []
        seen_urls = set()

        for link in soup.select(f'a[href*="/money/story/"]'):
            href = link.get("href", "")
            if not href or href in seen_urls:
                continue
            # Ensure absolute URL
            if href.startswith("/"):
                href = BASE_URL + href

            title = link.get_text(strip=True)
            # Skip very short or navigation-only text
            if len(title) < 5:
                continue

            # Remove leading time prefix like "15:32 "
            import re
            title = re.sub(r"^\d{2}:\d{2}\s*", "", title)

            seen_urls.add(href)
            articles.append({"title": title, "url": href, "category": CATEGORIES.get(cate_id, "")})

        return articles

    def fetch_article(self, url):
        """Fetch full article content and publish time from article page."""
        resp = self.fetch(url)
        if not resp:
            return None, None

        soup = BeautifulSoup(resp.text, "lxml")

        # Extract title (h1)
        h1 = soup.select_one("h1")
        title = h1.get_text(strip=True) if h1 else None

        # Extract body text
        body = soup.select_one("section.article-body")
        content = ""
        if body:
            paragraphs = body.find_all("p")
            content = "\n".join(p.get_text(strip=True) for p in paragraphs if p.get_text(strip=True))

        # Extract publish time
        published_at = None
        time_el = soup.find("time")
        if time_el:
            published_at = time_el.get("datetime") or time_el.get_text(strip=True)
        if not published_at:
            meta = soup.find("meta", attrs={"property": "article:published_time"})
            if meta:
                published_at = meta.get("content")

        return content, published_at

    def scrape_all(self, max_per_category=20):
        """Scrape all categories and store to database. Returns count of new articles."""
        total_new = 0
        for cate_id, cate_name in CATEGORIES.items():
            logger.info(f"Scraping UDN category: {cate_name} ({cate_id})")
            articles = self.fetch_news_list(cate_id)

            for article in articles[:max_per_category]:
                content, published_at = self.fetch_article(article["url"])
                title = article["title"]
                news_id = insert_news(
                    title=title,
                    url=article["url"],
                    source="udn",
                    category=article["category"],
                    content=content,
                    published_at=published_at,
                )
                if news_id:
                    total_new += 1
                    logger.debug(f"  New: {title[:40]}")

        logger.info(f"UDN scrape done. New articles: {total_new}")
        return total_new


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    scraper = UdnScraper()
    count = scraper.scrape_all(max_per_category=5)
    print(f"Scraped {count} new articles from UDN")
