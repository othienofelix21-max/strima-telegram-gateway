from typing import Optional

from fastapi import Header, HTTPException

import app as base

app = base.app


@app.post("/admin/telegram/import-one/{source_message_id}")
async def import_one_movie(
    source_message_id: int,
    admin_key: Optional[str] = Header(default=None, alias="X-STRIMA-Admin-Key"),
):
    base.require_admin_key(admin_key)

    if not base.client.is_connected():
        raise HTTPException(status_code=503, detail="Telegram client is disconnected")
    if base.SOURCE_INPUT_ENTITY is None:
        raise HTTPException(status_code=503, detail="Telegram source channel is not resolved")
    if base.CHANNEL_INPUT_ENTITY is None:
        raise HTTPException(status_code=503, detail="Telegram destination channel is not resolved")

    source_message = await base.client.get_messages(
        base.SOURCE_INPUT_ENTITY,
        ids=source_message_id,
    )
    if not source_message or not getattr(source_message, "file", None):
        raise HTTPException(status_code=404, detail="Source message not found or has no media")
    if not base.is_video_file(source_message.file, source_message):
        raise HTTPException(status_code=400, detail="Source message is not a supported video")

    item = base.source_item_from_message(source_message)
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

    return {
        "ok": True,
        "stage": "ready_for_copy",
        "source_message_id": source_message_id,
        "destination_channel_id": base.TG_CHANNEL_ID,
        "item": item,
    }
