"""Milestone 2's own import/rename/move step (2026-09-17) -- what
Whisparr's own `DownloadedMovieImportService` would otherwise do, now
that `enableCompletedDownloadHandling` is turned off on the real
instance so Whisparr stops racing us to it.

Runs against whatever download_sources.py's pluggable source reports
(today: WhisparrQueueSource, reading Whisparr's own `/queue`) -- this
module never talks to Whisparr's queue/movie endpoints directly, only
to the plain dicts that source produces, so Milestone 3's own download-
client source can be swapped in later without any change here.

Path translation: a source item's `output_path` is in whichever
namespace produced it -- Whisparr's own view of the shared library
volume (e.g. `/data/...`, from `WHISPARR_ROOT_FOLDER_PATH`) for
`WhisparrQueueSource`, or SABnzbd's own container path (e.g.
`/downloads/...`) for `SabnzbdSource` (2026-09-19, see
download_sources.py) -- this sidecar runs on a different machine than
either and has that same physical share mounted at a different
container path instead. `import_one()`/`run_file_import()` take the
matching `(source_remote_root, local_mount)` pair as plain arguments
rather than assuming which source produced the item; main.py picks the
right pair based on the active `file_import_source` setting.
`_to_local_path()` does the one-prefix-swap translation itself.
"""
import history
import grp
import json
import logging
import os
import queue
import subprocess
import shutil
import threading
import time

import download_client_repo
import download_history_cleanup
import encode_queue
import file_naming
import library_check
import quality_detect
import recycle_bin
import import_progress
import release_repo
import transcode
from library_organize import PHASH_TOLERANCE
from sabnzbd_client import SabnzbdClient
from verification import verify_downloaded_file, phash_distance

logger = logging.getLogger(__name__)

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".avi", ".wmv", ".mov", ".m4v", ".webm", ".flv", ".mpg", ".mpeg"}


def _to_local_path(remote_path, whisparr_root, local_media_mount):
    """Swaps Whisparr's own root-folder prefix for this sidecar's own
    local mount of the same share. Raises if `remote_path` doesn't
    actually start with `whisparr_root` -- that would mean either
    setting is wrong, better to fail loudly than silently operate on
    the wrong path."""
    whisparr_root = whisparr_root.rstrip("/")
    local_media_mount = local_media_mount.rstrip("/")
    if not remote_path.startswith(whisparr_root):
        raise ValueError(
            f"'{remote_path}' doesn't start with the configured Whisparr root folder "
            f"'{whisparr_root}' -- check the whisparr_root_folder_path/local_media_mount settings"
        )
    return local_media_mount + remote_path[len(whisparr_root):]


def _find_media_file(local_path):
    """`local_path` may already be a single file (some releases) or a
    folder (most -- the release's own folder, possibly alongside nfo/
    sample/subtitle files). Returns the largest video-extension file
    found, since a bigger file is virtually always the real release
    rather than a sample."""
    if os.path.isfile(local_path):
        return local_path
    candidates = []
    for root, _dirs, files in os.walk(local_path):
        for name in files:
            if os.path.splitext(name)[1].lower() in VIDEO_EXTENSIONS:
                full = os.path.join(root, name)
                candidates.append((os.path.getsize(full), full))
    if not candidates:
        raise FileNotFoundError(f"no video file found under {local_path}")
    return max(candidates, key=lambda c: c[0])[1]


_COPY_CHUNK = 8 * 1024 * 1024  # 8 MB


def _copy_with_progress(source_path, target_path, progress):
    """shutil.copy2() equivalent in 8 MB blocks, calling
    `progress(bytes_done, bytes_total)` about once a second (2026-09-26,
    import progress on the Activity page). Same result as copy2: data,
    then permission bits + timestamps via copystat()."""
    total = os.path.getsize(source_path)
    done, last_report = 0, 0.0
    with open(source_path, "rb") as src, open(target_path, "wb") as dst:
        while True:
            chunk = src.read(_COPY_CHUNK)
            if not chunk:
                break
            dst.write(chunk)
            done += len(chunk)
            now = time.monotonic()
            if progress and now - last_report >= 1.0:
                progress(done, total)
                last_report = now
    shutil.copystat(source_path, target_path)
    if progress:
        progress(done, total)


def _move_file(source_path, target_path, use_hardlinks, progress=None, keep_source=False):
    """Unlike Whisparr's own hardlink option (which deliberately keeps
    the source file in place, for an active torrent still being
    seeded), the source is always removed here once the target exists
    -- this project's own download client is usenet-only (SABnzbd, no
    seeding concept), so there's no reason to keep the download
    folder's own copy around, and leaving it behind would silently
    defeat `_cleanup_source_dir()`'s "remove the now-empty download
    folder" job every single time (confirmed live 2026-09-17: the
    first version of this function returned right after `os.link()`
    without removing the source, leaving the source folder non-empty
    forever).

    `keep_source` (v0.135.0, torrents): the download is still being seeded, so the source file stays where it is -- hard-linked into the
    library when `use_hardlinks` is on (and both are on one volume), else copied. Same as the *arr applications."""
    os.makedirs(os.path.dirname(target_path), exist_ok=True)
    if use_hardlinks:
        try:
            os.link(source_path, target_path)
            if not keep_source:
                os.remove(source_path)
            return
        except OSError:
            logger.info("_move_file: hardlink failed (likely cross-device), falling back to copy: %s -> %s",
                        source_path, target_path)
    if progress:
        _copy_with_progress(source_path, target_path, progress)
    else:
        shutil.copy2(source_path, target_path)
    if not keep_source:
        os.remove(source_path)


def _delete_extra_files(local_output_path):
    """Removes every file left behind under a completed download's own
    folder once its video has already been moved out by `_move_file()`
    (samples, nfo, thumbnails, subtitles, par2/sfv repair files, etc.)
    -- added 2026-09-19 per the user's own explicit request. This is
    NOT Whisparr's "Import Extra Files" feature (which copies
    subtitles/nfo into the library alongside the video); the user
    doesn't want that, just wants the download folder actually empty
    afterward so `_cleanup_source_dir()`'s "remove the now-empty
    download folder" step has something to do -- confirmed live
    2026-09-19 that without this, a leftover extra (a thumbnail, in
    that case) means the folder is never empty and sits there forever,
    since `_cleanup_source_dir()`'s `os.removedirs()` silently no-ops
    on a non-empty directory.

    `local_output_path` may already be a single file (nothing left to
    delete -- `_move_file()` already removed it) or a folder; this only
    does anything for the folder case. Best-effort per file/subfolder,
    same reasoning as `_cleanup_source_dir()` below -- one file this
    process can't remove (permissions, still open elsewhere) shouldn't
    fail the whole import that already succeeded."""
    if os.path.isfile(local_output_path):
        return
    for root, dirs, files in os.walk(local_output_path, topdown=False):
        for name in files:
            try:
                os.remove(os.path.join(root, name))
            except OSError:
                logger.warning("_delete_extra_files: couldn't remove leftover file %s",
                                os.path.join(root, name))
        for name in dirs:
            try:
                os.rmdir(os.path.join(root, name))
            except OSError:
                pass  # not empty (a leftover file above we couldn't remove) -- leave it


def _apply_permissions(target_path, set_permissions, chmod_folder, chown_group):
    """Whisparr's own Media Management > Permissions section (Set
    Permissions / chmod Folder / chown Group), added 2026-09-19 --
    third of the four re-prioritized Milestone 2 file-handling gaps.
    Only touches the imported file itself and its immediate containing
    folder (the scene's own folder) -- deliberately NOT recursive up
    into pre-existing Studio/library ancestor folders, which may
    already hold other scenes with permissions set for other reasons
    unrelated to this one import.

    `chmod_folder` is an octal string (e.g. "755"); the file itself
    gets that same mode with every execute bit cleared (standard
    convention -- a media file is never executable, only its
    containing folder needs the execute bit to be enterable).
    `chown_group` only changes the GROUP, never the owner (`-1` for
    uid) -- changing group membership is commonly permitted without
    root, changing owner usually isn't, and Whisparr's own setting is
    named "chown *Group*" specifically for the same reason.

    Best-effort: a permission/chown failure (e.g. this process isn't in
    the target group, or isn't allowed to chmod at all on this
    filesystem) is logged and swallowed rather than failing an import
    that otherwise succeeded -- same reasoning as
    `_cleanup_source_dir()`'s own best-effort cleanup."""
    if not set_permissions or (not chmod_folder and not chown_group):
        return
    folder_path = os.path.dirname(target_path)
    try:
        gid = None
        if chown_group:
            try:
                gid = int(chown_group)
            except ValueError:
                gid = grp.getgrnam(chown_group).gr_gid

        for path, is_folder in ((folder_path, True), (target_path, False)):
            if chmod_folder:
                mode = int(chmod_folder, 8)
                os.chmod(path, mode if is_folder else (mode & 0o666))
            if gid is not None:
                os.chown(path, -1, gid)
    except (OSError, LookupError, ValueError) as e:
        logger.warning("_apply_permissions: couldn't set permissions/group on %s: %s", target_path, e)


def _recycle_existing_target(target_path, local_media_mount, recycle_bin_path):
    """Moves whatever's already at `target_path` into the recycle bin
    instead of letting `_move_file()` below silently overwrite/destroy
    it (`os.link()` fails with `FileExistsError` when the target
    already exists, and the fallback `shutil.copy2()` overwrites in
    place) -- added 2026-09-19, the safety net the same day's real
    data-loss bug (see PROGRESS.md) didn't have. No-op in the
    overwhelming common case: a genuinely new scene has nothing at
    `target_path` yet.

    Recycled under a timestamp suffix (not the original name alone) so
    two replacements of the same scene don't collide with each other in
    the bin -- see recycle_bin.py's own cleanup job for when these
    actually get deleted for good."""
    if not os.path.exists(target_path):
        return
    root = recycle_bin.recycle_bin_root(local_media_mount, recycle_bin_path)
    os.makedirs(root, exist_ok=True)
    dest = os.path.join(root, f"{os.path.basename(target_path)}.{int(time.time())}")
    shutil.move(target_path, dest)
    try:  # the retention counts from NOW, not from the file's old mtime (see recycle_bin.cleanup_recycle_bin)
        os.utime(dest, None)
    except OSError:
        pass
    logger.warning("_recycle_existing_target: %s already existed -- moved it to the recycle bin (%s) "
                    "before replacing it", target_path, dest)


def _cleanup_source_dir(output_path):
    """Best-effort: remove the now-empty download folder (and any
    empty parent directories left behind) -- swallows the error once a
    directory still has something left in it (extras we didn't move,
    or output_path was itself a single file with no folder to clean
    up), matching Whisparr's own "Delete empty folders" behavior."""
    if os.path.isfile(output_path):
        return
    try:
        os.removedirs(output_path)
    except OSError:
        pass


def _provider_client(provider, stashdb_client, get_provider_client):
    """Metadata client for a release's provider (2026-09-25 -- imports
    aren't StashDB-only any more). All of them share get_scene_detail()/
    get_scene_fingerprints() shapes, see theporndb_client.py."""
    if provider == "stashdb":
        return stashdb_client
    return get_provider_client(provider) if get_provider_client else None


def _provider_endpoint(provider, stash_client, provider_client):
    """The stash-box endpoint a provider's scene ids live on, for
    tagging via Stash's scraper: None = StashDB (stash_client's
    default), ThePornDB's stash-box, or a user-added stash-box's own URL."""
    if provider == "stashdb":
        return None
    if provider == "theporndb":
        return stash_client.theporndb_endpoint
    return getattr(provider_client, "url", None)


def _provider_label(provider):
    return {"stashdb": "StashDB", "theporndb": "ThePornDB"}.get(provider, provider)


def import_one(db, stash_client, stashdb_client, source_item, source_remote_root,
                source_local_mount, whisparr_root_folder_path, local_media_mount,
                scene_format, scene_folder_format, use_hardlinks, recycle_bin_path,
                set_permissions, chmod_folder, chown_group, get_provider_client=None):
    """Full pipeline for one completed download. Returns a short
    result dict for logging; never raises for an expected failure mode
    (missing file, verification mismatch) -- those are reported in the
    result instead so one bad item doesn't stop the rest of a batch.

    `source_remote_root`/`source_local_mount` translate `source_item`'s
    own `output_path` -- varies by which source produced it (see module
    docstring). `whisparr_root_folder_path`/`local_media_mount` are
    always the *destination* library's pair regardless of source, since
    the imported file always ends up in the same library either way."""
    stash_id = source_item["stash_id"]
    provider = source_item.get("provider") or "stashdb"
    # Live phase for the Activity page (2026-09-26, see import_progress.py);
    # always cleared again below -- once the file is moved, the database
    # (pending_import_path) carries the "verifying" phase instead.
    import_progress.set_phase(provider, stash_id, "preparing")
    try:
        return _import_one(db, stash_client, stashdb_client, source_item, source_remote_root,
                           source_local_mount, whisparr_root_folder_path, local_media_mount,
                           scene_format, scene_folder_format, use_hardlinks, recycle_bin_path,
                           set_permissions, chmod_folder, chown_group, get_provider_client, stash_id, provider)
    finally:
        if not encode_active(provider, stash_id):  # a queued / running encode keeps its own phase
            import_progress.clear(provider, stash_id)


def _import_one(db, stash_client, stashdb_client, source_item, source_remote_root,
                source_local_mount, whisparr_root_folder_path, local_media_mount,
                scene_format, scene_folder_format, use_hardlinks, recycle_bin_path,
                set_permissions, chmod_folder, chown_group, get_provider_client, stash_id, provider):
    provider_client = _provider_client(provider, stashdb_client, get_provider_client)
    if provider_client is None:
        return {"stash_id": stash_id, "status": "failed", "error": f"no metadata client for provider {provider!r}"}

    scene_detail = provider_client.get_scene_detail(stash_id)
    if scene_detail is None:
        # Permanent -- StashDB has genuinely lost this scene (deleted/
        # merged upstream), a retry will never succeed. Promoted to a
        # real 'import_failed' status (2026-09-23 -- previously this
        # was NEVER persisted at all, the release just silently stayed
        # 'sent_to_whisparr' forever) with no retry offered on the
        # Activity page for this reason code.
        gone = f"{_provider_label(provider)} no longer has this scene"
        release_repo.set_status_for_scene(db, provider, stash_id, "import_failed")
        release_repo.set_import_error_for_scene(db, provider, stash_id, "scene_gone", gone)
        history.scene("error", "import", f"Import failed: {gone}", provider, stash_id)
        return {"stash_id": stash_id, "status": "failed", "error": gone}

    # A client with its own path mapping (qBittorrent: remote/local path of the client) overrides the run's SABnzbd downloads pair.
    local_output_path = _to_local_path(source_item["output_path"], source_item.get("remote_root") or source_remote_root,
                                       source_item.get("local_mount") or source_local_mount)
    keep_source = bool(source_item.get("keep_source"))
    try:
        source_file = _find_media_file(local_output_path)
    except FileNotFoundError as e:
        # Deliberately does NOT change status (stays 'sent_to_whisparr')
        # -- 2026-09-23, user's own explicit choice: could be a genuine
        # race (source file not fully synced yet) or permanent (someone
        # deleted the download), can't tell which from here, so let the
        # existing automatic per-cycle retry keep running unchanged.
        # Only the reason becomes visible now (Activity page's "Stuck"
        # section), where it was previously silent.
        release_repo.set_import_error_for_scene(db, provider, stash_id, "source_missing", str(e))
        history.scene("warning", "import", "Downloaded file not found -- retrying automatically", provider, stash_id,
                      details={"error": str(e)}, dedupe_minutes=24 * 60)
        return {"stash_id": stash_id, "status": "failed", "error": str(e)}

    rejected = _reject_unreadable(db, provider, stash_id, source_file, local_output_path,
                                  local_media_mount, recycle_bin_path, keep_source=keep_source)
    if rejected is not None:
        return rejected
    reencoded = _maybe_reencode(db, provider, stash_id, source_file)
    if reencoded is not None:
        if reencoded.get("status") in ("failed", "waiting_encode"):
            return {"stash_id": stash_id, **reencoded}
        original_file, source_file = source_file, reencoded["file"]
    rejected = _reject_unwanted_resolution(db, provider, stash_id, source_file, local_output_path,
                                           local_media_mount, recycle_bin_path, keep_source=keep_source)
    if rejected is not None:
        return rejected

    # Source tag from the ORIGINAL release name when known (the download's
    # own file name often drops it), VR width class for VR scenes.
    rows = release_repo.list_rows_for_scene(db, provider, stash_id)
    release_name = (rows[0].get("grabbed_title") if rows else None) or os.path.basename(source_file)
    is_vr = bool(rows and rows[0].get("is_vr"))
    quality_full = quality_detect.detect_quality(source_file, release_name, is_vr=is_vr)
    naming_studio = _studio_name_for_naming(stash_client, stashdb_client, provider, provider_client, scene_detail)
    if naming_studio:
        scene_detail = {**scene_detail, "studio": {**(scene_detail.get("studio") or {}), "name": naming_studio}}
    from routers.settings import get_setting_value  # (here, not at the top: routers.settings imports half the app)
    scene_tags = [t for t in str((rows[0].get("tags") if rows else "") or "").split("|") if t] + (["VR"] if is_vr else [])
    tokens = file_naming.build_tokens(scene_detail, quality_full, tags=scene_tags,
                                      options=file_naming.naming_options(lambda key: get_setting_value(db, key, "[]")))
    extension = os.path.splitext(source_file)[1]
    relative_target = file_naming.render_path(scene_folder_format, scene_format, tokens, extension)
    target_path = os.path.join(local_media_mount.rstrip("/"), relative_target.lstrip("/"))

    _recycle_existing_target(target_path, local_media_mount, recycle_bin_path)
    if reencoded is not None:  # marked BEFORE the move: the verification that follows must not compare fingerprints of a new encode
        release_repo.set_import_error_for_scene(db, provider, stash_id, "reencoded", reencoded["detail"])
    copy_started = time.time()
    _move_file(source_file, target_path, use_hardlinks,
               progress=lambda done, total: import_progress.update_copy(provider, stash_id, done, total, copy_started),
               keep_source=False if reencoded is not None else keep_source)  # the encoded copy is ours; a torrent's original stays
    if reencoded is not None:
        _forget_encode(stash_id)
        if not keep_source:  # the 2160p original is not needed any more: the encode replaced it (a torrent's original stays for seeding)
            try:
                os.remove(original_file)
            except OSError:
                logger.warning("could not remove the 2160p original %s after the re-encode", original_file)
    _apply_permissions(target_path, set_permissions, chmod_folder, chown_group)
    if not keep_source:  # a torrent's folder stays complete for seeding
        _delete_extra_files(local_output_path)
        _cleanup_source_dir(local_output_path)

    remote_target_path = whisparr_root_folder_path.rstrip("/") + target_path[len(local_media_mount.rstrip("/")):]

    # Recorded BEFORE scan/verify, not after -- this is the one piece
    # retry_pending_verifications() needs to re-locate this exact file
    # later if verification doesn't resolve on the first attempt below
    # (the original download-folder source item is already gone by
    # this point, per _cleanup_source_dir() above, so nothing else
    # could find it again). Cleared once verification reaches a
    # terminal answer (True or False); left set for `None` on purpose.
    release_repo.set_pending_import_path_for_scene(db, provider, stash_id, remote_target_path)

    # Fire-and-forget since 2026-09-25 (was: wait_for_job() up to 600s,
    # then verify inline). With Stash's job queue busy -- a long
    # Generate job for a big VR file was enough, seen live -- every
    # import sat out the full 600s, blocked the whole run and ended in a
    # TimeoutError. Now verification is event-driven instead: Stash's
    # Scene.Create.Post hook (see stash-release-manager.yml) calls
    # verify_created_scene() below the moment the scan creates the
    # scene, and retry_pending_verifications() at the end of every
    # import run stays the safety net for anything the hook missed.
    stash_client.scan_paths([remote_target_path])
    return {"stash_id": stash_id, "status": "moved_unverified", "path": remote_target_path}


# Serializes the verify -> tag -> status-update step between the hook
# endpoint (verify_created_scene()) and the import run's own
# retry_pending_verifications(), which can overlap -- both run in this
# one sidecar process, just on different threads.
_finalize_lock = threading.Lock()

# Called with the new Stash scene id after a successful import (set by
# main.py, 2026-09-26: the VR "is it really stereo?" check). Runs in its
# own thread -- it reads frames from the file and must not hold up the
# import pipeline.
after_import_hook = None
# Upgrade search (2026-09-26, upgrades.py): called as
# (provider, stash_id, new_path, verified) once an import finishes, so an
# upgrade download can retire the file it replaces. Synchronous, cheap.
upgrade_import_hook = None
# callable(provider, stash_id) -> bool: is this import an upgrade download?
upgrade_is_pending = None


def _studio_name_for_naming(stash_client, stashdb_client, provider, provider_client, scene_detail):
    """Which spelling of the studio goes into file and folder names
    (2026-09-28, user rule): StashDB's name whenever the studio HAS a
    StashDB id; ThePornDB's (or another stash-box's) only when it has
    none. Found live: a ThePornDB import landed in "IntimatePOV/" next to
    every other file in StashDB's "Intimate POV/".

    A StashDB scene already carries StashDB's name -> None (keep it). For
    another provider the local Stash studio is looked up by that
    provider's studio id first; only without such an id by name/alias
    (spaces/punctuation ignored, unambiguous only) -- and then the local
    studio gets that provider's id linked, so the next import finds it by
    id (user: linked ids make the name search unnecessary). If that local
    studio carries a StashDB id, StashDB's current name is used.
    None = keep the provider's name. Best-effort.
    (Files already imported under another spelling are moved by the later
    bulk rename/move maintenance feature, not here.)"""
    if provider == "stashdb" or stashdb_client is None:
        return None
    studio = scene_detail.get("studio") or {}
    if not studio.get("name") and not studio.get("id"):
        return None
    try:
        endpoint = _provider_endpoint(provider, stash_client, provider_client)
        local_id = None
        if studio.get("id") and endpoint:
            local = stash_client.find_studio_by_stash_id(studio["id"], endpoint)
            local_id = local["id"] if local else None
        if local_id is None:
            local_id = stash_client.find_studio_id_by_name(studio.get("name"))
            if local_id is None:
                return None
            if studio.get("id") and endpoint:
                stash_client.add_studio_stash_id(local_id, endpoint, studio["id"])
        stashdb_id = next((s["stash_id"] for s in stash_client.studio_stash_ids(local_id)
                           if s["endpoint"] == stash_client.stashdb_endpoint), None)
        if not stashdb_id:
            return None
        return stashdb_client.get_studio_name(stashdb_id)
    except Exception:
        logger.exception("couldn't resolve the StashDB name for studio %r, using the provider's name",
                         studio.get("name"))
        return None


def unreadable(local_path, timeout=60):
    """None when ffprobe reads the file as a video (or ffprobe is not installed / the share is too slow to tell); else a short reason (v0.164.0). A download
    can be "completed" in SABnzbd and still be junk (a failed unpack, an obfuscated post renamed .wmv) -- found with "All Star POV": 660 MB of random bytes."""
    if shutil.which("ffprobe") is None:
        return None
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "default=nw=1", local_path],
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None
    if r.returncode != 0 or "codec_type=video" not in r.stdout:
        return ((r.stderr.strip().splitlines() or ["no video stream found"])[-1])[:200]
    return None


def _reject_unreadable(db, provider, stash_id, source_file, local_output_path,
                       local_media_mount, recycle_bin_path, keep_source=False):
    """Post-download readability check (v0.165.1, same idea as the movie import): a "completed" download that ffprobe cannot
    read as a video (failed unpack, obfuscated junk) can give no fingerprints and would only land in the library as a broken
    scene. Like _reject_unwanted_resolution(): the release is blocklisted for this scene, the file goes to the recycle bin
    (reversible) and the scene is searched again. Returns the result dict then, else None."""
    reason = unreadable(source_file)
    if not reason:
        return None
    rows = release_repo.list_rows_for_scene(db, provider, stash_id)
    title = rows[0].get("grabbed_title") if rows else None
    detail = (f"The downloaded file \"{os.path.basename(source_file)}\" is not a readable video ({reason}) -- probably a failed unpack. "
              "Nothing was imported; the release is blocklisted, "
              + ("the torrent stays in the client" if keep_source else "the file went to the recycle bin") + ", and the scene is searched again")
    logger.warning("scene %s: %s", stash_id, detail)
    if title:
        release_repo.add_review_rejected_title(db, provider, stash_id, title, exact=True)  # this file only, other encodes may be fine
    try:
        if not keep_source:
            _recycle_existing_target(source_file, local_media_mount, recycle_bin_path)
            _delete_extra_files(local_output_path)
            _cleanup_source_dir(local_output_path)
    except Exception:
        logger.exception("couldn't move the unreadable download %s to the recycle bin", source_file)
    release_repo.set_download_job_for_scene(db, provider, stash_id, None, None)
    release_repo.set_import_error_for_scene(db, provider, stash_id, "unreadable", detail)
    history.scene("warning", "import", f"Download rejected: {detail}", provider, stash_id)
    return {"stash_id": stash_id, "status": "failed", "error": detail}


# --- Re-encode as its own stage (v0.219.0) -----------------------------------------------------------------------------------------
# The encode takes an hour on a small NAS, so it must not hold the import run (and with it every other finished download) up: _maybe_reencode()
# only QUEUES the job (encode_queue.py) and returns "waiting_encode". The sidecar's own thread encodes one file at a time -- or, with the setting
# encode_target = "worker", an encode worker on another machine does (routers/encode_worker.py, v0.220.0). Either side writes a marker next to
# the result and starts an import run, which then finds the finished encode and imports it like a download. A restart loses the queue and a
# running local encode (no marker): the next import run queues it again.
_encode_thread = None
_encode_thread_guard = threading.Lock()
_encode_db = None
on_encode_done = None   # set by main.py: starts an import run


def encode_active(provider, stash_id):
    return encode_queue.active(provider, stash_id)


def _transcode_dir():
    path = os.path.join(os.environ.get("DATA_DIR", "/data"), "transcode")
    os.makedirs(path, exist_ok=True)
    return path


def _marker_path(stash_id):
    return os.path.join(_transcode_dir(), f"{stash_id}.done.json")


def _finished_encode(stash_id, source_file):
    """{"file", "detail"} of a finished encode of exactly this download, else None (a marker of another file is dropped)."""
    marker = _marker_path(stash_id)
    try:
        with open(marker) as f:
            data = json.load(f)
        if (data.get("source") == source_file and data.get("source_size") == os.path.getsize(source_file)
                and os.path.exists(data.get("file", ""))):
            return {"file": data["file"], "detail": data["detail"]}
    except (OSError, ValueError):
        pass
    _forget_encode(stash_id)
    return None


def _forget_encode(stash_id):
    try:
        os.remove(_marker_path(stash_id))
    except OSError:
        pass


def _reencode_plan(db, provider, stash_id, source_file):
    """None when this download needs no re-encode, a failed-result dict when it cannot be done, else {"info": probe of the source}."""
    rows = release_repo.list_rows_for_scene(db, provider, stash_id)
    if not rows:
        return None
    row = rows[0]
    if row.get("is_vr") or row.get("upgrade_of"):
        return None
    import indexer_search
    from routers.settings import resolve_quality_profiles
    if 2160 not in indexer_search.fallback_resolutions(db, row.get("scene_date")):
        return None  # switched off, or the scene is too young for a last resort
    profile, _ = resolve_quality_profiles(db)
    if 2160 in ((profile or {}).get("resolutions") or []):
        return None  # 2160p is wanted as it is
    info = transcode.probe(source_file)
    if not info or not info.get("height"):
        return None
    if quality_detect.vr_label(info["width"], info["height"]):
        return None
    if max(info["height"], int(info["width"] * 9 / 16)) < 2160:
        return None
    try:
        need = os.path.getsize(source_file) // 2
        if shutil.disk_usage(_transcode_dir()).free < need:
            raise OSError(f"not enough free space in {_transcode_dir()} (about {need // 10**9} GB needed)")
    except OSError as e:
        return _encode_failed(db, provider, stash_id, str(e))
    return {"info": info}


def _maybe_reencode(db, provider, stash_id, source_file):
    """A 2160p download taken as a last resort (indexer_search.fallback_resolutions(): the scene is old enough, nothing in the quality profile
    existed) is scaled down to 1080p before the import (v0.218.0, transcode.py). Returns None when this download needs nothing,
    {"file", "detail"} for a finished encode (in DATA_DIR/transcode, moved into the library like a download), {"status": "waiting_encode"}
    while the job is queued or running (v0.219.0), or a failed-result dict ({"status": "failed", ...}) when the encode did not work --
    nothing is imported then and the download stays where it is."""
    if encode_active(provider, stash_id):
        return {"status": "waiting_encode"}
    done = _finished_encode(stash_id, source_file)
    if done is not None:
        return done
    plan = _reencode_plan(db, provider, stash_id, source_file)
    if plan is None or plan.get("status") == "failed":
        return plan
    global _encode_db
    _encode_db = db
    encode_queue.add(db, provider, stash_id, source_file, plan["info"])
    _ensure_encode_thread()
    from routers.settings import get_setting_value
    import_progress.set_phase(provider, stash_id, "waiting_encode",
                              can_local=get_setting_value(db, "encode_target", "nas") == "worker")
    history.scene("info", "import", "Download complete -- queued for the re-encode to 1080p", provider, stash_id)
    return {"status": "waiting_encode"}


def _ensure_encode_thread():
    global _encode_thread
    with _encode_thread_guard:
        if _encode_thread is None or not _encode_thread.is_alive():
            _encode_thread = threading.Thread(target=_encode_worker, daemon=True, name="srm-encode")
            _encode_thread.start()


def _local_may_encode():
    """(wants_worker, wait_hours): whether the sidecar leaves jobs to a worker, and after how many hours it takes them itself (0 = never)."""
    from routers.settings import get_setting_value
    try:
        hours = float(get_setting_value(_encode_db, "encode_worker_wait_hours", "0") or 0)
    except ValueError:
        hours = 0.0
    return get_setting_value(_encode_db, "encode_target", "nas") == "worker", hours


def _encode_worker():
    while True:
        wants_worker, hours = _local_may_encode()
        job = encode_queue.claim_local(wants_worker, hours)
        if job is None:
            time.sleep(3)
            continue
        db, provider, stash_id = job["db"], job["provider"], job["stash_id"]
        try:
            _run_encode(db, provider, stash_id, job["source"], job["info"])
        except Exception:
            logger.exception("re-encode of scene %s crashed", stash_id)
            try:
                _encode_failed(db, provider, stash_id, "unexpected error, see the sidecar log")
            except Exception:
                logger.exception("could not record the failed encode of scene %s", stash_id)
        finally:
            _encode_finished(provider, stash_id)


def _encode_finished(provider, stash_id):
    encode_queue.remove(provider, stash_id)
    import_progress.clear(provider, stash_id)
    if on_encode_done:
        try:
            on_encode_done()
        except Exception:
            logger.exception("could not start the import run after an encode")


def remote_output_path(source_file, stash_id, info):
    """Where an encode worker writes its result: next to the download (both machines see that folder)."""
    return os.path.join(os.path.dirname(source_file), f"{stash_id}.srm-encode{transcode.output_extension(info)}")


def _write_marker(db, provider, stash_id, source_file, info, dst, hw, codec, seconds, out):
    detail = (f"Re-encoded from {info['width']}x{info['height']} to {out.get('width')}x{out.get('height')} {'H.264' if codec == 'h264' else 'HEVC'} "
              f"({'hardware' if hw else 'software'}, {seconds // 60} min {seconds % 60} s, "
              f"{os.path.getsize(source_file) / 1e9:.1f} GB -> {os.path.getsize(dst) / 1e9:.1f} GB) -- new file hashes, checked by phash after the scan")
    with open(_marker_path(stash_id), "w") as f:
        json.dump({"file": dst, "detail": detail, "source": source_file, "source_size": os.path.getsize(source_file)}, f)
    history.scene("info", "import", detail, provider, stash_id)


def finish_remote_encode(provider, stash_id, worker, ok, error=None, seconds=None, hw=False, codec="h265"):
    """An encode worker reports its result (routers/encode_worker.py). The sidecar checks the file itself before it trusts it -- in the
    background: decoding the whole file takes minutes on a NAS and the worker's request would time out (found 2026-10-17)."""
    encode_queue.adopt(provider, stash_id, worker)  # a restart queued the job again unclaimed while this worker kept encoding it
    job = encode_queue.get(provider, stash_id)
    if not job or job["claimed_by"] != worker:
        return {"error": "this encode is not (or no longer) assigned to this worker"}
    db = job["db"]
    if not ok:
        try:
            _encode_failed(db, provider, stash_id, f"{worker}: {error or 'the encode failed'}")
        finally:
            _encode_finished(provider, stash_id)
        return {"ok": True, "recorded": "failed"}
    encode_queue.start_verify(provider, stash_id, worker)
    import_progress.set_phase(provider, stash_id, "encoding", percentage=100, method=f"{worker}: checking the result")
    threading.Thread(target=_verify_remote_result, args=(provider, stash_id, worker, seconds, hw, codec), daemon=True,
                     name=f"verify-encode-{stash_id[:8]}").start()
    return {"ok": True, "recorded": "verifying"}


def _verify_remote_result(provider, stash_id, worker, seconds, hw, codec):
    job = encode_queue.get(provider, stash_id)
    if not job:
        return
    db, source_file, info = job["db"], job["source"], job["info"]
    dst = remote_output_path(source_file, stash_id, info)
    try:
        problem = transcode.verify_output(dst, info)
        if problem:
            transcode._remove(dst)
            _encode_failed(db, provider, stash_id, f"{worker}: {problem}")
            return
        _write_marker(db, provider, stash_id, source_file, info, dst, hw, codec, int(seconds or 0), transcode.probe(dst) or {})
    except Exception as e:  # noqa: BLE001
        logger.exception("checking the result of %s failed", stash_id)
        _encode_failed(db, provider, stash_id, f"{worker}: checking the result failed ({type(e).__name__}: {e})")
    finally:
        _encode_finished(provider, stash_id)


def _run_encode(db, provider, stash_id, source_file, info):
    from routers.settings import get_setting_value
    dst = os.path.join(_transcode_dir(), f"{stash_id}{transcode.output_extension(info)}")
    try:
        quality = int(get_setting_value(db, "transcode_quality", "24") or 24)
    except ValueError:
        quality = 24
    hw_mode = get_setting_value(db, "transcode_hw", "auto")
    codec = "h264" if get_setting_value(db, "transcode_codec", "h265") == "h264" and not info.get("hdr") else "h265"
    preset = get_setting_value(db, "transcode_preset", "fast")
    method = transcode.method_label("off" if info.get("hdr") else hw_mode, codec)
    import_progress.set_phase(provider, stash_id, "encoding", percentage=0, method=method)
    history.scene("info", "import", f"Re-encoding {info['width']}x{info['height']} to 1080p before the import ({method})", provider, stash_id)
    res = transcode.transcode(source_file, dst, quality=quality, hw_mode=hw_mode, info=info, codec=codec, preset=preset,
                              progress=lambda pct, speed, left: import_progress.set_phase(provider, stash_id, "encoding", percentage=pct,
                                                                                         speed_x=speed, seconds_left=left, method=method))
    if not res["ok"]:
        _encode_failed(db, provider, stash_id, res["error"])
        return
    _write_marker(db, provider, stash_id, source_file, info, dst, res["hw"], res.get("codec", "h265"), res["seconds"], res["out"] or {})


def _encode_failed(db, provider, stash_id, error):
    detail = f"Re-encoding the 2160p download to 1080p failed: {error}. Nothing was imported; the download is still in place."
    logger.warning("scene %s: %s", stash_id, detail)
    release_repo.set_status_for_scene(db, provider, stash_id, "import_failed")
    release_repo.set_import_error_for_scene(db, provider, stash_id, "encode_failed", detail)
    history.scene("error", "import", detail, provider, stash_id)
    return {"stash_id": stash_id, "status": "failed", "error": detail}


def _reject_unwanted_resolution(db, provider, stash_id, source_file, local_output_path,
                                local_media_mount, recycle_bin_path, keep_source=False):
    """Post-download resolution check (2026-09-26, user decision "c"): a
    release whose NAME carries no resolution may be grabbed (see
    indexer_search._select_best()'s allow_unknown_resolution), so its real
    resolution is only known now. Outside the quality profile -> the
    release goes on this scene's blocklist, the file to the recycle bin
    (reversible, never a hard delete), and the scene back on the wishlist
    for the next search. Returns the result dict then, else None.

    Only for such unlabeled grabs, not normal VR scenes or upgrades: a
    labelled "1080p" release is trusted as before (cropped 1920x1072/
    1920x800 encodes would otherwise read as 720p). The frame's width
    counts too (x 9/16) for the same reason."""
    rows = release_repo.list_rows_for_scene(db, provider, stash_id)
    if not rows:
        return None
    row = rows[0]
    title = row.get("grabbed_title")
    if not title or row.get("is_vr") or row.get("upgrade_of"):
        return None
    import indexer_search
    if indexer_search._RESOLUTION_RE.search(title):
        return None
    from routers.settings import resolve_quality_profiles
    profile, _ = resolve_quality_profiles(db)
    allowed = list((profile or {}).get("resolutions") or [])
    allowed += [r for r in indexer_search.fallback_resolutions(db, row.get("scene_date")) if r not in allowed]  # the last-resort kinds are wanted too
    width, height = quality_detect.probe_size(source_file)
    if not allowed or not height or quality_detect.vr_label(width, height):
        return None
    effective = max(height, int((width or 0) * 9 / 16))
    bucket = next((h for h, _label in quality_detect.RESOLUTION_BUCKETS if effective >= h), 0)
    if bucket in allowed:
        return None
    detail = (f"Downloaded {title!r}: the file is {width}x{height} ({bucket or 'below 480'}p class), "
              f"not in the quality profile ({', '.join(f'{r}p' for r in allowed)}) -- blocklisted, "
              + ("torrent left in the client, searching again" if keep_source else "file moved to the recycle bin, searching again"))
    logger.warning("scene %s: %s", stash_id, detail)
    release_repo.add_review_rejected_title(db, provider, stash_id, title, exact=True)  # the resolution belongs to THIS encode; others may be fine
    try:
        if not keep_source:  # a seeding torrent's files are not ours to move; the torrent stays in the client
            _recycle_existing_target(source_file, local_media_mount, recycle_bin_path)
            _delete_extra_files(local_output_path)
            _cleanup_source_dir(local_output_path)
    except Exception:
        logger.exception("couldn't move the rejected download %s to the recycle bin", source_file)
    release_repo.set_download_job_for_scene(db, provider, stash_id, None, None)
    release_repo.set_import_error_for_scene(db, provider, stash_id, "quality_rejected", detail)
    history.scene("warning", "import", f"Download rejected: {detail}", provider, stash_id)
    return {"stash_id": stash_id, "status": "failed", "error": detail}


def _after_upgrade(provider, stash_id, path, verified):
    if upgrade_import_hook is not None:
        try:
            return upgrade_import_hook(provider, stash_id, path, verified)
        except Exception:
            logger.exception("upgrade hook failed for scene %s", stash_id)
    return None


def _after_import(scene_id, path=None):
    if after_import_hook is not None:
        threading.Thread(target=after_import_hook, args=(scene_id, path), daemon=True).start()


def _finalize_verification(db, stash_client, provider_client, provider, stash_id, path):
    """Verifies one moved file and applies the outcome -- shared by
    verify_created_scene() and retry_pending_verifications(). Returns the
    result dict for a terminal answer, or None when it's still unknown
    (pending_import_path stays set, retried later). Caller holds
    _finalize_lock and has re-checked the release is still pending."""
    endpoint = _provider_endpoint(provider, stash_client, provider_client)
    rows = release_repo.list_rows_for_scene(db, provider, stash_id)
    reencoded = bool(rows and rows[0].get("import_error_code") == "reencoded")
    # A re-encode of a 2160p download (transcode.py) has new file hashes, but its perceptual hash stays near the original's: compared with
    # a tolerance (verification.phash_tolerance). No fingerprints at the provider -> imported without the comparison, the note stays.
    verified, found_title = verify_downloaded_file(stash_client, provider_client, stash_id, path,
                                                   phash_tolerance=PHASH_TOLERANCE if reencoded else 0)
    if reencoded and verified in (True, "unverified"):
        scene_id = _tag_imported_scene(stash_client, stash_id, path, endpoint)
        if scene_id is not None:
            _trigger_generate(stash_client, scene_id)
            _after_import(scene_id, path)
        release_repo.set_pending_import_path_for_scene(db, provider, stash_id, None)
        release_repo.set_status_for_scene(db, provider, stash_id, "imported")
        how = "phash matches the provider's" if verified is True else "the provider has no fingerprints for this scene"
        history.scene("info", "import", f"Imported (re-encoded to 1080p; {how})", provider, stash_id, details={"path": path})
        return {"stash_id": stash_id, "status": "imported", "path": path, "reencoded": True}
    if verified is True and upgrade_is_pending is not None and upgrade_is_pending(provider, stash_id):
        # Upgrade download (2026-09-26): merged into the existing library
        # scene by the hook instead of tagging a new one -- see
        # upgrades.on_upgrade_imported().
        release_repo.set_pending_import_path_for_scene(db, provider, stash_id, None)
        release_repo.set_status_for_scene(db, provider, stash_id, "imported")
        release_repo.set_import_error_for_scene(db, provider, stash_id, None, None)
        scene_id = _after_upgrade(provider, stash_id, path, True)
        if scene_id is not None:
            _trigger_generate(stash_client, scene_id)
        history.scene("info", "import", "Upgrade imported and verified", provider, stash_id, details={"path": path})
        return {"stash_id": stash_id, "status": "imported", "path": path, "upgrade": True}
    if verified is True:
        scene_id = _tag_imported_scene(stash_client, stash_id, path, endpoint)
        if scene_id is not None:
            _trigger_generate(stash_client, scene_id)
            _after_import(scene_id, path)
        release_repo.set_pending_import_path_for_scene(db, provider, stash_id, None)
        release_repo.set_status_for_scene(db, provider, stash_id, "imported")
        release_repo.set_import_error_for_scene(db, provider, stash_id, None, None)
        history.scene("info", "import", "Imported and verified", provider, stash_id, details={"path": path})
        return {"stash_id": stash_id, "status": "imported", "path": path}
    if verified == "unverified" and upgrade_is_pending is not None and upgrade_is_pending(provider, stash_id):
        # Unverified upgrade: keep the old file, don't tag the new scene
        # (the old scene still holds the provider id) -- 'review' for a
        # manual look, see upgrades.on_upgrade_imported().
        release_repo.set_pending_import_path_for_scene(db, provider, stash_id, None)
        release_repo.set_status_for_scene(db, provider, stash_id, "imported")
        _after_upgrade(provider, stash_id, path, False)
        history.scene("warning", "import", "Upgrade imported UNVERIFIED -- old file kept for review", provider,
                      stash_id, details={"path": path})
        return {"stash_id": stash_id, "status": "imported", "path": path, "upgrade": True, "unverified": True}
    if verified == "unverified":
        # Provider has no fingerprints for this scene yet (2026-09-25,
        # user decision): import now, visibly marked, and keep
        # pending_import_path so recheck_unverified_imports() can
        # verify it once fingerprints show up. Tagged normally -- the
        # pre-download title/date/performer checks already chose it.
        scene_id = _tag_imported_scene(stash_client, stash_id, path, endpoint)
        if scene_id is not None:
            _trigger_generate(stash_client, scene_id)
            _after_import(scene_id, path)
        release_repo.set_status_for_scene(db, provider, stash_id, "imported")
        release_repo.set_import_error_for_scene(
            db, provider, stash_id, "unverified",
            f"Imported unverified -- {_provider_label(provider)} has no fingerprints for this scene yet; "
            "re-checked automatically")
        history.scene("warning", "import",
                      f"Imported unverified -- {_provider_label(provider)} has no fingerprints yet, re-checked automatically",
                      provider, stash_id, details={"path": path})
        return {"stash_id": stash_id, "status": "imported", "path": path, "unverified": True}
    if verified is False:
        # pending_import_path deliberately LEFT SET (2026-09-23) -- the
        # "Fix wrong download" retry action needs the exact wrong-file
        # path to clean it up. Safe to leave set:
        # list_pending_sidecar_verifications() only ever looks at
        # 'sent_to_whisparr' rows, and this release is now 'import_failed'.
        release_repo.set_status_for_scene(db, provider, stash_id, "import_failed")
        # Readable reason (2026-09-26: "Stash has it as ''" said nothing --
        # the freshly scanned scene has no title yet).
        rows = release_repo.list_rows_for_scene(db, provider, stash_id)
        grabbed = rows[0].get("grabbed_title") if rows else None
        # Names the download (2026-09-26: a second wrong download read
        # exactly like the first, so it looked like the old message).
        what = f"The downloaded release {grabbed!r}" if grabbed else "The downloaded file"
        detail = (f"{what} matches none of this scene's fingerprints on "
                  f"{_provider_label(provider)} -- a different video was downloaded")
        if found_title:
            detail += f" (Stash identifies it as {found_title!r})"
        release_repo.set_import_error_for_scene(db, provider, stash_id, "fingerprint_mismatch", detail)
        history.scene("error", "import", f"Import failed: {detail}", provider, stash_id, details={"path": path})
        logger.warning("scene %s at %s doesn't match %s's own fingerprints (Stash has it as %r)",
                       stash_id, path, _provider_label(provider), found_title)
        return {"stash_id": stash_id, "status": "import_failed", "path": path, "found_as": found_title}
    return None


def confirm_mismatched_file(db, stash_client, stashdb_client, provider, stash_id, get_provider_client=None, submit=False,
                            upload_enabled=False):
    """"File is correct" on an Import-failed row (v0.106.6): the verification said the file matches none of the provider's
    fingerprints (import_error_code 'fingerprint_mismatch'), the user looked at it and says it IS the scene (a re-cut, another intro
    length, a version nobody registered). Nothing is deleted or moved: the scene Stash made from the file gets the release's metadata
    exactly like a verified import, the release counts as imported and is marked 'user_confirmed' (badge; never re-checked --
    recheck_unverified_imports() only looks at 'unverified').
    `submit` (v0.106.7): afterwards submit the file's fingerprints to StashDB (public, StashDB releases only, and only when the
    setting fingerprint_upload_enabled is on -- `upload_enabled`; refused up front otherwise, nothing is done). A failing submission
    does not undo the confirmation: the result carries "submit_error". Returns {"ok", "submitted"} or {"error"}."""
    if submit and not upload_enabled:
        return {"error": "fingerprint upload is switched off (Settings > Schedule & Import)"}
    if submit and provider != "stashdb":
        return {"error": "fingerprints can only be submitted to StashDB (this release comes from another provider)"}
    with _finalize_lock:
        row = next((r for r in release_repo.list_rows_for_scene(db, provider, stash_id)
                    if r.get("import_error_code") == "fingerprint_mismatch" and r.get("pending_import_path")), None)
        if row is None:
            return {"error": "no failed import with a recorded file for this release"}
        if row.get("upgrade_of"):
            return {"error": "this is an upgrade download -- it is handled in Upgrade wanted / Activity > Needs review"}
        path = row["pending_import_path"]
        scene = stash_client.find_scene_by_path(path)
        if scene is None:
            return {"error": "Stash has no scene for this file (any more)"}
        provider_client = _provider_client(provider, stashdb_client, get_provider_client)
        endpoint = _provider_endpoint(provider, stash_client, provider_client)
        try:
            library_check.apply_fix(stash_client, scene["id"], stash_id, create_missing_performers=True,
                                    endpoint=endpoint, keep_other_endpoints=True)
        except Exception as e:
            logger.exception("confirm_mismatched_file: tagging scene %s failed", scene["id"])
            return {"error": f"couldn't apply the metadata to the scene: {type(e).__name__}: {e}"}
        _trigger_generate(stash_client, scene["id"])
        _after_import(scene["id"], path)
        release_repo.set_pending_import_path_for_scene(db, provider, stash_id, None)
        release_repo.set_status_for_scene(db, provider, stash_id, "imported")
        release_repo.set_import_error_for_scene(
            db, provider, stash_id, "user_confirmed",
            f"Confirmed by you although the file matches none of {_provider_label(provider)}'s fingerprints")
        history.scene("warning", "import", "File confirmed by the user although its fingerprints don't match -- imported",
                      provider, stash_id, details={"path": path, "scene": scene["id"]})
        result = {"ok": True, "scene_id": scene["id"], "submitted": False}
        if submit:
            try:
                result["submitted"] = stash_client.submit_fingerprints(scene["id"])
                history.scene("info", "import", "Fingerprints of the confirmed file submitted to StashDB", provider, stash_id,
                              details={"scene": scene["id"]})
            except Exception as e:
                logger.exception("confirm_mismatched_file: fingerprint submission for scene %s failed", scene["id"])
                result["submit_error"] = f"{type(e).__name__}: {e}"
        return result


def recheck_failed_import(db, stash_client, stashdb_client, provider, stash_id, get_provider_client=None):
    """"Re-check fingerprints" on an Import-failed row (v0.235.0, 'fingerprint_mismatch'): asks the provider again for the scene's fingerprints
    -- somebody may have submitted ones since the download was judged -- and compares them with the file's. An exact match imports the file
    like a verified one (the normal verification path, incl. tagging). Anything else changes nothing and says what was found: how many
    fingerprints the provider has now and how close the nearest perceptual hash is (0 = identical; up to PHASH_TOLERANCE bits is how another
    encode of the same video looks -- reported as a hint only, the import needs an exact match), so the user can decide on "File is correct" with facts.
    Returns {"result": "verified" | "still_mismatch" | "no_fingerprints" | "unknown", "message", ...} or {"error"}."""
    with _finalize_lock:
        row = next((r for r in release_repo.list_rows_for_scene(db, provider, stash_id)
                    if r.get("import_error_code") == "fingerprint_mismatch" and r.get("pending_import_path")), None)
        if row is None:
            return {"error": "no failed import with a recorded file for this release"}
        if row.get("upgrade_of"):
            return {"error": "this is an upgrade download -- it is handled in Upgrade wanted / Activity > Needs review"}
        path = row["pending_import_path"]
        client = _provider_client(provider, stashdb_client, get_provider_client)
        verified, found_title = verify_downloaded_file(stash_client, client, stash_id, path)
        if verified is True:
            result = _finalize_verification(db, stash_client, client, provider, stash_id, path)
            history.scene("info", "import", "Verified on a re-check (fingerprints are on the provider now)", provider, stash_id,
                          details={"path": path})
            return {"result": "verified", "message": "The file matches the provider's fingerprints now -- imported.",
                    "status": (result or {}).get("status")}
        remote = {(fp["algorithm"].lower(), fp["hash"]) for fp in (client.get_scene_fingerprints(stash_id) or []) if fp.get("hash")}
        scene = stash_client.find_scene_by_path(path)
        matching = next((f for f in (scene or {}).get("files", []) if f["path"] == path), None)
        local = {(fp["type"].lower(), fp["value"]) for fp in (matching or {}).get("fingerprints", []) if fp.get("value")}
        bits = phash_distance(local, remote) if local and remote else 99
        bits = None if bits == 99 else bits
        info = {"remote_fingerprints": len(remote), "closest_phash_bits": bits}
        if verified is False:
            near = ""
            likely = bits is not None and bits <= PHASH_TOLERANCE
            if bits is not None:
                near = f"; the closest perceptual hash is {bits} bits away (0 = identical)"
            if likely:
                near += (f". {bits} of 64 bits is very likely the same video in another encode, but only an exact match imports it "
                         "automatically -- if you have looked at the file and it is the scene, use \"File is correct\"")
            return {"result": "still_mismatch", **info, "likely_same_video": likely,
                    "message": f"Still no exact match: {_provider_label(provider)} has {len(remote)} fingerprint(s) for this scene{near}."}
        if not remote:
            return {"result": "no_fingerprints", **info,
                    "message": f"{_provider_label(provider)} still has no fingerprints for this scene."}
        return {"result": "unknown", **info, "message": "Not decidable yet (Stash has not finished fingerprinting the file, or the lookup failed)."}


def verify_created_scene(db, stash_client, stashdb_client, scene_id, get_provider_client=None):
    """Stash Scene.Create.Post hook target (2026-09-25): if the newly
    created scene is one of this pipeline's own pending imports,
    verify + tag it right now instead of waiting for the next import
    run. Cheap no-op for the normal case -- the hook fires for EVERY
    scene Stash creates (library scans included): with nothing pending
    it returns before touching Stash at all, otherwise it costs one
    small scene lookup. A still-unknown answer (e.g. phash not computed
    yet) just stays pending for retry_pending_verifications()."""
    pending = release_repo.list_pending_sidecar_verifications(db)
    if not pending:
        return {"matched": False}
    paths = set(stash_client.scene_file_paths(scene_id))
    release = next((r for r in pending if r["pending_import_path"] in paths), None)
    if release is None:
        return {"matched": False}
    stash_id = release["scene_stashdb_id"]
    with _finalize_lock:
        still_pending = any(r["scene_stashdb_id"] == stash_id
                            for r in release_repo.list_pending_sidecar_verifications(db))
        if not still_pending:
            return {"matched": True, "result": "already_done"}
        provider = release["provider"]
        result = _finalize_verification(db, stash_client,
                                        _provider_client(provider, stashdb_client, get_provider_client),
                                        provider, stash_id, release["pending_import_path"])
    logger.info("scene-created hook: scene %s (%s) -> %s", scene_id, stash_id,
                result["status"] if result else "not verifiable yet, left pending")
    return {"matched": True, "result": result["status"] if result else "pending"}


def _tag_imported_scene(stash_client, stash_id, remote_target_path, endpoint=None):
    """Applies full StashDB metadata (title/date/details/studio/
    performers/stash_ids) to a freshly-imported scene -- added
    2026-09-19 after a real gap found live: this pipeline only ever
    scanned a new file into Stash and fingerprint-verified it
    (verify_downloaded_file() above), it never tagged it, so every
    scene this sidecar has imported since Milestone 2 (2026-09-17,
    when file handling moved off Whisparr) landed in Stash completely
    blank -- no title, no studio, no performers -- and effectively
    invisible browsing normally (Whisparr's own import used to leave
    scenes properly tagged; nothing replaced that step once this
    project took over). Reuses library_check.apply_fix()'s exact
    scrape-by-id + conflict-check + retag sequence -- the same "Fix
    mismatch" maintenance tool, just applied to a brand new scene
    instead of a wrongly-tagged existing one, with
    `create_missing_performers=True` (2026-09-19, per the user's own
    request after noticing a performer StashDB knows about but Stash
    didn't yet got silently dropped instead of created -- see
    stash_client.py's create_performer_from_scraped()/retag_scene()
    docstrings; opted in ONLY here, not for the Library Check
    maintenance tool's own default use of apply_fix(), which can touch
    many scenes at once and keeps its existing conservative behavior).
    Best-effort: a tagging failure here doesn't fail the import itself
    (the file is already correctly placed and fingerprint-verified
    either way) -- a still-untagged scene can be caught and retried
    later by a Library Check maintenance pass. Returns the local scene
    id on success (so the caller can also trigger `_trigger_generate()`
    without a second find_scene_by_path() lookup), or None if the
    scene couldn't be found/tagged."""
    scene = stash_client.find_scene_by_path(remote_target_path)
    if scene is None:
        logger.warning("_tag_imported_scene: scene not found at %s right after a successful scan+verify, "
                        "skipping tagging", remote_target_path)
        return None
    try:
        library_check.apply_fix(stash_client, scene["id"], stash_id, create_missing_performers=True,
                                endpoint=endpoint, keep_other_endpoints=True)
        return scene["id"]
    except Exception:
        logger.exception("_tag_imported_scene: failed to tag newly-imported scene %s (stashdb %s)",
                          scene["id"], stash_id)
        return scene["id"]


def _trigger_generate(stash_client, scene_id):
    """Triggers Stash's own Generate task (covers/sprites/previews) for
    a freshly-imported scene -- added 2026-09-19 per the user's own
    request after noticing a newly-imported scene had no thumbnail/
    preview even once correctly scanned and tagged (scan_paths()'s own
    `scanGeneratePhashes` only covers the fingerprint verification
    needs, not the actual UI media). Fire-and-forget (see
    stash_client.py's generate_metadata_for_scenes() docstring for why
    this deliberately does NOT wait_for_job() on it) -- best-effort,
    same reasoning as _tag_imported_scene() above: a failure here
    doesn't fail the import, the file is already correctly placed and
    verified either way."""
    try:
        stash_client.generate_metadata_for_scenes([scene_id])
    except Exception:
        logger.exception("_trigger_generate: failed to queue Generate for scene %s", scene_id)


def run_file_import(db, whisparr_client, stash_client, stashdb_client, source,
                     source_remote_root, source_local_mount, whisparr_root_folder_path,
                     local_media_mount, scene_format, scene_folder_format, use_hardlinks, recycle_bin_path,
                     set_permissions, chmod_folder, chown_group, get_provider_client=None):
    """Runs `source.list_completed_downloads()` and imports each one.
    Returns {"checked", "imported": [...], "import_failed": [...], "errors": [...]}.

    `source_remote_root`/`source_local_mount` are the pair matching
    whichever `source` was passed in (see download_sources.py and
    main.py's run_file_import() wrapper, which picks the right pair for
    the active `file_import_source` setting)."""
    items = source.list_completed_downloads()
    imported, failed, errors = [], [], []

    for item in items:
        try:
            result = import_one(db, stash_client, stashdb_client, item, source_remote_root,
                                 source_local_mount, whisparr_root_folder_path, local_media_mount,
                                 scene_format, scene_folder_format, use_hardlinks, recycle_bin_path,
                                 set_permissions, chmod_folder, chown_group, get_provider_client)
        except Exception as e:
            logger.exception("run_file_import: unexpected failure importing scene %s", item.get("stash_id"))
            errors.append({"stash_id": item.get("stash_id"), "error": str(e)})
            continue

        if result["status"] == "imported":
            imported.append(result)
            # SabnzbdSource items never have a Whisparr queue entry to
            # clear (queue_id is always None there, see
            # download_sources.py) -- only WhisparrQueueSource items do.
            if item.get("queue_id"):
                whisparr_client.clear_queue_item(item["queue_id"])
        elif result["status"] in ("failed", "import_failed"):
            failed.append(result)
        # "moved_unverified": left in Whisparr's queue, retried next run

    if imported:
        logger.info("file import: %d download(s) imported: %s", len(imported), imported)
    if failed:
        logger.warning("file import: %d download(s) failed: %s", len(failed), failed)

    retry_result = retry_pending_verifications(db, stash_client, stashdb_client, get_provider_client)
    recheck_unverified_imports(db, stash_client, stashdb_client, get_provider_client)
    imported.extend(retry_result["imported"])
    failed.extend(retry_result["import_failed"])

    return {"checked": len(items) + retry_result["checked"], "imported": imported,
            "import_failed": failed, "errors": errors}


def retry_pending_verifications(db, stash_client, stashdb_client, get_provider_client=None):
    """Retries verification for every 'sent_to_whisparr' release whose
    file this pipeline already moved into place (import_one() recorded
    `pending_import_path` right after the move, before verification)
    but couldn't yet confirm -- the `moved_unverified` outcome above,
    e.g. Stash's own phash generation lagging behind its scan job.

    Added 2026-09-19 (v0.41.12) as a purely ADDITIVE companion to
    import_check.py's check_import_status() -- that check is the
    original Milestone 1 path, reading Whisparr's own `movieFileId`,
    and stays completely untouched here: it's correct and still needed
    for anyone running this project with Whisparr's own native
    Completed Download Handling back ON (`file_import_source =
    "whisparr_built_in"`), where Whisparr does its own import and this
    sidecar's pipeline never runs at all, so `pending_import_path` is
    never set in that mode either -- this function has nothing to do
    for that setup and stays a no-op. Under the `sidecar_sabnzbd`
    default, though, Whisparr's CDH is off and never learns about a
    file this sidecar moved itself, so `movieFileId` never gets set --
    found live 2026-09-19: a release stuck at 'sent_to_whisparr'
    forever despite its file already sitting correctly in the library,
    with nothing able to notice. This is the missing other half,
    scoped to exactly the releases import_one() itself flagged as
    still-pending.

    Returns {"checked", "imported": [...], "import_failed": [...]},
    same shape run_file_import() merges its own result with."""
    imported, failed = [], []
    pending = release_repo.list_pending_sidecar_verifications(db)
    for release in pending:
        stash_id = release["scene_stashdb_id"]
        path = release["pending_import_path"]
        try:
            with _finalize_lock:
                # Re-checked under the lock: the scene-created hook may
                # have finished this one while this loop was running.
                if not any(r["scene_stashdb_id"] == stash_id
                           for r in release_repo.list_pending_sidecar_verifications(db)):
                    continue
                provider = release["provider"]
                result = _finalize_verification(db, stash_client,
                                                _provider_client(provider, stashdb_client, get_provider_client),
                                                provider, stash_id, path)
        except Exception:
            logger.exception("retry_pending_verifications: verification failed for scene %s", stash_id)
            continue
        if result is None:
            continue  # still not ready -- pending_import_path stays set, retried again next run
        (imported if result["status"] == "imported" else failed).append(result)

    return {"checked": len(pending), "imported": imported, "import_failed": failed}


# Last re-check per scene id (in-memory, resets on restart -- then one
# extra check, harmless). Keeps the provider API calls for unverified
# imports to one per scene per _UNVERIFIED_RECHECK_SECONDS, although
# recheck_unverified_imports() is called on every import run.
_unverified_last_check = {}
_UNVERIFIED_RECHECK_SECONDS = 6 * 3600


def recheck_unverified_imports(db, stash_client, stashdb_client, get_provider_client=None):
    """Re-verifies imports that went in unverified (provider had no
    fingerprints yet, 2026-09-25). Match -> the marker is cleared;
    mismatch -> 'import_failed' + fingerprint_mismatch, so the Activity
    page offers "Fix wrong download"; still nothing on the provider ->
    stays unverified, checked again in 6h. Best-effort per scene."""
    now = time.time()
    for release in release_repo.list_unverified_imports(db):
        stash_id, provider, path = release["scene_stashdb_id"], release["provider"], release["pending_import_path"]
        if now - _unverified_last_check.get(stash_id, 0) < _UNVERIFIED_RECHECK_SECONDS:
            continue
        _unverified_last_check[stash_id] = now
        try:
            client = _provider_client(provider, stashdb_client, get_provider_client)
            verified, found_title = verify_downloaded_file(stash_client, client, stash_id, path)
        except Exception:
            logger.exception("recheck_unverified_imports: check failed for %s scene %s", provider, stash_id)
            continue
        if verified is True:
            release_repo.set_pending_import_path_for_scene(db, provider, stash_id, None)
            release_repo.set_import_error_for_scene(db, provider, stash_id, None, None)
            logger.info("recheck_unverified_imports: %s scene %s now verified", provider, stash_id)
            history.scene("info", "import", "Verified later (fingerprints appeared upstream)", provider, stash_id)
        elif verified is False:
            release_repo.set_status_for_scene(db, provider, stash_id, "import_failed")
            release_repo.set_import_error_for_scene(db, provider, stash_id, "fingerprint_mismatch",
                                                      f"Stash has it as {found_title!r}")
            logger.warning("recheck_unverified_imports: %s scene %s turned out NOT to match (Stash has it as %r)",
                           provider, stash_id, found_title)
            history.scene("error", "import", "Imported file turned out NOT to match the scene's fingerprints",
                          provider, stash_id)


def blocklist_download(db, release):
    """Puts the release this sidecar downloaded for `release`'s scene on
    that scene's own blocklist (`review_rejected_titles`, checked by every
    later search), looked up via its recorded download job (SABnzbd's
    history `name` is the original release title). Returns the title, or
    None when it couldn't be determined. Split out of fix_wrong_download()
    2026-09-26 -- the VR check reuses it for a 2D download of a VR scene."""
    provider, scene_stashdb_id = release["provider"], release["scene_stashdb_id"]
    title = None
    if release.get("download_client_id") and release.get("download_job_id"):
        try:
            client_row = download_client_repo.get_one(db, release["download_client_id"])
            if client_row is not None and client_row["type"] == "sabnzbd":
                client = SabnzbdClient(client_row["url"], client_row["api_key"])
                job = client.find_history_by_nzo_id(release["download_job_id"])
                if job is not None:
                    title = job.get("name")
            elif client_row is not None and client_row["type"] == "qbittorrent":
                title = release.get("grabbed_title")  # the torrent's own name can differ from the release title the indexer showed
        except Exception:
            logger.exception("blocklist_download: couldn't look up the download job for scene %s", scene_stashdb_id)
    if not title:  # the entry may be gone from the client's history (removed after the import): our own records still know the name
        title = release.get("grabbed_title") or download_history_cleanup.ledger_title_for_job(db, release.get("download_job_id"))
    if title:
        release_repo.add_review_rejected_title(db, provider, scene_stashdb_id, title)
        logger.info("blocklisted %r for scene %s (this sidecar's own list)", title, scene_stashdb_id)
        history.scene("warning", "blocklist", f"Blocklisted {title}", provider, scene_stashdb_id)
    else:
        logger.warning("couldn't determine the downloaded release's own title for scene %s -- this sidecar's "
                       "own blocklist won't exclude it", scene_stashdb_id)
    return title


def fix_wrong_download(db, stash_client, whisparr_client, provider, scene_stashdb_id, entity_type,
                        entity_stashdb_id, whisparr_root_folder_path, local_media_mount, recycle_bin_path,
                        file_path=None):
    """"Fix wrong download" retry action for a 'fingerprint_mismatch'
    'import_failed' release (2026-09-23) -- see project_roadmap_todo.md
    memory's "Import-failed reason + conditional Retry" entry for the
    full design discussion. Distinct from handoff.fix_mismatched_download()
    (built for a DIFFERENT scenario -- Whisparr's own `hasFile: true`
    pointing at wrong content): this is for a release THIS sidecar's own
    pipeline imported and then found doesn't match StashDB's own
    fingerprints (see verify_downloaded_file(), import_one()'s
    `verified is False` branch above).

    1. Blocklist the exact wrong release so it doesn't get grabbed
       again. ALWAYS added to this sidecar's own
       `release.review_rejected_titles` (release_repo.
       add_review_rejected_title() -- the same list _select_best() now
       also checks, see indexer_search.py's own 2026-09-23 fix) --
       looked up via the release's recorded `download_client_id`/
       `download_job_id` (SabnzbdClient.find_history_by_nzo_id()'s own
       `name` field is the original release title). Best-effort ALSO
       pushed to Whisparr's OWN blocklist
       (whisparr_client.find_grabbed_history_event()+
       mark_history_failed(), same pattern handoff.
       fix_mismatched_download() already uses) -- only actually does
       anything if Whisparr itself made the original grab
       (search_sidecar_import/full_whisparr presets); silently no-ops
       for a sidecar-initiated grab (bookkeeping_only, via Whisparr's
       /release OR Prowlarr), since Whisparr has no history record of a
       grab it never initiated.
    2. Removes the wrong file SAFELY, not a hard delete: deletes just
       the Stash scene record (`stash_client.destroy_scene(delete_file=
       False)`), then moves the actual file into the recycle bin via
       the same `_recycle_existing_target()` mechanism already used for
       an overwrite -- reversible, matching why the recycle bin exists
       at all (user's own explicit choice over a hard delete).
    3. Puts the release back on the wishlist ('sent_to_whisparr' since
       2026-09-26, was 'backlog' -- which is never searched automatically;
       clears
       download_client_id/download_job_id/import_error_code/
       import_error_detail/pending_import_path) so the next search
       cycle retries fresh, now excluding the just-blocklisted title.

    Best-effort throughout except for the release lookup itself --
    a missing download-job/Stash-scene/Whisparr-history piece just
    means that one part of the cleanup has nothing to do, not a reason
    to fail the whole retry. Returns a small summary dict."""
    release = release_repo.get_release(db, provider, scene_stashdb_id, entity_type, entity_stashdb_id)
    if release is None:
        raise ValueError(f"No release found for scene {scene_stashdb_id} / {entity_type} {entity_stashdb_id}")

    blocklisted_title = blocklist_download(db, release)

    try:
        movie = whisparr_client.find_by_stash_id(scene_stashdb_id)
        if movie is not None:
            history_id = whisparr_client.find_grabbed_history_event(movie["id"])
            if history_id is not None:
                whisparr_client.mark_history_failed(history_id)
                logger.info("fix_wrong_download: blocklisted grab %s on Whisparr too, movie %s",
                            history_id, movie["id"])
    except Exception:
        logger.exception("fix_wrong_download: best-effort Whisparr blocklist failed for scene %s, continuing",
                          scene_stashdb_id)

    # `file_path` (2026-09-26, VR check): the flagged file's own Stash path
    # -- a verified import no longer has pending_import_path set.
    path = file_path or release["pending_import_path"]
    if path:
        try:
            scene = stash_client.find_scene_by_path(path)
            if scene is not None:
                stash_client.destroy_scene(scene["id"], delete_file=False, delete_generated=True)
                logger.info("fix_wrong_download: deleted Stash scene %s (kept the file for the recycle bin)",
                            scene["id"])
            local_path = _to_local_path(path, whisparr_root_folder_path, local_media_mount)
            _recycle_existing_target(local_path, local_media_mount, recycle_bin_path)
        except Exception:
            logger.exception("fix_wrong_download: couldn't clean up the wrong file at %s for scene %s",
                              path, scene_stashdb_id)

    # Back on the WISHLIST (2026-09-26, was 'backlog'): the button promises
    # "let this sidecar search again", but nothing ever searches the
    # backlog automatically -- 'sent_to_whisparr' without a download job
    # is exactly what the periodic indexer search picks up (the
    # blocklisted title stays excluded).
    release_repo.set_status_for_scene(db, provider, scene_stashdb_id, "sent_to_whisparr")
    release_repo.set_download_job_for_scene(db, provider, scene_stashdb_id, None, None)
    release_repo.set_import_error_for_scene(db, provider, scene_stashdb_id, None, None)
    release_repo.set_pending_import_path_for_scene(db, provider, scene_stashdb_id, None)

    history.scene("info", "wishlist", "Fix wrong download: wrong file to the recycle bin, back on the wishlist",
                  provider, scene_stashdb_id)
    return {"blocklisted_title": blocklisted_title, "status": "sent_to_whisparr"}
