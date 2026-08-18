import asyncio
import logging
import re
from typing import Optional

from fastapi import Header
from telethon.errors import FloodWaitError

import register_existing_app as current
import app as base
import import_app as importer
import metadata_app as metadata

app = current.app
log = logging.getLogger("strima-series-worker")

# Generic series worker. These values define only the CURRENT controlled test.
SERIES_SOURCE_TITLE = "PREMIUM SERIES"
TEST_SERIES_TITLE = "Jumong"
TEST_SERIES_SLUG = "jumong"
TEST_SEASON_NUMBER = 1
TEST_EPISODES = {1, 2}
PLAYBACK_BASE = "https://mc-p2ku5nz4qw.bunny.run/movie"

STATE = {
    "running": False,
    "completed": False,
    "phase": "idle",
    "source_channel_id": None,
    "series_title": TEST_SERIES_TITLE,
    "season_number": TEST_SEASON_NUMBER,
    "found": 0,
    "copied": 0,
    "registered": 0,
    "already_registered": 0,
    "tmdb_series_enriched": False,
    "tmdb_episodes_enriched": 0,
    "tmdb_matched": False,
    "tmdb_id": None,
    "failed": 0,
    "current_episode": None,
    "last_error": None,
}
TASK = None


def _episode_number(message):
    file_obj = getattr(message, "file", None)
    name = str(getattr(file_obj, "name", "") or "")
    caption = str(getattr(message, "message", "") or "")
    text = f"{name}\n{caption}"
    m = re.search(r"\bjumong[\s._-]*0*(\d{1,3})\b", text, flags=re.I)
    return int(m.group(1)) if m else None


async def _resolve_series_source():
    dialogs = await base.client.get_dialogs(limit=None)
    wanted = SERIES_SOURCE_TITLE.casefold()
    exact = [d for d in dialogs if (getattr(d, "title", "") or "").strip().casefold() == wanted]
    partial = [d for d in dialogs if wanted in (getattr(d, "title", "") or "").casefold()]
    matches = exact or partial
    if not matches:
        raise RuntimeError(f"Telegram channel {SERIES_SOURCE_TITLE!r} was not found")
    return matches[0]


async def _find_test_episodes(source):
    found = {}
    async for message in base.client.iter_messages(source.input_entity, search=TEST_SERIES_TITLE):
        file_obj = getattr(message, "file", None)
        if not base.is_video_file(file_obj, message):
            continue
        ep = _episode_number(message)
        if ep in TEST_EPISODES and ep not in found:
            found[ep] = message
        if TEST_EPISODES.issubset(found.keys()):
            break
    missing = sorted(TEST_EPISODES - set(found.keys()))
    if missing:
        raise RuntimeError(f"Missing test episode(s): {missing}")
    STATE["found"] = len(found)
    return [found[n] for n in sorted(TEST_EPISODES)]


async def _episode_lookup(source_channel_id: int, source_message_id: int):
    rows = await importer._rpc(
        "strima_gateway_episode_source_lookup",
        {
            "p_admin_key": base.STRIMA_ADMIN_KEY,
            "p_source_channel_id": source_channel_id,
            "p_source_message_id": source_message_id,
        },
    )
    return rows[0] if isinstance(rows, list) and rows else None


async def _register_episode(source_channel_id: int, source_message, destination_message, episode_number: int):
    file_obj = source_message.file
    size_bytes = int(getattr(file_obj, "size", 0) or 0)
    duration = getattr(file_obj, "duration", None)
    filename = str(getattr(file_obj, "name", "") or "")
    playback_url = f"{PLAYBACK_BASE}/{int(destination_message.id)}"

    rows = await importer._rpc(
        "strima_gateway_register_episode_v1",
        {
            "p_admin_key": base.STRIMA_ADMIN_KEY,
            "p_series_title": TEST_SERIES_TITLE,
            "p_series_slug": TEST_SERIES_SLUG,
            "p_season_number": TEST_SEASON_NUMBER,
            "p_episode_number": episode_number,
            "p_episode_title": f"Episode {episode_number}",
            "p_duration_seconds": int(duration) if duration else None,
            "p_source_channel_id": source_channel_id,
            "p_source_message_id": int(source_message.id),
            "p_destination_channel_id": int(base.TG_CHANNEL_ID),
            "p_destination_message_id": int(destination_message.id),
            "p_playback_url": playback_url,
            "p_file_size_mb": round(size_bytes / (1024 * 1024), 2) if size_bytes else None,
            "p_source_filename": filename or None,
            "p_source_normalized_title": f"{TEST_SERIES_TITLE.lower()} season {TEST_SEASON_NUMBER} episode {episode_number}",
        },
    )
    return rows[0] if isinstance(rows, list) and rows else None


async def _copy_message(message):
    try:
        result = await base.client.send_file(
            base.CHANNEL_INPUT_ENTITY,
            file=message.media,
            caption=message.message or "",
            formatting_entities=(message.entities or None),
        )
    except FloodWaitError as exc:
        await asyncio.sleep(int(getattr(exc, "seconds", 0) or 0) + 2)
        result = await base.client.send_file(
            base.CHANNEL_INPUT_ENTITY,
            file=message.media,
            caption=message.message or "",
            formatting_entities=(message.entities or None),
        )
    if isinstance(result, (list, tuple)):
        result = result[0] if result else None
    if not result or not getattr(result, "id", None):
        raise RuntimeError("Telegram did not return destination message ID")
    return result


async def _find_tmdb_tv():
    if metadata.METADATA_PROVIDER != "tmdb" or not metadata.TMDB_BEARER_TOKEN:
        return None, None
    candidates = await metadata._tmdb_search_kind("tv", TEST_SERIES_TITLE, None)
    scored = sorted(
        ((metadata._score_tmdb_candidate(TEST_SERIES_TITLE, None, c), c) for c in candidates),
        key=lambda row: row[0],
        reverse=True,
    )
    if not scored or scored[0][0] < 70:
        return None, None
    score, candidate = scored[0]
    details = await metadata._tmdb_get(f"/tv/{int(candidate['id'])}", {"language": metadata.TMDB_LANGUAGE})
    return candidate, details


async def _enrich_series_and_episode(registered: dict, episode_number: int, candidate: dict, details: dict):
    if not candidate or not details:
        return

    poster_path = details.get("poster_path") or candidate.get("poster_path")
    backdrop_path = details.get("backdrop_path") or candidate.get("backdrop_path")
    countries = details.get("origin_country") or []
    first_air = str(details.get("first_air_date") or candidate.get("first_air_date") or "")
    release_year = int(first_air[:4]) if len(first_air) >= 4 and first_air[:4].isdigit() else None

    await importer._rpc(
        "strima_gateway_update_series_metadata_v1",
        {
            "p_admin_key": base.STRIMA_ADMIN_KEY,
            "p_series_id": registered.get("series_id"),
            "p_tmdb_id": int(candidate.get("id")) if candidate.get("id") else None,
            "p_description": str(details.get("overview") or candidate.get("overview") or "").strip() or None,
            "p_poster_url": f"https://image.tmdb.org/t/p/w500{poster_path}" if poster_path else None,
            "p_banner_url": f"https://image.tmdb.org/t/p/original{backdrop_path}" if backdrop_path else None,
            "p_thumbnail_url": f"https://image.tmdb.org/t/p/w500{poster_path}" if poster_path else None,
            "p_release_year": release_year,
            "p_country": str(countries[0]) if countries else None,
        },
    )
    STATE["tmdb_series_enriched"] = True
    STATE["tmdb_matched"] = True
    STATE["tmdb_id"] = int(candidate.get("id")) if candidate.get("id") else None

    try:
        ep = await metadata._tmdb_get(
            f"/tv/{int(candidate['id'])}/season/{TEST_SEASON_NUMBER}/episode/{episode_number}",
            {"language": metadata.TMDB_LANGUAGE},
        )
    except Exception:
        log.exception("TMDB episode lookup failed for episode %s", episode_number)
        return

    still_path = ep.get("still_path") if isinstance(ep, dict) else None
    await importer._rpc(
        "strima_gateway_update_episode_metadata_v1",
        {
            "p_admin_key": base.STRIMA_ADMIN_KEY,
            "p_episode_id": registered.get("episode_id"),
            "p_title": str(ep.get("name") or "").strip() or None,
            "p_description": str(ep.get("overview") or "").strip() or None,
            "p_thumbnail_url": f"https://image.tmdb.org/t/p/w500{still_path}" if still_path else None,
            "p_duration_minutes": int(ep.get("runtime")) if ep.get("runtime") else None,
        },
    )
    STATE["tmdb_episodes_enriched"] += 1


async def _worker():
    STATE.update({
        "running": True,
        "completed": False,
        "phase": "resolving_source",
        "source_channel_id": None,
        "series_title": TEST_SERIES_TITLE,
        "season_number": TEST_SEASON_NUMBER,
        "found": 0,
        "copied": 0,
        "registered": 0,
        "already_registered": 0,
        "tmdb_series_enriched": False,
        "tmdb_episodes_enriched": 0,
        "tmdb_matched": False,
        "tmdb_id": None,
        "failed": 0,
        "current_episode": None,
        "last_error": None,
    })
    try:
        if not base.client.is_connected():
            raise RuntimeError("Telegram client is disconnected")
        if base.CHANNEL_INPUT_ENTITY is None:
            raise RuntimeError("STRIMA destination Telegram channel is not resolved")

        source = await _resolve_series_source()
        source_channel_id = int(source.id)
        STATE["source_channel_id"] = source_channel_id
        STATE["phase"] = "finding_episodes"
        messages = await _find_test_episodes(source)

        STATE["phase"] = "tmdb_preflight"
        tmdb_candidate, tmdb_details = await _find_tmdb_tv()

        for message in messages:
            ep = _episode_number(message)
            STATE["current_episode"] = ep
            STATE["phase"] = f"episode_{ep}_preflight"

            existing = await _episode_lookup(source_channel_id, int(message.id))
            if existing:
                STATE["already_registered"] += 1
                if tmdb_candidate and tmdb_details:
                    await _enrich_series_and_episode(existing, ep, tmdb_candidate, tmdb_details)
                continue

            STATE["phase"] = f"episode_{ep}_copying"
            destination = await _copy_message(message)
            STATE["copied"] += 1

            STATE["phase"] = f"episode_{ep}_registering"
            registered = await _register_episode(source_channel_id, message, destination, ep)
            if not registered:
                raise RuntimeError(f"Supabase returned no row for episode {ep}")
            STATE["registered"] += 1

            if tmdb_candidate and tmdb_details:
                STATE["phase"] = f"episode_{ep}_tmdb_enrichment"
                await _enrich_series_and_episode(registered, ep, tmdb_candidate, tmdb_details)

            await asyncio.sleep(1)

        STATE["phase"] = "complete"
        STATE["completed"] = True
        STATE["current_episode"] = None
    except asyncio.CancelledError:
        STATE["phase"] = "stopped"
        raise
    except Exception as exc:
        STATE["failed"] += 1
        STATE["phase"] = "error"
        STATE["last_error"] = f"{type(exc).__name__}: {str(exc)[:500]}"
        log.exception("Series worker test failed")
    finally:
        STATE["running"] = False


@app.post("/admin/telegram/series/test/start")
async def start_series_test(
    admin_key: Optional[str] = Header(default=None, alias="X-STRIMA-Admin-Key"),
):
    global TASK
    base.require_admin_key(admin_key)
    if TASK is not None and not TASK.done():
        return {"ok": True, "started": False, "size_limit": "unlimited", **STATE}
    TASK = asyncio.create_task(_worker(), name="strima-series-test")
    return {"ok": True, "started": True, "size_limit": "unlimited", **STATE}


@app.get("/admin/telegram/series/test/status")
async def series_test_status(
    admin_key: Optional[str] = Header(default=None, alias="X-STRIMA-Admin-Key"),
):
    base.require_admin_key(admin_key)
    return {
        "ok": True,
        "worker": "STRIMA Series Worker",
        "test_series": TEST_SERIES_TITLE,
        "size_limit": "unlimited",
        "tmdb_enabled": metadata.METADATA_PROVIDER == "tmdb" and bool(metadata.TMDB_BEARER_TOKEN),
        "task_running": bool(TASK is not None and not TASK.done()),
        **STATE,
    }
