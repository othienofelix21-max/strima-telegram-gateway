import asyncio
import logging
import re
from typing import Optional

from fastapi import Header
from telethon.errors import FloodWaitError

import register_existing_app as current
import app as base
import import_app as importer

app = current.app
log = logging.getLogger("strima-series-test")

SERIES_SOURCE_TITLE = "PREMIUM SERIES"
SERIES_TITLE = "Jumong"
SERIES_SLUG = "jumong"
SEASON_NUMBER = 1
TARGET_EPISODES = {1, 2}
PLAYBACK_BASE = "https://mc-p2ku5nz4qw.bunny.run/movie"

STATE = {
    "running": False,
    "completed": False,
    "phase": "idle",
    "source_channel_id": None,
    "found": 0,
    "copied": 0,
    "registered": 0,
    "already_registered": 0,
    "failed": 0,
    "current_episode": None,
    "last_error": None,
}
TASK = None


def _episode_number(message):
    file_obj = getattr(message, "file", None)
    name = str(getattr(file_obj, "name", "") or "")
    caption = str(getattr(message, "message", "") or "")
    text = f"{name}\n{caption}"
    m = re.search(r"\bjumong[\s._-]*0*(\d{1,3})\b", text, flags=re.I)
    return int(m.group(1)) if m else None


async def _resolve_series_source():
    dialogs = await base.client.get_dialogs(limit=None)
    wanted = SERIES_SOURCE_TITLE.casefold()
    exact = [d for d in dialogs if (getattr(d, "title", "") or "").strip().casefold() == wanted]
    partial = [d for d in dialogs if wanted in (getattr(d, "title", "") or "").casefold()]
    matches = exact or partial
    if not matches:
        raise RuntimeError(f"Telegram channel {SERIES_SOURCE_TITLE!r} was not found")
    return matches[0]


async def _find_test_episodes(source):
    found = {}
    async for message in base.client.iter_messages(source.input_entity, search=SERIES_TITLE):
        file_obj = getattr(message, "file", None)
        if not base.is_video_file(file_obj, message):
            continue
        ep = _episode_number(message)
        if ep in TARGET_EPISODES and ep not in found:
            found[ep] = message
        if TARGET_EPISODES.issubset(found.keys()):
            break
    missing = sorted(TARGET_EPISODES - set(found.keys()))
    if missing:
        raise RuntimeError(f"Missing Jumong episode(s): {missing}")
    STATE["found"] = len(found)
    return [found[n] for n in sorted(TARGET_EPISODES)]


async def _episode_lookup(source_channel_id: int, source_message_id: int):
    rows = await importer._rpc(
        "strima_gateway_episode_source_lookup",
        {
            "p_admin_key": base.STRIMA_ADMIN_KEY,
            "p_source_channel_id": source_channel_id,
            "p_source_message_id": source_message_id,
        },
    )
    return rows[0] if isinstance(rows, list) and rows else None


async def _register_episode(source_channel_id: int, source_message, destination_message, episode_number: int):
    file_obj = source_message.file
    size_bytes = int(getattr(file_obj, "size", 0) or 0)
    duration = getattr(file_obj, "duration", None)
    filename = str(getattr(file_obj, "name", "") or "")
    playback_url = f"{PLAYBACK_BASE}/{int(destination_message.id)}"

    rows = await importer._rpc(
        "strima_gateway_register_episode_v1",
        {
            "p_admin_key": base.STRIMA_ADMIN_KEY,
            "p_series_title": SERIES_TITLE,
            "p_series_slug": SERIES_SLUG,
            "p_season_number": SEASON_NUMBER,
            "p_episode_number": episode_number,
            "p_episode_title": f"Episode {episode_number}",
            "p_duration_seconds": int(duration) if duration else None,
            "p_source_channel_id": source_channel_id,
            "p_source_message_id": int(source_message.id),
            "p_destination_channel_id": int(base.TG_CHANNEL_ID),
            "p_destination_message_id": int(destination_message.id),
            "p_playback_url": playback_url,
            "p_file_size_mb": round(size_bytes / (1024 * 1024), 2) if size_bytes else None,
            "p_source_filename": filename or None,
            "p_source_normalized_title": f"jumong season 1 episode {episode_number}",
        },
    )
    return rows[0] if isinstance(rows, list) and rows else None


async def _copy_message(message):
    try:
        result = await base.client.send_file(
            base.CHANNEL_INPUT_ENTITY,
            file=message.media,
            caption=message.message or "",
            formatting_entities=(message.entities or None),
        )
    except FloodWaitError as exc:
        await asyncio.sleep(int(getattr(exc, "seconds", 0) or 0) + 2)
        result = await base.client.send_file(
            base.CHANNEL_INPUT_ENTITY,
            file=message.media,
            caption=message.message or "",
            formatting_entities=(message.entities or None),
        )
    if isinstance(result, (list, tuple)):
        result = result[0] if result else None
    if not result or not getattr(result, "id", None):
        raise RuntimeError("Telegram did not return destination message ID")
    return result


async def _worker():
    STATE.update({
        "running": True,
        "completed": False,
        "phase": "resolving_source",
        "source_channel_id": None,
        "found": 0,
        "copied": 0,
        "registered": 0,
        "already_registered": 0,
        "failed": 0,
        "current_episode": None,
        "last_error": None,
    })
    try:
        if not base.client.is_connected():
            raise RuntimeError("Telegram client is disconnected")
        if base.CHANNEL_INPUT_ENTITY is None:
            raise RuntimeError("STRIMA destination Telegram channel is not resolved")

        source = await _resolve_series_source()
        source_channel_id = int(source.id)
        STATE["source_channel_id"] = source_channel_id
        STATE["phase"] = "finding_episodes"
        messages = await _find_test_episodes(source)

        for message in messages:
            ep = _episode_number(message)
            STATE["current_episode"] = ep
            STATE["phase"] = f"episode_{ep}_preflight"

            existing = await _episode_lookup(source_channel_id, int(message.id))
            if existing:
                STATE["already_registered"] += 1
                continue

            STATE["phase"] = f"episode_{ep}_copying"
            destination = await _copy_message(message)
            STATE["copied"] += 1

            STATE["phase"] = f"episode_{ep}_registering"
            registered = await _register_episode(source_channel_id, message, destination, ep)
            if not registered:
                raise RuntimeError(f"Supabase returned no row for Jumong episode {ep}")
            STATE["registered"] += 1
            await asyncio.sleep(1)

        STATE["phase"] = "complete"
        STATE["completed"] = True
        STATE["current_episode"] = None
    except asyncio.CancelledError:
        STATE["phase"] = "stopped"
        raise
    except Exception as exc:
        STATE["failed"] += 1
        STATE["phase"] = "error"
        STATE["last_error"] = f"{type(exc).__name__}: {str(exc)[:500]}"
        log.exception("Jumong series test failed")
    finally:
        STATE["running"] = False


@app.post("/admin/telegram/series/jumong-test/start")
async def start_jumong_test(
    admin_key: Optional[str] = Header(default=None, alias="X-STRIMA-Admin-Key"),
):
    global TASK
    base.require_admin_key(admin_key)
    if TASK is not None and not TASK.done():
        return {"ok": True, "started": False, "size_limit": "unlimited", **STATE}
    TASK = asyncio.create_task(_worker(), name="strima-jumong-series-test")
    return {"ok": True, "started": True, "size_limit": "unlimited", **STATE}


@app.get("/admin/telegram/series/jumong-test/status")
async def jumong_test_status(
    admin_key: Optional[str] = Header(default=None, alias="X-STRIMA-Admin-Key"),
):
    base.require_admin_key(admin_key)
    return {
        "ok": True,
        "size_limit": "unlimited",
        "task_running": bool(TASK is not None and not TASK.done()),
        **STATE,
    }
