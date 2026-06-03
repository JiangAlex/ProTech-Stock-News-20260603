"""Base scraper with rate-limiting, retry, and common HTTP utilities."""

import time
import logging
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
}


class BaseScraper:
    """Base scraper with rate-limiting and retry logic."""

    def __init__(self, delay=1.5, max_retries=3, timeout=15):
        self.delay = delay
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)

        retry_strategy = Retry(
            total=max_retries,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self._last_request_time = 0

    def _rate_limit(self):
        """Enforce minimum delay between requests."""
        elapsed = time.time() - self._last_request_time
        if elapsed < self.delay:
            time.sleep(self.delay - elapsed)
        self._last_request_time = time.time()

    def fetch(self, url, **kwargs):
        """Fetch a URL with rate-limiting and error handling."""
        self._rate_limit()
        try:
            resp = self.session.get(url, timeout=self.timeout, **kwargs)
            resp.raise_for_status()
            return resp
        except requests.RequestException as e:
            logger.warning(f"Request failed for {url}: {e}")
            return None
