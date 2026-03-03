import os
import time
import json
import re
import io
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List

import requests
from flask import Flask, request

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload


# =========================
# ENV
# =========================
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET")

GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID")
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
GOOGLE_DRIVE_FOLDER_ID = os.environ.get("GOOGLE_DRIVE_FOLDER_ID")

if not BOT_TOKEN:
    raise RuntimeError("Missing TELEGRAM_BOT_TOKEN")

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

app = Flask(__name__)

SESSIONS: Dict[int, Dict[str, Any]] = {}
_sheets_service = None
_drive_service = None


# =========================
# HELPERS
# =========================
def now_utc_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def normalize_text_command(text: str) -> str:
    if not text:
        return ""
    t = text.strip()
    if t.startswith("/"):
        t = "/" + t[1:].replace(" ", "")
    return t


def get_session(chat_id: int):
    if chat_id not in SESSIONS:
        SESSIONS[chat_id] = {"items": [], "last_draft": None}
    return SESSIONS[chat_id]


def clear_session(chat_id: int):
    SESSIONS[chat_id] = {"items": [], "last_draft": None}


def send_message(chat_id: int, text: str, reply_markup=None):
    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup

    requests.post(f"{TELEGRAM_API}/sendMessage", json=payload)


def start_keyboard():
    return {
        "keyboard": [
            [{"text": "🆕 New Draft"}, {"text": "✍️ Generate"}],
            [{"text": "📌 Status"}, {"text": "🔚 End Session"}],
            [{"text": "🧾 Help"}],
        ],
        "resize_keyboard": True,
    }


def draft_keyboard(draft_id: str):
    return {
        "inline_keyboard": [
            [
                {"text": "✅ Approve", "callback_data": f"approve|{draft_id}"},
                {"text": "❌ Reject", "callback_data": f"reject|{draft_id}"},
            ]
        ]
    }


# =========================
# GOOGLE SERVICES
# =========================
def get_credentials():
    info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    return Credentials.from_service_account_info(
        info,
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ],
    )


def get_sheets_service():
    global _sheets_service
    if _sheets_service:
        return _sheets_service
    creds = get_credentials()
    _sheets_service = build("sheets", "v4", credentials=creds, cache_discovery=False)
    return _sheets_service


def get_drive_service():
    global _drive_service
    if _drive_service:
        return _drive_service
    creds = get_credentials()
    _drive_service = build("drive", "v3", credentials=creds, cache_discovery=False)
    return _drive_service


# =========================
# ID GENERATION
# =========================
def next_daily_id(prefix="FB"):
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
        v = row[0]
        if v.startswith(base):
            tail = v.replace(base, "")
            if tail.isdigit():
                max_n = max(max_n, int(tail))

    return f"{base}{max_n + 1:03d}"


# =========================
# TELEGRAM FILE DOWNLOAD
# =========================
def download_telegram_file(file_id):
    resp = requests.get(f"{TELEGRAM_API}/getFile?file_id={file_id}").json()
    file_path = resp["result"]["file_path"]
    file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
    return requests.get(file_url).content


# =========================
# DRIVE UPLOAD
# =========================
def create_drive_folder(name, parent_id):
    service = get_drive_service()

    metadata = {
        "name": name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_id],
    }

    folder = service.files().create(body=metadata, fields="id").execute()
    return folder["id"]


def upload_file_to_drive(file_bytes, filename, folder_id):
    service = get_drive_service()

    file_metadata = {
        "name": filename,
        "parents": [folder_id],
    }

    media = MediaIoBaseUpload(io.BytesIO(file_bytes), resumable=True)

    file = service.files().create(
        body=file_metadata,
        media_body=media,
        fields="id, webViewLink"
    ).execute()

    return file["webViewLink"]


# =========================
# DRAFT GENERATOR
# =========================
def build_draft_text(items):
    return (
        f"Dried Persimmon Market Update\n\n"
        f"- Based on {len(items)} material(s)\n"
        f"- Demand planning becomes critical before Q4\n\n"
        f"Open for inquiries.\n\n"
        f"#persimmon #export #driedfruit"
    )


# =========================
# FLASK ROUTES
# =========================
@app.get("/")
def health():
    return {"status": "running"}


@app.post("/webhook")
def webhook():
    if WEBHOOK_SECRET:
        secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
        if secret != WEBHOOK_SECRET:
            return {"error": "unauthorized"}, 403

    data = request.json

    # CALLBACK BUTTONS
    if "callback_query" in data:
        cq = data["callback_query"]
        chat_id = cq["message"]["chat"]["id"]
        action, _ = cq["data"].split("|")

        sess = get_session(chat_id)
        last = sess.get("last_draft")

        if not last:
            send_message(chat_id, "No active draft.")
            return {"ok": True}

        if action == "approve":
            final_id = next_daily_id()

            # CREATE DRIVE FOLDER
            folder_id = create_drive_folder(final_id, GOOGLE_DRIVE_FOLDER_ID)

            media_links = []

            for idx, item in enumerate(sess["items"]):
                if item.get("type") == "photo":
                    file_bytes = download_telegram_file(item["file_id"])
                    filename = f"image_{idx+1}.jpg"
                    link = upload_file_to_drive(file_bytes, filename, folder_id)
                    media_links.append(link)

            # SAVE TO SHEET
            service = get_sheets_service()
            service.spreadsheets().values().append(
                spreadsheetId=GOOGLE_SHEET_ID,
                range="Sheet1!A:G",
                valueInputOption="RAW",
                body={
                    "values": [[
                        final_id,
                        "Approved",
                        now_utc_str(),
                        len(sess["items"]),
                        last["text"],
                        "",
                        "\n".join(media_links)
                    ]]
                }
            ).execute()

            send_message(chat_id, f"Approved ✅ Final ID: {final_id}")
            clear_session(chat_id)
            send_message(chat_id, "Session auto-closed 🔚")

        return {"ok": True}

    # NORMAL MESSAGES
    msg = data.get("message")
    if not msg:
        return {"ok": True}

    chat_id = msg["chat"]["id"]
    text = normalize_text_command(msg.get("text", ""))

    raw_text = (msg.get("text") or "").strip().lower()
    if raw_text in {"hi", "hello", "привет", "menu", "start"}:
        send_message(chat_id, "Hi 👋 Choose action:", reply_markup=start_keyboard())
        return {"ok": True}

    if text == "/start":
        send_message(chat_id, "Hi 👋 Choose action:", reply_markup=start_keyboard())
        return {"ok": True}

    if text in {"🆕 New Draft", "/new"}:
        clear_session(chat_id)
        send_message(chat_id, "New draft started ✅")
        return {"ok": True}

    if text in {"📌 Status", "/status"}:
        sess = get_session(chat_id)
        send_message(chat_id, f"Materials: {len(sess['items'])}")
        return {"ok": True}

    if text in {"✍️ Generate", "/generate"}:
        sess = get_session(chat_id)
        if not sess["items"]:
            send_message(chat_id, "No materials.")
            return {"ok": True}

        draft_id = "temp"
        draft_text = build_draft_text(sess["items"])
        sess["last_draft"] = {"id": draft_id, "text": draft_text}

        send_message(chat_id, draft_text, draft_keyboard(draft_id))
        return {"ok": True}

    if text in {"🔚 End Session"}:
        clear_session(chat_id)
        send_message(chat_id, "Session closed 🔚")
        return {"ok": True}

    # COLLECT PHOTO
    if msg.get("photo"):
        file_id = msg["photo"][-1]["file_id"]
        sess = get_session(chat_id)
        sess["items"].append({"type": "photo", "file_id": file_id})
        send_message(chat_id, f"Photo received. Total: {len(sess['items'])}")
        return {"ok": True}

    if msg.get("text"):
        sess = get_session(chat_id)
        sess["items"].append({"type": "text", "text": msg["text"]})
        send_message(chat_id, f"Note received. Total: {len(sess['items'])}")
        return {"ok": True}

    return {"ok": True}
