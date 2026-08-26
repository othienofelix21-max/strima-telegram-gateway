import asyncio
import re
from typing import Optional

from fastapi import Header, HTTPException, Query

import series_guarded_app as guarded
import register_existing_app as reg
import import_app as importer
import app as base

app = guarded.app

SCAN_TASK = None
SCAN_ERROR = None
SCAN_NAME = "movies_strict"
_DESTINATION_MAP_CACHE = None
_DESTINATION_MAP_LOCK = asyncio.Lock()


# Replace the older synchronous strict scan/status/upload routes with
# background-safe, batch-unique, checkpoint-aware versions.
for route in list(app.router.routes):
    path = getattr(route, "path", None)
    methods = set(getattr(route, "methods", set()) or set())
    if path == "/admin/telegram/movies/strict/scan" and "POST" in methods:
        app.router.routes.remove(route)
    elif path == "/admin/telegram/movies/strict/status" and "GET" in methods:
        app.router.routes.remove(route)
    elif path == "/admin/telegram/movies/strict/upload/start" and "POST" in methods:
        app.router.routes.remove(route)


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


async def _destination_maps_cached():
    global _DESTINATION_MAP_CACHE
    if _DESTINATION_MAP_CACHE is not None:
        return _DESTINATION_MAP_CACHE
    async with _DESTINATION_MAP_LOCK:
        if _DESTINATION_MAP_CACHE is None:
            _DESTINATION_MAP_CACHE = await reg._build_destination_maps()
    return _DESTINATION_MAP_CACHE


async def _registered_id_sets():
    rows = await importer._rpc(
        "strima_gateway_movie_scan_registered_ids",
        {"p_admin_key": base.STRIMA_ADMIN_KEY},
    )
    source_ids = set()
    destination_ids = set()
    for row in rows if isinstance(rows, list) else []:
        source_id = row.get("source_message_id") if isinstance(row, dict) else None
        destination_id = row.get("destination_message_id") if isinstance(row, dict) else None
        if source_id is not None:
            source_ids.add(int(source_id))
        if destination_id is not None:
            destination_ids.add(int(destination_id))
    return source_ids, destination_ids


async def _checkpoint_get():
    rows = await importer._rpc(
        "strima_gateway_movie_scan_checkpoint_get",
        {
            "p_admin_key": base.STRIMA_ADMIN_KEY,
            "p_scan_name": SCAN_NAME,
        },
    )
    if isinstance(rows, list) and rows:
        row = rows[0] or {}
        return {
            "archive_before_id": int(row["archive_before_id"]) if row.get("archive_before_id") is not None else None,
            "source_high_water_id": int(row["source_high_water_id"]) if row.get("source_high_water_id") is not None else None,
            "archive_exhausted": bool(row.get("archive_exhausted", False)),
        }
    return {
        "archive_before_id": None,
        "source_high_water_id": None,
        "archive_exhausted": False,
    }


async def _checkpoint_set(archive_before_id, source_high_water_id, archive_exhausted: bool):
    return await importer._rpc(
        "strima_gateway_movie_scan_checkpoint_set",
        {
            "p_admin_key": base.STRIMA_ADMIN_KEY,
            "p_scan_name": SCAN_NAME,
            "p_archive_before_id": int(archive_before_id) if archive_before_id is not None else None,
            "p_source_high_water_id": int(source_high_water_id) if source_high_water_id is not None else None,
            "p_archive_exhausted": bool(archive_exhausted),
        },
    )


async def _scan_unique_movies(limit: int, reset_checkpoint: bool = False):
    if not base.client.is_connected():
        raise RuntimeError("Telegram client is disconnected")
    if base.SOURCE_INPUT_ENTITY is None:
        raise RuntimeError("Old movie source channel is not resolved")
    if base.CHANNEL_INPUT_ENTITY is None:
        raise RuntimeError("Premium movie destination channel is not resolved")

    if reset_checkpoint:
        await _checkpoint_set(None, None, False)

    checkpoint = await _checkpoint_get()
    archive_before_start = checkpoint.get("archive_before_id")
    high_water_start = checkpoint.get("source_high_water_id")
    archive_exhausted_start = bool(checkpoint.get("archive_exhausted", False))

    reg.STRICT_SCAN.update({
        "ready": [],
        "requested": int(limit),
        "scanned": 0,
        "already_registered": 0,
        "destination_already_registered": 0,
        "duplicates_blocked": 0,
        "unmatched_destination": 0,
        "missing_filename": 0,
        "metadata_match_failed": 0,
        "missing_artwork": 0,
        "wrong_content_blocked": 0,
        "ready_for_upload": False,
        "scan_pass": "starting",
        "checkpoint_active": bool(archive_before_start or high_water_start or archive_exhausted_start),
        "checkpoint_archive_start": archive_before_start,
        "checkpoint_high_water_start": high_water_start,
        "checkpoint_archive_exhausted_start": archive_exhausted_start,
        "checkpoint_archive_next": archive_before_start,
        "checkpoint_high_water_next": high_water_start,
        "checkpoint_archive_exhausted_next": archive_exhausted_start,
        "checkpoint_commit_pending": False,
        "checkpoint_committed": False,
        "checkpoint_commit_error": None,
        "registered_source_ids_cached": 0,
        "registered_destination_ids_cached": 0,
        "destination_map_cached": _DESTINATION_MAP_CACHE is not None,
    })

    doc_to_message, fingerprint_to_message = await _destination_maps_cached()
    registered_source_ids, registered_destination_ids = await _registered_id_sets()
    reg.STRICT_SCAN["registered_source_ids_cached"] = len(registered_source_ids)
    reg.STRICT_SCAN["registered_destination_ids_cached"] = len(registered_destination_ids)
    reg.STRICT_SCAN["destination_map_cached"] = True

    seen_tmdb = set()
    seen_destinations = set()
    seen_titles = set()

    async def consider(source_message):
        if len(reg.STRICT_SCAN["ready"]) >= limit:
            return

        reg.STRICT_SCAN["scanned"] += 1
        file_obj = getattr(source_message, "file", None)
        if not base.is_video_file(file_obj, source_message):
            return

        item = base.source_item_from_message(source_message)
        if (item.get("content_kind") or "movie") != "movie" or item.get("episode_number"):
            reg.STRICT_SCAN["wrong_content_blocked"] += 1
            return

        source_id = int(source_message.id)
        if source_id in registered_source_ids:
            reg.STRICT_SCAN["already_registered"] += 1
            return

        destination_message_id = None
        doc = reg._document_id(source_message)
        if doc is not None:
            destination_message_id = doc_to_message.get(doc)
        if destination_message_id is None:
            fp = reg._fingerprint(source_message)
            if fp is not None:
                destination_message_id = fingerprint_to_message.get(fp)
        if destination_message_id is None:
            reg.STRICT_SCAN["unmatched_destination"] += 1
            return

        destination_message_id = int(destination_message_id)
        if destination_message_id in registered_destination_ids:
            reg.STRICT_SCAN["destination_already_registered"] += 1
            return

        filename = str(getattr(file_obj, "name", "") or "").strip()
        if not filename:
            reg.STRICT_SCAN["missing_filename"] += 1
            return

        # Only make the more expensive database duplicate call after the cheap
        # local/source/destination checks have passed.
        matches = await importer._duplicate_check(item)
        top = matches[0] if isinstance(matches, list) and matches else None
        if top and top.get("decision") == "duplicate":
            reg.STRICT_SCAN["duplicates_blocked"] += 1
            return

        tmdb = await reg._tmdb_preflight(item)
        if not tmdb:
            reg.STRICT_SCAN["metadata_match_failed"] += 1
            return
        if not tmdb.get("artwork_ok"):
            reg.STRICT_SCAN["missing_artwork"] += 1
            return

        candidate = tmdb.get("candidate") or {}
        tmdb_id = candidate.get("id")
        tmdb_kind = str(tmdb.get("kind") or "movie")
        tmdb_title = reg._candidate_title(candidate)
        public_title = importer._clean_public_title(item)

        tmdb_key = (tmdb_kind, str(tmdb_id)) if tmdb_id is not None else None
        title_key = _norm(tmdb_title or public_title)
        if (
            destination_message_id in seen_destinations
            or (tmdb_key is not None and tmdb_key in seen_tmdb)
            or (tmdb_key is None and title_key and title_key in seen_titles)
        ):
            reg.STRICT_SCAN["duplicates_blocked"] += 1
            return

        seen_destinations.add(destination_message_id)
        if tmdb_key is not None:
            seen_tmdb.add(tmdb_key)
        if title_key:
            seen_titles.add(title_key)

        reg.STRICT_SCAN["ready"].append({
            "number": len(reg.STRICT_SCAN["ready"]) + 1,
            "title": public_title,
            "source_message_id": source_id,
            "destination_message_id": destination_message_id,
            "download_file_name": filename,
            "tmdb_id": tmdb_id,
            "tmdb_title": tmdb_title,
            "tmdb_score": tmdb.get("score"),
            "poster_url": tmdb.get("poster"),
            "banner_url": tmdb.get("banner"),
            "playback_url": f"https://mc-p2ku5nz4qw.bunny.run/movie/{destination_message_id}",
            "download_url": f"https://mc-p2ku5nz4qw.bunny.run/download/{destination_message_id}",
        })

    high_water_next = high_water_start

    # First, inspect only messages that arrived after the previous high-water mark.
    # This keeps newly-added movies from being missed while the archive cursor moves
    # steadily backwards through older content.
    if high_water_start is not None:
        reg.STRICT_SCAN["scan_pass"] = "new_arrivals"
        async for source_message in base.client.iter_messages(
            base.SOURCE_INPUT_ENTITY,
            min_id=int(high_water_start),
        ):
            source_id = int(source_message.id)
            if high_water_next is None or source_id > high_water_next:
                high_water_next = source_id
            await consider(source_message)
            if len(reg.STRICT_SCAN["ready"]) >= limit:
                break

    archive_next = archive_before_start
    archive_exhausted_next = archive_exhausted_start

    # Then continue from the exact archive position committed by the previous
    # successful upload instead of re-reading thousands of old Telegram messages.
    if len(reg.STRICT_SCAN["ready"]) < limit and not archive_exhausted_start:
        reg.STRICT_SCAN["scan_pass"] = "archive"
        kwargs = {}
        if archive_before_start is not None:
            kwargs["offset_id"] = int(archive_before_start)

        archive_exhausted_next = True
        async for source_message in base.client.iter_messages(base.SOURCE_INPUT_ENTITY, **kwargs):
            source_id = int(source_message.id)
            archive_next = source_id
            if high_water_next is None or source_id > high_water_next:
                high_water_next = source_id
            await consider(source_message)
            if len(reg.STRICT_SCAN["ready"]) >= limit:
                archive_exhausted_next = False
                break

    reg.STRICT_SCAN["checkpoint_archive_next"] = archive_next
    reg.STRICT_SCAN["checkpoint_high_water_next"] = high_water_next
    reg.STRICT_SCAN["checkpoint_archive_exhausted_next"] = archive_exhausted_next
    reg.STRICT_SCAN["ready_for_upload"] = len(reg.STRICT_SCAN["ready"]) == int(limit)
    reg.STRICT_SCAN["checkpoint_commit_pending"] = bool(reg.STRICT_SCAN["ready_for_upload"])
    reg.STRICT_SCAN["scan_pass"] = "complete"
    return reg.STRICT_SCAN


async def _scan_worker(limit: int, reset_checkpoint: bool = False):
    global SCAN_ERROR
    SCAN_ERROR = None
    try:
        await _scan_unique_movies(limit, reset_checkpoint=reset_checkpoint)
    except Exception as exc:
        SCAN_ERROR = f"{type(exc).__name__}: {str(exc)[:500]}"


async def _strict_upload_with_checkpoint():
    await reg._strict_upload_worker()

    pending = bool(reg.STRICT_SCAN.get("checkpoint_commit_pending"))
    upload_ok = bool(reg.STATE.get("completed")) and int(reg.STATE.get("failed", 0) or 0) == 0
    if not pending or not upload_ok:
        return

    try:
        await _checkpoint_set(
            reg.STRICT_SCAN.get("checkpoint_archive_next"),
            reg.STRICT_SCAN.get("checkpoint_high_water_next"),
            bool(reg.STRICT_SCAN.get("checkpoint_archive_exhausted_next", False)),
        )
        reg.STRICT_SCAN["checkpoint_committed"] = True
        reg.STRICT_SCAN["checkpoint_commit_pending"] = False
        reg.STRICT_SCAN["checkpoint_commit_error"] = None
    except Exception as exc:
        # The movies are already safely registered. Keep the upload successful and
        # expose only the checkpoint error so the next scan can safely repeat work.
        reg.STRICT_SCAN["checkpoint_committed"] = False
        reg.STRICT_SCAN["checkpoint_commit_error"] = f"{type(exc).__name__}: {str(exc)[:300]}"


@app.post("/admin/telegram/movies/strict/scan")
async def strict_movie_scan_background(
    limit: int = Query(default=100, ge=1, le=200),
    reset_checkpoint: bool = Query(default=False),
    admin_key: Optional[str] = Header(default=None, alias="X-STRIMA-Admin-Key"),
):
    global SCAN_TASK, SCAN_ERROR
    base.require_admin_key(admin_key)

    if reg.TASK is not None and not reg.TASK.done():
        raise HTTPException(status_code=409, detail="Another movie registration task is running")
    if reg.STRICT_UPLOAD_TASK is not None and not reg.STRICT_UPLOAD_TASK.done():
        raise HTTPException(status_code=409, detail="Strict movie upload is running")
    if SCAN_TASK is not None and not SCAN_TASK.done():
        return {
            "ok": True,
            "started": False,
            "reason": "Strict movie scan is already running",
            "requested": reg.STRICT_SCAN.get("requested", limit),
            "ready_count": len(reg.STRICT_SCAN.get("ready", [])),
        }

    SCAN_ERROR = None
    SCAN_TASK = asyncio.create_task(
        _scan_worker(limit, reset_checkpoint=reset_checkpoint),
        name="strima-strict-movie-scan",
    )
    return {
        "ok": True,
        "started": True,
        "requested": int(limit),
        "checkpoint_mode": True,
        "checkpoint_reset": bool(reset_checkpoint),
        "message": "Checkpoint-aware unique strict preflight scan started in background. Poll /admin/telegram/movies/strict/status.",
    }


@app.get("/admin/telegram/movies/strict/status")
async def strict_movie_status_background(
    admin_key: Optional[str] = Header(default=None, alias="X-STRIMA-Admin-Key"),
):
    base.require_admin_key(admin_key)
    scan_running = bool(SCAN_TASK is not None and not SCAN_TASK.done())
    ready = reg.STRICT_SCAN.get("ready", [])
    return {
        "ok": True,
        "scan_running": scan_running,
        "scan_error": SCAN_ERROR,
        "requested": reg.STRICT_SCAN.get("requested", 0),
        "ready_count": len(ready),
        "scanned": reg.STRICT_SCAN.get("scanned", 0),
        "already_registered": reg.STRICT_SCAN.get("already_registered", 0),
        "destination_already_registered": reg.STRICT_SCAN.get("destination_already_registered", 0),
        "duplicates_blocked": reg.STRICT_SCAN.get("duplicates_blocked", 0),
        "unmatched_destination": reg.STRICT_SCAN.get("unmatched_destination", 0),
        "missing_filename": reg.STRICT_SCAN.get("missing_filename", 0),
        "metadata_match_failed": reg.STRICT_SCAN.get("metadata_match_failed", 0),
        "missing_artwork": reg.STRICT_SCAN.get("missing_artwork", 0),
        "wrong_content_blocked": reg.STRICT_SCAN.get("wrong_content_blocked", 0),
        "ready_for_upload": reg.STRICT_SCAN.get("ready_for_upload", False),
        "scan_pass": reg.STRICT_SCAN.get("scan_pass"),
        "checkpoint_active": reg.STRICT_SCAN.get("checkpoint_active", False),
        "checkpoint_archive_start": reg.STRICT_SCAN.get("checkpoint_archive_start"),
        "checkpoint_archive_next": reg.STRICT_SCAN.get("checkpoint_archive_next"),
        "checkpoint_high_water_start": reg.STRICT_SCAN.get("checkpoint_high_water_start"),
        "checkpoint_high_water_next": reg.STRICT_SCAN.get("checkpoint_high_water_next"),
        "checkpoint_archive_exhausted_next": reg.STRICT_SCAN.get("checkpoint_archive_exhausted_next", False),
        "checkpoint_commit_pending": reg.STRICT_SCAN.get("checkpoint_commit_pending", False),
        "checkpoint_committed": reg.STRICT_SCAN.get("checkpoint_committed", False),
        "checkpoint_commit_error": reg.STRICT_SCAN.get("checkpoint_commit_error"),
        "registered_source_ids_cached": reg.STRICT_SCAN.get("registered_source_ids_cached", 0),
        "registered_destination_ids_cached": reg.STRICT_SCAN.get("registered_destination_ids_cached", 0),
        "destination_map_cached": reg.STRICT_SCAN.get("destination_map_cached", False),
        "upload_running": bool(reg.STRICT_UPLOAD_TASK is not None and not reg.STRICT_UPLOAD_TASK.done()),
        "first_ready": ready[0] if ready else None,
        "last_ready": ready[-1] if ready else None,
        "upload_state": reg.STATE,
    }


@app.get("/admin/telegram/movies/strict/ready")
async def strict_movie_ready_list(
    admin_key: Optional[str] = Header(default=None, alias="X-STRIMA-Admin-Key"),
):
    base.require_admin_key(admin_key)
    return {
        "ok": True,
        "ready_count": len(reg.STRICT_SCAN.get("ready", [])),
        "ready_for_upload": reg.STRICT_SCAN.get("ready_for_upload", False),
        "ready": reg.STRICT_SCAN.get("ready", []),
    }


@app.post("/admin/telegram/movies/strict/upload/start")
async def strict_movie_upload_start_checkpoint(
    admin_key: Optional[str] = Header(default=None, alias="X-STRIMA-Admin-Key"),
):
    base.require_admin_key(admin_key)
    if not reg.STRICT_SCAN.get("ready_for_upload") or not reg.STRICT_SCAN.get("ready"):
        raise HTTPException(status_code=400, detail="Run a successful strict scan first")
    if reg.STRICT_UPLOAD_TASK is not None and not reg.STRICT_UPLOAD_TASK.done():
        return {"ok": True, "started": False, "reason": "Strict movie upload is already running", **reg.STATE}

    reg.STRICT_UPLOAD_TASK = asyncio.create_task(
        _strict_upload_with_checkpoint(),
        name="strima-strict-movie-upload-checkpoint",
    )
    return {
        "ok": True,
        "started": True,
        "target": len(reg.STRICT_SCAN["ready"]),
        "ready_for_upload": True,
        "checkpoint_commit_after_success": True,
    }
