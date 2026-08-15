import asyncio
import logging
from typing import Optional

from fastapi import Header, Query

import copy_movies_app as current
import app as base
import import_app as importer
import metadata_hotfix as hotfix

app = current.app
log = logging.getLogger("strima-register-existing")

STATE = {
    "running": False,
    "completed": False,
    "phase": "idle",
    "target": 0,
    "destination_videos_indexed": 0,
    "source_messages_scanned": 0,
    "video_candidates": 0,
    "registered": 0,
    "already_registered": 0,
    "blocked_duplicates": 0,
    "unmatched_destination": 0,
    "metadata_applied": 0,
    "metadata_failed": 0,
    "failed": 0,
    "current_source_message_id": None,
    "last_destination_message_id": None,
    "last_error": None,
}

TASK = None


def _document_id(message):
    document = getattr(message, "document", None)
    if document is not None and getattr(document, "id", None) is not None:
        return int(document.id)
    media = getattr(message, "media", None)
    document = getattr(media, "document", None)
    if document is not None and getattr(document, "id", None) is not None:
        return int(document.id)
    return None


def _fingerprint(message):
    file_obj = getattr(message, "file", None)
    if not file_obj:
        return None
    name = str(getattr(file_obj, "name", "") or "").strip().lower()
    size = int(getattr(file_obj, "size", 0) or 0)
    if not name and not size:
        return None
    return (name, size)


async def _build_destination_maps():
    doc_to_message = {}
    fingerprint_to_message = {}
    count = 0

    async for message in base.client.iter_messages(base.CHANNEL_INPUT_ENTITY):
        file_obj = getattr(message, "file", None)
        if not base.is_video_file(file_obj, message):
            continue
        count += 1

        doc = _document_id(message)
        if doc is not None and doc not in doc_to_message:
            doc_to_message[doc] = int(message.id)

        fp = _fingerprint(message)
        if fp is not None and fp not in fingerprint_to_message:
            fingerprint_to_message[fp] = int(message.id)

        if count % 500 == 0:
            STATE["destination_videos_indexed"] = count
            log.info("Indexed %s destination movie videos", count)

    STATE["destination_videos_indexed"] = count
    return doc_to_message, fingerprint_to_message


async def _apply_metadata(source_message_id: int):
    try:
        result = await hotfix.enrich_source_movie_smart(
            source_message_id,
            apply=True,
            admin_key=base.STRIMA_ADMIN_KEY,
        )
        if isinstance(result, dict) and result.get("applied"):
            STATE["metadata_applied"] += 1
        else:
            # A valid movie may have only Telegram/local metadata and no TMDB patch.
            STATE["metadata_failed"] += 1
    except Exception as exc:
        STATE["metadata_failed"] += 1
        STATE["last_error"] = (
            f"metadata source {source_message_id}: {type(exc).__name__}: {str(exc)[:250]}"
        )
        log.exception("Metadata enrichment failed for source=%s", source_message_id)


async def _worker(target: int):
    STATE.update(
        {
            "running": True,
            "completed": False,
            "phase": "indexing_destination",
            "target": int(target),
            "destination_videos_indexed": 0,
            "source_messages_scanned": 0,
            "video_candidates": 0,
            "registered": 0,
            "already_registered": 0,
            "blocked_duplicates": 0,
            "unmatched_destination": 0,
            "metadata_applied": 0,
            "metadata_failed": 0,
            "failed": 0,
            "current_source_message_id": None,
            "last_destination_message_id": None,
            "last_error": None,
        }
    )

    try:
        if not base.client.is_connected():
            raise RuntimeError("Telegram client is disconnected")
        if base.SOURCE_INPUT_ENTITY is None:
            raise RuntimeError("Old movie source channel is not resolved")
        if base.CHANNEL_INPUT_ENTITY is None:
            raise RuntimeError("Premium movie destination channel is not resolved")

        doc_to_message, fingerprint_to_message = await _build_destination_maps()
        STATE["phase"] = "registering"

        # Newest-first is intentional: it keeps newly added STRIMA content fresh.
        # Already registered source messages are skipped, so repeated runs safely
        # continue with the next unregistered movies.
        async for source_message in base.client.iter_messages(base.SOURCE_INPUT_ENTITY):
            if STATE["registered"] >= target:
                break

            STATE["source_messages_scanned"] += 1
            STATE["current_source_message_id"] = int(source_message.id)

            file_obj = getattr(source_message, "file", None)
            if not base.is_video_file(file_obj, source_message):
                continue

            STATE["video_candidates"] += 1
            source_id = int(source_message.id)

            try:
                existing = await importer._source_lookup(source_id)
                if existing:
                    STATE["already_registered"] += 1
                    continue

                item = base.source_item_from_message(source_message)
                matches = await importer._duplicate_check(item)
                top = matches[0] if isinstance(matches, list) and matches else None
                if top and top.get("decision") == "duplicate":
                    STATE["blocked_duplicates"] += 1
                    continue

                destination_message_id = None
                doc = _document_id(source_message)
                if doc is not None:
                    destination_message_id = doc_to_message.get(doc)
                if destination_message_id is None:
                    fp = _fingerprint(source_message)
                    if fp is not None:
                        destination_message_id = fingerprint_to_message.get(fp)

                if destination_message_id is None:
                    STATE["unmatched_destination"] += 1
                    continue

                movie = await importer._register_movie(item, int(destination_message_id))
                if not movie:
                    raise RuntimeError("Supabase registration returned no movie row")

                STATE["registered"] += 1
                STATE["last_destination_message_id"] = int(destination_message_id)
                log.info(
                    "Registered existing Telegram movie source=%s destination=%s (%s/%s)",
                    source_id,
                    destination_message_id,
                    STATE["registered"],
                    target,
                )

                await _apply_metadata(source_id)
                await asyncio.sleep(0.5)

            except Exception as exc:
                STATE["failed"] += 1
                STATE["last_error"] = (
                    f"source {source_id}: {type(exc).__name__}: {str(exc)[:250]}"
                )
                log.exception("Existing-movie registration failed source=%s", source_id)

        STATE["phase"] = "complete"
        STATE["completed"] = True
        STATE["current_source_message_id"] = None

    except asyncio.CancelledError:
        STATE["phase"] = "stopped"
        raise
    except Exception as exc:
        STATE["phase"] = "error"
        STATE["last_error"] = f"{type(exc).__name__}: {str(exc)[:250]}"
        log.exception("Existing archive registrar stopped with an error")
    finally:
        STATE["running"] = False


@app.post("/admin/telegram/register-existing/start")
async def start_register_existing(
    limit: int = Query(default=50, ge=1, le=200),
    admin_key: Optional[str] = Header(default=None, alias="X-STRIMA-Admin-Key"),
):
    global TASK
    base.require_admin_key(admin_key)

    if TASK is not None and not TASK.done():
        return {"ok": True, "started": False, **STATE}

    TASK = asyncio.create_task(_worker(limit), name="strima-register-existing")
    return {"ok": True, "started": True, **STATE}


@app.get("/admin/telegram/register-existing/status")
async def register_existing_status(
    admin_key: Optional[str] = Header(default=None, alias="X-STRIMA-Admin-Key"),
):
    base.require_admin_key(admin_key)
    return {
        "ok": True,
        "task_running": bool(TASK is not None and not TASK.done()),
        **STATE,
    }


@app.post("/admin/telegram/register-existing/stop")
async def stop_register_existing(
    admin_key: Optional[str] = Header(default=None, alias="X-STRIMA-Admin-Key"),
):
    global TASK
    base.require_admin_key(admin_key)
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
