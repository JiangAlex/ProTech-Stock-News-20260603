"""
HTTP retry utility with exponential backoff.

Provides retry_urlopen() as a drop-in enhancement for urllib.request.urlopen,
adding automatic retry with exponential backoff for transient failures
(timeouts, network errors, server 5xx).
"""

import time
import logging
import urllib.request
import urllib.error
from typing import Optional

logger = logging.getLogger(__name__)

# Errors that are worth retrying
_RETRYABLE_EXCEPTIONS = (
    TimeoutError,
    ConnectionError,
    urllib.error.URLError,
)

# HTTP status codes worth retrying
_RETRYABLE_HTTP_CODES = {429, 500, 502, 503, 504}


def retry_urlopen(
    req: urllib.request.Request,
    *,
    timeout: int = 30,
    max_retries: int = 2,
    base_delay: float = 5.0,
    max_delay: float = 60.0,
    backoff_factor: float = 2.0,
) -> bytes:
    """Execute urllib.request.urlopen with retry and exponential backoff.

    Args:
        req: The prepared urllib.request.Request object.
        timeout: Socket timeout in seconds.
        max_retries: Maximum number of retry attempts (0 = no retry).
        base_delay: Initial delay between retries in seconds.
        max_delay: Maximum delay cap in seconds.
        backoff_factor: Multiplier for delay on each retry.

    Returns:
        Response body as bytes.

    Raises:
        The last exception encountered if all retries are exhausted.
    """
    last_exception: Optional[Exception] = None

    for attempt in range(max_retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()

        except urllib.error.HTTPError as e:
            # Only retry on retryable HTTP status codes
            if e.code in _RETRYABLE_HTTP_CODES and attempt < max_retries:
                delay = min(base_delay * (backoff_factor ** attempt), max_delay)
                logger.warning(
                    f"HTTP {e.code} on attempt {attempt + 1}/{max_retries + 1}, "
                    f"retrying in {delay:.1f}s..."
                )
                last_exception = e
                time.sleep(delay)
                continue
            raise

        except _RETRYABLE_EXCEPTIONS as e:
            last_exception = e
            if attempt < max_retries:
                delay = min(base_delay * (backoff_factor ** attempt), max_delay)
                reason = str(e)
                if hasattr(e, 'reason'):
                    reason = str(e.reason)
                logger.warning(
                    f"Request failed ({reason}) on attempt {attempt + 1}/{max_retries + 1}, "
                    f"retrying in {delay:.1f}s..."
                )
                time.sleep(delay)
                continue
            raise

    # Should not reach here, but just in case
    if last_exception:
        raise last_exception
