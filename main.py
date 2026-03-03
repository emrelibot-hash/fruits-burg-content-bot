import os
import time
import json
import requests
from datetime import datetime, timezone
from flask import Flask, request

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET")

GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID")
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")

if not BOT_TOKEN:
    raise RuntimeError("Missing TELEGRAM_BOT_TOKEN")

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
app = Flask(__name__)

SESSIONS = {}
_sheets_service = None


# =========================
# Google Sheets Integration
# =========================

def get_sheets_service():
    global _sheets_service
    if _sheets_service:
        return _sheets_service

    if not GOOGLE_SHEET_ID or not GOOGLE_SERVICE_ACCOUNT_JSON:
        raise RuntimeError("Missing GOOGLE_SHEET_ID or GOOGLE_SERVICE_ACCOUNT_JSON")

    info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    creds = Credentials.from_service_account_info(
        info,
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )

    _sheets_service = build("sheets", "v4", credentials=creds, cache_discovery=False)
    return _sheets_service


def append_approved_to_sheet(draft_id, materials_count, post_text):
    service = get_sheets_service()
    created_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    values = [[
        draft_id,
        "Approved",
        created_utc,
        materials_count,
        post_text,
        ""
    ]]

    service.spreadsheets().values().append(
        spreadsheetId=GOOGLE_SHEET_ID,
        range="Sheet1!A:F",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": values},
    ).execute()


# =========================
# Telegram Helpers
# =========================

def send_message(chat_id, text, reply_markup=None):
    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup

    requests.post(f"{TELEGRAM_API}/sendMessage", json=payload)


def get_session(chat_id):
    if chat_id not in SESSIONS:
        SESSIONS[chat_id] = {"items": [], "last_draft": None}
    return SESSIONS[chat_id]


def make_draft_id():
    return f"FB-{int(time.time())}"


def build_draft_text(item_count):
    return (
        f"Dried Persimmon Market Update\n\n"
        f"- Based on {item_count} material(s)\n"
        f"- Market insight: demand planning becomes critical before Q4\n"
        f"- Buyers should secure specifications and volumes early\n\n"
        f"Open for inquiries and supply discussions.\n\n"
        f"#driedfruit #persimmon #export #foodtrade #supplychain"
    )


def draft_keyboard(draft_id):
    return {
        "inline_keyboard": [
            [
                {"text": "✅ Approve", "callback_data": f"approve|{draft_id}"},
                {"text": "❌ Reject", "callback_data": f"reject|{draft_id}"}
            ]
        ]
    }


# =========================
# Flask Routes
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

    # Handle button clicks
    if "callback_query" in data:
        cq = data["callback_query"]
        chat_id = cq["message"]["chat"]["id"]
        action, draft_id = cq["data"].split("|")

        sess = get_session(chat_id)
        last = sess.get("last_draft")

        if not last or last["id"] != draft_id:
            send_message(chat_id, "Draft not found. Generate again with /generate.")
            return {"ok": True}

        if action == "approve":
            try:
                append_approved_to_sheet(
                    draft_id=draft_id,
                    materials_count=len(sess["items"]),
                    post_text=last["text"]
                )
                send_message(chat_id, f"Approved ✅ Draft ID: {draft_id}\nSaved to Google Sheet.")
            except Exception as e:
                send_message(chat_id, f"Approved but failed to save:\n{e}")

        elif action == "reject":
            send_message(chat_id, f"Rejected ❌ Draft ID: {draft_id}")

        return {"ok": True}

    # Handle messages
    if "message" in data:
        msg = data["message"]
        chat_id = msg["chat"]["id"]
        text = msg.get("text", "")

        if text == "/start":
            send_message(chat_id, "FruitsBurg Bot is live 🚀")
            return {"ok": True}

        if text == "/new":
            sess = get_session(chat_id)
            sess["items"] = []
            sess["last_draft"] = None
            send_message(chat_id, "New draft session started.")
            return {"ok": True}

        if text == "/status":
            sess = get_session(chat_id)
            send_message(chat_id, f"Session materials: {len(sess['items'])}")
            return {"ok": True}

        if text == "/generate":
            sess = get_session(chat_id)
            if not sess["items"]:
                send_message(chat_id, "No materials yet.")
                return {"ok": True}

            draft_id = make_draft_id()
            draft_text = build_draft_text(len(sess["items"]))
            sess["last_draft"] = {"id": draft_id, "text": draft_text}

            send_message(chat_id, f"Draft ID: {draft_id}\n\n{draft_text}", draft_keyboard(draft_id))
            return {"ok": True}

        # Collect materials
        if msg.get("photo"):
            sess = get_session(chat_id)
            sess["items"].append({"type": "photo"})
            send_message(chat_id, f"Photo received. Session materials: {len(sess['items'])}")
            return {"ok": True}

        if text:
            sess = get_session(chat_id)
            sess["items"].append({"type": "text", "text": text})
            send_message(chat_id, f"Note received. Session materials: {len(sess['items'])}")
            return {"ok": True}

    return {"ok": True}
