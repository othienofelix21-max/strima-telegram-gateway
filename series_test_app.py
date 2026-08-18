import asyncio
import logging
import re
from typing import Optional

from fastapi import Header, Query
from telethon.errors import FloodWaitError

import register_existing_app as current
import app as base
import import_app as importer
import metadata_app as metadata

app = current.app
log = logging.getLogger("strima-series-worker")

SERIES_SOURCE_TITLE = "PREMIUM SERIES"
PLAYBACK_BASE = "https://mc-p2ku5nz4qw.bunny.run/movie"

STATE = {
    "running": False,
    "completed": False,
    "phase": "idle",
    "source_channel_id": None,
    "series_title": None,
    "series_slug": None,
    "season_number": None,
    "found": 0,
    "first_episode": None,
    "last_episode": None,
    "copied": 0,
    "registered": 0,
    "already_registered": 0,
    "duplicates_ignored": 0,
    "tmdb_series_enriched": False,
    "tmdb_episodes_enriched": 0,
    "tmdb_matched": False,
    "tmdb_id": None,
    "tmdb_expected_season_episodes": None,
    "failed": 0,
    "current_episode": None,
    "last_error": None,
}
TASK = None


def _slugify(text: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", str(text or "").strip().lower()).strip("-")
    return value or "series"


def _episode_number(message, series_title: str):
    file_obj = getattr(message, "file", None)
    name = str(getattr(file_obj, "name", "") or "")
    caption = str(getattr(message, "message", "") or "")
    text = f"{name}\n{caption}"

    title_pattern = re.escape(str(series_title or "").strip()).replace(r"\ ", r"[\s._-]+")
    patterns = [
        rf"\b{title_pattern}[\s._-]*(?:s\s*0*\d{{1,2}}[\s._-]*)?(?:e|ep|episode)?[\s._-]*0*(\d{{1,3}})\b",
        rf"\b{title_pattern}[\s._-]+0*(\d{{1,3}})\b",
    ]
    for pattern in patterns:
        m = re.search(pattern, text, flags=re.I)
        if m:
            value = int(m.group(1))
            if 1 <= value <= 500:
                return value
    return None


async def _resolve_series_source():
    dialogs = await base.client.get_dialogs(limit=None)
    wanted = SERIES_SOURCE_TITLE.casefold()
    exact = [d for d in dialogs if (getattr(d, "title", "") or "").strip().casefold() == wanted]
    partial = [d for d in dialogs if wanted in (getattr(d, "title", "") or "").casefold()]
    matches = exact or partial
    if not matches:
        raise RuntimeError(f"Telegram channel {SERIES_SOURCE_TITLE!r} was not found")
    return matches[0]


async def _find_all_episodes(source, series_title: str):
    found = {}
    duplicates = 0
    async for message in base.client.iter_messages(source.input_entity, search=series_title):
        file_obj = getattr(message, "file", None)
        if not base.is_video_file(file_obj, message):
            continue
        ep = _episode_number(message, series_title)
        if ep is None:
            continue
        if ep in found:
            duplicates += 1
            continue
        found[ep] = message

    if not found:
        raise RuntimeError(f"No numbered video episodes found for {series_title!r}")

    episode_numbers = sorted(found)
    STATE["found"] = len(episode_numbers)
    STATE["first_episode"] = episode_numbers[0]
    STATE["last_episode"] = episode_numbers[-1]
    STATE["duplicates_ignored"] = duplicates
    return [found[n] for n in episode_numbers]


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


async def _register_episode(
    source_channel_id: int,
    source_message,
    destination_message,
    episode_number: int,
    series_title: str,
    series_slug: str,
    season_number: int,
):
    file_obj = source_message.file
    size_bytes = int(getattr(file_obj, "size", 0) or 0)
    duration = getattr(file_obj, "duration", None)
    filename = str(getattr(file_obj, "name", "") or "")
    playback_url = f"{PLAYBACK_BASE}/{int(destination_message.id)}"

    rows = await importer._rpc(
        "strima_gateway_register_episode_v1",
        {
            "p_admin_key": base.STRIMA_ADMIN_KEY,
            "p_series_title": series_title,
            "p_series_slug": series_slug,
            "p_season_number": season_number,
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
            "p_source_normalized_title": f"{series_title.lower()} season {season_number} episode {episode_number}",
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
        wait_seconds = int(getattr(exc, "seconds", 0) or 0) + 2
        log.warning("Telegram FloodWait: waiting %ss", wait_seconds)
        await asyncio.sleep(wait_seconds)
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


async def _find_tmdb_tv(series_title: str, season_number: int):
    if metadata.METADATA_PROVIDER != "tmdb" or not metadata.TMDB_BEARER_TOKEN:
        return None, None, None

    candidates = await metadata._tmdb_search_kind("tv", series_title, None)
    scored = sorted(
        ((metadata._score_tmdb_candidate(series_title, None, c), c) for c in candidates),
        key=lambda row: row[0],
        reverse=True,
    )
    if not scored or scored[0][0] < 70:
        return None, None, None

    _, candidate = scored[0]
    details = await metadata._tmdb_get(
        f"/tv/{int(candidate['id'])}",
        {"language": metadata.TMDB_LANGUAGE},
    )
    season_details = None
    try:
        season_details = await metadata._tmdb_get(
            f"/tv/{int(candidate['id'])}/season/{season_number}",
            {"language": metadata.TMDB_LANGUAGE},
        )
        episodes = season_details.get("episodes") if isinstance(season_details, dict) else None
        if isinstance(episodes, list):
            STATE["tmdb_expected_season_episodes"] = len(episodes)
    except Exception:
        log.exception("TMDB season lookup failed for %s season %s", series_title, season_number)

    return candidate, details, season_details


async def _enrich_series_and_episode(
    registered: dict,
    episode_number: int,
    candidate: dict,
    details: dict,
    season_details: Optional[dict],
    season_number: int,
):
    if not candidate or not details:
        return

    poster_path = details.get("poster_path") or candidate.get("poster_path")
    backdrop_path = details.get("backdrop_path") or candidate.get("backdrop_path")
    countries = details.get("origin_country") or []
    first_air = str(details.get("first_air_date") or candidate.get("first_air_date") or "")
    release_year = int(first_air[:4]) if len(first_air) >= 4 and first_air[:4].isdigit() else None

    poster_url = f"https://image.tmdb.org/t/p/w500{poster_path}" if poster_path else None
    banner_url = f"https://image.tmdb.org/t/p/original{backdrop_path}" if backdrop_path else None
    default_episode_thumbnail = banner_url or poster_url

    await importer._rpc(
        "strima_gateway_update_series_metadata_v1",
        {
            "p_admin_key": base.STRIMA_ADMIN_KEY,
            "p_series_id": registered.get("series_id"),
            "p_tmdb_id": int(candidate.get("id")) if candidate.get("id") else None,
            "p_description": str(details.get("overview") or candidate.get("overview") or "").strip() or None,
            "p_poster_url": poster_url,
            "p_banner_url": banner_url,
            "p_thumbnail_url": poster_url or banner_url,
            "p_release_year": release_year,
            "p_country": str(countries[0]) if countries else None,
        },
    )
    STATE["tmdb_series_enriched"] = True
    STATE["tmdb_matched"] = True
    STATE["tmdb_id"] = int(candidate.get("id")) if candidate.get("id") else None

    episode_metadata = None
    if isinstance(season_details, dict):
        episodes = season_details.get("episodes") or []
        for item in episodes:
            try:
                if int(item.get("episode_number")) == int(episode_number):
                    episode_metadata = item
                    break
            except (TypeError, ValueError):
                continue

    if episode_metadata is None:
        try:
            episode_metadata = await metadata._tmdb_get(
                f"/tv/{int(candidate['id'])}/season/{season_number}/episode/{episode_number}",
                {"language": metadata.TMDB_LANGUAGE},
            )
        except Exception:
            log.exception("TMDB episode lookup failed for episode %s", episode_number)
            episode_metadata = {}

    ep = episode_metadata if isinstance(episode_metadata, dict) else {}
    await importer._rpc(
        "strima_gateway_update_episode_metadata_v1",
        {
            "p_admin_key": base.STRIMA_ADMIN_KEY,
            "p_episode_id": registered.get("episode_id"),
            "p_title": str(ep.get("name") or "").strip() or None,
            "p_description": str(ep.get("overview") or "").strip() or None,
            # STRIMA default: every episode uses the series banner/poster as its thumbnail.
            "p_thumbnail_url": default_episode_thumbnail,
            "p_duration_minutes": int(ep.get("runtime")) if ep.get("runtime") else None,
        },
    )
    STATE["tmdb_episodes_enriched"] += 1


async def _worker(series_title: str, season_number: int):
    series_title = re.sub(r"\s+", " ", str(series_title or "")).strip()
    if not series_title:
        raise RuntimeError("Series title is required")
    if season_number < 1:
        raise RuntimeError("Season number must be >= 1")
    series_slug = _slugify(series_title)

    STATE.update({
        "running": True,
        "completed": False,
        "phase": "resolving_source",
        "source_channel_id": None,
        "series_title": series_title,
        "series_slug": series_slug,
        "season_number": season_number,
        "found": 0,
        "first_episode": None,
        "last_episode": None,
        "copied": 0,
        "registered": 0,
        "already_registered": 0,
        "duplicates_ignored": 0,
        "tmdb_series_enriched": False,
        "tmdb_episodes_enriched": 0,
        "tmdb_matched": False,
        "tmdb_id": None,
        "tmdb_expected_season_episodes": None,
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

        STATE["phase"] = "finding_all_episodes"
        messages = await _find_all_episodes(source, series_title)

        STATE["phase"] = "tmdb_preflight"
        tmdb_candidate, tmdb_details, tmdb_season = await _find_tmdb_tv(series_title, season_number)

        for message in messages:
            ep = _episode_number(message, series_title)
            STATE["current_episode"] = ep
            STATE["phase"] = f"episode_{ep}_preflight"

            existing = await _episode_lookup(source_channel_id, int(message.id))
            if existing:
                STATE["already_registered"] += 1
                if tmdb_candidate and tmdb_details:
                    await _enrich_series_and_episode(
                        existing, ep, tmdb_candidate, tmdb_details, tmdb_season, season_number
                    )
                continue

            STATE["phase"] = f"episode_{ep}_copying"
            destination = await _copy_message(message)
            STATE["copied"] += 1

            STATE["phase"] = f"episode_{ep}_registering"
            registered = await _register_episode(
                source_channel_id,
                message,
                destination,
                ep,
                series_title,
                series_slug,
                season_number,
            )
            if not registered:
                raise RuntimeError(f"Supabase returned no row for episode {ep}")
            STATE["registered"] += 1

            if tmdb_candidate and tmdb_details:
                STATE["phase"] = f"episode_{ep}_tmdb_enrichment"
                await _enrich_series_and_episode(
                    registered, ep, tmdb_candidate, tmdb_details, tmdb_season, season_number
                )

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
        log.exception("Series worker failed")
    finally:
        STATE["running"] = False


@app.post("/admin/telegram/series/run/start")
async def start_series_run(
    series_title: str = Query(..., min_length=1),
    season_number: int = Query(default=1, ge=1),
    admin_key: Optional[str] = Header(default=None, alias="X-STRIMA-Admin-Key"),
):
    global TASK
    base.require_admin_key(admin_key)
    if TASK is not None and not TASK.done():
        return {"ok": True, "started": False, "reason": "A series job is already running", "size_limit": "unlimited", **STATE}
    TASK = asyncio.create_task(
        _worker(series_title, season_number),
        name=f"strima-series-{_slugify(series_title)}-s{season_number}",
    )
    return {
        "ok": True,
        "started": True,
        "worker": "STRIMA Series Worker",
        "requested_series": series_title,
        "requested_season": season_number,
        "size_limit": "unlimited",
    }


@app.get("/admin/telegram/series/run/status")
async def series_run_status(
    admin_key: Optional[str] = Header(default=None, alias="X-STRIMA-Admin-Key"),
):
    base.require_admin_key(admin_key)
    return {
        "ok": True,
        "worker": "STRIMA Series Worker",
        "source_channel": SERIES_SOURCE_TITLE,
        "size_limit": "unlimited",
        "episode_thumbnail_default": "series_banner_or_poster",
        "tmdb_enabled": metadata.METADATA_PROVIDER == "tmdb" and bool(metadata.TMDB_BEARER_TOKEN),
        "task_running": bool(TASK is not None and not TASK.done()),
        **STATE,
    }


# Backward-compatible controlled test endpoints kept for the already-proven 2-episode flow.
@app.post("/admin/telegram/series/test/start")
async def start_series_test(
    admin_key: Optional[str] = Header(default=None, alias="X-STRIMA-Admin-Key"),
):
    global TASK
    base.require_admin_key(admin_key)
    if TASK is not None and not TASK.done():
        return {"ok": True, "started": False, "size_limit": "unlimited", **STATE}
    TASK = asyncio.create_task(_worker("Jumong", 1), name="strima-series-jumong-s1")
    return {"ok": True, "started": True, "size_limit": "unlimited", "note": "This endpoint now runs the full Jumong season; use /series/run/start for future titles."}


@app.get("/admin/telegram/series/test/status")
async def series_test_status(
    admin_key: Optional[str] = Header(default=None, alias="X-STRIMA-Admin-Key"),
):
    base.require_admin_key(admin_key)
    return {
        "ok": True,
        "worker": "STRIMA Series Worker",
        "size_limit": "unlimited",
        "episode_thumbnail_default": "series_banner_or_poster",
        "tmdb_enabled": metadata.METADATA_PROVIDER == "tmdb" and bool(metadata.TMDB_BEARER_TOKEN),
        "task_running": bool(TASK is not None and not TASK.done()),
        **STATE,
    }
