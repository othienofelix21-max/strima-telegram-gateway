import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import Header
from fastapi.responses import JSONResponse
from telethon import events
from telethon.errors import FloodWaitError

import app as base
import import_app as importer
import metadata_hotfix as hotfix

app = hotfix.app
log = logging.getLogger("strima-archive-sync")

AUTO_MOVIE_ARCHIVE_SYNC = os.getenv("AUTO_MOVIE_ARCHIVE_SYNC", "false").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
MOVIE_MAX_SIZE_GB = float(os.getenv("MOVIE_MAX_SIZE_GB", "1.8"))
MOVIE_MAX_FILE_BYTES = int(MOVIE_MAX_SIZE_GB * 1024 * 1024 * 1024)
MOVIE_SYNC_DELAY_SECONDS = max(0.5, float(os.getenv("MOVIE_SYNC_DELAY_SECONDS", "2")))

STATE = {
    "enabled": False,
    "mode": "idle",
    "backfill_complete": False,
    "scanned_messages": 0,
    "video_candidates": 0,
    "registered": 0,
    "already_registered": 0,
    "blocked_duplicates": 0,
    "skipped_too_large": 0,
    "failed": 0,
    "metadata_applied": 0,
    "metadata_failed": 0,
    "live_events": 0,
    "current_source_message_id": None,
    "last_error": None,
}

STOP = None
TASK = None
_SYNC_LOCK = asyncio.Lock()
_ORIGINAL_LIFESPAN = app.router.lifespan_context


def _reset_backfill_counters() -> None:
    STATE.update(
        {
            "mode": "backfill",
            "backfill_complete": False,
            "scanned_messages": 0,
            "video_candidates": 0,
            "registered": 0,
            "already_registered": 0,
            "blocked_duplicates": 0,
            "skipped_too_large": 0,
            "failed": 0,
            "metadata_applied": 0,
            "metadata_failed": 0,
            "current_source_message_id": None,
            "last_error": None,
        }
    )


async def _apply_metadata_if_new(source_message_id: int) -> None:
    try:
        result = await hotfix.m.enrich_source_movie(
            source_message_id,
            apply=True,
            admin_key=base.STRIMA_ADMIN_KEY,
        )
        provider = (result or {}).get("provider") or {}
        if (result or {}).get("applied"):
            STATE["metadata_applied"] += 1
            log.info(
                "Metadata applied source=%s matched=%s confidence=%s candidate=%s",
                source_message_id,
                provider.get("matched"),
                provider.get("confidence"),
                provider.get("candidate_title"),
            )
    except Exception as exc:
        STATE["metadata_failed"] += 1
        STATE["last_error"] = f"metadata {source_message_id}: {type(exc).__name__}: {str(exc)[:250]}"
        log.exception("Metadata enrichment failed for source message %s", source_message_id)


async def _process_video_message(message, *, live: bool = False) -> None:
    async with _SYNC_LOCK:
        source_message_id = int(message.id)
        STATE["current_source_message_id"] = source_message_id

        file_obj = getattr(message, "file", None)
        if not base.is_video_file(file_obj, message):
            return

        STATE["video_candidates"] += 1
        size_bytes = int(getattr(file_obj, "size", None) or 0)
        if size_bytes >= MOVIE_MAX_FILE_BYTES:
            STATE["skipped_too_large"] += 1
            log.info(
                "Skipping source=%s because size %.2f GB is at/above %.2f GB limit",
                source_message_id,
                size_bytes / (1024 ** 3),
                MOVIE_MAX_SIZE_GB,
            )
            return

        try:
            result = await importer.import_one_movie(
                source_message_id,
                base.STRIMA_ADMIN_KEY,
            )
        except FloodWaitError as exc:
            wait_seconds = int(getattr(exc, "seconds", 0) or 0) + 2
            STATE["last_error"] = f"Telegram FloodWait {wait_seconds}s at source {source_message_id}"
            log.warning("Telegram requested FloodWait=%ss; sleeping and retrying source=%s", wait_seconds, source_message_id)
            await asyncio.sleep(wait_seconds)
            result = await importer.import_one_movie(
                source_message_id,
                base.STRIMA_ADMIN_KEY,
            )
        except Exception as exc:
            STATE["failed"] += 1
            STATE["last_error"] = f"source {source_message_id}: {type(exc).__name__}: {str(exc)[:250]}"
            log.exception("Archive sync import failed for source=%s", source_message_id)
            return

        if isinstance(result, JSONResponse):
            STATE["failed"] += 1
            STATE["last_error"] = f"source {source_message_id}: importer returned HTTP {result.status_code}"
            log.error("Archive sync importer failed source=%s HTTP=%s", source_message_id, result.status_code)
            return

        stage = str((result or {}).get("stage") or "unknown")
        if stage in {"registered", "registered_after_retry"}:
            STATE["registered"] += 1
            log.info(
                "Archive sync registered source=%s destination=%s live=%s",
                source_message_id,
                (result or {}).get("destination_message_id"),
                live,
            )
            await _apply_metadata_if_new(source_message_id)
        elif stage == "already_registered":
            STATE["already_registered"] += 1
        elif stage == "blocked_duplicate":
            STATE["blocked_duplicates"] += 1
        else:
            log.info("Archive sync source=%s finished stage=%s", source_message_id, stage)


async def _backfill_worker() -> None:
    _reset_backfill_counters()
    STATE["enabled"] = True

    try:
        if not base.client.is_connected():
            raise RuntimeError("Telegram client is disconnected")
        if base.SOURCE_INPUT_ENTITY is None or base.CHANNEL_INPUT_ENTITY is None:
            raise RuntimeError("Telegram source or destination is not resolved")

        log.info(
            "Starting movie archive backfill source=%s destination=%s max_size=%.2fGB",
            base.TG_SOURCE_CHANNEL_ID,
            base.TG_CHANNEL_ID,
            MOVIE_MAX_SIZE_GB,
        )

        async for message in base.client.iter_messages(base.SOURCE_INPUT_ENTITY, reverse=True):
            if STOP is not None and STOP.is_set():
                return

            STATE["scanned_messages"] += 1
            STATE["current_source_message_id"] = int(message.id)

            file_obj = getattr(message, "file", None)
            if not base.is_video_file(file_obj, message):
                continue

            await _process_video_message(message, live=False)
            await asyncio.sleep(MOVIE_SYNC_DELAY_SECONDS)

        STATE["backfill_complete"] = True
        STATE["mode"] = "live"
        STATE["current_source_message_id"] = None
        STATE["last_error"] = None
        log.info(
            "Movie archive backfill complete. registered=%s existing=%s duplicates=%s too_large=%s failed=%s. Live sync is now active.",
            STATE["registered"],
            STATE["already_registered"],
            STATE["blocked_duplicates"],
            STATE["skipped_too_large"],
            STATE["failed"],
        )

        while STOP is not None and not STOP.is_set():
            await asyncio.sleep(60)

    except asyncio.CancelledError:
        raise
    except Exception as exc:
        STATE["mode"] = "error"
        STATE["last_error"] = f"{type(exc).__name__}: {str(exc)[:250]}"
        log.exception("Movie archive sync worker stopped with an error")


@base.client.on(events.NewMessage)
async def _new_source_message(event):
    if not STATE.get("enabled"):
        return
    if int(event.chat_id or 0) != int(base.TG_SOURCE_CHANNEL_ID):
        return

    message = event.message
    if not message:
        return

    STATE["live_events"] += 1
    try:
        await _process_video_message(message, live=True)
    except Exception as exc:
        STATE["failed"] += 1
        STATE["last_error"] = f"live source {getattr(message, 'id', None)}: {type(exc).__name__}: {str(exc)[:250]}"
        log.exception("Live movie sync failed")


async def _start_worker_if_needed() -> bool:
    global TASK
    if TASK is not None and not TASK.done():
        return False
    TASK = asyncio.create_task(_backfill_worker(), name="strima-movie-archive-sync")
    return True


@app.post("/admin/telegram/archive-sync/start")
async def start_archive_sync(
    admin_key: Optional[str] = Header(default=None, alias="X-STRIMA-Admin-Key"),
):
    base.require_admin_key(admin_key)
    started = await _start_worker_if_needed()
    return {"ok": True, "started": started, **STATE}


@app.post("/admin/telegram/archive-sync/stop")
async def stop_archive_sync(
    admin_key: Optional[str] = Header(default=None, alias="X-STRIMA-Admin-Key"),
):
    global TASK
    base.require_admin_key(admin_key)
    STATE["enabled"] = False
    STATE["mode"] = "stopping"
    if TASK is not None and not TASK.done():
        TASK.cancel()
        try:
            await TASK
        except asyncio.CancelledError:
            pass
    TASK = None
    STATE["mode"] = "idle"
    return {"ok": True, **STATE}


@app.get("/admin/telegram/archive-sync/status")
async def archive_sync_status(
    admin_key: Optional[str] = Header(default=None, alias="X-STRIMA-Admin-Key"),
):
    base.require_admin_key(admin_key)
    return {
        "ok": True,
        "auto_start_configured": AUTO_MOVIE_ARCHIVE_SYNC,
        "max_file_size_gb": MOVIE_MAX_SIZE_GB,
        "delay_seconds": MOVIE_SYNC_DELAY_SECONDS,
        "task_running": bool(TASK is not None and not TASK.done()),
        **STATE,
    }


@asynccontextmanager
async def archive_sync_lifespan(app_obj):
    global STOP, TASK
    async with _ORIGINAL_LIFESPAN(app_obj):
        STOP = asyncio.Event()
        if AUTO_MOVIE_ARCHIVE_SYNC:
            await _start_worker_if_needed()
        try:
            yield
        finally:
            STATE["enabled"] = False
            if STOP is not None:
                STOP.set()
            if TASK is not None and not TASK.done():
                TASK.cancel()
                try:
                    await TASK
                except asyncio.CancelledError:
                    pass
            TASK = None
            STATE["mode"] = "stopped"


app.router.lifespan_context = archive_sync_lifespan
