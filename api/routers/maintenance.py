"""Maintenance page: retroactive Library Check + bulk correction
(2026-09-16, see PROGRESS.md's "Maintenance page" section).

Backed by library_check.py's shared logic (also used standalone by
scripts/audit_library_fingerprints.py and
scripts/fix_library_mismatches.py) and library_check_repo.py's DB
persistence (library_check_run/library_check_finding, see db.py).

Deliberately does NOT use scan.py's pattern of letting FastAPI's own
threadpool block the triggering HTTP call until the whole job
finishes -- a full check took ~9 minutes against the real library
(2026-09-15/16), and the Maintenance page needs a genuinely live,
Stash-Tagger-style progress bar while it runs. `run_now()` below
explicitly spawns its own `threading.Thread` and returns immediately;
`/library-check/status` reads the run row's live progress from the DB,
pollable independently the whole time."""
import renamer_check
import logging
import threading
import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import history
import fingerprint_provider
import library_check
import library_check_repo
import quality_profile_repo
import release_repo
import library_rename
import upgrades
import vr_check
import duration_check
import group_compare
import kept_lists

router = APIRouter(prefix="/maintenance", tags=["maintenance"])

logger = logging.getLogger(__name__)
_db = None
_get_stash_client = None
_get_stashdb_client = None
_get_provider_client = None
_to_local = None
_organizer = None
_importer = None
_movies = None
_check_lock = threading.Lock()


_vr_job = None
_fix_wrong_download = None


_upgrade_search = None
_cutoff_resolution = None


def init_maintenance_router(db, get_stash_client, get_stashdb_client, vr_job=None, fix_wrong_download=None,
                            upgrade_search=None, cutoff_resolution=None, get_provider_client=None, to_local=None,
                            organizer_factory=None, importer_factory=None, movies_factory=None, duration_job=None, readd=None, library_check_job=None):
    global _db, _get_stash_client, _get_stashdb_client, _vr_job, _fix_wrong_download
    global _upgrade_search, _cutoff_resolution, _get_provider_client, _to_local, _organizer, _importer, _movies
    global _duration_job, _readd, _library_check_job
    _duration_job, _readd, _library_check_job = duration_job, readd, library_check_job
    _importer = importer_factory
    _movies = movies_factory
    _get_provider_client = get_provider_client
    _to_local = to_local
    _organizer = organizer_factory
    _db = db
    _get_stash_client = get_stash_client
    _get_stashdb_client = get_stashdb_client
    _vr_job = vr_job
    _fix_wrong_download = fix_wrong_download
    _upgrade_search = upgrade_search
    _cutoff_resolution = cutoff_resolution


# --- Upgrade search (2026-09-26, upgrades.py) ---

@router.get("/upgrades")
def upgrades_list():
    from routers.settings import get_setting_value
    candidates = upgrades.list_candidates(_db, ("wanted", "grabbed", "review", "done", "dismissed", "wishlist", "check"))
    # Studio + release date for the manual search prefill ("Studio.YY.MM.DD"), from the release rows when the scene has any.
    with _db.connect() as conn:
        info = {(r["provider"], r["scene_stashdb_id"]): (r["studio_name"], r["scene_date"]) for r in conn.execute(
            "SELECT provider, scene_stashdb_id, studio_name, scene_date FROM release WHERE studio_name != ''")}
    for c in candidates:
        c["studio_name"], c["scene_date"] = info.get((c.get("provider"), c.get("provider_scene_id")), (None, None))
    return {
        "candidates": candidates,
        "enabled": get_setting_value(_db, "upgrade_search_enabled", "false") == "true",
        "per_run": get_setting_value(_db, "upgrade_search_per_run", "20"),
        "interval_hours": get_setting_value(_db, "upgrade_search_interval_hours", "12"),
        "cutoff_resolution": _cutoff_resolution() if _cutoff_resolution else None,
        "dry_run_button": get_setting_value(_db, "upgrade_dry_run_button", "false") == "true",
        "profiles": [{"id": p["id"], "name": p["name"]} for p in quality_profile_repo.list_all(_db)],
        "active_profile_id": get_setting_value(_db, "quality_profile_id", ""),
    }


@router.post("/upgrades/refresh")
def upgrades_refresh():
    from routers.settings import get_setting_value
    return upgrades.refresh_candidates(_db, _get_stash_client(), lambda k, d: get_setting_value(_db, k, d),
                                       _cutoff_resolution() if _cutoff_resolution else None)


@router.post("/upgrades/{cand_id}/search")
def upgrades_search(cand_id: int, dry_run: bool = True):
    return _upgrade_search(cand_id, dry_run=dry_run)


@router.post("/upgrades/library-check/start")
def upgrades_library_check_start(force: bool = False):
    """One-time check of the whole library for upgrade candidates (upgrade_library_check.py); `force` asks the providers for every length again."""
    return _library_check_job.start(force)


@router.get("/upgrades/library-check/status")
def upgrades_library_check_status():
    from routers.settings import get_setting_value
    return {"job": _library_check_job.status, "last": _library_check_job.last_run(), "cutoff_resolution": _cutoff_resolution() if _cutoff_resolution else None,
            "vr_min_mbps": get_setting_value(_db, "vr_upgrade_min_mbps", "8"), **duration_check.summary(_db)}


# --- "Wrong length" candidates (v0.241.0): the Duration mismatch page moved into Upgrade wanted ---

_dur_sync = {"running": False, "phase": "", "done": 0, "total": 0, "result": None, "error": None, "finished_at": None}


@router.post("/upgrades/duration/sync")
def upgrades_duration_sync(body: dict | None = None):
    """Runs in the background: optionally the duration check itself (`run_check`: the providers are asked for the lengths of new scenes;
    `force` = every scene again), then the 'wrong length' candidates are made to match the stored comparison at `threshold` percent (saved)."""
    import threading
    import time
    import upgrade_library_check
    body = body or {}
    if _dur_sync["running"]:
        return {"started": False, "running": True}
    if body.get("threshold") is not None:
        try:
            upgrade_library_check.set_duration_threshold(_db, body["threshold"])
        except (TypeError, ValueError):
            raise HTTPException(400, "threshold must be a number (percent)")
    run_check, force = bool(body.get("run_check")), bool(body.get("force"))

    def work():
        _dur_sync.update(running=True, phase="", done=0, total=0, result=None, error=None)
        try:
            if run_check:
                _dur_sync["phase"] = "asking the providers for the lengths"
                res = _duration_job.run(force)
                if res.get("skipped"):  # one is already running: join it
                    while _duration_job.status.get("running"):
                        time.sleep(3)
                elif res.get("error"):
                    raise RuntimeError(f"the duration check failed: {res['error']}")
            _dur_sync["phase"] = "updating the list"
            _dur_sync["result"] = upgrade_library_check.sync_duration_candidates(
                _db, _get_stash_client(), upgrade_library_check.duration_threshold(_db), _dur_sync)
        except Exception as e:  # noqa: BLE001
            logger.exception("duration candidates failed")
            _dur_sync["error"] = f"{type(e).__name__}: {e}"
        finally:
            _dur_sync.update(running=False, phase="", finished_at=time.time())

    threading.Thread(target=work, daemon=True, name="duration-candidates").start()
    return {"started": True}


@router.get("/upgrades/duration/status")
def upgrades_duration_status():
    import upgrade_library_check
    return {"sync": dict(_dur_sync), "job": _duration_job.status, "threshold": upgrade_library_check.duration_threshold(_db), **duration_check.summary(_db)}


class UpgradeBulkBody(BaseModel):
    ids: list[int]
    action: str  # dismiss | restore | approve | wishlist | unwishlist | profile | search
    statuses: dict[str, str] | None = None  # restore: {id: status to go back to}
    profile_id: int | None = None  # profile: None / 0 = the profile of the Settings again
    previous: dict | None = None  # unwishlist: {id: {"status", "rows"}} as wishlist returned it


BULK_SEARCH_MAX = 10


@router.post("/upgrades/bulk")
def upgrades_bulk(body: UpgradeBulkBody):
    """Bulk actions on Upgrade wanted (v0.237.0). dismiss: returns the previous status of each (for the Undo); restore: back to those
    statuses; wishlist / unwishlist: to the normal wishlist and back (upgrades.to_wishlist); profile: sets / clears the candidates' own quality profile; search: real searches, one after the other, at most BULK_SEARCH_MAX."""
    ids = list(dict.fromkeys(int(i) for i in body.ids))
    if not ids:
        raise HTTPException(400, "no candidates selected")
    if body.action == "dismiss":
        previous = {}
        for i in ids:
            cand = upgrades.get_candidate(_db, i)
            if cand and cand["status"] in ("wanted", "review", "grabbed", "check"):
                previous[str(i)] = cand["status"]
                upgrades.set_candidate(_db, i, status="dismissed")
        return {"ok": True, "changed": len(previous), "previous": previous}
    if body.action == "restore":
        restored = 0
        for i in ids:
            cand = upgrades.get_candidate(_db, i)
            status = (body.statuses or {}).get(str(i), "wanted")
            if cand and cand["status"] == "dismissed" and status in ("wanted", "grabbed", "review", "check"):
                upgrades.set_candidate(_db, i, status=status)
                restored += 1
        return {"ok": True, "restored": restored}
    if body.action == "wishlist":
        previous, errors = {}, {}
        for i in ids:
            out = upgrades.to_wishlist(_db, i, _get_provider_client)
            if out.get("error"):
                errors[str(i)] = out["error"]
            else:
                previous[str(i)] = {"status": out["status_before"], "rows": out["previous"]}
        return {"ok": True, "changed": len(previous), "previous": previous, "errors": errors}
    if body.action == "unwishlist":
        undone = 0
        for i in ids:
            old = (body.previous or {}).get(str(i)) or {}
            if upgrades.undo_wishlist(_db, i, old.get("rows"), old.get("status")).get("ok"):
                undone += 1
        return {"ok": True, "restored": undone}
    if body.action == "approve":
        # 'check' -> 'wanted': the candidate found by the library check is released for the (automatic) upgrade search
        done = 0
        for i in ids:
            cand = upgrades.get_candidate(_db, i)
            if cand and cand["status"] == "check":
                upgrades.set_candidate(_db, i, status="wanted")
                done += 1
        return {"ok": True, "changed": done}
    if body.action == "profile":
        pid = body.profile_id or None
        if pid is not None and quality_profile_repo.get_one(_db, pid) is None:
            raise HTTPException(400, "unknown quality profile")
        for i in ids:
            upgrades.set_candidate(_db, i, quality_profile_id=pid)
        return {"ok": True, "changed": len(ids), "profile_id": pid}
    if body.action == "search":
        if len(ids) > BULK_SEARCH_MAX:
            raise HTTPException(400, f"at most {BULK_SEARCH_MAX} searches at a time (each asks the indexers); select fewer")
        results = []
        for i in ids:
            out = _upgrade_search(i, dry_run=False)
            results.append({"id": i, "grab": bool((out or {}).get("grab")), "result": (out or {}).get("result"), "error": (out or {}).get("error")})
        return {"ok": True, "results": results, "grabbed": sum(1 for r in results if r["grab"])}
    raise HTTPException(400, "unknown action")


# --- Duration mismatch (v0.209.0, duration_check.py) ---

@router.post("/duration-check/run")
def duration_check_run(force: bool = False):
    return _duration_job.start(force)


@router.get("/duration-check/status")
def duration_check_status():
    return {"job": _duration_job.status, **duration_check.summary(_db)}


@router.get("/duration-check/findings")
def duration_check_findings(threshold: float = 5.0, direction: str = "", ignored: bool = False, groups: str = "exclude"):
    if direction not in ("", "shorter", "longer"):
        raise HTTPException(400, "direction must be '', 'shorter' or 'longer'")
    if groups not in ("exclude", "only"):
        raise HTTPException(400, "groups must be 'exclude' or 'only'")
    rows = duration_check.list_findings(_db, threshold, direction, include_ignored=ignored, groups=groups)
    # Which of them are already on the way (an upgrade candidate of this file is wanted / grabbed).
    with _db.connect() as conn:
        busy = {(r["scene_id"]): r["status"] for r in conn.execute(
            "SELECT scene_id, status FROM upgrade_candidate WHERE reason IN ('duration', 'readd') AND status IN ('wanted', 'grabbed', 'review')")}
    for r in rows:
        r["upgrade_status"] = busy.get(r["scene_id"])
    return {"findings": rows, "threshold": threshold}


@router.post("/duration-check/{scene_id}/ignore")
def duration_check_ignore(scene_id: str, restore: bool = False):
    if not duration_check.get_row(_db, scene_id):
        raise HTTPException(404, "scene not in the duration check")
    duration_check.set_status(_db, scene_id, "checked" if restore else "ignored")
    return {"ok": True}


@router.post("/duration-check/{scene_id}/readd")
def duration_check_readd(scene_id: str, search: bool = True):
    row = duration_check.get_row(_db, scene_id)
    if not row:
        raise HTTPException(404, "scene not in the duration check")
    return _readd(scene_id, "duration", row.get("provider_sec"), search)


# --- Organize the library: rename / move (2026-09-28, library_organize.py) ---

@router.get("/sanity")
def sanity(renamers: bool = False):
    """The safety check for the moving/renaming functions (renamer_check.py): other renaming plugins in Stash, target folders inside a Stash
    library, formats that give every scene one name, Whisparr scope, files moved again. Read-only."""
    importer = _importer()
    return renamer_check.sanity(importer.stash, importer.setting, importer.db, show_renamers=renamers)


@router.get("/organize/studios")
def organize_studios(refresh: bool = False):
    import library_organize
    data = _organizer().overview(refresh=refresh)
    at = library_organize.stored_checked_at(_db)
    return {"studios": data, "verified": at is not None, "at": at}


@router.post("/organize/studios/recount")
def organize_recount():
    """Starts the background Recount: every studio with local candidates gets its real plan; only studios with something to do stay (v0.240.17)."""
    return _organizer().start_recount()


@router.get("/organize/studios/recount-status")
def organize_recount_status():
    return _organizer().recount_status()


@router.get("/organize/plan")
def organize_plan(studio_id: str, fresh: bool = False):
    """The studio's stored plan (instant), or a new one with fresh=true / when none is stored (v0.240.27)."""
    return _organizer().plan_for_studio(studio_id, fresh=fresh)


@router.post("/organize/apply")
def organize_apply(body: dict):
    kinds = [k for k in (body.get("kinds") or ["rename", "move"]) if k in ("rename", "move", "check")]
    return _organizer().apply(body.get("studio_id"), body.get("file_ids") or [], kinds=kinds, force=bool(body.get("force")),
                              ignore_renamers=bool(body.get("ignore_renamers")))


@router.post("/organize/check-file")
def organize_check_file(body: dict):
    """"Check" on a plan row that waits for a fingerprint check (v0.240.15): asks StashDB what the scene's file is, like the Fingerprint check page does,
    and only reports (nothing is written)."""
    scene_id = str(body.get("scene_id") or "")
    stash = _get_stash_client()
    scene = stash.scene_for_fingerprint_check(scene_id) if scene_id else None
    if scene is None:
        raise HTTPException(status_code=404, detail="Stash has no scene with this id any more")
    # The provider whose id the scene carries (StashDB first); a scene without any of the set-up providers' ids is looked up at the first one set up.
    rows = _fingerprint_providers()
    ids = {sid["endpoint"] for sid in scene["stash_ids"]}
    row = next((r for r in rows if r["endpoint"] in ids), rows[0] if rows else None)
    if row is None:
        return {"result": "unverifiable", "message": "No metadata provider is set up (Settings > Connections)."}
    lookup, row = _resolve_provider(row["key"])
    name = row["name"]
    matches, mismatches, ambiguous, unverifiable = library_check.find_library_mismatches(
        lookup, [scene], row["endpoint"], delay=0, provider_name=name)
    if matches:
        return {"result": "match", "message": f"{name} has this file's fingerprints on the scene it is tagged with -- the tag is right. Press \"Check again\" to plan it."}
    if mismatches:
        names = ", ".join(c.get("title") or c["id"] for c in mismatches[0]["found_as"])
        return {"result": "mismatch", "message": f"{name} identifies the file as another scene: {names}. The tag looks wrong -- fix it on the Fingerprint check page."}
    if ambiguous:
        return {"result": "ambiguous", "message": f"{name} lists {len(ambiguous[0]['found_as'])} scenes for this file: "
                + ", ".join(c.get("title") or c["id"] for c in ambiguous[0]["found_as"])}
    reason = unverifiable[0]["reason"] if unverifiable else "unknown"
    return {"result": "unverifiable", "message": f"{name} cannot tell: {reason}."}


@router.get("/organize/movies/plan")
def organize_movies_plan():
    """Existing films (Stash groups in the movie folders) against the movie folder/file templates. Read-only."""
    return _organizer().movies_plan()


@router.post("/organize/movies/apply")
def organize_movies_apply(body: dict):
    kinds = [k for k in (body.get("kinds") or ["rename", "move"]) if k in ("rename", "move")]
    return _organizer().movies_apply(body.get("file_ids") or [], kinds=kinds, force=bool(body.get("force")),
                                     ignore_renamers=bool(body.get("ignore_renamers")))


# --- Folder import: the source folders of the setting import_sources (2026-09-29, library_import.py) ---

@router.get("/import/sources")
def import_sources():
    return {"sources": _importer().sources()}


@router.get("/import/analyse")
def import_analyse(root: str):
    importer = _importer()
    if root.rstrip("/") not in importer.roots():
        raise HTTPException(status_code=400, detail="not an import source")
    return importer.analyse(root)


@router.post("/import/apply")
def import_apply(body: dict):
    importer = _importer()
    root = (body.get("root") or "").rstrip("/")
    if root not in importer.roots():
        raise HTTPException(status_code=400, detail="not an import source")
    return importer.apply(root, body.get("scene_ids") or [], picks=body.get("picks") or {}, force=bool(body.get("force")),
                          ignore_renamers=bool(body.get("ignore_renamers")), trust=body.get("trust") or [])


@router.post("/import/organized-hook")
def import_organized_hook(body: dict):
    """The plugin's Scene.Update.Post hook calls this when a scene was marked Organized in Stash (v0.191.0): what happens is decided by the
    setting organized_hook (off / preview / on), see library_import.Importer.organized_scene()."""
    importer = _importer()
    scene_id = body.get("scene_id")
    if scene_id is None:
        raise HTTPException(status_code=400, detail="scene_id is required")
    return importer.organized_scene(scene_id, (importer.setting("organized_hook") or "off").strip())


@router.get("/import/add-check")
def import_add_check(path: str):
    """Dry run of "add this folder as an import source" (v0.226.0): problems, video count, whether Stash gets a new library path."""
    return _importer().add_check(path)


@router.post("/import/add")
def import_add(body: dict):
    if body.get("confirm") is not True:
        raise HTTPException(status_code=400, detail="confirm=true required")
    importer = _importer()
    return importer.add_source(body.get("path") or "", importer.backup_dir)


@router.post("/import/remove")
def import_remove(body: dict):
    return _importer().remove_source(body.get("root") or "")


@router.get("/import/finish-check")
def import_finish_check(root: str):
    return _importer().finish_check(root)


@router.post("/import/finish")
def import_finish(body: dict):
    importer = _importer()
    root = (body.get("root") or "").rstrip("/")
    if root not in importer.roots():
        raise HTTPException(status_code=400, detail="not an import source")
    if body.get("confirm") is not True:
        raise HTTPException(status_code=400, detail="confirm=true required")
    return importer.finish_source(root, importer.backup_dir)


# --- Movies & groups lists (2026-09-29, library_movies.py) ---

@router.get("/movies/no-group")
def movies_no_group(refresh: bool = False):
    folders, at = kept_lists.get(_db, "movies_no_group", lambda: _movies().videos_without_group(), refresh)
    return {"folders": folders, "kept_at": at}


@router.post("/movies/create-group")
def movies_create_group(body: dict):
    try:
        result = _movies().create_group(body.get("title"), body.get("scene_ids") or [], group_id=body.get("group_id"),
                                        scrape_url=body.get("scrape_url"))
        kept_lists.invalidate(_db, *kept_lists.MOVIE_LISTS)  # the video has a group now, a group has a scene
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/movies/tpdb-search")
def movies_tpdb_search(body: dict):
    """ThePornDB movie search for a Group that still lacks the link (Import folder > "Complete via ThePornDB")."""
    client = _get_provider_client("theporndb") if _get_provider_client else None
    if client is None:
        raise HTTPException(status_code=400, detail="ThePornDB is not configured")
    q = (body.get("q") or "").strip()
    if not q:
        raise HTTPException(status_code=400, detail="q is required")
    try:
        return {"query": q, "results": _movies().tpdb_search(client, q, body.get("minutes"))}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"ThePornDB did not answer: {type(e).__name__}: {str(e)[:200]}")


@router.post("/movies/link-tpdb")
def movies_link_tpdb(body: dict):
    """The chosen ThePornDB movie becomes the Group's link; empty fields of the Group are filled (library_movies.link_group_to_tpdb)."""
    client = _get_provider_client("theporndb") if _get_provider_client else None
    if client is None:
        raise HTTPException(status_code=400, detail="ThePornDB is not configured")
    if not body.get("group_id") or not body.get("tpdb_id"):
        raise HTTPException(status_code=400, detail="group_id and tpdb_id are required")
    movie = client._get(f"/movies/{body['tpdb_id']}").json().get("data")
    if not movie:
        raise HTTPException(status_code=404, detail="ThePornDB does not know this movie")
    try:
        res = _movies().link_group_to_tpdb(movie, body["group_id"])
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    history.add("info", "history", "movies", f"Group {res['name']!r} linked to ThePornDB movie {movie.get('title')!r}"
                + (f" (filled: {', '.join(res['filled'])})" if res["filled"] else ""), details={"group": res["group_id"], "tpdb": body["tpdb_id"]})
    return res


@router.get("/movies/compare-groups")
def movies_compare_groups(refresh: bool = False):
    """Film folders without a group next to the empty groups they may belong to (suggestions only, group_compare.py). The two lists it is made
    from are the kept ones of Movies & groups (v0.241.7); the pairing itself is quick and always uses the current "Not this" choices."""
    m = _movies()
    folders, at1 = kept_lists.get(_db, "movies_no_group", lambda: m.videos_without_group(), refresh)
    groups, at2 = kept_lists.get(_db, "movies_groups_empty", lambda: m.groups_without_files(), refresh)
    return {**group_compare.build(_db, folders, groups), "kept_at": min(at1, at2)}


@router.post("/movies/compare-groups/ignore")
def movies_compare_groups_ignore(body: dict):
    """"Not this" for one suggested pair (undo=true takes it back)."""
    key, gid = body.get("folder_key"), body.get("group_id")
    if not key or not gid:
        raise HTTPException(status_code=400, detail="folder_key and group_id are required")
    group_compare.set_ignored(_db, key, gid, not body.get("undo"))
    return {"ok": True}


@router.get("/movies/group-sources")
def movies_group_sources():
    """The title-search sources and the installed Stash group scrapers (which page addresses work)."""
    return _movies().group_sources()


@router.get("/movies/group-search")
def movies_group_search(source: str, q: str):
    """Title search at one source (Stash's group scrapers only read a page): [{id, title, url, image, type}]."""
    try:
        return {"results": _movies().group_search(source, q)}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"search failed: {e}")


@router.get("/movies/performers-empty")
def movies_performers_empty():
    return {"performers": _movies().performers_without_scenes()}


@router.post("/movies/performers/{performer_id}/unwatch")
def movies_performer_unwatch(performer_id: str):
    """Takes a performer off the watchlist (the tag, or the Favorite flag, by Settings > watchlist source). The performer stays in Stash."""
    import watchlist_source
    try:
        watchlist_source.set_watched(_db, _get_stash_client(), "performer", performer_id, False)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"{type(e).__name__}: {e}")
    return {"ok": True}


@router.post("/movies/performers/{performer_id}/delete")
def movies_performer_delete(performer_id: str):
    """Deletes a performer that has no scene from Stash (re-checked here; the next watchlist sync drops it from the watchlist)."""
    try:
        result = _movies().delete_empty_performer(performer_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"{type(e).__name__}: {e}")
    history.event("info", "maintenance", f"Performer \"{result['name']}\" (no scenes) deleted from Stash", details={"performer": performer_id})
    return result


_slow_counts = {"at": 0.0, "data": {}}


@router.get("/slow-counts")
def slow_counts():
    """Badge numbers of the Maintenance side menu that need Stash (v0.239.1): Performers without scenes (the ones NOT on the watchlist), Unidentified
    scenes, Compare groups (open suggestions), Movies & groups (videos without a group). Kept for a minute; a number that cannot be read is left out (no badge)."""
    if time.time() - _slow_counts["at"] < 60:
        return _slow_counts["data"]
    import routers.compare as compare_router
    out = {}
    for key, count in (
        ("performers", lambda: len([p for p in _movies().performers_without_scenes() if not p["on_watchlist"]])),
        ("unidentified", lambda: len(compare_router._compare().unidentified_list()["scenes"])),
        # Movies & groups: the videos in the movie folders that are in no group yet (v0.241.8), from the kept list of that page. (Compare groups is
        # a tab of that page since v0.242.0; its number is in the tab.)
        ("movies", lambda: len(kept_lists.get(_db, "movies_no_group", lambda: _movies().videos_without_group())[0])),
    ):
        try:
            out[key] = count()
        except Exception:
            logger.info("slow badge count %s failed", key, exc_info=True)
    _slow_counts.update(at=time.time(), data=out)
    return out


@router.get("/movies/group-namesakes")
def movies_group_namesakes(refresh: bool = False):
    """Empty groups that have a namesake with files (kept like the other lists of the Movies pages)."""
    rows, at = kept_lists.get(_db, "movies_group_namesakes", lambda: _movies().group_namesakes(), refresh)
    # What the user dismissed ("not the same film") is left out when the answer is given, so the kept list needs no change for it.
    ignored = group_compare.ignored_pairs(_db)
    shown, hidden = [], 0
    for r in rows:
        rest = [n for n in r["namesakes"] if (f"group:{r['group']['id']}", str(n["id"])) not in ignored]
        if rest:
            shown.append({**r, "namesakes": rest})
        else:
            hidden += 1
    return {"namesakes": shown, "kept_at": at, "hidden": hidden}


@router.post("/movies/group-namesakes/dismiss")
def movies_group_namesakes_dismiss(body: dict):
    """"Dismiss": the empty group is NOT the same film as these namesakes (undo=true takes it back)."""
    gid, ids = body.get("group_id"), body.get("namesake_ids") or []
    if not gid or not ids:
        raise HTTPException(status_code=400, detail="group_id and namesake_ids are required")
    for nid in ids:
        group_compare.set_ignored(_db, f"group:{gid}", str(nid), not body.get("undo"))
    return {"ok": True}


@router.post("/movies/groups/{group_id}/delete")
def movies_group_delete(group_id: str):
    try:
        result = _movies().delete_empty_group(group_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Stash refused: {e}")
    kept_lists.invalidate(_db, *kept_lists.MOVIE_LISTS, "movies_group_namesakes")
    history.event("info", "movies", f"Empty group \"{result['name']}\" deleted from Stash (a namesake with files exists)", details={"group": group_id})
    return result


@router.get("/movies/groups-empty")
def movies_groups_empty(refresh: bool = False):
    groups, at = kept_lists.get(_db, "movies_groups_empty", lambda: _movies().groups_without_files(), refresh)
    return {"groups": groups, "kept_at": at}


@router.post("/import/suggest")
def import_suggest(body: dict):
    importer = _importer()
    root = (body.get("root") or "").rstrip("/")
    if root not in importer.roots():
        raise HTTPException(status_code=400, detail="not an import source")
    return {"suggestions": importer.suggest(root, body.get("scene_ids") or [])}


# --- File names with "Unknown" as quality (2026-09-28, library_rename.py) ---

@router.get("/unknown-quality")
def unknown_quality():
    return {"items": library_rename.proposals(_db, _get_stash_client())}


@router.post("/unknown-quality/apply")
def unknown_quality_apply(body: dict):
    return library_rename.apply(_db, _get_stash_client(), _to_local, body.get("paths") or [])


@router.post("/upgrades/{cand_id}/review/accept")
def upgrades_review_accept(cand_id: int):
    """Activity > Needs review (2026-09-28): grab the stored candidate."""
    return upgrades.accept_review(_db, cand_id, _get_provider_client)


@router.post("/upgrades/{cand_id}/review/reject")
def upgrades_review_reject(cand_id: int):
    return upgrades.reject_review(_db, cand_id)


@router.post("/upgrades/{cand_id}/manual-grab")
def upgrades_manual_grab(cand_id: int, body: dict):
    """Grab of a result the user picked from a typed search on Upgrade wanted (the search itself is releases/.../manual-search)."""
    result = upgrades.manual_grab(_db, cand_id, _get_provider_client, body.get("candidate") or {})
    if result.get("error"):
        raise HTTPException(400, result["error"])
    return result


@router.post("/upgrades/{cand_id}/dismiss")
def upgrades_dismiss(cand_id: int):
    cand = upgrades.get_candidate(_db, cand_id)
    upgrades.set_candidate(_db, cand_id, status="dismissed")
    return {"ok": True, "previous": cand["status"] if cand else None}


class RestoreBody(BaseModel):
    status: str


@router.post("/upgrades/{cand_id}/restore")
def upgrades_restore(cand_id: int, body: RestoreBody):
    """Undo of "Dismiss" on Upgrade wanted: back to the status the candidate had before."""
    cand = upgrades.get_candidate(_db, cand_id)
    if not cand or cand["status"] != "dismissed" or body.status not in ("wanted", "grabbed", "review", "check"):
        raise HTTPException(400, "nothing to restore")
    upgrades.set_candidate(_db, cand_id, status=body.status)
    return {"ok": True}


# --- VR check: "VR scene, but the file is 2D" (2026-09-26, vr_check.py) ---

@router.post("/vr-check/run")
def vr_check_run(mode: str = "quick", force: bool = False):
    if mode not in ("quick", "full"):
        raise HTTPException(400, "mode must be 'quick' or 'full'")
    return _vr_job.start(mode, force)


@router.get("/vr-check/status")
def vr_check_status():
    return {"job": _vr_job.status, "last_run": vr_check.last_run(_db)}


def _sidecar_release(finding):
    """The release THIS sidecar downloaded for the finding's scene, if any
    -- only then can "Fix wrong download" blocklist + re-search it."""
    if not finding.get("provider_scene_id"):
        return None
    with _db.connect() as conn:
        row = conn.execute(
            """SELECT provider, scene_stashdb_id, entity_type, entity_stashdb_id FROM release
               WHERE provider = ? AND scene_stashdb_id = ? AND download_client_id IS NOT NULL
                 AND status IN ('imported', 'import_failed') LIMIT 1""",
            (finding["provider"], finding["provider_scene_id"])).fetchone()
    return dict(row) if row else None


@router.get("/vr-check/findings")
def vr_check_findings(status: str = "pending"):
    if status in ("pending", "dismissed"):
        try:
            vr_check.prune_stale(_db, _get_stash_client())
        except Exception:
            logger.exception("VR check: couldn't prune stale findings")
    findings = vr_check.list_findings(_db, status)
    for f in findings:
        f["sidecar_release"] = _sidecar_release(f)
    return {"findings": findings}


@router.get("/vr-check/findings/{finding_id}/frame")
def vr_check_frame(finding_id: int):
    """A frame from the middle of the flagged file as a data: URL (goes
    through the plugin bridge like every other call, hence JSON)."""
    import base64
    finding = vr_check.get_finding(_db, finding_id)
    if finding is None:
        raise HTTPException(404, "finding not found")
    try:
        jpeg = vr_check.frame_jpeg(_vr_job.to_local_path(finding["file_path"]))
    except Exception as e:
        raise HTTPException(500, f"couldn't read a frame: {e}")
    return {"data_url": "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()}


@router.post("/vr-check/findings/{finding_id}/dismiss")
def vr_check_dismiss(finding_id: int):
    finding = vr_check.get_finding(_db, finding_id)
    vr_check.set_finding_status(_db, finding_id, "dismissed")
    return {"ok": True, "previous": finding["finding_status"] if finding else None}


class VerdictBody(BaseModel):
    verdict: str


@router.post("/vr-check/findings/{finding_id}/verdict")
def vr_check_set_verdict(finding_id: int, body: VerdictBody):
    """The user's own verdict for a file: 2D ('mono'), 'vr_mono' or 'unclear' (v0.105.0). Kept over re-judging/re-analysis of the
    same file. Answers with the previous state for the Undo."""
    if body.verdict not in vr_check.MANUAL_VERDICTS:
        raise HTTPException(400, "verdict must be one of: " + ", ".join(vr_check.MANUAL_VERDICTS))
    before = vr_check.set_verdict(_db, finding_id, body.verdict)
    if before is None:
        raise HTTPException(404, "finding not found")
    return {"ok": True, "previous": before}


class VerdictRestoreBody(BaseModel):
    verdict: str
    manual: bool = False
    finding_status: str = "pending"


@router.post("/vr-check/findings/{finding_id}/verdict-restore")
def vr_check_restore_verdict(finding_id: int, body: VerdictRestoreBody):
    if not vr_check.get_finding(_db, finding_id):
        raise HTTPException(404, "finding not found")
    vr_check.restore_verdict(_db, finding_id, body.verdict, body.manual, body.finding_status)
    return {"ok": True}


@router.post("/vr-check/findings/{finding_id}/restore")
def vr_check_restore(finding_id: int, body: RestoreBody):
    """Undo of "Dismiss" on a VR finding: back to the status it had before."""
    finding = vr_check.get_finding(_db, finding_id)
    if not finding or finding["finding_status"] != "dismissed" or body.status in ("dismissed", "none", ""):
        raise HTTPException(400, "nothing to restore")
    vr_check.set_finding_status(_db, finding_id, body.status)
    return {"ok": True}


@router.post("/vr-check/findings/{finding_id}/fix")
def vr_check_fix(finding_id: int):
    """"Fix wrong download" for a flagged file this sidecar downloaded:
    blocklist the release, move the file to the recycle bin, reset the
    release so it gets searched again (file_importer.fix_wrong_download()
    with the flagged file's own path)."""
    finding = vr_check.get_finding(_db, finding_id)
    if finding is None:
        raise HTTPException(404, "finding not found")
    release = _sidecar_release(finding)
    if release is None:
        raise HTTPException(409, "not downloaded by this sidecar -- nothing to re-search")
    result = _fix_wrong_download(release["provider"], release["scene_stashdb_id"], release["entity_type"],
                                 release["entity_stashdb_id"], finding["file_path"])
    vr_check.set_finding_status(_db, finding_id, "fixed")
    return result


@router.get("/library-paths")
def library_paths():
    """Stash's own configured top-level library directories -- shown
    as quick-jump shortcuts above the directory browser (see
    /directory-browse below), same starting points Stash's native
    task dialogs offer before you navigate into a subdirectory."""
    return {"paths": _get_stash_client().list_library_paths()}


@router.get("/directory-browse")
def directory_browse(path: str | None = None):
    """Backs the Maintenance page's directory picker -- mirrors Stash's
    own "Select folders" dialog (used by Scan/Clean/Generate's
    "Selective..." buttons) exactly: browse via `directory`, add the
    current path to a selection list client-side (see
    stash_client.browse_directory())."""
    return _get_stash_client().browse_directory(path)


def _fingerprint_providers():
    import metadata_provider_repo
    return fingerprint_provider.available(_get_stash_client(), _get_provider_client, metadata_provider_repo.list_all(_db, enabled_only=True))


def _resolve_provider(key):
    """(lookup client, provider row) for the Fingerprint check; HTTP 400 when that provider is not set up."""
    import metadata_provider_repo
    try:
        return fingerprint_provider.resolve(key, _get_stash_client(), _get_provider_client, metadata_provider_repo.list_all(_db, enabled_only=True))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


def _run_check(run_id, paths, provider="stashdb"):
    stash_client = _get_stash_client()
    try:
        stashdb_client, prow = _resolve_provider(provider)
        scenes = library_check.list_scenes_in_scope(stash_client, paths, prow["endpoint"])
        library_check_repo.update_run_progress(_db, run_id, 0, len(scenes))
        matches, mismatches, ambiguous, unverifiable = library_check.find_library_mismatches(
            stashdb_client, scenes, prow["endpoint"],
            progress_cb=lambda current, total: library_check_repo.update_run_progress(_db, run_id, current, total),
            provider_name=prow["name"],
        )
        for m in mismatches:
            library_check_repo.insert_finding(
                _db, run_id, m["id"], m["title"], "mismatch",
                expected_stashdb_id=m["expected_stashdb_id"], candidates=m["found_as"], file_paths=m["paths"],
            )
        for a in ambiguous:
            library_check_repo.insert_finding(
                _db, run_id, a["id"], a["title"], "ambiguous",
                expected_stashdb_id=a["expected_stashdb_id"], candidates=a["found_as"], file_paths=a["paths"],
            )
        for u in unverifiable:
            library_check_repo.insert_finding(
                _db, run_id, u["id"], u["title"], "unverifiable", reason=u["reason"], file_paths=u["paths"],
            )
        library_check_repo.finish_run(
            _db, run_id, "done",
            match_count=len(matches), mismatch_count=len(mismatches), unverifiable_count=len(unverifiable) + len(ambiguous),
        )
    except Exception as e:
        library_check_repo.finish_run(_db, run_id, "failed", error=str(e))
    finally:
        _check_lock.release()


@router.post("/library-check/run")
def run_now(body: dict | None = None):
    paths = (body or {}).get("paths") or []
    provider = (body or {}).get("provider") or "stashdb"
    _resolve_provider(provider)
    if not _check_lock.acquire(blocking=False):
        return {"skipped": True, "reason": "a library check is already running"}
    run_id = library_check_repo.create_run(_db, paths, provider)
    threading.Thread(target=_run_check, args=(run_id, paths, provider), daemon=True).start()
    return {"run_id": run_id}


@router.get("/library-check/providers")
def library_check_providers():
    """The providers the Fingerprint check can ask (set up in Settings > Connections) and the one used last, which is the default. `batch` false =
    asked one file at a time (ThePornDB): a whole-library run takes long, a single re-check does not."""
    rows = _fingerprint_providers()
    last = (library_check_repo.get_latest_run(_db) or {}).get("provider")
    default = last if any(r["key"] == last for r in rows) else (rows[0]["key"] if rows else None)
    return {"providers": rows, "default": default}


@router.get("/library-check/status")
def status():
    run = library_check_repo.get_latest_run(_db)
    if not run:
        return {"phase": "idle"}
    phase = "running" if run["status"] == "running" else run["status"]
    return {
        "phase": phase,
        "run_id": run["id"],
        "provider": run.get("provider") or "stashdb",
        "current": run["current"],
        "total": run["total"],
        "match_count": run["match_count"],
        "mismatch_count": run["mismatch_count"],
        "unverifiable_count": run["unverifiable_count"],
        "error": run["error"],
    }


@router.get("/library-check/findings")
def findings(status: str = "pending", kind: str | None = None):
    return {"findings": library_check_repo.list_findings(_db, status=status, kind=kind)}


@router.post("/library-check/findings/{finding_id}/apply")
def apply_finding(finding_id: int):
    finding = library_check_repo.get_finding(_db, finding_id)
    if not finding:
        raise HTTPException(status_code=404, detail="finding not found")
    if finding["kind"] != "mismatch":
        raise HTTPException(status_code=400, detail=f"finding is '{finding['kind']}', not an actionable mismatch")
    candidates = finding["candidates"]
    target_id = candidates[0]["id"]
    provider = library_check_repo.provider_of_finding(_db, finding)
    endpoint = None if provider == "stashdb" else _resolve_provider(provider)[1]["endpoint"]
    try:
        result = library_check.apply_fix(_get_stash_client(), finding["scene_id"], target_id, endpoint=endpoint,
                                         keep_other_endpoints=endpoint is not None)
        library_check_repo.set_finding_status(_db, finding_id, "applied", applied_title=result["title"])
        return {"applied": True, "title": result["title"]}
    except Exception as e:
        library_check_repo.set_finding_status(_db, finding_id, "failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/library-check/findings/{finding_id}/recheck")
def recheck_finding(finding_id: int):
    """"Re-check fingerprints" on a Needs-manual-review finding: StashDB is asked again for this scene's file(s), see library_check.recheck_finding()."""
    finding = library_check_repo.get_finding(_db, finding_id)
    if not finding:
        raise HTTPException(status_code=404, detail="finding not found")
    if finding.get("status") != "pending":
        raise HTTPException(status_code=400, detail="only an open finding can be re-checked")
    lookup, prow = _resolve_provider(library_check_repo.provider_of_finding(_db, finding))
    result = library_check.recheck_finding(_db, _get_stash_client(), lookup, finding, endpoint=prow["endpoint"], provider_name=prow["name"])
    if result.get("error"):
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@router.post("/library-check/findings/{finding_id}/confirm")
def confirm_finding(finding_id: int, body: dict | None = None):
    """"File is correct" on a Needs-manual-review finding that StashDB has no fingerprint match for (v0.238.2): the user says the scene's StashDB id is right,
    the finding closes (status 'applied', "Confirmed by you"). body {"submit": true} first submits the file's fingerprints to StashDB (public, not undoable;
    only while the setting fingerprint_upload_enabled is on) -- a failing submission leaves the finding open."""
    from routers.settings import get_setting_value
    finding = library_check_repo.get_finding(_db, finding_id)
    if not finding:
        raise HTTPException(status_code=404, detail="finding not found")
    if finding.get("status") != "pending" or finding.get("kind") != "unverifiable":
        raise HTTPException(status_code=400, detail="only an open 'needs manual review' finding can be confirmed")
    submitted = False
    if (body or {}).get("submit"):
        if get_setting_value(_db, "fingerprint_upload_enabled", "false") != "true":
            raise HTTPException(status_code=400, detail="fingerprint upload is switched off (Settings > Schedule & Import)")
        stash_client = _get_stash_client()
        prow = _resolve_provider(library_check_repo.provider_of_finding(_db, finding))[1]
        if not prow["submit"]:
            raise HTTPException(status_code=400, detail=f"{prow['name']} does not take fingerprints from Stash")
        scene = stash_client.scene_for_fingerprint_check(finding["scene_id"])
        if scene is None:
            raise HTTPException(status_code=400, detail="Stash has no scene with this id any more")
        if not any(sid["endpoint"] == prow["endpoint"] for sid in scene["stash_ids"]):
            raise HTTPException(status_code=400, detail=f"the scene carries no {prow['name']} id, there is nothing to submit the fingerprints to")
        try:
            submitted = stash_client.submit_fingerprints(finding["scene_id"], prow["endpoint"])
        except Exception as e:
            logging.getLogger(__name__).exception("fingerprint submission for scene %s failed", finding["scene_id"])
            raise HTTPException(status_code=400, detail=f"submitting the fingerprints failed: {type(e).__name__}: {e}")
    library_check_repo.set_finding_status(_db, finding_id, "applied", applied_title="Confirmed by you")
    history.event("info", "maintenance", f"Fingerprint check: \"{finding.get('scene_title')}\" confirmed by the user"
                  + (f" and its fingerprints submitted to {_fingerprint_provider_name(finding)}" if submitted else ""), details={"scene": finding.get("scene_id")})
    return {"ok": True, "submitted": submitted}


def _fingerprint_provider_name(finding):
    key = library_check_repo.provider_of_finding(_db, finding)
    return next((r["name"] for r in _fingerprint_providers() if r["key"] == key), key)


@router.post("/library-check/findings/{finding_id}/dismiss")
def dismiss_finding(finding_id: int):
    finding = library_check_repo.get_finding(_db, finding_id)
    if not finding:
        raise HTTPException(status_code=404, detail="finding not found")
    library_check_repo.set_finding_status(_db, finding_id, "dismissed")
    return {"dismissed": True, "previous": finding.get("status")}


@router.post("/library-check/findings/{finding_id}/restore")
def restore_finding(finding_id: int, body: RestoreBody):
    """Undo of "Dismiss" on a Library-check finding: back to 'pending' (the only status Dismiss is offered from)."""
    finding = library_check_repo.get_finding(_db, finding_id)
    if not finding or finding.get("status") != "dismissed" or body.status != "pending":
        raise HTTPException(400, "nothing to restore")
    with _db.connect() as conn:
        conn.execute("UPDATE library_check_finding SET status = 'pending', resolved_at = NULL WHERE id = ?", (finding_id,))
    return {"ok": True}
