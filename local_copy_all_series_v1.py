import asyncio
import getpass
import json
import os
from pathlib import Path

from telethon import TelegramClient
from telethon.errors import FloodWaitError, SessionPasswordNeededError
from telethon.sessions import StringSession

COPY_DELAY_SECONDS = 1.5

BASE_DIR = Path(__file__).resolve().parent
SESSION_FILE = BASE_DIR / "local_copy_session_string.txt"
CREDS_FILE = BASE_DIR / "local_telegram_credentials.json"
CONFIG_FILE = BASE_DIR / "series_copy_config.json"
PROGRESS_FILE = BASE_DIR / "series_copy_progress.json"

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


def default_progress(source_id=None, destination_id=None):
    return {
        "source_channel_id": source_id,
        "destination_channel_id": destination_id,
        "last_source_message_id": 0,
        "copied": 0,
        "existing": 0,
        "failed": 0,
    }


def load_progress(source_id, destination_id):
    if not PROGRESS_FILE.exists():
        return default_progress(source_id, destination_id)
    try:
        data = json.loads(PROGRESS_FILE.read_text(encoding="utf-8"))
        saved_source = int(data.get("source_channel_id") or 0)
        saved_destination = int(data.get("destination_channel_id") or 0)
        if saved_source != int(source_id) or saved_destination != int(destination_id):
            print("Series channel selection changed. Starting a fresh series progress file.")
            return default_progress(source_id, destination_id)
        return {
            "source_channel_id": int(source_id),
            "destination_channel_id": int(destination_id),
            "last_source_message_id": int(data.get("last_source_message_id", 0) or 0),
            "copied": int(data.get("copied", 0) or 0),
            "existing": int(data.get("existing", 0) or 0),
            "failed": int(data.get("failed", 0) or 0),
        }
    except Exception:
        return default_progress(source_id, destination_id)


def save_progress(state):
    tmp = PROGRESS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(PROGRESS_FILE)


def load_api_credentials():
    api_id = os.getenv("TG_API_ID") or os.getenv("API_ID")
    api_hash = os.getenv("TG_API_HASH") or os.getenv("API_HASH")

    if (not api_id or not api_hash) and CREDS_FILE.exists():
        try:
            saved = json.loads(CREDS_FILE.read_text(encoding="utf-8"))
            api_id = api_id or saved.get("api_id")
            api_hash = api_hash or saved.get("api_hash")
        except Exception:
            pass

    if not api_id or not api_hash:
        print("\nEnter the SAME Telegram API ID and API HASH used by the STRIMA gateway.")
        api_id = input("Telegram API ID: ").strip()
        api_hash = input("Telegram API HASH: ").strip()
        if not api_id or not api_hash:
            raise RuntimeError("Telegram API credentials are required")
        CREDS_FILE.write_text(
            json.dumps({"api_id": int(api_id), "api_hash": api_hash}, indent=2),
            encoding="utf-8",
        )

    return int(api_id), str(api_hash).strip()


async def get_client(api_id, api_hash):
    session_string = ""
    if SESSION_FILE.exists():
        session_string = SESSION_FILE.read_text(encoding="utf-8").strip()

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

    if not await client.is_user_authorized():
        print("\nCreating/re-authorizing the dedicated STRIMA local Telegram session.")
        phone = input("Telegram phone number (with country code): ").strip()
        if not phone:
            raise RuntimeError("Phone number is required")
        await client.send_code_request(phone)
        code = input("Telegram login code: ").strip()
        try:
            await client.sign_in(phone=phone, code=code)
        except SessionPasswordNeededError:
            password = getpass.getpass("Telegram 2-step verification password: ")
            await client.sign_in(password=password)

        SESSION_FILE.write_text(client.session.save(), encoding="utf-8")
        print(f"Dedicated local session saved to: {SESSION_FILE}")
        print("Do NOT share that file or its contents.")

    return client


async def get_dialog_choices(client):
    dialogs = await client.get_dialogs()
    choices = []
    for dialog in dialogs:
        title = str(getattr(dialog, "title", "") or "").strip()
        if not title:
            continue
        entity = getattr(dialog, "entity", None)
        if entity is None:
            continue
        if not (getattr(dialog, "is_channel", False) or getattr(dialog, "is_group", False)):
            continue
        choices.append((int(dialog.id), title, dialog.input_entity))
    return choices


def load_config():
    if not CONFIG_FILE.exists():
        return None
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        return {
            "source_channel_id": int(data["source_channel_id"]),
            "destination_channel_id": int(data["destination_channel_id"]),
            "source_title": str(data.get("source_title") or ""),
            "destination_title": str(data.get("destination_title") or ""),
        }
    except Exception:
        return None


def save_config(source_id, destination_id, source_title, destination_title):
    CONFIG_FILE.write_text(
        json.dumps(
            {
                "source_channel_id": int(source_id),
                "destination_channel_id": int(destination_id),
                "source_title": source_title,
                "destination_title": destination_title,
            },
            indent=2,
        ),
        encoding="utf-8",
    )


async def choose_series_channels(client):
    choices = await get_dialog_choices(client)
    if not choices:
        raise RuntimeError("No Telegram channels/groups are visible to this account")

    print("\nVISIBLE TELEGRAM CHANNELS / GROUPS")
    print("-" * 72)
    for idx, (dialog_id, title, _) in enumerate(choices, start=1):
        print(f"{idx:>3}. {title} | id={dialog_id}")
    print("-" * 72)
    print("Choose carefully: SOURCE must be the OLD series channel; DESTINATION must be your NEW STRIMA series channel.")

    while True:
        raw = input("Number for OLD SERIES SOURCE: ").strip()
        try:
            source_choice = choices[int(raw) - 1]
            break
        except Exception:
            print("Invalid number. Try again.")

    while True:
        raw = input("Number for NEW STRIMA SERIES DESTINATION: ").strip()
        try:
            destination_choice = choices[int(raw) - 1]
            break
        except Exception:
            print("Invalid number. Try again.")

    source_id, source_title, source_entity = source_choice
    destination_id, destination_title, destination_entity = destination_choice

    if source_id == destination_id:
        raise RuntimeError("Source and destination cannot be the same Telegram channel")

    print("\nYou selected:")
    print(f"SOURCE      : {source_title} ({source_id})")
    print(f"DESTINATION : {destination_title} ({destination_id})")
    confirm = input("Type YES to save these channels and start copying: ").strip().upper()
    if confirm != "YES":
        raise RuntimeError("Cancelled before copying. Nothing was changed.")

    save_config(source_id, destination_id, source_title, destination_title)
    return source_entity, destination_entity, source_id, destination_id, source_title, destination_title


async def resolve_saved_channels(client, config):
    dialogs = await client.get_dialogs()
    source = None
    destination = None
    source_title = config.get("source_title") or ""
    destination_title = config.get("destination_title") or ""

    for dialog in dialogs:
        if int(dialog.id) == int(config["source_channel_id"]):
            source = dialog.input_entity
            source_title = dialog.title
        if int(dialog.id) == int(config["destination_channel_id"]):
            destination = dialog.input_entity
            destination_title = dialog.title

    if source is None or destination is None:
        raise RuntimeError(
            "A saved series channel is no longer visible. Delete series_copy_config.json and run again to choose channels."
        )

    return (
        source,
        destination,
        int(config["source_channel_id"]),
        int(config["destination_channel_id"]),
        source_title,
        destination_title,
    )


async def build_destination_index(client, destination, destination_title):
    print(f"\nScanning {destination_title} first so existing episodes are NOT copied again...")
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
            print(f"  Indexed {count} existing destination episode videos...")
    print(f"Destination index ready: {count} existing episode videos.\n")
    return doc_ids, fingerprints


async def copy_message(client, source, destination, message):
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
        print(f"Telegram rate limit: waiting {wait_seconds}s automatically...")
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
    print("STRIMA - LOCAL COPY ALL SERIES V1")
    print("OLD Telegram series channel -> NEW STRIMA series channel")
    print("NO episode size limit. Existing destination videos are skipped.")
    print("Uses the same dedicated local Telegram session as the movie copier.")
    print("Progress is saved so the job can resume after shutdown/restart.")
    print("=" * 72)

    api_id, api_hash = load_api_credentials()
    client = await get_client(api_id, api_hash)

    try:
        me = await client.get_me()
        print(f"\nTelegram connected as: @{getattr(me, 'username', None) or ''} ({me.id})")

        config = load_config()
        if config:
            source, destination, source_id, destination_id, source_title, destination_title = await resolve_saved_channels(client, config)
            print("\nUsing saved series channels:")
            print(f"SOURCE      : {source_title} ({source_id})")
            print(f"DESTINATION : {destination_title} ({destination_id})")
        else:
            source, destination, source_id, destination_id, source_title, destination_title = await choose_series_channels(client)

        destination_doc_ids, destination_fingerprints = await build_destination_index(
            client, destination, destination_title
        )

        state = load_progress(source_id, destination_id)
        last_id = int(state["last_source_message_id"] or 0)
        if last_id > 0:
            print(f"RESUMING after source message {last_id}.")
        else:
            print("Starting from the oldest source series messages.")

        scanned_this_run = 0
        copied_this_run = 0
        existing_this_run = 0
        failed_this_run = 0

        async for message in client.iter_messages(source, reverse=True, min_id=last_id):
            scanned_this_run += 1
            state["last_source_message_id"] = int(message.id)

            if not is_video_message(message):
                save_progress(state)
                continue

            file_obj = getattr(message, "file", None)
            file_name = str(getattr(file_obj, "name", "") or f"message-{message.id}")
            size_mb = int(getattr(file_obj, "size", 0) or 0) / 1024 / 1024

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
                    f"COPIED #{copied_this_run} | source={message.id} -> series={destination_message.id} "
                    f"| {size_mb:.2f} MB | {file_name}"
                )
            except Exception as exc:
                failed_this_run += 1
                state["failed"] += 1
                print(f"FAILED | source={message.id} | {type(exc).__name__}: {exc}")

            save_progress(state)
            await asyncio.sleep(COPY_DELAY_SECONDS)

        print("\n" + "=" * 72)
        print("SERIES COPY FINISHED")
        print(f"Messages scanned this run : {scanned_this_run}")
        print(f"Episodes copied this run  : {copied_this_run}")
        print(f"Existing videos skipped   : {existing_this_run}")
        print(f"Failures this run          : {failed_this_run}")
        print(f"Total copied in progress  : {state['copied']}")
        print(f"Progress file             : {PROGRESS_FILE}")
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
