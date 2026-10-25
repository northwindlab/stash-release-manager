"""Maintenance > Convert (v0.245.0, see convert.py): the list, the buttons, and the hook the encode worker's "done" report calls."""
import threading

from fastapi import APIRouter

import convert
import encode_queue
from routers.settings import get_setting_value

router = APIRouter(prefix="/maintenance/convert", tags=["maintenance-convert"])
_db = None
_get_stash_client = None
_to_local = None
_recycle = None
_refresh_lock = threading.Lock()


def init_convert_router(db, get_stash_client, to_local, recycle):
    global _db, _get_stash_client, _to_local, _recycle
    _db, _get_stash_client, _to_local, _recycle = db, get_stash_client, to_local, recycle


def finish_row(row):
    """Called in the background after a worker reported a finished file (routers/encode_worker.py)."""
    convert.finish(_db, _get_stash_client(), _to_local, _recycle, row)


def claim_job(worker):
    return convert.claim_next(_db, worker, _to_local)


def resume():
    convert.resume(_db, _get_stash_client(), _to_local, _recycle)


def worker_maps():
    """Where the worker machine mounts the folders the sidecar knows (settings `encode_worker_media_path` / `encode_worker_downloads_path`), as
    [{"remote", "local"}]; sent with every job so nobody has to repeat them on the command line. Empty values are left out."""
    out = []
    for remote_key, remote_default, local_key in (("local_media_mount", "/media", "encode_worker_media_path"),
                                                  ("local_downloads_mount", "/downloads", "encode_worker_downloads_path")):
        local = (get_setting_value(_db, local_key, "") or "").strip()
        if local:
            out.append({"remote": get_setting_value(_db, remote_key, remote_default), "local": local})
    return out


@router.get("")
def convert_list():
    rows = convert.rows(_db)
    return {"rows": rows, "worker": encode_queue.worker_status(), "media_path_set": bool((get_setting_value(_db, "encode_worker_media_path", "") or "").strip()),
            "done": len(convert.history_rows(_db, "done")), "dismissed": len(convert.history_rows(_db, "dismissed"))}


@router.get("/history")
def convert_history(status: str = "done"):
    return {"rows": convert.history_rows(_db, status if status in ("done", "dismissed") else "done")}


@router.post("/refresh")
def convert_refresh():
    if not _refresh_lock.acquire(blocking=False):
        return {"skipped": True, "reason": "a refresh is already running"}
    try:
        return convert.refresh(_db, _get_stash_client())
    finally:
        _refresh_lock.release()


@router.post("/queue")
def convert_queue(body: dict):
    return {"queued": convert.queue(_db, body.get("ids") or [])}


@router.post("/cancel")
def convert_cancel(body: dict):
    return {"cancelled": convert.cancel(_db, body.get("ids") or [])}


@router.post("/dismiss")
def convert_dismiss(body: dict):
    return {"changed": convert.dismiss(_db, body.get("ids") or [], restore=bool(body.get("restore")))}
