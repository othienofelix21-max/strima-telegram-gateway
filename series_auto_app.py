import asyncio
import logging
import re
from collections import Counter
from typing import Optional

from fastapi import Header, Query

import series_test_app as series

app = series.app
log = logging.getLogger("strima-series-auto-batch")

AUTO_TASK = None
AUTO_STATE = {
    "running": False,
    "completed": False,
    "phase": "idle",
    "batch_limit": 5,
    "scanned_messages": 0,
    "video_messages": 0,
    "discovered_series": 0,
    "eligible_series": 0,
    "selected_series": [],
    "current_series": None,
    "current_index": 0,
    "completed_series": 0,
    "failed_series": 0,
    "series_results": [],
    "last_error": None,
}


def _slugify(text: str) -> str:
    return series._slugify(text)


def _clean_label(message) -> str:
    file_obj = getattr(message, "file", None)
    filename = str(getattr(file_obj, "name", "") or "").strip()
    caption = str(getattr(message, "message", "") or "").strip()
    raw = filename or (caption.splitlines()[0] if caption else "")
    raw = re.sub(r"\.(mkv|mp4|avi|mov|webm|m4v|ts)$", "", raw, flags=re.I)
    raw = raw.replace("_", " ").replace(".", " ")
    raw = re.sub(r"\s+", " ", raw).strip()
    return raw


def _clean_title(title: str) -> str:
    value = str(title or "")
    value = re.sub(r"^[\s\W_]+", "", value, flags=re.UNICODE)
    value = re.sub(r"^(?:premium\s+series|series)\s*[:\-–—]\s*", "", value, flags=re.I)
    value = re.sub(r"(?:\s*[\-–—]\s*)?(?:season|s)\s*0*\d{1,2}\s*$", "", value, flags=re.I)
    value = re.sub(r"\b(?:episode|ep|e)\s*$", "", value, flags=re.I)
    value = re.sub(r"\s+", " ", value).strip(" -–—_:;[]()")
    return value


def _discover_title_episode(message):
    text = _clean_label(message)
    if not text:
        return None

    explicit_patterns = [
        r"^(?P<title>.+?)\s+[Ss]\s*0*(?P<season>\d{1,2})\s*[Ee]\s*0*(?P<ep>\d{1,3})\b",
        r"^(?P<title>.+?)\s+(?:Season\s*0*(?P<season>\d{1,2})\s+)?(?:Episode|Ep|E)\s*0*(?P<ep>\d{1,3})\b",
    ]
    for pattern in explicit_patterns:
        m = re.search(pattern, text, flags=re.I)
        if m:
            title = _clean_title(m.group("title"))
            ep = int(m.group("ep"))
            season_raw = m.groupdict().get("season")
            season = int(season_raw) if season_raw else 1
            if title and 1 <= ep <= 500 and 1 <= season <= 50:
                return title, season, ep

    # Common VJ naming pattern: <Series title> <episode number> -/by <translator>.
    # Take the last plausible episode-sized number and ignore common resolution values.
    candidates = []
    for m in re.finditer(r"(?<!\d)(\d{1,3})(?!\d)", text):
        value = int(m.group(1))
        if value in {240, 360, 480, 540, 576, 720}:
            continue
        if 1 <= value <= 500:
            candidates.append((m, value))
    if not candidates:
        return None

    m, ep = candidates[-1]
    title = _clean_title(text[:m.start()])
    if not title or len(title) < 2:
        return None
    if title.casefold() in {"episode", "ep", "part", "season", "premium series"}:
        return None
    return title, 1, ep


async def _discover_channel_series(source):
    groups = {}
    scanned = 0
    videos = 0

    async for message in series.base.client.iter_messages(source.input_entity):
        scanned += 1
        file_obj = getattr(message, "file", None)
        if not series.base.is_video_file(file_obj, message):
            continue
        videos += 1

        parsed = _discover_title_episode(message)
        if not parsed:
            continue
        title, season_number, episode_number = parsed
        slug = _slugify(title)

        group = groups.setdefault(
            slug,
            {
                "slug": slug,
                "titles": Counter(),
                "seasons": {},
                "newest_message_id": int(getattr(message, "id", 0) or 0),
            },
        )
        group["titles"][title] += 1
        group["newest_message_id"] = max(
            int(group["newest_message_id"]),
            int(getattr(message, "id", 0) or 0),
        )
        season_map = group["seasons"].setdefault(int(season_number), {})
        if episode_number not in season_map:
            season_map[int(episode_number)] = int(getattr(message, "id", 0) or 0)

    AUTO_STATE["scanned_messages"] = scanned
    AUTO_STATE["video_messages"] = videos

    discovered = []
    for group in groups.values():
        title = group["titles"].most_common(1)[0][0]
        season_eps = group["seasons"].get(1) or {}
        if len(season_eps) < 2:
            continue
        discovered.append(
            {
                "title": title,
                "slug": group["slug"],
                "season_number": 1,
                "telegram_episode_count": len(season_eps),
                "first_episode": min(season_eps),
                "last_episode": max(season_eps),
                "newest_message_id": group["newest_message_id"],
            }
        )

    discovered.sort(key=lambda x: x["newest_message_id"], reverse=True)
    AUTO_STATE["discovered_series"] = len(discovered)
    return discovered


async def _supabase_catalog():
    rows = await series.importer._rpc(
        "strima_gateway_series_catalog_v1",
        {"p_admin_key": series.base.STRIMA_ADMIN_KEY},
    )
    catalog = {}
    for row in rows if isinstance(rows, list) else []:
        slug = str(row.get("slug") or "").strip()
        if not slug:
            continue
        catalog[slug] = {
            "title": row.get("title"),
            "episode_count": int(row.get("episode_count") or 0),
        }
    return catalog


async def _auto_batch_worker(limit: int):
    AUTO_STATE.update(
        {
            "running": True,
            "completed": False,
            "phase": "resolving_source",
            "batch_limit": limit,
            "scanned_messages": 0,
            "video_messages": 0,
            "discovered_series": 0,
            "eligible_series": 0,
            "selected_series": [],
            "current_series": None,
            "current_index": 0,
            "completed_series": 0,
            "failed_series": 0,
            "series_results": [],
            "last_error": None,
        }
    )

    try:
        if not series.base.client.is_connected():
            raise RuntimeError("Telegram client is disconnected")
        if series.base.CHANNEL_INPUT_ENTITY is None:
            raise RuntimeError("STRIMA destination Telegram channel is not resolved")

        source = await series._resolve_series_source()

        AUTO_STATE["phase"] = "scanning_premium_series"
        discovered = await _discover_channel_series(source)

        AUTO_STATE["phase"] = "checking_supabase"
        catalog = await _supabase_catalog()

        eligible = []
        for item in discovered:
            existing_count = int(catalog.get(item["slug"], {}).get("episode_count", 0))
            item = {**item, "supabase_episode_count": existing_count}
            if item["telegram_episode_count"] > existing_count:
                eligible.append(item)

        AUTO_STATE["eligible_series"] = len(eligible)
        selected = eligible[:limit]
        AUTO_STATE["selected_series"] = [
            {
                "title": x["title"],
                "telegram_episode_count": x["telegram_episode_count"],
                "supabase_episode_count": x["supabase_episode_count"],
            }
            for x in selected
        ]

        if not selected:
            AUTO_STATE["phase"] = "complete"
            AUTO_STATE["completed"] = True
            return

        for index, item in enumerate(selected, start=1):
            title = item["title"]
            AUTO_STATE["current_index"] = index
            AUTO_STATE["current_series"] = title
            AUTO_STATE["phase"] = f"processing_{index}_of_{len(selected)}"

            await series._worker(title, int(item.get("season_number") or 1))
            snapshot = {
                "title": title,
                "found": int(series.STATE.get("found") or 0),
                "copied": int(series.STATE.get("copied") or 0),
                "registered": int(series.STATE.get("registered") or 0),
                "already_registered": int(series.STATE.get("already_registered") or 0),
                "tmdb_matched": bool(series.STATE.get("tmdb_matched")),
                "tmdb_id": series.STATE.get("tmdb_id"),
                "failed": int(series.STATE.get("failed") or 0),
                "last_error": series.STATE.get("last_error"),
            }
            AUTO_STATE["series_results"].append(snapshot)

            if series.STATE.get("completed") and not series.STATE.get("failed"):
                AUTO_STATE["completed_series"] += 1
            else:
                AUTO_STATE["failed_series"] += 1

        AUTO_STATE["current_series"] = None
        AUTO_STATE["phase"] = "complete"
        AUTO_STATE["completed"] = True

    except asyncio.CancelledError:
        AUTO_STATE["phase"] = "stopped"
        raise
    except Exception as exc:
        AUTO_STATE["phase"] = "error"
        AUTO_STATE["last_error"] = f"{type(exc).__name__}: {str(exc)[:500]}"
        log.exception("Auto series batch failed")
    finally:
        AUTO_STATE["running"] = False


@app.post("/admin/telegram/series/auto/start")
async def start_auto_series_batch(
    limit: int = Query(default=5, ge=1, le=5),
    admin_key: Optional[str] = Header(default=None, alias="X-STRIMA-Admin-Key"),
):
    global AUTO_TASK
    series.base.require_admin_key(admin_key)

    if AUTO_TASK is not None and not AUTO_TASK.done():
        return {
            "ok": True,
            "started": False,
            "reason": "Automatic series batch is already running",
            **AUTO_STATE,
        }
    if series.TASK is not None and not series.TASK.done():
        return {
            "ok": True,
            "started": False,
            "reason": "Another series job is already running",
            **series.STATE,
        }

    AUTO_TASK = asyncio.create_task(
        _auto_batch_worker(limit),
        name=f"strima-series-auto-{limit}",
    )
    series.TASK = AUTO_TASK

    return {
        "ok": True,
        "started": True,
        "worker": "STRIMA Auto Series Batch",
        "source_channel": series.SERIES_SOURCE_TITLE,
        "batch_limit": limit,
        "size_limit": "unlimited",
        "episode_thumbnail_default": "series_banner_or_poster",
    }


@app.get("/admin/telegram/series/auto/status")
async def auto_series_batch_status(
    admin_key: Optional[str] = Header(default=None, alias="X-STRIMA-Admin-Key"),
):
    series.base.require_admin_key(admin_key)
    return {
        "ok": True,
        "worker": "STRIMA Auto Series Batch",
        "source_channel": series.SERIES_SOURCE_TITLE,
        "size_limit": "unlimited",
        "episode_thumbnail_default": "series_banner_or_poster",
        "tmdb_enabled": series.metadata.METADATA_PROVIDER == "tmdb"
        and bool(series.metadata.TMDB_BEARER_TOKEN),
        "task_running": bool(AUTO_TASK is not None and not AUTO_TASK.done()),
        **AUTO_STATE,
    }
