import asyncio
import logging
import mimetypes
import os
import re
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional, Tuple

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from telethon import TelegramClient
from telethon.sessions import MemorySession

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("strima-gateway")

TG_API_ID = int(os.environ["TG_API_ID"])
TG_API_HASH = os.environ["TG_API_HASH"]
TG_BOT_TOKEN = os.environ["TG_BOT_TOKEN"]
TG_CHANNEL_ID = int(os.environ["TG_CHANNEL_ID"])

PORT = int(os.getenv("PORT", "80"))
CHUNK_SIZE = int(os.getenv("TG_CHUNK_SIZE", str(512 * 1024)))
CACHE_SECONDS = int(os.getenv("CACHE_SECONDS", "86400"))

client = TelegramClient(
    MemorySession(),
    TG_API_ID,
    TG_API_HASH,
    sequential_updates=False,
    connection_retries=10,
    retry_delay=2,
    auto_reconnect=True,
)

@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Connecting STRIMA Telegram Gateway to Telegram...")
    await client.start(bot_token=TG_BOT_TOKEN)
    me = await client.get_me()
    log.info("Telegram connected as @%s (%s)", getattr(me, "username", None), me.id)
    try:
        yield
    finally:
        await client.disconnect()
        log.info("Telegram disconnected")

app = FastAPI(title="STRIMA Telegram Gateway", version="0.1.0", lifespan=lifespan)

def parse_range_header(value: Optional[str], size: int) -> Tuple[int, int, bool]:
    if not value:
        return 0, size - 1, False
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", value.strip())
    if not match:
        raise HTTPException(
            status_code=416,
            detail="Only a single bytes=start-end range is supported",
            headers={"Content-Range": f"bytes */{size}"},
        )
    left, right = match.groups()
    if left == "" and right == "":
        raise HTTPException(
            status_code=416,
            detail="Invalid byte range",
            headers={"Content-Range": f"bytes */{size}"},
        )
    if left == "":
        suffix = int(right)
        if suffix <= 0:
            raise HTTPException(
                status_code=416,
                detail="Invalid suffix range",
                headers={"Content-Range": f"bytes */{size}"},
            )
        suffix = min(suffix, size)
        start = size - suffix
        end = size - 1
    else:
        start = int(left)
        end = int(right) if right else size - 1

    if start >= size or start < 0 or end < start:
        raise HTTPException(
            status_code=416,
            detail="Range outside file",
            headers={"Content-Range": f"bytes */{size}"},
        )
    end = min(end, size - 1)
    return start, end, True

async def get_movie_message(message_id: int):
    try:
        message = await client.get_messages(TG_CHANNEL_ID, ids=message_id)
    except Exception as exc:
        log.exception("Telegram message lookup failed")
        raise HTTPException(
            status_code=502,
            detail=f"Telegram lookup failed: {type(exc).__name__}",
        )
    if not message or not getattr(message, "media", None) or not getattr(message, "file", None):
        raise HTTPException(status_code=404, detail="Telegram movie not found")
    size = int(message.file.size or 0)
    if size <= 0:
        raise HTTPException(status_code=404, detail="Telegram media has no downloadable file")
    return message, size

def media_headers(message, size: int, start: int, end: int, partial: bool):
    filename = message.file.name or f"telegram-{message.id}.mp4"
    mime = (
        message.file.mime_type
        or mimetypes.guess_type(filename)[0]
        or "application/octet-stream"
    )
    headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": f"public, max-age={CACHE_SECONDS}",
        "Access-Control-Allow-Origin": "*",
        "Content-Type": mime,
        "Content-Length": str(end - start + 1),
        "ETag": f'"tg-{TG_CHANNEL_ID}-{message.id}-{size}"',
        "X-STRIMA-Telegram-Message": str(message.id),
    }
    if partial:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    return headers

async def telegram_byte_stream(message, start: int, end: int) -> AsyncIterator[bytes]:
    remaining = end - start + 1
    try:
        iterator = client.iter_download(
            message.media,
            offset=start,
            request_size=CHUNK_SIZE,
            chunk_size=CHUNK_SIZE,
            file_size=int(message.file.size or 0),
        )
        async for chunk in iterator:
            if remaining <= 0:
                break
            data = bytes(chunk)
            if len(data) > remaining:
                data = data[:remaining]
            remaining -= len(data)
            if data:
                yield data
            if remaining <= 0:
                break
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception(
            "Telegram streaming failed for message=%s start=%s end=%s",
            message.id, start, end
        )
        raise

@app.get("/")
async def root():
    return {
        "service": "STRIMA Telegram Gateway",
        "status": "online",
        "mode": "Telegram origin -> Bunny CDN",
    }

@app.get("/health")
async def health():
    if not client.is_connected():
        return JSONResponse(
            status_code=503,
            content={"ok": False, "telegram": "disconnected"},
        )
    try:
        me = await client.get_me()
        return {
            "ok": True,
            "telegram": "connected",
            "bot_id": me.id,
            "bot_username": getattr(me, "username", None),
            "channel_id": TG_CHANNEL_ID,
        }
    except Exception as exc:
        return JSONResponse(
            status_code=503,
            content={"ok": False, "telegram": type(exc).__name__},
        )

@app.api_route("/movie/{message_id}", methods=["GET", "HEAD"])
async def movie(
    request: Request,
    message_id: int,
    range_header: Optional[str] = Header(default=None, alias="Range"),
):
    message, size = await get_movie_message(message_id)
    start, end, partial = parse_range_header(range_header, size)
    headers = media_headers(message, size, start, end, partial)

    if request.method == "HEAD":
        return Response(
            status_code=206 if partial else 200,
            headers=headers,
        )

    return StreamingResponse(
        telegram_byte_stream(message, start, end),
        status_code=206 if partial else 200,
        headers=headers,
        media_type=headers["Content-Type"],
    )
