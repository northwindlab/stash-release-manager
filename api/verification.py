"""Verifies that a file Whisparr reports as downloaded for a scene is
actually that scene -- 2026-09-15, milestone 1 of the "get Whisparr out
of the workflow" roadmap (see PROGRESS.md's conversation on that date).

Whisparr never fingerprint-checks a grabbed release against StashDB
itself -- it trusts the indexer's own title/metadata match. A
mislabeled release (see PROGRESS.md's "Boosting Morale"/"Summer Camp
Rules" investigation: a real, live case where Whisparr's own download
for "Boosting Morale" turned out to be byte-identical to an unrelated,
already-owned scene) leaves Whisparr believing it satisfied a movie it
never actually matched, with no way for Whisparr itself to notice.

The earlier "wrong file" check (routers/releases.py's library_status(),
2026-09-14) only ever caught this by coincidence -- it compared the
downloaded file against whatever ELSE already existed in the local
library, so it only worked when the wrong content happened to collide
with a scene already owned. A genuinely wrong, previously-unseen file
(no local collision to catch it against) would scan in as a brand new,
wrongly-identified scene and pass silently. This module replaces that
narrower check with the real thing: comparing the local file's own
oshash/phash fingerprints directly against the ones StashDB has
registered for the *target* scene -- the correct source of truth,
independent of whatever else is or isn't already in the library.
"""
import history
import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


# Stash file id -> phash regeneration job id (in memory: after a restart a
# suspicious file just gets regenerated once more, harmless).
_phash_regen_jobs = {}
_STALE_FILE_MARGIN = timedelta(minutes=5)

# Set by main.py (v0.246.2): callable(scene, file, provider_scene_id, found, client) -- told when a file that matched its scene is also registered at OTHER
# provider scenes (re-released footage): the match then proves little, see shared_fingerprint.py. The verdict stays "verified" (the release was asked for
# by name); the hook records a hint (History + the Shared fingerprints list) unless the length singles the scene out.
shared_hook = None


def _parse_ts(value):
    try:
        return datetime.fromisoformat((value or "").replace("Z", "+00:00"))
    except ValueError:
        return None


def _fingerprints_may_be_stale(stash_client, scene, matching_file):
    """True while the file's stored fingerprints can't be trusted yet
    (2026-09-26, found live): Stash keeps its FILE record for a path when
    only the scene is deleted ("Fix wrong download" destroys the scene,
    the file goes to the recycle bin), and when a new download later lands
    on the same path it rescans the oshash but keeps the OLD phash. Two
    correct "Some Series with Host" downloads were rejected with the phash
    of the wrong file from three days earlier -- and the reverse (a wrong
    file inheriting a right phash) would verify falsely. Detected by the
    file record being older than the scene just created from it; then the
    phash is regenerated once (overwrite) and trusted after that job."""
    file_created = _parse_ts(matching_file.get("created_at"))
    scene_created = _parse_ts(scene.get("created_at"))
    if not file_created or not scene_created or file_created >= scene_created - _STALE_FILE_MARGIN:
        return False
    file_id = matching_file.get("id")
    job_id = _phash_regen_jobs.get(file_id)
    try:
        if job_id is None:
            _phash_regen_jobs[file_id] = stash_client.regenerate_phash(scene["id"])
            logger.warning("verify_downloaded_file: Stash file %s (%s) predates its scene %s -- its phash may "
                           "belong to an earlier file at this path, regenerating it before verifying",
                           file_id, matching_file.get("path"), scene["id"])
            history.add("info", "history", "import", "Stash kept an old fingerprint for this path -- "
                        "phash is recomputed before verifying", details={"path": matching_file.get("path")},
                        scene_title=scene.get("title"))
            return True
        return not stash_client.job_finished(job_id)
    except Exception:
        logger.exception("verify_downloaded_file: phash regeneration for scene %s failed", scene["id"])
        return True


def phash_distance(local_fingerprints, remote_fingerprints):
    """The smallest number of differing bits between a local and a remote phash (99 when either side has none)."""
    local = [h for algo, h in local_fingerprints if algo == "phash"]
    remote = [h for algo, h in remote_fingerprints if algo == "phash"]
    try:
        return min(bin(int(a, 16) ^ int(b, 16)).count("1") for a in local for b in remote)
    except ValueError:  # no pair, or a value that is not hex
        return 99


def verify_downloaded_file(stash_client, stashdb_client, scene_stashdb_id, file_path, phash_tolerance=0):
    """Returns a `(verified, found_title)` pair:
    - `(True, <title>)`: fingerprints match, genuinely verified.
    - `(False, <title>)`: fingerprints were compared and none match --
      a real mismatch. `<title>` is whatever Stash actually has at this
      path (its own possibly-wrong identification), handy to show the
      user what the file got mistaken for.
    - `(None, None)`: not enough information yet to say either way --
      Stash hasn't scanned this exact path, the file has no
      fingerprints yet, StashDB has none registered for this scene, or
      a lookup failed. Callers must treat this as "still unknown", not
      as a mismatch -- the whole point is to only ever act on a
      *confirmed* answer.

    `phash_tolerance` (v0.219.2): a phash within this many bits of one of the provider's counts as a match. 0 = exact hashes only (every
    normal download). A re-encode has new file hashes, but its perceptual hash stays close to the original's (the importer passes 8 then).

    `file_path` is expected to be Whisparr's own reported file path
    (e.g. `movie["movieFile"]["path"]`) -- the same path this project
    already reads for the older, narrower mismatch check."""
    scene = stash_client.find_scene_by_path(file_path)
    if scene is None:
        return None, None  # not scanned into Stash yet -- still just pending

    matching_file = next((f for f in scene["files"] if f["path"] == file_path), None)
    if matching_file is None:
        return None, None  # shouldn't happen (the query filtered on this exact path), best-effort anyway

    if _fingerprints_may_be_stale(stash_client, scene, matching_file):
        return None, None

    local_fingerprints = {
        (fp["type"].lower(), fp["value"]) for fp in matching_file["fingerprints"] if fp.get("value")
    }
    if not local_fingerprints:
        return None, None  # Stash hasn't generated fingerprints for this file yet

    try:
        remote_raw = stashdb_client.get_scene_fingerprints(scene_stashdb_id)
    except Exception:
        logger.exception("verify_downloaded_file: StashDB fingerprint lookup failed for scene %s",
                          scene_stashdb_id)
        history.event("error", "connection", "Fingerprint lookup at the metadata provider failed -- "
                      "verification waits (check the connection / container DNS)", dedupe_minutes=60)
        return None, None
    remote_fingerprints = {
        (fp["algorithm"].lower(), fp["hash"]) for fp in remote_raw if fp.get("hash")
    }
    if not remote_fingerprints:
        # The provider has nothing registered for this scene yet -- the
        # normal case for a brand-new ThePornDB scene (2026-09-25). Once
        # the local file is fully fingerprinted (phash present) there's
        # nothing more to wait for on OUR side: "unverified" lets the
        # caller import it now and re-check later (user decision, see
        # file_importer._finalize_verification()). Without a phash yet,
        # it's still just "not known yet".
        if any(algo == "phash" for algo, _ in local_fingerprints):
            return "unverified", scene["title"]
        return None, None

    matched = bool(local_fingerprints & remote_fingerprints)
    if not matched and phash_tolerance:
        matched = phash_distance(local_fingerprints, remote_fingerprints) <= phash_tolerance
    if not matched and not any(algo == "phash" for algo, _ in local_fingerprints):
        # No mismatch verdict without a local phash (2026-09-25, needed
        # once verification can run straight from Stash's
        # Scene.Create.Post hook, see file_importer.verify_created_scene()):
        # the scene can exist with only its oshash/md5 while the same
        # scan job is still computing the phash. An exact-file hash not
        # matching says little (StashDB may only know other encodes of
        # this scene); the perceptual hash is what can actually rule a
        # file out -- so without it the answer is "not yet known".
        return None, None
    if not matched:
        logger.warning(
            "verify_downloaded_file: scene %s's file at %s does NOT match any of StashDB's own "
            "registered fingerprints for it (found in Stash as scene id %s, title %r)",
            scene_stashdb_id, file_path, scene["id"], scene["title"],
        )
    if matched and shared_hook is not None and hasattr(stashdb_client, "find_scenes_by_fingerprints"):
        fps = [{"hash": v, "algorithm": t.upper()} for t, v in local_fingerprints if t in ("oshash", "phash", "md5")]
        try:
            found = stashdb_client.find_scenes_by_fingerprints([fps])[0] if fps else []
            if len(found) > 1 and any(f["id"] == scene_stashdb_id for f in found):
                shared_hook(scene, matching_file, scene_stashdb_id, found, stashdb_client)
        except Exception:
            logger.exception("verify_downloaded_file: the check for other scenes with the same fingerprint failed (the verification stands)")
    return matched, scene["title"]
