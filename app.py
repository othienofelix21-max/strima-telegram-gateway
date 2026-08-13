import asyncio
import json
import logging
import mimetypes
import os
import re
import secrets
import urllib.error
import urllib.request
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

TG_API_ID = int(os.environ["TG_API_ID"])
TG_API_HASH = os.environ["TG_API_HASH"]
TG_SESSION_STRING = os.environ["TG_SESSION_STRING"].strip()
TG_CHANNEL_ID = int(os.environ["TG_CHANNEL_ID"])
TG_SOURCE_CHANNEL_ID = int(os.environ["TG_SOURCE_CHANNEL_ID"])
STRIMA_ADMIN_KEY = os.getenv("STRIMA_ADMIN_KEY", "").strip()
SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_PUBLISHABLE_KEY = os.getenv("SUPABASE_PUBLISHABLE_KEY", "").strip()
PORT = int(os.getenv("PORT", "80"))
CHUNK_SIZE = int(os.getenv("TG_CHUNK_SIZE", str(512 * 1024)))
CACHE_SECONDS = int(os.getenv("CACHE_SECONDS", "86400"))

client = TelegramClient(
    StringSession(TG_SESSION_STRING),
    TG_API_ID,
    TG_API_HASH,
    sequential_updates=False,
    connection_retries=10,
    retry_delay=2,
    auto_reconnect=True,
)

CHANNEL_INPUT_ENTITY = None
SOURCE_INPUT_ENTITY = None
SOURCE_CHANNEL_TITLE = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global CHANNEL_INPUT_ENTITY, SOURCE_INPUT_ENTITY, SOURCE_CHANNEL_TITLE
    log.info("Connecting STRIMA Telegram Gateway to Telegram...")
    await client.connect()
    if not await client.is_user_authorized():
        raise RuntimeError("TG_SESSION_STRING is not authorized.")
    me = await client.get_me()
    log.info("Telegram connected as @%s (%s)", getattr(me, "username", None), me.id)
    dialogs = await client.get_dialogs()
    for dialog in dialogs:
        if dialog.id == TG_CHANNEL_ID:
            CHANNEL_INPUT_ENTITY = dialog.input_entity
            log.info("Telegram destination resolved: %s (%s)", dialog.title, dialog.id)
        if dialog.id == TG_SOURCE_CHANNEL_ID:
            SOURCE_INPUT_ENTITY = dialog.input_entity
            SOURCE_CHANNEL_TITLE = dialog.title
            log.info("Telegram source resolved: %s (%s)", dialog.title, dialog.id)
    if CHANNEL_INPUT_ENTITY is None:
        raise RuntimeError(f"Configured Telegram destination {TG_CHANNEL_ID} is not visible.")
    if SOURCE_INPUT_ENTITY is None:
        log.warning("Configured Telegram source %s is not visible. Movie playback will still work.", TG_SOURCE_CHANNEL_ID)
    try:
        yield
    finally:
        await client.disconnect()
        log.info("Telegram disconnected")

app = FastAPI(title="STRIMA Telegram Gateway", version="0.6.0", lifespan=lifespan)

def require_admin_key(admin_key: Optional[str]) -> None:
    if not STRIMA_ADMIN_KEY:
        raise HTTPException(status_code=503, detail="STRIMA_ADMIN_KEY is not configured")
    if not admin_key or not secrets.compare_digest(admin_key, STRIMA_ADMIN_KEY):
        raise HTTPException(status_code=401, detail="Invalid admin key")

def parse_range_header(value: Optional[str], size: int) -> Tuple[int, int, bool]:
    if not value:
        return 0, size - 1, False
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", value.strip())
    if not match:
        raise HTTPException(status_code=416, detail="Only one byte range is supported", headers={"Content-Range": f"bytes */{size}"})
    left, right = match.groups()
    if left == "" and right == "":
        raise HTTPException(status_code=416, detail="Invalid byte range", headers={"Content-Range": f"bytes */{size}"})
    if left == "":
        suffix = int(right)
        if suffix <= 0:
            raise HTTPException(status_code=416, detail="Invalid suffix range", headers={"Content-Range": f"bytes */{size}"})
        suffix = min(suffix, size)
        start = size - suffix
        end = size - 1
    else:
        start = int(left)
        end = int(right) if right else size - 1
    if start >= size or start < 0 or end < start:
        raise HTTPException(status_code=416, detail="Range outside file", headers={"Content-Range": f"bytes */{size}"})
    end = min(end, size - 1)
    return start, end, True

async def get_movie_message(message_id: int):
    if CHANNEL_INPUT_ENTITY is None:
        raise HTTPException(status_code=503, detail="Telegram destination channel is not resolved")
    try:
        message = await client.get_messages(CHANNEL_INPUT_ENTITY, ids=message_id)
    except Exception as exc:
        log.exception("Telegram lookup failed for channel=%s message=%s", TG_CHANNEL_ID, message_id)
        raise HTTPException(status_code=502, detail=f"Telegram lookup failed: {type(exc).__name__}")
    if not message or not getattr(message, "media", None) or not getattr(message, "file", None):
        raise HTTPException(status_code=404, detail="Telegram movie not found or message has no file")
    size = int(message.file.size or 0)
    if size <= 0:
        raise HTTPException(status_code=404, detail="Telegram media has no downloadable file")
    return message, size

def media_headers(message, size: int, start: int, end: int, partial: bool):
    filename = message.file.name or f"telegram-{message.id}.mp4"
    mime = message.file.mime_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
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
        log.exception("Telegram streaming failed for message=%s start=%s end=%s", message.id, start, end)
        raise

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v"}

def format_duration(seconds) -> Optional[str]:
    if seconds is None:
        return None
    try:
        total = int(seconds)
    except (TypeError, ValueError):
        return None
    if total <= 0:
        return None
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}hr {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"

def detect_year(text: str) -> Optional[int]:
    matches = re.findall(r"(?<!\d)(19\d{2}|20\d{2})(?!\d)", text or "")
    if not matches:
        return None
    year = int(matches[-1])
    return year if 1900 <= year <= 2100 else None

def detect_vj(text: str) -> Optional[str]:
    if not text:
        return None
    match = re.search(r"\bVJ[\s._-]*([A-Za-z][A-Za-z0-9'-]{1,30})\b", text, flags=re.IGNORECASE)
    if not match:
        return None
    return f"VJ {match.group(1).strip()}"

def clean_detected_title(filename: str, caption: str) -> Optional[str]:
    raw = filename or (caption.splitlines()[0] if caption else "")
    if not raw:
        return None
    raw = re.sub(r"\.(mp4|mkv|mov|avi|webm|m4v)$", "", raw, flags=re.IGNORECASE)
    raw = raw.replace("_", " ").replace(".", " ")
    raw = re.sub(r"\s+", " ", raw).strip(" -_")
    return raw or None

def normalize_title_for_compare(title: str) -> str:
    text = (title or "").lower()
    text = re.sub(r"\.(mp4|mkv|mov|avi|webm|m4v)$", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\(\s*no'?s\s*\)", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\b(360p|480p|540p|720p|1080p|1440p|2160p|4k|uhd|hdr|x264|x265|h264|h265|hevc)\b", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\b(19\d{2}|20\d{2})\b", " ", text)
    text = re.sub(r"\bvj[\s._-]+[a-z0-9'-]+\b", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()

def detect_content_structure(title: str):
    text = title or ""
    match = re.search(r"\bS(?:EASON)?\s*(\d{1,2})\s*E(?:P(?:ISODE)?)?\s*(\d{1,3})\b", text, flags=re.IGNORECASE)
    if match:
        return {"content_kind": "episode", "season_number": int(match.group(1)), "episode_number": int(match.group(2)), "part_number": None}
    match = re.search(r"\b(?:EP|EPISODE)\s*(\d{1,3})\b", text, flags=re.IGNORECASE)
    if match:
        return {"content_kind": "episode", "season_number": None, "episode_number": int(match.group(1)), "part_number": None}
    match = re.search(r"\b(?:PART|PT)\s*(\d{1,3})\b", text, flags=re.IGNORECASE)
    if match:
        return {"content_kind": "multipart_movie", "season_number": None, "episode_number": None, "part_number": int(match.group(1))}
    return {"content_kind": "movie", "season_number": None, "episode_number": None, "part_number": None}

def nearly_same_size(a: Optional[int], b: Optional[int]) -> bool:
    if not a or not b:
        return False
    return abs(a - b) <= max(5 * 1024 * 1024, int(a * 0.01))

def nearly_same_duration(a: Optional[int], b: Optional[int]) -> bool:
    if not a or not b:
        return True
    return abs(a - b) <= 10

def source_preference_score(item: dict) -> int:
    score = 0
    filename = str(item.get("file_name") or "").lower()
    mime = str(item.get("mime_type") or "").lower()
    if mime == "video/mp4" or filename.endswith(".mp4"):
        score += 40
    if item.get("duration_seconds"):
        score += 20
    if item.get("width") and item.get("height"):
        score += 10
    if item.get("caption"):
        score += 2
    return score

def is_video_file(file_obj, message) -> bool:
    if not file_obj:
        return False
    filename = getattr(file_obj, "name", None) or ""
    mime_type = getattr(file_obj, "mime_type", None) or ""
    extension = os.path.splitext(filename.lower())[1]
    return mime_type.startswith("video/") or extension in VIDEO_EXTENSIONS or bool(getattr(message, "video", None))

def source_item_from_message(message) -> dict:
    file_obj = message.file
    filename = getattr(file_obj, "name", None) or ""
    mime_type = getattr(file_obj, "mime_type", None) or ""
    caption = (getattr(message, "message", None) or "").strip()
    combined_text = f"{filename}\n{caption}"
    duration = getattr(file_obj, "duration", None)
    width = getattr(file_obj, "width", None)
    height = getattr(file_obj, "height", None)
    size_bytes = int(getattr(file_obj, "size", None) or 0)
    detected_title = clean_detected_title(filename, caption)
    structure = detect_content_structure(detected_title or "")
    return {
        "source_message_id": message.id,
        "date": message.date.isoformat() if getattr(message, "date", None) else None,
        "file_name": filename or None,
        "mime_type": mime_type or None,
        "file_size_bytes": size_bytes,
        "file_size_mb": round(size_bytes / (1024 * 1024), 2) if size_bytes else None,
        "duration_seconds": int(duration) if duration else None,
        "duration_text": format_duration(duration),
        "width": int(width) if width else None,
        "height": int(height) if height else None,
        "caption": caption[:500] if caption else None,
        "detected_title": detected_title,
        "detected_year": detect_year(combined_text),
        "detected_vj_name": detect_vj(combined_text),
        "normalized_title": normalize_title_for_compare(detected_title or filename),
        **structure,
    }

async def collect_source_movies(limit: int) -> tuple[list[dict], int]:
    if SOURCE_INPUT_ENTITY is None:
        raise HTTPException(status_code=503, detail=f"Telegram source channel {TG_SOURCE_CHANNEL_ID} has not been resolved")
    movies = []
    scanned = 0
    scan_limit = min(max(limit * 8, 50), 500)
    async for message in client.iter_messages(SOURCE_INPUT_ENTITY, limit=scan_limit):
        scanned += 1
        file_obj = getattr(message, "file", None)
        if not is_video_file(file_obj, message):
            continue
        movies.append(source_item_from_message(message))
        if len(movies) >= limit:
            break
    return movies, scanned

def _supabase_duplicate_rpc_sync(item: dict):
    if not SUPABASE_URL or not SUPABASE_PUBLISHABLE_KEY:
        raise RuntimeError("SUPABASE_URL or SUPABASE_PUBLISHABLE_KEY is missing")
    url = f"{SUPABASE_URL}/rest/v1/rpc/strima_gateway_duplicate_check"
    payload = {
        "p_title": item.get("detected_title") or item.get("file_name") or "",
        "p_year": item.get("detected_year"),
        "p_file_size_bytes": item.get("file_size_bytes"),
        "p_duration_seconds": item.get("duration_seconds"),
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "apikey": SUPABASE_PUBLISHABLE_KEY,
            "Authorization": f"Bearer {SUPABASE_PUBLISHABLE_KEY}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            body = response.read().decode("utf-8")
            return json.loads(body) if body else []
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Supabase RPC HTTP {exc.code}: {body[:300]}")
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Supabase RPC connection failed: {exc.reason}")

async def supabase_duplicate_rpc(item: dict):
    return await asyncio.to_thread(_supabase_duplicate_rpc_sync, item)

def classify_source_duplicates(items: list[dict]) -> dict[int, dict]:
    decisions: dict[int, dict] = {}
    for item in items:
        message_id = int(item["source_message_id"])
        siblings = [
            other for other in items
            if int(other["source_message_id"]) != message_id
            and other.get("normalized_title")
            and other.get("normalized_title") == item.get("normalized_title")
            and (
                item.get("detected_year") is None
                or other.get("detected_year") is None
                or item.get("detected_year") == other.get("detected_year")
            )
        ]
        strong = [
            other for other in siblings
            if nearly_same_size(item.get("file_size_bytes"), other.get("file_size_bytes"))
            and nearly_same_duration(item.get("duration_seconds"), other.get("duration_seconds"))
        ]
        if strong:
            group = [item, *strong]
            group.sort(
                key=lambda x: (source_preference_score(x), int(x.get("source_message_id") or 0)),
                reverse=True,
            )
            preferred = group[0]
            if int(preferred["source_message_id"]) != message_id:
                decisions[message_id] = {
                    "decision": "SOURCE_DUPLICATE",
                    "confidence": 0.99,
                    "review_required": False,
                    "reason": f"Same cleaned title/year and nearly identical file size/duration. Preferred source message: {preferred['source_message_id']}.",
                    "preferred_source_message_id": int(preferred["source_message_id"]),
                }
                continue
        if siblings and not strong:
            decisions[message_id] = {
                "decision": "REVIEW_POSSIBLE_VARIANT",
                "confidence": 0.50,
                "review_required": True,
                "reason": "Same cleaned title exists in the source batch, but file size/duration differs. It may be multipart, a sequel, alternate cut, or an episode.",
                "preferred_source_message_id": None,
            }
    return decisions

@app.get("/")
async def root():
    return {
        "service": "STRIMA Telegram Gateway",
        "status": "online",
        "mode": "Telegram origin -> Bunny CDN",
        "version": "0.6.0",
    }

@app.get("/health")
async def health():
    if not client.is_connected():
        return JSONResponse(status_code=503, content={"ok": False, "telegram": "disconnected"})
    try:
        me = await client.get_me()
        return {
            "ok": True,
            "telegram": "connected",
            "telegram_user_id": me.id,
            "telegram_username": getattr(me, "username", None),
            "channel_id": TG_CHANNEL_ID,
            "channel_resolved": CHANNEL_INPUT_ENTITY is not None,
            "source_channel_id": TG_SOURCE_CHANNEL_ID,
            "source_channel_resolved": SOURCE_INPUT_ENTITY is not None,
            "source_channel_title": SOURCE_CHANNEL_TITLE,
            "supabase_configured": bool(SUPABASE_URL and SUPABASE_PUBLISHABLE_KEY),
        }
    except Exception as exc:
        return JSONResponse(status_code=503, content={"ok": False, "telegram": type(exc).__name__})

@app.get("/admin/telegram/channels")
async def list_telegram_channels(
    admin_key: Optional[str] = Header(default=None, alias="X-STRIMA-Admin-Key"),
):
    require_admin_key(admin_key)
    if not client.is_connected():
        raise HTTPException(status_code=503, detail="Telegram client is disconnected")
    try:
        dialogs = await client.get_dialogs()
        channels = []
        for dialog in dialogs:
            if not getattr(dialog, "is_channel", False):
                continue
            entity = getattr(dialog, "entity", None)
            channels.append({
                "id": dialog.id,
                "title": dialog.title,
                "username": getattr(entity, "username", None),
                "is_group": bool(getattr(dialog, "is_group", False)),
                "is_channel": True,
                "is_configured_destination": dialog.id == TG_CHANNEL_ID,
                "is_configured_source": dialog.id == TG_SOURCE_CHANNEL_ID,
            })
        channels.sort(key=lambda item: (item.get("title") or "").lower())
        return {
            "ok": True,
            "count": len(channels),
            "configured_destination_channel_id": TG_CHANNEL_ID,
            "configured_source_channel_id": TG_SOURCE_CHANNEL_ID,
            "channels": channels,
        }
    except HTTPException:
        raise
    except Exception as exc:
        log.exception("Failed to list Telegram channels")
        raise HTTPException(status_code=502, detail=f"Telegram channel discovery failed: {type(exc).__name__}")

@app.get("/admin/telegram/source-preview")
async def source_preview(
    limit: int = Query(default=20, ge=1, le=100),
    admin_key: Optional[str] = Header(default=None, alias="X-STRIMA-Admin-Key"),
):
    require_admin_key(admin_key)
    if not client.is_connected():
        raise HTTPException(status_code=503, detail="Telegram client is disconnected")
    try:
        movies, scanned = await collect_source_movies(limit)
        return {
            "ok": True,
            "mode": "read_only_preview",
            "source_channel": {"id": TG_SOURCE_CHANNEL_ID, "title": SOURCE_CHANNEL_TITLE},
            "destination_channel_id": TG_CHANNEL_ID,
            "scanned_messages": scanned,
            "movie_candidates": len(movies),
            "limit": limit,
            "movies": movies,
        }
    except HTTPException:
        raise
    except Exception as exc:
        log.exception("Telegram source preview failed for channel=%s", TG_SOURCE_CHANNEL_ID)
        raise HTTPException(status_code=502, detail=f"Telegram source preview failed: {type(exc).__name__}")

@app.get("/admin/telegram/import-preview")
async def import_preview(
    limit: int = Query(default=20, ge=1, le=100),
    admin_key: Optional[str] = Header(default=None, alias="X-STRIMA-Admin-Key"),
):
    require_admin_key(admin_key)
    if not client.is_connected():
        raise HTTPException(status_code=503, detail="Telegram client is disconnected")
    movies, scanned = await collect_source_movies(limit)
    source_decisions = classify_source_duplicates(movies)
    results = []
    for item in movies:
        message_id = int(item["source_message_id"])
        source_decision = source_decisions.get(message_id)
        if source_decision and source_decision["decision"] == "SOURCE_DUPLICATE":
            results.append({**item, **source_decision, "supabase_match": None})
            continue
        try:
            matches = await supabase_duplicate_rpc(item)
        except Exception as exc:
            log.exception("Supabase duplicate check failed for source message %s", message_id)
            results.append({
                **item,
                "decision": "CHECK_ERROR",
                "confidence": 0.0,
                "review_required": True,
                "reason": str(exc),
                "preferred_source_message_id": None,
                "supabase_match": None,
            })
            continue
        top = matches[0] if isinstance(matches, list) and matches else None
        if top and top.get("decision") == "duplicate":
            final_decision = {
                "decision": "ALREADY_IN_SUPABASE",
                "confidence": float(top.get("confidence") or 0.99),
                "review_required": False,
                "reason": top.get("reason") or "Strong duplicate already exists in Supabase.",
                "preferred_source_message_id": None,
                "supabase_match": {
                    "movie_id": top.get("movie_id"),
                    "title": top.get("title"),
                    "release_year": top.get("release_year"),
                },
            }
        elif top:
            final_decision = {
                "decision": "REVIEW_POSSIBLE_VARIANT",
                "confidence": float(top.get("confidence") or 0.50),
                "review_required": True,
                "reason": top.get("reason") or "Same cleaned title exists in Supabase, but evidence is not strong enough to skip.",
                "preferred_source_message_id": None,
                "supabase_match": {
                    "movie_id": top.get("movie_id"),
                    "title": top.get("title"),
                    "release_year": top.get("release_year"),
                },
            }
        elif source_decision:
            final_decision = {**source_decision, "supabase_match": None}
        else:
            final_decision = {
                "decision": "READY_TO_IMPORT",
                "confidence": 1.0,
                "review_required": False,
                "reason": "No strong source duplicate and no existing Supabase movie match.",
                "preferred_source_message_id": None,
                "supabase_match": None,
            }
        results.append({**item, **final_decision})
    summary = {}
    for item in results:
        decision = item["decision"]
        summary[decision] = summary.get(decision, 0) + 1
    return {
        "ok": True,
        "mode": "read_only_import_decision_preview",
        "source_channel": {"id": TG_SOURCE_CHANNEL_ID, "title": SOURCE_CHANNEL_TITLE},
        "destination_channel_id": TG_CHANNEL_ID,
        "scanned_messages": scanned,
        "candidate_count": len(results),
        "summary": summary,
        "items": results,
    }

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
        return Response(status_code=206 if partial else 200, headers=headers)
    return StreamingResponse(
        telegram_byte_stream(message, start, end),
        status_code=206 if partial else 200,
        headers=headers,
        media_type=headers["Content-Type"],
    )
