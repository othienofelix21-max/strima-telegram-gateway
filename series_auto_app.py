import asyncio
import logging
import re
from collections import Counter
from typing import Optional

from fastapi import Header, Query

import series_test_app as series

app = series.app
log = logging.getLogger("strima-series-auto-safe")

SCAN_TASK = None
UPLOAD_TASK = None
SELECTED = []
AUTO_STATE = {
    "running": False,
    "completed": False,
    "ready_for_upload": False,
    "phase": "idle",
    "batch_limit": 5,
    "scanned_messages": 0,
    "video_messages": 0,
    "discovered_candidates": 0,
    "tmdb_candidates_checked": 0,
    "tmdb_candidates_rejected": 0,
    "selected_series": [],
    "current_series": None,
    "current_index": 0,
    "completed_series": 0,
    "failed_series": 0,
    "series_results": [],
    "last_error": None,
}


def _clean_label(message) -> str:
    file_obj = getattr(message, "file", None)
    filename = str(getattr(file_obj, "name", "") or "").strip()
    caption = str(getattr(message, "message", "") or "").strip()
    raw = filename or (caption.splitlines()[0] if caption else "")
    raw = re.sub(r"\.(mkv|mp4|avi|mov|webm|m4v|ts)$", "", raw, flags=re.I)
    raw = raw.replace("_", " ").replace(".", " ")
    return re.sub(r"\s+", " ", raw).strip()


def _clean_title(value: str) -> str:
    value = str(value or "")
    value = re.sub(r"^[\s\W_]+", "", value, flags=re.UNICODE)
    value = re.sub(r"^(?:premium\s+series|series)\s*[:\-–—]\s*", "", value, flags=re.I)
    value = re.sub(r"\s+", " ", value).strip(" -–—_:;[]()")
    return value


def _valid_title(title: str) -> bool:
    t = str(title or "").strip()
    compact = re.sub(r"[^a-z0-9]", "", t.lower())
    if len(re.sub(r"[^A-Za-z]", "", t)) < 3:
        return False
    if len(t) > 100:
        return False
    if re.fullmatch(r"s\d{1,2}|season\s*\d{1,2}|e\d{1,3}|ep\d{1,3}|episode\s*\d{1,3}", t, flags=re.I):
        return False
    if compact in {"episode", "episodes", "season", "series", "premiumseries", "part", "video"}:
        return False
    return True


def _parse_episode(message):
    text = _clean_label(message)
    if not text:
        return None

    patterns = [
        r"^(?P<title>.+?)\s+[Ss]\s*0*(?P<season>\d{1,2})\s*[Ee]\s*0*(?P<ep>\d{1,3})\b",
        r"^(?P<title>.+?)\s+(?:Season\s*0*(?P<season>\d{1,2})\s+)?(?:Episode|Ep|E)\s*0*(?P<ep>\d{1,3})\b",
        r"^(?P<title>.+?)\s+0*(?P<ep>\d{1,3})\s*(?:[-–—]\s*)?(?:by\b|vj\b|ice\b|$)",
        r"^(?P<title>.+?)\s+0*(?P<ep>\d{1,3})\b",
    ]

    for pattern in patterns:
        m = re.search(pattern, text, flags=re.I)
        if not m:
            continue
        title = _clean_title(m.group("title"))
        if not _valid_title(title):
            continue
        ep = int(m.group("ep"))
        season_raw = m.groupdict().get("season")
        season = int(season_raw) if season_raw else 1
        if 1 <= ep <= 500 and 1 <= season <= 50:
            return title, season, ep
    return None


async def _discover_groups(source):
    groups = {}
    scanned = 0
    videos = 0

    async for message in series.base.client.iter_messages(source.input_entity):
        scanned += 1
        file_obj = getattr(message, "file", None)
        if not series.base.is_video_file(file_obj, message):
            continue
        videos += 1
        parsed = _parse_episode(message)
        if not parsed:
            continue
        title, season_number, ep = parsed
        slug = series._slugify(title)
        key = (slug, int(season_number))
        group = groups.setdefault(key, {
            "source_title": title,
            "source_titles": Counter(),
            "season_number": int(season_number),
            "episodes": {},
            "newest_message_id": 0,
        })
        group["source_titles"][title] += 1
        group["newest_message_id"] = max(group["newest_message_id"], int(message.id))
        group["episodes"].setdefault(int(ep), message)

    AUTO_STATE["scanned_messages"] = scanned
    AUTO_STATE["video_messages"] = videos

    candidates = []
    for group in groups.values():
        eps = sorted(group["episodes"])
        if len(eps) < 2:
            continue
        # A genuine series group should normally include its opening episodes.
        # This deliberately rejects fragment aliases such as a second naming style starting at episode 13.
        if min(eps) > 3:
            continue
        span = max(eps) - min(eps) + 1
        density = len(eps) / max(span, 1)
        if density < 0.55:
            continue
        group["source_title"] = group["source_titles"].most_common(1)[0][0]
        group["first_episode"] = min(eps)
        group["last_episode"] = max(eps)
        group["telegram_episode_count"] = len(eps)
        candidates.append(group)

    candidates.sort(key=lambda x: x["newest_message_id"], reverse=True)
    AUTO_STATE["discovered_candidates"] = len(candidates)
    return candidates


async def _tmdb_validate(source_title: str, season_number: int):
    if series.metadata.METADATA_PROVIDER != "tmdb" or not series.metadata.TMDB_BEARER_TOKEN:
        return None
    candidates = await series.metadata._tmdb_search_kind("tv", source_title, None)
    scored = sorted(
        ((series.metadata._score_tmdb_candidate(source_title, None, c), c) for c in candidates),
        key=lambda row: row[0],
        reverse=True,
    )
    if not scored or scored[0][0] < 78:
        return None
    score, candidate = scored[0]
    details = await series.metadata._tmdb_get(
        f"/tv/{int(candidate['id'])}",
        {"language": series.metadata.TMDB_LANGUAGE},
    )
    canonical_title = str(details.get("name") or candidate.get("name") or source_title).strip()
    if not _valid_title(canonical_title):
        return None
    season_details = None
    try:
        season_details = await series.metadata._tmdb_get(
            f"/tv/{int(candidate['id'])}/season/{season_number}",
            {"language": series.metadata.TMDB_LANGUAGE},
        )
    except Exception:
        log.exception("TMDB season lookup failed for %s", source_title)
    return {
        "score": int(score),
        "candidate": candidate,
        "details": details,
        "season_details": season_details,
        "tmdb_id": int(candidate["id"]),
        "canonical_title": canonical_title,
    }


async def _existing_episode_numbers(tmdb_id: int, slug: str):
    rows = await series.importer._rpc(
        "strima_gateway_series_episode_numbers_v1",
        {
            "p_admin_key": series.base.STRIMA_ADMIN_KEY,
            "p_tmdb_id": int(tmdb_id) if tmdb_id else None,
            "p_slug": slug,
        },
    )
    out = set()
    for row in rows if isinstance(rows, list) else []:
        try:
            out.add(int(row.get("episode_number")))
        except (TypeError, ValueError):
            pass
    return out


async def _scan_worker(limit: int):
    global SELECTED
    SELECTED = []
    AUTO_STATE.update({
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
        "selected_series": [],
        "current_series": None,
        "current_index": 0,
        "completed_series": 0,
        "failed_series": 0,
        "series_results": [],
        "last_error": None,
    })
    try:
        source = await series._resolve_series_source()
        AUTO_STATE["phase"] = "scanning_premium_series"
        candidates = await _discover_groups(source)
        AUTO_STATE["phase"] = "tmdb_validating"

        seen_tmdb = {}
        for group in candidates:
            if len(SELECTED) >= limit:
                break
            AUTO_STATE["tmdb_candidates_checked"] += 1
            match = await _tmdb_validate(group["source_title"], group["season_number"])
            if not match:
                AUTO_STATE["tmdb_candidates_rejected"] += 1
                continue

            tmdb_id = match["tmdb_id"]
            if tmdb_id in seen_tmdb:
                # Same real show under another Telegram spelling: merge episode messages.
                target = seen_tmdb[tmdb_id]
                for ep, msg in group["episodes"].items():
                    target["episodes"].setdefault(ep, msg)
                target["source_aliases"].add(group["source_title"])
                continue

            canonical_title = match["canonical_title"]
            slug = series._slugify(canonical_title)
            existing_eps = await _existing_episode_numbers(tmdb_id, slug)
            missing_eps = sorted(set(group["episodes"]) - existing_eps)
            if not missing_eps:
                continue

            item = {
                **group,
                **match,
                "slug": slug,
                "existing_episode_numbers": existing_eps,
                "missing_episode_numbers": missing_eps,
                "source_aliases": {group["source_title"]},
            }
            SELECTED.append(item)
            seen_tmdb[tmdb_id] = item

        AUTO_STATE["selected_series"] = [
            {
                "source_title": x["source_title"],
                "canonical_title": x["canonical_title"],
                "tmdb_id": x["tmdb_id"],
                "tmdb_score": x["score"],
                "season_number": x["season_number"],
                "telegram_episode_count": len(x["episodes"]),
                "already_in_supabase": len(x["existing_episode_numbers"]),
                "missing_to_upload": len(x["missing_episode_numbers"]),
                "first_episode": min(x["episodes"]),
                "last_episode": max(x["episodes"]),
            }
            for x in SELECTED
        ]
        AUTO_STATE["ready_for_upload"] = bool(SELECTED)
        AUTO_STATE["completed"] = True
        AUTO_STATE["phase"] = "ready_for_confirmation" if SELECTED else "nothing_to_upload"
    except asyncio.CancelledError:
        AUTO_STATE["phase"] = "stopped"
        raise
    except Exception as exc:
        AUTO_STATE["phase"] = "error"
        AUTO_STATE["last_error"] = f"{type(exc).__name__}: {str(exc)[:500]}"
        log.exception("Safe series scan failed")
    finally:
        AUTO_STATE["running"] = False


async def _process_one(item):
    source = await series._resolve_series_source()
    source_channel_id = int(source.id)
    existing_eps = set(item["existing_episode_numbers"])
    copied = 0
    registered_count = 0
    skipped = 0

    for ep in sorted(item["episodes"]):
        if ep in existing_eps:
            skipped += 1
            continue
        message = item["episodes"][ep]
        AUTO_STATE["phase"] = f"{item['canonical_title']}_episode_{ep}_copying"

        existing_source = await series._episode_lookup(source_channel_id, int(message.id))
        if existing_source:
            skipped += 1
            continue

        destination = await series._copy_message(message)
        copied += 1
        registered = await series._register_episode(
            source_channel_id,
            message,
            destination,
            int(ep),
            item["canonical_title"],
            item["slug"],
            int(item["season_number"]),
        )
        if not registered:
            raise RuntimeError(f"Supabase returned no row for {item['canonical_title']} episode {ep}")
        registered_count += 1

        await series._enrich_series_and_episode(
            registered,
            int(ep),
            item["candidate"],
            item["details"],
            item["season_details"],
            int(item["season_number"]),
        )
        await asyncio.sleep(1)

    return {
        "title": item["canonical_title"],
        "tmdb_id": item["tmdb_id"],
        "copied": copied,
        "registered": registered_count,
        "already_present": skipped,
        "failed": 0,
    }


async def _upload_worker():
    AUTO_STATE.update({
        "running": True,
        "completed": False,
        "ready_for_upload": False,
        "phase": "uploading_selected_series",
        "current_series": None,
        "current_index": 0,
        "completed_series": 0,
        "failed_series": 0,
        "series_results": [],
        "last_error": None,
    })
    try:
        for index, item in enumerate(SELECTED, start=1):
            AUTO_STATE["current_index"] = index
            AUTO_STATE["current_series"] = item["canonical_title"]
            try:
                result = await _process_one(item)
                AUTO_STATE["series_results"].append(result)
                AUTO_STATE["completed_series"] += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                AUTO_STATE["failed_series"] += 1
                AUTO_STATE["series_results"].append({
                    "title": item["canonical_title"],
                    "tmdb_id": item["tmdb_id"],
                    "failed": 1,
                    "last_error": f"{type(exc).__name__}: {str(exc)[:400]}",
                })
                log.exception("Series upload failed for %s", item["canonical_title"])

        AUTO_STATE["current_series"] = None
        AUTO_STATE["phase"] = "complete"
        AUTO_STATE["completed"] = True
    except asyncio.CancelledError:
        AUTO_STATE["phase"] = "stopped"
        raise
    except Exception as exc:
        AUTO_STATE["phase"] = "error"
        AUTO_STATE["last_error"] = f"{type(exc).__name__}: {str(exc)[:500]}"
    finally:
        AUTO_STATE["running"] = False


def _busy():
    return bool(
        (SCAN_TASK is not None and not SCAN_TASK.done())
        or (UPLOAD_TASK is not None and not UPLOAD_TASK.done())
    )


@app.post("/admin/telegram/series/auto/scan")
async def scan_auto_series_batch(
    limit: int = Query(default=5, ge=1, le=5),
    admin_key: Optional[str] = Header(default=None, alias="X-STRIMA-Admin-Key"),
):
    global SCAN_TASK
    series.base.require_admin_key(admin_key)
    if _busy():
        return {"ok": True, "started": False, "reason": "Auto series task is already running", **AUTO_STATE}
    SCAN_TASK = asyncio.create_task(_scan_worker(limit), name=f"strima-series-safe-scan-{limit}")
    return {"ok": True, "started": True, "mode": "scan_only", "batch_limit": limit, "note": "No episodes will be copied until /auto/upload/start is called."}


# Backward-compatible endpoint is deliberately scan-only now for safety.
@app.post("/admin/telegram/series/auto/start")
async def legacy_auto_start(
    limit: int = Query(default=5, ge=1, le=5),
    admin_key: Optional[str] = Header(default=None, alias="X-STRIMA-Admin-Key"),
):
    return await scan_auto_series_batch(limit=limit, admin_key=admin_key)


@app.post("/admin/telegram/series/auto/upload/start")
async def upload_auto_series_batch(
    admin_key: Optional[str] = Header(default=None, alias="X-STRIMA-Admin-Key"),
):
    global UPLOAD_TASK
    series.base.require_admin_key(admin_key)
    if _busy():
        return {"ok": True, "started": False, "reason": "Auto series task is already running", **AUTO_STATE}
    if not SELECTED or not AUTO_STATE.get("ready_for_upload"):
        return {"ok": False, "started": False, "reason": "Run /admin/telegram/series/auto/scan first and confirm the selected titles."}
    UPLOAD_TASK = asyncio.create_task(_upload_worker(), name="strima-series-safe-upload")
    series.TASK = UPLOAD_TASK
    return {"ok": True, "started": True, "worker": "STRIMA Safe Auto Series Batch", "selected_count": len(SELECTED), "size_limit": "unlimited"}


@app.post("/admin/telegram/series/auto/stop")
async def stop_auto_series_batch(
    admin_key: Optional[str] = Header(default=None, alias="X-STRIMA-Admin-Key"),
):
    series.base.require_admin_key(admin_key)
    stopped = False
    for task in (SCAN_TASK, UPLOAD_TASK):
        if task is not None and not task.done():
            task.cancel()
            stopped = True
    if series.TASK in (SCAN_TASK, UPLOAD_TASK):
        series.TASK = None
    AUTO_STATE["running"] = False
    AUTO_STATE["ready_for_upload"] = False
    AUTO_STATE["phase"] = "stopped"
    return {"ok": True, "stopped": stopped}


@app.get("/admin/telegram/series/auto/status")
async def auto_series_batch_status(
    admin_key: Optional[str] = Header(default=None, alias="X-STRIMA-Admin-Key"),
):
    series.base.require_admin_key(admin_key)
    return {
        "ok": True,
        "worker": "STRIMA Safe Auto Series Batch",
        "source_channel": series.SERIES_SOURCE_TITLE,
        "size_limit": "unlimited",
        "episode_thumbnail_default": "series_banner_or_poster",
        "tmdb_enabled": series.metadata.METADATA_PROVIDER == "tmdb" and bool(series.metadata.TMDB_BEARER_TOKEN),
        "task_running": _busy(),
        **AUTO_STATE,
    }
