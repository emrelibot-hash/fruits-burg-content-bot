import os
import time
import json
import re
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List

import requests
from flask import Flask, request

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build


# =========================
# Env
# =========================
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET")

GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID")
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")

if not BOT_TOKEN:
    raise RuntimeError("Missing TELEGRAM_BOT_TOKEN env var")

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

app = Flask(__name__)


# =========================
# In-memory sessions (MVP)
# =========================
# NOTE: This is RAM-only. Render restart/redeploy clears it.
SESSIONS: Dict[int, Dict[str, Any]] = {}
_sheets_service = None


# =========================
# Helpers
# =========================
URL_RE = re.compile(r"(https?://[^\s]+)", re.IGNORECASE)


def extract_first_url(text: str) -> Optional[str]:
    if not text:
        return None
    m = URL_RE.search(text)
    return m.group(1) if m else None


def now_utc_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def normalize_text_command(text: str) -> str:
    """
    Makes '/ new' behave like '/new' etc.
    Keeps normal non-command texts unchanged enough.
    """
    if not text:
        return ""
    t = text.strip()
    if t.startswith("/"):
        # remove spaces after slash
        t = "/" + t[1:].replace(" ", "")
    return t


def tg_request(method: str, payload: dict):
    r = requests.post(f"{TELEGRAM_API}/{method}", json=payload, timeout=25)
    r.raise_for_status()
    return r.json()


def send_message(chat_id: int, text: str, reply_markup: Optional[dict] = None):
    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return tg_request("sendMessage", payload)


def answer_callback(callback_query_id: str):
    # Acknowledge button press to Telegram (avoids spinning UI)
    payload = {"callback_query_id": callback_query_id}
    try:
        tg_request("answerCallbackQuery", payload)
    except Exception:
        # Non-fatal
        pass


def get_session(chat_id: int) -> Dict[str, Any]:
    if chat_id not in SESSIONS:
        SESSIONS[chat_id] = {"items": [], "last_draft": None}
    return SESSIONS[chat_id]


def clear_session(chat_id: int):
    sess = get_session(chat_id)
    sess["items"] = []
    sess["last_draft"] = None


def make_draft_id() -> str:
    # Time-based unique enough for MVP
    return f"FB-{int(time.time())}"


def start_keyboard() -> dict:
    return {
        "keyboard": [
            [{"text": "🆕 New Draft"}, {"text": "✍️ Generate"}],
            [{"text": "📌 Status"}, {"text": "🔚 End Session"}],
            [{"text": "🧾 Help"}],
        ],
        "resize_keyboard": True,
        "one_time_keyboard": False,
    }


def draft_keyboard(draft_id: str) -> dict:
    return {
        "inline_keyboard": [
            [
                {"text": "✅ Approve", "callback_data": f"approve|{draft_id}"},
                {"text": "✏ Edit", "callback_data": f"edit|{draft_id}"},
            ],
            [
                {"text": "♻ Rewrite", "callback_data": f"rewrite|{draft_id}"},
                {"text": "❌ Reject", "callback_data": f"reject|{draft_id}"},
            ],
        ]
    }


def build_draft_text(items: List[Dict[str, Any]]) -> str:
    """
    Placeholder generator (no OpenAI yet).
    Uses notes/captions/links to make draft less generic.
    """
    notes: List[str] = []
    links: List[str] = []
    photo_count = 0
    video_count = 0
    file_count = 0

    for it in items:
        t = it.get("type")
        if t == "text":
            notes.append(it.get("text", "").strip())
        elif t == "photo":
            photo_count += 1
            cap = it.get("caption", "").strip()
            if cap:
                notes.append(cap)
        elif t == "video":
            video_count += 1
            cap = it.get("caption", "").strip()
            if cap:
                notes.append(cap)
        elif t == "document":
            file_count += 1
            cap = it.get("caption", "").strip()
            if cap:
                notes.append(cap)
        elif t == "link":
            u = it.get("url")
            if u:
                links.append(u)
            extra = it.get("text", "").strip()
            # If user added extra text with link, keep it as a note too
            if extra and extra != u:
                notes.append(extra)

    notes = [n for n in notes if n]
    # Keep draft readable: cap notes length
    notes = notes[:6]
    links = links[:3]

    lines = []
    lines.append("Dried Persimmon Market Update\n")
    lines.append(f"- Based on {len(items)} material(s): {photo_count} photo(s), {video_count} video(s), {file_count} file(s)\n")
    lines.append("- Market insight: demand planning becomes critical ahead of Q4")
    lines.append("- Buyer takeaway: secure specifications and volumes early\n")

    if notes:
        lines.append("Key inputs:")
        for n in notes:
            # keep each note short-ish
            n2 = n.replace("\n", " ").strip()
            if len(n2) > 180:
                n2 = n2[:180] + "…"
            lines.append(f"• {n2}")
        lines.append("")

    if links:
        lines.append("Sources / reading:")
        for u in links:
            lines.append(f"• {u}")
        lines.append("")

    lines.append("Open for inquiries and supply planning discussions.")
    lines.append("")
    lines.append("#driedfruit #persimmon #export #foodtrade #supplychain")

    return "\n".join(lines)


# =========================
# Google Sheets Integration
# =========================
def get_sheets_service():
    global _sheets_service
    if _sheets_service is not None:
        return _sheets_service

    if not GOOGLE_SHEET_ID or not GOOGLE_SERVICE_ACCOUNT_JSON:
        raise RuntimeError("Missing GOOGLE_SHEET_ID or GOOGLE_SERVICE_ACCOUNT_JSON env vars")

    info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    creds = Credentials.from_service_account_info(
        info,
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    _sheets_service = build("sheets", "v4", credentials=creds, cache_discovery=False)
    return _sheets_service


def append_approved_to_sheet(draft_id: str, materials_count: int, post_text: str):
    service = get_sheets_service()
    values = [[draft_id, "Approved", now_utc_str(), materials_count, post_text, ""]]

    service.spreadsheets().values().append(
        spreadsheetId=GOOGLE_SHEET_ID,
        range="Sheet1!A:F",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": values},
    ).execute()


# =========================
# Flask routes
# =========================
@app.get("/")
def health():
    return {"status": "running"}


@app.post("/webhook")
def webhook():
    # Secret gate (recommended)
    if WEBHOOK_SECRET:
        got = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if got != WEBHOOK_SECRET:
            return {"error": "unauthorized"}, 403

    data = request.get_json(silent=True) or {}

    # -------------------------
    # Callback buttons
    # -------------------------
    if "callback_query" in data:
        cq = data["callback_query"]
        callback_id = cq.get("id", "")
        answer_callback(callback_id)

        message = cq.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")

        raw = (cq.get("data") or "")
        if "|" not in raw or chat_id is None:
            return {"ok": True}

        action, _draft_id_from_button = raw.split("|", 1)

        sess = get_session(chat_id)
        last = sess.get("last_draft")

        if not last:
            send_message(chat_id, "No active draft. Use /generate first.")
            return {"ok": True}

        if action == "approve":
            try:
                append_approved_to_sheet(
                    draft_id=last["id"],
                    materials_count=len(sess["items"]),
                    post_text=last["text"],
                )
                send_message(chat_id, f"Approved ✅ Draft ID: {last['id']}\nSaved to Google Sheet.")
                # Auto-clear after approve to avoid mixing next post
                clear_session(chat_id)
                send_message(chat_id, "Session auto-closed 🔚\nStart a new one with 🆕 New Draft.")
            except Exception as e:
                send_message(chat_id, f"Approved ✅ Draft ID: {last['id']}\nBUT failed to save to Google Sheet:\n{e}")

        elif action == "edit":
            send_message(chat_id, f"Send edits in one message:\n/edit {last['id']} <your changes>")

        elif action == "rewrite":
            # Rebuild using same materials (placeholder rewrite)
            last["text"] = build_draft_text(sess["items"])
            send_message(chat_id, f"Rewritten ♻ Draft ID: {last['id']}\n\n{last['text']}", reply_markup=draft_keyboard(last["id"]))

        elif action == "reject":
            sess["last_draft"] = None
            send_message(chat_id, f"Rejected ❌ Draft ID: {last['id']}\nMaterials are still in session. You can /generate again or 🔚 End Session.")

        return {"ok": True}

    # -------------------------
    # Normal messages
    # -------------------------
    msg = data.get("message")
    if not msg:
        return {"ok": True}

    chat_id = msg["chat"]["id"]
    text = normalize_text_command(msg.get("text", ""))

    # Menu buttons come as plain text messages
    if text == "🆕 New Draft":
        clear_session(chat_id)
        send_message(chat_id, "New draft session started ✅\nSend materials, then ✍️ Generate.")
        return {"ok": True}

    if text == "📌 Status":
        sess = get_session(chat_id)
        send_message(chat_id, f"Current session: {len(sess['items'])} material(s).")
        return {"ok": True}

    if text == "✍️ Generate":
        # Same as /generate
        sess = get_session(chat_id)
        if not sess["items"]:
            send_message(chat_id, "No materials yet. Send text/photo/file/link first.")
            return {"ok": True}
        draft_id = make_draft_id()
        draft_text = build_draft_text(sess["items"])
        sess["last_draft"] = {"id": draft_id, "text": draft_text}
        send_message(chat_id, f"Draft ID: {draft_id}\n\n{draft_text}", reply_markup=draft_keyboard(draft_id))
        return {"ok": True}

    if text == "🔚 End Session":
        clear_session(chat_id)
        send_message(chat_id, "Session closed 🔚\nAll materials cleared.")
        return {"ok": True}

    if text == "🧾 Help":
        send_message(
            chat_id,
            "How to use:\n"
            "1) 🆕 New Draft\n"
            "2) Send materials (text/photo/video/file/link)\n"
            "3) ✍️ Generate\n"
            "4) ✅ Approve (saves to Google Sheet)\n"
            "5) 🔚 End Session (or auto-close after approve)\n\n"
            "Commands (optional):\n"
            "/new, /status, /generate, /edit",
            reply_markup=start_keyboard(),
        )
        return {"ok": True}

    # Commands
    if text.startswith("/start"):
        send_message(
            chat_id,
            "FruitsBurg Bot is live 🚀\n\nChoose an action:",
            reply_markup=start_keyboard(),
        )
        return {"ok": True}

    if text.startswith("/new"):
        clear_session(chat_id)
        send_message(chat_id, "New draft session started ✅\nSend materials, then /generate.")
        return {"ok": True}

    if text.startswith("/status"):
        sess = get_session(chat_id)
        send_message(chat_id, f"Current session: {len(sess['items'])} material(s).")
        return {"ok": True}

    if text.startswith("/generate"):
        sess = get_session(chat_id)
        if not sess["items"]:
            send_message(chat_id, "No materials yet. Send text/photo/file/link first.")
            return {"ok": True}
        draft_id = make_draft_id()
        draft_text = build_draft_text(sess["items"])
        sess["last_draft"] = {"id": draft_id, "text": draft_text}
        send_message(chat_id, f"Draft ID: {draft_id}\n\n{draft_text}", reply_markup=draft_keyboard(draft_id))
        return {"ok": True}

    if text.startswith("/edit"):
        # format: /edit <draft_id> <changes>
        parts = text.split(" ", 2)
        if len(parts) < 3:
            send_message(chat_id, "Use: /edit <draft_id> <your changes>")
            return {"ok": True}
        _, draft_id, changes = parts
        sess = get_session(chat_id)
        last = sess.get("last_draft")
        if not last:
            send_message(chat_id, "No active draft. Use /generate first.")
            return {"ok": True}
        # We accept any draft_id but apply edits to current last_draft (MVP simplicity)
        last["text"] = last["text"] + f"\n\nEdits requested:\n• {changes.strip()}"
        send_message(chat_id, f"Updated ✏ Draft ID: {last['id']}\n\n{last['text']}", reply_markup=draft_keyboard(last["id"]))
        return {"ok": True}

    # -------------------------
    # Collect materials
    # -------------------------
    sess = get_session(chat_id)

    # Photos (caption is important)
    if msg.get("photo"):
        caption = (msg.get("caption") or "").strip()
        sess["items"].append({"type": "photo", "caption": caption} if caption else {"type": "photo"})
        if caption:
            send_message(chat_id, f"Photo + caption received. Session materials: {len(sess['items'])}. Send more or /generate.")
        else:
            send_message(chat_id, f"Photo received. Session materials: {len(sess['items'])}. Send more or /generate.")
        return {"ok": True}

    # Videos (caption too)
    if msg.get("video"):
        caption = (msg.get("caption") or "").strip()
        sess["items"].append({"type": "video", "caption": caption} if caption else {"type": "video"})
        if caption:
            send_message(chat_id, f"Video + caption received. Session materials: {len(sess['items'])}. Send more or /generate.")
        else:
            send_message(chat_id, f"Video received. Session materials: {len(sess['items'])}. Send more or /generate.")
        return {"ok": True}

    # Documents (PDF, etc) + optional caption
    if msg.get("document"):
        caption = (msg.get("caption") or "").strip()
        filename = msg["document"].get("file_name", "file")
        item = {"type": "document", "filename": filename}
        if caption:
            item["caption"] = caption
        sess["items"].append(item)
        send_message(chat_id, f"File received ({filename}). Session materials: {len(sess['items'])}. Send more or /generate.")
        return {"ok": True}

    # Plain text OR link
    raw_text = (msg.get("text") or "").strip()
    if raw_text:
        url = extract_first_url(raw_text)
        if url:
            sess["items"].append({"type": "link", "url": url, "text": raw_text})
            send_message(chat_id, f"Link received. Session materials: {len(sess['items'])}.\n{url}")
            return {"ok": True}

        sess["items"].append({"type": "text", "text": raw_text})
        send_message(chat_id, f"Note received. Session materials: {len(sess['items'])}. Send more or /generate.")
        return {"ok": True}

    return {"ok": True}