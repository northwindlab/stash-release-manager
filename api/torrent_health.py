"""Watching running torrent downloads (v0.231.0).

A newsgroup download either finishes or fails. A torrent can stall, lose its sources, vanish from the client or end in an error state, so
the grab is not fire-and-forget. `run()` looks at every torrent the sidecar started (scenes and movies) at the interval
`torrent_health_minutes` (30), keeps one row per torrent in `torrent_health` and acts on it:

* progress, or download speed -> `ok` (the stall timer starts again);
* paused / stopped by the user -> `stalled` with a note, never switched, no timer; queued / checking by the client -> `ok` with a note;
* the stall time is OBSERVED time: neither the sidecar nor the client runs all the time, so two looks further apart than 2.5 rounds start a
  new window (a client that was off, disconnected from the network, or a sidecar that was not running counts as unknown, not as stalled);
* no progress for `torrent_stall_hours` (1) of observed time -> with real progress (`torrent_real_progress_percent`, 5 %) `stalled`: flagged, never given up;
  without it the download is dead: when the search finds another torrent it is taken and the dead one is removed (with its partial data) and
  blocked by hash; when there is no other one the state is `stuck` and the search is repeated every `torrent_alt_search_hours`;
* qBittorrent state `error` / `missingFiles`, or the torrent is gone from the client -> `error` / `missing`: flagged; the same switch rule as
  for a dead torrent applies when nothing was downloaded;
* a stuck / stalled torrent that moves again is `ok` again, and is still watched.

Nothing is ever deleted unless a replacement was grabbed first (or the user says so). `classify()` is a pure function of the torrent, the
previous row, the time and the settings, so the rules can be tested without a client. Movies are never grabbed by themselves (their
candidates wait in Needs review), so a dead movie download is flagged and the user decides: "Remove" sends the movie back to the wishlist
(torrent removed, blocked by hash) and its normal search proposes another release.
"""
import logging
import re
import time

import download_client_repo
import history
import qbittorrent_client
from qbittorrent_client import QbittorrentClient

logger = logging.getLogger(__name__)

OK, STALLED, STUCK, ERROR, MISSING = "ok", "stalled", "stuck", "error", "missing"
PROBLEM_STATES = (STALLED, STUCK, ERROR, MISSING)
GONE_DETAIL = "removed from the download client"


def default_cfg():
    return {"stall_s": 3600.0, "real_progress": 0.05, "alt_s": 6 * 3600.0, "max_gap_s": 5400.0}


def load_cfg(db):
    from routers.settings import get_setting_value
    def num(key, default):
        try:
            return float(get_setting_value(db, key, "") or default)
        except ValueError:
            return float(default)
    minutes = num("torrent_health_minutes", 30)
    return {"stall_s": num("torrent_stall_hours", 1) * 3600, "real_progress": num("torrent_real_progress_percent", 5) / 100,
            "alt_s": num("torrent_alt_search_hours", 6) * 3600,
            # two looks further apart than this are not "continuous" (2.5 rounds, at least 30 minutes)
            "max_gap_s": max(2.5 * minutes * 60, 1800.0)}


def _hm(seconds):
    h = seconds / 3600
    return f"{h:.1f} h" if h >= 1 else f"{int(seconds // 60)} min"


def check_interval_s(db):
    """Seconds between two looks (setting torrent_health_minutes; 0 = the monitor is off)."""
    from routers.settings import get_setting_value
    try:
        return max(float(get_setting_value(db, "torrent_health_minutes", "30") or 30), 0) * 60
    except ValueError:
        return 1800.0


_STALL_NOTE = re.compile(r"^no progress for [\d.]+ (?:min|h)")


def live_detail(row, now, cfg, interval_s):
    """The note of a stored row as it reads NOW. The row is written at each look (every `torrent_health_minutes`); between two looks the stall
    time shown would stand still, so it is carried on by the time since the last look -- as long as that look is recent (a longer silence is
    not continuous watching, nothing is added) -- and the note says when the last look was and when the next one is due. The decision
    (switch / flag) is still taken only at a look."""
    detail = row.get("detail") or ""
    if not _STALL_NOTE.match(detail) or row.get("state") not in (OK, STALLED):
        return detail
    age = max(0.0, now - (row.get("last_checked_at") or now))
    stalled = (row.get("stalled_s") or 0) + (age if age <= cfg["max_gap_s"] else 0)
    out = _STALL_NOTE.sub(f"no progress for {_hm(stalled)}", detail, count=1)
    when = f"looked {int(age // 60)} min ago"
    due = (row.get("last_checked_at") or now) + interval_s - now
    if interval_s and due > 0:
        when += f", next look in {max(1, int(due // 60))} min"
    return f"{out} ({when})"


def live_view(row, torrent, now, cfg):
    """DISPLAY ONLY: the stored row with the client's numbers of this moment (seeds, peers, availability, state, the stall time carried on, a torrent
    that moves again, one that was paused or resumed). Nothing is saved and nothing is decided here: switching, flagging and the two-looks rule
    for a missing / errored torrent stay with the monitor's rounds (every `torrent_health_minutes`) and the hour of observed stall. Returns None
    when the client has nothing for the row (the stored row is shown then), or when the torrent is missing / in error (shown only once the
    monitor has confirmed it: a client that has just started can show an empty list)."""
    if torrent is None:
        return None
    verdict = classify(torrent, row, now, cfg)
    if verdict.get("done") or verdict["state"] in (MISSING, ERROR):
        return None
    state, detail = verdict["state"], verdict["detail"]
    if state == STUCK:  # an hour without progress and nothing downloaded: acted on at the next look
        if row.get("state") == STUCK:
            note = (row.get("detail") or "").split(" -- ")[1:]
            detail = verdict["detail"] + (" -- " + " -- ".join(note) if note else "")
            state = STUCK
        else:
            state, detail = OK, verdict["detail"] + " (the monitor acts on it at its next look)"
    return {"state": state, "detail": detail, "seeds": torrent.get("num_complete"), "peers": torrent.get("num_incomplete"),
            "availability": torrent.get("availability"), "raw_state": torrent.get("state"), "progress": float(torrent.get("progress") or 0)}


def _first(*values):
    """The first value that is not None (0 is a value)."""
    return next((v for v in values if v is not None), None)


# qBittorrent states in which a torrent is not trying to download for a reason that is not a dead swarm (the client waits, or the user stopped it)
PAUSED_STATES = {"pausedDL", "pausedUP", "stoppedDL", "stoppedUP"}
WAITING_STATES = {"queuedDL", "queuedUP", "checkingDL", "checkingUP", "checkingResumeData", "allocating", "moving"}


def error_text(raw, found=None):
    """The note for a torrent in qBittorrent's error state. `found` = what the client's log says ({"reason", "folder"}) or None."""
    if raw == "missingFiles":
        return "files missing on disk -- recheck or fix the path in qBittorrent"
    if not found or not found.get("reason"):
        return "qBittorrent reports an error for this torrent -- its log (Tools > Log) says why"
    reason, where = found["reason"], found.get("folder") or "its download folder"
    low = reason.lower()
    if "permission denied" in low:
        return (f"qBittorrent cannot write to {where} (permission denied) -- give the qBittorrent user write access to that folder, "
                "then resume the torrent")
    if "no space" in low:
        return f"the disk qBittorrent writes to ({where}) is full -- free some space, then resume the torrent"
    if "read-only" in low:
        return f"{where} is read-only for qBittorrent -- fix the mount or the folder, then resume the torrent"
    return f"qBittorrent cannot write its files to {where}: {reason} -- fix that, then resume the torrent"


def classify(torrent, prev, now, cfg, error_found=None):
    """What to make of one torrent right now. `torrent` = the client's dict or None (not in the client), `prev` = the stored row (dict) or None
    (first sight). Returns {state, detail, progress, max_progress, last_progress_change_at, stalled_since, stalled_s, done, action} where action
    is 'switch' (look for another torrent now) or 'none'.

    The stall time is OBSERVED time, never wall-clock: neither the sidecar nor the download client runs all the time. Two looks at most
    `max_gap_s` apart with no progress in between add the time between them; a longer gap (the sidecar was off, the client was off or
    disconnected) starts a new window, because nothing is known about what happened in between. Paused / stopped and queued / checking torrents
    never count as stalled time; a paused one is shown as stalled with a note, and is never switched."""
    prev = prev or {}
    first = not prev
    progress = float((torrent or {}).get("progress") or 0)
    max_progress = max(float(prev.get("max_progress") or 0), progress)
    checked = prev.get("last_checked_at")
    gap = (now - float(checked)) if checked is not None else None
    continuous = gap is not None and 0 <= gap <= cfg.get("max_gap_s", 5400.0)
    out = {"progress": progress if torrent else float(prev.get("progress") or 0), "max_progress": max_progress,
           "last_progress_change_at": _first(prev.get("last_progress_change_at"), now), "stalled_since": None, "stalled_s": 0.0,
           "done": False, "action": "none"}
    real = max_progress >= cfg["real_progress"]
    last_alt = prev.get("last_alt_search_at")
    alt_due = last_alt is None or (now - float(last_alt)) >= cfg["alt_s"]
    kept = float(prev.get("keep_until") or 0) > now  # "Keep waiting" pressed

    def dead(state, detail, confirmed=True):
        out.update(state=state, detail=detail)
        if confirmed and not real and alt_due and not kept:
            out["action"] = "switch"
        return out

    def seen_before(state):
        """The same problem was already seen at the previous look (a client that has just started can show an empty list or a passing error)."""
        return continuous and prev.get("state") == state

    if torrent is None:
        return dead(MISSING, GONE_DETAIL, confirmed=seen_before(MISSING))
    status = qbittorrent_client.status_of(torrent)
    raw = torrent.get("state") or ""
    if status == "error":
        return dead(ERROR, error_text(raw, error_found), confirmed=seen_before(ERROR))
    if progress >= 1 or (status in ("seeding", "done") and progress >= 1):
        out.update(state=OK, detail="", done=True)
        return out
    if raw in PAUSED_STATES:
        word = "stopped" if raw.startswith("stopped") else "paused"
        out.update(state=STALLED, detail=f"{word} in the download client -- resume it there if it should go on at {progress * 100:.0f} %")
        return out
    if raw in WAITING_STATES:
        out.update(state=OK, detail={"queuedDL": "queued in the download client", "queuedUP": "queued in the download client",
                                     "moving": "being moved by the download client"}.get(raw, "the download client is checking it"))
        return out
    moved = progress > float(prev.get("progress") or 0) + 1e-6 or (torrent.get("dlspeed") or 0) > 0
    if moved:
        out.update(state=OK, detail="", last_progress_change_at=now)
        return out
    # Not moving. Counted only between two looks that follow each other closely, and only when at the previous look the torrent was trying to
    # download (not paused, queued or being checked: it may have been resumed since); otherwise a new window starts at this look.
    if continuous and prev.get("raw_state") not in PAUSED_STATES | WAITING_STATES:
        stalled_s = float(prev.get("stalled_s") or 0) + gap
        out["stalled_since"] = _first(prev.get("stalled_since"), checked)
    else:
        stalled_s = 0.0
        out["stalled_since"] = now
    out["stalled_s"] = stalled_s
    if stalled_s < cfg["stall_s"]:
        out.update(state=OK, detail=f"no progress for {_hm(stalled_s)}" if stalled_s >= 60 else "")
        return out
    detail = f"no progress for {_hm(stalled_s)} at {progress * 100:.0f} %"
    if real:
        out.update(state=STALLED, detail=detail)  # flagged only: it had real progress, it is never given up
        return out
    return dead(STUCK, detail)


# -- the stored rows ---------------------------------------------------------------------------------------------------------------

_COLS = ("client_id", "job_id", "kind", "provider", "scene_id", "film_id", "title", "state", "detail", "progress", "max_progress", "first_seen_at",
         "last_checked_at", "last_progress_change_at", "stalled_since", "stalled_s", "last_alt_search_at", "keep_until", "seeds", "peers", "availability",
         "raw_state")


def get_row(db, client_id, job_id):
    with db.connect() as conn:
        r = conn.execute("SELECT * FROM torrent_health WHERE client_id = ? AND job_id = ?", (client_id, str(job_id).lower())).fetchone()
    return dict(r) if r else None


def save_row(db, row):
    row = {**row, "job_id": str(row["job_id"]).lower()}
    with db.connect() as conn:
        conn.execute(f"INSERT OR REPLACE INTO torrent_health ({', '.join(_COLS)}) VALUES ({', '.join('?' for _ in _COLS)})",
                     tuple(row.get(c) for c in _COLS))


def delete_row(db, client_id, job_id):
    with db.connect() as conn:
        conn.execute("DELETE FROM torrent_health WHERE client_id = ? AND job_id = ?", (client_id, str(job_id).lower()))


def list_rows(db, states=None):
    with db.connect() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM torrent_health ORDER BY first_seen_at")]
    return [r for r in rows if not states or r["state"] in states]


def row_for_scene(db, provider, scene_id):
    with db.connect() as conn:
        r = conn.execute("SELECT * FROM torrent_health WHERE kind = 'scene' AND provider = ? AND scene_id = ?", (provider, scene_id)).fetchone()
    return dict(r) if r else None


def row_for_film(db, film_id):
    with db.connect() as conn:
        r = conn.execute("SELECT * FROM torrent_health WHERE kind = 'film' AND film_id = ?", (film_id,)).fetchone()
    return dict(r) if r else None


def keep_waiting(db, client_id, job_id, hours=24, now=None):
    """The user says "keep waiting": no switching for `hours` hours."""
    with db.connect() as conn:
        conn.execute("UPDATE torrent_health SET keep_until = ? WHERE client_id = ? AND job_id = ?",
                     ((now or time.time()) + hours * 3600, client_id, str(job_id).lower()))


# -- the targets (what the sidecar started) --------------------------------------------------------------------------------------------

def targets(db):
    """Every running torrent download the sidecar knows: [{kind, provider, scene_id, film_id, title, client_id, job_id, upgrade}]. Scenes: releases
    with a job (deduped by scene); movies: status 'grabbed' with a job. Only qBittorrent clients count."""
    import release_repo
    out = []
    for r in release_repo.list_grabbed_awaiting_import(db):
        out.append({"kind": "scene", "provider": r["provider"], "scene_id": r["scene_stashdb_id"], "film_id": None,
                    "title": r.get("title") or r.get("grabbed_title") or "", "client_id": r["download_client_id"],
                    "job_id": r["download_job_id"], "upgrade": bool(r.get("upgrade_of")), "release": r})
    with db.connect() as conn:
        films = [dict(f) for f in conn.execute(
            "SELECT id, title, download_client_id, download_job_id, grabbed_title, upgrading FROM film_want "
            "WHERE status = 'grabbed' AND download_job_id IS NOT NULL")]
    for f in films:
        out.append({"kind": "film", "provider": None, "scene_id": None, "film_id": f["id"], "title": f["title"],
                    "client_id": f["download_client_id"], "job_id": f["download_job_id"], "upgrade": bool(f.get("upgrading")), "film": f})
    result = []
    for t in out:
        client_row = download_client_repo.get_one(db, t["client_id"]) if t["client_id"] else None
        if client_row is not None and client_row["type"] == "qbittorrent" and t["job_id"]:
            result.append({**t, "client_row": client_row})
    return result


def current_for_scene(db, provider, scene_id):
    """The torrent a scene is downloading right now as a row-like dict (what finish_replacement() needs), whether or not the monitor has seen it
    yet; None when the scene has no running qBittorrent download."""
    for t in targets(db):
        if t["kind"] == "scene" and t["provider"] == provider and t["scene_id"] == scene_id:
            return {"client_id": t["client_id"], "job_id": t["job_id"], "kind": "scene", "provider": provider, "scene_id": scene_id,
                    "film_id": None, "title": t["title"]}
    return None


def client_for(client_row):
    return QbittorrentClient(client_row["url"], client_row["username"], client_row["password"], client_row["category"], client_row.get("options"))


# -- the monitor ----------------------------------------------------------------------------------------------------------------------

def run(db, hooks, now=None, client_factory=None):
    """One round. `hooks` = {"switch_scene": fn(target) -> bool (another torrent was grabbed)}. `client_factory(client_row)` -> a
    QbittorrentClient-like object (tests). Returns a summary."""
    now = time.time() if now is None else now
    cfg = load_cfg(db)
    make_client = client_factory or client_for
    summary = {"checked": 0, "problems": 0, "switched": 0, "unreachable": 0, "offline": 0}
    by_client = {}
    for t in targets(db):
        by_client.setdefault(t["client_id"], []).append(t)
    seen_keys = set()
    for client_id, items in by_client.items():
        client = make_client(items[0]["client_row"])
        try:
            torrents = {str(x.get("hash") or "").lower(): x for x in client.get_torrents(category="")}
        except Exception:
            logger.warning("torrent health: couldn't ask the download client %s -- skipped this round", items[0]["client_row"].get("name"), exc_info=True)
            summary["unreachable"] += 1
            for t in items:
                seen_keys.add((client_id, str(t["job_id"]).lower()))  # keep their rows
            continue
        if _offline(client):
            logger.info("torrent health: %s is running but not connected to the network -- skipped this round", items[0]["client_row"].get("name"))
            summary["offline"] += 1
            for t in items:
                seen_keys.add((client_id, str(t["job_id"]).lower()))  # keep their rows, and their old look time: the time offline is not counted
            continue
        for t in items:
            job_id = str(t["job_id"]).lower()
            seen_keys.add((client_id, job_id))
            torrent = torrents.get(job_id)
            prev = get_row(db, client_id, job_id)
            found = None
            if torrent is not None and qbittorrent_client.status_of(torrent) == "error" and (torrent.get("state") or "") != "missingFiles":
                try:
                    found = client.error_reason(torrent) if hasattr(client, "error_reason") else None
                except Exception:
                    logger.debug("torrent health: could not read the error reason", exc_info=True)
            verdict = classify(torrent, prev, now, cfg, error_found=found)
            summary["checked"] += 1
            if verdict["done"]:
                if prev:
                    delete_row(db, client_id, job_id)  # complete: the import takes over
                continue
            row = {
                "client_id": client_id, "job_id": job_id, "kind": t["kind"], "provider": t["provider"], "scene_id": t["scene_id"],
                "film_id": t["film_id"], "title": t["title"], "state": verdict["state"], "detail": verdict["detail"],
                "progress": verdict["progress"], "max_progress": verdict["max_progress"],
                "first_seen_at": _first((prev or {}).get("first_seen_at"), now), "last_checked_at": now,
                "last_progress_change_at": verdict["last_progress_change_at"], "stalled_since": verdict["stalled_since"],
                "stalled_s": verdict["stalled_s"],
                "last_alt_search_at": (prev or {}).get("last_alt_search_at"), "keep_until": (prev or {}).get("keep_until"),
                "seeds": (torrent or {}).get("num_complete") if torrent else None, "peers": (torrent or {}).get("num_incomplete") if torrent else None,
                "availability": (torrent or {}).get("availability") if torrent else None, "raw_state": (torrent or {}).get("state"),
            }
            if verdict["action"] == "switch" and not t.get("upgrade"):
                row["last_alt_search_at"] = now
                try:
                    switched = _switch(db, t, torrent, client, hooks)
                except Exception:
                    logger.exception("torrent health: looking for an alternative failed for %r", t["title"])
                    switched = False
                if switched == "grabbed":
                    summary["switched"] += 1
                    delete_row(db, client_id, job_id)
                    continue
                if switched == "failed":
                    # No answer from the indexers is not "no alternative": no 6 h back-off, the next look searches again
                    row["last_alt_search_at"] = (prev or {}).get("last_alt_search_at")
                    note = " -- could not search for another torrent (the indexer did not answer), trying again at the next look"
                else:
                    note = " -- no other torrent found"
                if switched == "film":
                    row["detail"] = verdict["detail"] + " -- remove it to look for another release"
                elif row["state"] in (MISSING, ERROR):
                    row["detail"] = verdict["detail"] + note
                else:
                    row["state"] = STUCK
                    row["detail"] = verdict["detail"] + note
            elif row["state"] == STUCK and prev and prev["state"] == STUCK and " -- " in (prev.get("detail") or ""):
                row["detail"] = verdict["detail"] + " -- " + prev["detail"].split(" -- ")[-1]  # between two searches the note stays
            elif verdict["action"] == "switch" and t.get("upgrade"):
                row["last_alt_search_at"] = now  # an upgrade download: flagged only (an alternative must prove it is better, see upgrades.py)
            if prev is None or prev["state"] != row["state"]:
                _log_change(t, prev, row)
            if row["state"] in PROBLEM_STATES:
                summary["problems"] += 1
            save_row(db, row)
    # rows of downloads that are no longer running (imported, failed back, switched by hand)
    for r in list_rows(db):
        if (r["client_id"], r["job_id"]) not in seen_keys:
            delete_row(db, r["client_id"], r["job_id"])
    return summary


def _offline(client):
    """True when the client runs but reports no network connection (connection_status 'disconnected', e.g. the VPN is down): nothing can
    download then, so a round says nothing about the torrents. Best effort: a client without the call, or an error, counts as online."""
    try:
        info = client.transfer_info()
    except Exception:
        return False
    return str((info or {}).get("connection_status") or "").lower() == "disconnected"


def _log_change(t, prev, row):
    if row["state"] == OK:
        if prev and prev["state"] in PROBLEM_STATES:
            _history(t, "info", f"Torrent moves again: {t['title']}")
        return
    level = "warning"
    text = {STALLED: "stalled", STUCK: "stuck", ERROR: "has an error", MISSING: "is missing"}[row["state"]]
    _history(t, level, f"Torrent {text} ({row['detail']}): {t['title']}", dedupe_minutes=60 * 12)


def _history(t, level, message, dedupe_minutes=None):
    if t["kind"] == "scene":
        history.scene(level, "torrent", message, t["provider"], t["scene_id"], dedupe_minutes=dedupe_minutes)
    else:
        history.event(level, "torrent", message, dedupe_minutes=dedupe_minutes)


def _switch(db, t, torrent, client, hooks):
    """Looks for another torrent. Returns 'grabbed' (scene: replaced, old one removed), 'film' (a movie: flagged only), '' (nothing found) or
    'failed' (the search could not be made)."""
    job_id = str(t["job_id"]).lower()
    if t["kind"] == "film":
        return "film"
    found = hooks["switch_scene"](t)
    if found == "failed":
        return "failed"  # the search itself did not work (an indexer not answering): not the same as "no other torrent exists"
    if not found:
        return ""
    # the replacement is running: the dead one goes, with its partial data, and is blocked by its hash
    remove_torrent(client, job_id)
    import release_repo
    release_repo.add_review_rejected_title(db, t["provider"], t["scene_id"], "hash:" + job_id)
    _history(t, "warning", f"Switched from a dead torrent to another one: {t['title']}")
    return "grabbed"


def remove_torrent(client, job_id):
    """Deletes the torrent and its data from the client; a torrent the client no longer has is fine."""
    try:
        client.delete(job_id, delete_files=True)
    except Exception:
        logger.warning("torrent health: couldn't remove torrent %s from the client", job_id, exc_info=True)


# -- what the user can do about a flagged torrent ----------------------------------------------------------------------------------------

def _client_of(db, client_id):
    row = download_client_repo.get_one(db, client_id)
    return client_for(row) if row is not None and row["type"] == "qbittorrent" else None


def finish_replacement(db, old_row):
    """A replacement for the flagged torrent was grabbed (by hand from the manual search): the old one goes with its partial data, blocked by
    its hash, and its row is dropped."""
    client = _client_of(db, old_row["client_id"])
    if client is not None:
        remove_torrent(client, old_row["job_id"])
    if old_row["kind"] == "scene":
        import release_repo
        release_repo.add_review_rejected_title(db, old_row["provider"], old_row["scene_id"], "hash:" + old_row["job_id"])
    delete_row(db, old_row["client_id"], old_row["job_id"])
    _history({"kind": old_row["kind"], "provider": old_row["provider"], "scene_id": old_row["scene_id"]}, "warning",
             f"Replaced a flagged torrent by hand: {old_row['title']}")


def abandon(db, client_id, job_id):
    """"Remove": the torrent goes (with its data), is blocked by its hash, and the scene / movie is back in the search queue."""
    row = get_row(db, client_id, job_id)
    if row is None:
        return {"error": "this torrent is not on the monitor's list (any more)"}
    client = _client_of(db, client_id)
    if client is not None:
        remove_torrent(client, row["job_id"])
    if row["kind"] == "scene":
        import release_repo
        release_repo.add_review_rejected_title(db, row["provider"], row["scene_id"], "hash:" + row["job_id"])
        release_repo.set_download_job_for_scene(db, row["provider"], row["scene_id"], None, None)
        release_repo.set_search_result_for_scene(db, row["provider"], row["scene_id"], "download failed: torrent removed by hand")
    else:
        import film_import
        import film_repo
        film = film_repo.get(db, row["film_id"])
        if film:
            msg = f"The torrent was removed ({row['detail']}) -- back on the wishlist"
            film_repo.update(db, film["id"], **film_import._fail_back(film), download_job_id=None, download_client_id=None, import_error=msg,
                             last_search_result="download_failed",
                             rejected_titles=[*film.get("rejected_titles", []), "hash:" + row["job_id"]])
    delete_row(db, client_id, job_id)
    _history(row, "warning", f"Torrent removed by hand, back in the search queue: {row['title']}")
    return {"ok": True}


def dismiss(db, client_id, job_id):
    """"Dismiss": the entry is dropped for good -- the torrent goes from the client (with its data, when it is still there), nothing is searched
    again and nothing is blocked (the scene or movie arrived another way, or is not wanted any more). A scene on the wishlist is dismissed; an
    UPGRADE download leaves the library file as it was and its candidate is dismissed; a movie is dismissed (an upgrade of a movie goes back to
    plain 'imported')."""
    row = get_row(db, client_id, job_id)
    if row is None:
        return {"error": "this torrent is not on the monitor's list (any more)"}
    client = _client_of(db, client_id)
    if client is not None:
        remove_torrent(client, row["job_id"])
    if row["kind"] == "scene":
        import json
        import release_repo
        import upgrades
        provider, pid = row["provider"], row["scene_id"]
        rows = upgrades._scene_rows(db, provider, pid)
        upgrade_of = next((r.get("upgrade_of") for r in rows if r.get("upgrade_of")), None)
        release_repo.set_download_job_for_scene(db, provider, pid, None, None)
        cand = None
        if upgrade_of:
            with db.connect() as conn:
                cand = conn.execute("SELECT * FROM upgrade_candidate WHERE provider_scene_id = ? AND file_path = ?", (pid, upgrade_of)).fetchone()
        if cand is not None:  # the rows go back as they were before the grab (only the pseudo row of a library scene is removed)
            cand = dict(cand)
            previous = [(r["entity_type"], r["entity_stashdb_id"], "imported" if r.get("imported_at") else "new")
                        for r in rows if not str(r["entity_stashdb_id"]).startswith("upgrade:")]
            upgrades._undo_release_rows(db, cand, previous)
            upgrades.set_candidate(db, cand["id"], status="dismissed", review_candidate=None, last_search_result="dismissed")
        else:
            release_repo.set_status_for_scene(db, provider, pid, "dismissed")
    else:
        import film_import
        import film_repo
        film = film_repo.get(db, row["film_id"])
        if film:
            fields = {"status": "imported", "upgrading": 0, "upgrade_wanted": 0} if film.get("upgrading") else {"status": "dismissed"}
            film_repo.update(db, film["id"], **fields, download_job_id=None, download_client_id=None, review_candidate=None)
    delete_row(db, client_id, job_id)
    _history(row, "info", f"Dismissed from the torrents needing a look: {row['title']}")
    return {"ok": True}


def readd(db, client_id, job_id):
    """"Re-add": puts the same torrent (by its hash) into the client again; its timers start again."""
    row = get_row(db, client_id, job_id)
    if row is None:
        return {"error": "this torrent is not on the monitor's list (any more)"}
    client = _client_of(db, client_id)
    if client is None:
        return {"error": "the download client is gone"}
    try:
        client.add(f"magnet:?xt=urn:btih:{row['job_id']}", name=row["title"])
    except qbittorrent_client.AlreadyAdded:
        pass
    except qbittorrent_client.QbittorrentError as e:
        return {"error": str(e)}
    delete_row(db, client_id, job_id)  # adopted again, with fresh timers, at the next round
    _history(row, "info", f"Torrent added to the client again: {row['title']}")
    return {"ok": True}
