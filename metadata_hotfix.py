import asyncio
import math
import re
from typing import Optional

from fastapi import Header, HTTPException, Query

import metadata_app as m


async def _tmdb_find_best_split_runtime_tiebreak(
    query_title: str,
    expected_year,
    source_message_id: int,
):
    """Prefer the closest-runtime adaptation when split-family candidates tie.

    This keeps the existing score system, but records each candidate's runtime
    difference from the aggregate duration of nearby source parts and uses that
    difference as a deterministic secondary ranking signal. This prevents an
    older movie and a TV/miniseries with the same title from tying at 100 and
    being chosen only because movie candidates were appended first.
    """
    movie_candidates, tv_candidates = await asyncio.gather(
        m._tmdb_search_kind("movie", query_title, expected_year),
        m._tmdb_search_kind("tv", query_title, expected_year),
    )
    evidence = await m._split_family_evidence(source_message_id, query_title)
    aggregate = evidence.get("aggregate_minutes")
    parts = evidence.get("distinct_parts") or []

    combined = []
    for candidate in movie_candidates[:5]:
        combined.append([
            m._score_tmdb_candidate(query_title, expected_year, candidate),
            candidate,
            "movie",
            None,
            None,
        ])
    for candidate in tv_candidates[:5]:
        combined.append([
            m._score_tmdb_candidate(query_title, expected_year, candidate),
            candidate,
            "tv",
            None,
            None,
        ])

    if aggregate and len(parts) >= 2:
        plausible = [row for row in combined if row[0] >= 55]
        for row in plausible:
            runtime = await m._tmdb_total_runtime_minutes(row[2], row[1])
            row[3] = runtime
            if not runtime:
                continue

            diff = abs(int(runtime) - int(aggregate))
            row[4] = diff
            ratio = diff / max(1, int(aggregate))

            if ratio <= 0.10:
                row[0] += 22
            elif ratio <= 0.15:
                row[0] += 18
            elif ratio <= 0.30:
                row[0] += 8
            elif ratio >= 0.35:
                row[0] -= 25

            if row[2] == "tv" and len(parts) >= 3 and ratio <= 0.20:
                row[0] += 5

            row[0] = max(0, min(100, row[0]))

    def rank_key(row):
        score, candidate, kind, runtime, diff = row
        runtime_distance = diff if diff is not None else math.inf
        try:
            popularity = float(candidate.get("popularity") or 0)
        except (TypeError, ValueError):
            popularity = 0.0
        return (-score, runtime_distance, -popularity)

    combined.sort(key=rank_key)
    if not combined:
        return 0, None, None, evidence, None

    best = combined[0]
    return best[0], best[1], best[2], evidence, best[3]


m._tmdb_find_best_split = _tmdb_find_best_split_runtime_tiebreak
app = m.app


def _fallback_part_alias(title: str):
    """Recognize Telegram split titles such as 'Helen Of Troy 1' even when duration is missing."""
    value = re.sub(r"\s+", " ", str(title or "")).strip()
    match = re.fullmatch(r"(.+?)\s+(\d{1,2})", value)
    if not match:
        return None, None
    part = int(match.group(2))
    if part < 1 or part > 20:
        return None, None
    alias = match.group(1).strip(" -_()[]")
    if len(alias) < 3:
        return None, None
    return alias, part


async def _best_with_year_retry(query_title: str, expected_year):
    """Search with the detected year first, then retry without it if Telegram supplied a bad year."""
    score, candidate, kind = await m._tmdb_find_best(query_title, expected_year)
    used_year = expected_year
    if score < 70 and expected_year:
        retry_score, retry_candidate, retry_kind = await m._tmdb_find_best(query_title, None)
        if retry_score > score:
            score, candidate, kind = retry_score, retry_candidate, retry_kind
            used_year = None
    return score, candidate, kind, used_year


@app.post("/admin/metadata/enrich-source-smart/{source_message_id}")
async def enrich_source_movie_smart(
    source_message_id: int,
    apply: bool = Query(default=True),
    admin_key: Optional[str] = Header(default=None, alias="X-STRIMA-Admin-Key"),
):
    """Metadata enrichment that recovers from stale DB titles and incorrect detected years."""
    m.base.require_admin_key(admin_key)

    existing = await m.importer._source_lookup(source_message_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Source movie is not registered in Supabase")

    movie_id = str(existing.get("movie_id"))
    movie = await m._metadata_target(movie_id)
    if not movie:
        raise HTTPException(status_code=404, detail="Movie metadata target was not found")

    local_patch = await m._telegram_local_patch(source_message_id)
    patch = {
        key: value
        for key, value in local_patch.items()
        if key != "duration_minutes" and value not in (None, "")
    }

    requested_title = str(
        local_patch.get("title")
        or movie.get("source_normalized_title")
        or movie.get("title")
        or ""
    ).strip()
    requested_title = m._strip_trailing_vj_jr(requested_title)
    expected_year = local_patch.get("year") or movie.get("release_year")

    best_score, best, best_kind, used_year = await _best_with_year_retry(requested_title, expected_year)
    used_query = requested_title
    split_part_number = None
    family_evidence = {"distinct_parts": [], "aggregate_minutes": None}
    matched_runtime = None

    duration_minutes = local_patch.get("duration_minutes") or movie.get("duration_minutes")
    alias, split_part_number = m._split_part_alias(requested_title, duration_minutes)
    if not alias:
        alias, split_part_number = _fallback_part_alias(requested_title)

    if alias:
        alias_score, alias_best, alias_kind, family_evidence, matched_runtime = await m._tmdb_find_best_split(
            alias,
            expected_year,
            source_message_id,
        )
        if alias_score < 70 and expected_year:
            retry_score, retry_best, retry_kind, retry_evidence, retry_runtime = await m._tmdb_find_best_split(
                alias,
                None,
                source_message_id,
            )
            if retry_score > alias_score:
                alias_score, alias_best, alias_kind = retry_score, retry_best, retry_kind
                family_evidence, matched_runtime = retry_evidence, retry_runtime
                used_year = None
        if alias_score > best_score:
            best_score, best, best_kind = alias_score, alias_best, alias_kind
            used_query = alias

    provider_result = {
        "provider": m.METADATA_PROVIDER,
        "configured": bool(m.TMDB_BEARER_TOKEN),
        "matched": False,
        "confidence": round(best_score / 100.0, 2) if best_score else 0.0,
        "external_id": None,
        "external_media_type": None,
        "candidate_title": None,
        "query_title": used_query,
        "requested_year": expected_year,
        "search_year_used": used_year,
        "source_part_number": split_part_number,
        "split_family_parts": family_evidence.get("distinct_parts") or [],
        "split_family_duration_minutes": family_evidence.get("aggregate_minutes"),
        "matched_runtime_minutes": matched_runtime,
        "reason": "No external movie or TV match found.",
    }

    if m.METADATA_PROVIDER == "tmdb" and m.TMDB_BEARER_TOKEN and best and best_score >= 70:
        external_patch = m._tmdb_patch(best)
        for key, value in external_patch.items():
            if value not in (None, ""):
                patch[key] = value
        provider_result.update({
            "matched": True,
            "confidence": round(best_score / 100.0, 2),
            "external_id": best.get("id"),
            "external_media_type": best_kind,
            "candidate_title": m._candidate_title(best) or None,
            "reason": "High-confidence smart match; retried without a bad year and/or used a cleaned split-title alias when needed.",
        })
    elif best:
        provider_result.update({
            "external_id": best.get("id"),
            "external_media_type": best_kind,
            "candidate_title": m._candidate_title(best) or None,
            "reason": "Possible match found, but confidence is below the automatic-apply threshold.",
        })

    applied_movie = None
    if apply and patch:
        applied_movie = await m._apply_metadata(movie_id, patch)

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
