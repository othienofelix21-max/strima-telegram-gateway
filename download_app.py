import asyncio
from typing import Optional
from urllib.parse import quote

from fastapi import Header, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse

import app as base
import movie_strict_async_app as guarded

# Keep all existing STRIMA routes and add dedicated Telegram download/audit routes.
app = guarded.app

INVENTORY_TASK = None
INVENTORY_ERROR = None
INVENTORY_ITEMS = []
INVENTORY_STATE = {
    "running": False,
    "completed": False,
    "scanned_messages": 0,
    "total_channel_videos": 0,
    "uploaded_to_supabase": 0,
    "not_uploaded_to_supabase": 0,
    "movie_like_videos": 0,
    "episode_like_videos": 0,
}


def _download_filename(message) -> str:
    file_obj = getattr(message, "file", None)
    name = str(getattr(file_obj, "name", None) or f"telegram-{message.id}.mp4")
    return name.replace("\r", "").replace("\n", "").replace('"', "'")


def _channel_title(message) -> str:
    file_obj = getattr(message, "file", None)
    filename = str(getattr(file_obj, "name", None) or "").strip()
    caption = str(getattr(message, "message", None) or "").strip()
    return base.clean_detected_title(filename, caption) or filename or caption or f"Telegram {message.id}"


async def _inventory_worker():
    global INVENTORY_ERROR, INVENTORY_ITEMS
    INVENTORY_ERROR = None
    INVENTORY_ITEMS = []
    INVENTORY_STATE.update({
        "running": True,
        "completed": False,
        "scanned_messages": 0,
        "total_channel_videos": 0,
        "uploaded_to_supabase": 0,
        "not_uploaded_to_supabase": 0,
        "movie_like_videos": 0,
        "episode_like_videos": 0,
    })

    try:
        if not base.client.is_connected():
            raise RuntimeError("Telegram client is disconnected")
        if base.CHANNEL_INPUT_ENTITY is None:
            raise RuntimeError("Premium movie destination channel is not resolved")

        _, registered_destination_ids = await guarded._registered_id_sets()

        async for message in base.client.iter_messages(base.CHANNEL_INPUT_ENTITY):
            INVENTORY_STATE["scanned_messages"] += 1
            file_obj = getattr(message, "file", None)
            if not base.is_video_file(file_obj, message):
                continue

            destination_message_id = int(message.id)
            uploaded = destination_message_id in registered_destination_ids
            filename = str(getattr(file_obj, "name", None) or "").strip()
            title = _channel_title(message)
            structure = base.detect_content_structure(title or filename)
            content_kind = str(structure.get("content_kind") or "movie")
            size_bytes = int(getattr(file_obj, "size", 0) or 0)

            INVENTORY_STATE["total_channel_videos"] += 1
            if uploaded:
                INVENTORY_STATE["uploaded_to_supabase"] += 1
            else:
                INVENTORY_STATE["not_uploaded_to_supabase"] += 1

            if content_kind == "episode":
                INVENTORY_STATE["episode_like_videos"] += 1
            else:
                INVENTORY_STATE["movie_like_videos"] += 1

            INVENTORY_ITEMS.append({
                "destination_message_id": destination_message_id,
                "title": title,
                "file_name": filename or None,
                "status": "UPLOADED" if uploaded else "NOT_UPLOADED",
                "uploaded_to_supabase": uploaded,
                "content_kind": content_kind,
                "season_number": structure.get("season_number"),
                "episode_number": structure.get("episode_number"),
                "file_size_mb": round(size_bytes / (1024 * 1024), 2) if size_bytes else None,
            })

        INVENTORY_STATE["completed"] = True
    except Exception as exc:
        INVENTORY_ERROR = f"{type(exc).__name__}: {str(exc)[:500]}"
    finally:
        INVENTORY_STATE["running"] = False


@app.post("/admin/telegram/movies/channel-inventory/start")
async def movie_channel_inventory_start(
    admin_key: Optional[str] = Header(default=None, alias="X-STRIMA-Admin-Key"),
):
    global INVENTORY_TASK
    base.require_admin_key(admin_key)

    if INVENTORY_TASK is not None and not INVENTORY_TASK.done():
        return {
            "ok": True,
            "started": False,
            "reason": "Movie inventory audit is already running",
            **INVENTORY_STATE,
        }

    INVENTORY_TASK = asyncio.create_task(_inventory_worker(), name="strima-movie-channel-inventory")
    return {
        "ok": True,
        "started": True,
        "message": "Movie inventory audit started in background. Poll /admin/telegram/movies/channel-inventory/status.",
    }


@app.get("/admin/telegram/movies/channel-inventory/status")
async def movie_channel_inventory_status(
    admin_key: Optional[str] = Header(default=None, alias="X-STRIMA-Admin-Key"),
):
    base.require_admin_key(admin_key)
    return {
        "ok": INVENTORY_ERROR is None,
        "error": INVENTORY_ERROR,
        **INVENTORY_STATE,
        "items_cached": len(INVENTORY_ITEMS),
    }


@app.get("/admin/telegram/movies/channel-inventory")
async def movie_channel_inventory(
    status: str = Query(default="all", pattern="^(all|uploaded|not_uploaded)$"),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=200, ge=1, le=500),
    admin_key: Optional[str] = Header(default=None, alias="X-STRIMA-Admin-Key"),
):
    """Return cached inventory results without re-scanning Telegram."""
    base.require_admin_key(admin_key)

    if not INVENTORY_STATE["completed"] and not INVENTORY_ITEMS:
        raise HTTPException(status_code=409, detail="Start the movie inventory audit first")

    if status == "uploaded":
        filtered = [row for row in INVENTORY_ITEMS if row["uploaded_to_supabase"]]
    elif status == "not_uploaded":
        filtered = [row for row in INVENTORY_ITEMS if not row["uploaded_to_supabase"]]
    else:
        filtered = INVENTORY_ITEMS

    page = filtered[offset: offset + limit]
    return {
        "ok": INVENTORY_ERROR is None,
        "error": INVENTORY_ERROR,
        "destination_channel_id": base.TG_CHANNEL_ID,
        "summary": {
            "total_channel_videos": INVENTORY_STATE["total_channel_videos"],
            "uploaded_to_supabase": INVENTORY_STATE["uploaded_to_supabase"],
            "not_uploaded_to_supabase": INVENTORY_STATE["not_uploaded_to_supabase"],
            "movie_like_videos": INVENTORY_STATE["movie_like_videos"],
            "episode_like_videos": INVENTORY_STATE["episode_like_videos"],
        },
        "running": INVENTORY_STATE["running"],
        "completed": INVENTORY_STATE["completed"],
        "filter": status,
        "filtered_total": len(filtered),
        "offset": offset,
        "limit": limit,
        "returned": len(page),
        "items": page,
    }


@app.api_route("/download/{message_id}", methods=["GET", "HEAD"])
async def download_movie(
    request: Request,
    message_id: int,
    range_header: Optional[str] = Header(default=None, alias="Range"),
):
    """Return the real Telegram media bytes as a downloadable file."""
    message, size = await base.get_movie_message(message_id)
    start, end, partial = base.parse_range_header(range_header, size)
    headers = base.media_headers(message, size, start, end, partial)

    filename = _download_filename(message)
    headers["Content-Disposition"] = (
        "attachment; filename*=UTF-8''" + quote(filename, safe="")
    )
    headers["X-STRIMA-Download"] = "1"

    if request.method == "HEAD":
        return Response(
            status_code=206 if partial else 200,
            headers=headers,
        )

    return StreamingResponse(
        base.telegram_byte_stream(message, start, end),
        status_code=206 if partial else 200,
        headers=headers,
        media_type=headers["Content-Type"],
    )
