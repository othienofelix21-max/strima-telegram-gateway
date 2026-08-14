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

# Keep the external provider behind a small adapter so STRIMA can switch
# providers later without changing the Telegram importer or Supabase schema.
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
    # Some source files use only "JR" rather than the explicit "VJ JR" label.
    # Strip it only when it is a final source tag so normal movie words are safe.
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


# Apply the compatibility patch to all future imports in this process.
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
            "User-Agent": "STRIMA-Metadata/1.0",
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
    release_date = str(candidate.get("release_date") or "")
    if len(release_date) >= 4 and release_date[:4].isdigit():
        return int(release_date[:4])
    return None


def _score_tmdb_candidate(movie: dict, candidate: dict) -> int:
    requested = base.normalize_title_for_compare(str(movie.get("title") or ""))
    candidate_title = base.normalize_title_for_compare(str(candidate.get("title") or candidate.get("original_title") or ""))

    score = 0
    if requested and candidate_title:
        if requested == candidate_title:
            score += 75
        elif requested in candidate_title or candidate_title in requested:
            score += 52
        else:
            requested_words = set(requested.split())
            candidate_words = set(candidate_title.split())
            if requested_words and candidate_words:
                overlap = len(requested_words & candidate_words) / max(len(requested_words), len(candidate_words))
                score += int(overlap * 45)

    expected_year = movie.get("release_year")
    found_year = _candidate_year(candidate)
    if expected_year and found_year:
        if int(expected_year) == int(found_year):
            score += 20
        elif abs(int(expected_year) - int(found_year)) == 1:
            score += 8
        else:
            score -= 12

    popularity = candidate.get("popularity")
    try:
        if float(popularity or 0) > 5:
            score += 5
    except (TypeError, ValueError):
        pass

    return max(0, min(100, score))


def _category_from_genres(candidate: dict) -> Optional[str]:
    genre_ids = candidate.get("genre_ids") or []
    for genre_id in genre_ids:
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
    year = _candidate_year(candidate)
    return {
        "year": year,
        "description": str(candidate.get("overview") or "").strip() or None,
        "poster_url": f"https://image.tmdb.org/t/p/w500{poster_path}" if poster_path else None,
        "banner_url": f"https://image.tmdb.org/t/p/original{backdrop_path}" if backdrop_path else None,
        "thumbnail_url": f"https://image.tmdb.org/t/p/w500{poster_path}" if poster_path else None,
        "category_slug": _category_from_genres(candidate),
    }


async def _metadata_target(movie_id: str) -> Optional[dict]:
    rows = await importer._rpc(
        "strima_gateway_metadata_target",
        {
            "p_admin_key": base.STRIMA_ADMIN_KEY,
            "p_movie_id": movie_id,
        },
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
    patch = {key: value for key, value in local_patch.items() if value not in (None, "")}

    provider_result = {
        "provider": METADATA_PROVIDER,
        "configured": False,
        "matched": False,
        "confidence": 0.0,
        "external_id": None,
        "candidate_title": None,
        "reason": "Telegram metadata cleanup only; no external provider is configured.",
    }

    if METADATA_PROVIDER == "tmdb" and TMDB_BEARER_TOKEN:
        provider_result["configured"] = True
        params = {
            "query": movie.get("title") or local_patch.get("title") or "",
            "include_adult": "false",
            "language": TMDB_LANGUAGE,
            "page": 1,
        }
        if movie.get("release_year"):
            params["primary_release_year"] = int(movie["release_year"])

        response = await _tmdb_get("/search/movie", params)
        candidates = response.get("results") if isinstance(response, dict) else []
        candidates = candidates if isinstance(candidates, list) else []

        scored = [(_score_tmdb_candidate(movie, candidate), candidate) for candidate in candidates]
        scored.sort(key=lambda pair: pair[0], reverse=True)
        best_score, best = scored[0] if scored else (0, None)

        if best and best_score >= 70:
            external_patch = _tmdb_patch(best)
            for key, value in external_patch.items():
                if value not in (None, ""):
                    patch[key] = value
            provider_result.update({
                "matched": True,
                "confidence": round(best_score / 100.0, 2),
                "external_id": best.get("id"),
                "candidate_title": best.get("title") or best.get("original_title"),
                "reason": "High-confidence title/year match.",
            })
        elif best:
            provider_result.update({
                "matched": False,
                "confidence": round(best_score / 100.0, 2),
                "external_id": best.get("id"),
                "candidate_title": best.get("title") or best.get("original_title"),
                "reason": "Possible match found, but confidence is below the automatic-apply threshold.",
            })
        else:
            provider_result["reason"] = "No external movie match found."
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
