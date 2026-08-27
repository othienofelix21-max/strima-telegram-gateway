import asyncio
import csv
import getpass
import json
import os
import re
from collections import defaultdict
from pathlib import Path

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
from telethon.sessions import StringSession

BASE_DIR = Path(__file__).resolve().parent
SESSION_FILE = BASE_DIR / "local_copy_session_string.txt"
CREDS_FILE = BASE_DIR / "local_telegram_credentials.json"
SERIES_CONFIG_FILE = BASE_DIR / "series_copy_config.json"
OUTPUT_DIR = BASE_DIR / "telegram_inventory"

# Current STRIMA Premium Movies destination used by local_copy_all_movies_v2.py
MOVIE_CHANNEL_ID = -1003386868177

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".mpeg", ".mpg", ".ts"}

EPISODE_PATTERNS = [
    re.compile(r"(?i)\bS(?:EASON)?\s*0*(\d{1,2})\s*[._\- ]*E(?:P(?:ISODE)?)?\s*0*(\d{1,3})\b"),
    re.compile(r"(?i)\bS\s*0*(\d{1,2})\s*[._\- ]+0*(\d{1,3})\b"),
    re.compile(r"(?i)\bSEASON\s*0*(\d{1,2})\s*[._\- ]*EPISODE\s*0*(\d{1,3})\b"),
]

NOISE_PATTERNS = [
    re.compile(r"(?i)\b(2160p|1440p|1080p|720p|480p|360p|4k|uhd|hdr|x264|x265|hevc)\b"),
    re.compile(r"(?i)\bVJ\s*(JR|JUNIOR|SOUL|EMMY|MUBA|MUKANO|SHIELD|SAMMY|ICE|JINGO)?\b"),
    re.compile(r"(?i)\b(19|20)\d{2}\b"),
    re.compile(r"(?i)\b(END|NO'?S|HD|MOVIES?)\b"),
]


def is_video_message(message):
    file_obj = getattr(message, "file", None)
    if not file_obj:
        return False
    mime = str(getattr(file_obj, "mime_type", "") or "").lower()
    name = str(getattr(file_obj, "name", "") or "").lower()
    suffix = Path(name).suffix.lower() if name else ""
    return mime.startswith("video/") or suffix in VIDEO_EXTENSIONS


def display_name(message):
    file_obj = getattr(message, "file", None)
    name = str(getattr(file_obj, "name", "") or "").strip()
    if name:
        return name
    caption = str(getattr(message, "message", "") or "").strip().splitlines()
    return caption[0].strip() if caption else f"message-{message.id}"


def strip_extension(name):
    suffix = Path(name).suffix
    return name[:-len(suffix)] if suffix else name


def clean_spacing(text):
    text = re.sub(r"[_]+", " ", text)
    text = re.sub(r"[.]+", " ", text)
    text = re.sub(r"\s*-\s*", " - ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip(" -_.")


def clean_movie_title(filename):
    text = clean_spacing(strip_extension(filename))
    for pattern in NOISE_PATTERNS:
        text = pattern.sub(" ", text)
    text = re.sub(r"(?i)\bBY\s+VJ\b.*$", "", text)
    text = re.sub(r"\[[^\]]+\]", " ", text)
    text = re.sub(r"\([^)]*\)", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" -_.")
    return text or clean_spacing(strip_extension(filename))


def parse_series_episode(filename):
    raw = clean_spacing(strip_extension(filename))
    season = None
    episode = None
    match = None
    for pattern in EPISODE_PATTERNS:
        match = pattern.search(raw)
        if match:
            season = int(match.group(1))
            episode = int(match.group(2))
            break

    title_text = raw
    if match:
        title_text = (raw[:match.start()] + " " + raw[match.end():]).strip()

    # Handle loose "EP 12" / "E12" only if no season marker was found.
    if episode is None:
        loose = re.search(r"(?i)\b(?:EP(?:ISODE)?|E)\s*0*(\d{1,3})\b", title_text)
        if loose:
            episode = int(loose.group(1))
            season = 1
            title_text = (title_text[:loose.start()] + " " + title_text[loose.end():]).strip()

    for pattern in NOISE_PATTERNS:
        title_text = pattern.sub(" ", title_text)
    title_text = re.sub(r"(?i)\bBY\s+VJ\b.*$", "", title_text)
    title_text = re.sub(r"\[[^\]]+\]", " ", title_text)
    title_text = re.sub(r"\([^)]*\)", " ", title_text)
    title_text = re.sub(r"\s+", " ", title_text).strip(" -_.")

    return title_text or raw, season, episode


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
        api_id = input("Telegram API ID: ").strip()
        api_hash = input("Telegram API HASH: ").strip()
        if not api_id or not api_hash:
            raise RuntimeError("Telegram API credentials are required")
    return int(api_id), str(api_hash).strip()


async def get_client(api_id, api_hash):
    session_string = SESSION_FILE.read_text(encoding="utf-8").strip() if SESSION_FILE.exists() else ""
    client = TelegramClient(StringSession(session_string), api_id, api_hash, connection_retries=10, retry_delay=2, auto_reconnect=True)
    await client.connect()
    if not await client.is_user_authorized():
        phone = input("Telegram phone number (with country code): ").strip()
        await client.send_code_request(phone)
        code = input("Telegram login code: ").strip()
        try:
            await client.sign_in(phone=phone, code=code)
        except SessionPasswordNeededError:
            await client.sign_in(password=getpass.getpass("Telegram 2-step verification password: "))
        SESSION_FILE.write_text(client.session.save(), encoding="utf-8")
    return client


def load_series_destination_id():
    if not SERIES_CONFIG_FILE.exists():
        raise RuntimeError("series_copy_config.json was not found. Run the existing series copier once so the saved PREMIUM SERIES destination is available.")
    data = json.loads(SERIES_CONFIG_FILE.read_text(encoding="utf-8"))
    return int(data["destination_channel_id"])


async def resolve_channel(client, channel_id):
    dialogs = await client.get_dialogs()
    for dialog in dialogs:
        if int(dialog.id) == int(channel_id):
            return dialog.input_entity, dialog.title
    raise RuntimeError(f"Telegram channel {channel_id} is not visible to this account")


async def scan_channel(client, entity, label):
    rows = []
    count = 0
    print(f"\nScanning {label} ...")
    async for message in client.iter_messages(entity, reverse=True):
        if not is_video_message(message):
            continue
        count += 1
        file_obj = getattr(message, "file", None)
        size_mb = round(int(getattr(file_obj, "size", 0) or 0) / 1024 / 1024, 2)
        rows.append({
            "message_id": int(message.id),
            "filename": display_name(message),
            "size_mb": size_mb,
            "date": message.date.isoformat() if getattr(message, "date", None) else "",
        })
        if count % 250 == 0:
            print(f"  {count} videos indexed...")
    print(f"Finished {label}: {count} videos.")
    return rows


def write_csv(path, fieldnames, rows):
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def export_movies(rows):
    items = []
    for row in rows:
        item = dict(row)
        item["title"] = clean_movie_title(row["filename"])
        items.append(item)
    items.sort(key=lambda x: (x["title"].lower(), x["message_id"]))
    numbered = []
    for index, item in enumerate(items, 1):
        numbered.append({"number": index, **item})

    write_csv(OUTPUT_DIR / "telegram_movies_inventory.csv", ["number", "title", "filename", "message_id", "size_mb", "date"], numbered)
    with (OUTPUT_DIR / "telegram_movies_checklist.txt").open("w", encoding="utf-8") as fh:
        fh.write(f"STRIMA TELEGRAM MOVIE CHECKLIST\nTOTAL VIDEO FILES: {len(numbered)}\n\n")
        for item in numbered:
            fh.write(f"[ ] {item['number']:04d}. {item['title']}\n")
    return numbered


def export_series(rows):
    episode_rows = []
    grouped = defaultdict(lambda: defaultdict(list))

    for row in rows:
        series_title, season, episode = parse_series_episode(row["filename"])
        season = season or 0
        item = {
            **row,
            "series_title": series_title,
            "season": season if season else "",
            "episode": episode if episode is not None else "",
        }
        episode_rows.append(item)
        grouped[series_title][season].append(item)

    episode_rows.sort(key=lambda x: (x["series_title"].lower(), int(x["season"] or 0), int(x["episode"] or 0), x["message_id"]))
    write_csv(OUTPUT_DIR / "telegram_series_episodes.csv", ["series_title", "season", "episode", "filename", "message_id", "size_mb", "date"], episode_rows)

    summary_rows = []
    for series_title in sorted(grouped, key=str.lower):
        seasons = grouped[series_title]
        season_numbers = sorted(seasons)
        total_files = sum(len(v) for v in seasons.values())
        season_labels = []
        for season in season_numbers:
            eps = sorted({int(x["episode"]) for x in seasons[season] if str(x["episode"]).isdigit()})
            if season == 0:
                season_labels.append(f"Unparsed:{len(seasons[season])}")
            elif eps:
                season_labels.append(f"S{season}:" + ",".join(str(e) for e in eps))
            else:
                season_labels.append(f"S{season}:{len(seasons[season])} files")
        summary_rows.append({
            "series_title": series_title,
            "season_count": len([s for s in season_numbers if s > 0]),
            "episode_files": total_files,
            "seasons_and_episodes": " | ".join(season_labels),
        })

    numbered = []
    for index, item in enumerate(summary_rows, 1):
        numbered.append({"number": index, **item})
    write_csv(OUTPUT_DIR / "telegram_series_inventory.csv", ["number", "series_title", "season_count", "episode_files", "seasons_and_episodes"], numbered)

    with (OUTPUT_DIR / "telegram_series_checklist.txt").open("w", encoding="utf-8") as fh:
        fh.write(f"STRIMA TELEGRAM SERIES CHECKLIST\nSERIES GROUPS FOUND: {len(numbered)}\nEPISODE VIDEO FILES: {len(episode_rows)}\n\n")
        for item in numbered:
            fh.write(f"[ ] {item['number']:04d}. {item['series_title']} | {item['seasons_and_episodes']}\n")
    return numbered, episode_rows


async def main():
    print("=" * 72)
    print("STRIMA - READ-ONLY TELEGRAM LIBRARY INVENTORY")
    print("This script ONLY reads Telegram channel history and creates checklist files.")
    print("It does NOT copy, delete, edit, upload, or modify any Telegram message.")
    print("=" * 72)

    OUTPUT_DIR.mkdir(exist_ok=True)
    api_id, api_hash = load_api_credentials()
    client = await get_client(api_id, api_hash)
    try:
        series_channel_id = load_series_destination_id()
        movie_entity, movie_title = await resolve_channel(client, MOVIE_CHANNEL_ID)
        series_entity, series_title = await resolve_channel(client, series_channel_id)

        print(f"MOVIES CHANNEL : {movie_title} ({MOVIE_CHANNEL_ID})")
        print(f"SERIES CHANNEL : {series_title} ({series_channel_id})")

        movie_rows = await scan_channel(client, movie_entity, movie_title)
        series_rows = await scan_channel(client, series_entity, series_title)

        movies = export_movies(movie_rows)
        series, episodes = export_series(series_rows)

        summary = (
            "STRIMA TELEGRAM LIBRARY INVENTORY\n"
            f"Movie video files: {len(movies)}\n"
            f"Series groups found: {len(series)}\n"
            f"Series episode video files: {len(episodes)}\n"
            f"Output folder: {OUTPUT_DIR}\n"
        )
        (OUTPUT_DIR / "telegram_library_summary.txt").write_text(summary, encoding="utf-8")

        print("\n" + "=" * 72)
        print(summary.strip())
        print("Files created:")
        print("  telegram_movies_checklist.txt")
        print("  telegram_movies_inventory.csv")
        print("  telegram_series_checklist.txt")
        print("  telegram_series_inventory.csv")
        print("  telegram_series_episodes.csv")
        print("  telegram_library_summary.txt")
        print("=" * 72)
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
