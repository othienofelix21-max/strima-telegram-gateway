from typing import Optional
from urllib.parse import quote

from fastapi import Header, Query, Request, Response
from fastapi.responses import StreamingResponse

import app as base
import movie_strict_async_app as guarded

# Keep all existing STRIMA routes and add dedicated Telegram download/audit routes.
app = guarded.app


def _download_filename(message) -> str:
    file_obj = getattr(message, "file", None)
    name = str(getattr(file_obj, "name", None) or f"telegram-{message.id}.mp4")
    return name.replace("\r", "").replace("\n", "").replace('"', "'")


def _channel_title(message) -> str:
    file_obj = getattr(message, "file", None)
    filename = str(getattr(file_obj, "name", None) or "").strip()
    caption = str(getattr(message, "message", None) or "").strip()
    return base.clean_detected_title(filename, caption) or filename or caption or f"Telegram {message.id}"


@app.get("/admin/telegram/movies/channel-inventory")
async def movie_channel_inventory(
    status: str = Query(default="all", pattern="^(all|uploaded|not_uploaded)$"),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=200, ge=1, le=500),
    admin_key: Optional[str] = Header(default=None, alias="X-STRIMA-Admin-Key"),
):
    """Audit every video in the STRIMA movie destination channel against Supabase.

    status=uploaded returns destination videos already registered in movies.
    status=not_uploaded returns destination videos not yet registered in movies.
    status=all returns both. Results are paginated with offset/limit.
    """
    base.require_admin_key(admin_key)
    if not base.client.is_connected():
        raise RuntimeError("Telegram client is disconnected")
    if base.CHANNEL_INPUT_ENTITY is None:
        raise RuntimeError("Premium movie destination channel is not resolved")

    _, registered_destination_ids = await guarded._registered_id_sets()

    total_videos = 0
    uploaded_count = 0
    not_uploaded_count = 0
    movie_like_count = 0
    episode_like_count = 0
    filtered_total = 0
    items = []

    async for message in base.client.iter_messages(base.CHANNEL_INPUT_ENTITY):
        file_obj = getattr(message, "file", None)
        if not base.is_video_file(file_obj, message):
            continue

        total_videos += 1
        destination_message_id = int(message.id)
        uploaded = destination_message_id in registered_destination_ids
        if uploaded:
            uploaded_count += 1
        else:
            not_uploaded_count += 1

        filename = str(getattr(file_obj, "name", None) or "").strip()
        caption = str(getattr(message, "message", None) or "").strip()
        title = _channel_title(message)
        structure = base.detect_content_structure(title or filename)
        content_kind = str(structure.get("content_kind") or "movie")
        if content_kind == "episode":
            episode_like_count += 1
        else:
            movie_like_count += 1

        matches_filter = (
            status == "all"
            or (status == "uploaded" and uploaded)
            or (status == "not_uploaded" and not uploaded)
        )
        if not matches_filter:
            continue

        row_index = filtered_total
        filtered_total += 1
        if row_index < offset or len(items) >= limit:
            continue

        size_bytes = int(getattr(file_obj, "size", 0) or 0)
        items.append({
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

    return {
        "ok": True,
        "destination_channel_id": base.TG_CHANNEL_ID,
        "summary": {
            "total_channel_videos": total_videos,
            "uploaded_to_supabase": uploaded_count,
            "not_uploaded_to_supabase": not_uploaded_count,
            "movie_like_videos": movie_like_count,
            "episode_like_videos": episode_like_count,
        },
        "filter": status,
        "filtered_total": filtered_total,
        "offset": offset,
        "limit": limit,
        "returned": len(items),
        "items": items,
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
