import asyncio
import json
import logging
import os
import socket
import uuid
import urllib.request
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import Header

import app as base
import import_app as wrapped

app = wrapped.app
log = logging.getLogger("strima-single-owner")

OWNER_ID = f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
STATE = {"worker": "starting", "owns_lease": False, "lease_until": None, "last_error": None}
STOP = None
TASK = None


def rpc_sync(name, payload):
    url = f"{base.SUPABASE_URL}/rest/v1/rpc/{name}"
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "apikey": base.SUPABASE_PUBLISHABLE_KEY,
            "Authorization": f"Bearer {base.SUPABASE_PUBLISHABLE_KEY}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        body = response.read().decode("utf-8")
        return json.loads(body) if body else None


async def rpc(name, payload):
    return await asyncio.to_thread(rpc_sync, name, payload)


async def touch_lease():
    return await rpc("strima_telegram_lease_touch", {
        "p_owner_id": OWNER_ID,
        "p_admin_key": base.STRIMA_ADMIN_KEY,
        "p_ttl_seconds": 20,
    })


async def release_lease():
    try:
        await rpc("strima_telegram_lease_release", {
            "p_owner_id": OWNER_ID,
            "p_admin_key": base.STRIMA_ADMIN_KEY,
        })
    except Exception:
        log.exception("Lease release failed")


def clear_entities():
    base.CHANNEL_INPUT_ENTITY = None
    base.SOURCE_INPUT_ENTITY = None
    base.SOURCE_CHANNEL_TITLE = None


async def disconnect_client():
    if base.client.is_connected():
        try:
            await base.client.disconnect()
        except Exception:
            log.exception("Client disconnect failed")
    clear_entities()


async def resolve_entities():
    clear_entities()
    me = await base.client.get_me()
    log.info("Exclusive worker connected as @%s (%s)", getattr(me, "username", None), me.id)
    dialogs = await base.client.get_dialogs()
    for dialog in dialogs:
        if dialog.id == base.TG_CHANNEL_ID:
            base.CHANNEL_INPUT_ENTITY = dialog.input_entity
            log.info("Destination resolved: %s (%s)", dialog.title, dialog.id)
        if dialog.id == base.TG_SOURCE_CHANNEL_ID:
            base.SOURCE_INPUT_ENTITY = dialog.input_entity
            base.SOURCE_CHANNEL_TITLE = dialog.title
            log.info("Source resolved: %s (%s)", dialog.title, dialog.id)
    if base.CHANNEL_INPUT_ENTITY is None or base.SOURCE_INPUT_ENTITY is None:
        raise RuntimeError("Configured source or destination could not be resolved")


async def supervisor():
    while STOP is not None and not STOP.is_set():
        try:
            lease = await touch_lease()
            owns = bool((lease or {}).get("acquired"))
            STATE["owns_lease"] = owns
            STATE["lease_until"] = (lease or {}).get("lease_until")

            if not owns:
                STATE["worker"] = "waiting"
                if base.client.is_connected():
                    await disconnect_client()
                await asyncio.sleep(5)
                continue

            if not base.client.is_connected():
                STATE["worker"] = "connecting"
                await base.client.connect()
                if not await base.client.is_user_authorized():
                    raise RuntimeError("Configured authorization is not valid")
                await resolve_entities()
                STATE["worker"] = "connected"
                STATE["last_error"] = None
                log.info("Exclusive worker ready owner=%s", OWNER_ID)

            await asyncio.sleep(5)

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            STATE["last_error"] = f"{type(exc).__name__}: {str(exc)[:250]}"
            STATE["worker"] = "error_retrying"
            log.exception("Worker error; HTTP remains available")
            await disconnect_client()
            if STATE.get("owns_lease"):
                await release_lease()
            STATE["owns_lease"] = False
            STATE["lease_until"] = None
            await asyncio.sleep(5)


@asynccontextmanager
async def lifespan(_app):
    global STOP, TASK
    STOP = asyncio.Event()
    STATE["worker"] = "waiting"
    TASK = asyncio.create_task(supervisor())
    log.info("HTTP startup complete; exclusive worker starts in background owner=%s", OWNER_ID)
    try:
        yield
    finally:
        STOP.set()
        if TASK:
            TASK.cancel()
            try:
                await TASK
            except asyncio.CancelledError:
                pass
        await disconnect_client()
        if STATE.get("owns_lease"):
            await release_lease()
        STATE["owns_lease"] = False
        STATE["worker"] = "stopped"


app.router.lifespan_context = lifespan


@app.get("/admin/telegram/runtime-status")
async def runtime_status(admin_key: Optional[str] = Header(default=None, alias="X-STRIMA-Admin-Key")):
    base.require_admin_key(admin_key)
    return {
        "ok": True,
        "owner_id": OWNER_ID,
        "worker": STATE.get("worker"),
        "owns_lease": STATE.get("owns_lease"),
        "lease_until": STATE.get("lease_until"),
        "connected": base.client.is_connected(),
        "destination_resolved": base.CHANNEL_INPUT_ENTITY is not None,
        "source_resolved": base.SOURCE_INPUT_ENTITY is not None,
        "last_error": STATE.get("last_error"),
    }
