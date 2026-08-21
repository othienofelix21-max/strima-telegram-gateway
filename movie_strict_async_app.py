import asyncio
from typing import Optional

from fastapi import Header, HTTPException, Query

import series_guarded_app as guarded
import register_existing_app as reg
import app as base

app = guarded.app

SCAN_TASK = None
SCAN_ERROR = None


# Replace the synchronous strict scan/status routes with background-safe versions.
for route in list(app.router.routes):
    path = getattr(route, "path", None)
    methods = set(getattr(route, "methods", set()) or set())
    if path == "/admin/telegram/movies/strict/scan" and "POST" in methods:
        app.router.routes.remove(route)
    elif path == "/admin/telegram/movies/strict/status" and "GET" in methods:
        app.router.routes.remove(route)


async def _scan_worker(limit: int):
    global SCAN_ERROR
    SCAN_ERROR = None
    try:
        await reg._scan_strict_movies(limit)
    except Exception as exc:
        SCAN_ERROR = f"{type(exc).__name__}: {str(exc)[:500]}"


@app.post("/admin/telegram/movies/strict/scan")
async def strict_movie_scan_background(
    limit: int = Query(default=100, ge=1, le=200),
    admin_key: Optional[str] = Header(default=None, alias="X-STRIMA-Admin-Key"),
):
    global SCAN_TASK, SCAN_ERROR
    base.require_admin_key(admin_key)

    if reg.TASK is not None and not reg.TASK.done():
        raise HTTPException(status_code=409, detail="Another movie registration task is running")
    if reg.STRICT_UPLOAD_TASK is not None and not reg.STRICT_UPLOAD_TASK.done():
        raise HTTPException(status_code=409, detail="Strict movie upload is running")
    if SCAN_TASK is not None and not SCAN_TASK.done():
        return {
            "ok": True,
            "started": False,
            "reason": "Strict movie scan is already running",
            "requested": reg.STRICT_SCAN.get("requested", limit),
            "ready_count": len(reg.STRICT_SCAN.get("ready", [])),
        }

    SCAN_ERROR = None
    SCAN_TASK = asyncio.create_task(_scan_worker(limit), name="strima-strict-movie-scan")
    return {
        "ok": True,
        "started": True,
        "requested": int(limit),
        "message": "Strict preflight scan started in background. Poll /admin/telegram/movies/strict/status.",
    }


@app.get("/admin/telegram/movies/strict/status")
async def strict_movie_status_background(
    admin_key: Optional[str] = Header(default=None, alias="X-STRIMA-Admin-Key"),
):
    base.require_admin_key(admin_key)
    scan_running = bool(SCAN_TASK is not None and not SCAN_TASK.done())
    return {
        "ok": True,
        "scan_running": scan_running,
        "scan_error": SCAN_ERROR,
        "ready_count": len(reg.STRICT_SCAN.get("ready", [])),
        "upload_running": bool(reg.STRICT_UPLOAD_TASK is not None and not reg.STRICT_UPLOAD_TASK.done()),
        **reg.STRICT_SCAN,
        "upload_state": reg.STATE,
    }
