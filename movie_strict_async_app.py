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


# Replace the older synchronous strict scan/status routes with background-safe,
# batch-unique versions.
for route in list(app.router.routes):
    path = getattr(route, "path", None)
    methods = set(getattr(route, "methods", set()) or set())
    if path == "/admin/telegram/movies/strict/scan" and "POST" in methods:
        app.router.routes.remove(route)
    elif path == "/admin/telegram/movies/strict/status" and "GET" in methods:
        app.router.routes.remove(route)


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


async def _scan_unique_movies(limit: int):
    if not base.client.is_connected():
        raise RuntimeError("Telegram client is disconnected")
    if base.SOURCE_INPUT_ENTITY is None:
        raise RuntimeError("Old movie source channel is not resolved")
    if base.CHANNEL_INPUT_ENTITY is None:
        raise RuntimeError("Premium movie destination channel is not resolved")

    reg.STRICT_SCAN.update({
        "ready": [],
        "requested": int(limit),
        "scanned": 0,
        "already_registered": 0,
        "duplicates_blocked": 0,
        "unmatched_destination": 0,
        "missing_filename": 0,
        "metadata_match_failed": 0,
        "missing_artwork": 0,
        "wrong_content_blocked": 0,
        "ready_for_upload": False,
    })

    doc_to_message, fingerprint_to_message = await reg._build_destination_maps()
    seen_tmdb = set()
    seen_destinations = set()
    seen_titles = set()

    async for source_message in base.client.iter_messages(base.SOURCE_INPUT_ENTITY):
        if len(reg.STRICT_SCAN["ready"]) >= limit:
            break

        reg.STRICT_SCAN["scanned"] += 1
        file_obj = getattr(source_message, "file", None)
        if not base.is_video_file(file_obj, source_message):
            continue

        item = base.source_item_from_message(source_message)
        if (item.get("content_kind") or "movie") != "movie" or item.get("episode_number"):
            reg.STRICT_SCAN["wrong_content_blocked"] += 1
            continue

        source_id = int(source_message.id)
        if await importer._source_lookup(source_id):
            reg.STRICT_SCAN["already_registered"] += 1
            continue

        matches = await importer._duplicate_check(item)
        top = matches[0] if isinstance(matches, list) and matches else None
        if top and top.get("decision") == "duplicate":
            reg.STRICT_SCAN["duplicates_blocked"] += 1
            continue

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
            continue

        filename = str(getattr(file_obj, "name", "") or "").strip()
        if not filename:
            reg.STRICT_SCAN["missing_filename"] += 1
            continue

        tmdb = await reg._tmdb_preflight(item)
        if not tmdb:
            reg.STRICT_SCAN["metadata_match_failed"] += 1
            continue
        if not tmdb.get("artwork_ok"):
            reg.STRICT_SCAN["missing_artwork"] += 1
            continue

        destination_message_id = int(destination_message_id)
        candidate = tmdb.get("candidate") or {}
        tmdb_id = candidate.get("id")
        tmdb_kind = str(tmdb.get("kind") or "movie")
        tmdb_title = reg._candidate_title(candidate)
        public_title = importer._clean_public_title(item)

        # Critical batch-level protection: the database duplicate checker only
        # knows about rows already in Supabase. This prevents multiple Telegram
        # copies of the same movie from entering the same preflight batch.
        tmdb_key = (tmdb_kind, str(tmdb_id)) if tmdb_id is not None else None
        title_key = _norm(tmdb_title or public_title)
        if (
            destination_message_id in seen_destinations
            or (tmdb_key is not None and tmdb_key in seen_tmdb)
            or (tmdb_key is None and title_key and title_key in seen_titles)
        ):
            reg.STRICT_SCAN["duplicates_blocked"] += 1
            continue

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

    reg.STRICT_SCAN["ready_for_upload"] = len(reg.STRICT_SCAN["ready"]) == int(limit)
    return reg.STRICT_SCAN


async def _scan_worker(limit: int):
    global SCAN_ERROR
    SCAN_ERROR = None
    try:
        await _scan_unique_movies(limit)
    except Exception as exc:
        SCAN_ERROR = f"{type(exc).__name__}: {str(exc)[:500]}"


@app.post("/admin/telegram/movies/strict/scan")
async def strict_movie_scan_background(
    limit: int = Query(default=100, ge=1, le=200),
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
    SCAN_TASK = asyncio.create_task(_scan_worker(limit), name="strima-strict-movie-scan")
    return {
        "ok": True,
        "started": True,
        "requested": int(limit),
        "message": "Unique strict preflight scan started in background. Poll /admin/telegram/movies/strict/status.",
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
        "duplicates_blocked": reg.STRICT_SCAN.get("duplicates_blocked", 0),
        "unmatched_destination": reg.STRICT_SCAN.get("unmatched_destination", 0),
        "missing_filename": reg.STRICT_SCAN.get("missing_filename", 0),
        "metadata_match_failed": reg.STRICT_SCAN.get("metadata_match_failed", 0),
        "missing_artwork": reg.STRICT_SCAN.get("missing_artwork", 0),
        "wrong_content_blocked": reg.STRICT_SCAN.get("wrong_content_blocked", 0),
        "ready_for_upload": reg.STRICT_SCAN.get("ready_for_upload", False),
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
