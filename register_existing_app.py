import asyncio
import logging
from typing import Optional

from fastapi import Header, HTTPException, Query

import copy_movies_app as current
import app as base
import import_app as importer
import metadata_hotfix as hotfix
import metadata_app as metadata

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

STRICT_SCAN = {
    "ready": [],
    "requested": 0,
    "scanned": 0,
    "already_registered": 0,
    "duplicates_blocked": 0,
    "unmatched_destination": 0,
    "missing_filename": 0,
    "metadata_match_failed": 0,
    "missing_artwork": 0,
    "wrong_content_blocked": 0,
    "ready_for_upload": False,
}

TASK = None
STRICT_UPLOAD_TASK = None


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


def _candidate_title(candidate: dict) -> str:
    return str(candidate.get("title") or candidate.get("name") or "").strip()


def _tmdb_artwork(candidate: dict):
    poster_path = candidate.get("poster_path")
    backdrop_path = candidate.get("backdrop_path")
    poster_url = f"https://image.tmdb.org/t/p/w500{poster_path}" if poster_path else None
    banner_url = f"https://image.tmdb.org/t/p/original{backdrop_path}" if backdrop_path else None
    return poster_url, banner_url


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
    STATE["destination_videos_indexed"] = count
    return doc_to_message, fingerprint_to_message


async def _apply_metadata(source_message_id: int):
    try:
        result = await hotfix.enrich_source_movie_smart(source_message_id, apply=True, admin_key=base.STRIMA_ADMIN_KEY)
        if isinstance(result, dict) and result.get("applied"):
            STATE["metadata_applied"] += 1
        else:
            STATE["metadata_failed"] += 1
    except Exception as exc:
        STATE["metadata_failed"] += 1
        STATE["last_error"] = f"metadata source {source_message_id}: {type(exc).__name__}: {str(exc)[:250]}"
        log.exception("Metadata enrichment failed for source=%s", source_message_id)


async def _tmdb_preflight(item: dict):
    title = importer._clean_public_title(item)
    year = item.get("detected_year")
    try:
        score, candidate, kind = await metadata._tmdb_find_best(title, year)
        if score < 70 and year:
            retry_score, retry_candidate, retry_kind = await metadata._tmdb_find_best(title, None)
            if retry_score > score:
                score, candidate, kind = retry_score, retry_candidate, retry_kind
        if not candidate or score < 70:
            return None
        poster, banner = _tmdb_artwork(candidate)
        return {
            "score": score,
            "candidate": candidate,
            "kind": kind,
            "poster": poster,
            "banner": banner,
            "artwork_ok": bool(poster and banner),
        }
    except Exception:
        log.exception("TMDB preflight failed for %s", title)
        return None


async def _scan_strict_movies(limit: int):
    if not base.client.is_connected():
        raise RuntimeError("Telegram client is disconnected")
    if base.SOURCE_INPUT_ENTITY is None:
        raise RuntimeError("Old movie source channel is not resolved")
    if base.CHANNEL_INPUT_ENTITY is None:
        raise RuntimeError("Premium movie destination channel is not resolved")

    STRICT_SCAN.update({
        "ready": [], "requested": int(limit), "scanned": 0, "already_registered": 0,
        "duplicates_blocked": 0, "unmatched_destination": 0, "missing_filename": 0,
        "metadata_match_failed": 0, "missing_artwork": 0, "wrong_content_blocked": 0,
        "ready_for_upload": False,
    })

    doc_to_message, fingerprint_to_message = await _build_destination_maps()
    async for source_message in base.client.iter_messages(base.SOURCE_INPUT_ENTITY):
        if len(STRICT_SCAN["ready"]) >= limit:
            break
        STRICT_SCAN["scanned"] += 1
        file_obj = getattr(source_message, "file", None)
        if not base.is_video_file(file_obj, source_message):
            continue

        item = base.source_item_from_message(source_message)
        if (item.get("content_kind") or "movie") != "movie" or item.get("episode_number"):
            STRICT_SCAN["wrong_content_blocked"] += 1
            continue

        source_id = int(source_message.id)
        if await importer._source_lookup(source_id):
            STRICT_SCAN["already_registered"] += 1
            continue

        matches = await importer._duplicate_check(item)
        top = matches[0] if isinstance(matches, list) and matches else None
        if top and top.get("decision") == "duplicate":
            STRICT_SCAN["duplicates_blocked"] += 1
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
            STRICT_SCAN["unmatched_destination"] += 1
            continue

        filename = str(getattr(file_obj, "name", "") or "").strip()
        if not filename:
            STRICT_SCAN["missing_filename"] += 1
            continue

        tmdb = await _tmdb_preflight(item)
        if not tmdb:
            STRICT_SCAN["metadata_match_failed"] += 1
            continue
        if not tmdb.get("artwork_ok"):
            STRICT_SCAN["missing_artwork"] += 1
            continue

        destination_message_id = int(destination_message_id)
        title = importer._clean_public_title(item)
        candidate = tmdb.get("candidate") or {}
        STRICT_SCAN["ready"].append({
            "number": len(STRICT_SCAN["ready"]) + 1,
            "title": title,
            "source_message_id": source_id,
            "destination_message_id": destination_message_id,
            "download_file_name": filename,
            "tmdb_id": candidate.get("id"),
            "tmdb_title": _candidate_title(candidate),
            "tmdb_score": tmdb.get("score"),
            "poster_url": tmdb.get("poster"),
            "banner_url": tmdb.get("banner"),
            "playback_url": f"https://mc-p2ku5nz4qw.bunny.run/movie/{destination_message_id}",
            "download_url": f"https://mc-p2ku5nz4qw.bunny.run/download/{destination_message_id}",
        })

    STRICT_SCAN["ready_for_upload"] = len(STRICT_SCAN["ready"]) == limit
    return STRICT_SCAN


async def _worker(target: int):
    STATE.update({
        "running": True, "completed": False, "phase": "indexing_destination", "target": int(target),
        "destination_videos_indexed": 0, "source_messages_scanned": 0, "video_candidates": 0,
        "registered": 0, "already_registered": 0, "blocked_duplicates": 0,
        "unmatched_destination": 0, "metadata_applied": 0, "metadata_failed": 0,
        "failed": 0, "current_source_message_id": None, "last_destination_message_id": None,
        "last_error": None,
    })
    try:
        doc_to_message, fingerprint_to_message = await _build_destination_maps()
        STATE["phase"] = "registering"
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
                await _apply_metadata(source_id)
                await asyncio.sleep(0.5)
            except Exception as exc:
                STATE["failed"] += 1
                STATE["last_error"] = f"source {source_id}: {type(exc).__name__}: {str(exc)[:250]}"
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


async def _strict_upload_worker():
    STATE.update({
        "running": True, "completed": False, "phase": "strict_upload",
        "target": len(STRICT_SCAN["ready"]), "registered": 0, "already_registered": 0,
        "blocked_duplicates": 0, "metadata_applied": 0, "metadata_failed": 0,
        "failed": 0, "last_error": None,
    })
    try:
        for row in list(STRICT_SCAN["ready"]):
            source_id = int(row["source_message_id"])
            destination_message_id = int(row["destination_message_id"])
            try:
                existing = await importer._source_lookup(source_id)
                if existing:
                    STATE["already_registered"] += 1
                    continue
                source_message = await base.client.get_messages(base.SOURCE_INPUT_ENTITY, ids=source_id)
                if not source_message or not getattr(source_message, "file", None):
                    raise RuntimeError("Source message disappeared after preflight")
                item = base.source_item_from_message(source_message)
                matches = await importer._duplicate_check(item)
                top = matches[0] if isinstance(matches, list) and matches else None
                if top and top.get("decision") == "duplicate":
                    STATE["blocked_duplicates"] += 1
                    continue
                movie = await importer._register_movie(item, destination_message_id)
                if not movie:
                    raise RuntimeError("Supabase registration returned no movie row")
                STATE["registered"] += 1
                STATE["last_destination_message_id"] = destination_message_id
                await _apply_metadata(source_id)
                await asyncio.sleep(0.5)
            except Exception as exc:
                STATE["failed"] += 1
                STATE["last_error"] = f"source {source_id}: {type(exc).__name__}: {str(exc)[:250]}"
                log.exception("Strict movie upload failed source=%s", source_id)
        STATE["phase"] = "complete"
        STATE["completed"] = True
    finally:
        STATE["running"] = False


@app.post("/admin/telegram/movies/strict/scan")
async def strict_movie_scan(limit: int = Query(default=100, ge=1, le=200), admin_key: Optional[str] = Header(default=None, alias="X-STRIMA-Admin-Key")):
    base.require_admin_key(admin_key)
    if TASK is not None and not TASK.done():
        raise HTTPException(status_code=409, detail="Another movie registration task is running")
    result = await _scan_strict_movies(limit)
    return {"ok": True, **result}


@app.get("/admin/telegram/movies/strict/status")
async def strict_movie_status(admin_key: Optional[str] = Header(default=None, alias="X-STRIMA-Admin-Key")):
    base.require_admin_key(admin_key)
    return {"ok": True, "upload_running": bool(STRICT_UPLOAD_TASK is not None and not STRICT_UPLOAD_TASK.done()), **STRICT_SCAN, "upload_state": STATE}


@app.post("/admin/telegram/movies/strict/upload/start")
async def strict_movie_upload_start(admin_key: Optional[str] = Header(default=None, alias="X-STRIMA-Admin-Key")):
    global STRICT_UPLOAD_TASK
    base.require_admin_key(admin_key)
    if not STRICT_SCAN.get("ready_for_upload") or not STRICT_SCAN.get("ready"):
        raise HTTPException(status_code=400, detail="Run a successful strict scan first")
    if STRICT_UPLOAD_TASK is not None and not STRICT_UPLOAD_TASK.done():
        return {"ok": True, "started": False, "reason": "Strict movie upload is already running", **STATE}
    STRICT_UPLOAD_TASK = asyncio.create_task(_strict_upload_worker(), name="strima-strict-movie-upload")
    return {"ok": True, "started": True, "target": len(STRICT_SCAN["ready"]), "ready_for_upload": True}


@app.post("/admin/telegram/register-existing/start")
async def start_register_existing(limit: int = Query(default=50, ge=1, le=200), admin_key: Optional[str] = Header(default=None, alias="X-STRIMA-Admin-Key")):
    global TASK
    base.require_admin_key(admin_key)
    if TASK is not None and not TASK.done():
        return {"ok": True, "started": False, **STATE}
    TASK = asyncio.create_task(_worker(limit), name="strima-register-existing")
    return {"ok": True, "started": True, **STATE}


@app.get("/admin/telegram/register-existing/status")
async def register_existing_status(admin_key: Optional[str] = Header(default=None, alias="X-STRIMA-Admin-Key")):
    base.require_admin_key(admin_key)
    return {"ok": True, "task_running": bool(TASK is not None and not TASK.done()), **STATE}


@app.post("/admin/telegram/register-existing/stop")
async def stop_register_existing(admin_key: Optional[str] = Header(default=None, alias="X-STRIMA-Admin-Key")):
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
