import os
import time
import json
import re
import io
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List

import requests
from flask import Flask, request

from google.oauth2.service_account import Credentials as SACredentials
from google.oauth2.credentials import Credentials as UserCredentials
from google.auth.transport.requests import Request as GoogleRequest
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload


# =========================
# ENV
# =========================
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET")

GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID")
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")  # Sheets only
GOOGLE_DRIVE_FOLDER_ID = os.environ.get("GOOGLE_DRIVE_FOLDER_ID")            # Parent folder in YOUR Drive

# OAuth (Drive only)
GOOGLE_OAUTH_CLIENT_ID = os.environ.get("GOOGLE_OAUTH_CLIENT_ID")
GOOGLE_OAUTH_CLIENT_SECRET = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET")
GOOGLE_OAUTH_REFRESH_TOKEN = os.environ.get("GOOGLE_OAUTH_REFRESH_TOKEN")

if not BOT_TOKEN:
    raise RuntimeError("Missing TELEGRAM_BOT_TOKEN")
if not GOOGLE_SHEET_ID:
    raise RuntimeError("Missing GOOGLE_SHEET_ID")
if not GOOGLE_SERVICE_ACCOUNT_JSON:
    raise RuntimeError("Missing GOOGLE_SERVICE_ACCOUNT_JSON")
if not GOOGLE_DRIVE_FOLDER_ID:
    raise RuntimeError("Missing GOOGLE_DRIVE_FOLDER_ID")

# Drive OAuth must exist
if not GOOGLE_OAUTH_CLIENT_ID or not GOOGLE_OAUTH_CLIENT_SECRET or not GOOGLE_OAUTH_REFRESH_TOKEN:
    raise RuntimeError("Missing GOOGLE_OAUTH_CLIENT_ID/SECRET/REFRESH_TOKEN for Drive OAuth")

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
TELEGRAM_FILE_API = f"https://api.telegram.org/file/bot{BOT_TOKEN}"

app = Flask(__name__)

# In-memory sessions (MVP). With >1 gunicorn worker, sessions will split. Use -w 1.
SESSIONS: Dict[int, Dict[str, Any]] = {}
_sheets_service = None
_drive_service = None


# =========================
# TEXT / URL / MENU
# =========================
URL_RE = re.compile(r"(https?://[^\s]+)", re.IGNORECASE)
MENU_TRIGGERS = {
    "hi", "hello", "hey",
    "привет", "здравствуй", "здравствуйте",
    "menu", "start", "help",
    "меню", "старт", "помощь",
}

def extract_first_url(text: str) -> Optional[str]:
    if not text:
        return None
    m = URL_RE.search(text)
    return m.group(1) if m else None

def now_utc_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

def normalize_text_command(text: str) -> str:
    """Makes '/ new' behave like '/new'."""
    if not text:
        return ""
    t = text.strip()
    if t.startswith("/"):
        t = "/" + t[1:].replace(" ", "")
    return t


# =========================
# Telegram helpers
# =========================
def tg_post(method: str, payload: dict) -> dict:
    r = requests.post(f"{TELEGRAM_API}/{method}", json=payload, timeout=30)
    r.raise_for_status()
    return r.json()

def send_message(chat_id: int, text: str, reply_markup: Optional[dict] = None):
    payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return tg_post("sendMessage", payload)

def answer_callback(callback_query_id: str):
    try:
        tg_post("answerCallbackQuery", {"callback_query_id": callback_query_id})
    except Exception:
        pass


# =========================
# UI keyboards
# =========================
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


# =========================
# Sessions
# =========================
def get_session(chat_id: int) -> Dict[str, Any]:
    if chat_id not in SESSIONS:
        SESSIONS[chat_id] = {"items": [], "last_draft": None}
    return SESSIONS[chat_id]

def clear_session(chat_id: int):
    SESSIONS[chat_id] = {"items": [], "last_draft": None}


# =========================
# Google: Sheets via Service Account
# =========================
def get_sheets_service():
    global _sheets_service
    if _sheets_service:
        return _sheets_service

    info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    creds = SACredentials.from_service_account_info(
        info,
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    _sheets_service = build("sheets", "v4", credentials=creds, cache_discovery=False)
    return _sheets_service


# =========================
# Google: Drive via OAuth (YOUR account)
# =========================
def get_drive_service():
    global _drive_service
    if _drive_service:
        return _drive_service

    creds = UserCredentials(
        token=None,
        refresh_token=GOOGLE_OAUTH_REFRESH_TOKEN,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=GOOGLE_OAUTH_CLIENT_ID,
        client_secret=GOOGLE_OAUTH_CLIENT_SECRET,
        scopes=["https://www.googleapis.com/auth/drive.file"],
    )
    creds.refresh(GoogleRequest())

    _drive_service = build("drive", "v3", credentials=creds, cache_discovery=False)
    return _drive_service


# =========================
# ID generation (on Approve)
# =========================
def next_daily_id(prefix: str = "FB") -> str:
    """FB-YYYYMMDD-### increments by day (UTC) using Sheet1 column A."""
    service = get_sheets_service()
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    base = f"{prefix}-{today}-"

    resp = service.spreadsheets().values().get(
        spreadsheetId=GOOGLE_SHEET_ID,
        range="Sheet1!A:A"
    ).execute()

    values = resp.get("values", [])
    max_n = 0
    for row in values:
        if not row:
            continue
        v = str(row[0]).strip()
        if v.startswith(base):
            tail = v.replace(base, "")
            if tail.isdigit():
                max_n = max(max_n, int(tail))
    return f"{base}{max_n + 1:03d}"


# =========================
# Telegram file download
# =========================
def get_telegram_file_path(file_id: str) -> str:
    resp = requests.get(f"{TELEGRAM_API}/getFile", params={"file_id": file_id}, timeout=30).json()
    if not resp.get("ok"):
        raise RuntimeError(f"getFile failed: {resp}")
    return resp["result"]["file_path"]

def download_telegram_file_bytes(file_id: str) -> bytes:
    file_path = get_telegram_file_path(file_id)
    file_url = f"{TELEGRAM_FILE_API}/{file_path}"
    r = requests.get(file_url, timeout=120)
    r.raise_for_status()
    return r.content


# =========================
# Drive upload helpers
# =========================
def create_drive_folder(name: str, parent_id: str) -> str:
    service = get_drive_service()
    metadata = {
        "name": name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_id],
    }
    folder = service.files().create(body=metadata, fields="id").execute()
    return folder["id"]

def upload_bytes_to_drive(file_bytes: bytes, filename: str, folder_id: str, mime_type: str) -> str:
    service = get_drive_service()
    file_metadata = {"name": filename, "parents": [folder_id]}
    media = MediaIoBaseUpload(io.BytesIO(file_bytes), mimetype=mime_type, resumable=True)
    f = service.files().create(body=file_metadata, media_body=media, fields="id,webViewLink").execute()
    return f["webViewLink"]


# =========================
# Draft generator (placeholder, but uses inputs)
# =========================
def build_draft_text(items: List[Dict[str, Any]]) -> str:
    notes: List[str] = []
    links: List[str] = []
    photo_count = 0
    video_count = 0
    doc_count = 0

    for it in items:
        t = it.get("type")
        if t == "text":
            notes.append(it.get("text", "").strip())
        elif t == "link":
            u = it.get("url")
            if u:
                links.append(u)
            extra = it.get("text", "").strip()
            if extra and extra != u:
                notes.append(extra)
        elif t == "photo":
            photo_count += 1
            cap = (it.get("caption") or "").strip()
            if cap:
                notes.append(cap)
        elif t == "video":
            video_count += 1
            cap = (it.get("caption") or "").strip()
            if cap:
                notes.append(cap)
        elif t == "document":
            doc_count += 1
            cap = (it.get("caption") or "").strip()
            if cap:
                notes.append(cap)

    notes = [n for n in notes if n][:6]
    links = links[:3]

    lines = []
    lines.append("Dried Persimmon Market Update\n")
    lines.append(f"- Based on {len(items)} material(s): {photo_count} photo(s), {video_count} video(s), {doc_count} file(s)")
    lines.append("- Market insight: demand planning becomes critical ahead of Q4")
    lines.append("- Buyer takeaway: secure specifications and volumes early\n")

    if notes:
        lines.append("Key inputs:")
        for n in notes:
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
# Sheets write (Approved row)
# Columns A:G:
# ID | Status | Created_UTC | Materials_Count | Post_Text | Published_URL | Media_Links
# =========================
def append_approved_row(final_id: str, materials_count: int, post_text: str, media_links: List[str]):
    service = get_sheets_service()
    media_cell = "\n".join(media_links) if media_links else ""
    values = [[final_id, "Approved", now_utc_str(), materials_count, post_text, "", media_cell]]

    service.spreadsheets().values().append(
        spreadsheetId=GOOGLE_SHEET_ID,
        range="Sheet1!A:G",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": values},
    ).execute()


# =========================
# Routes
# =========================
@app.get("/")
def health():
    return {"status": "running"}


@app.post("/webhook")
def webhook():
    # Secret gate
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
        answer_callback(cq.get("id", ""))

        chat_id = cq["message"]["chat"]["id"]
        raw = cq.get("data") or ""
        if "|" not in raw:
            return {"ok": True}

        action, _button_draft_id = raw.split("|", 1)

        sess = get_session(chat_id)
        last = sess.get("last_draft")

        if not last:
            send_message(chat_id, "No active draft. Generate first (✍️ Generate).", reply_markup=start_keyboard())
            return {"ok": True}

        if action == "approve":
            try:
                final_id = next_daily_id()

                # Create Drive folder for this approved post
                folder_id = create_drive_folder(final_id, GOOGLE_DRIVE_FOLDER_ID)

                # Upload all media from session
                media_links: List[str] = []
                media_idx = 0

                for it in sess["items"]:
                    t = it.get("type")

                    if t == "photo":
                        media_idx += 1
                        file_bytes = download_telegram_file_bytes(it["file_id"])
                        link = upload_bytes_to_drive(
                            file_bytes=file_bytes,
                            filename=f"photo_{media_idx}.jpg",
                            folder_id=folder_id,
                            mime_type="image/jpeg",
                        )
                        media_links.append(f"PHOTO {media_idx}: {link}")

                    elif t == "video":
                        media_idx += 1
                        file_bytes = download_telegram_file_bytes(it["file_id"])
                        link = upload_bytes_to_drive(
                            file_bytes=file_bytes,
                            filename=f"video_{media_idx}.mp4",
                            folder_id=folder_id,
                            mime_type="video/mp4",
                        )
                        media_links.append(f"VIDEO {media_idx}: {link}")

                    elif t == "document":
                        media_idx += 1
                        file_bytes = download_telegram_file_bytes(it["file_id"])
                        filename = it.get("filename") or f"file_{media_idx}"
                        filename = filename.replace("/", "_").replace("\\", "_")
                        link = upload_bytes_to_drive(
                            file_bytes=file_bytes,
                            filename=filename,
                            folder_id=folder_id,
                            mime_type="application/octet-stream",
                        )
                        media_links.append(f"FILE {media_idx}: {link}")

                # Write row to Google Sheet
                append_approved_row(
                    final_id=final_id,
                    materials_count=len(sess["items"]),
                    post_text=last["text"],
                    media_links=media_links,
                )

                send_message(chat_id, f"Approved ✅ Final ID: {final_id}\nSaved to Google Sheet + Drive.")
                clear_session(chat_id)
                send_message(chat_id, "Session auto-closed 🔚\nStart a new one with 🆕 New Draft.", reply_markup=start_keyboard())

            except Exception as e:
                send_message(chat_id, f"Approve failed:\n{e}", reply_markup=start_keyboard())

            return {"ok": True}

        if action == "edit":
            send_message(chat_id, f"Send edits in one message:\n/edit DRAFT <your changes>", reply_markup=start_keyboard())
            return {"ok": True}

        if action == "rewrite":
            last["text"] = build_draft_text(sess["items"])
            send_message(chat_id, f"Rewritten ♻ Draft\n\n{last['text']}", reply_markup=draft_keyboard("DRAFT"))
            return {"ok": True}

        if action == "reject":
            sess["last_draft"] = None
            send_message(chat_id, "Rejected ❌ Draft removed.\nMaterials still in session. Generate again or End Session.", reply_markup=start_keyboard())
            return {"ok": True}

        return {"ok": True}

    # -------------------------
    # Normal messages
    # -------------------------
    msg = data.get("message")
    if not msg:
        return {"ok": True}

    chat_id = msg["chat"]["id"]

    # Greeting opens menu
    raw_text_lower = (msg.get("text") or "").strip().lower()
    if raw_text_lower in MENU_TRIGGERS:
        send_message(chat_id, "Hi 👋 FruitsBurg Bot is live 🚀\n\nChoose an action:", reply_markup=start_keyboard())
        return {"ok": True}

    # Normalize commands
    text = normalize_text_command(msg.get("text", ""))

    # Menu buttons
    if text == "🆕 New Draft":
        clear_session(chat_id)
        send_message(chat_id, "Hi 👋 New draft session started ✅\nSend materials, then ✍️ Generate.", reply_markup=start_keyboard())
        return {"ok": True}

    if text == "📌 Status":
        sess = get_session(chat_id)
        send_message(chat_id, f"Current session: {len(sess['items'])} material(s).", reply_markup=start_keyboard())
        return {"ok": True}

    if text == "✍️ Generate":
        sess = get_session(chat_id)
        if not sess["items"]:
            send_message(chat_id, "No materials yet. Send text/photo/video/file/link first.", reply_markup=start_keyboard())
            return {"ok": True}
        draft_text = build_draft_text(sess["items"])
        sess["last_draft"] = {"id": "DRAFT", "text": draft_text}
        send_message(chat_id, draft_text, reply_markup=draft_keyboard("DRAFT"))
        return {"ok": True}

    if text == "🔚 End Session":
        clear_session(chat_id)
        send_message(chat_id, "Session closed 🔚\nAll materials cleared.", reply_markup=start_keyboard())
        return {"ok": True}

    if text == "🧾 Help":
        send_message(
            chat_id,
            "How to use:\n"
            "1) 🆕 New Draft\n"
            "2) Send materials (text/photo/video/file/link)\n"
            "3) ✍️ Generate\n"
            "4) ✅ Approve (uploads media to Drive + saves row to Sheet)\n"
            "5) Session auto-closes (or 🔚 End Session)\n\n"
            "Tip: type hi / привет / menu to open this menu.",
            reply_markup=start_keyboard(),
        )
        return {"ok": True}

    # Commands
    if text.startswith("/start"):
        send_message(chat_id, "Hi 👋 FruitsBurg Bot is live 🚀\n\nChoose an action:", reply_markup=start_keyboard())
        return {"ok": True}

    if text.startswith("/new"):
        clear_session(chat_id)
        send_message(chat_id, "Hi 👋 New draft session started ✅\nSend materials, then /generate.", reply_markup=start_keyboard())
        return {"ok": True}

    if text.startswith("/status"):
        sess = get_session(chat_id)
        send_message(chat_id, f"Current session: {len(sess['items'])} material(s).", reply_markup=start_keyboard())
        return {"ok": True}

    if text.startswith("/generate"):
        sess = get_session(chat_id)
        if not sess["items"]:
            send_message(chat_id, "No materials yet. Send text/photo/video/file/link first.", reply_markup=start_keyboard())
            return {"ok": True}
        draft_text = build_draft_text(sess["items"])
        sess["last_draft"] = {"id": "DRAFT", "text": draft_text}
        send_message(chat_id, draft_text, reply_markup=draft_keyboard("DRAFT"))
        return {"ok": True}

    if text.startswith("/edit"):
        parts = (msg.get("text") or "").split(" ", 2)
        if len(parts) < 3:
            send_message(chat_id, "Use: /edit DRAFT <your changes>", reply_markup=start_keyboard())
            return {"ok": True}
        _, _draft_id, changes = parts
        sess = get_session(chat_id)
        last = sess.get("last_draft")
        if not last:
            send_message(chat_id, "No active draft. Generate first.", reply_markup=start_keyboard())
            return {"ok": True}
        last["text"] = last["text"] + f"\n\nEdits requested:\n• {changes.strip()}"
        send_message(chat_id, f"Updated ✏ Draft\n\n{last['text']}", reply_markup=draft_keyboard("DRAFT"))
        return {"ok": True}

    # -------------------------
    # Collect materials
    # -------------------------
    sess = get_session(chat_id)

    # Photo (+caption)
    if msg.get("photo"):
        file_id = msg["photo"][-1]["file_id"]
        caption = (msg.get("caption") or "").strip()
        item = {"type": "photo", "file_id": file_id}
        if caption:
            item["caption"] = caption
        sess["items"].append(item)
        send_message(chat_id, f"Photo received. Session materials: {len(sess['items'])}.", reply_markup=start_keyboard())
        return {"ok": True}

    # Video (+caption)
    if msg.get("video"):
        file_id = msg["video"]["file_id"]
        caption = (msg.get("caption") or "").strip()
        item = {"type": "video", "file_id": file_id}
        if caption:
            item["caption"] = caption
        sess["items"].append(item)
        send_message(chat_id, f"Video received. Session materials: {len(sess['items'])}.", reply_markup=start_keyboard())
        return {"ok": True}

    # Document (+caption)
    if msg.get("document"):
        file_id = msg["document"]["file_id"]
        filename = msg["document"].get("file_name") or "file"
        caption = (msg.get("caption") or "").strip()
        item = {"type": "document", "file_id": file_id, "filename": filename}
        if caption:
            item["caption"] = caption
        sess["items"].append(item)
        send_message(chat_id, f"File received ({filename}). Session materials: {len(sess['items'])}.", reply_markup=start_keyboard())
        return {"ok": True}

    # Text or link
    if msg.get("text"):
        t = (msg.get("text") or "").strip()
        url = extract_first_url(t)
        if url:
            sess["items"].append({"type": "link", "url": url, "text": t})
            send_message(chat_id, f"Link received. Session materials: {len(sess['items'])}.\n{url}", reply_markup=start_keyboard())
            return {"ok": True}
        sess["items"].append({"type": "text", "text": t})
        send_message(chat_id, f"Note received. Session materials: {len(sess['items'])}.", reply_markup=start_keyboard())
        return {"ok": True}

    return {"ok": True}