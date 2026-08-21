from typing import Optional
from urllib.parse import quote

from fastapi import Header, Request, Response
from fastapi.responses import StreamingResponse

import app as base
import movie_strict_async_app as guarded

# Keep all existing STRIMA routes and add a dedicated Telegram file-download route.
app = guarded.app


def _download_filename(message) -> str:
    file_obj = getattr(message, "file", None)
    name = str(getattr(file_obj, "name", None) or f"telegram-{message.id}.mp4")
    # Prevent malformed response headers while preserving the user's filename.
    return name.replace("\r", "").replace("\n", "").replace('"', "'")


@app.api_route("/download/{message_id}", methods=["GET", "HEAD"])
async def download_movie(
    request: Request,
    message_id: int,
    range_header: Optional[str] = Header(default=None, alias="Range"),
):
    """Return the real Telegram media bytes as a downloadable file.

    This is deliberately separate from /movie/{message_id}, which is used for
    playback. Range requests are supported so Android download clients can
    resume and report byte progress reliably.
    """
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
