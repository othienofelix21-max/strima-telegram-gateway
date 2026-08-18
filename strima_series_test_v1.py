#!/usr/bin/env python3
"""STRIMA controlled series test: Jumong S01E01 + S01E02.

Telegram PREMIUM SERIES -> Bunny 480p -> Backblaze HLS -> Supabase
series/seasons/episodes -> delete temporary Bunny copy.

There is deliberately NO STRIMA source-file size filter. The Telegram source
is streamed through a small RAM queue; the full original is never saved to EC2.
"""

import asyncio
import json
import queue
import re
import threading
import time
from pathlib import Path

import requests
from telethon import TelegramClient

import strima_full_pipeline_v4_complete as core

BASE = Path(__file__).resolve().parent
TG_CONFIG = BASE / "telegram_config.json"
BUNNY_CONFIG = BASE / "bunny_config.json"
B2_CONFIG = BASE / "backblaze_config.json"
SB_CONFIG = BASE / "supabase_config.json"
SESSION = BASE / "strima_session"
STATE_FILE = BASE / "strima_series_test_state.json"
ASSET_DB = BASE / "strima_series_assets.db"

SOURCE_TITLE = "PREMIUM SERIES"
SERIES_TITLE = "Jumong"
SERIES_SLUG = "jumong"
SEASON_NO = 1
TEST_EPISODES = {1, 2}
VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".m4v", ".mpeg", ".mpg"}
CHUNK = 512 * 1024
QUEUE = 8


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def load_state():
    if not STATE_FILE.exists():
        return {"jobs": {}}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"jobs": {}}


def save_state(state):
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)


def sb_headers(cfg):
    key = cfg["service_role_key"]
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def sb_upsert(cfg, table, payload, conflict):
    base = str(cfg["url"]).rstrip("/")
    headers = sb_headers(cfg)
    headers["Prefer"] = "resolution=merge-duplicates,return=representation"
    r = requests.post(
        f"{base}/rest/v1/{table}",
        headers=headers,
        params={"on_conflict": conflict},
        json={k: v for k, v in payload.items() if v is not None},
        timeout=40,
    )
    if r.status_code not in (200, 201):
        raise RuntimeError(f"Supabase {table} upsert HTTP {r.status_code}: {r.text[:1000]}")
    rows = r.json()
    if not isinstance(rows, list) or not rows:
        raise RuntimeError(f"Supabase {table} upsert returned no row")
    return rows[0]


def ensure_catalog(cfg):
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    series = sb_upsert(
        cfg,
        "series",
        {
            "title": SERIES_TITLE,
            "slug": SERIES_SLUG,
            "source_normalized_title": SERIES_SLUG,
            "is_free": False,
            "is_premium": True,
            "allow_download": False,
            "is_featured": False,
            "status": "published",
            "published_at": now,
        },
        "slug",
    )
    season = sb_upsert(
        cfg,
        "seasons",
        {
            "series_id": series["id"],
            "season_number": SEASON_NO,
            "title": "Season 1",
            "status": "published",
        },
        "series_id,season_number",
    )
    return series["id"], season["id"]


def is_video(message):
    if not message.file:
        return False
    mime = (message.file.mime_type or "").lower()
    ext = (message.file.ext or "").lower()
    return (
        message.video is not None
        or mime.startswith("video/")
        or mime in {"application/x-matroska", "video/x-matroska"}
        or ext in VIDEO_EXTS
    )


def filename(message):
    return getattr(message.file, "name", None) or f"telegram_episode_{message.id}{message.file.ext or '.mp4'}"


def episode_no(message):
    text = f"{filename(message)}\n{message.text or ''}"
    m = re.search(r"\bjumong[\s_-]*0*(\d{1,3})\b", text, re.I)
    return int(m.group(1)) if m else None


async def resolve_source(client):
    dialogs = await client.get_dialogs(limit=None)
    exact = [d for d in dialogs if (getattr(d, "title", "") or "").strip().casefold() == SOURCE_TITLE.casefold()]
    partial = [d for d in dialogs if SOURCE_TITLE.casefold() in (getattr(d, "title", "") or "").casefold()]
    matches = exact or partial
    if not matches:
        visible = [(getattr(d, "title", "") or "") for d in dialogs if "series" in (getattr(d, "title", "") or "").casefold()]
        raise RuntimeError(f"Could not find {SOURCE_TITLE!r}. Visible series-like channels: {visible[:20]}")
    d = matches[0]
    print(f"Telegram source: {d.title} ({d.id})")
    return d


async def find_jumong(client, source):
    found = {}
    async for m in client.iter_messages(source.input_entity, search=SERIES_TITLE):
        if not is_video(m):
            continue
        n = episode_no(m)
        if n in TEST_EPISODES and n not in found:
            found[n] = m
            print(f"FOUND E{n:02d}: TG {m.id} | {filename(m)} | {core.readable_size(m.file.size or 0)}")
        if TEST_EPISODES.issubset(found):
            break
    missing = sorted(TEST_EPISODES - set(found))
    if missing:
        raise RuntimeError(f"Missing Jumong test episode(s): {missing}")
    return [found[n] for n in sorted(TEST_EPISODES)]


class StreamError:
    def __init__(self, error):
        self.error = error


class TGStream:
    def __init__(self, q, total, label):
        self.q, self.total, self.label = q, total, label
        self.sent, self.started, self.last = 0, time.time(), 0

    def __len__(self):
        return self.total

    def __iter__(self):
        while True:
            item = self.q.get()
            if item is None:
                return
            if isinstance(item, StreamError):
                raise item.error
            self.sent += len(item)
            now = time.time()
            if now - self.last >= 5:
                elapsed = max(now - self.started, 0.001)
                speed = self.sent / elapsed
                pct = self.sent / self.total * 100 if self.total else 0
                eta = (self.total - self.sent) / speed if speed else 0
                print(
                    f"[{self.label}] upload {pct:6.2f}% | {core.readable_size(self.sent)}/{core.readable_size(self.total)} | "
                    f"{core.readable_size(speed)}/s | ETA {core.readable_time(eta)}",
                    flush=True,
                )
                self.last = now
            yield item


async def upload_unlimited(client, message, library_id, key, video_id, label):
    """No STRIMA byte-size limit and no overall HTTP read timeout."""
    total = int(message.file.size or 0)
    last_error = RuntimeError("Bunny upload failed")

    for attempt in range(1, 4):
        q = queue.Queue(maxsize=QUEUE)
        stop = threading.Event()

        def put(item):
            while not stop.is_set():
                try:
                    q.put(item, timeout=0.5)
                    return True
                except queue.Full:
                    pass
            return False

        async def produce():
            try:
                async for part in client.iter_download(
                    message.media,
                    request_size=CHUNK,
                    chunk_size=CHUNK,
                    file_size=total,
                ):
                    if stop.is_set() or not await asyncio.to_thread(put, bytes(part)):
                        break
                if not stop.is_set():
                    await asyncio.to_thread(put, None)
            except Exception as exc:
                if not stop.is_set():
                    await asyncio.to_thread(put, StreamError(exc))

        producer = asyncio.create_task(produce())
        stream = TGStream(q, total, label)
        url = f"https://video.bunnycdn.com/library/{library_id}/videos/{video_id}"

        def do_put():
            return requests.put(
                url,
                headers={"AccessKey": key, "Content-Type": "application/octet-stream", "Accept": "application/json"},
                params={"enabledResolutions": "480p"},
                data=stream,
                timeout=(30, None),
            )

        try:
            print(f"[{label}] Telegram -> Bunny attempt {attempt}/3")
            r = await asyncio.to_thread(do_put)
            if r.ok:
                return
            last_error = RuntimeError(f"Bunny HTTP {r.status_code}: {r.text[:800]}")
        except Exception as exc:
            last_error = exc
        finally:
            stop.set()
            try:
                await producer
            except Exception:
                pass

        if attempt < 3:
            await asyncio.sleep(10 * attempt)

    raise last_error


async def wait_ready(library_id, key, video_id, label):
    last = None
    while True:
        data = await core.get_bunny_video(library_id, key, video_id)
        status = int(data.get("status", -1))
        progress = data.get("encodeProgress", 0)
        available = str(data.get("availableResolutions") or "").lower()
        line = f"[{label}] Bunny encoding status={status}, progress={progress}%, resolutions={available or 'none'}"
        if line != last:
            print(line, flush=True)
            last = line
        if status == 5:
            raise RuntimeError("Bunny reported encoding failure")
        if "480p" in available and status in (3, 4):
            return data
        if status == 4 and "240p" in available:
            print(f"[{label}] Using 240p fallback")
            return data
        await asyncio.sleep(30)


def upsert_episode(cfg, series_id, season_id, n, source_id, message, playback, bunny_data):
    minutes, duration_text = core.duration_fields_from_bunny(bunny_data or {})
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return sb_upsert(
        cfg,
        "episodes",
        {
            "series_id": series_id,
            "season_id": season_id,
            "episode_number": n,
            "title": f"Episode {n}",
            "slug": f"jumong-s01-e{n:02d}",
            "video_provider": "backblaze",
            "playback_url": playback,
            "duration_minutes": minutes,
            "duration_text": duration_text,
            "is_free": False,
            "is_premium": True,
            "allow_download": False,
            "status": "published",
            "published_at": now,
            "telegram_channel_id": int(source_id),
            "telegram_message_id": int(message.id),
            "source_telegram_channel_id": int(source_id),
            "source_telegram_message_id": int(message.id),
            "source_filename": filename(message),
            "source_normalized_title": f"jumong season 1 episode {n}",
        },
        "season_id,episode_number",
    )


async def process(client, state, source_id, message, n, library_id, bunny_key, bunny_host, b2, sb, series_id, season_id):
    jobs = state.setdefault("jobs", {})
    job = jobs.setdefault(str(message.id), {})
    label = f"JUMONG-S01E{n:02d}:TG{message.id}"
    prefix = job.get("prefix") or f"series/jumong/season-01/episode-{n:02d}-tg{message.id}"
    job["prefix"] = prefix
    save_state(state)

    print("\n" + "=" * 76)
    print(f"[{label}] START | {filename(message)} | {core.readable_size(message.file.size or 0)}")
    print(f"[{label}] STRIMA FILE-SIZE LIMIT: NONE")
    print("=" * 76)

    if job.get("status") == "complete" and job.get("episode_id"):
        print(f"[{label}] Already complete; skipping duplicate work.")
        return True

    video_id = job.get("video_id")
    bunny_data = {}

    if job.get("status") not in {"uploaded", "encoding", "backblaze", "finalizing"}:
        if video_id:
            try:
                await asyncio.to_thread(core.delete_bunny_stream_video, library_id, bunny_key, video_id, label)
            except Exception:
                pass
        video_id = await asyncio.to_thread(core.create_bunny_video, library_id, bunny_key, f"Jumong S01E{n:02d}")
        job.update({"video_id": video_id, "status": "uploading"})
        save_state(state)
        await upload_unlimited(client, message, library_id, bunny_key, video_id, label)
        job["status"] = "uploaded"
        save_state(state)

    if job.get("status") in {"uploaded", "encoding"}:
        job["status"] = "encoding"
        save_state(state)
        bunny_data = await wait_ready(library_id, bunny_key, video_id, label)

    if job.get("status") in {"uploaded", "encoding"}:
        core.CHECKPOINT_DB = ASSET_DB
        core.SOURCE_CHANNEL_ID = int(source_id)
        result = await asyncio.to_thread(
            core.copy_hls_to_backblaze,
            video_id,
            prefix,
            bunny_host,
            b2,
            label,
            message.id,
        )
        job.update({"status": "backblaze", "hls": result["master_key"]})
        save_state(state)

    hls = job.get("hls")
    if not hls:
        raise RuntimeError("Backblaze HLS path is missing from checkpoint")
    playback = core.build_pullzone_playback_url(hls)

    episode = await asyncio.to_thread(
        upsert_episode,
        sb,
        series_id,
        season_id,
        n,
        source_id,
        message,
        playback,
        bunny_data,
    )
    job.update({"status": "finalizing", "episode_id": episode["id"]})
    save_state(state)

    if video_id:
        try:
            await asyncio.to_thread(core.delete_bunny_stream_video, library_id, bunny_key, video_id, label)
        except Exception as exc:
            print(f"[{label}] Bunny cleanup pending: {type(exc).__name__}: {exc}")

    job["status"] = "complete"
    save_state(state)
    print(f"[{label}] COMPLETE | Episode ID {episode['id']} | {playback}")
    return True


async def main():
    print("=" * 76)
    print("STRIMA SERIES TEST V1 - JUMONG S01E01 + S01E02")
    print("NO STRIMA SOURCE FILE-SIZE LIMIT")
    print("=" * 76)

    tg, bunny, b2, sb = load(TG_CONFIG), load(BUNNY_CONFIG), load(B2_CONFIG), load(SB_CONFIG)
    client = TelegramClient(str(SESSION), int(tg["api_id"]), tg["api_hash"])
    state = load_state()

    try:
        await client.connect()
        if not await client.is_user_authorized():
            raise RuntimeError("Existing strima_session is not authorized")

        me = await client.get_me()
        print(f"Telegram authorized: {getattr(me, 'first_name', '')} (@{getattr(me, 'username', None)})")
        source = await resolve_source(client)
        messages = await find_jumong(client, source)
        series_id, season_id = await asyncio.to_thread(ensure_catalog, sb)

        library_id = int(bunny["library_id"])
        bunny_key = bunny["api_key"]
        bunny_host = bunny.get("cdn_hostname") or core.DEFAULT_BUNNY_HOST
        successes = 0

        for message in messages:
            n = episode_no(message)
            try:
                if await process(
                    client, state, int(source.id), message, n,
                    library_id, bunny_key, bunny_host, b2, sb, series_id, season_id,
                ):
                    successes += 1
            except Exception as exc:
                state.setdefault("jobs", {}).setdefault(str(message.id), {})["last_error"] = f"{type(exc).__name__}: {exc}"
                save_state(state)
                print(f"[JUMONG-S01E{n:02d}] FAILED SAFELY: {type(exc).__name__}: {exc}")

        print("\n" + "#" * 76)
        print(f"JUMONG TEST RESULT: {successes}/2 EPISODES COMPLETE")
        print("Re-running this same worker resumes from its saved checkpoint.")
        print("#" * 76)
    finally:
        if client.is_connected():
            await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
