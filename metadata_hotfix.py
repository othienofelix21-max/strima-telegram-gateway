import asyncio
import math

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

            # Keep the existing broad scoring idea, but avoid collapsing all
            # close candidates into an indistinguishable 100-point tie.
            if ratio <= 0.10:
                row[0] += 22
            elif ratio <= 0.15:
                row[0] += 18
            elif ratio <= 0.30:
                row[0] += 8
            elif ratio >= 0.35:
                row[0] -= 25

            # A 3+ part source family is compatible with a TV/miniseries source,
            # but runtime closeness remains the stronger signal.
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
        # Higher score first; among ties choose runtime closest to the complete
        # split-family duration; then use popularity only as a final tie-break.
        return (-score, runtime_distance, -popularity)

    combined.sort(key=rank_key)
    if not combined:
        return 0, None, None, evidence, None

    best = combined[0]
    return best[0], best[1], best[2], evidence, best[3]


m._tmdb_find_best_split = _tmdb_find_best_split_runtime_tiebreak
app = m.app
