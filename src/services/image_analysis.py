"""Image analysis service — use MiniMax M3 vision model, fallback to tesseract OCR."""

import base64
import json
import logging
import mimetypes
import os
import urllib.request
from src.services.http_retry import retry_urlopen

logger = logging.getLogger(__name__)

MINIMAX_API_URL = "https://api.minimax.io/v1/chat/completions"

# Prompt for news image analysis — concise version
_ANALYSIS_PROMPT = """\
你是台股新聞摘要助理。請用繁體中文，簡潔扼要地摘要這張新聞圖片的重點。

規則：
- 一句話標題摘要
- 列出關鍵數據（如有）
- 標註影響屬性：🔴利多 / 🟢利空 / ⚪中性
- 提及的個股加上代號
- 總字數控制在150字以內
- 不要前綴說明，直接給摘要"""


def analyze_image_ai(image_data: bytes, filename: str = "") -> str | None:
    """
    Use MiniMax M3 vision model to analyze a news image.
    Returns extracted text/summary (max 200 chars), or None if AI unavailable.
    """
    api_key = os.getenv("MINIMAX_API_KEY", "")
    if not api_key:
        logger.warning("MINIMAX_API_KEY not set, AI image analysis unavailable")
        return None

    # Determine MIME type
    media_type = None
    if filename:
        media_type, _ = mimetypes.guess_type(filename)
    if not media_type:
        media_type = "image/jpeg"

    allowed = {"image/jpeg", "image/png", "image/gif", "image/webp"}
    if media_type not in allowed:
        media_type = "image/jpeg"

    # Check size limit (10MB)
    if len(image_data) > 10 * 1024 * 1024:
        logger.warning(f"Image too large for AI analysis: {len(image_data)} bytes")
        return None

    # Downscale image before encoding to reduce payload & CPU
    try:
        from PIL import Image
        import io
        img = Image.open(io.BytesIO(image_data))
        w, h = img.size
        max_dim = 1024
        if max(w, h) > max_dim:
            ratio = max_dim / max(w, h)
            img = img.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)
            buf = io.BytesIO()
            fmt = "JPEG" if media_type == "image/jpeg" else "PNG"
            img.save(buf, format=fmt, quality=80)
            image_data = buf.getvalue()
    except Exception as e:
        logger.warning(f"Image downscale failed, using original: {e}")

    # Encode to base64 data URL
    encoded = base64.b64encode(image_data).decode("ascii")
    data_url = f"data:{media_type};base64,{encoded}"

    payload = json.dumps({
        "model": "MiniMax-M3",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": _ANALYSIS_PROMPT},
                    {
                        "type": "image_url",
                        "image_url": {"url": data_url, "detail": "low"},
                    },
                ],
            }
        ],
        "max_tokens": 500,
        "temperature": 0.2,
        "thinking": {"type": "disabled"},
    }).encode()

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    try:
        req = urllib.request.Request(MINIMAX_API_URL, data=payload, method="POST", headers=headers)
        result = json.loads(retry_urlopen(req, timeout=30, max_retries=2))
        content = result["choices"][0]["message"]["content"].strip()
        # Remove <think>...</think> blocks if present
        import re
        content = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL).strip()
        if content:
            return content[:500]
        return None
    except Exception as e:
        logger.error(f"AI image analysis failed: {e}")
        return None


def analyze_image_ocr(image_data: bytes) -> str:
    """
    Fallback: use tesseract OCR to extract text from image.
    Returns extracted text (max 200 chars), or empty string.
    """
    try:
        import pytesseract
        from PIL import Image
        import io

        img = Image.open(io.BytesIO(image_data))
        # Downscale large images to reduce memory usage
        w, h = img.size
        max_pixels = 1600
        if max(w, h) > max_pixels:
            ratio = max_pixels / max(w, h)
            img = img.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)

        ocr_text = pytesseract.image_to_string(img, lang='chi_tra+eng').strip()
        return ocr_text[:200] if ocr_text else ""
    except Exception as e:
        logger.error(f"OCR failed: {e}")
        return ""
