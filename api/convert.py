"""Maintenance > Convert (v0.245.0): library files that no browser plays directly (old video codec such as MPEG-4 ASP / WMV / FLV / MPEG-1, or good
H.264 in an old container such as FLV) are turned into H.264 + AAC in an MP4 by an encode worker. The Stash scene stays: the new file joins it
(sceneMerge), becomes its primary file, the old file goes to the recycle bin.

One table, `convert_candidate`, is candidate list AND job queue (it survives a restart, unlike encode_queue.py). Flow of a row:
wanted -> queued (the user pressed Convert) -> running (a worker claimed it: GET /encode-worker/next) -> verifying (the worker reported "ok",
the sidecar looks at the file) -> replacing (swap into the scene) -> done; failed on any error (the old file is untouched until the very end).

Out of scope on purpose: HEVC / VR files, and files whose only problem is AC3 sound.
"""
import logging
import os
import threading
import time

import history
import quality_detect
import transcode

logger = logging.getLogger(__name__)

BROWSER_VIDEO = {"h264", "hevc", "vp8", "vp9", "av1"}
BROWSER_CONTAINERS = {"mp4", "mov", "m4v", "webm", "matroska", "mkv"}
LEASE_SECONDS = 600  # a running job without a sign of life for this long goes back to the queue
OPEN = ("wanted", "queued", "running", "verifying", "replacing", "failed")  # everything the page lists besides done / dismissed
_replace_lock = threading.Lock()

SCENES_QUERY = """
query { findScenes(filter: {per_page: -1}) { scenes { id title
  files { id path format video_codec audio_codec bit_rate width height size duration } } } }
"""


def classify(file):
    """'reencode', 'remux' or None for one Stash video file."""
    v = (file.get("video_codec") or "").lower()
    c = (file.get("format") or "").lower()
    if not v or not c:
        return None
    if quality_detect.vr_label(file.get("width"), file.get("height")):
        return None  # VR files are not part of this page
    if v not in BROWSER_VIDEO:
        return "reencode"
    if v == "hevc":
        return None  # played directly by the browsers in use; HEVC is not converted here
    if c not in BROWSER_CONTAINERS:
        return "remux"
    return None


def refresh(db, stash_client):
    """Reads every Stash file and rebuilds the 'wanted' list. Rows in any other status are kept (and updated with the file's current numbers while
    they are still 'wanted' / 'failed'). A file that Upgrade wanted can search for (it has a provider id) is left out. Returns {"wanted", "remux", "reencode", "added", "removed"}."""
    scenes = stash_client._graphql(SCENES_QUERY, {})["findScenes"]["scenes"]
    with db.connect() as conn:
        taken = {(r["scene_id"], r["file_path"]) for r in conn.execute(
            "SELECT scene_id, file_path FROM upgrade_candidate WHERE status NOT IN ('done', 'dismissed') "
            "AND provider_scene_id IS NOT NULL AND provider_scene_id != ''")}  # a candidate WITHOUT a provider id cannot be searched, so Convert takes it
        existing = {(r["scene_id"], r["file_path"]): r["status"] for r in conn.execute("SELECT scene_id, file_path, status FROM convert_candidate")}
        seen, added = set(), 0
        for s in scenes:
            for f in s.get("files") or []:
                mode = classify(f)
                key = (str(s["id"]), f["path"])
                if mode is None or key in taken:
                    continue
                seen.add(key)
                values = (s.get("title") or "", f["id"], f.get("format"), f.get("video_codec"), f.get("audio_codec"), f.get("width"), f.get("height"),
                          f.get("size"), f.get("bit_rate"), f.get("duration"), mode)
                if key not in existing:
                    conn.execute("""INSERT INTO convert_candidate (scene_id, file_path, scene_title, file_id, container, video_codec, audio_codec,
                                    width, height, size, bit_rate, duration, mode) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", (key[0], key[1], *values))
                    added += 1
                elif existing[key] in ("wanted", "failed"):
                    conn.execute("""UPDATE convert_candidate SET scene_title = ?, file_id = ?, container = ?, video_codec = ?, audio_codec = ?, width = ?,
                                    height = ?, size = ?, bit_rate = ?, duration = ?, mode = ? WHERE scene_id = ? AND file_path = ?""", (*values, *key))
        removed = 0
        for key, status in existing.items():
            if status == "wanted" and key not in seen:  # fixed meanwhile (converted outside, replaced, now in Upgrade wanted)
                conn.execute("DELETE FROM convert_candidate WHERE scene_id = ? AND file_path = ?", key)
                removed += 1
        counts = {r["mode"]: r["n"] for r in conn.execute(
            "SELECT mode, COUNT(*) AS n FROM convert_candidate WHERE status IN ('wanted', 'queued', 'running', 'verifying', 'replacing', 'failed') GROUP BY mode")}
    return {"remux": counts.get("remux", 0), "reencode": counts.get("reencode", 0), "wanted": sum(counts.values()), "added": added, "removed": removed}


# The step a finished conversion is in on the sidecar (v0.249.7): {row id: {"step", "since" (epoch), "percent" (0-100 or None), "step_since"}}. Memory only: it
# only tells the page what is going on; the state itself is the row's status.
_phase = {}


def set_phase(row_id, step, percent=None):
    old = _phase.get(row_id)
    now = time.time()
    _phase[row_id] = {"step": step, "percent": percent, "since": old["since"] if old else now,
                      "step_since": old["step_since"] if old and old["step"] == step else now}


def clear_phase(row_id):
    _phase.pop(row_id, None)


def _wait_scan(stash_client, job_id, row_id, step):
    """Waits for a Stash job and tells the page how far it is (Stash reports progress 0..1)."""
    set_phase(row_id, step)
    stash_client.wait_for_job(job_id, on_progress=lambda job: set_phase(row_id, step, round(float(job["progress"]) * 100) if job.get("progress") is not None else None))
    set_phase(row_id, step, 100)


def rows(db):
    """Every row the page shows (everything but done and dismissed; those come with `include`)."""
    with db.connect() as conn:
        out = [dict(r) for r in conn.execute("SELECT * FROM convert_candidate WHERE status IN ('wanted', 'queued', 'running', 'verifying', 'replacing', 'failed') ORDER BY id")]
    for r in out:
        if r["status"] in ("verifying", "replacing") and r["id"] in _phase:
            p = dict(_phase[r["id"]])
            p["step_seconds"] = int(time.time() - p["step_since"])  # told as a duration, so a clock that differs between NAS and browser does not matter
            r["phase"] = p
    return out


def history_rows(db, status):
    with db.connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM convert_candidate WHERE status = ? ORDER BY finished_at DESC, id DESC", (status,))]


def badge(db):
    with db.connect() as conn:
        return conn.execute("SELECT COUNT(*) AS n FROM convert_candidate WHERE status IN ('wanted', 'failed')").fetchone()["n"]


def queue(db, ids):
    """The user pressed Convert: wanted / failed rows -> queued. Returns the number queued."""
    n = 0
    with db.connect() as conn:
        for i in ids:
            n += conn.execute("UPDATE convert_candidate SET status = 'queued', error = NULL, queued_at = datetime('now'), percentage = NULL, speed_x = NULL, "
                              "seconds_left = NULL WHERE id = ? AND status IN ('wanted', 'failed')", (int(i),)).rowcount
    return n


def accept_length(db, items):
    """The user accepts that a source file is shorter than its header says (cut off): `items` = [{"id", "seconds"}] with the length the file really
    decodes to. The row is queued again; the worker and the sidecar check the converted file against that length. Returns the number queued."""
    n = 0
    with db.connect() as conn:
        for it in items:
            sec = float(it["seconds"])
            if sec <= 0:
                continue
            n += conn.execute("UPDATE convert_candidate SET accept_duration = ?, status = 'queued', error = NULL, queued_at = datetime('now'), percentage = NULL, "
                              "speed_x = NULL, seconds_left = NULL WHERE id = ? AND status IN ('wanted', 'failed')", (sec, int(it["id"]))).rowcount
    return n


def cancel(db, ids):
    """Queued and running rows go back to 'wanted' (a running worker is told to stop with its next progress report)."""
    n = 0
    with db.connect() as conn:
        for i in ids:
            n += conn.execute("UPDATE convert_candidate SET status = 'wanted', worker = NULL, percentage = NULL, speed_x = NULL, seconds_left = NULL, "
                              "started_at = NULL WHERE id = ? AND status IN ('queued', 'running')", (int(i),)).rowcount
    return n


def dismiss(db, ids, restore=False):
    with db.connect() as conn:
        if restore:
            return sum(conn.execute("UPDATE convert_candidate SET status = 'wanted' WHERE id = ? AND status = 'dismissed'", (int(i),)).rowcount for i in ids)
        return sum(conn.execute("UPDATE convert_candidate SET status = 'dismissed' WHERE id = ? AND status IN ('wanted', 'failed')", (int(i),)).rowcount for i in ids)


# ---------- the worker side ----------

def claim_next(db, worker, to_local, auto=False):
    """The oldest queued row, marked as running for `worker`; None when nothing waits. `to_local` maps Stash's path to the sidecar's own.
    `auto` (the widget's "work through the whole list" switch): when nothing is queued, the next 'wanted' row of the list is taken (failed and
    dismissed rows never are, so a broken file cannot loop)."""
    now = time.time()
    with db.connect() as conn:
        conn.execute("UPDATE convert_candidate SET status = 'queued', worker = NULL WHERE status = 'running' AND beat_at < ?", (now - LEASE_SECONDS,))
        row = conn.execute("SELECT * FROM convert_candidate WHERE status = 'queued' ORDER BY queued_at, id LIMIT 1").fetchone()
        if row is None and auto:
            row = conn.execute("SELECT * FROM convert_candidate WHERE status = 'wanted' ORDER BY id LIMIT 1").fetchone()
        if row is None:
            return None
        conn.execute("UPDATE convert_candidate SET status = 'running', worker = ?, beat_at = ?, started_at = datetime('now'), percentage = 0 WHERE id = ?",
                     (worker, now, row["id"]))
    source = to_local(row["file_path"])
    return {"kind": "convert", "job_id": row["id"], "provider": "convert", "stash_id": str(row["id"]), "mode": row["mode"], "source": source,
            "dest": to_local(_written_path(row["file_path"])), "title": row["scene_title"],
            "accept_duration": row["accept_duration"]}


def progress(db, job_id, worker, percentage, speed_x, seconds_left):
    """False when the worker no longer holds the job (cancelled, expired): it stops."""
    with db.connect() as conn:
        return conn.execute("UPDATE convert_candidate SET beat_at = ?, percentage = ?, speed_x = ?, seconds_left = ? WHERE id = ? AND status = 'running' AND worker = ?",
                            (time.time(), percentage, speed_x, seconds_left, int(job_id), worker)).rowcount == 1


def worker_done(db, job_id, worker, ok, error, finish):
    """The worker reports its result. Returns an error text when the worker does not hold the job. On success `finish(row)` runs in the background."""
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM convert_candidate WHERE id = ? AND status = 'running' AND worker = ?", (int(job_id), worker)).fetchone()
        if row is None:
            return "this convert job is not (or no longer) assigned to this worker"
        if not ok:
            conn.execute("UPDATE convert_candidate SET status = 'failed', error = ?, finished_at = datetime('now'), worker = NULL WHERE id = ?",
                         (f"{worker}: {error or 'the conversion failed'}"[:500], row["id"]))
            return None
        conn.execute("UPDATE convert_candidate SET status = 'verifying', percentage = 100 WHERE id = ?", (row["id"],))
    threading.Thread(target=finish, args=(dict(row),), daemon=True, name=f"convert-finish-{row['id']}").start()
    return None


# ---------- the sidecar side: check, then swap the file into the scene ----------

class Retry(Exception):
    """A step that has to wait (Stash has not scanned the file yet): the row stays in its state and is tried again."""


def _fail(db, row_id, error):
    clear_phase(row_id)
    with db.connect() as conn:
        conn.execute("UPDATE convert_candidate SET status = 'failed', error = ?, finished_at = datetime('now'), worker = NULL WHERE id = ?", (error[:500], row_id))


def _set(db, row_id, **fields):
    with db.connect() as conn:
        conn.execute(f"UPDATE convert_candidate SET {', '.join(k + ' = ?' for k in fields)} WHERE id = ?", (*fields.values(), row_id))


def finish(db, stash_client, to_local, recycle, row, probe=transcode.probe):
    """Verifies the worker's file lightly (the worker already decoded all of it) and replaces the old file. Never raises."""
    row_id = row["id"]
    try:
        if not row.get("new_path"):  # new_path is set once the swap itself happened; a resumed run goes straight on
            set_phase(row_id, "checking the converted file")
            dst_local = to_local(_written_path(row["file_path"]))
            if not os.path.exists(dst_local):
                return _fail(db, row_id, f"the converted file is missing: {dst_local}")
            out = probe(dst_local)
            if not out or out.get("vcodec") != "h264":
                transcode._remove(dst_local)
                return _fail(db, row_id, "the converted file cannot be read or is not H.264")
            expected = row.get("accept_duration") or row.get("duration")  # accept_duration: the user accepted the real (shorter) length of a cut-off source
            if expected and abs(out["duration"] - expected) > max(2.0, expected * 0.01):
                transcode._remove(dst_local)
                return _fail(db, row_id, f"the converted file is {out['duration']:.0f} s long, the original {expected:.0f} s")
        _set(db, row_id, status="replacing")
        replace(db, stash_client, to_local, recycle, row)
    except Retry as e:
        _fail(db, row_id, f"{e} -- the converted file is kept next to the original; press Convert again to continue")
    except Exception as e:  # noqa: BLE001
        logger.exception("convert: replacing %s failed", row.get("file_path"))
        _fail(db, row_id, f"{type(e).__name__}: {e} -- the original file is still in place" if not row.get("new_path") else f"{type(e).__name__}: {e}")


def _written_path(old_path):
    """Where the worker writes the result (Stash path): next to the source, `.mp4`; a source that is already `.mp4` gets a temporary name."""
    stem, ext = os.path.splitext(old_path)
    return stem + (".srm-convert.mp4" if ext.lower() == ".mp4" else ".mp4")


def replace(db, stash_client, to_local, recycle, row):
    """Swaps the converted file into the scene. Every step looks at the state first, so a run that was cut off (restart, Stash busy) can be repeated.
    Order: the old file leaves only after the new one is in the scene and primary; `new_path` marks that point. `row`: a convert_candidate dict
    (Stash paths)."""
    with _replace_lock:
        old, scene_id, row_id = row["file_path"], str(row["scene_id"]), row["id"]
        written = _written_path(old)
        same_name = written != os.path.splitext(old)[0] + ".mp4"  # the old file is an .mp4 itself: the result is renamed onto it at the end
        final = old if same_name else written
        folder = old.rsplit("/", 1)[0]
        if not row.get("new_path"):
            files = stash_client.scene_files(scene_id)
            if files is None:
                raise RuntimeError(f"scene {scene_id} no longer exists in Stash")
            if written not in [f["path"] for f in files]:
                if not os.path.exists(to_local(written)):
                    raise RuntimeError("the converted file is missing")
                _wait_scan(stash_client, stash_client.scan_paths([folder]), row_id, "Stash scans the converted file")
                set_phase(row_id, "joining the converted file to the scene")
                found = stash_client.find_scene_by_path(written)
                if found is None:
                    raise Retry("Stash has not picked up the converted file yet")
                if str(found["id"]) != scene_id:
                    stash_client.merge_scene_into(found["id"], scene_id)
                files = stash_client.scene_files(scene_id) or []
            new_file = next((f for f in files if f["path"] == written), None)
            if new_file is None:
                raise Retry("the converted file is not part of the scene after the merge")
            stash_client.set_primary_file(scene_id, new_file["id"])
            set_phase(row_id, "moving the old file to the recycle bin")
            if os.path.exists(to_local(old)):
                recycle(to_local(old))
            _set(db, row_id, new_path=final)  # from here on only the tail is left
        if same_name and os.path.exists(to_local(written)):  # x.srm-convert.mp4 moves onto the freed x.mp4; Stash sees the move by the fingerprint
            os.rename(to_local(written), to_local(old))
            _wait_scan(stash_client, stash_client.scan_paths([folder]), row_id, "Stash scans the renamed file")
        set_phase(row_id, "cleaning up in Stash")
        with db.connect() as conn:
            conn.execute("DELETE FROM vr_file_check WHERE file_path = ?", (old,))
        try:
            stash_client.clean_paths([folder])
        except Exception:  # noqa: BLE001
            logger.exception("convert: Stash clean for %s failed (the old entry stays until the next clean)", folder)
        try:
            stash_client.generate_metadata_for_scenes([scene_id])
        except Exception:  # noqa: BLE001
            logger.exception("convert: Generate for scene %s could not be started", scene_id)
        size_after = os.path.getsize(to_local(final)) if os.path.exists(to_local(final)) else None
        clear_phase(row_id)
        _set(db, row_id, status="done", new_path=final, size_after=size_after, error=None, worker=None, finished_at=time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()))
        history.add("info", "history", "convert",
                    f"Converted to H.264 + AAC ({row['mode']}): {os.path.basename(old)} -> {os.path.basename(final)} "
                    f"({(row.get('size') or 0) / 1e6:.0f} MB -> {(size_after or 0) / 1e6:.0f} MB); the old file is in the recycle bin",
                    scene_id=scene_id, scene_title=row.get("scene_title"), details={"old": old, "new": final, "mode": row["mode"]})


def converted_paths(db):
    """Stash paths of files this page produced: their hashes are new, so the fingerprint check must not call them wrong."""
    with db.connect() as conn:
        return {r["new_path"] for r in conn.execute("SELECT new_path FROM convert_candidate WHERE status = 'done' AND new_path IS NOT NULL")}


def resume(db, stash_client, to_local, recycle):
    """After a restart: rows that were being checked / swapped continue; running rows keep their lease (the worker may still be encoding)."""
    with db.connect() as conn:
        todo = [dict(r) for r in conn.execute("SELECT * FROM convert_candidate WHERE status IN ('verifying', 'replacing')")]
    for row in todo:
        threading.Thread(target=finish, args=(db, stash_client, to_local, recycle, row), daemon=True).start()
