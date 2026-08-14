import asyncio
import logging
import os
from typing import Optional

from fastapi import Header
from telethon.errors import FloodWaitError

import app as base
import metadata_hotfix as hotfix

app = hotfix.app
log = logging.getLogger("strima-copy-movies")

MOVIE_MAX_SIZE_GB = float(os.getenv("MOVIE_MAX_SIZE_GB", "1.8"))
MOVIE_MAX_FILE_BYTES = int(MOVIE_MAX_SIZE_GB * 1024 * 1024 * 1024)
COPY_DELAY_SECONDS = max(0.5, float(os.getenv("MOVIE_COPY_DELAY_SECONDS", "1.5")))

STATE = {
    "running": False,
    "completed": False,
    "phase": "idle",
    "scanned_source": 0,
    "destination_videos_indexed": 0,
    "video_candidates": 0,
    "copied": 0,
    "already_in_destination": 0,
    "skipped_too_large": 0,
    "failed": 0,
    "current_source_message_id": None,
    "last_destination_message_id": None,
    "last_error": None,
}

TASK = None
STOP = None


def _document_id(message):
    document = getattr(message, "document", None)
    if document is not None and getattr(document, "id", None) is not None:
        return int(document.id)
    media = getattr(message, "media", None)
    document = getattr(media, "document", None)
    if document is not None and getattr(document, "id", None) is not None:
        return int(document.id)
    return None


def _fallback_fingerprint(message):
    file_obj = getattr(message, "file", None)
    if not file_obj:
        return None
    name = str(getattr(file_obj, "name", None) or "").strip().lower()
    size = int(getattr(file_obj, "size", None) or 0)
    if not name and not size:
        return None
    return (name, size)


async def _build_destination_index():
    document_ids = set()
    fingerprints = set()
    count = 0

    async for message in base.client.iter_messages(base.CHANNEL_INPUT_ENTITY):
        file_obj = getattr(message, "file", None)
        if not base.is_video_file(file_obj, message):
            continue
        count += 1
        doc_id = _document_id(message)
        if doc_id is not None:
            document_ids.add(doc_id)
        fp = _fallback_fingerprint(message)
        if fp is not None:
            fingerprints.add(fp)

    STATE["destination_videos_indexed"] = count
    return document_ids, fingerprints


async def _copy_one(message):
    try:
        destination_message = await base.client.send_file(
            base.CHANNEL_INPUT_ENTITY,
            file=message.media,
            caption=message.message or "",
            formatting_entities=(message.entities or None),
        )
    except FloodWaitError as exc:
        wait_seconds = int(getattr(exc, "seconds", 0) or 0) + 2
        STATE["last_error"] = f"Telegram FloodWait {wait_seconds}s at source {message.id}"
        log.warning("Telegram FloodWait=%ss; waiting before retry source=%s", wait_seconds, message.id)
        await asyncio.sleep(wait_seconds)
        destination_message = await base.client.send_file(
            base.CHANNEL_INPUT_ENTITY,
            file=message.media,
            caption=message.message or "",
            formatting_entities=(message.entities or None),
        )

    if isinstance(destination_message, (list, tuple)):
        destination_message = destination_message[0] if destination_message else None

    if not destination_message or not getattr(destination_message, "id", None):
        raise RuntimeError("Telegram did not return a destination message ID")

    return destination_message


async def _copy_all_movies_worker():
    global STOP
    STATE.update({
        "running": True,
        "completed": False,
        "phase": "indexing_destination",
        "scanned_source": 0,
        "destination_videos_indexed": 0,
        "video_candidates": 0,
        "copied": 0,
        "already_in_destination": 0,
        "skipped_too_large": 0,
        "failed": 0,
        "current_source_message_id": None,
        "last_destination_message_id": None,
        "last_error": None,
    })

    try:
        if not base.client.is_connected():
            raise RuntimeError("Telegram client is disconnected")
        if base.SOURCE_INPUT_ENTITY is None:
            raise RuntimeError("Old movie source channel is not resolved")
        if base.CHANNEL_INPUT_ENTITY is None:
            raise RuntimeError("Premium movie destination channel is not resolved")

        destination_doc_ids, destination_fingerprints = await _build_destination_index()
        STATE["phase"] = "copying"

        log.info(
            "Starting COPY-ONLY movie backfill source=%s destination=%s indexed_destination=%s max_size=%.2fGB",
            base.TG_SOURCE_CHANNEL_ID,
            base.TG_CHANNEL_ID,
            STATE["destination_videos_indexed"],
            MOVIE_MAX_SIZE_GB,
        )

        async for message in base.client.iter_messages(base.SOURCE_INPUT_ENTITY, reverse=True):
            if STOP is not None and STOP.is_set():
                STATE["phase"] = "stopped"
                return

            STATE["scanned_source"] += 1
            STATE["current_source_message_id"] = int(message.id)

            file_obj = getattr(message, "file", None)
            if not base.is_video_file(file_obj, message):
                continue

            STATE["video_candidates"] += 1
            size_bytes = int(getattr(file_obj, "size", None) or 0)
            if size_bytes >= MOVIE_MAX_FILE_BYTES:
                STATE["skipped_too_large"] += 1
                continue

            doc_id = _document_id(message)
            fingerprint = _fallback_fingerprint(message)
            if (doc_id is not None and doc_id in destination_doc_ids) or (
                fingerprint is not None and fingerprint in destination_fingerprints
            ):
                STATE["already_in_destination"] += 1
                continue

            try:
                destination_message = await _copy_one(message)
                STATE["copied"] += 1
                STATE["last_destination_message_id"] = int(destination_message.id)

                destination_doc_id = _document_id(destination_message)
                if destination_doc_id is not None:
                    destination_doc_ids.add(destination_doc_id)
                destination_fp = _fallback_fingerprint(destination_message)
                if destination_fp is not None:
                    destination_fingerprints.add(destination_fp)

                log.info(
                    "Copied movie source=%s -> destination=%s (%s copied so far)",
                    message.id,
                    destination_message.id,
                    STATE["copied"],
                )
            except Exception as exc:
                STATE["failed"] += 1
                STATE["last_error"] = f"source {message.id}: {type(exc).__name__}: {str(exc)[:250]}"
                log.exception("Failed copying source movie %s", message.id)

            await asyncio.sleep(COPY_DELAY_SECONDS)

        STATE["phase"] = "complete"
        STATE["completed"] = True
        STATE["current_source_message_id"] = None
        log.info(
            "COPY-ONLY movie backfill complete copied=%s existing=%s too_large=%s failed=%s",
            STATE["copied"],
            STATE["already_in_destination"],
            STATE["skipped_too_large"],
            STATE["failed"],
        )
    except asyncio.CancelledError:
        STATE["phase"] = "stopped"
        raise
    except Exception as exc:
        STATE["phase"] = "error"
        STATE["last_error"] = f"{type(exc).__name__}: {str(exc)[:250]}"
        log.exception("COPY-ONLY movie worker stopped with error")
    finally:
        STATE["running"] = False


@app.post("/admin/telegram/copy-all-movies/start")
async def start_copy_all_movies(
    admin_key: Optional[str] = Header(default=None, alias="X-STRIMA-Admin-Key"),
):
    global TASK, STOP
    base.require_admin_key(admin_key)
    if TASK is not None and not TASK.done():
        return {"ok": True, "started": False, **STATE}
    STOP = asyncio.Event()
    TASK = asyncio.create_task(_copy_all_movies_worker(), name="strima-copy-all-movies")
    return {"ok": True, "started": True, **STATE}


@app.post("/admin/telegram/copy-all-movies/stop")
async def stop_copy_all_movies(
    admin_key: Optional[str] = Header(default=None, alias="X-STRIMA-Admin-Key"),
):
    global TASK
    base.require_admin_key(admin_key)
    if STOP is not None:
        STOP.set()
    if TASK is not None and not TASK.done():
        TASK.cancel()
        try:
            await TASK
        except asyncio.CancelledError:
            pass
    TASK = None
    STATE["running"] = False
    STATE["phase"] = "stopped"
    return {"ok": True, **STATE}


@app.get("/admin/telegram/copy-all-movies/status")
async def copy_all_movies_status(
    admin_key: Optional[str] = Header(default=None, alias="X-STRIMA-Admin-Key"),
):
    base.require_admin_key(admin_key)
    return {
        "ok": True,
        "max_file_size_gb": MOVIE_MAX_SIZE_GB,
        "delay_seconds": COPY_DELAY_SECONDS,
        "task_running": bool(TASK is not None and not TASK.done()),
        **STATE,
    }
