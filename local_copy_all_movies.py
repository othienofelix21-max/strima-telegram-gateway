import asyncio
import ast
import json
import os
import re
from pathlib import Path

from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.sessions import StringSession

# STRIMA movie channels already configured in the project.
SOURCE_CHANNEL_ID = -1001884106307      # OLD movie source channel
DESTINATION_CHANNEL_ID = -1003386868177 # STRIMA Premium Movies
COPY_DELAY_SECONDS = 1.5

BASE_DIR = Path(__file__).resolve().parent
SESSION_FILE = BASE_DIR / "gateway_session_string.txt"
LOCAL_CREDS_FILE = BASE_DIR / "local_telegram_credentials.json"
PROGRESS_FILE = BASE_DIR / "movie_copy_progress.json"
CREATE_SESSION_FILE = BASE_DIR / "create_gateway_session.py"

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".mpeg", ".mpg", ".ts"}


def is_video_message(message):
    file_obj = getattr(message, "file", None)
    if not file_obj:
        return False
    mime = str(getattr(file_obj, "mime_type", "") or "").lower()
    name = str(getattr(file_obj, "name", "") or "").lower()
    suffix = Path(name).suffix.lower() if name else ""
    return mime.startswith("video/") or suffix in VIDEO_EXTENSIONS


def document_id(message):
    document = getattr(message, "document", None)
    if document is not None and getattr(document, "id", None) is not None:
        return int(document.id)
    media = getattr(message, "media", None)
    document = getattr(media, "document", None)
    if document is not None and getattr(document, "id", None) is not None:
        return int(document.id)
    return None


def fingerprint(message):
    file_obj = getattr(message, "file", None)
    if not file_obj:
        return None
    name = str(getattr(file_obj, "name", "") or "").strip().lower()
    size = int(getattr(file_obj, "size", 0) or 0)
    if not name and not size:
        return None
    return (name, size)


def load_progress():
    if not PROGRESS_FILE.exists():
        return {"last_source_message_id": 0, "copied": 0, "existing": 0, "failed": 0}
    try:
        data = json.loads(PROGRESS_FILE.read_text(encoding="utf-8"))
        return {
            "last_source_message_id": int(data.get("last_source_message_id", 0) or 0),
            "copied": int(data.get("copied", 0) or 0),
            "existing": int(data.get("existing", 0) or 0),
            "failed": int(data.get("failed", 0) or 0),
        }
    except Exception:
        return {"last_source_message_id": 0, "copied": 0, "existing": 0, "failed": 0}


def save_progress(state):
    temp = PROGRESS_FILE.with_suffix(".tmp")
    temp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    temp.replace(PROGRESS_FILE)


def _literal_assignments_from_python(path):
    values = {}
    if not path.exists():
        return values
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return values
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        value_node = node.value
        try:
            value = ast.literal_eval(value_node)
        except Exception:
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                values[target.id.upper()] = value
    return values


def load_api_credentials():
    api_id = os.getenv("TG_API_ID") or os.getenv("API_ID")
    api_hash = os.getenv("TG_API_HASH") or os.getenv("API_HASH")

    if (not api_id or not api_hash) and LOCAL_CREDS_FILE.exists():
        try:
            saved = json.loads(LOCAL_CREDS_FILE.read_text(encoding="utf-8"))
            api_id = api_id or saved.get("api_id")
            api_hash = api_hash or saved.get("api_hash")
        except Exception:
            pass

    if not api_id or not api_hash:
        assignments = _literal_assignments_from_python(CREATE_SESSION_FILE)
        for key in ("TG_API_ID", "API_ID", "TELEGRAM_API_ID"):
            if not api_id and assignments.get(key):
                api_id = assignments[key]
        for key in ("TG_API_HASH", "API_HASH", "TELEGRAM_API_HASH"):
            if not api_hash and assignments.get(key):
                api_hash = assignments[key]

    if not api_id or not api_hash:
        print("\nTelegram API ID/HASH were not found automatically.")
        print("Enter the SAME API credentials that were used to create gateway_session_string.txt.")
        api_id = input("Telegram API ID: ").strip()
        api_hash = input("Telegram API HASH: ").strip()
        if not api_id or not api_hash:
            raise RuntimeError("Telegram API credentials are required.")
        LOCAL_CREDS_FILE.write_text(
            json.dumps({"api_id": int(api_id), "api_hash": api_hash}, indent=2),
            encoding="utf-8",
        )
        print(f"Saved locally to: {LOCAL_CREDS_FILE}")

    return int(api_id), str(api_hash).strip()


async def resolve_channel(client, channel_id):
    dialogs = await client.get_dialogs()
    for dialog in dialogs:
        if int(dialog.id) == int(channel_id):
            return dialog.input_entity, dialog.title
    raise RuntimeError(f"Telegram channel {channel_id} is not visible to this account.")


async def build_destination_index(client, destination):
    print("\nScanning Premium Movies first so existing movies are NOT copied again...")
    doc_ids = set()
    fingerprints = set()
    count = 0
    async for message in client.iter_messages(destination):
        if not is_video_message(message):
            continue
        count += 1
        doc = document_id(message)
        if doc is not None:
            doc_ids.add(doc)
        fp = fingerprint(message)
        if fp is not None:
            fingerprints.add(fp)
        if count % 100 == 0:
            print(f"  Indexed {count} existing destination videos...")
    print(f"Destination index ready: {count} videos already in Premium Movies.\n")
    return doc_ids, fingerprints


async def copy_message(client, source, destination, message):
    # Re-fetch just before copying so Telegram gives us a fresh file reference.
    fresh = await client.get_messages(source, ids=message.id)
    if not fresh or not getattr(fresh, "media", None):
        raise RuntimeError("Source message/media disappeared")

    try:
        result = await client.send_file(
            destination,
            file=fresh.media,
            caption=fresh.message or "",
            formatting_entities=(fresh.entities or None),
        )
    except FloodWaitError as exc:
        wait_seconds = int(getattr(exc, "seconds", 0) or 0) + 2
        print(f"Telegram asked us to wait {wait_seconds}s. Waiting automatically...")
        await asyncio.sleep(wait_seconds)
        fresh = await client.get_messages(source, ids=message.id)
        result = await client.send_file(
            destination,
            file=fresh.media,
            caption=fresh.message or "",
            formatting_entities=(fresh.entities or None),
        )

    if isinstance(result, (list, tuple)):
        result = result[0] if result else None
    if not result or not getattr(result, "id", None):
        raise RuntimeError("Telegram did not return a destination message ID")
    return result


async def main():
    print("=" * 72)
    print("STRIMA - LOCAL COPY ALL MOVIES")
    print("OLD Telegram movie channel  ->  STRIMA Premium Movies")
    print("NO movie size limit. Existing destination movies are skipped.")
    print("Progress is saved so the job can resume after a restart.")
    print("=" * 72)

    if not SESSION_FILE.exists():
        raise RuntimeError(f"Missing session file: {SESSION_FILE}")

    session_string = SESSION_FILE.read_text(encoding="utf-8").strip()
    if not session_string:
        raise RuntimeError("gateway_session_string.txt is empty")

    api_id, api_hash = load_api_credentials()
    client = TelegramClient(
        StringSession(session_string),
        api_id,
        api_hash,
        sequential_updates=False,
        connection_retries=10,
        retry_delay=2,
        auto_reconnect=True,
    )

    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise RuntimeError("The local Telegram session is not authorized anymore.")

        me = await client.get_me()
        print(f"\nTelegram connected as: @{getattr(me, 'username', None) or ''} ({me.id})")

        source, source_title = await resolve_channel(client, SOURCE_CHANNEL_ID)
        destination, destination_title = await resolve_channel(client, DESTINATION_CHANNEL_ID)
        print(f"SOURCE      : {source_title} ({SOURCE_CHANNEL_ID})")
        print(f"DESTINATION : {destination_title} ({DESTINATION_CHANNEL_ID})")

        destination_doc_ids, destination_fingerprints = await build_destination_index(client, destination)

        state = load_progress()
        last_id = int(state["last_source_message_id"] or 0)
        if last_id > 0:
            print(f"RESUMING after source message {last_id}.")
        else:
            print("Starting from the oldest source messages.")

        scanned_this_run = 0
        copied_this_run = 0
        existing_this_run = 0
        failed_this_run = 0

        async for message in client.iter_messages(source, reverse=True, min_id=last_id):
            scanned_this_run += 1

            # Always advance the resume marker, including non-video posts.
            state["last_source_message_id"] = int(message.id)

            if not is_video_message(message):
                save_progress(state)
                continue

            file_obj = getattr(message, "file", None)
            file_name = str(getattr(file_obj, "name", "") or f"message-{message.id}")
            size_mb = (int(getattr(file_obj, "size", 0) or 0) / 1024 / 1024)

            doc = document_id(message)
            fp = fingerprint(message)
            already_exists = (
                (doc is not None and doc in destination_doc_ids)
                or (fp is not None and fp in destination_fingerprints)
            )

            if already_exists:
                existing_this_run += 1
                state["existing"] += 1
                print(f"SKIP existing | source={message.id} | {file_name}")
                save_progress(state)
                continue

            try:
                destination_message = await copy_message(client, source, destination, message)
                copied_this_run += 1
                state["copied"] += 1

                dest_doc = document_id(destination_message)
                if dest_doc is not None:
                    destination_doc_ids.add(dest_doc)
                dest_fp = fingerprint(destination_message)
                if dest_fp is not None:
                    destination_fingerprints.add(dest_fp)

                print(
                    f"COPIED #{copied_this_run} | source={message.id} -> premium={destination_message.id} "
                    f"| {size_mb:.2f} MB | {file_name}"
                )
            except Exception as exc:
                failed_this_run += 1
                state["failed"] += 1
                print(f"FAILED | source={message.id} | {type(exc).__name__}: {exc}")

            save_progress(state)
            await asyncio.sleep(COPY_DELAY_SECONDS)

        print("\n" + "=" * 72)
        print("MOVIE COPY FINISHED")
        print(f"Messages scanned this run : {scanned_this_run}")
        print(f"Movies copied this run    : {copied_this_run}")
        print(f"Existing movies skipped   : {existing_this_run}")
        print(f"Failures this run          : {failed_this_run}")
        print(f"Total copied in progress   : {state['copied']}")
        print(f"Progress file              : {PROGRESS_FILE}")
        print("=" * 72)
    finally:
        await client.disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped by you. Progress was saved; run the script again to resume.")
    except Exception as exc:
        print(f"\nERROR: {type(exc).__name__}: {exc}")
        print("Nothing was deleted. Fix the error and run the script again to resume.")
        input("Press Enter to close...")
