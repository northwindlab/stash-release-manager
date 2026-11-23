"""The torrent monitor's state for the Activity page and the actions on a flagged torrent (v0.231.0) -- see torrent_health.py. A torrent is
addressed by its client id and hash (the monitor's own key); no response contains a link or key."""
import threading
import time

from fastapi import APIRouter

import torrent_health

router = APIRouter(prefix="/torrent-health", tags=["torrent-health"])

_db = None
_hooks = {}
_run = {"fn": None, "running": False}


def init_torrent_health_router(db, switch_scene, run_once):
    global _db, _hooks
    _db = db
    _hooks = {"switch_scene": switch_scene}
    _run["fn"] = run_once


def _live_torrents(rows):
    """({(client id, hash): the client's dict}, the ids of the clients that answered) for these rows, one request per client; a client that does
    not answer contributes nothing."""
    out, answered = {}, set()
    for client_id in {r["client_id"] for r in rows}:
        client = torrent_health._client_of(_db, client_id)
        try:
            for t in (client.get_torrents(category="") if client else []):
                out[(client_id, str(t.get("hash") or "").lower())] = t
            if client:
                answered.add(client_id)
        except Exception:
            continue
    return out, answered


def _public(r, now, live=None, answered=()):
    cfg = torrent_health.load_cfg(_db)
    key = (r["client_id"], str(r["job_id"]).lower())
    if r["client_id"] in answered and key not in (live or {}) and r["state"] != torrent_health.MISSING:
        # removed in the client since the monitor's last look: shown as missing now, the monitor records it in the round asked for here
        r = {**r, "state": torrent_health.MISSING, "detail": torrent_health.GONE_DETAIL, "seeds": None, "peers": None, "availability": None,
             "raw_state": None, "stalled_s": 0}
        kick_round()
    view = torrent_health.live_view(r, (live or {}).get((r["client_id"], str(r["job_id"]).lower())), now, cfg)
    if view:  # the client's numbers of this moment (display only; the monitor decides at its looks)
        r = {**r, **{k: view[k] for k in ("state", "detail", "seeds", "peers", "availability", "raw_state")}, "progress": view["progress"]}
    else:
        r = {**r, "detail": torrent_health.live_detail(r, now, cfg, torrent_health.check_interval_s(_db))}
    stalled = r.get("stalled_s") if r.get("stalled_s") else None  # observed stall time (see torrent_health.classify)
    return {
        "client_id": r["client_id"], "job_id": r["job_id"], "kind": r["kind"], "provider": r["provider"], "scene_id": r["scene_id"],
        "film_id": r["film_id"], "title": r["title"], "state": r["state"], "detail": r["detail"],
        "percent": round((r["progress"] or 0) * 100, 1), "max_percent": round((r["max_progress"] or 0) * 100, 1),
        "seeds": r["seeds"], "peers": r["peers"], "availability": r["availability"], "raw_state": r["raw_state"],
        "stalled_for": int(stalled) if stalled is not None else None, "checked_ago": int(now - r["last_checked_at"]),
    }


@router.get("")
def list_health(all: bool = False):
    """The torrents that need a look (stalled, stuck, error, missing); `all=true` lists every watched torrent."""
    now = time.time()
    rows = torrent_health.list_rows(_db, None if all else torrent_health.PROBLEM_STATES)
    live, answered = _live_torrents(rows)
    return {"rows": [_public(r, now, live, answered) for r in rows]}


def _start_round():
    """One monitor round in the background (a search for an alternative can take a while); False when one is already running."""
    if _run["running"] or _run["fn"] is None:
        return False
    _run["running"] = True  # set before the thread starts: two quick calls must not both start one
    def work():
        try:
            _run["fn"]()
        finally:
            _run["running"] = False
    threading.Thread(target=work, daemon=True).start()
    return True


_kicked = {"at": 0.0}


def kick_round(min_gap_s=120):
    """A round asked for by a live view that found something the stored rows do not say yet (a torrent removed in the client). Throttled, so a
    page that polls does not start one round after another."""
    now = time.time()
    if now - _kicked["at"] < min_gap_s:
        return False
    _kicked["at"] = now
    return _start_round()


@router.post("/check-now")
def check_now():
    """One monitor round right now, in the background."""
    if not _start_round():
        return {"started": False, "reason": "a check is already running"}
    return {"started": True}


@router.post("/switch")
def switch(body: dict):
    """"Find another": looks for another torrent now, whatever the progress (the user decided); the old one is removed when a replacement was
    grabbed. Scenes only -- a movie is removed and searched again from its wishlist entry."""
    row = torrent_health.get_row(_db, body.get("client_id"), str(body.get("job_id") or ""))
    if row is None:
        return {"error": "this torrent is not on the monitor's list (any more)"}
    if row["kind"] != "scene":
        return {"error": "for a movie use Remove: it goes back to the wishlist and its search proposes another release"}
    target = _target_of(row)
    if target is None:
        return {"error": "the download is no longer running"}
    found = _hooks["switch_scene"](target)
    if found == "failed":  # not a replacement: the old torrent stays
        return {"switched": False, "message": "the search did not work (the indexer did not answer) -- try again"}
    if not found:
        return {"switched": False, "message": "no other torrent found"}
    torrent_health.finish_replacement(_db, row)
    return {"switched": True}


def _target_of(row):
    for t in torrent_health.targets(_db):
        if str(t["job_id"]).lower() == row["job_id"] and t["client_id"] == row["client_id"]:
            return t
    return None


@router.post("/remove")
def remove(body: dict):
    return torrent_health.abandon(_db, body.get("client_id"), str(body.get("job_id") or ""))


@router.post("/dismiss")
def dismiss(body: dict):
    return torrent_health.dismiss(_db, body.get("client_id"), str(body.get("job_id") or ""))


@router.post("/readd")
def readd(body: dict):
    return torrent_health.readd(_db, body.get("client_id"), str(body.get("job_id") or ""))


@router.post("/keep")
def keep(body: dict):
    """"Keep waiting": the monitor flags the torrent but does not switch it for a day."""
    torrent_health.keep_waiting(_db, body.get("client_id"), str(body.get("job_id") or ""))
    return {"ok": True}
