import asyncio
import json
import logging
import re
import urllib.error
import urllib.request
from typing import Optional

from fastapi import Header, HTTPException, Query
from fastapi.responses import JSONResponse

import app as base

app = base.app
log = logging.getLogger("strima-import")

# One process / one worker is used in Bunny. These locks prevent accidental
# double-click copies during the same running container session.
_copy_lock = asyncio.Lock()
_copied_this_runtime: dict[int, int] = {}


def _rpc_sync(name: str, payload: dict):
    if not base.SUPABASE_URL or not base.SUPABASE_PUBLISHABLE_KEY:
        raise RuntimeError("SUPABASE_URL or SUPABASE_PUBLISHABLE_KEY is missing")

    url = f"{base.SUPABASE_URL}/rest/v1/rpc/{name}"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "apikey": base.SUPABASE_PUBLISHABLE_KEY,
            "Authorization": f"Bearer {base.SUPABASE_PUBLISHABLE_KEY}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Supabase RPC {name} HTTP {exc.code}: {body[:500]}") from exc


async def _rpc(name: str, payload: dict):
    return await asyncio.to_thread(_rpc_sync, name, payload)


async def _get_source_message(source_message_id: int):
    """Fetch one exact source message and return a useful error if it fails."""
    try:
        message = await base.client.get_messages(
            base.SOURCE_INPUT_ENTITY,
            ids=source_message_id,
        )
        return message
    except Exception:
        log.exception("Failed to fetch source Telegram message %s", source_message_id)
        raise


def _clean_public_title(item: dict) -> str:
    """Turn Telegram-style filenames into a cleaner title for STRIMA."""
    raw = str(item.get("detected_title") or item.get("file_name") or "").strip()
    raw = re.sub(r"\.(mp4|mkv|mov|avi|webm|m4v)$", "", raw, flags=re.IGNORECASE)
    raw = raw.replace("_", " ").replace(".", " ")
    raw = re.sub(r"\(\s*no'?s\s*\)", " ", raw, flags=re.IGNORECASE)
    raw = re.sub(r"\b(360p|480p|540p|720p|1080p|1440p|2160p|4k|uhd|hdr|x264|x265|h264|h265|hevc)\b", " ", raw, flags=re.IGNORECASE)

    year = item.get("detected_year")
    if year:
        raw = re.sub(rf"\b{int(year)}\b", " ", raw)

    # Remove an explicit VJ label directly from the filename/title. This is more
    # reliable than depending only on metadata extraction and prevents titles such
    # as "Helen Of Troy 1 Jr" when the source was "... VJ JR".
    raw = re.sub(
        r"\bVJ[\s._-]*[A-Za-z][A-Za-z0-9'-]{1,30}\b",
        " ",
        raw,
        flags=re.IGNORECASE,
    )

    vj_name = str(item.get("detected_vj_name") or "").strip()
    if vj_name:
        raw = re.sub(re.escape(vj_name), " ", raw, flags=re.IGNORECASE)

    raw = re.sub(r"\bVJ\b", " ", raw, flags=re.IGNORECASE)
    raw = re.sub(r"\s+", " ", raw).strip(" -_()[]")

    if not raw:
        raw = str(item.get("detected_title") or item.get("file_name") or "Untitled Movie")

    # Keep short all-caps source titles readable; otherwise use title case.
    letters = [c for c in raw if c.isalpha()]
    if letters and sum(c.isupper() for c in letters) / len(letters) > 0.75:
        return raw.title()
    return raw


def _description_from_item(item: dict, public_title: str) -> Optional[str]:
    caption = str(item.get("caption") or "").strip()
    if not caption:
        return None

    # Many source captions contain only the movie title. Do not store that as a description.
    normalized_caption = base.normalize_title_for_compare(caption)
    normalized_title = base.normalize_title_for_compare(public_title)
    if normalized_caption == normalized_title or len(caption) < 40:
        return None
    return caption[:1500]


async def _source_lookup(source_message_id: int):
    rows = await _rpc(
        "strima_gateway_source_lookup",
        {
            "p_admin_key": base.STRIMA_ADMIN_KEY,
            "p_source_channel_id": base.TG_SOURCE_CHANNEL_ID,
            "p_source_message_id": source_message_id,
        },
    )
    return rows[0] if isinstance(rows, list) and rows else None


async def _duplicate_check(item: dict):
    return await _rpc(
        "strima_gateway_duplicate_check_v2",
        {
            "p_title": item.get("detected_title") or item.get("file_name") or "",
            "p_normalized_title": item.get("normalized_title"),
            "p_year": item.get("detected_year"),
            "p_file_size_bytes": item.get("file_size_bytes"),
            "p_duration_seconds": item.get("duration_seconds"),
            "p_content_kind": item.get("content_kind") or "movie",
            "p_part_number": item.get("part_number"),
            "p_season_number": item.get("season_number"),
            "p_episode_number": item.get("episode_number"),
        },
    )


async def _register_movie(item: dict, destination_message_id: int):
    public_title = _clean_public_title(item)
    playback_url = f"https://mc-p2ku5nz4qw.bunny.run/movie/{destination_message_id}"
    rows = await _rpc(
        "strima_gateway_register_movie_v2",
        {
            "p_admin_key": base.STRIMA_ADMIN_KEY,
            "p_title": public_title,
            "p_normalized_title": item.get("normalized_title"),
            "p_year": item.get("detected_year"),
            "p_duration_seconds": item.get("duration_seconds"),
            "p_duration_text": item.get("duration_text"),
            "p_vj_name": item.get("detected_vj_name"),
            "p_source_channel_id": base.TG_SOURCE_CHANNEL_ID,
            "p_source_message_id": item.get("source_message_id"),
            "p_destination_channel_id": base.TG_CHANNEL_ID,
            "p_destination_message_id": destination_message_id,
            "p_playback_url": playback_url,
            "p_file_size_mb": item.get("file_size_mb"),
            "p_description": _description_from_item(item, public_title),
            "p_category_slug": None,
            "p_content_kind": item.get("content_kind") or "movie",
            "p_part_number": item.get("part_number"),
            "p_season_number": item.get("season_number"),
            "p_episode_number": item.get("episode_number"),
        },
    )
    return rows[0] if isinstance(rows, list) and rows else None


@app.post("/admin/telegram/import-one/{source_message_id}")
async def import_one_movie(
    source_message_id: int,
    admin_key: Optional[str] = Header(default=None, alias="X-STRIMA-Admin-Key"),
):
    base.require_admin_key(admin_key)

    stage = "preflight"

    try:
        if not base.client.is_connected():
            raise HTTPException(status_code=503, detail="Telegram client is disconnected")
        if base.SOURCE_INPUT_ENTITY is None:
            raise HTTPException(status_code=503, detail="Telegram source channel is not resolved")
        if base.CHANNEL_INPUT_ENTITY is None:
            raise HTTPException(status_code=503, detail="Telegram destination channel is not resolved")

        # Persistent exact-source duplicate check BEFORE touching Telegram.
        stage = "source_lookup"
        existing = await _source_lookup(source_message_id)
        if existing:
            return {
                "ok": True,
                "stage": "already_registered",
                "source_message_id": source_message_id,
                "movie": existing,
            }

        async with _copy_lock:
            # Check again after waiting for the lock.
            existing = await _source_lookup(source_message_id)
            if existing:
                return {
                    "ok": True,
                    "stage": "already_registered",
                    "source_message_id": source_message_id,
                    "movie": existing,
                }

            stage = "fetch_source"
            source_message = await _get_source_message(source_message_id)

            if not source_message or not getattr(source_message, "file", None):
                raise HTTPException(status_code=404, detail="Source message not found or has no media")

            if not base.is_video_file(source_message.file, source_message):
                raise HTTPException(status_code=400, detail="Source message is not a supported video")

            stage = "extract_metadata"
            item = base.source_item_from_message(source_message)

            # If this source was copied during this runtime but registration failed,
            # retry Supabase registration without copying Telegram again.
            if source_message_id in _copied_this_runtime:
                destination_message_id = _copied_this_runtime[source_message_id]
                stage = "supabase_register_retry"
                movie = await _register_movie(item, destination_message_id)
                return {
                    "ok": True,
                    "stage": "registered_after_retry",
                    "source_message_id": source_message_id,
                    "destination_message_id": destination_message_id,
                    "playback_url": f"https://mc-p2ku5nz4qw.bunny.run/movie/{destination_message_id}",
                    "movie": movie,
                    "item": item,
                }

            # Canonical duplicate check protects against the same movie existing
            # under another Telegram source message while preserving sequels,
            # explicit multipart movies, seasons and episodes.
            stage = "supabase_duplicate_check"
            matches = await _duplicate_check(item)
            top = matches[0] if isinstance(matches, list) and matches else None

            if top and top.get("decision") == "duplicate":
                return {
                    "ok": True,
                    "stage": "blocked_duplicate",
                    "source_message_id": source_message_id,
                    "decision": "ALREADY_IN_SUPABASE",
                    "match": top,
                }

            # Re-send the existing Telegram media handle as a new message.
            # This is not forward_messages(), so there is no forwarded-from label.
            stage = "telegram_copy"
            destination_message = await base.client.send_file(
                base.CHANNEL_INPUT_ENTITY,
                file=source_message.media,
                caption=source_message.message or "",
                formatting_entities=(source_message.entities or None),
            )

            if isinstance(destination_message, (list, tuple)):
                destination_message = destination_message[0] if destination_message else None

            if not destination_message or not getattr(destination_message, "id", None):
                raise RuntimeError("Telegram did not return a destination message ID")

            destination_message_id = int(destination_message.id)
            _copied_this_runtime[source_message_id] = destination_message_id

            stage = "supabase_register"
            movie = await _register_movie(item, destination_message_id)

            return {
                "ok": True,
                "stage": "registered",
                "source_channel_id": base.TG_SOURCE_CHANNEL_ID,
                "source_message_id": source_message_id,
                "destination_channel_id": base.TG_CHANNEL_ID,
                "destination_message_id": destination_message_id,
                "playback_url": f"https://mc-p2ku5nz4qw.bunny.run/movie/{destination_message_id}",
                "movie": movie,
                "item": item,
            }

    except HTTPException:
        raise
    except Exception as exc:
        log.exception(
            "STRIMA import-one failed at stage=%s source_message_id=%s",
            stage,
            source_message_id,
        )
        return JSONResponse(
            status_code=500,
            content={
                "ok": False,
                "stage": stage,
                "source_message_id": source_message_id,
                "error_type": type(exc).__name__,
                "error": str(exc)[:500],
            },
        )


@app.post("/admin/telegram/import-batch")
async def import_movie_batch(
    limit: int = Query(default=5, ge=1, le=20),
    admin_key: Optional[str] = Header(default=None, alias="X-STRIMA-Admin-Key"),
):
    """Controlled sequential batch import. Start with 5; maximum is 20 per call."""
    base.require_admin_key(admin_key)

    movies, scanned = await base.collect_source_movies(limit)
    results = []
    registered = 0
    skipped = 0
    failed = 0

    for item in movies:
        source_message_id = int(item["source_message_id"])
        result = await import_one_movie(source_message_id, admin_key)

        if isinstance(result, JSONResponse):
            failed += 1
            results.append({"source_message_id": source_message_id, "stage": "failed"})
        else:
            stage = result.get("stage")
            if stage in {"registered", "registered_after_retry"}:
                registered += 1
            elif stage in {"already_registered", "blocked_duplicate"}:
                skipped += 1
            else:
                skipped += 1
            results.append({
                "source_message_id": source_message_id,
                "stage": stage,
                "destination_message_id": result.get("destination_message_id"),
                "playback_url": result.get("playback_url"),
            })

        # Be gentle with Telegram while we validate the production pipeline.
        await asyncio.sleep(1)

    return {
        "ok": failed == 0,
        "requested": limit,
        "scanned": scanned,
        "registered": registered,
        "skipped": skipped,
        "failed": failed,
        "results": results,
    }