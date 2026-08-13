import asyncio
import logging
from typing import Optional

from fastapi import Header, HTTPException
from fastapi.responses import JSONResponse

import app as base

app = base.app
log = logging.getLogger("strima-import")

# One process / one worker is used in Bunny. These locks prevent accidental
# double-click copies during the same running container session.
_copy_lock = asyncio.Lock()
_copied_this_runtime: dict[int, int] = {}


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

        # Prevent a repeated click in the same Bunny container session.
        if source_message_id in _copied_this_runtime:
            destination_message_id = _copied_this_runtime[source_message_id]
            return {
                "ok": True,
                "stage": "already_copied_this_runtime",
                "source_message_id": source_message_id,
                "destination_message_id": destination_message_id,
                "playback_url": f"https://mc-p2ku5nz4qw.bunny.run/movie/{destination_message_id}",
            }

        async with _copy_lock:
            if source_message_id in _copied_this_runtime:
                destination_message_id = _copied_this_runtime[source_message_id]
                return {
                    "ok": True,
                    "stage": "already_copied_this_runtime",
                    "source_message_id": source_message_id,
                    "destination_message_id": destination_message_id,
                    "playback_url": f"https://mc-p2ku5nz4qw.bunny.run/movie/{destination_message_id}",
                }

            stage = "fetch_source"
            source_message = await _get_source_message(source_message_id)

            if not source_message or not getattr(source_message, "file", None):
                raise HTTPException(status_code=404, detail="Source message not found or has no media")

            if not base.is_video_file(source_message.file, source_message):
                raise HTTPException(status_code=400, detail="Source message is not a supported video")

            stage = "extract_metadata"
            item = base.source_item_from_message(source_message)

            # Final Supabase duplicate check before any Telegram copy.
            stage = "supabase_duplicate_check"
            matches = await base.supabase_duplicate_rpc(item)
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

            return {
                "ok": True,
                "stage": "copied",
                "source_channel_id": base.TG_SOURCE_CHANNEL_ID,
                "source_message_id": source_message_id,
                "destination_channel_id": base.TG_CHANNEL_ID,
                "destination_message_id": destination_message_id,
                "playback_url": f"https://mc-p2ku5nz4qw.bunny.run/movie/{destination_message_id}",
                "item": item,
                "note": "Copy completed. Persistent Supabase mapping will be recorded next.",
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
