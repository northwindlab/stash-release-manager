"""Film wishlist, step 4 (v0.110.0): imports a completed film download and builds its Group.

1. A 'grabbed' film whose SABnzbd job is Completed: the video file(s) are moved (hardlink, else copy -- like scenes) into
   <library>/Movies/<Title> (<Year>)/ and Stash is told to scan that folder. The new paths are recorded on the film
   (pending_import_path, a JSON list) -- the scene(s) do not exist yet.
2. Once Stash has created the scene(s) for ALL files (next run; a finished scan kicks a run): the summed length is compared with the film's
   length (+-15 %), then the Group is created from the ThePornDB movie (or the film's existing empty Stash group, or just the title) and
   the scenes are put in it in file-name order. The film is 'imported'. A length that doesn't fit leaves it 'import_failed' with
   the files in place -- "Group anyway" (force) builds the Group regardless.
A SABnzbd job that failed puts the film back on the wishlist. Nothing is deleted except what a normal import also cleans up
(the download folder, an identical target file goes to the recycle bin)."""
import json
import logging
import os
import re
import shutil
import subprocess

import download_client_repo
import film_names
import download_jobs
import file_importer
import film_repo
import film_whisparr
import history
import quality_detect
from sabnzbd_client import SabnzbdClient

logger = logging.getLogger(__name__)

LENGTH_TOLERANCE = 0.15
_MIN_PART_RATIO = 0.10  # a video smaller than 10 % of the biggest one is a sample/trailer, not a part


def _safe(name):
    t = re.sub(r'[\\/:*?"<>|]', " ", name or "")
    return re.sub(r"\s+", " ", t).strip().rstrip(".")


def folder_name(film):
    return film_names.with_year(_safe(film["title"]), film.get("year"))


unreadable = file_importer.unreadable  # moved to file_importer in v0.165.1 (scene import shares it)


def _to_local(remote_path, cfg):
    root = cfg["stash_root"].rstrip("/")
    return cfg["local_media_mount"].rstrip("/") + remote_path[len(root):] if remote_path.startswith(root) else remote_path


def _video_files(local_path):
    if os.path.isfile(local_path):
        return [local_path]
    found = []
    for root, _dirs, files in os.walk(local_path):
        for name in files:
            if os.path.splitext(name)[1].lower() in file_importer.VIDEO_EXTENSIONS:
                full = os.path.join(root, name)
                found.append((os.path.getsize(full), full))
    if not found:
        return []
    biggest = max(size for size, _ in found)
    return sorted(full for size, full in found if size >= biggest * _MIN_PART_RATIO)


def _to_remote(local_path, cfg):
    mount = cfg["local_media_mount"].rstrip("/")
    return cfg["stash_root"].rstrip("/") + local_path[len(mount):]


def import_downloaded(db, stash_client, film, job, cfg):
    """Step 1 -- moves the finished download into the library and starts the scan."""
    keep_source = bool(job.get("keep_source"))  # torrent: the files keep seeding (hard-linked / copied, never moved)
    out = file_importer._to_local_path(job["storage"], job.get("remote_root") or cfg["sab_remote_root"],
                                       job.get("local_mount") or cfg["sab_local_mount"])
    videos = _video_files(out)
    if not videos:
        film_repo.update(db, film["id"], import_error=f"Downloaded file not found under {out} -- retrying automatically")
        history.event("warning", "movies", f"{film['title']}: downloaded file not found -- retrying", dedupe_minutes=24 * 60)
        return {"id": film["id"], "status": "waiting_for_file"}
    for video in videos:  # a download that is not a readable video never goes into the library: back on the wishlist, the release is rejected
        reason = unreadable(video)
        if reason:
            msg = (f"The downloaded file \"{os.path.basename(video)}\" is not a readable video ({reason}) -- probably a failed unpack. "
                   "Nothing was moved into the library; the release is rejected and the movie is searched again")
            rejected = [*film.get("rejected_titles", []), *([film["grabbed_title"]] if film.get("grabbed_title") else [])]
            film_repo.update(db, film["id"], **_fail_back(film), download_client_id=None, download_job_id=None, grabbed_title=None, grabbed_at=None, review_candidate=None, as_scene=0,
                             import_error=msg, last_search_result="unreadable", rejected_titles=rejected)
            history.event("warning", "movies", f"{film['title']}: {msg}")
            return {"id": film["id"], "status": "unreadable"}
    base = folder_name(film)  # (for the History line; the paths come from the movie templates)
    targets, folders = [], []
    for index, source in enumerate(videos, 1):
        quality = quality_detect.detect_quality(source, film.get("grabbed_title") or os.path.basename(source))
        tokens = film_names.movie_tokens(_safe(film["title"]), film.get("year"), index if len(videos) > 1 else None, quality,
                                         film.get("studio_name") or "", cfg.get("naming_options"))
        rel = film_names.render_movie_path(cfg.get("movie_folder_format"), cfg.get("movie_naming_format"), tokens, os.path.splitext(source)[1])
        target = os.path.join(cfg["local_media_mount"].rstrip("/"), rel.lstrip("/"))
        if os.path.dirname(target) not in folders:
            folders.append(os.path.dirname(target))
        file_importer._recycle_existing_target(target, cfg["local_media_mount"], cfg["recycle_bin_path"])
        file_importer._move_file(source, target, cfg["use_hardlinks"], keep_source=keep_source)
        file_importer._apply_permissions(target, cfg["set_permissions"], cfg["chmod_folder"], cfg["chown_group"])
        targets.append(_to_remote(target, cfg))
    if not keep_source:
        file_importer._delete_extra_files(out)
        file_importer._cleanup_source_dir(out)
    film_repo.update(db, film["id"], pending_import_path=json.dumps(targets), import_error=None)
    stash_client.scan_paths([_to_remote(f, cfg) for f in folders])
    history.event("info", "movies", f"{film['title']}: {len(targets)} file(s) moved into the library, Stash scan started",
                  details={"folder": base})
    return {"id": film["id"], "status": "moved", "files": len(targets)}


def _scene_for_path(stash_client, path):
    sc = stash_client.find_scene_by_path(path)
    if sc is None:
        return None
    full = stash_client._graphql("query($id: ID!) { findScene(id: $id) { id files { path duration } } }", {"id": str(sc["id"])})["findScene"]
    duration = sum((f.get("duration") or 0) for f in full["files"] if f["path"] == path) or sum((f.get("duration") or 0) for f in full["files"])
    return {"id": str(sc["id"]), "duration": duration}


def _tpdb_record(film, get_provider_client):
    """The full ThePornDB movie record for the group data (the search result may be thin); one request, falls back to the stored one."""
    stored = film.get("tpdb_json")
    if not film.get("tpdb_id"):
        return None
    client = get_provider_client("theporndb") if get_provider_client else None
    if client is not None:
        try:
            resp = client._get(f"/movies/{film['tpdb_id']}")
            resp.raise_for_status()
            data = resp.json()
            return data.get("data", data)
        except Exception:  # noqa: BLE001
            logger.exception("movie import: ThePornDB movie detail failed for %s, using the stored record", film["tpdb_id"])
    return stored


def finalize(db, stash_client, film, movies, get_provider_client, force=False, cfg=None, file_tools=None):
    """Step 2 -- scenes -> length check -> Group. Returns {"status": waiting | imported | import_failed, ...}. With `cfg`, a file that Stash has not turned
    into a scene is checked for being a readable video at all: if not, the movie goes to import_failed (use "Wrong movie") instead of waiting forever."""
    paths = json.loads(film["pending_import_path"])
    scenes = []
    for path in paths:
        sc = _scene_for_path(stash_client, path)
        if sc is None:
            reason = unreadable(_to_local(path, cfg)) if cfg else None
            if reason:
                msg = (f"The file \"{os.path.basename(path)}\" is not a readable video ({reason}), so Stash cannot make a scene of it -- probably a failed unpack. "
                       "Use 'Wrong movie' to remove it and search again")
                film_repo.update(db, film["id"], status="import_failed", import_error=msg)
                history.event("warning", "movies", f"{film['title']}: {msg}")
                return {"id": film["id"], "status": "import_failed", "reason": "unreadable"}
            return {"id": film["id"], "status": "waiting"}  # Stash has not created every scene yet
        scenes.append(sc)
    ids = [s["id"] for s in scenes]
    actual = sum(s["duration"] for s in scenes) / 60
    expected = film.get("duration_min")
    if film.get("as_scene"):
        return _add_part(db, stash_client, film, ids, actual, movies, get_provider_client)
    if expected and actual and not force and abs(actual / expected - 1) > LENGTH_TOLERANCE:
        msg = (f"The file(s) are {actual:.0f} min, ThePornDB says {expected:.0f} min -- a different cut or the wrong movie. "
               "Nothing was grouped; look at the files (the Scenes button lists them), then 'Group anyway' or fix it by hand")
        film_repo.update(db, film["id"], status="import_failed", import_error=msg)
        history.event("warning", "movies", f"{film['title']}: {msg}")
        return {"id": film["id"], "status": "import_failed", "actual": round(actual, 1), "expected": expected}
    if film.get("upgrading"):  # the better version of a file we own: replaces it, no new Group
        if len(ids) != 1:
            msg = f"The upgrade download has {len(ids)} files -- it replaces one file only. Look at it, then 'Wrong movie' or fix it by hand"
            film_repo.update(db, film["id"], status="import_failed", import_error=msg)
            history.event("warning", "movies", f"{film['title']}: {msg}")
            return {"id": film["id"], "status": "import_failed", "reason": "upgrade_files"}
        return _finish_upgrade(db, stash_client, film, scenes[0], json.loads(film["pending_import_path"])[0], file_tools, force=force)
    group_id = _make_group(db, film, ids, movies, get_provider_client)
    for sid in ids:
        file_importer._trigger_generate(stash_client, sid)
    film_repo.update(db, film["id"], status="imported", imported_group_id=group_id, pending_import_path=None, import_error=None)
    film_whisparr.queue_file(db, film["id"])
    history.event("info", "movies", f"{film['title']} imported: group {group_id} with {len(ids)} scene(s)",
                  details={"scenes": ids, "minutes": round(actual, 1)})
    return {"id": film["id"], "status": "imported", "group_id": group_id, "scenes": len(ids)}


_GROUP_FILES_QUERY = "query($id: ID!) { findGroup(id: $id) { id scenes { id files { id path height width duration } } } }"


def upgrade_target(stash_client, group_id):
    """The owned file an upgrade replaces (v0.222.0): {"scene_id", "file_id", "path", "height", "width"}; None when the Group is gone; a text (why
    not) when the Group holds more than one scene or file -- which file would be replaced is then the user's call, so it is not searched."""
    group = stash_client._graphql(_GROUP_FILES_QUERY, {"id": str(group_id)}).get("findGroup")
    if not group:
        return None
    scenes = group.get("scenes") or []
    if len(scenes) != 1:
        return f"the Group holds {len(scenes)} scenes -- replace the files by hand"
    files = scenes[0].get("files") or []
    if len(files) != 1:
        return f"the scene holds {len(files)} files -- replace the file by hand"
    f = files[0]
    return {"scene_id": str(scenes[0]["id"]), "file_id": str(f["id"]), "path": f["path"], "height": f.get("height") or 0, "width": f.get("width") or 0}


def _rejected_with_grabbed(film):
    """The movie's blocklist plus the release that just failed (an upgrade must not offer the same failing release again)."""
    return [*film.get("rejected_titles", []), *([film["grabbed_title"]] if film.get("grabbed_title") and film.get("upgrading") else [])]


def _fail_back(film):
    """Where a movie goes when a download or its import fails: the wishlist -- but a movie that was being UPGRADED still owns its file, so it
    goes back onto the Upgrade list (status 'imported', upgrade_wanted)."""
    if film.get("upgrading"):
        return {"status": "imported", "upgrading": 0, "upgrade_wanted": 1}
    return {"status": "wanted"}


def _finish_upgrade(db, stash_client, film, scene, new_path, file_tools, force=False):
    """The better file is in the library and its scene exists: merged into the owned scene (Stash sceneMerge keeps that scene's metadata, rating,
    play history and its place in the Group, like a scene upgrade), the new file becomes primary, the old file goes to the recycle bin. Nothing
    is touched when the owned file cannot be found any more (import_failed, both files stay)."""
    recycle, to_local = file_tools if file_tools else (None, None)
    target = upgrade_target(stash_client, film["imported_group_id"]) if film.get("imported_group_id") else None
    if not isinstance(target, dict):
        why = target or "the Stash Group does not exist any more"
        msg = f"Upgrade not applied: {why}. The new file is in the library; put it in the Group by hand"
        film_repo.update(db, film["id"], status="import_failed", import_error=msg)
        history.event("warning", "movies", f"{film['title']}: {msg}")
        return {"id": film["id"], "status": "import_failed", "reason": "upgrade_target"}
    if recycle is None:
        raise RuntimeError("no recycle tool for an upgrade")
    new_scene_id = scene["id"]
    old_scene_id, old_path = target["scene_id"], target["path"]
    if str(new_scene_id) != str(old_scene_id):
        stash_client.merge_scene_into(new_scene_id, old_scene_id)
    files = stash_client.scene_files(old_scene_id) or []
    new_file = next((x for x in files if x["path"] == new_path), None)
    if new_file:
        stash_client.set_primary_file(old_scene_id, new_file["id"])
    if old_path != new_path:
        recycle(old_path)
        try:
            stash_client.clean_paths([old_path.rsplit("/", 1)[0]])
        except Exception:  # noqa: BLE001
            logger.exception("movie upgrade: Stash clean for %s failed (the old file entry stays until the next clean)", old_path)
    file_importer._trigger_generate(stash_client, old_scene_id)
    height = next((x.get("height") for x in (stash_client._graphql("query($id: ID!) { findScene(id: $id) { files { path height } } }",
                                                                    {"id": str(old_scene_id)})["findScene"]["files"]) if x["path"] == new_path), None)
    film_repo.update(db, film["id"], status="imported", upgrading=0, upgrade_wanted=0, owned_height=height or None, pending_import_path=None,
                     import_error=None, download_client_id=None, download_job_id=None, grabbed_title=None, grabbed_at=None, review_candidate=None,
                     last_search_result="upgraded")
    history.event("info", "movies", f"{film['title']}: upgrade done -- the better file ({height or '?'}p) is in the group, the old one ({target['height']}p) is in the recycle bin",
                  details={"scene": old_scene_id, "old": old_path, "new": new_path})
    return {"id": film["id"], "status": "imported", "group_id": film.get("imported_group_id"), "scenes": 1, "upgraded": True}


def forget_dead_groups(db, stash_client, dry_run=False):
    """Wishlist movies ('suggested' / 'wanted') whose remembered Stash Group was deleted since. A movie that came from an empty group
    (source 'empty-group') existed only because of that Group, so it is dismissed (reversible: Restore); any other movie just forgets the
    dead id. ONE Stash query (all group ids); an empty answer is not believed (Stash down / wrong library) and changes nothing."""
    alive = {str(g["id"]) for g in stash_client._graphql("{ findGroups(filter: {per_page: -1}) { groups { id } } }", {})["findGroups"]["groups"]}
    found = []
    if not alive:
        return found
    for f in film_repo.list_films(db, ["suggested", "wanted"], per_page=0)["rows"]:
        gid = f.get("stash_group_id")
        if gid and str(gid) not in alive:
            found.append({"id": f["id"], "title": f["title"], "stash_group_id": gid, "previous": f["status"],
                          "action": "dismiss" if f.get("source") == "empty-group" else "forget_group"})
    if not dry_run:
        for d in found:
            if d["action"] == "dismiss":
                film_repo.update(db, d["id"], status="dismissed", review_candidate=None)
                history.event("info", "movies", f"{d['title']}: dismissed, its empty Stash Group {d['stash_group_id']} was deleted")
            else:
                film_repo.update(db, d["id"], stash_group_id=None)
                history.event("info", "movies", f"{d['title']}: forgot the deleted Stash Group {d['stash_group_id']}")
    return found


_FILLED_QUERY = "{ findGroups(filter: {per_page: -1}) { groups { id scenes { files { duration width height } } } } }"


def min_film_resolution(db):
    """The smallest resolution of the scene quality profile (Settings > Quality), None when no profile is set: a movie file below it is an upgrade."""
    from routers.settings import resolve_quality_profiles
    profile, _ = resolve_quality_profiles(db)
    resolutions = [int(r) for r in ((profile or {}).get("resolutions") or [])]
    return min(resolutions) if resolutions else None


def _meets(width, height, resolution):
    """A file counts as `resolution` p when its height reaches it or its width does (letterboxed 1920x800 is 1080p); 10 % slack."""
    return (height or 0) >= resolution * 0.9 or (width or 0) >= resolution * 16 / 9 * 0.9


def adopt_filled_groups(db, stash_client, min_resolution, dry_run=False):
    """Wishlist movies ('suggested' / 'wanted') whose remembered Stash Group has a file now that covers the movie (v0.221.12): the user or the
    library filled the Group, so nothing needs to be downloaded. The movie becomes 'imported' with that Group; when the best file is below
    `min_resolution` (the smallest resolution of the quality profile) it is flagged upgrade_wanted and shows under Upgrade, not on the wishlist.
    Length rule = the film import's (+-15 % of the ThePornDB length, scenes summed); a movie without a known length is left alone.
    ONE Stash query; an empty answer is not believed and changes nothing."""
    groups = {str(g["id"]): g for g in stash_client._graphql(_FILLED_QUERY, {})["findGroups"]["groups"]}
    found = []
    if not groups:
        return found
    for f in film_repo.list_films(db, ["suggested", "wanted"], per_page=0)["rows"]:
        gid = f.get("stash_group_id")
        g = groups.get(str(gid)) if gid else None
        if not g or not f.get("duration_min") or f.get("parts_imported"):
            continue
        scenes = [[x for x in (s.get("files") or [])] for s in g["scenes"]]
        scenes = [fl for fl in scenes if fl]
        minutes = sum(max((x.get("duration") or 0) for x in fl) for fl in scenes) / 60
        if not minutes or abs(minutes / float(f["duration_min"]) - 1) > LENGTH_TOLERANCE:
            continue
        best = max((x for fl in scenes for x in fl), key=lambda x: (x.get("height") or 0))
        low = bool(min_resolution) and not _meets(best.get("width"), best.get("height"), int(min_resolution))
        found.append({"id": f["id"], "title": f["title"], "stash_group_id": gid, "previous": f["status"], "minutes": round(minutes, 1),
                      "height": best.get("height"), "upgrade": low})
    if not dry_run:
        for d in found:
            film_repo.update(db, d["id"], status="imported", imported_group_id=d["stash_group_id"], review_candidate=None, import_error=None,
                             upgrade_wanted=1 if d["upgrade"] else 0, owned_height=d["height"], last_search_result="in_library")
            history.event("info", "movies", f"{d['title']}: the Stash Group {d['stash_group_id']} already has the movie ({d['minutes']} min, "
                          f"{d['height']}p) -- " + ("below the quality profile, listed under Upgrade" if d["upgrade"] else "taken off the wishlist"))
    return found


def _add_part(db, stash_client, film, ids, actual, movies, get_provider_client):
    """"Accept as one scene" (v0.221.0): the file(s) are only a scene of the movie, so there is no length check and the movie stays 'wanted'.
    The scenes join the movie's Group (made on the first part and remembered in stash_group_id, so the next part joins the same one); the
    release is rejected for this movie so the search proposes the other parts, not this one again."""
    group_id = _make_group(db, film, ids, movies, get_provider_client)
    for sid in ids:
        file_importer._trigger_generate(stash_client, sid)
    rejected = [*film.get("rejected_titles", []), *([film["grabbed_title"]] if film.get("grabbed_title") else [])]
    parts = (film.get("parts_imported") or 0) + len(ids)
    film_repo.update(db, film["id"], status="wanted", stash_group_id=group_id, parts_imported=parts, as_scene=0, pending_import_path=None,
                     download_client_id=None, download_job_id=None, grabbed_title=None, grabbed_at=None, review_candidate=None,
                     rejected_titles=rejected, last_search_result="part_imported", import_error=None)
    history.event("info", "movies", f"{film['title']}: {len(ids)} scene(s) added to group {group_id} as a part ({parts} so far), still wanted",
                  details={"scenes": ids, "minutes": round(actual, 1)})
    return {"id": film["id"], "status": "part_imported", "group_id": group_id, "scenes": len(ids)}


def _make_group(db, film, ids, movies, get_provider_client):
    if film.get("stash_group_id"):
        try:
            return movies.create_group(None, ids, group_id=film["stash_group_id"])["group_id"]
        except ValueError as e:  # the remembered Group was deleted in Stash meanwhile: forget it and build a new one below
            if "group not found" not in str(e):
                raise
            history.event("warning", "movies", f"{film['title']}: Group {film['stash_group_id']} no longer exists in Stash -- a new Group is made")
            film_repo.update(db, film["id"], stash_group_id=None)
    if film.get("tpdb_id"):
        record = _tpdb_record(film, get_provider_client)
        if record:
            return movies.create_from_tpdb(record, ids, movies.local_studio_index())["group_id"]
    return movies.create_group(film["title"], ids)["group_id"]


def assemble(db, stash_client, film, movies, get_provider_client):
    """Mode B (v0.111.0): the film is put together from StashDB scenes that went on the scene wishlist. Waits until ALL of them are
    imported (normal scene import, fingerprint-verified), then builds the Group with the scenes in "Scene k" order."""
    import release_repo
    ordered = sorted(film.get("scene_ids") or [], key=lambda s: s["k"])
    if not ordered:
        return {"id": film["id"], "status": "waiting"}
    for sc in ordered:
        if not any(r["status"] == "imported" for r in release_repo.list_rows_for_scene(db, "stashdb", sc["id"])):
            return {"id": film["id"], "status": "waiting"}
    local = []
    for sc in ordered:
        found = stash_client.find_scene_ids_by_stash_id(stash_client.stashdb_endpoint, sc["id"])
        if not found:
            return {"id": film["id"], "status": "waiting"}  # imported, but Stash has not got the id on the scene yet
        local.append(found[0])
    group_id = _make_group(db, film, local, movies, get_provider_client)
    film_repo.update(db, film["id"], status="imported", imported_group_id=group_id, import_error=None)
    film_whisparr.queue_file(db, film["id"])
    history.event("info", "movies", f"{film['title']} put together from {len(local)} StashDB scene(s): group {group_id}",
                  details={"scenes": local})
    return {"id": film["id"], "status": "imported", "group_id": group_id, "scenes": len(local)}


def run(db, stash_client, movies, get_provider_client, cfg, file_tools=None):
    """One pass over every 'grabbed' film -- called at the end of every file-import run (loop, SABnzbd webhook, finished scan)."""
    result = {"moved": [], "imported": [], "failed": [], "waiting": []}
    for row in film_repo.list_films(db, ["grabbed"], per_page=0)["rows"]:
        film = film_repo.get(db, row["id"], with_json=True)
        try:
            if film.get("mode") == "scenes":
                out = assemble(db, stash_client, film, movies, get_provider_client)
                (result["imported"] if out["status"] == "imported" else result["waiting"]).append(out)
                continue
            if film.get("pending_import_path"):
                out = finalize(db, stash_client, film, movies, get_provider_client, cfg=cfg, file_tools=file_tools)
                {"imported": result["imported"], "part_imported": result["imported"], "import_failed": result["failed"]}.get(out["status"], result["waiting"]).append(out)
                continue
            client_row = download_client_repo.get_one(db, film["download_client_id"]) if film.get("download_client_id") else None
            if client_row is None or client_row["type"] not in ("sabnzbd", "qbittorrent") or not film.get("download_job_id"):
                continue
            job = download_jobs.job_state(client_row, film["download_job_id"])
            if job["state"] == "failed":
                label = job.get("client") or "SABnzbd"
                msg = f"Download failed in {label}{(': ' + job['fail_message']) if job.get('fail_message') else ''} -- back on the wishlist"
                film_repo.update(db, film["id"], **_fail_back(film), download_job_id=None, download_client_id=None, import_error=msg,
                                 last_search_result="download_failed", rejected_titles=_rejected_with_grabbed(film))
                history.event("error", "movies", f"{film['title']}: {msg}")
                result["failed"].append({"id": film["id"], "status": "download_failed"})
                continue
            if download_jobs.is_lost(job, film.get("grabbed_at")):
                msg = "The download client no longer knows this download (removed from its history?) -- back on the wishlist"
                film_repo.update(db, film["id"], **_fail_back(film), download_job_id=None, download_client_id=None, import_error=msg,
                                 last_search_result="download_failed", rejected_titles=_rejected_with_grabbed(film))
                history.event("warning", "movies", f"{film['title']}: {msg}")
                result["failed"].append({"id": film["id"], "status": "download_lost"})
                continue
            if job["state"] == "post_processing":  # "Completed" comes before the output path is set (found with the first real film grab, v0.125.3)
                result["waiting"].append({"id": film["id"], "status": "post_processing"})
                continue
            if job["state"] != "completed":
                continue  # still downloading, or the client doesn't know the id (yet) / a torrent removed by hand
            out = import_downloaded(db, stash_client, film, job, cfg)
            (result["failed"] if out.get("status") == "unreadable" else result["moved"]).append(out)
        except Exception as e:  # noqa: BLE001
            logger.exception("movie import failed for movie %s", film["id"])
            film_repo.update(db, film["id"], import_error=f"{type(e).__name__}: {e}")
            result["failed"].append({"id": film["id"], "status": "error", "error": str(e)})
    return result


def discard(db, stash_client, film, recycle, to_local, dismiss=False):
    """"Wrong movie" for a movie whose import failed (status import_failed, files already in the library): removes the Stash scene of each
    downloaded file (only when the scene holds just that file), moves the file to the recycle bin (reversible for its retention time), drops
    the empty movie folder and the Stash entries, blocklists the release for this movie and sets the movie back to 'wanted' (searched again)
    or, with `dismiss`, to 'dismissed'. Only the files recorded in pending_import_path are touched."""
    paths = json.loads(film.get("pending_import_path") or "[]")
    if not paths:
        raise ValueError("no imported file is recorded for this movie")
    removed_scenes, recycled, missing = [], [], []
    for path in paths:
        sc = stash_client.find_scene_by_path(path)
        if sc is not None and len(stash_client.scene_files(str(sc["id"])) or []) <= 1:
            stash_client.destroy_scene(str(sc["id"]), delete_file=False)
            removed_scenes.append(str(sc["id"]))
        local = to_local(path)
        if os.path.exists(local):
            recycle(local)
            recycled.append(os.path.basename(local))
        else:
            missing.append(os.path.basename(local))
    folders = sorted({os.path.dirname(p) for p in paths})
    for folder in folders:
        try:
            os.rmdir(to_local(folder))  # only succeeds when empty
        except OSError:
            pass
    try:
        stash_client.clean_paths(folders)
    except Exception:  # noqa: BLE001
        logger.exception("movie discard: Stash clean for %s failed (the entries go with the next clean)", folders)
    rejected = [*film.get("rejected_titles", []), *([film["grabbed_title"]] if film.get("grabbed_title") else [])]
    back = {"status": "dismissed" if dismiss else "wanted"}
    if film.get("upgrading"):  # the owned file stays: back on the Upgrade list (or off it when the user dismisses)
        back = {"status": "imported", "upgrading": 0, "upgrade_wanted": 0 if dismiss else 1}
    film_repo.update(db, film["id"], **back, pending_import_path=None, import_error=None,
                     download_client_id=None, download_job_id=None, grabbed_title=None, grabbed_at=None,
                     review_candidate=None, last_search_at=None, last_search_result="rejected", rejected_titles=rejected)
    history.event("info", "movies", f"{film['title']}: wrong movie removed ({len(recycled)} file(s) in the recycle bin), "
                  + ("dismissed" if dismiss else "searched again"),
                  details={"scenes": removed_scenes, "files": recycled, "missing": missing, "blocklisted": film.get("grabbed_title")})
    return {"ok": True, "files": recycled, "scenes": removed_scenes, "missing": missing, "status": back["status"]}
