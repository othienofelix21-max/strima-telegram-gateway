# STRIMA strict movie preflight

Production-safe flow:

1. `POST /admin/telegram/movies/strict/scan?limit=100`
2. Review `ready`, `ready_for_upload`, and blocked/error counters.
3. Only when `ready_for_upload=true`, run `POST /admin/telegram/movies/strict/upload/start`.
4. Monitor with `GET /admin/telegram/movies/strict/status`.

The scan is read-only with respect to movie registration. It validates source/destination matching, duplicates, content type, filename, TMDB confidence, artwork, and proposes playback/download URLs before registration.
