"""Image analysis service — use MiniMax M3 vision model, fallback to tesseract OCR."""

import base64
import json
import logging
import mimetypes
import os
import urllib.request

logger = logging.getLogger(__name__)

MINIMAX_API_URL = "https://api.minimax.io/v1/chat/completions"

# Prompt for news image analysis
_ANALYSIS_PROMPT = """\
[角色任務]：你是一名專業台股新聞圖片分析助理，負責將新聞圖片資料轉化為結構化的股市研判摘要。
[背景資訊]：使用者上傳包含台股財經新聞、產業報導或個股數據表格的圖片，需要迅速掌握市場重點、個股動向、籌碼狀態與技術面位階。
[具體指令]：
1. 完整辨識並逐字擷取圖片中的新聞標題、內文與關鍵數據。
2. 整理核心新聞主題與關鍵影響因子，並標註新聞內個股屬性（「🔴 利多」、「🟢 利空」或「⚪ 中性」）。
3. 根據新聞內文提及之數據，分析法人籌碼動向（如外資、投信買賣超金額/張數）與技術面位階（如股價相對於均線位置、創高或跌深等）。
4. 根據新聞主題進行產業延伸推演，主動列出未在新聞中提及但可能受連動影響的「上中下游供應鏈與相關概念股」（含名稱與四位數代號）。
5. 只回傳摘要內容，不要前綴說明。
[約束條件]：請全程使用繁體中文回覆，專業術語保持英文（如 QoQ、YoY、EPS、ASIC、CoWoS），格式採用清晰的標題與條列點呈現。"""


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
                        "image_url": {"url": data_url, "detail": "high"},
                    },
                ],
            }
        ],
        "max_tokens": 4000,
        "temperature": 0.2,
        "thinking": {"type": "disabled"},
    }).encode()

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    try:
        req = urllib.request.Request(MINIMAX_API_URL, data=payload, method="POST", headers=headers)
        with urllib.request.urlopen(req, timeout=30) as r:
            result = json.loads(r.read())
            content = result["choices"][0]["message"]["content"].strip()
            # Remove <think>...</think> blocks if present
            import re
            content = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL).strip()
            if content:
                return content[:4000]
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
