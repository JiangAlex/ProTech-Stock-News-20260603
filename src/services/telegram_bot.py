"""Telegram Bot — 雙向互動（Inline Keyboard 選單）.

Long polling 接收群組訊息與 callback_query，分派到對應 handler。
"""

import asyncio
import io
import json
import logging
import os
import urllib.parse
import urllib.request
from datetime import date, datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
# Load from DB (default user) with env var fallback
def _load_bot_config():
    """Load Telegram bot config from DB, fallback to env vars."""
    try:
        from src.core.database import get_alert_settings
        settings = get_alert_settings("default")
        token = settings.get("telegram_bot_token", "") or ""
        chat_id = settings.get("telegram_chat_id", "") or ""
        return token, chat_id
    except Exception:
        return os.getenv("TELEGRAM_BOT_TOKEN", ""), os.getenv("TELEGRAM_CHAT_ID", "")


# Module-level config (loaded lazily on first use)
_bot_config_loaded = False
BOT_TOKEN = ""
GROUP_CHAT_ID = ""


def _ensure_config():
    """Lazy-load bot config on first use."""
    global _bot_config_loaded, BOT_TOKEN, GROUP_CHAT_ID
    if not _bot_config_loaded:
        BOT_TOKEN, GROUP_CHAT_ID = _load_bot_config()
        _bot_config_loaded = True


def reload_bot_config():
    """Reload bot config from DB (called when settings are updated)."""
    global _bot_config_loaded, BOT_TOKEN, GROUP_CHAT_ID
    BOT_TOKEN, GROUP_CHAT_ID = _load_bot_config()
    _bot_config_loaded = True

# Telegram user_id → app username mapping (via DB)
def get_app_user(tg_user_id: int) -> str:
    """Map Telegram user_id to app username via DB. Falls back to 'default'."""
    from src.core.database import get_bound_app_user
    return get_bound_app_user(tg_user_id)


# Per-user state for multi-step interactions
# user_states[user_id] = {"action": "...", ...}
user_states: Dict[int, dict] = {}

# Per-user scan conditions (multi-select)
# scan_states[user_id] = {"rsi_max": 30, "ma_arrangement": "多頭排列", ...}
scan_states: Dict[int, dict] = {}


# ---------------------------------------------------------------------------
# Telegram API Helpers
# ---------------------------------------------------------------------------

def _api_call(method: str, data: dict = None, files: dict = None) -> Optional[dict]:
    """Call Telegram Bot API. Supports JSON data or multipart (for sendPhoto)."""
    _ensure_config()
    if not BOT_TOKEN:
        logger.warning("TELEGRAM_BOT_TOKEN not set")
        return None

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"

    try:
        if files:
            # Multipart form-data for file upload
            boundary = "----PythonBotBoundary"
            body = b""
            for key, val in (data or {}).items():
                if val is None:
                    continue
                body += f"--{boundary}\r\n".encode()
                body += f"Content-Disposition: form-data; name=\"{key}\"\r\n\r\n".encode()
                body += f"{val}\r\n".encode()
            for key, (filename, file_bytes, content_type) in files.items():
                body += f"--{boundary}\r\n".encode()
                body += f"Content-Disposition: form-data; name=\"{key}\"; filename=\"{filename}\"\r\n".encode()
                body += f"Content-Type: {content_type}\r\n\r\n".encode()
                body += file_bytes
                body += b"\r\n"
            body += f"--{boundary}--\r\n".encode()

            req = urllib.request.Request(url, data=body, method="POST")
            req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
        else:
            # JSON request
            payload = json.dumps(data or {}).encode("utf-8")
            req = urllib.request.Request(url, data=payload, method="POST")
            req.add_header("Content-Type", "application/json")

        with urllib.request.urlopen(req, timeout=60) as resp:
            result = json.loads(resp.read())
            if not result.get("ok"):
                logger.error(f"Telegram API {method} error: {result}")
            return result
    except Exception as e:
        logger.error(f"Telegram API {method} failed: {e}")
        return None


def send_message(chat_id, text: str, reply_markup: dict = None,
                 parse_mode: str = "HTML", reply_to: int = None) -> Optional[dict]:
    """Send a text message, optionally with inline keyboard."""
    data = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
    }
    if reply_markup:
        data["reply_markup"] = json.dumps(reply_markup)
    if reply_to:
        data["reply_to_message_id"] = reply_to
    return _api_call("sendMessage", data)


def send_photo(chat_id, photo_bytes: bytes, caption: str = "",
               reply_markup: dict = None) -> Optional[dict]:
    """Send a photo (PNG bytes) with optional caption and inline keyboard."""
    data = {
        "chat_id": str(chat_id),
        "caption": caption,
        "parse_mode": "HTML",
    }
    if reply_markup:
        data["reply_markup"] = json.dumps(reply_markup)
    files = {
        "photo": ("chart.png", photo_bytes, "image/png"),
    }
    return _api_call("sendPhoto", data, files)


def answer_callback_query(callback_query_id: str, text: str = "", show_alert: bool = False):
    """Answer a callback query (dismiss the loading indicator)."""
    _api_call("answerCallbackQuery", {
        "callback_query_id": callback_query_id,
        "text": text,
        "show_alert": show_alert,
    })


def edit_message_text(chat_id, message_id: int, text: str,
                      reply_markup: dict = None, parse_mode: str = "HTML"):
    """Edit an existing message's text and/or keyboard."""
    data = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": parse_mode,
    }
    if reply_markup:
        data["reply_markup"] = json.dumps(reply_markup)
    return _api_call("editMessageText", data)


def edit_message_reply_markup(chat_id, message_id: int, reply_markup: dict):
    """Edit only the inline keyboard of a message."""
    return _api_call("editMessageReplyMarkup", {
        "chat_id": chat_id,
        "message_id": message_id,
        "reply_markup": json.dumps(reply_markup),
    })


# ---------------------------------------------------------------------------
# Inline Keyboard Builders
# ---------------------------------------------------------------------------

def build_keyboard(buttons: List[List[tuple]]) -> dict:
    """Build InlineKeyboardMarkup from list of rows.

    Each row is a list of (text, callback_data) tuples.
    """
    return {
        "inline_keyboard": [
            [{"text": text, "callback_data": cb} for text, cb in row]
            for row in buttons
        ]
    }


def main_menu_keyboard() -> dict:
    """Main menu inline keyboard."""
    return build_keyboard([
        [("📊 查股票", "stock"), ("📋 自選股", "wl")],
        [("📡 掃描", "scan"), ("💼 持股分析", "portfolio")],
        [("📰 新聞", "news"), ("❓ 幫助", "help")],
    ])


def persistent_reply_keyboard() -> dict:
    """Persistent ReplyKeyboardMarkup — always visible at bottom of chat."""
    return {
        "keyboard": [
            [{"text": "📊 查股票"}, {"text": "📋 自選股"}],
            [{"text": "📡 掃描"}, {"text": "💼 持股分析"}],
            [{"text": "📰 新聞"}, {"text": "❓ 幫助"}],
        ],
        "resize_keyboard": True,
        "is_persistent": True,
    }


# ---------------------------------------------------------------------------
# Message Dispatch
# ---------------------------------------------------------------------------

async def handle_message(message: dict):
    """Handle an incoming message (text command or general discussion)."""
    text = message.get("text", "")
    chat_id = message["chat"]["id"]
    user_id = message["from"]["id"]
    user_name = message["from"].get("first_name", "") or message["from"].get("username", "")

    # Check if bot is mentioned (for @Bot queries)
    bot_mentioned = False
    entities = message.get("entities", [])
    for ent in entities:
        if ent.get("type") == "mention":
            bot_mentioned = True
            break

    # Commands
    if text.startswith("/menu") or text.startswith("/start"):
        send_message(chat_id, "📈 <b>SoftSnail</b> — 請選擇功能：",
                     reply_markup=persistent_reply_keyboard())
        return

    if text.startswith("/bind"):
        # /bind <app_username> — bind this Telegram account to an app user
        parts = text.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            current = get_app_user(user_id)
            msg = (f"🔗 目前綁定：<b>{current}</b>\n\n"
                   f"用法：<code>/bind 帳號名稱</code>\n"
                   f"例如：<code>/bind alex</code>")
            send_message(chat_id, msg)
            return
        app_username = parts[1].strip()
        tg_username = message["from"].get("username", "")
        from src.core.database import bind_telegram_user
        bind_telegram_user(user_id, app_username, tg_username)
        send_message(chat_id, f"✅ 已綁定 Telegram → <b>{app_username}</b>")
        return

    # Handle persistent reply keyboard button presses
    if text == "📊 查股票":
        user_states[user_id] = {"action": "waiting_stock_code"}
        user_states[chat_id] = {"action": "waiting_stock_code"}
        send_message(chat_id, "📊 請輸入股票代號：",
                     reply_markup=persistent_reply_keyboard())
        return
    elif text == "📋 自選股":
        await _cb_watchlist_entry(chat_id, None, user_id)
        return
    elif text == "📡 掃描":
        await _cb_scan_entry(chat_id, None, user_id)
        return
    elif text == "💼 持股分析":
        await _cb_portfolio(chat_id, None, user_id)
        return
    elif text == "📰 新聞":
        await _cb_news_entry(chat_id, None, user_id)
        return
    elif text == "❓ 幫助":
        _cb_help(chat_id, None)
        return

    # Check user state (waiting for input)
    # Also check chat-level state for cases where user sends as channel identity
    state = user_states.get(user_id) or user_states.get(chat_id)
    if state:
        # Clean up both possible keys
        user_states.pop(user_id, None)
        user_states.pop(chat_id, None)
        await _handle_user_state(message, state)
        return

    # Fallback: if user is replying to bot's ForceReply message, check state
    reply_to = message.get("reply_to_message")
    if reply_to and reply_to.get("from", {}).get("is_bot"):
        # User replied to bot but state may have been cleared or not set
        # Try to infer action from the bot's original message
        bot_text = reply_to.get("text", "")
        if "股票代號" in bot_text:
            code = text.upper().replace(" ", "")
            await _send_stock_chart(chat_id, code)
            return
        elif "備註" in bot_text:
            # Try to find stock code from prior message context
            pass

    # @Bot mention → AI semantic search
    if bot_mentioned:
        await _handle_mention_query(message, text)
        return

    # General message → save to discussion archive
    await _save_discussion(message)


async def handle_callback_query(callback_query: dict):
    """Handle inline keyboard button press."""
    data = callback_query.get("data", "")
    chat_id = callback_query["message"]["chat"]["id"]
    message_id = callback_query["message"]["message_id"]
    user_id = callback_query["from"]["id"]
    callback_id = callback_query["id"]

    # Always answer callback to remove loading state
    answer_callback_query(callback_id)

    # Route by callback_data prefix
    if data == "menu":
        result = edit_message_text(chat_id, message_id,
                          "📈 <b>SoftSnail</b> — 請選擇功能：",
                          reply_markup=main_menu_keyboard())
        if result is None:
            # edit failed (e.g. photo message can't be edited to text)
            send_message(chat_id, "📈 <b>SoftSnail</b> — 請選擇功能：",
                         reply_markup=main_menu_keyboard())

    elif data == "stock":
        await _cb_stock_entry(chat_id, message_id, user_id)

    elif data.startswith("stock_add_"):
        await _cb_stock_add(chat_id, message_id, user_id, data)

    elif data.startswith("stock_ai_"):
        await _cb_stock_ai(chat_id, message_id, user_id, data)

    elif data == "wl":
        await _cb_watchlist_entry(chat_id, message_id, user_id)

    elif data.startswith("wl_group_"):
        await _cb_watchlist_group(chat_id, message_id, user_id, data)

    elif data.startswith("wl_stock_"):
        await _cb_watchlist_stock(chat_id, message_id, user_id, data)

    elif data.startswith("wl_note_"):
        await _cb_watchlist_note(chat_id, message_id, user_id, data)

    elif data.startswith("wl_chart_"):
        await _cb_watchlist_chart(chat_id, message_id, user_id, data)

    elif data.startswith("wl_ai_"):
        await _cb_watchlist_ai(chat_id, message_id, user_id, data)

    elif data == "scan":
        await _cb_scan_entry(chat_id, message_id, user_id)

    elif data.startswith("scan_cat_"):
        await _cb_scan_category(chat_id, message_id, user_id, data)

    elif data.startswith("scan_opt_"):
        await _cb_scan_option(chat_id, message_id, user_id, data)

    elif data == "scan_run":
        await _cb_scan_run(chat_id, message_id, user_id)

    elif data == "scan_clear":
        scan_states.pop(user_id, None)
        await _cb_scan_entry(chat_id, message_id, user_id)

    elif data == "portfolio":
        await _cb_portfolio(chat_id, message_id, user_id)

    elif data == "news":
        await _cb_news_entry(chat_id, message_id, user_id)

    elif data == "news_titles" or data.startswith("news_titles_"):
        news_date = data.replace("news_titles_", "") if "_" in data else None
        await _cb_news_titles(chat_id, message_id, user_id, news_date)

    elif data == "news_ai" or data.startswith("news_ai_"):
        news_date = data.replace("news_ai_", "") if "_" in data else None
        await _cb_news_ai(chat_id, message_id, user_id, news_date)

    elif data.startswith("news_cat_"):
        # news_cat_{key}_{date} e.g. news_cat_ai_2026-08-04, news_cat_titles_2026-08-03
        parts = data[len("news_cat_"):]  # e.g. "ai_2026-08-04" or "other_經濟日報_2026-08-03"
        # Date is always last 10 chars (YYYY-MM-DD)
        news_date = parts[-10:]
        key = parts[:-11]  # remove _date
        if key == "ai":
            await _cb_news_ai(chat_id, message_id, user_id, news_date)
        elif key == "titles":
            await _cb_news_titles(chat_id, message_id, user_id, news_date)
        else:
            # Other category like "other_經濟日報"
            cat_name = key.replace("other_", "", 1)
            await _cb_news_other(chat_id, message_id, user_id, news_date, cat_name)

    elif data.startswith("news_date_"):
        # Layer 2: show categories for that date
        target_date = data.replace("news_date_", "")
        await _cb_news_date(chat_id, message_id, user_id, target_date)

    elif data == "help":
        _cb_help(chat_id, message_id)


# ---------------------------------------------------------------------------
# User State Handlers
# ---------------------------------------------------------------------------

async def _handle_user_state(message: dict, state: dict):
    """Handle message when user is in a waiting state."""
    text = message.get("text", "").strip()
    chat_id = message["chat"]["id"]
    user_id = message["from"]["id"]

    action = state.get("action")

    if action == "waiting_stock_code":
        # User entered stock code for 查股票
        user_states.pop(user_id, None)
        user_states.pop(chat_id, None)
        code = text.upper().replace(" ", "")
        await _send_stock_chart(chat_id, code)

    elif action == "waiting_note":
        # User entered note content
        user_states.pop(user_id, None)
        user_states.pop(chat_id, None)
        stock_code = state.get("stock_code", "")
        await _save_note(chat_id, user_id, stock_code, text)

    else:
        user_states.pop(user_id, None)


# ---------------------------------------------------------------------------
# Callback Handlers (stubs — will be implemented in subsequent tasks)
# ---------------------------------------------------------------------------

async def _cb_stock_entry(chat_id, message_id, user_id):
    """Ask user to input stock code."""
    user_states[user_id] = {"action": "waiting_stock_code"}
    user_states[chat_id] = {"action": "waiting_stock_code"}
    # Edit original message to show status
    edit_message_text(chat_id, message_id,
                      "📊 查股票 — 等待輸入代號...",
                      reply_markup=build_keyboard([[("🔙 取消", "menu")]]))
    # Send new message with ForceReply so bot can receive the reply in group chats
    # Send prompt while keeping persistent keyboard visible
    send_message(chat_id, "📊 請輸入股票代號：",
                 reply_markup=persistent_reply_keyboard())


async def _send_stock_chart(chat_id, code: str):
    """Generate MA chart and send to user."""
    try:
        from src.core.pg_client import get_daily_kline, get_all_stocks
        from src.services.chart_service import generate_ma_chart

        # Get stock name
        stocks = get_all_stocks()
        stock_name = ""
        for s in stocks:
            if s["code"] == code:
                stock_name = s["name"]
                break

        # Get K-line data
        kline = get_daily_kline(code, days=120)
        if not kline:
            send_message(chat_id, f"❌ 找不到 {code} 的資料")
            return

        # Generate chart
        chart_bytes = generate_ma_chart(kline, code, stock_name)
        if not chart_bytes:
            send_message(chat_id, f"❌ {code} 圖表生成失敗")
            return

        # Send photo with action buttons
        keyboard = build_keyboard([
            [("➕ 加自選", f"stock_add_{code}"), ("🤖 AI分析", f"stock_ai_{code}")],

        ])
        send_photo(chat_id, chart_bytes,
                   caption=f"📊 <b>{code} {stock_name}</b> — MA 均線圖",
                   reply_markup=keyboard)
    except Exception as e:
        logger.error(f"Stock chart failed for {code}: {e}")
        send_message(chat_id, f"❌ 查詢 {code} 失敗：{e}")


async def _cb_stock_add(chat_id, message_id, user_id, data: str):
    """Add stock to watchlist."""
    code = data.replace("stock_add_", "")
    try:
        from src.core.database import add_watchlist
        from src.core.pg_client import get_all_stocks

        # Get stock name
        stocks = get_all_stocks()
        stock_name = ""
        for s in stocks:
            if s["code"] == code:
                stock_name = s["name"]
                break

        add_watchlist(code, stock_name, "預設", get_app_user(user_id))
        send_message(chat_id, f"✅ 已將 <b>{code} {stock_name}</b> 加入自選股（預設群組）")
    except Exception as e:
        logger.error(f"Add watchlist failed for {code}: {e}")
        send_message(chat_id, f"❌ 加入自選失敗：{e}")


async def _cb_stock_ai(chat_id, message_id, user_id, data: str):
    """Run AI analysis on stock."""
    code = data.replace("stock_ai_", "")
    send_message(chat_id, f"🤖 正在分析 <b>{code}</b>，請稍候（約 10-30 秒）...")

    try:
        from src.core.pg_client import get_daily_kline
        from src.services.kline_analysis import analyze_kline

        kline = get_daily_kline(code, days=120)
        if not kline:
            send_message(chat_id, f"❌ 找不到 {code} 的資料")
            return

        # Get stock name
        from src.core.pg_client import get_all_stocks as _get_stocks
        _stocks = _get_stocks()
        _stock_name = ""
        for s in _stocks:
            if s["code"] == code:
                _stock_name = s["name"]
                break
        result = analyze_kline(code, _stock_name, kline, user_id=get_app_user(user_id))
        if result and result.get("analysis"):
            analysis_text = result["analysis"]
            # Telegram message limit is 4096 chars
            if len(analysis_text) > 3800:
                analysis_text = analysis_text[:3800] + "\n\n... (內容過長已截斷)"
            # Escape HTML special chars in AI output to avoid 400 Bad Request
            import html as _html
            safe_text = _html.escape(analysis_text)
            resp = send_message(chat_id, f"🤖 <b>{code} AI 技術分析</b>\n\n{safe_text}",
                         reply_markup=None)
            # Fallback: if HTML parse failed, retry without parse_mode
            if resp is None:
                send_message(chat_id, f"🤖 {code} AI 技術分析\n\n{analysis_text}",
                             reply_markup=None, parse_mode="")
        else:
            send_message(chat_id, f"❌ {code} AI 分析無結果")
    except Exception as e:
        logger.error(f"AI analysis failed for {code}: {e}")
        send_message(chat_id, f"❌ AI 分析失敗：{e}")


async def _cb_watchlist_entry(chat_id, message_id, user_id):
    """Show watchlist groups as buttons."""
    try:
        from src.core.database import get_all_groups
        groups = get_all_groups(get_app_user(user_id))
        if not groups:
            groups = ["預設"]

        rows = []
        row = []
        for g in groups:
            row.append((f"📁 {g}", f"wl_group_{g}"))
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)

        kb = build_keyboard(rows)
        text = "📋 <b>自選股</b> — 選擇分組："
        if message_id:
            edit_message_text(chat_id, message_id, text, reply_markup=kb)
        else:
            send_message(chat_id, text, reply_markup=kb)
    except Exception as e:
        logger.error(f"Watchlist entry failed: {e}")
        err_kb = None
        if message_id:
            edit_message_text(chat_id, message_id, f"❌ 載入失敗：{e}", reply_markup=err_kb)
        else:
            send_message(chat_id, f"❌ 載入失敗：{e}", reply_markup=err_kb)


async def _cb_watchlist_group(chat_id, message_id, user_id, data: str):
    """Show stocks in a group."""
    group_name = data.replace("wl_group_", "")
    try:
        from src.core.database import get_watchlist
        watchlist = get_watchlist(get_app_user(user_id))
        stocks = [s for s in watchlist if s.get("group_name", "預設") == group_name]

        if not stocks:
            edit_message_text(chat_id, message_id,
                              f"📁 <b>{group_name}</b> — 沒有股票",
                              reply_markup=build_keyboard([[("🔙 返回", "wl")]]))
            return

        rows = []
        for s in stocks:
            code = s["stock_code"]
            name = s.get("stock_name", "")
            rows.append([(f"{code} {name}", f"wl_stock_{code}")])

        rows.append([("🔙 返回分組", "wl")])

        edit_message_text(chat_id, message_id,
                          f"📁 <b>{group_name}</b> — 點選個股操作：",
                          reply_markup=build_keyboard(rows))
    except Exception as e:
        logger.error(f"Watchlist group failed: {e}")
        edit_message_text(chat_id, message_id, f"❌ 載入失敗：{e}",
                          reply_markup=build_keyboard([[("🔙 返回", "wl")]]))


async def _cb_watchlist_stock(chat_id, message_id, user_id, data: str):
    """Show stock actions for a watchlist stock."""
    code = data.replace("wl_stock_", "")
    keyboard = build_keyboard([
        [("📝 備註", f"wl_note_{code}"), ("📊 看圖", f"wl_chart_{code}")],
        [("🤖 AI分析", f"wl_ai_{code}")],
        [("🔙 返回", "wl")],
    ])
    edit_message_text(chat_id, message_id,
                      f"📋 <b>{code}</b> — 選擇操作：",
                      reply_markup=keyboard)


async def _cb_watchlist_note(chat_id, message_id, user_id, data: str):
    """Handle note viewing/adding."""
    code = data.replace("wl_note_", "")
    try:
        from src.core.database import get_notes
        notes = get_notes(code, get_app_user(user_id))

        text = f"📝 <b>{code} 備註</b>\n\n"
        if notes:
            # Show latest 5 notes
            for n in notes[:5]:
                content = (n.get("content") or "")[:100]
                date_str = n.get("created_at", "")
                if hasattr(date_str, "strftime"):
                    date_str = date_str.strftime("%m/%d %H:%M")
                text += f"• {date_str}: {content}\n"
            if len(notes) > 5:
                text += f"\n... 共 {len(notes)} 筆備註\n"
        else:
            text += "（尚無備註）\n"

        text += "\n💬 新增備註："

        # Set user state to wait for note input
        user_states[user_id] = {"action": "waiting_note", "stock_code": code}
        user_states[chat_id] = {"action": "waiting_note", "stock_code": code}

        edit_message_text(chat_id, message_id, text,
                          reply_markup=build_keyboard([[("❌ 取消", f"wl_stock_{code}")]]))
        # Send prompt while keeping persistent keyboard visible
        send_message(chat_id, "💬 請輸入備註內容：",
                     reply_markup=persistent_reply_keyboard())
    except Exception as e:
        logger.error(f"Watchlist note failed: {e}")
        edit_message_text(chat_id, message_id, f"❌ 載入失敗：{e}",
                          reply_markup=build_keyboard([[("🔙 返回", "wl")]]))


async def _cb_watchlist_chart(chat_id, message_id, user_id, data: str):
    """Send MA chart for watchlist stock."""
    code = data.replace("wl_chart_", "")
    await _send_stock_chart(chat_id, code)


async def _cb_watchlist_ai(chat_id, message_id, user_id, data: str):
    """Run AI analysis for watchlist stock."""
    code = data.replace("wl_ai_", "")
    # Reuse the stock AI callback
    await _cb_stock_ai(chat_id, message_id, user_id, f"stock_ai_{code}")


async def _cb_scan_entry(chat_id, message_id, user_id):
    """Show scan condition categories."""
    conditions = scan_states.get(user_id, {})
    count = len(conditions)
    status = f"（已選 {count} 個條件）" if count > 0 else ""

    rows = [
        [("RSI", "scan_cat_rsi"), ("均線排列", "scan_cat_ma_arr")],
        [("站上MA", "scan_cat_above_ma"), ("跌破MA", "scan_cat_below_ma")],
        [("MA方向", "scan_cat_ma_dir"), ("MA交叉", "scan_cat_ma_cross")],
        [("量比", "scan_cat_vol_ratio"), ("量趨勢", "scan_cat_vol_trend")],
        [("漲跌%", "scan_cat_change_pct"), ("漲幅排名", "scan_cat_rank")],
        [("產業", "scan_cat_industry"), ("市場", "scan_cat_market")],
    ]
    action_row = [("🔍 執行掃描", "scan_run")]
    if count > 0:
        action_row.append(("🗑 清除條件", "scan_clear"))
    rows.append(action_row)

    kb = build_keyboard(rows)
    text = f"📡 <b>掃描</b> — 選擇條件分類 {status}："
    if message_id:
        edit_message_text(chat_id, message_id, text, reply_markup=kb)
    else:
        send_message(chat_id, text, reply_markup=kb)


# Scan category options definitions
SCAN_OPTIONS = {
    "rsi": [
        ("< 30 超賣", "rsi_max=30"),
        ("> 70 超買", "rsi_min=70"),
        ("< 50", "rsi_max=50"),
    ],
    "ma_arr": [
        ("多頭排列", "ma_arrangement=多頭排列"),
        ("空頭排列", "ma_arrangement=空頭排列"),
        ("糾結", "ma_arrangement=糾結"),
        ("交錯", "ma_arrangement=交錯"),
    ],
    "above_ma": [
        ("站上 MA5", "price_above_ma=5"),
        ("站上 MA10", "price_above_ma=10"),
        ("站上 MA20", "price_above_ma=20"),
        ("站上 MA60", "price_above_ma=60"),
    ],
    "below_ma": [
        ("跌破 MA5", "price_below_ma=5"),
        ("跌破 MA10", "price_below_ma=10"),
        ("跌破 MA20", "price_below_ma=20"),
        ("跌破 MA60", "price_below_ma=60"),
    ],
    "ma_dir": [
        ("MA5↑", "ma_dir=5_up"), ("MA10↑", "ma_dir=10_up"),
        ("MA20↑", "ma_dir=20_up"), ("MA60↑", "ma_dir=60_up"),
        ("MA5↓", "ma_dir=5_down"), ("MA10↓", "ma_dir=10_down"),
        ("MA20↓", "ma_dir=20_down"), ("MA60↓", "ma_dir=60_down"),
    ],
    "ma_cross": [
        ("MA5金叉MA20", "ma_cross=5_20_up"),
        ("MA5死叉MA20", "ma_cross=5_20_down"),
        ("MA5金叉MA60", "ma_cross=5_60_up"),
        ("MA5死叉MA60", "ma_cross=5_60_down"),
        ("MA10金叉MA20", "ma_cross=10_20_up"),
        ("MA10死叉MA20", "ma_cross=10_20_down"),
        ("MA20金叉MA60", "ma_cross=20_60_up"),
        ("MA20死叉MA60", "ma_cross=20_60_down"),
    ],
    "vol_ratio": [
        ("≥ 1.5", "volume_ratio_min=1.5"),
        ("≥ 2.0", "volume_ratio_min=2.0"),
        ("≥ 3.0", "volume_ratio_min=3.0"),
    ],
    "vol_trend": [
        ("放量", "volume_trend=放量"),
        ("縮量", "volume_trend=縮量"),
        ("平量", "volume_trend=平量"),
    ],
    "change_pct": [
        ("漲 ≥ 3%", "change_pct_min=3"),
        ("漲 ≥ 5%", "change_pct_min=5"),
        ("跌 ≤ -3%", "change_pct_max=-3"),
        ("跌 ≤ -5%", "change_pct_max=-5"),
    ],
    "rank": [
        ("Top 10", "change_rank_max=10"),
        ("Top 20", "change_rank_max=20"),
        ("Top 50", "change_rank_max=50"),
        ("Top 100", "change_rank_max=100"),
    ],
    "industry": [],  # Dynamically loaded
    "market": [
        ("上市 (TSE)", "market=TSE"),
        ("上櫃 (TPEx)", "market=TPEx"),
    ],
}


async def _cb_scan_category(chat_id, message_id, user_id, data: str):
    """Show options for a scan category (multi-select with ✅ marks)."""
    category = data.replace("scan_cat_", "")
    conditions = scan_states.get(user_id, {})

    # Handle dynamic industry list
    options = SCAN_OPTIONS.get(category, [])
    if category == "industry" and not options:
        try:
            import psycopg2
            from psycopg2.extras import RealDictCursor
            from src.core.pg_client import DB_CONFIG
            conn = psycopg2.connect(**DB_CONFIG)
            cur = conn.cursor()
            cur.execute("SELECT DISTINCT industry FROM stock_basic WHERE industry IS NOT NULL AND industry != '' ORDER BY industry")
            industries = [r[0] for r in cur.fetchall()]
            conn.close()
            options = [(ind, f"industry={ind}") for ind in industries[:20]]  # Limit to 20
        except Exception:
            options = []

    # Build buttons with ✅ for selected options
    rows = []
    row = []
    for label, value in options:
        # Parse value to check if selected
        key, val = value.split("=", 1)
        is_selected = conditions.get(key) == val
        btn_text = f"✅ {label}" if is_selected else label
        row.append((btn_text, f"scan_opt_{category}_{value}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    rows.append([("🔙 返回條件列表", "scan")])

    category_names = {
        "rsi": "RSI", "ma_arr": "均線排列", "above_ma": "站上MA",
        "below_ma": "跌破MA", "ma_dir": "MA方向", "ma_cross": "MA交叉",
        "vol_ratio": "量比", "vol_trend": "量趨勢", "change_pct": "漲跌%",
        "rank": "漲幅排名", "industry": "產業", "market": "市場",
    }
    cat_name = category_names.get(category, category)

    edit_message_text(chat_id, message_id,
                      f"📡 掃描 > <b>{cat_name}</b>（可多選）：",
                      reply_markup=build_keyboard(rows))


async def _cb_scan_option(chat_id, message_id, user_id, data: str):
    """Toggle a scan option (multi-select)."""
    # data format: scan_opt_{category}_{key}={value}
    parts = data.replace("scan_opt_", "").split("_", 1)
    if len(parts) < 2:
        return
    category = parts[0]
    kv = parts[1]

    # Handle categories with underscores (e.g., ma_arr, above_ma, etc.)
    # Re-parse: everything after "scan_opt_" until the last segment with "="
    remainder = data.replace("scan_opt_", "")
    # Find the key=value part (last occurrence of a known key)
    eq_idx = remainder.rfind("=")
    if eq_idx == -1:
        return
    # The value part
    value = remainder[eq_idx + 1:]
    # The key part (between last _ before = and =)
    before_eq = remainder[:eq_idx]
    last_underscore = before_eq.rfind("_")
    if last_underscore == -1:
        return
    key = before_eq[last_underscore + 1:]
    category = before_eq[:last_underscore]

    # Toggle condition
    if user_id not in scan_states:
        scan_states[user_id] = {}

    if scan_states[user_id].get(key) == value:
        # Deselect
        del scan_states[user_id][key]
    else:
        # Select (replace previous value for same key)
        scan_states[user_id][key] = value

    # Refresh the category view
    await _cb_scan_category(chat_id, message_id, user_id, f"scan_cat_{category}")


async def _cb_scan_run(chat_id, message_id, user_id):
    """Execute scan with accumulated conditions."""
    conditions = scan_states.get(user_id, {})
    if not conditions:
        edit_message_text(chat_id, message_id,
                          "⚠️ 尚未選擇任何條件，請先選擇掃描條件。",
                          reply_markup=build_keyboard([[("🔙 返回", "scan")]]))
        return

    edit_message_text(chat_id, message_id, "🔍 掃描中...")

    try:
        import psycopg2
        from psycopg2.extras import RealDictCursor
        from src.core.pg_client import DB_CONFIG

        conn = psycopg2.connect(**DB_CONFIG)
        cur = conn.cursor(cursor_factory=RealDictCursor)

        # Get latest date
        cur.execute("SELECT MAX(date) FROM daily_indicators")
        latest = cur.fetchone()["max"]
        if not latest:
            edit_message_text(chat_id, message_id, "❌ 沒有掃描資料",
                              reply_markup=build_keyboard([[("🔙 返回", "scan")]]))
            conn.close()
            return

        # Build WHERE clause
        where = ["di.date = %s"]
        params = [latest]

        if "rsi_min" in conditions:
            where.append("di.rsi14 >= %s")
            params.append(float(conditions["rsi_min"]))
        if "rsi_max" in conditions:
            where.append("di.rsi14 <= %s")
            params.append(float(conditions["rsi_max"]))
        if "ma_arrangement" in conditions:
            where.append("di.ma_arrangement = %s")
            params.append(conditions["ma_arrangement"])
        if "price_above_ma" in conditions:
            ma_col = f"di.ma{conditions['price_above_ma']}"
            where.append(f"di.close > {ma_col}")
            where.append(f"{ma_col} IS NOT NULL")
        if "price_below_ma" in conditions:
            ma_col = f"di.ma{conditions['price_below_ma']}"
            where.append(f"di.close < {ma_col}")
            where.append(f"{ma_col} IS NOT NULL")
        if "ma_dir" in conditions:
            parts = conditions["ma_dir"].split("_")
            if len(parts) == 2:
                dir_col = f"di.ma{parts[0]}_dir"
                dir_val = "↑" if parts[1] == "up" else "↓"
                where.append(f"{dir_col} = %s")
                params.append(dir_val)
        if "ma_cross" in conditions:
            parts = conditions["ma_cross"].split("_")
            if len(parts) == 3:
                fast_col = f"di.ma{parts[0]}"
                slow_col = f"di.ma{parts[1]}"
                if parts[2] == "up":
                    where.append(f"{fast_col} > {slow_col}")
                    where.append(f"({fast_col} - {slow_col}) / {slow_col} < 0.01")
                else:
                    where.append(f"{fast_col} < {slow_col}")
                    where.append(f"({slow_col} - {fast_col}) / {fast_col} < 0.01")
                where.append(f"{fast_col} IS NOT NULL AND {slow_col} IS NOT NULL")
        if "volume_ratio_min" in conditions:
            where.append("di.volume_ratio >= %s")
            params.append(float(conditions["volume_ratio_min"]))
        if "volume_trend" in conditions:
            where.append("di.volume_trend = %s")
            params.append(conditions["volume_trend"])
        if "change_pct_min" in conditions:
            where.append("di.change_pct >= %s")
            params.append(float(conditions["change_pct_min"]))
        if "change_pct_max" in conditions:
            where.append("di.change_pct <= %s")
            params.append(float(conditions["change_pct_max"]))
        if "change_rank_max" in conditions:
            where.append("di.change_rank <= %s")
            params.append(int(conditions["change_rank_max"]))
        if "industry" in conditions:
            where.append("sb.industry = %s")
            params.append(conditions["industry"])
        if "market" in conditions:
            where.append("sb.market = %s")
            params.append(conditions["market"])

        where_clause = " AND ".join(where)
        sql = f"""
            SELECT di.stock_code, sb.stock_name, di.close, di.change_pct,
                   di.rsi14, di.volume_ratio, di.ma_arrangement
            FROM daily_indicators di
            JOIN stock_basic sb ON sb.stock_code = di.stock_code
            WHERE {where_clause}
            ORDER BY di.volume DESC
            LIMIT 30
        """
        cur.execute(sql, params)
        results = [dict(r) for r in cur.fetchall()]

        # Get total
        count_sql = f"SELECT COUNT(*) FROM daily_indicators di JOIN stock_basic sb ON sb.stock_code = di.stock_code WHERE {where_clause}"
        cur.execute(count_sql, params)
        total = cur.fetchone()["count"]
        conn.close()

        # Format results
        cond_text = "、".join(f"{k}={v}" for k, v in conditions.items())
        text = f"📡 <b>掃描結果</b>（{latest}）\n"
        text += f"條件：{cond_text}\n"
        text += f"共 {total} 檔符合（顯示前 30）\n\n"

        for r in results:
            chg = r.get("change_pct") or 0
            sign = "+" if chg >= 0 else ""
            rsi = f"RSI:{r['rsi14']:.0f}" if r.get("rsi14") else ""
            vr = f"量比:{r['volume_ratio']:.1f}" if r.get("volume_ratio") else ""
            text += f"<b>{r['stock_code']}</b> {r['stock_name'] or ''} {r['close']} ({sign}{chg:.1f}%) {rsi} {vr}\n"

        if len(text) > 3800:
            text = text[:3800] + "\n... (截斷)"

        edit_message_text(chat_id, message_id, text,
                          reply_markup=build_keyboard([
                              [("🔄 重新掃描", "scan"), ("🗑 清除條件", "scan_clear")],
                          ]))
    except Exception as e:
        logger.error(f"Scan run failed: {e}")
        edit_message_text(chat_id, message_id, f"❌ 掃描失敗：{e}",
                          reply_markup=build_keyboard([[("🔙 返回", "scan")]]))
    finally:
        # Clear conditions after run
        scan_states.pop(user_id, None)


async def _cb_portfolio(chat_id, message_id, user_id):
    """Run portfolio analysis."""
    if message_id:
        edit_message_text(chat_id, message_id, "💼 正在分析持股，請稍候...")
    else:
        send_message(chat_id, "💼 正在分析持股，請稍候...")

    try:
        from src.services.portfolio_advisor import analyze_portfolio
        result = analyze_portfolio(get_app_user(user_id))

        if result and result.get("analysis"):
            import html as _html
            analysis_content = result['analysis']
            if len(analysis_content) > 3800:
                analysis_content = analysis_content[:3800] + "\n\n... (內容過長已截斷)"
            safe_content = _html.escape(analysis_content)
            text = f"💼 <b>持股分析</b>\n\n{safe_content}"
            kb = None
            if message_id:
                edit_message_text(chat_id, message_id, text, reply_markup=kb)
            else:
                send_message(chat_id, text, reply_markup=kb)
        else:
            kb = None
            if message_id:
                edit_message_text(chat_id, message_id, "💼 目前沒有持股資料或分析結果。", reply_markup=kb)
            else:
                send_message(chat_id, "💼 目前沒有持股資料或分析結果。", reply_markup=kb)
    except Exception as e:
        logger.error(f"Portfolio analysis failed: {e}")
        kb = None
        if message_id:
            edit_message_text(chat_id, message_id, f"❌ 持股分析失敗：{e}", reply_markup=kb)
        else:
            send_message(chat_id, f"❌ 持股分析失敗：{e}", reply_markup=kb)


async def _cb_news_entry(chat_id, message_id, user_id):
    """Layer 1: Show date buttons (last 3 days)."""
    try:
        from src.core.database import get_notes
        from datetime import timedelta
        cutoff_str = (date.today() - timedelta(days=3)).isoformat()

        notes = get_notes("NEWS", "shared")
        # Collect dates that have any data
        dates_with_data = set()
        for n in notes:
            d = str(n.get("news_date", ""))
            if d >= cutoff_str:
                dates_with_data.add(d)

        if not dates_with_data:
            kb = None
            msg = "📰 近 3 日無新聞資料。"
            if message_id:
                edit_message_text(chat_id, message_id, msg, reply_markup=kb)
            else:
                send_message(chat_id, msg, reply_markup=kb)
            return

        # One button per date
        rows = []
        for d in sorted(dates_with_data, reverse=True):
            rows.append([(f"📅 {d}", f"news_date_{d}")])

        kb = build_keyboard(rows)
        text = "📰 <b>新聞</b> — 選擇日期："
        if message_id:
            edit_message_text(chat_id, message_id, text, reply_markup=kb)
        else:
            send_message(chat_id, text, reply_markup=kb)
    except Exception as e:
        logger.error(f"News entry failed: {e}")
        kb = None
        if message_id:
            edit_message_text(chat_id, message_id, f"❌ 載入失敗：{e}", reply_markup=kb)
        else:
            send_message(chat_id, f"❌ 載入失敗：{e}", reply_markup=kb)


async def _cb_news_date(chat_id, message_id, user_id, target_date):
    """Layer 2: Show available categories for a specific date."""
    try:
        from src.core.database import get_notes
        from collections import OrderedDict
        notes = get_notes("NEWS", "shared")

        # Collect distinct categories for this date
        cats = OrderedDict()
        for n in notes:
            if str(n.get("news_date", "")) != target_date:
                continue
            title = n.get("title") or ""
            if "AI 盤後分析" in title:
                cats["🤖 AI盤後分析"] = "ai"
            elif "財經" in title or "熱門新聞" in title:
                cats["📋 財經熱門"] = "titles"
            elif title:
                cats[f"📰 {title}"] = f"other_{title}"

        if not cats:
            edit_message_text(chat_id, message_id,
                              f"📅 {target_date} 無新聞資料。",
                              reply_markup=build_keyboard([[("🔙 返回", "news")]]))
            return

        rows = []
        btns = []
        for label, key in cats.items():
            cb_data = f"news_cat_{key}_{target_date}"
            if len(cb_data) > 64:
                cb_data = cb_data[:64]
            btns.append((label, cb_data))
        for i in range(0, len(btns), 2):
            rows.append(btns[i:i+2])
        rows.append([("🔙 返回", "news")])

        kb = build_keyboard(rows)
        edit_message_text(chat_id, message_id,
                          f"📅 <b>{target_date}</b> — 選擇類別：",
                          reply_markup=kb)
    except Exception as e:
        logger.error(f"News date failed: {e}")
        edit_message_text(chat_id, message_id, f"❌ 載入失敗：{e}",
                          reply_markup=None)


async def _cb_news_titles(chat_id, message_id, user_id, target_date=None):
    """Show news titles for a specific date."""
    try:
        from src.core.database import get_notes
        notes = get_notes("NEWS", "shared")

        # Find the note for the target date
        titles_note = None
        for n in notes:
            if target_date and str(n.get("news_date", "")) != target_date:
                continue
            title = n.get("title") or ""
            if "熱門新聞" in title or "財經" in title:
                titles_note = n
                break

        if titles_note and titles_note.get("content"):
            content = titles_note["content"]
            nd = titles_note.get("news_date", "")
            text = f"📋 <b>每日財經熱門話題</b>（{nd}）\n\n{content}"
            if len(text) > 3800:
                text = text[:3800] + "\n... (截斷)"
        else:
            text = "📋 該日無新聞標題。"

        edit_message_text(chat_id, message_id, text,
                          reply_markup=build_keyboard([[("🔙 返回", "news")]]))
    except Exception as e:
        logger.error(f"News titles failed: {e}")
        edit_message_text(chat_id, message_id, f"❌ 載入失敗：{e}",
                          reply_markup=None)


async def _cb_news_ai(chat_id, message_id, user_id, target_date=None):
    """Show AI analysis for a specific date."""
    try:
        from src.core.database import get_notes
        notes = get_notes("NEWS", "shared")

        # Find the AI note for the target date
        ai_note = None
        for n in notes:
            if target_date and str(n.get("news_date", "")) != target_date:
                continue
            title = n.get("title") or ""
            if "AI 盤後分析" in title:
                ai_note = n
                break

        if ai_note and ai_note.get("content"):
            content = ai_note["content"]
            nd = ai_note.get("news_date", "")
            text = f"🤖 <b>AI 盤後分析</b>（{nd}）\n\n{content}"
            if len(text) > 3800:
                text = text[:3800] + "\n... (截斷)"
        else:
            text = "🤖 該日無 AI 盤後分析。"

        edit_message_text(chat_id, message_id, text,
                          reply_markup=build_keyboard([[("🔙 返回", "news")]]))
    except Exception as e:
        logger.error(f"News AI failed: {e}")
        edit_message_text(chat_id, message_id, f"❌ 載入失敗：{e}",
                          reply_markup=None)


async def _cb_news_other(chat_id, message_id, user_id, target_date, category):
    """Show news for a specific category and date (e.g. 經濟日報)."""
    try:
        from src.core.database import get_notes
        notes = get_notes("NEWS", "shared")

        # Find all notes matching this date and category
        matched = []
        for n in notes:
            if str(n.get("news_date", "")) != target_date:
                continue
            title = n.get("title") or ""
            if title == category:
                matched.append(n)

        if matched:
            # Combine content from all matched notes
            parts = []
            for n in matched:
                content = (n.get("content") or "").strip()
                if content:
                    parts.append(content)
            combined = "\n\n".join(parts) if parts else "（無內容）"
            text = f"📰 <b>{category}</b>（{target_date}）\n\n{combined}"
            if len(text) > 3800:
                text = text[:3800] + "\n... (截斷)"
        else:
            text = f"📰 {target_date} 無「{category}」資料。"

        edit_message_text(chat_id, message_id, text,
                          reply_markup=build_keyboard([[("🔙 返回", "news")]]))
    except Exception as e:
        logger.error(f"News other failed: {e}")
        edit_message_text(chat_id, message_id, f"❌ 載入失敗：{e}",
                          reply_markup=None)



def _cb_help(chat_id, message_id):
    """Show help text."""
    help_text = (
        "❓ <b>使用說明</b>\n\n"
        "📊 <b>查股票</b> — 輸入代號查看 MA 均線圖\n"
        "📋 <b>自選股</b> — 查看分組、備註、分析\n"
        "📡 <b>掃描</b> — 多條件組合篩選股票\n"
        "💼 <b>持股分析</b> — AI 投資組合分析\n"
        "📰 <b>新聞</b> — 今日財經熱門話題\n\n"
        "💬 在群組中 <b>@Bot 問題</b> 可觸發 AI 搜尋\n"
        "💬 一般訊息會自動存檔為討論紀錄"
    )
    kb = None
    if message_id:
        edit_message_text(chat_id, message_id, help_text, reply_markup=kb)
    else:
        send_message(chat_id, help_text, reply_markup=kb)


# ---------------------------------------------------------------------------
# @Bot Mention Handler
# ---------------------------------------------------------------------------

async def _handle_mention_query(message: dict, text: str):
    """Handle @Bot mention — AI semantic search."""
    chat_id = message["chat"]["id"]
    message_id = message["message_id"]
    user_id = message["from"]["id"]

    # Strip bot mention from text
    import re
    query = re.sub(r"@\w+", "", text).strip()
    if not query:
        send_message(chat_id, "請在 @Bot 後面加上你的問題 🤔", reply_to=message_id)
        return

    send_message(chat_id, "🔍 搜尋中...", reply_to=message_id)

    try:
        from src.services.semantic_search import ask_news
        result = ask_news(query, "shared")
        answer = result.get("answer", "找不到相關資訊")
        send_message(chat_id, f"🤖 <b>AI 回覆</b>\n\n{answer}", reply_to=message_id)
    except Exception as e:
        logger.error(f"Mention query failed: {e}")
        send_message(chat_id, "❌ 搜尋失敗，請稍後再試", reply_to=message_id)


# ---------------------------------------------------------------------------
# Discussion Archive
# ---------------------------------------------------------------------------

async def _save_discussion(message: dict):
    """Save non-command message to telegram_discussions table."""
    text = message.get("text", "")
    if not text:
        return

    user_id = message["from"]["id"]
    user_name = message["from"].get("first_name", "") or message["from"].get("username", "unknown")

    try:
        from src.core.database import save_discussion
        save_discussion(user_name, user_id, text)
    except Exception as e:
        logger.error(f"Failed to save discussion: {e}")


# ---------------------------------------------------------------------------
# Note Save Helper
# ---------------------------------------------------------------------------

async def _save_note(chat_id, user_id: int, stock_code: str, content: str):
    """Save a note for a stock."""
    try:
        from src.core.database import add_note
        add_note(stock_code=stock_code, content=content, user_id=get_app_user(user_id))
        send_message(chat_id, f"✅ 備註已儲存 — {stock_code}")
    except Exception as e:
        logger.error(f"Failed to save note: {e}")
        send_message(chat_id, "❌ 儲存失敗")


# ---------------------------------------------------------------------------
# Long Polling Loop
# ---------------------------------------------------------------------------

async def run_polling(executor=None):
    """Main long polling loop — runs as asyncio background task.

    Args:
        executor: Optional ThreadPoolExecutor for blocking HTTP calls.
                  If None, uses the default event loop executor.
    """
    _ensure_config()
    if not BOT_TOKEN:
        logger.warning("TELEGRAM_BOT_TOKEN not set, polling disabled")
        return

    import threading

    logger.info("Telegram bot polling started")
    offset = 0
    loop = asyncio.get_event_loop()
    _stop = threading.Event()

    def _fetch_updates(url: str):
        """Blocking HTTP call — executed in thread executor.
        Uses short socket timeout and checks stop event to allow fast exit.
        """
        if _stop.is_set():
            return None
        req = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return json.loads(resp.read())
        except Exception:
            if _stop.is_set():
                return None
            raise

    while True:
        try:
            url = (f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
                   f"?offset={offset}&timeout=4&allowed_updates=[\"message\",\"callback_query\"]")
            data = await loop.run_in_executor(executor, _fetch_updates, url)

            if data is None:
                break

            if not data.get("ok"):
                logger.error(f"getUpdates error: {data}")
                await asyncio.sleep(5)
                continue

            for update in data.get("result", []):
                offset = update["update_id"] + 1

                if "callback_query" in update:
                    await handle_callback_query(update["callback_query"])
                elif "message" in update:
                    await handle_message(update["message"])

        except asyncio.CancelledError:
            _stop.set()
            logger.info("Telegram bot polling stopped (cancelled)")
            break
        except Exception as e:
            if _stop.is_set():
                break
            logger.error(f"Polling error: {e}")
            await asyncio.sleep(5)
