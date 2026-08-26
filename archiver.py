import asyncio
import base64
import json
import logging
import os
import threading
import time
import traceback
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import requests
import firebase_admin
from firebase_admin import credentials, firestore
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import User
from telethon.tl.functions.users import GetFullUserRequest

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("archiver")

# Newer Python versions no longer auto-create an event loop on the main
# thread. Telethon (older versions) expects one to already exist, so we
# create and register it explicitly before the client is built.
try:
    LOOP = asyncio.get_event_loop()
except RuntimeError:
    LOOP = asyncio.new_event_loop()
    asyncio.set_event_loop(LOOP)

# ---------------- tiny web server: /status, /index (viewer UI), /api/* ----------------
START_TIME = datetime.now(timezone.utc)
INDEX_HTML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")
PAGE_SIZE = 50  # messages per page: initial load and each "load older" scroll


def _json_bytes(obj):
    return json.dumps(obj, default=str).encode()


class StatusHandler(BaseHTTPRequestHandler):
    def _send_json(self, obj, status=200):
        body = _json_bytes(obj)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/status":
            uptime = datetime.now(timezone.utc) - START_TIME
            self._send_json(
                {
                    "status": "running",
                    "started_at": START_TIME.isoformat(),
                    "uptime_seconds": int(uptime.total_seconds()),
                }
            )
            return

        if path in ("/", "/index", "/index.html"):
            try:
                with open(INDEX_HTML_PATH, "r", encoding="utf-8") as f:
                    html = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(html.encode())
            except FileNotFoundError:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(b"index.html not found next to archiver.py")
            return

        if path == "/api/users":
            try:
                users = []
                for doc in db.collection("users").stream():
                    u = doc.to_dict()
                    u["id"] = doc.id
                    u["last"] = u.pop("last_message", None)  # already cached, no extra query
                    users.append(u)
                self._send_json(users)
            except Exception as e:
                log.error("/api/users failed:\n%s", traceback.format_exc())
                self._send_json({"error": str(e)}, status=500)
            return

        if path == "/api/messages":
            qs = parse_qs(parsed.query)
            user_id = (qs.get("user_id") or [None])[0]
            after = (qs.get("after") or [None])[0]
            before = (qs.get("before") or [None])[0]
            if not user_id:
                self._send_json({"error": "user_id is required"}, status=400)
                return
            try:
                col = db.collection("users").document(user_id).collection("messages")

                if after:
                    # Incremental poll on an already-open chat: only messages
                    # created/edited/deleted since the client's last known
                    # "updated_at" cursor. A poll with nothing new returns
                    # (and charges for) zero documents.
                    query = col.where("updated_at", ">", after).order_by(
                        "updated_at", direction=firestore.Query.ASCENDING
                    )
                    msgs = [doc.to_dict() for doc in query.stream()]

                elif before:
                    # Scrolled up for older history: one page of messages
                    # strictly older than the oldest one currently shown.
                    query = (
                        col.where("sent_at", "<", before)
                        .order_by("sent_at", direction=firestore.Query.DESCENDING)
                        .limit(PAGE_SIZE)
                    )
                    msgs = [doc.to_dict() for doc in query.stream()]
                    msgs.reverse()

                else:
                    # First open of a chat: only the most recent PAGE_SIZE
                    # messages, not the whole history. Older ones load on
                    # scroll-up via `before` above — they're still safely in
                    # Firestore either way, just not pulled unless asked for.
                    query = col.order_by(
                        "sent_at", direction=firestore.Query.DESCENDING
                    ).limit(PAGE_SIZE)
                    msgs = [doc.to_dict() for doc in query.stream()]
                    msgs.reverse()

                self._send_json(msgs)
            except Exception as e:
                log.error("/api/messages failed:\n%s", traceback.format_exc())
                self._send_json({"error": str(e)}, status=500)
            return

        self.send_response(404)
        self.end_headers()

    def log_message(self, format, *args):
        pass  # keep Render logs clean, no per-request spam


def run_status_server():
    port = int(os.environ.get("PORT", 10000))
    # ThreadingHTTPServer so a slow request (or Telethon doing work on the
    # main thread) can't make /status or /api/* calls queue up behind it.
    server = ThreadingHTTPServer(("0.0.0.0", port), StatusHandler)
    log.info("Web server listening on port %s (/status, /index, /api/*)", port)
    server.serve_forever()

# ---------------- CONFIG (set these as env vars on Render) ----------------
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
SESSION_STRING = os.environ["SESSION_STRING"]
IMGBB_API_KEY = os.environ["IMGBB_API_KEY"]
# Either the raw JSON content of your Firebase service account key, or a
# path to a mounted file containing it.
FIREBASE_CRED = os.environ["FIREBASE_CRED_JSON"]

# ---------------- Firebase init ----------------
if os.path.isfile(FIREBASE_CRED):
    cred = credentials.Certificate(FIREBASE_CRED)
else:
    cred = credentials.Certificate(json.loads(FIREBASE_CRED))
firebase_admin.initialize_app(cred)
db = firestore.client()

# Start the web server (status + viewer UI + API) only after `db` exists,
# since /api/* routes read from it.
threading.Thread(target=run_status_server, daemon=True).start()

# message_id -> user_id. Needed because Telegram's delete event for private
# chats only gives you the message IDs, not which chat they belonged to.
# We fill this in the moment a message is first seen (new/edit) so that a
# later delete event can be traced back to the right user.
MSG_TO_USER = {}

# user_id -> unix timestamp of last full profile sync (bio/dp/etc). Keeps us
# from re-downloading + re-uploading the profile photo on every single message.
PROFILE_SYNCED_AT = {}
PROFILE_SYNC_COOLDOWN = 6 * 60 * 60  # 6 hours

client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)


def upload_to_imgbb(file_bytes: bytes) -> str | None:
    """Uploads raw bytes to imgbb and returns the public URL, or None on failure."""
    try:
        resp = requests.post(
            "https://api.imgbb.com/1/upload",
            data={
                "key": IMGBB_API_KEY,
                "image": base64.b64encode(file_bytes).decode(),
            },
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()["data"]["url"]
    except Exception as e:
        log.error("imgbb upload failed: %s", e)
        return None


async def sync_user_profile(user: User, force: bool = False):
    """Refreshes username/name/phone/bio/dp for this user in Firestore.

    Profile info rarely changes message-to-message, so we only actually hit
    Telegram + imgbb + Firestore for this at most once per PROFILE_SYNC_COOLDOWN
    per user — otherwise every single message would trigger a full profile
    re-fetch and re-upload, burning through reads/writes and imgbb calls for
    no reason.
    """
    now = time.time()
    last_synced = PROFILE_SYNCED_AT.get(user.id, 0)
    if not force and (now - last_synced) < PROFILE_SYNC_COOLDOWN:
        return
    PROFILE_SYNCED_AT[user.id] = now

    bio = None
    try:
        full = await client(GetFullUserRequest(user.id))
        bio = full.full_user.about
    except Exception as e:
        log.warning("could not fetch full user info: %s", e)

    dp_url = None
    try:
        photo_bytes = await client.download_profile_photo(user, file=bytes)
        if photo_bytes:
            dp_url = upload_to_imgbb(photo_bytes)
    except Exception as e:
        log.warning("dp download failed: %s", e)

    db.collection("users").document(str(user.id)).set(
        {
            "user_id": user.id,
            "username": user.username,
            "first_name": user.first_name,
            "last_name": user.last_name,
            "phone": user.phone,
            "bio": bio,
            "dp_url": dp_url,
            "profile_updated_at": datetime.now(timezone.utc).isoformat(),
        },
        merge=True,
    )


async def save_message(event, peer: User):
    # `peer` is always the OTHER person in this private chat — whether the
    # message was sent by them or by us. This is the key we group by, so
    # both sides of the conversation land under the same user document.
    user_id = peer.id
    msg = event.message
    MSG_TO_USER[msg.id] = user_id

    media_url = None
    if msg.media:
        try:
            file_bytes = await client.download_media(msg, file=bytes)
            if file_bytes:
                media_url = upload_to_imgbb(file_bytes)
        except Exception as e:
            log.error("media download failed: %s", e)

    now_iso = datetime.now(timezone.utc).isoformat()
    text = msg.raw_text or ""
    data = {
        "message_id": msg.id,
        "text": text,
        "media_url": media_url,
        "sent_at": msg.date.isoformat() if msg.date else None,
        "edited_at": None,
        "deleted": False,
        "deleted_at": None,
        "direction": "outgoing" if msg.out else "incoming",
        "updated_at": now_iso,
    }

    db.collection("users").document(str(user_id)).collection("messages").document(
        str(msg.id)
    ).set(data, merge=True)

    # Cache a preview of the latest message directly on the user doc, so the
    # sidebar (/api/users) doesn't need one extra query per user just to
    # show "last message" — that alone roughly doubles read cost otherwise.
    db.collection("users").document(str(user_id)).set(
        {
            "last_message": {
                "text": text,
                "media_url": media_url,
                "sent_at": data["sent_at"],
                "deleted": False,
            }
        },
        merge=True,
    )

    await sync_user_profile(peer)


@client.on(events.NewMessage)
async def on_new_message(event):
    if not event.is_private:
        return  # skip groups/channels entirely
    peer = await event.get_chat()
    if not isinstance(peer, User) or peer.bot:
        return  # skip bot chats
    try:
        await save_message(event, peer)
    except Exception as e:
        log.error("failed to save new message: %s", e)


@client.on(events.MessageEdited)
async def on_edit(event):
    if not event.is_private:
        return
    peer = await event.get_chat()
    if not isinstance(peer, User) or peer.bot:
        return

    msg = event.message
    MSG_TO_USER[msg.id] = peer.id
    now_iso = datetime.now(timezone.utc).isoformat()
    text = msg.raw_text or ""

    db.collection("users").document(str(peer.id)).collection("messages").document(
        str(msg.id)
    ).set(
        {
            "text": text,
            "edited_at": now_iso,
            "updated_at": now_iso,
        },
        merge=True,
    )


@client.on(events.MessageDeleted)
async def on_delete(event):
    now_iso = datetime.now(timezone.utc).isoformat()
    for msg_id in event.deleted_ids:
        user_id = MSG_TO_USER.get(msg_id)
        if user_id is None:
            # We never saw this message (e.g. bot restarted before it arrived),
            # so we have no way to know which chat it belonged to.
            continue
        db.collection("users").document(str(user_id)).collection("messages").document(
            str(msg_id)
        ).set(
            {
                "deleted": True,
                "deleted_at": now_iso,
                "updated_at": now_iso,
            },
            merge=True,
        )


async def main():
    await client.start()
    me = await client.get_me()
    log.info("Archiver running as %s (id=%s)", me.username or me.first_name, me.id)
    await client.run_until_disconnected()


if __name__ == "__main__":
    while True:
        try:
            LOOP.run_until_complete(main())
        except KeyboardInterrupt:
            break
        except Exception as e:
            log.error("Crashed, restarting in 5s: %s", e)
            time.sleep(5)
