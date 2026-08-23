# Telegram Message Archiver

## What it does
- Listens to your Telegram account (via session string) for new/edited/deleted
  private messages only (groups and channels are ignored).
- Saves every message to Firestore under `users/{telegram_user_id}/messages/{message_id}`.
- Downloads any photo/video and uploads it to imgbb, storing just the URL.
- Keeps each user's profile (username, name, phone, bio, DP) updated in
  `users/{telegram_user_id}`.
- If a message gets deleted (either side), it is NOT removed from Firestore —
  it's marked `deleted: true` with a timestamp, so nothing is lost.

## Env vars to set on Render
| Variable | Where to get it |
|---|---|
| `API_ID` | https://my.telegram.org → API development tools |
| `API_HASH` | same page as above |
| `SESSION_STRING` | generate locally with your `session.py` script (never share this) |
| `IMGBB_API_KEY` | free key from https://api.imgbb.com/ |
| `FIREBASE_CRED_JSON` | paste the full content of your Firebase service account JSON |

## Deploying on Render
1. Push this folder to a GitHub repo.
2. On Render: New → Background Worker (not Web Service — this has no HTTP server).
3. Build command: `pip install -r requirements.txt`
4. Start command: `python archiver.py`
5. Add the env vars above under Environment.
6. Deploy. Check logs for "Archiver running as ...".

## Notes
- Background Workers on Render's free tier can still sleep/restart occasionally —
  the `while True` retry loop in the script handles reconnects automatically,
  session string doesn't expire from disconnects.
- Don't regenerate the session string unless you deliberately log out that
  session from Telegram's Active Sessions.
