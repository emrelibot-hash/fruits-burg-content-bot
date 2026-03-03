import os
import time
import secrets
import requests
from flask import Flask, request

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET")

if not BOT_TOKEN:
    raise RuntimeError("Missing TELEGRAM_BOT_TOKEN")

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
app = Flask(__name__)

# In-memory sessions (MVP). Will be lost on redeploy — ok for now.
# session[chat_id] = {"items": [{"type": "...", "file_id": "...", "text": "...", "ts": ...}], "last_draft": {...}}
SESSIONS = {}

def send_message(chat_id: int, text: str, reply_markup: dict | None = None):
    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    r = requests.post(f"{TELEGRAM_API}/sendMessage", json=payload, timeout=20)
    r.raise_for_status()
    return r.json()

def get_session(chat_id: int):
    if chat_id not in SESSIONS:
        SESSIONS[chat_id] = {"items": [], "last_draft": None}
    return SESSIONS[chat_id]

def add_item(chat_id: int, item: dict):
    sess = get_session(chat_id)
    sess["items"].append(item)

def make_draft_id():
    # Simple sequential-ish ID, good enough for MVP
    return f"FB-{int(time.time())}"

def build_draft_text(item_count: int):
    # Placeholder draft generator (next step we will use OpenAI)
    return (
        f"**Dried Persimmon Market Note**\n\n"
        f"- Update based on {item_count} material(s)\n"
        f"- Insight: demand planning matters most before Q4\n"
        f"- Buyer takeaway: secure specs and volumes early\n\n"
        f"Open to inquiries and supply planning discussions.\n\n"
        f"#driedfruit #persimmon #export #foodtrade #supplychain"
    )

def draft_keyboard(draft_id: str):
    return {
        "inline_keyboard": [
            [{"text": "✅ Approve", "callback_data": f"approve|{draft_id}"},
             {"text": "✏ Edit", "callback_data": f"edit|{draft_id}"}],
            [{"text": "♻ Rewrite", "callback_data": f"rewrite|{draft_id}"},
             {"text": "❌ Reject", "callback_data": f"reject|{draft_id}"}],
        ]
    }

@app.get("/")
def health():
    return {"status": "running"}

@app.post("/webhook")
def webhook():
    # Optional: secret-token gate
    if WEBHOOK_SECRET:
        secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
        if secret != WEBHOOK_SECRET:
            return {"error": "unauthorized"}, 403

    data = request.get_json(silent=True) or {}

    # Handle button callbacks
    if "callback_query" in data:
        cq = data["callback_query"]
        chat_id = cq["message"]["chat"]["id"]
        action, draft_id = (cq.get("data") or "").split("|", 1)

        sess = get_session(chat_id)
        last = sess.get("last_draft")

        if not last or last.get("id") != draft_id:
            send_message(chat_id, f"Draft {draft_id} not found in session (MVP). Generate again with /generate.")
            return {"ok": True}

        if action == "approve":
            send_message(chat_id, f"Approved ✅ Draft ID: {draft_id}\n\n(Next step: we’ll write this into Google Sheet.)")
        elif action == "edit":
            send_message(chat_id, f"Send your edits in one message.\nStart with:\n/edit {draft_id} <your changes>")
        elif action == "rewrite":
            # regenerate placeholder text
            last["text"] = build_draft_text(item_count=len(sess["items"]))
            send_message(chat_id, f"Rewritten ♻ Draft ID: {draft_id}\n\n{last['text']}", reply_markup=draft_keyboard(draft_id))
        elif action == "reject":
            send_message(chat_id, f"Rejected ❌ Draft ID: {draft_id}")
            sess["last_draft"] = None

        return {"ok": True}

    # Handle messages
    msg = data.get("message")
    if not msg:
        return {"ok": True}

    chat_id = msg["chat"]["id"]
    text = msg.get("text", "")

    # Commands
    if text.startswith("/start"):
        send_message(chat_id, "FruitsBurg Bot is live 🚀\n\nSend materials (text/photo/file), then /generate.")
        return {"ok": True}

    if text.startswith("/new"):
        sess = get_session(chat_id)
        sess["items"] = []
        sess["last_draft"] = None
        send_message(chat_id, "New draft session started ✅\nSend materials, then /generate.")
        return {"ok": True}

    if text.startswith("/status"):
        sess = get_session(chat_id)
        send_message(chat_id, f"Current session: {len(sess['items'])} material(s).")
        return {"ok": True}

    if text.startswith("/generate"):
        sess = get_session(chat_id)
        if not sess["items"]:
            send_message(chat_id, "No materials yet. Send text/photo/file first.")
            return {"ok": True}

        draft_id = make_draft_id()
        draft_text = build_draft_text(item_count=len(sess["items"]))
        sess["last_draft"] = {"id": draft_id, "text": draft_text}

        send_message(chat_id, f"Draft ID: {draft_id}\n\n{draft_text}", reply_markup=draft_keyboard(draft_id))
        return {"ok": True}

    if text.startswith("/edit "):
        # format: /edit <draft_id> <changes>
        parts = text.split(" ", 2)
        if len(parts) < 3:
            send_message(chat_id, "Use: /edit <draft_id> <your changes>")
            return {"ok": True}
        _, draft_id, changes = parts
        sess = get_session(chat_id)
        last = sess.get("last_draft")
        if not last or last.get("id") != draft_id:
            send_message(chat_id, f"Draft {draft_id} not found. Use /generate again.")
            return {"ok": True}

        # MVP: just append edits note
        last["text"] = last["text"] + f"\n\nEdits requested:\n- {changes}"
        send_message(chat_id, f"Updated ✏ Draft ID: {draft_id}\n\n{last['text']}", reply_markup=draft_keyboard(draft_id))
        return {"ok": True}

    # Collect materials
    if msg.get("photo"):
        # Take best quality photo (last item)
        file_id = msg["photo"][-1]["file_id"]
        add_item(chat_id, {"type": "photo", "file_id": file_id, "ts": time.time()})
        sess = get_session(chat_id)
        send_message(chat_id, f"Photo received. Session materials: {len(sess['items'])}. Send more or /generate.")
        return {"ok": True}

    if msg.get("document"):
        file_id = msg["document"]["file_id"]
        filename = msg["document"].get("file_name", "file")
        add_item(chat_id, {"type": "document", "file_id": file_id, "filename": filename, "ts": time.time()})
        sess = get_session(chat_id)
        send_message(chat_id, f"File received ({filename}). Session materials: {len(sess['items'])}. Send more or /generate.")
        return {"ok": True}

    # Plain text material
    if text:
        add_item(chat_id, {"type": "text", "text": text, "ts": time.time()})
        sess = get_session(chat_id)
        send_message(chat_id, f"Note received. Session materials: {len(sess['items'])}. Send more or /generate.")
        return {"ok": True}

    return {"ok": True}
