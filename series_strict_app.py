import asyncio
from typing import Optional

from fastapi import Header, Query

import series_auto_app as auto

app = auto.app
series = auto.series

SCAN_TASK = None
UPLOAD_TASK = None
SELECTED = []
STATE = {
    "running": False,
    "completed": False,
    "ready_for_upload": False,
    "phase": "idle",
    "batch_limit": 2,
    "scanned_messages": 0,
    "video_messages": 0,
    "discovered_candidates": 0,
    "tmdb_candidates_checked": 0,
    "tmdb_candidates_rejected": 0,
    "episode_range_rejected": 0,
    "existing_series_rejected": 0,
    "selected_series": [],
    "current_series": None,
    "completed_series": 0,
    "failed_series": 0,
    "series_results": [],
    "last_error": None,
}


def _busy():
    return bool(
        (SCAN_TASK is not None and not SCAN_TASK.done())
        or (UPLOAD_TASK is not None and not UPLOAD_TASK.done())
    )


def _tmdb_episode_numbers(match: dict) -> set[int]:
    season_details = match.get("season_details") if isinstance(match, dict) else None
    episodes = season_details.get("episodes") if isinstance(season_details, dict) else None
    out = set()
    for row in episodes if isinstance(episodes, list) else []:
        try:
            number = int(row.get("episode_number"))
        except (TypeError, ValueError, AttributeError):
            continue
        if number > 0:
            out.add(number)
    return out


async def _scan_worker(limit: int):
    global SELECTED
    SELECTED = []
    STATE.update({
        "running": True,
        "completed": False,
        "ready_for_upload": False,
        "phase": "resolving_source",
        "batch_limit": limit,
        "scanned_messages": 0,
        "video_messages": 0,
        "discovered_candidates": 0,
        "tmdb_candidates_checked": 0,
        "tmdb_candidates_rejected": 0,
        "episode_range_rejected": 0,
        "existing_series_rejected": 0,
        "selected_series": [],
        "current_series": None,
        "completed_series": 0,
        "failed_series": 0,
        "series_results": [],
        "last_error": None,
    })
    try:
        source = await series._resolve_series_source()
        STATE["phase"] = "scanning_premium_series"
        candidates = await auto._discover_groups(source)
        STATE["scanned_messages"] = auto.AUTO_STATE.get("scanned_messages", 0)
        STATE["video_messages"] = auto.AUTO_STATE.get("video_messages", 0)
        STATE["discovered_candidates"] = len(candidates)
        STATE["phase"] = "tmdb_and_episode_range_validation"

        seen_tmdb = {}
        for group in candidates:
            if len(SELECTED) >= limit:
                break

            STATE["tmdb_candidates_checked"] += 1
            match = await auto._tmdb_validate(group["source_title"], group["season_number"])
            if not match:
                STATE["tmdb_candidates_rejected"] += 1
                continue

            allowed_eps = _tmdb_episode_numbers(match)
            if not allowed_eps:
                STATE["episode_range_rejected"] += 1
                continue

            source_eps = set(int(x) for x in group["episodes"].keys())
            invalid_eps = sorted(source_eps - allowed_eps)
            if invalid_eps:
                STATE["episode_range_rejected"] += 1
                continue

            tmdb_id = int(match["tmdb_id"])
            if tmdb_id in seen_tmdb:
                target = seen_tmdb[tmdb_id]
                merged = dict(target["episodes"])
                merged.update(group["episodes"])
                merged_eps = set(int(x) for x in merged.keys())
                if merged_eps - allowed_eps:
                    continue
                target["episodes"] = merged
                target["source_aliases"].add(group["source_title"])
                target["missing_episode_numbers"] = sorted(merged_eps)
                continue

            canonical_title = match["canonical_title"]
            slug = series._slugify(canonical_title)
            existing_eps = await auto._existing_episode_numbers(tmdb_id, slug)
            if existing_eps:
                STATE["existing_series_rejected"] += 1
                continue

            missing_eps = sorted(source_eps)
            if not missing_eps:
                continue

            item = {
                **group,
                **match,
                "slug": slug,
                "existing_episode_numbers": set(),
                "missing_episode_numbers": missing_eps,
                "source_aliases": {group["source_title"]},
                "tmdb_episode_numbers": allowed_eps,
            }
            SELECTED.append(item)
            seen_tmdb[tmdb_id] = item

        STATE["selected_series"] = [
            {
                "source_title": x["source_title"],
                "canonical_title": x["canonical_title"],
                "tmdb_id": x["tmdb_id"],
                "tmdb_score": x["score"],
                "season_number": x["season_number"],
                "tmdb_season_episode_count": len(x["tmdb_episode_numbers"]),
                "telegram_episode_count": len(x["episodes"]),
                "already_in_supabase": 0,
                "missing_to_upload": len(x["missing_episode_numbers"]),
                "first_episode": min(x["episodes"]),
                "last_episode": max(x["episodes"]),
            }
            for x in SELECTED
        ]
        STATE["ready_for_upload"] = len(SELECTED) == limit
        STATE["completed"] = True
        STATE["phase"] = "ready_for_confirmation" if SELECTED else "nothing_to_upload"
    except asyncio.CancelledError:
        STATE["phase"] = "stopped"
        raise
    except Exception as exc:
        STATE["phase"] = "error"
        STATE["last_error"] = f"{type(exc).__name__}: {str(exc)[:500]}"
    finally:
        STATE["running"] = False


async def _upload_worker():
    STATE.update({
        "running": True,
        "completed": False,
        "ready_for_upload": False,
        "phase": "uploading_confirmed_series",
        "current_series": None,
        "completed_series": 0,
        "failed_series": 0,
        "series_results": [],
        "last_error": None,
    })
    try:
        for item in SELECTED:
            STATE["current_series"] = item["canonical_title"]
            try:
                result = await auto._process_one(item)
                STATE["series_results"].append(result)
                STATE["completed_series"] += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                STATE["failed_series"] += 1
                STATE["series_results"].append({
                    "title": item["canonical_title"],
                    "tmdb_id": item["tmdb_id"],
                    "failed": 1,
                    "last_error": f"{type(exc).__name__}: {str(exc)[:400]}",
                })
        STATE["current_series"] = None
        STATE["completed"] = True
        STATE["phase"] = "complete"
    except asyncio.CancelledError:
        STATE["phase"] = "stopped"
        raise
    except Exception as exc:
        STATE["phase"] = "error"
        STATE["last_error"] = f"{type(exc).__name__}: {str(exc)[:500]}"
    finally:
        STATE["running"] = False


@app.post("/admin/telegram/series/strict/scan")
async def strict_scan(
    limit: int = Query(default=2, ge=1, le=5),
    admin_key: Optional[str] = Header(default=None, alias="X-STRIMA-Admin-Key"),
):
    global SCAN_TASK
    series.base.require_admin_key(admin_key)
    if _busy():
        return {"ok": True, "started": False, "reason": "Strict series task already running", **STATE}
    SCAN_TASK = asyncio.create_task(_scan_worker(limit), name=f"strima-strict-scan-{limit}")
    return {
        "ok": True,
        "started": True,
        "mode": "strict_scan_only_new_series",
        "batch_limit": limit,
        "note": "Existing Supabase series are skipped. No episodes are copied until /strict/upload/start is called.",
    }


@app.post("/admin/telegram/series/strict/upload/start")
async def strict_upload(
    admin_key: Optional[str] = Header(default=None, alias="X-STRIMA-Admin-Key"),
):
    global UPLOAD_TASK
    series.base.require_admin_key(admin_key)
    if _busy():
        return {"ok": True, "started": False, "reason": "Strict series task already running", **STATE}
    if not SELECTED or not STATE.get("ready_for_upload"):
        return {"ok": False, "started": False, "reason": "Run strict scan and confirm the full selected batch first."}
    UPLOAD_TASK = asyncio.create_task(_upload_worker(), name="strima-strict-upload")
    return {"ok": True, "started": True, "worker": "STRIMA Strict Series Batch", "selected_count": len(SELECTED), "size_limit": "unlimited"}


@app.post("/admin/telegram/series/strict/stop")
async def strict_stop(
    admin_key: Optional[str] = Header(default=None, alias="X-STRIMA-Admin-Key"),
):
    series.base.require_admin_key(admin_key)
    stopped = False
    for task in (SCAN_TASK, UPLOAD_TASK):
        if task is not None and not task.done():
            task.cancel()
            stopped = True
    STATE["running"] = False
    STATE["ready_for_upload"] = False
    STATE["phase"] = "stopped"
    return {"ok": True, "stopped": stopped}


@app.get("/admin/telegram/series/strict/status")
async def strict_status(
    admin_key: Optional[str] = Header(default=None, alias="X-STRIMA-Admin-Key"),
):
    series.base.require_admin_key(admin_key)
    return {
        "ok": True,
        "worker": "STRIMA Strict Series Batch",
        "source_channel": series.SERIES_SOURCE_TITLE,
        "size_limit": "unlimited",
        "episode_thumbnail_default": "series_banner_or_poster",
        "tmdb_episode_range_validation": True,
        "new_series_only": True,
        "task_running": _busy(),
        **STATE,
    }
