import asyncio
import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

from fastapi import Header, HTTPException, Query
from fastapi.responses import JSONResponse

import import_app as importer

app = importer.app
base = importer.base
log = logging.getLogger("strima-metadata")

METADATA_PROVIDER = os.getenv("METADATA_PROVIDER", "telegram").strip().lower()
TMDB_BEARER_TOKEN = os.getenv("TMDB_BEARER_TOKEN", "").strip()
TMDB_LANGUAGE = os.getenv("TMDB_LANGUAGE", "en-US").strip() or "en-US"

GENRE_TO_CATEGORY = {
    28: "action",
    16: "animation",
    35: "comedy",
    18: "drama",
    27: "horror",
    10749: "romance",
}

_original_source_item_from_message = base.source_item_from_message
_original_clean_public_title = importer._clean_public_title


def _strip_trailing_vj_jr(text: str) -> str:
    value = str(text or "").strip()
    value = re.sub(
        r"\s+(?:VJ[\s._-]*)?(?:JR|JUNIOR)(?=\s*(?:19\d{2}|20\d{2})?\s*$)",
        " ",
        value,
        flags=re.IGNORECASE,
    )
    return re.sub(r"\s+", " ", value).strip(" -_()[]")


def _source_item_with_aliases(message) -> dict:
    item = _original_source_item_from_message(message)
    raw_title = str(item.get("detected_title") or item.get("file_name") or "")
    if not item.get("detected_vj_name"):
        if re.search(
            r"\b(?:VJ[\s._-]*)?(?:JR|JUNIOR)(?=\s*(?:19\d{2}|20\d{2})?\s*$)",
            raw_title,
            flags=re.IGNORECASE,
        ):
            item["detected_vj_name"] = "VJ JR"
            cleaned_for_compare = _strip_trailing_vj_jr(raw_title)
            item["normalized_title"] = base.normalize_title_for_compare(cleaned_for_compare)
    return item


def _clean_public_title_with_aliases(item: dict) -> str:
    title = _original_clean_public_title(item)
    if str(item.get("detected_vj_name") or "").upper() == "VJ JR":
        title = _strip_trailing_vj_jr(title)
    return title


base.source_item_from_message = _source_item_with_aliases
importer._clean_public_title = _clean_public_title_with_aliases


def _tmdb_get_sync(path: str, params: Optional[dict] = None):
    if not TMDB_BEARER_TOKEN:
        raise RuntimeError("TMDB_BEARER_TOKEN is not configured")
    query = urllib.parse.urlencode(params or {})
    url = f"https://api.themoviedb.org/3{path}"
    if query:
        url = f"{url}?{query}"
    req = urllib.request.Request(
        url,
        method="GET",
        headers={
            "Authorization": f"Bearer {TMDB_BEARER_TOKEN}",
            "Accept": "application/json",
            "User-Agent": "STRIMA-Metadata/1.1",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"TMDB HTTP {exc.code}: {body[:300]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"TMDB connection failed: {exc.reason}") from exc


async def _tmdb_get(path: str, params: Optional[dict] = None):
    return await asyncio.to_thread(_tmdb_get_sync, path, params)


def _candidate_year(candidate: dict) -> Optional[int]:
    date_value = str(candidate.get("release_date") or candidate.get("first_air_date") or "")
    if len(date_value) >= 4 and date_value[:4].isdigit():
        return int(date_value[:4])
    return None


def _candidate_title(candidate: dict) -> str:
    return str(
        candidate.get("title")
        or candidate.get("original_title")
        or candidate.get("name")
        or candidate.get("original_name")
        or ""
    ).strip()


def _normalize_tmdb_title(text: str) -> str:
    value = base.normalize_title_for_compare(str(text or ""))
    value = re.sub(r"\b(chapter|chap|volume|vol)\b", " ", value, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", value).strip()


def _score_tmdb_candidate(requested_title: str, expected_year: Optional[int], candidate: dict) -> int:
    requested = _normalize_tmdb_title(requested_title)
    candidate_title = _normalize_tmdb_title(_candidate_title(candidate))
    score = 0
    if requested and candidate_title:
        if requested == candidate_title:
            score += 82
        elif requested in candidate_title or candidate_title in requested:
            score += 58
        else:
            requested_words = set(requested.split())
            candidate_words = set(candidate_title.split())
            if requested_words and candidate_words:
                overlap = len(requested_words & candidate_words) / max(len(requested_words), len(candidate_words))
                score += int(overlap * 50)
    found_year = _candidate_year(candidate)
    if expected_year and found_year:
        if int(expected_year) == int(found_year):
            score += 15
        elif abs(int(expected_year) - int(found_year)) == 1:
            score += 6
        else:
            score -= 15
    try:
        if float(candidate.get("popularity") or 0) > 5:
            score += 3
    except (TypeError, ValueError):
        pass
    return max(0, min(100, score))


def _category_from_genres(candidate: dict) -> Optional[str]:
    for genre_id in candidate.get("genre_ids") or []:
        try:
            category = GENRE_TO_CATEGORY.get(int(genre_id))
        except (TypeError, ValueError):
            category = None
        if category:
            return category
    return None


def _tmdb_patch(candidate: dict) -> dict:
    poster_path = candidate.get("poster_path")
    backdrop_path = candidate.get("backdrop_path")
    return {
        "year": _candidate_year(candidate),
        "description": str(candidate.get("overview") or "").strip() or None,
        "poster_url": f"https://image.tmdb.org/t/p/w500{poster_path}" if poster_path else None,
        "banner_url": f"https://image.tmdb.org/t/p/original{backdrop_path}" if backdrop_path else None,
        "thumbnail_url": f"https://image.tmdb.org/t/p/w500{poster_path}" if poster_path else None,
        "category_slug": _category_from_genres(candidate),
    }


def _split_part_alias(title: str, duration_minutes: Optional[int]):
    value = re.sub(r"\s+", " ", str(title or "")).strip()
    match = re.fullmatch(r"(.+?)\s+(\d{1,2})", value)
    if not match:
        return None, None
    try:
        minutes = int(duration_minutes) if duration_minutes is not None else None
    except (TypeError, ValueError):
        minutes = None
    if minutes is None or minutes > 75:
        return None, None
    part_number = int(match.group(2))
    if part_number < 1 or part_number > 20:
        return None, None
    alias = match.group(1).strip(" -_()[]")
    if len(alias) < 3:
        return None, None
    return alias, part_number


async def _tmdb_search_kind(kind: str, query_title: str, expected_year: Optional[int]):
    params = {
        "query": query_title,
        "include_adult": "false",
        "language": TMDB_LANGUAGE,
        "page": 1,
    }
    if expected_year:
        if kind == "movie":
            params["primary_release_year"] = int(expected_year)
        else:
            params["first_air_date_year"] = int(expected_year)
    response = await _tmdb_get(f"/search/{kind}", params)
    candidates = response.get("results") if isinstance(response, dict) else []
    return candidates if isinstance(candidates, list) else []


async def _tmdb_find_best(query_title: str, expected_year: Optional[int]):
    movie_candidates, tv_candidates = await asyncio.gather(
        _tmdb_search_kind("movie", query_title, expected_year),
        _tmdb_search_kind("tv", query_title, expected_year),
    )
    combined = []
    for candidate in movie_candidates:
        combined.append((_score_tmdb_candidate(query_title, expected_year, candidate), candidate, "movie"))
    for candidate in tv_candidates:
        combined.append((_score_tmdb_candidate(query_title, expected_year, candidate), candidate, "tv"))
    combined.sort(key=lambda row: row[0], reverse=True)
    return combined[0] if combined else (0, None, None)


async def _metadata_target(movie_id: str) -> Optional[dict]:
    rows = await importer._rpc(
        "strima_gateway_metadata_target",
        {"p_admin_key": base.STRIMA_ADMIN_KEY, "p_movie_id": movie_id},
    )
    return rows[0] if isinstance(rows, list) and rows else None


async def _apply_metadata(movie_id: str, patch: dict):
    rows = await importer._rpc(
        "strima_gateway_update_metadata",
        {
            "p_admin_key": base.STRIMA_ADMIN_KEY,
            "p_movie_id": movie_id,
            "p_title": patch.get("title"),
            "p_year": patch.get("year"),
            "p_description": patch.get("description"),
            "p_poster_url": patch.get("poster_url"),
            "p_banner_url": patch.get("banner_url"),
            "p_thumbnail_url": patch.get("thumbnail_url"),
            "p_category_slug": patch.get("category_slug"),
            "p_vj_name": patch.get("vj_name"),
        },
    )
    return rows[0] if isinstance(rows, list) and rows else None


async def _telegram_local_patch(source_message_id: int) -> dict:
    if base.SOURCE_INPUT_ENTITY is None:
        return {}
    try:
        message = await base.client.get_messages(base.SOURCE_INPUT_ENTITY, ids=source_message_id)
    except Exception:
        log.exception("Metadata Telegram lookup failed for source message %s", source_message_id)
        return {}
    if not message or not getattr(message, "file", None):
        return {}
    item = base.source_item_from_message(message)
    public_title = importer._clean_public_title(item)
    return {
        "title": public_title or None,
        "year": item.get("detected_year"),
        "description": importer._description_from_item(item, public_title),
        "vj_name": item.get("detected_vj_name"),
        "duration_minutes": int(round(int(item["duration_seconds"]) / 60)) if item.get("duration_seconds") else None,
    }


@app.get("/admin/metadata/status")
async def metadata_status(
    admin_key: Optional[str] = Header(default=None, alias="X-STRIMA-Admin-Key"),
):
    base.require_admin_key(admin_key)
    return {
        "ok": True,
        "provider": METADATA_PROVIDER,
        "telegram_cleanup": True,
        "tmdb_adapter_available": True,
        "tmdb_configured": bool(TMDB_BEARER_TOKEN),
        "tmdb_searches_movie_and_tv": True,
        "split_part_alias_fallback": True,
    }


@app.post("/admin/metadata/enrich-source/{source_message_id}")
async def enrich_source_movie(
    source_message_id: int,
    apply: bool = Query(default=True),
    admin_key: Optional[str] = Header(default=None, alias="X-STRIMA-Admin-Key"),
):
    base.require_admin_key(admin_key)
    existing = await importer._source_lookup(source_message_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Source movie is not registered in Supabase")
    movie_id = str(existing.get("movie_id"))
    movie = await _metadata_target(movie_id)
    if not movie:
        raise HTTPException(status_code=404, detail="Movie metadata target was not found")

    local_patch = await _telegram_local_patch(source_message_id)
    patch = {
        key: value
        for key, value in local_patch.items()
        if key != "duration_minutes" and value not in (None, "")
    }

    provider_result = {
        "provider": METADATA_PROVIDER,
        "configured": False,
        "matched": False,
        "confidence": 0.0,
        "external_id": None,
        "external_media_type": None,
        "candidate_title": None,
        "query_title": None,
        "source_part_number": None,
        "reason": "Telegram metadata cleanup only; no external provider is configured.",
    }

    if METADATA_PROVIDER == "tmdb" and TMDB_BEARER_TOKEN:
        provider_result["configured"] = True
        requested_title = str(movie.get("title") or local_patch.get("title") or "").strip()
        expected_year = movie.get("release_year") or local_patch.get("year")

        best_score, best, best_kind = await _tmdb_find_best(requested_title, expected_year)
        used_query = requested_title
        split_part_number = None

        if best_score < 70:
            alias, split_part_number = _split_part_alias(
                requested_title,
                local_patch.get("duration_minutes") or movie.get("duration_minutes"),
            )
            if alias:
                alias_score, alias_best, alias_kind = await _tmdb_find_best(alias, expected_year)
                if alias_score > best_score:
                    best_score, best, best_kind = alias_score, alias_best, alias_kind
                    used_query = alias

        provider_result["query_title"] = used_query
        provider_result["source_part_number"] = split_part_number

        if best and best_score >= 70:
            external_patch = _tmdb_patch(best)
            for key, value in external_patch.items():
                if value not in (None, ""):
                    patch[key] = value
            reason = "High-confidence title/year match."
            if used_query != requested_title:
                reason = "High-confidence match using a short-file split-part alias; the STRIMA source title is preserved."
            provider_result.update({
                "matched": True,
                "confidence": round(best_score / 100.0, 2),
                "external_id": best.get("id"),
                "external_media_type": best_kind,
                "candidate_title": _candidate_title(best) or None,
                "reason": reason,
            })
        elif best:
            provider_result.update({
                "matched": False,
                "confidence": round(best_score / 100.0, 2),
                "external_id": best.get("id"),
                "external_media_type": best_kind,
                "candidate_title": _candidate_title(best) or None,
                "reason": "Possible movie/TV match found, but confidence is below the automatic-apply threshold.",
            })
        else:
            provider_result["reason"] = "No external movie or TV match found."
    elif METADATA_PROVIDER == "tmdb":
        provider_result["reason"] = "TMDB adapter selected but TMDB_BEARER_TOKEN is not configured."

    applied_movie = None
    if apply and patch:
        applied_movie = await _apply_metadata(movie_id, patch)

    return {
        "ok": True,
        "source_message_id": source_message_id,
        "movie_id": movie_id,
        "before": movie,
        "patch": patch,
        "provider": provider_result,
        "applied": bool(apply and patch),
        "movie": applied_movie,
    }
