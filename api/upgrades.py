"""Upgrade search (2026-09-26): replace library files with a better version.

Agreed with the user 2026-09-26 (whole library, recycle bin, 6K / 15 GB):

Candidates (table `upgrade_candidate`, refreshed by refresh_candidates()):
- 'vr_2d'          -- VR scene whose file is flat (VR check verdict 'mono');
                      target: real stereoscopic VR.
- 'vr_mono'        -- monoscopic 360/180 VR ('vr_mono'); fine to keep, but
                      should become stereoscopic.
- 'vr_low_quality' -- stereo VR below `vr_upgrade_min_mbps` Mbit/s (default 8;
                      the width rule of earlier versions is gone, v0.237.4);
                      target: higher VR resolution, never above the 6K /
                      15 GB limits of indexer_search.vr_candidate_problem().
- 'below_cutoff'   -- non-VR file below the active quality profile's
                      cutoff_resolution (none while no cutoff is set).

Searching (search_one()): the normal search pipeline
(indexer_search.search_and_grab) with a `candidate_filter` that only lets
through candidates that are actually BETTER than the owned file, via the
sidecar's own search source (Prowlarr / direct indexers -- Whisparr can't
search scenes it doesn't track). `dry_run=True` searches and selects but
grabs/writes nothing. A real grab is recorded on the scene's release rows
(created if the scene never was one of ours), with `release.upgrade_of` =
the file to replace.

Replacing (on_upgrade_imported(), called from the import pipeline once the
new file is verified): the old file goes to the recycle bin (reversible for
the recycle-bin retention period) and its old Stash scene entry is removed.
An upgrade that could only be imported UNVERIFIED keeps the old file
('review').

2D protection (flag_2d_import()): when the VR check finds that a file THIS
sidecar just imported is flat/mono, that release is blocklisted for the
scene and the scene becomes an upgrade candidate; the file stays as a
placeholder until the stereo version arrives.
"""
import history
import json
import logging
import re
from datetime import datetime, timedelta, timezone

import quality_profile_repo
import release_repo
import vr_check
from indexer_search import candidate_url, search_and_grab, vr_height

logger = logging.getLogger(__name__)

REASONS = ("vr_2d", "vr_mono", "vr_low_quality", "below_cutoff")
# Candidates the USER creates (never found by refresh_candidates, never removed by it): "readd" = Search page "Re-add" (fetch the scene again
# although a file exists), "duration" = Maintenance > Duration mismatch (the file is clearly shorter / longer than the provider's length).
MANUAL_REASONS = ("readd", "duration")
_MONO_RE = re.compile(r"(?i)(?:\bmono\b|_mono_|mono360|\bmono[._-])")
# Stash's ResolutionEnum for "files below the cutoff" queries.
_RESOLUTION_ENUM = {720: "STANDARD_HD", 1080: "FULL_HD", 1440: "QUAD_HD", 2160: "FOUR_K"}

_VR_FILES_QUERY = """
query($t: [ID!]) {
  findScenes(scene_filter: {tags: {value: $t, modifier: INCLUDES, depth: -1}}, filter: {per_page: -1}) {
    scenes { id title files { path width height size duration } stash_ids { endpoint stash_id } }
  }
}
"""
_SCENES_BY_IDS_QUERY = """
query($ids: [ID!]) {
  findScenes(ids: $ids, filter: {per_page: -1}) {
    scenes { id title files { path width height size duration } stash_ids { endpoint stash_id } }
  }
}
"""
_LOW_RES_QUERY = """
query($t: [ID!], $r: ResolutionEnum!) {
  findScenes(scene_filter: {resolution: {value: $r, modifier: LESS_THAN},
                            tags: {value: $t, modifier: EXCLUDES, depth: -1},
                            groups: {modifier: IS_NULL}}, filter: {per_page: -1}) {
    scenes { id title files { path width height size duration } stash_ids { endpoint stash_id } }
  }
}
"""


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _num(get_setting, key, default):
    try:
        return float(get_setting(key, "") or default)
    except ValueError:
        return float(default)


def _provider_ref(scene, stash_client):
    ids = {s["endpoint"]: s["stash_id"] for s in scene.get("stash_ids") or []}
    for provider, endpoint in (("stashdb", stash_client.stashdb_endpoint), ("theporndb", stash_client.theporndb_endpoint)):
        if endpoint in ids:
            return provider, ids[endpoint]
    return None, None


# --- candidates ---------------------------------------------------------------

def list_candidates(db, statuses=("wanted", "grabbed", "review")):
    with db.connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM upgrade_candidate WHERE status IN ({','.join('?' * len(statuses))}) "
            "ORDER BY CASE reason WHEN 'vr_2d' THEN 0 WHEN 'vr_mono' THEN 1 WHEN 'vr_low_quality' THEN 2 ELSE 3 END, "
            "scene_title", statuses).fetchall()
    return [dict(r) for r in rows]


def get_candidate(db, cand_id):
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM upgrade_candidate WHERE id = ?", (int(cand_id),)).fetchone()
    return dict(row) if row else None


def set_candidate(db, cand_id, **fields):
    fields["updated_at"] = _now()
    cols = ", ".join(f"{k} = ?" for k in fields)
    with db.connect() as conn:
        conn.execute(f"UPDATE upgrade_candidate SET {cols} WHERE id = ?", (*fields.values(), int(cand_id)))


def _upsert(db, scene, file, reason, provider, provider_scene_id, provider_duration=None, is_vr=None):
    mbps = (file["size"] * 8 / file["duration"] / 1e6) if file.get("size") and file.get("duration") else None
    with db.connect() as conn:
        existing = conn.execute("SELECT id, status FROM upgrade_candidate WHERE scene_id = ? AND file_path = ?",
                                (str(scene["id"]), file["path"])).fetchone()
        if existing:
            # A decision (dismissed) or an upgrade in flight/finished is kept.
            conn.execute(
                """UPDATE upgrade_candidate SET reason = ?, scene_title = ?, provider = ?, provider_scene_id = ?,
                   cur_width = ?, cur_height = ?, cur_size = ?, cur_mbps = ?, cur_duration = ?, provider_duration = ?, is_vr = ?,
                   updated_at = ? WHERE id = ?""",
                (reason, scene.get("title") or "", provider, provider_scene_id, file.get("width"), file.get("height"),
                 file.get("size"), mbps, file.get("duration"), provider_duration, None if is_vr is None else int(bool(is_vr)),
                 _now(), existing["id"]))
            return existing["id"]
        cur = conn.execute(
            """INSERT INTO upgrade_candidate (scene_id, file_path, scene_title, provider, provider_scene_id, reason,
                                             cur_width, cur_height, cur_size, cur_mbps, cur_duration, provider_duration, is_vr)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (str(scene["id"]), file["path"], scene.get("title") or "", provider, provider_scene_id, reason,
             file.get("width"), file.get("height"), file.get("size"), mbps, file.get("duration"), provider_duration,
             None if is_vr is None else int(bool(is_vr))))
        return cur.lastrowid


def _scenes_by_ids(stash_client, ids, chunk=100):
    """The scenes with these Stash ids. An id that is gone from Stash (a scene deleted after the VR check had judged its file) makes Stash
    answer the whole request with an error ("scene with id N not found"): then the chunk is asked for one id at a time and the missing ones
    are skipped (v0.237.6)."""
    out = []
    for i in range(0, len(ids), chunk):
        part = ids[i:i + chunk]
        try:
            out += stash_client._graphql(_SCENES_BY_IDS_QUERY, {"ids": part})["findScenes"]["scenes"]
        except Exception:
            for one in part:
                try:
                    out += stash_client._graphql(_SCENES_BY_IDS_QUERY, {"ids": [one]})["findScenes"]["scenes"]
                except Exception:
                    logger.info("upgrade candidates: scene %s is gone from Stash, skipped", one)
    return out


def refresh_candidates(db, stash_client, get_setting, cutoff_resolution=None):
    """Rebuilds the 'wanted' list from the VR check results and the
    library's current files. Returns counts per reason."""
    min_mbps = _num(get_setting, "vr_upgrade_min_mbps", 8)
    with db.connect() as conn:
        verdicts = {(r["scene_id"], r["file_path"]): (r["verdict"], r["finding_status"], bool(r["verdict_manual"]))
                    for r in conn.execute("SELECT scene_id, file_path, verdict, finding_status, verdict_manual FROM vr_file_check")}
    found = set()
    counts = {r: 0 for r in REASONS}
    tag_id = stash_client.find_tag_id(vr_check.VR_TAG_NAME)
    if tag_id:
        vr_scenes = stash_client._graphql(_VR_FILES_QUERY, {"t": [str(tag_id)]})["findScenes"]["scenes"]
        # Plus scenes the VR check covered without the tag (mostly-VR
        # studios, see vr_check.VrCheckJob._vr_scenes()).
        seen_ids = {s["id"] for s in vr_scenes}
        extra_ids = sorted({sid for sid, _ in verdicts} - seen_ids, key=int)
        if extra_ids:
            vr_scenes += _scenes_by_ids(stash_client, extra_ids)
        for scene in vr_scenes:
            provider, pid = _provider_ref(scene, stash_client)
            for file in scene.get("files") or []:
                verdict, finding, manual = verdicts.get((scene["id"], file["path"]), (None, None, False))
                reason = None
                # A file the automatic check judged 2D waits on the VR page for the user's "2D" (v0.239.0): the automatic judgement is only a
                # suspicion (a tagged VR scene, a flat-looking file); once the user confirms it, the file is handled here.
                if verdict == "mono" and finding != "dismissed" and (manual or finding == "handled"):
                    reason = "vr_2d"
                elif verdict == "vr_mono":
                    reason = "vr_mono"
                elif verdict == "stereo":
                    # By bitrate, not by resolution or size (user, v0.237.4): a stereo VR file under `vr_upgrade_min_mbps` is worth replacing.
                    mbps = (file["size"] * 8 / file["duration"] / 1e6) if file.get("size") and file.get("duration") else None
                    if mbps is not None and mbps < min_mbps:
                        reason = "vr_low_quality"
                if reason:
                    _upsert(db, scene, file, reason, provider, pid)
                    found.add((str(scene["id"]), file["path"]))
                    counts[reason] += 1
    if cutoff_resolution and tag_id and _RESOLUTION_ENUM.get(int(cutoff_resolution)):
        data = stash_client._graphql(_LOW_RES_QUERY, {"t": [str(tag_id)], "r": _RESOLUTION_ENUM[int(cutoff_resolution)]})
        for scene in data["findScenes"]["scenes"]:
            provider, pid = _provider_ref(scene, stash_client)
            for file in scene.get("files") or []:
                if (file.get("height") or 0) < int(cutoff_resolution):
                    _upsert(db, scene, file, "below_cutoff", provider, pid)
                    found.add((str(scene["id"]), file["path"]))
                    counts["below_cutoff"] += 1
    # 'wanted' rows that no longer qualify (file replaced, now fine, ...).
    with db.connect() as conn:
        for r in conn.execute("SELECT id, scene_id, file_path FROM upgrade_candidate WHERE status = 'wanted' "
                              "AND reason NOT IN ('readd', 'duration')").fetchall():
            if (r["scene_id"], r["file_path"]) not in found:
                conn.execute("DELETE FROM upgrade_candidate WHERE id = ?", (r["id"],))
    logger.info("upgrade candidates refreshed: %s", counts)
    return counts


def add_manual_candidate(db, stash_client, stash_scene_id, reason, provider_duration=None):
    """The user wants this library scene fetched again (reason 'readd' or 'duration'). The scene's primary file is the one a verified
    download replaces (old file -> recycle bin, see on_upgrade_imported()). Returns the candidate id, or {"error"}."""
    if reason not in MANUAL_REASONS:
        return {"error": f"unknown reason {reason}"}
    data = stash_client._graphql(
        "query($ids: [Int!]) { findScenes(scene_ids: $ids) { scenes { id title tags { name } "
        "files { path width height size duration } stash_ids { endpoint stash_id } } } }", {"ids": [int(stash_scene_id)]})
    scenes = data["findScenes"]["scenes"]
    if not scenes or not scenes[0].get("files"):
        return {"error": "Stash has no file for this scene (any more)"}
    scene = scenes[0]
    provider, pid = _provider_ref(scene, stash_client)
    if not pid:
        return {"error": "the scene has no StashDB / ThePornDB id -- nothing to search by"}
    is_vr = any(t.get("name") == vr_check.VR_TAG_NAME for t in scene.get("tags") or [])
    return {"id": _upsert(db, scene, scene["files"][0], reason, provider, pid, provider_duration, is_vr)}


# --- searching ------------------------------------------------------------------

def _rejected_titles(cand):
    try:
        return json.loads(cand.get("rejected_titles") or "[]")
    except ValueError:
        return []


def duration_size_ok(cand, size):
    """Maintenance > Duration mismatch: is a release of this size plausibly the scene at the length the provider has? The file is
    shorter than the scene (sample, cut): the release must be clearly bigger than the file and not wildly bigger than the file scaled
    to the provider's length. File longer than the scene: the mirror image. No size / length known -> no (cannot be proven)."""
    cur_size, cur_d, prov_d = cand.get("cur_size"), cand.get("cur_duration"), cand.get("provider_duration")
    if not (size and cur_size and cur_d and prov_d):
        return False
    expected = cur_size * prov_d / cur_d
    if prov_d > cur_d:
        return size >= cur_size * 1.3 and size <= expected * 2.5
    return size <= cur_size * 0.8 and size >= expected * 0.4


def _better_filter(cand):
    reason, cur_h = cand["reason"], cand.get("cur_height") or 0
    if reason == "readd":
        return None  # the user asked for this scene again: whatever the normal search accepts (quality profile, blocklist, ...)
    from indexer_search import _review_identity
    rejected = {_review_identity(t) for t in _rejected_titles(cand)}

    def better(r):
        title = r.get("title") or ""
        if _review_identity(title) in rejected:
            return False  # the user rejected it for this file (Activity > Needs review)
        if reason == "duration":
            return duration_size_ok(cand, r.get("size"))
        if reason.startswith("vr_"):
            if _MONO_RE.search(title):
                return False  # never "upgrade" to another monoscopic file
            if reason == "vr_low_quality":
                return vr_height(title) > cur_h  # must PROVE a higher resolution
            return True  # vr_2d / vr_mono: any accepted stereo VR release is better
        resolution = ((r.get("quality") or {}).get("quality") or {}).get("resolution")
        return bool(resolution and resolution > cur_h)
    return better


def filter_for_scene(db, provider, pid):
    """The "must really be better" test for a scene that is being upgraded (a release row with `upgrade_of`), or None for an ordinary wishlist
    scene (v0.237.1). The regular searches -- the wishlist's own, RSS, targeted -- ask for it, so a candidate that went "To wishlist" is held
    to the same rule as one the upgrade search handles itself: only a release above the file in the library is taken, never a worse one, and
    the last-resort kinds (2160p scaled down, SD after N days) do not apply. A re-add accepts what the normal search accepts, without the
    last resorts."""
    rows = [r for r in _scene_rows(db, provider, pid) if r.get("upgrade_of")]
    if not rows:
        return None
    with db.connect() as conn:
        cand = conn.execute("SELECT * FROM upgrade_candidate WHERE provider_scene_id = ? AND file_path = ?",
                            (pid, rows[0]["upgrade_of"])).fetchone()
    if cand is None:
        return lambda r: True
    return _better_filter(dict(cand)) or (lambda r: True)


def _scene_rows(db, provider, pid):
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM release WHERE provider = ? AND scene_stashdb_id = ?", (provider, pid)).fetchall()]


def _prepare_release_rows(db, cand, detail, is_vr):
    """Makes sure the scene has release rows the grab can be recorded on,
    marked with the file to replace. Returns the previous row states so a
    search that grabbed nothing can be undone."""
    provider, pid = cand["provider"], cand["provider_scene_id"]
    rows = _scene_rows(db, provider, pid)
    previous = [(r["entity_type"], r["entity_stashdb_id"], r["status"]) for r in rows]
    if not rows:
        scene = {"id": pid, "title": detail.get("title") or cand["scene_title"], "date": detail.get("date"),
                 "created": datetime.now(timezone.utc).isoformat(), "studio": detail.get("studio") or {},
                 "is_vr": is_vr, "details": detail.get("details") or ""}
        with db.connect() as conn:
            # Pseudo entity: a library scene outside the watchlist still
            # needs a row for the download/import pipeline to follow.
            release_repo.insert_release(db, conn, scene, provider, "studio", f"upgrade:{cand['scene_id']}",
                                        status="sent_to_whisparr")
    with db.connect() as conn:
        conn.execute("UPDATE release SET status = 'sent_to_whisparr', upgrade_of = ? WHERE provider = ? "
                     "AND scene_stashdb_id = ?", (cand["file_path"], provider, pid))
    # What was blocked for this candidate holds for the rows the search works on: a row made only for the upgrade is new and knows nothing.
    known = set(release_repo.get_review_rejected_titles(db, provider, pid) or [])
    for entry in _rejected_titles(cand):
        if entry and entry not in known:
            release_repo.add_review_rejected_title(db, provider, pid, entry)
    return previous


def return_to_upgrade_list(db, provider, pid):
    """A wrong upgrade download was thrown away (or given to another scene): nothing is downloading any more, so the candidate goes from
    'grabbed' back to 'wanted' (v0.262.3 -- it stayed 'grabbed' and refused every search). The release rows go back as they were before the
    grab (the row made only for the upgrade is removed), so that only the upgrade search works on the scene again; what is blocked moves
    to the candidate. A candidate in any other state (moved to the wishlist by the user) is left alone. True when it went back."""
    rows = _scene_rows(db, provider, pid)
    old_path = next((r.get("upgrade_of") for r in rows if r.get("upgrade_of")), None)
    if not old_path:
        return False
    with db.connect() as conn:
        cand = conn.execute("SELECT * FROM upgrade_candidate WHERE provider = ? AND provider_scene_id = ? AND file_path = ?",
                            (provider, pid, old_path)).fetchone()
    if cand is None or cand["status"] != "grabbed":
        return False
    cand = dict(cand)
    blocked = _rejected_titles(cand)
    for r in rows:
        try:
            blocked += [t for t in json.loads(r.get("review_rejected_titles") or "[]")]
        except ValueError:
            pass
    pseudo = f"upgrade:{cand['scene_id']}"
    previous = [(r["entity_type"], r["entity_stashdb_id"], "imported" if r.get("imported_at") else "new")
                for r in rows if r["entity_stashdb_id"] != pseudo]
    release_repo.set_download_job_for_scene(db, provider, pid, None, None)
    release_repo.set_import_error_for_scene(db, provider, pid, None, None)
    release_repo.set_pending_import_path_for_scene(db, provider, pid, None)
    release_repo.set_mismatch_found_for_scene(db, provider, pid, None)
    _undo_release_rows(db, cand, previous)
    if previous:
        with db.connect() as conn:
            conn.execute("DELETE FROM release WHERE provider = ? AND scene_stashdb_id = ? AND entity_stashdb_id = ?", (provider, pid, pseudo))
    set_candidate(db, cand["id"], status="wanted", review_candidate=None, rejected_titles=json.dumps(list(dict.fromkeys(blocked))),
                  last_search_result="rejected")
    return True


def repair_stranded_grabs(db):
    """Start-up: candidates left 'grabbed' by a wrong download thrown away before v0.262.3 -- their rows wait without a download, a file or an
    error, and something is blocked for the scene. Returns how many went back to 'wanted'."""
    with db.connect() as conn:
        grabbed = [dict(r) for r in conn.execute("SELECT * FROM upgrade_candidate WHERE status = 'grabbed'")]
    fixed = 0
    for cand in grabbed:
        rows = [r for r in _scene_rows(db, cand["provider"], cand["provider_scene_id"]) if r.get("upgrade_of") == cand["file_path"]]
        idle = rows and all(r["status"] == "sent_to_whisparr" and not r.get("download_job_id") and not r.get("pending_import_path")
                            and not r.get("import_error_code") for r in rows)
        if idle and any(json.loads(r.get("review_rejected_titles") or "[]") for r in rows):
            if return_to_upgrade_list(db, cand["provider"], cand["provider_scene_id"]):
                history.scene("info", "upgrade", "Upgrade is wanted again (it was still marked as downloading)", cand["provider"],
                              cand["provider_scene_id"], scene_title=cand["scene_title"])
                fixed += 1
    return fixed


def _undo_release_rows(db, cand, previous):
    provider, pid = cand["provider"], cand["provider_scene_id"]
    with db.connect() as conn:
        if not previous:
            conn.execute("DELETE FROM release WHERE provider = ? AND scene_stashdb_id = ? AND entity_stashdb_id = ?",
                         (provider, pid, f"upgrade:{cand['scene_id']}"))
            return
        for entity_type, entity_id, status in previous:
            conn.execute("""UPDATE release SET status = ?, upgrade_of = NULL WHERE provider = ? AND scene_stashdb_id = ?
                            AND entity_type = ? AND entity_stashdb_id = ?""", (status, provider, pid, entity_type, entity_id))


def search_one(db, cand_id, get_provider_client, search_source, quality_profile, vr_enabled, dry_run=True):
    cand = get_candidate(db, cand_id)
    if cand is None:
        return {"error": "candidate not found"}
    if not cand.get("provider_scene_id"):
        return {"error": "scene has no StashDB/ThePornDB link -- nothing to search by"}
    if cand["status"] == "grabbed" and not dry_run:
        return {"error": "an upgrade download is already running for this file"}
    if search_source is None or not getattr(search_source, "available", False):
        return {"error": "upgrade search needs Prowlarr or direct indexers as search backend (Settings > Indexers)"}
    client = get_provider_client(cand["provider"])
    detail = client.get_scene_detail(cand["provider_scene_id"]) if client else None
    if not detail:
        return {"error": f"{cand['provider']} doesn't know this scene (any more)"}
    if cand.get("quality_profile_id"):  # a profile set for this candidate (Upgrade wanted, bulk) beats the one of the Settings
        own = quality_profile_repo.get_one(db, int(cand["quality_profile_id"]))
        if own is not None:
            quality_profile = own
    is_vr = cand["reason"].startswith("vr_") or bool(cand.get("is_vr"))
    performers = [p["performer"]["name"] for p in detail.get("performers") or [] if (p.get("performer") or {}).get("name")]
    previous = None if dry_run else _prepare_release_rows(db, cand, detail, is_vr)
    try:
        result = search_and_grab(
            db, None, None, cand["provider"], cand["provider_scene_id"], detail.get("title") or cand["scene_title"],
            detail.get("date"), performers, quality_profile, is_vr=is_vr, vr_enabled=vr_enabled,
            search_source=search_source, studio_name=(detail.get("studio") or {}).get("name"),
            candidate_filter=_better_filter(cand) or (lambda r: True),  # never the last-resort kinds (a re-add included)
            allow_unknown_resolution=True if cand["reason"] == "readd" else None, dry_run=dry_run)
    except Exception as e:
        logger.exception("upgrade search failed for candidate %s", cand_id)
        if previous is not None:
            _undo_release_rows(db, cand, previous)
        return {"error": f"{type(e).__name__}: {e}"}
    if dry_run:
        return result
    grabbed = bool(result.get("grab"))
    # Keep a "needs review" candidate for Activity > Needs review (2026-09-28)
    # -- read before the undo below removes the rows it's stored on.
    review = None
    if result.get("result") == "needs_review":
        review = release_repo.get_review_candidate_for_scene(db, cand["provider"], cand["provider_scene_id"])
        if review and review.get("title") != (json.loads(cand["review_candidate"]).get("title")
                                              if cand.get("review_candidate") else None):
            history.scene("info", "review", f"Upgrade: possible better file needs review: {review.get('title')}",
                          cand["provider"], cand["provider_scene_id"], scene_title=cand["scene_title"])
    set_candidate(db, cand_id, review_candidate=json.dumps(review) if review else None)
    if not grabbed:
        _undo_release_rows(db, cand, previous)
    set_candidate(db, cand_id, last_search_at=_now(), last_search_result=result.get("result"),
                  status="grabbed" if grabbed else cand["status"],
                  detail=json.dumps(result.get("grab")) if grabbed else cand.get("detail"))
    return result


def to_wishlist(db, cand_id, get_provider_client):
    """"To wishlist" (v0.237.0): the candidate becomes a normal wishlist release (status sent_to_whisparr, marked with the file it replaces), so
    the regular wishlist search handles it -- all the rules of that search, not the "must be better" test of the upgrade search -- and it shows on
    Releases > On Wishlist (filter "Only upgrades"). The candidate itself gets the status 'wishlist' and is left alone by the upgrade search.
    Returns {"previous": release rows as they were (for the Undo), "status_before"} or {"error"}."""
    cand = get_candidate(db, cand_id)
    if cand is None:
        return {"error": "candidate not found"}
    if not cand.get("provider_scene_id"):
        return {"error": "scene has no StashDB/ThePornDB link -- identify it first"}
    if cand["status"] not in ("wanted", "review", "check"):
        return {"error": f"a candidate that is '{cand['status']}' cannot go to the wishlist"}
    client = get_provider_client(cand["provider"])
    detail = client.get_scene_detail(cand["provider_scene_id"]) if client else None
    if not detail:
        return {"error": f"{cand['provider']} doesn't know this scene (any more)"}
    is_vr = cand["reason"].startswith("vr_") or bool(cand.get("is_vr"))
    previous = _prepare_release_rows(db, cand, detail, is_vr)
    set_candidate(db, cand_id, status="wishlist")
    history.scene("info", "wishlist", "Upgrade moved to the wishlist (replaces the file in the library)", cand["provider"], cand["provider_scene_id"],
                  scene_title=cand["scene_title"])
    return {"previous": [list(x) for x in previous], "status_before": cand["status"]}


def undo_wishlist(db, cand_id, previous, status):
    """Undo of to_wishlist(): the release rows as they were, the candidate back to its old status."""
    cand = get_candidate(db, cand_id)
    if cand is None or cand["status"] != "wishlist" or status not in ("wanted", "review", "check"):
        return {"error": "nothing to undo"}
    _undo_release_rows(db, cand, [tuple(x) for x in previous or []])
    set_candidate(db, cand_id, status=status)
    return {"ok": True}


def accept_review(db, cand_id, get_provider_client):
    """Activity > Needs review "Accept" for an upgrade candidate: grabs the
    stored candidate exactly like the automatic upgrade path does."""
    cand = get_candidate(db, cand_id)
    if cand is None or not cand.get("review_candidate"):
        return {"error": "no review candidate stored for this file"}
    review = json.loads(cand["review_candidate"])
    return _grab_picked(db, cand, get_provider_client, review, "review", "Upgrade: possible better file accepted")


def manual_grab(db, cand_id, get_provider_client, picked):
    """Upgrade wanted > "Search…" > Grab (v0.106.0): the user picked a result of a typed search by hand. Recorded exactly like an
    automatic upgrade grab (release.upgrade_of = the old file), so after the verified import the old file goes to the recycle bin."""
    cand = get_candidate(db, cand_id)
    if cand is None:
        return {"error": "candidate not found"}
    if not cand.get("provider_scene_id"):
        return {"error": "scene has no StashDB/ThePornDB link -- the grab can't be recorded"}
    if cand["status"] == "grabbed":
        return {"error": "an upgrade download is already running for this file"}
    if not candidate_url(picked or {}):
        return {"error": "candidate.downloadUrl is required"}
    return _grab_picked(db, cand, get_provider_client, picked, "grab", "Upgrade: picked by hand from the manual search")


def _grab_picked(db, cand, get_provider_client, picked, category, message):
    from indexer_search import grab_candidate
    client = get_provider_client(cand["provider"])
    detail = client.get_scene_detail(cand["provider_scene_id"]) if client else None
    if not detail:
        return {"error": f"{cand['provider']} doesn't know this scene (any more)"}
    previous = _prepare_release_rows(db, cand, detail, cand["reason"].startswith("vr_") or bool(cand.get("is_vr")))
    history.scene("info", category, f"{message}: {picked.get('title')}",
                  cand["provider"], cand["provider_scene_id"], scene_title=cand["scene_title"])
    result = grab_candidate(db, cand["provider"], cand["provider_scene_id"], picked)
    if result.get("grab"):
        set_candidate(db, cand["id"], status="grabbed", review_candidate=None, last_search_at=_now(),
                      last_search_result=result.get("result"), detail=json.dumps(result["grab"]))
    else:
        _undo_release_rows(db, cand, previous)
    return result


def reject_review(db, cand_id):
    """Activity > Needs review "Reject": the title is never offered or
    grabbed for this file again."""
    cand = get_candidate(db, cand_id)
    if cand is None or not cand.get("review_candidate"):
        return {"error": "no review candidate stored for this file"}
    review = json.loads(cand["review_candidate"])
    rejected = _rejected_titles(cand) + [review.get("title")]
    set_candidate(db, cand_id, review_candidate=None, rejected_titles=json.dumps(rejected))
    history.scene("warning", "blocklist", f"Upgrade: possible better file rejected: {review.get('title')}",
                  cand["provider"], cand["provider_scene_id"], scene_title=cand["scene_title"])
    return {"ok": True}


def due_candidates(db, limit, min_hours=24):
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=min_hours)).strftime("%Y-%m-%d %H:%M:%S")
    with db.connect() as conn:
        rows = conn.execute(
            """SELECT id FROM upgrade_candidate WHERE status = 'wanted' AND provider_scene_id IS NOT NULL
               AND (last_search_at IS NULL OR last_search_at < ?)
               ORDER BY CASE reason WHEN 'readd' THEN -1 WHEN 'duration' THEN -1 WHEN 'vr_2d' THEN 0 WHEN 'vr_mono' THEN 1
                                    WHEN 'vr_low_quality' THEN 2 ELSE 3 END,
                        last_search_at IS NOT NULL, last_search_at LIMIT ?""", (cutoff, int(limit))).fetchall()
    return [r["id"] for r in rows]


# --- after the import -------------------------------------------------------------

def is_upgrade(db, provider, stash_id):
    return any(r.get("upgrade_of") for r in _scene_rows(db, provider, stash_id))


def on_upgrade_imported(db, stash_client, to_local_path, recycle, provider, stash_id, new_path, verified,
                        after_import=None):
    """Import-pipeline hook for an upgrade download (called INSTEAD of the
    normal tagging, see file_importer._finalize_verification()).

    Verified: the new file's freshly scanned scene is MERGED into the
    existing library scene (Stash sceneMerge -- keeps that scene's
    metadata, rating, markers, play and O history; the new file joins it),
    the new file becomes the primary file, the old file goes to the
    recycle bin and Stash's Clean drops its entry. Changed 2026-09-26 after
    the first live upgrade: destroying the old scene and tagging the new
    one lost the old scene's user data.

    Unverified: nothing is replaced -- the new file stays next to the old
    one for a manual look ('review'). Returns the scene id that now holds
    the file (for generate/VR check), or None."""
    rows = [r for r in _scene_rows(db, provider, stash_id) if r.get("upgrade_of")]
    if not rows:
        return None
    old_path = rows[0]["upgrade_of"]
    with db.connect() as conn:
        conn.execute("UPDATE release SET upgrade_of = NULL WHERE provider = ? AND scene_stashdb_id = ?", (provider, stash_id))
        cand = conn.execute("SELECT id FROM upgrade_candidate WHERE provider_scene_id = ? AND file_path = ?",
                            (stash_id, old_path)).fetchone()
    cand_id = cand["id"] if cand else None
    new_scene = stash_client.find_scene_by_path(new_path)
    if not verified:
        if cand_id:
            set_candidate(db, cand_id, status="review",
                          detail=json.dumps({"new_path": new_path, "note": "imported unverified -- old file kept"}))
        logger.warning("upgrade for %s imported unverified -- old file %s kept", stash_id, old_path)
        return new_scene["id"] if new_scene else None
    if old_path == new_path:
        # Same file name: the importer already moved the old file to the
        # recycle bin before writing the new one; Stash rescans the path.
        target = new_scene["id"] if new_scene else None
    else:
        old_scene = stash_client.find_scene_by_path(old_path)
        if old_scene is None and new_scene is not None:
            # The recorded path can be stale: the Organize engine moves/renames library files after the
            # candidate was recorded (2026-09-30: TmwVRnet/ -> "TMW VR NET/", the upgrade silently didn't
            # merge). The scene holding this provider id -- other than the fresh one -- IS the old scene,
            # and its own file is the one to replace.
            try:
                endpoint = stash_client.theporndb_endpoint if provider == "theporndb" else stash_client.stashdb_endpoint
                cand_scene = stash_client.find_scene_by_stashdb_id(stash_id, endpoint)
            except Exception:
                logger.exception("upgrade: provider-id lookup for the old scene of %s failed", stash_id)
                cand_scene = None
            if cand_scene and str(cand_scene["id"]) != str(new_scene["id"]):
                old_scene = cand_scene
                others = [f["path"] for f in stash_client.scene_files(old_scene["id"]) or [] if f["path"] != new_path]
                if len(others) == 1:
                    logger.warning("upgrade: recorded old path %s is stale, the scene's file is %s", old_path, others[0])
                    old_path = others[0]
        target = old_scene["id"] if old_scene else (new_scene["id"] if new_scene else None)
        if old_scene and new_scene and old_scene["id"] != new_scene["id"]:
            stash_client.merge_scene_into(new_scene["id"], old_scene["id"])
        files = stash_client.scene_files(target) if target else None
        new_file = next((f for f in files or [] if f["path"] == new_path), None)
        if new_file and target:
            stash_client.set_primary_file(target, new_file["id"])
        recycle(to_local_path(old_path))
        with db.connect() as conn:  # the replaced file's VR-check result is history now
            conn.execute("DELETE FROM vr_file_check WHERE file_path = ?", (old_path,))
        try:
            stash_client.clean_paths([old_path.rsplit("/", 1)[0]])
        except Exception:
            logger.exception("upgrade: Stash clean for %s failed (the old file entry stays until the next clean)",
                             old_path)
    if cand_id:
        set_candidate(db, cand_id, status="done",
                      detail=json.dumps({"new_path": new_path, "replaced": old_path, "scene_id": target}))
    logger.info("upgrade done for %s: %s replaced by %s in scene %s (old file in the recycle bin)",
                stash_id, old_path, new_path, target)
    history.scene("info", "upgrade", "Upgrade done: old file replaced (merged into the existing scene, "
                  "old file in the recycle bin)", provider, stash_id, details={"old": old_path, "new": new_path})
    if after_import and target:
        after_import(target, new_path)
    return target


def flag_2d_import(db, stash_client, scene_id, blocklist, only_path=None):
    """VR-check hook for a scene the sidecar just imported: if the file is
    flat/mono, blocklist our download of it and mark the scene for an
    upgrade (the file stays as a placeholder). `only_path` = the file that
    was just imported -- other files of the scene are not ours to judge."""
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM vr_file_check WHERE scene_id = ? AND verdict IN ('mono', 'vr_mono')",
                            (str(scene_id),)).fetchall()
    if only_path is not None:
        rows = [r for r in rows if r["file_path"] == only_path]
    if not rows:
        return None
    flagged = []
    for r in rows:
        releases = [x for x in _scene_rows(db, r["provider"], r["provider_scene_id"]) if x.get("download_job_id")]
        title = blocklist(releases[0]) if releases else None
        scene = {"id": scene_id, "title": r["scene_title"]}
        file = {"path": r["file_path"], "width": r["width"], "height": r["height"], "size": r["file_size"]}
        _upsert(db, scene, file, "vr_2d" if r["verdict"] == "mono" else "vr_mono", r["provider"], r["provider_scene_id"])
        if r["verdict"] == "mono":
            # Our own download turned out flat: the decision (blocklist + upgrade) is made here, so it does not wait on the VR page for the user's
            # "2D" and the next candidate refresh keeps it (v0.239.0).
            with db.connect() as conn:
                conn.execute("UPDATE vr_file_check SET finding_status = 'handled' WHERE scene_id = ? AND file_path = ? AND finding_status != 'dismissed'",
                             (str(scene_id), r["file_path"]))
        flagged.append({"file": r["file_path"], "blocklisted": title})
    logger.warning("VR check: imported scene %s is not stereo VR -- %s", scene_id, flagged)
    history.add("warning", "history", "vr_check", f"Imported file is not stereo VR ({flagged}) -- blocklisted",
                details={"stash_scene": scene_id})
    return flagged
