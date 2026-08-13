import asyncio
import logging
import mimetypes
import os
import re
import secrets
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional, Tuple

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from telethon import TelegramClient
from telethon.sessions import StringSession


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

log = logging.getLogger("strima-gateway")


# ============================================================
# STRIMA TELEGRAM SETTINGS
# Values come securely from Bunny Magic Container environment
# variables. No secrets are stored inside this GitHub file.
# ============================================================

TG_API_ID = int(os.environ["TG_API_ID"])
TG_API_HASH = os.environ["TG_API_HASH"]
TG_SESSION_STRING = os.environ["TG_SESSION_STRING"].strip()
TG_CHANNEL_ID = int(os.environ["TG_CHANNEL_ID"])

# Separate secret used only for private/admin discovery endpoints.
# Never expose the Telegram session string for this purpose.
STRIMA_ADMIN_KEY = os.getenv("STRIMA_ADMIN_KEY", "").strip()

PORT = int(os.getenv("PORT", "80"))

# Telegram streaming chunk size
CHUNK_SIZE = int(
    os.getenv(
        "TG_CHUNK_SIZE",
        str(512 * 1024),
    )
)

# Allow Bunny CDN to cache movie byte ranges
CACHE_SECONDS = int(
    os.getenv(
        "CACHE_SECONDS",
        "86400",
    )
)


# ============================================================
# TELEGRAM CLIENT
# ============================================================

client = TelegramClient(
    StringSession(TG_SESSION_STRING),
    TG_API_ID,
    TG_API_HASH,
    sequential_updates=False,
    connection_retries=10,
    retry_delay=2,
    auto_reconnect=True,
)


# This will contain the properly resolved Telegram channel.
#
# Important:
# A private Telegram channel cannot always be accessed reliably
# using only its numeric channel ID.
#
# Telethon also needs the channel's access hash.
#
# We therefore resolve the channel when the application starts.
CHANNEL_INPUT_ENTITY = None


# ============================================================
# APPLICATION STARTUP
# ============================================================

@asynccontextmanager
async def lifespan(app: FastAPI):

    global CHANNEL_INPUT_ENTITY

    log.info(
        "Connecting STRIMA Telegram Gateway to Telegram..."
    )

    # Connect using the dedicated Telegram USER session stored
    # securely in Bunny as TG_SESSION_STRING.
    await client.connect()

    if not await client.is_user_authorized():

        raise RuntimeError(
            "TG_SESSION_STRING is not authorized. "
            "Create a new dedicated Telegram user session."
        )

    me = await client.get_me()

    log.info(
        "Telegram connected as @%s (%s)",
        getattr(me, "username", None),
        me.id,
    )


    # --------------------------------------------------------
    # Resolve STRIMA Premium Members channel properly.
    #
    # This fixes the previous:
    #
    # Telegram lookup failed: ValueError
    #
    # Loading Telegram dialogs causes Telethon to learn the
    # access hash for channels visible to this Telegram account.
    # --------------------------------------------------------

    log.info(
        "Resolving configured Telegram channel %s...",
        TG_CHANNEL_ID,
    )

    dialogs = await client.get_dialogs()

    for dialog in dialogs:

        if dialog.id == TG_CHANNEL_ID:

            CHANNEL_INPUT_ENTITY = dialog.input_entity

            log.info(
                "Telegram channel resolved: %s (%s)",
                dialog.title,
                dialog.id,
            )

            break


    # If this happens, this Telegram account cannot currently
    # see the configured channel.
    if CHANNEL_INPUT_ENTITY is None:

        raise RuntimeError(
            f"Configured Telegram channel "
            f"{TG_CHANNEL_ID} is not visible to this Telegram account. "
            f"Confirm the account is still a member "
            f"of the channel."
        )


    try:

        yield

    finally:

        await client.disconnect()

        log.info(
            "Telegram disconnected"
        )


# ============================================================
# FASTAPI APPLICATION
# ============================================================

app = FastAPI(
    title="STRIMA Telegram Gateway",
    version="0.4.0",
    lifespan=lifespan,
)


# ============================================================
# BYTE RANGE SUPPORT
#
# Video players do not normally download the whole movie from
# the beginning.
#
# They request pieces such as:
#
# Range: bytes=0-1048575
#
# This allows seeking and progressive streaming.
# ============================================================

def parse_range_header(
    value: Optional[str],
    size: int,
) -> Tuple[int, int, bool]:

    # No Range header means return entire file.
    if not value:

        return (
            0,
            size - 1,
            False,
        )


    match = re.fullmatch(
        r"bytes=(\d*)-(\d*)",
        value.strip(),
    )


    if not match:

        raise HTTPException(
            status_code=416,
            detail=(
                "Only a single bytes=start-end "
                "range is supported"
            ),
            headers={
                "Content-Range": f"bytes */{size}"
            },
        )


    left, right = match.groups()


    if left == "" and right == "":

        raise HTTPException(
            status_code=416,
            detail="Invalid byte range",
            headers={
                "Content-Range": f"bytes */{size}"
            },
        )


    # Example:
    #
    # Range: bytes=-500
    #
    # Means final 500 bytes.
    if left == "":

        suffix = int(right)

        if suffix <= 0:

            raise HTTPException(
                status_code=416,
                detail="Invalid suffix range",
                headers={
                    "Content-Range": f"bytes */{size}"
                },
            )


        suffix = min(
            suffix,
            size,
        )

        start = size - suffix

        end = size - 1


    else:

        start = int(left)

        end = (
            int(right)
            if right
            else size - 1
        )


    if (
        start >= size
        or start < 0
        or end < start
    ):

        raise HTTPException(
            status_code=416,
            detail="Range outside file",
            headers={
                "Content-Range": f"bytes */{size}"
            },
        )


    end = min(
        end,
        size - 1,
    )


    return (
        start,
        end,
        True,
    )


# ============================================================
# FIND MOVIE INSIDE TELEGRAM
# ============================================================

async def get_movie_message(
    message_id: int
):

    if CHANNEL_INPUT_ENTITY is None:

        raise HTTPException(
            status_code=503,
            detail=(
                "Telegram channel has not been resolved"
            ),
        )


    try:

        message = await client.get_messages(
            CHANNEL_INPUT_ENTITY,
            ids=message_id,
        )


    except Exception as exc:

        log.exception(
            "Telegram message lookup failed "
            "for channel=%s message=%s",
            TG_CHANNEL_ID,
            message_id,
        )


        raise HTTPException(
            status_code=502,
            detail=(
                "Telegram lookup failed: "
                f"{type(exc).__name__}"
            ),
        )


    # Confirm that message exists and contains a file.
    if (
        not message
        or not getattr(
            message,
            "media",
            None,
        )
        or not getattr(
            message,
            "file",
            None,
        )
    ):

        raise HTTPException(
            status_code=404,
            detail=(
                "Telegram movie not found "
                "or message has no file"
            ),
        )


    size = int(
        message.file.size or 0
    )


    if size <= 0:

        raise HTTPException(
            status_code=404,
            detail=(
                "Telegram media has no "
                "downloadable file"
            ),
        )


    return (
        message,
        size,
    )


# ============================================================
# VIDEO RESPONSE HEADERS
# ============================================================

def media_headers(
    message,
    size: int,
    start: int,
    end: int,
    partial: bool,
):

    filename = (
        message.file.name
        or f"telegram-{message.id}.mp4"
    )


    mime = (
        message.file.mime_type
        or mimetypes.guess_type(filename)[0]
        or "application/octet-stream"
    )


    headers = {

        # Important for video seeking
        "Accept-Ranges": "bytes",

        # Allow Bunny CDN caching
        "Cache-Control": (
            f"public, max-age={CACHE_SECONDS}"
        ),

        # Allow FlutterFlow / web player access
        "Access-Control-Allow-Origin": "*",

        "Content-Type": mime,

        "Content-Length": str(
            end - start + 1
        ),

        # Stable cache ID for this Telegram movie
        "ETag": (
            f'"tg-{TG_CHANNEL_ID}-'
            f'{message.id}-{size}"'
        ),

        "X-STRIMA-Telegram-Message": str(
            message.id
        ),
    }


    if partial:

        headers["Content-Range"] = (
            f"bytes {start}-{end}/{size}"
        )


    return headers


# ============================================================
# STREAM TELEGRAM FILE WITHOUT SAVING WHOLE MOVIE
# ============================================================

async def telegram_byte_stream(
    message,
    start: int,
    end: int,
) -> AsyncIterator[bytes]:

    remaining = (
        end - start + 1
    )


    try:

        iterator = client.iter_download(

            message.media,

            offset=start,

            request_size=CHUNK_SIZE,

            chunk_size=CHUNK_SIZE,

            file_size=int(
                message.file.size or 0
            ),
        )


        async for chunk in iterator:

            if remaining <= 0:

                break


            data = bytes(chunk)


            # Do not send more than browser requested.
            if len(data) > remaining:

                data = data[:remaining]


            remaining -= len(data)


            if data:

                yield data


            if remaining <= 0:

                break


    except asyncio.CancelledError:

        # Browser stopped playback or changed seek location.
        raise


    except Exception:

        log.exception(
            "Telegram streaming failed "
            "for message=%s start=%s end=%s",
            message.id,
            start,
            end,
        )

        raise


# ============================================================
# ROOT ENDPOINT
# ============================================================

@app.get("/")
async def root():

    return {

        "service":
            "STRIMA Telegram Gateway",

        "status":
            "online",

        "mode":
            "Telegram origin -> Bunny CDN",

        "version":
            "0.4.0",
    }


# ============================================================
# HEALTH CHECK
# ============================================================

@app.get("/health")
async def health():

    if not client.is_connected():

        return JSONResponse(
            status_code=503,
            content={
                "ok": False,
                "telegram": "disconnected",
            },
        )


    try:

        me = await client.get_me()


        return {

            "ok": True,

            "telegram":
                "connected",

            "telegram_user_id":
                me.id,

            "telegram_username":
                getattr(
                    me,
                    "username",
                    None,
                ),

            "channel_id":
                TG_CHANNEL_ID,

            "channel_resolved":
                CHANNEL_INPUT_ENTITY
                is not None,
        }


    except Exception as exc:

        return JSONResponse(
            status_code=503,
            content={
                "ok": False,
                "telegram":
                    type(exc).__name__,
            },
        )


# ============================================================
# PRIVATE TELEGRAM CHANNEL DISCOVERY
#
# This endpoint is intentionally protected by a separate admin
# key. It lists channels visible to the authenticated Telegram
# USER session so we can safely choose an old source channel.
#
# Header required:
# X-STRIMA-Admin-Key: <STRIMA_ADMIN_KEY>
# ============================================================

@app.get("/admin/telegram/channels")
async def list_telegram_channels(
    admin_key: Optional[str] = Header(
        default=None,
        alias="X-STRIMA-Admin-Key",
    ),
):

    if not STRIMA_ADMIN_KEY:
        raise HTTPException(
            status_code=503,
            detail=(
                "STRIMA_ADMIN_KEY is not configured on the server"
            ),
        )

    if (
        not admin_key
        or not secrets.compare_digest(
            admin_key,
            STRIMA_ADMIN_KEY,
        )
    ):
        raise HTTPException(
            status_code=401,
            detail="Invalid admin key",
        )

    if not client.is_connected():
        raise HTTPException(
            status_code=503,
            detail="Telegram client is disconnected",
        )

    try:
        dialogs = await client.get_dialogs()

        channels = []

        for dialog in dialogs:
            if not getattr(dialog, "is_channel", False):
                continue

            entity = getattr(dialog, "entity", None)

            channels.append(
                {
                    "id": dialog.id,
                    "title": dialog.title,
                    "username": getattr(
                        entity,
                        "username",
                        None,
                    ),
                    "is_group": bool(
                        getattr(dialog, "is_group", False)
                    ),
                    "is_channel": True,
                    "is_configured_destination": (
                        dialog.id == TG_CHANNEL_ID
                    ),
                }
            )

        channels.sort(
            key=lambda item: (
                item.get("title") or ""
            ).lower()
        )

        return {
            "ok": True,
            "count": len(channels),
            "configured_destination_channel_id": TG_CHANNEL_ID,
            "channels": channels,
        }

    except HTTPException:
        raise

    except Exception as exc:
        log.exception(
            "Failed to list Telegram channels"
        )
        raise HTTPException(
            status_code=502,
            detail=(
                "Telegram channel discovery failed: "
                f"{type(exc).__name__}"
            ),
        )


# ============================================================
# MOVIE STREAMING ENDPOINT
#
# Example:
#
# https://YOUR-BUNNY-ENDPOINT/movie/468
#
# This reads Telegram message 468.
#
# It DOES NOT permanently store the movie inside the container.
# ============================================================

@app.api_route(
    "/movie/{message_id}",
    methods=[
        "GET",
        "HEAD",
    ],
)
async def movie(
    request: Request,
    message_id: int,
    range_header: Optional[str] = Header(
        default=None,
        alias="Range",
    ),
):

    # Find Telegram movie.
    message, size = (
        await get_movie_message(
            message_id
        )
    )


    # Work out which part of movie browser requested.
    start, end, partial = (
        parse_range_header(
            range_header,
            size,
        )
    )


    headers = media_headers(
        message,
        size,
        start,
        end,
        partial,
    )


    # HEAD request:
    # return information only, not movie bytes.
    if request.method == "HEAD":

        return Response(

            status_code=(
                206
                if partial
                else 200
            ),

            headers=headers,
        )


    # Stream requested movie bytes from Telegram.
    return StreamingResponse(

        telegram_byte_stream(
            message,
            start,
            end,
        ),

        status_code=(
            206
            if partial
            else 200
        ),

        headers=headers,

        media_type=headers[
            "Content-Type"
        ],
    )
