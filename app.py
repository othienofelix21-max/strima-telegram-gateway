import asyncio
import logging
import mimetypes
import os
import re
import secrets
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional, Tuple

from fastapi import FastAPI, Header, HTTPException, Query, Request, Response
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
# ============================================================
#
# All sensitive settings come from Bunny Magic Container
# environment variables.
#
# Nothing sensitive should be hard-coded into GitHub.
# ============================================================

TG_API_ID = int(
    os.environ["TG_API_ID"]
)

TG_API_HASH = os.environ[
    "TG_API_HASH"
]

TG_SESSION_STRING = os.environ[
    "TG_SESSION_STRING"
].strip()


# ------------------------------------------------------------
# DESTINATION CHANNEL
#
# This is STRIMA Premium Members.
#
# Movies that eventually get imported will be copied here.
# Our existing /movie/{message_id} player also reads movies
# from this channel.
# ------------------------------------------------------------

TG_CHANNEL_ID = int(
    os.environ["TG_CHANNEL_ID"]
)


# ------------------------------------------------------------
# SOURCE CHANNEL
#
# This is the OLD movie channel we are scanning.
#
# Currently:
#
# VVIP LATEST MOVES
# -1001884106307
#
# The real value comes from Bunny environment variables.
# ------------------------------------------------------------

TG_SOURCE_CHANNEL_ID = int(
    os.environ["TG_SOURCE_CHANNEL_ID"]
)


# ------------------------------------------------------------
# PRIVATE ADMIN KEY
#
# Used only for private STRIMA administration endpoints.
#
# NEVER use TG_SESSION_STRING as an admin password.
# ------------------------------------------------------------

STRIMA_ADMIN_KEY = os.getenv(
    "STRIMA_ADMIN_KEY",
    "",
).strip()


PORT = int(
    os.getenv(
        "PORT",
        "80",
    )
)


# Telegram streaming chunk size.
CHUNK_SIZE = int(
    os.getenv(
        "TG_CHUNK_SIZE",
        str(
            512 * 1024
        ),
    )
)


# Bunny cache duration.
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
    StringSession(
        TG_SESSION_STRING
    ),
    TG_API_ID,
    TG_API_HASH,
    sequential_updates=False,
    connection_retries=10,
    retry_delay=2,
    auto_reconnect=True,
)


# ============================================================
# RESOLVED TELEGRAM CHANNELS
# ============================================================

# STRIMA Premium Members
CHANNEL_INPUT_ENTITY = None

# VVIP LATEST MOVES
SOURCE_INPUT_ENTITY = None

SOURCE_CHANNEL_TITLE = None


# ============================================================
# APPLICATION STARTUP
# ============================================================

@asynccontextmanager
async def lifespan(
    app: FastAPI
):

    global CHANNEL_INPUT_ENTITY
    global SOURCE_INPUT_ENTITY
    global SOURCE_CHANNEL_TITLE

    log.info(
        "Connecting STRIMA Telegram Gateway to Telegram..."
    )

    await client.connect()


    if not await client.is_user_authorized():

        raise RuntimeError(
            "TG_SESSION_STRING is not authorized. "
            "Create a new dedicated Telegram user session."
        )


    me = await client.get_me()


    log.info(
        "Telegram connected as @%s (%s)",
        getattr(
            me,
            "username",
            None,
        ),
        me.id,
    )


    # --------------------------------------------------------
    # Load dialogs.
    #
    # This allows Telethon to learn channel access hashes and
    # lets us resolve both our source and destination channels.
    # --------------------------------------------------------

    dialogs = await client.get_dialogs()


    for dialog in dialogs:

        # STRIMA Premium Members destination.
        if dialog.id == TG_CHANNEL_ID:

            CHANNEL_INPUT_ENTITY = (
                dialog.input_entity
            )

            log.info(
                "Telegram destination resolved: %s (%s)",
                dialog.title,
                dialog.id,
            )


        # Old VVIP movie source.
        if (
            dialog.id
            == TG_SOURCE_CHANNEL_ID
        ):

            SOURCE_INPUT_ENTITY = (
                dialog.input_entity
            )

            SOURCE_CHANNEL_TITLE = (
                dialog.title
            )

            log.info(
                "Telegram source resolved: %s (%s)",
                dialog.title,
                dialog.id,
            )


    # --------------------------------------------------------
    # Destination must always work.
    #
    # Existing movie playback depends on this.
    # --------------------------------------------------------

    if CHANNEL_INPUT_ENTITY is None:

        raise RuntimeError(
            f"Configured Telegram destination channel "
            f"{TG_CHANNEL_ID} is not visible to "
            f"this Telegram account."
        )


    # --------------------------------------------------------
    # Source channel failure should NOT break existing movie
    # playback.
    #
    # We only disable source preview if it cannot be resolved.
    # --------------------------------------------------------

    if SOURCE_INPUT_ENTITY is None:

        log.warning(
            "Configured Telegram source channel %s "
            "is not visible. Streaming will still work, "
            "but source preview will be unavailable.",
            TG_SOURCE_CHANNEL_ID,
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
    version="0.5.0",
    lifespan=lifespan,
)


# ============================================================
# ADMIN AUTHENTICATION
# ============================================================

def require_admin_key(
    admin_key: Optional[str]
) -> None:

    if not STRIMA_ADMIN_KEY:

        raise HTTPException(
            status_code=503,
            detail=(
                "STRIMA_ADMIN_KEY is not "
                "configured on the server"
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


# ============================================================
# BYTE RANGE SUPPORT
# ============================================================

def parse_range_header(
    value: Optional[str],
    size: int,
) -> Tuple[
    int,
    int,
    bool,
]:

    # Browser requested entire movie.
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
                "Content-Range":
                    f"bytes */{size}"
            },
        )


    left, right = (
        match.groups()
    )


    if (
        left == ""
        and right == ""
    ):

        raise HTTPException(
            status_code=416,
            detail="Invalid byte range",
            headers={
                "Content-Range":
                    f"bytes */{size}"
            },
        )


    # --------------------------------------------------------
    # Suffix range
    #
    # Example:
    #
    # Range: bytes=-500
    # --------------------------------------------------------

    if left == "":

        suffix = int(
            right
        )


        if suffix <= 0:

            raise HTTPException(
                status_code=416,
                detail=(
                    "Invalid suffix range"
                ),
                headers={
                    "Content-Range":
                        f"bytes */{size}"
                },
            )


        suffix = min(
            suffix,
            size,
        )


        start = (
            size - suffix
        )

        end = (
            size - 1
        )


    else:

        start = int(
            left
        )


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
                "Content-Range":
                    f"bytes */{size}"
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
# GET MOVIE FROM STRIMA PREMIUM CHANNEL
# ============================================================

async def get_movie_message(
    message_id: int
):

    if CHANNEL_INPUT_ENTITY is None:

        raise HTTPException(
            status_code=503,
            detail=(
                "Telegram destination channel "
                "has not been resolved"
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
        message.file.size
        or 0
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
        or
        f"telegram-{message.id}.mp4"
    )


    mime = (
        message.file.mime_type
        or mimetypes.guess_type(
            filename
        )[0]
        or "application/octet-stream"
    )


    headers = {

        "Accept-Ranges":
            "bytes",

        "Cache-Control":
            f"public, max-age={CACHE_SECONDS}",

        "Access-Control-Allow-Origin":
            "*",

        "Content-Type":
            mime,

        "Content-Length":
            str(
                end - start + 1
            ),

        "ETag":
            (
                f'"tg-{TG_CHANNEL_ID}-'
                f'{message.id}-{size}"'
            ),

        "X-STRIMA-Telegram-Message":
            str(
                message.id
            ),
    }


    if partial:

        headers[
            "Content-Range"
        ] = (
            f"bytes "
            f"{start}-{end}/{size}"
        )


    return headers


# ============================================================
# STREAM TELEGRAM FILE
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

        iterator = (
            client.iter_download(
                message.media,
                offset=start,
                request_size=(
                    CHUNK_SIZE
                ),
                chunk_size=(
                    CHUNK_SIZE
                ),
                file_size=int(
                    message.file.size
                    or 0
                ),
            )
        )


        async for chunk in iterator:

            if remaining <= 0:

                break


            data = bytes(
                chunk
            )


            if (
                len(data)
                > remaining
            ):

                data = data[
                    :remaining
                ]


            remaining -= len(
                data
            )


            if data:

                yield data


            if remaining <= 0:

                break


    except asyncio.CancelledError:

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
# MOVIE METADATA HELPERS
# ============================================================

def format_duration(
    seconds
) -> Optional[str]:

    if seconds is None:

        return None


    try:

        total = int(
            seconds
        )

    except (
        TypeError,
        ValueError,
    ):

        return None


    if total <= 0:

        return None


    hours, remainder = divmod(
        total,
        3600,
    )


    minutes, secs = divmod(
        remainder,
        60,
    )


    if hours:

        return (
            f"{hours}hr "
            f"{minutes}m"
        )


    if minutes:

        return (
            f"{minutes}m "
            f"{secs}s"
        )


    return (
        f"{secs}s"
    )


# ------------------------------------------------------------
# Detect movie release year from filename/caption.
# ------------------------------------------------------------

def detect_year(
    text: str
) -> Optional[int]:

    matches = re.findall(
        r"(?<!\d)"
        r"(19\d{2}|20\d{2})"
        r"(?!\d)",
        text or "",
    )


    if not matches:

        return None


    year = int(
        matches[-1]
    )


    if (
        1900
        <= year
        <= 2100
    ):

        return year


    return None


# ------------------------------------------------------------
# Try to detect a VJ name.
#
# Examples:
#
# VJ ULIO
# VJ Junior
# VJ_JINGO
# ------------------------------------------------------------

def detect_vj(
    text: str
) -> Optional[str]:

    if not text:

        return None


    match = re.search(
        r"\bVJ[\s._-]*"
        r"([A-Za-z]"
        r"[A-Za-z0-9'-]{1,30})"
        r"\b",
        text,
        flags=re.IGNORECASE,
    )


    if not match:

        return None


    return (
        f"VJ "
        f"{match.group(1).strip()}"
    )


# ------------------------------------------------------------
# Clean filename into a temporary movie-title candidate.
#
# This is only a first guess.
#
# Later TMDB/metadata enrichment will improve it.
# ------------------------------------------------------------

def clean_detected_title(
    filename: str,
    caption: str,
) -> Optional[str]:

    raw = (
        filename
        or (
            caption.splitlines()[0]
            if caption
            else ""
        )
    )


    if not raw:

        return None


    # Remove common video extension.
    raw = re.sub(
        r"\."
        r"(mp4|mkv|mov|avi|webm|m4v)"
        r"$",
        "",
        raw,
        flags=re.IGNORECASE,
    )


    raw = (
        raw
        .replace(
            "_",
            " ",
        )
        .replace(
            ".",
            " ",
        )
    )


    raw = re.sub(
        r"\s+",
        " ",
        raw,
    ).strip(
        " -_"
    )


    return (
        raw
        or None
    )


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
            "0.5.0",
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
                "ok":
                    False,

                "telegram":
                    "disconnected",
            },
        )


    try:

        me = await client.get_me()


        return {

            "ok":
                True,

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
                (
                    CHANNEL_INPUT_ENTITY
                    is not None
                ),

            "source_channel_id":
                TG_SOURCE_CHANNEL_ID,

            "source_channel_resolved":
                (
                    SOURCE_INPUT_ENTITY
                    is not None
                ),

            "source_channel_title":
                SOURCE_CHANNEL_TITLE,
        }


    except Exception as exc:

        return JSONResponse(
            status_code=503,
            content={
                "ok":
                    False,

                "telegram":
                    type(
                        exc
                    ).__name__,
            },
        )


# ============================================================
# PRIVATE TELEGRAM CHANNEL DISCOVERY
# ============================================================

@app.get(
    "/admin/telegram/channels"
)
async def list_telegram_channels(
    admin_key: Optional[str] = Header(
        default=None,
        alias="X-STRIMA-Admin-Key",
    ),
):

    require_admin_key(
        admin_key
    )


    if not client.is_connected():

        raise HTTPException(
            status_code=503,
            detail=(
                "Telegram client "
                "is disconnected"
            ),
        )


    try:

        dialogs = (
            await client.get_dialogs()
        )


        channels = []


        for dialog in dialogs:

            if not getattr(
                dialog,
                "is_channel",
                False,
            ):

                continue


            entity = getattr(
                dialog,
                "entity",
                None,
            )


            channels.append(
                {

                    "id":
                        dialog.id,

                    "title":
                        dialog.title,

                    "username":
                        getattr(
                            entity,
                            "username",
                            None,
                        ),

                    "is_group":
                        bool(
                            getattr(
                                dialog,
                                "is_group",
                                False,
                            )
                        ),

                    "is_channel":
                        True,

                    "is_configured_destination":
                        (
                            dialog.id
                            == TG_CHANNEL_ID
                        ),

                    "is_configured_source":
                        (
                            dialog.id
                            == TG_SOURCE_CHANNEL_ID
                        ),
                }
            )


        channels.sort(
            key=lambda item: (
                item.get(
                    "title"
                )
                or ""
            ).lower()
        )


        return {

            "ok":
                True,

            "count":
                len(
                    channels
                ),

            "configured_destination_channel_id":
                TG_CHANNEL_ID,

            "configured_source_channel_id":
                TG_SOURCE_CHANNEL_ID,

            "channels":
                channels,
        }


    except HTTPException:

        raise


    except Exception as exc:

        log.exception(
            "Failed to list "
            "Telegram channels"
        )


        raise HTTPException(
            status_code=502,
            detail=(
                "Telegram channel "
                "discovery failed: "
                f"{type(exc).__name__}"
            ),
        )


# ============================================================
# READ-ONLY OLD CHANNEL PREVIEW
# ============================================================
#
# IMPORTANT:
#
# This endpoint DOES NOT:
#
# - forward movies
# - copy movies
# - delete Telegram messages
# - edit Telegram messages
# - download entire movies
# - create Supabase rows
#
# It reads Telegram metadata only.
#
# Example:
#
# /admin/telegram/source-preview?limit=20
# ============================================================

@app.get(
    "/admin/telegram/source-preview"
)
async def source_preview(

    limit: int = Query(
        default=20,
        ge=1,
        le=100,
    ),

    admin_key: Optional[str] = Header(
        default=None,
        alias="X-STRIMA-Admin-Key",
    ),
):

    require_admin_key(
        admin_key
    )


    if not client.is_connected():

        raise HTTPException(
            status_code=503,
            detail=(
                "Telegram client "
                "is disconnected"
            ),
        )


    if SOURCE_INPUT_ENTITY is None:

        raise HTTPException(
            status_code=503,
            detail=(
                f"Telegram source channel "
                f"{TG_SOURCE_CHANNEL_ID} "
                f"has not been resolved"
            ),
        )


    movies = []

    scanned = 0


    # --------------------------------------------------------
    # Scan more Telegram messages than the movie limit.
    #
    # This is necessary because an old movie channel can have
    # posters, text posts, links, APK files and other items
    # between actual movies.
    # --------------------------------------------------------

    scan_limit = min(
        max(
            limit * 8,
            50,
        ),
        500,
    )


    try:

        async for message in (
            client.iter_messages(
                SOURCE_INPUT_ENTITY,
                limit=scan_limit,
            )
        ):

            scanned += 1


            file_obj = getattr(
                message,
                "file",
                None,
            )


            if not file_obj:

                continue


            filename = (
                getattr(
                    file_obj,
                    "name",
                    None,
                )
                or ""
            )


            mime_type = (
                getattr(
                    file_obj,
                    "mime_type",
                    None,
                )
                or ""
            )


            extension = (
                os.path.splitext(
                    filename.lower()
                )[1]
            )


            # ------------------------------------------------
            # Identify likely movie/video files.
            # ------------------------------------------------

            is_video = (

                mime_type.startswith(
                    "video/"
                )

                or extension
                in {
                    ".mp4",
                    ".mkv",
                    ".mov",
                    ".avi",
                    ".webm",
                    ".m4v",
                }

                or bool(
                    getattr(
                        message,
                        "video",
                        None,
                    )
                )
            )


            if not is_video:

                continue


            caption = (
                getattr(
                    message,
                    "message",
                    None,
                )
                or ""
            ).strip()


            combined_text = (
                f"{filename}\n"
                f"{caption}"
            )


            duration = getattr(
                file_obj,
                "duration",
                None,
            )


            width = getattr(
                file_obj,
                "width",
                None,
            )


            height = getattr(
                file_obj,
                "height",
                None,
            )


            size_bytes = int(
                getattr(
                    file_obj,
                    "size",
                    None,
                )
                or 0
            )


            # ------------------------------------------------
            # Build movie preview information.
            # ------------------------------------------------

            movies.append(
                {

                    "source_message_id":
                        message.id,

                    "date":
                        (
                            message.date.isoformat()
                            if getattr(
                                message,
                                "date",
                                None,
                            )
                            else None
                        ),

                    "file_name":
                        (
                            filename
                            or None
                        ),

                    "mime_type":
                        (
                            mime_type
                            or None
                        ),

                    "file_size_bytes":
                        size_bytes,

                    "file_size_mb":
                        (
                            round(
                                size_bytes
                                / (
                                    1024
                                    * 1024
                                ),
                                2,
                            )
                            if size_bytes
                            else None
                        ),

                    "duration_seconds":
                        (
                            int(
                                duration
                            )
                            if duration
                            else None
                        ),

                    "duration_text":
                        format_duration(
                            duration
                        ),

                    "width":
                        (
                            int(
                                width
                            )
                            if width
                            else None
                        ),

                    "height":
                        (
                            int(
                                height
                            )
                            if height
                            else None
                        ),

                    "caption":
                        (
                            caption[:500]
                            if caption
                            else None
                        ),

                    "detected_title":
                        clean_detected_title(
                            filename,
                            caption,
                        ),

                    "detected_year":
                        detect_year(
                            combined_text
                        ),

                    "detected_vj_name":
                        detect_vj(
                            combined_text
                        ),
                }
            )


            if (
                len(movies)
                >= limit
            ):

                break


        return {

            "ok":
                True,

            "mode":
                "read_only_preview",

            "source_channel": {

                "id":
                    TG_SOURCE_CHANNEL_ID,

                "title":
                    SOURCE_CHANNEL_TITLE,
            },

            "destination_channel_id":
                TG_CHANNEL_ID,

            "scanned_messages":
                scanned,

            "movie_candidates":
                len(
                    movies
                ),

            "limit":
                limit,

            "movies":
                movies,
        }


    except HTTPException:

        raise


    except Exception as exc:

        log.exception(
            "Telegram source preview "
            "failed for channel=%s",
            TG_SOURCE_CHANNEL_ID,
        )


        raise HTTPException(
            status_code=502,
            detail=(
                "Telegram source preview "
                "failed: "
                f"{type(exc).__name__}"
            ),
        )


# ============================================================
# EXISTING STRIMA MOVIE STREAMING ENDPOINT
# ============================================================
#
# Example:
#
# https://mc-p2ku5nz4qw.bunny.run/movie/455
#
# This continues to use STRIMA Premium Members.
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

    message, size = (
        await get_movie_message(
            message_id
        )
    )


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


    # HEAD request.
    if request.method == "HEAD":

        return Response(

            status_code=(
                206
                if partial
                else 200
            ),

            headers=headers,
        )


    # GET request.
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
