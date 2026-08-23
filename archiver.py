import asyncio
import base64
import json
import logging
import os
import time
from datetime import datetime, timezone

import requests
import firebase_admin
from firebase_admin import credentials, firestore
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import User
from telethon.tl.functions.users import GetFullUserRequest

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("archiver")

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

# message_id -> user_id. Needed because Telegram's delete event for private
# chats only gives you the message IDs, not which chat they belonged to.
# We fill this in the moment a message is first seen (new/edit) so that a
# later delete event can be traced back to the right user.
MSG_TO_USER = {}

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


async def sync_user_profile(user: User):
    """Refreshes username/name/phone/bio/dp for this user in Firestore."""
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


async def save_message(event, sender: User):
    user_id = sender.id
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

    data = {
        "message_id": msg.id,
        "text": msg.raw_text or "",
        "media_url": media_url,
        "sent_at": msg.date.isoformat() if msg.date else None,
        "edited_at": None,
        "deleted": False,
        "deleted_at": None,
        "direction": "outgoing" if msg.out else "incoming",
    }

    db.collection("users").document(str(user_id)).collection("messages").document(
        str(msg.id)
    ).set(data, merge=True)

    await sync_user_profile(sender)


@client.on(events.NewMessage)
async def on_new_message(event):
    if not event.is_private:
        return  # skip groups/channels entirely
    sender = await event.get_sender()
    if not isinstance(sender, User) or sender.bot:
        return
    try:
        await save_message(event, sender)
    except Exception as e:
        log.error("failed to save new message: %s", e)


@client.on(events.MessageEdited)
async def on_edit(event):
    if not event.is_private:
        return
    sender = await event.get_sender()
    if not isinstance(sender, User) or sender.bot:
        return

    msg = event.message
    MSG_TO_USER[msg.id] = sender.id

    db.collection("users").document(str(sender.id)).collection("messages").document(
        str(msg.id)
    ).set(
        {
            "text": msg.raw_text or "",
            "edited_at": datetime.now(timezone.utc).isoformat(),
        },
        merge=True,
    )


@client.on(events.MessageDeleted)
async def on_delete(event):
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
                "deleted_at": datetime.now(timezone.utc).isoformat(),
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
            client.loop.run_until_complete(main())
        except KeyboardInterrupt:
            break
        except Exception as e:
            log.error("Crashed, restarting in 5s: %s", e)
            time.sleep(5)
