"""Telegram notification service."""

import logging
import urllib.request
import urllib.parse
import json

logger = logging.getLogger(__name__)


def send_telegram_message(bot_token: str, chat_id: str, message: str, parse_mode: str = "HTML") -> bool:
    """Send a message via Telegram Bot API.

    Automatically splits messages exceeding 4096 chars into multiple parts.
    """
    if not bot_token or not chat_id:
        logger.warning("Telegram bot_token or chat_id not configured, skipping")
        return False

    MAX_LEN = 4096

    if len(message) <= MAX_LEN:
        return _send_single_message(bot_token, chat_id, message, parse_mode)

    # Split long message into chunks at newline boundaries
    chunks = _split_message(message, MAX_LEN)
    success = True
    for i, chunk in enumerate(chunks):
        if not _send_single_message(bot_token, chat_id, chunk, parse_mode):
            success = False
            logger.error(f"Telegram chunk {i+1}/{len(chunks)} failed")
        else:
            logger.info(f"Telegram chunk {i+1}/{len(chunks)} sent")
        # Small delay between chunks to avoid rate limit
        if i < len(chunks) - 1:
            import time
            time.sleep(0.5)

    return success


def _split_message(text: str, max_len: int) -> list[str]:
    """Split text into chunks at newline boundaries."""
    chunks = []
    while text:
        if len(text) <= max_len:
            chunks.append(text)
            break

        # Find last newline before max_len
        split_pos = text.rfind("\n", 0, max_len)
        if split_pos <= 0:
            # No newline found, hard cut
            split_pos = max_len

        chunks.append(text[:split_pos])
        text = text[split_pos:].lstrip("\n")

    return chunks


def _send_single_message(bot_token: str, chat_id: str, message: str, parse_mode: str = "HTML") -> bool:
    """Send a single message (must be <= 4096 chars)."""
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode

    data = urllib.parse.urlencode(payload).encode("utf-8")

    try:
        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
            if result.get("ok"):
                return True
            else:
                logger.error(f"Telegram API error: {result}")
                # If HTML parse fails, retry without parse_mode
                if "can't parse" in str(result).lower() and parse_mode:
                    logger.info("Retrying without HTML parse_mode...")
                    return _send_single_message(bot_token, chat_id, message, parse_mode="")
                return False
    except urllib.error.HTTPError as e:
        error_body = e.read().decode() if e.fp else ""
        logger.error(f"Telegram HTTP {e.code}: {error_body[:200]}")
        # If HTML parse error, retry without parse_mode
        if "can't parse" in error_body.lower() and parse_mode:
            logger.info("Retrying without HTML parse_mode...")
            return _send_single_message(bot_token, chat_id, message, parse_mode="")
        return False
    except Exception as e:
        logger.error(f"Telegram send failed: {e}")
        return False
