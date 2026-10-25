"""Shared logic behind the Maintenance page's "Library Check" and the
standalone scripts/audit_library_fingerprints.py +
scripts/fix_library_mismatches.py -- one source of truth for detecting
and correcting a local Stash scene pointing at the wrong StashDB scene
id, used by both the router (routers/maintenance.py, DB-backed,
progress-polled) and the CLI tools (JSON report, dry-run/--apply).

See PROGRESS.md's "Retroactive library audit" and "Maintenance page"
sections for the full investigation this was built from -- StashDB's
own crowd-sourced oshash/phash fingerprint registry is the source of
truth checked against, not just "does this collide with something
already in the local library" (see verification.py's
verify_downloaded_file() for the same idea applied to new hand-offs
instead of the existing library)."""
import time

from stashdb_client import MAX_FINGERPRINT_BATCH


def local_fingerprints(scene):
    """All {"hash", "algorithm"} dicts across every file this scene
    has locally, de-duplicated -- a scene can have more than one file
    (duplicates at different resolutions, see stash_client.py's
    count_scenes_meeting_resolution() docstring), and every one of
    them should be checked, not just the first."""
    seen = set()
    fps = []
    for f in scene.get("files") or []:
        for fp in f.get("fingerprints") or []:
            key = (fp["type"].upper(), fp["value"])
            if key in seen:
                continue
            seen.add(key)
            fps.append({"hash": fp["value"], "algorithm": fp["type"].upper()})
    return fps


def list_scenes_in_scope(stash_client, paths, endpoint=None):
    """Every local scene carrying a StashDB stash_id
    (stash_client.all_scenes_with_stashdb_id(); `endpoint` = the provider's, None = StashDB, v0.243.0), one bulk local
    GraphQL call), optionally narrowed to scenes with at least one
    file under one of `paths` -- any directory (not just a configured
    top-level library path), picked via the same browse-and-add
    mechanism as Stash's own native Scan/Clean/Generate task dialogs
    (see stash_client.browse_directory()). `paths` empty/None means no
    filtering (the full library). A root is matched with a trailing
    separator added first so e.g. "/data/movies" doesn't also match
    "/data/moviesXYZ"."""
    scenes = stash_client.all_scenes_with_stashdb_id(endpoint)
    if not paths:
        return scenes
    roots = [p if p.endswith("/") else p + "/" for p in paths]
    return [
        s for s in scenes
        if any(f["path"].startswith(root) for f in (s.get("files") or []) for root in roots)
    ]


def find_library_mismatches(stashdb_client, scenes, stashdb_endpoint,
                             batch_size=MAX_FINGERPRINT_BATCH, delay=0.5, progress_cb=None, provider_name="StashDB"):
    """The actual check: batches each in-scope scene's own local
    fingerprints through StashDB's reverse lookup
    (find_scenes_by_fingerprints()) and classifies against what that
    scene's own claimed stash_id should be.

    Returns (matches, mismatches, ambiguous, unverifiable) -- four
    lists of {"id", "title", ...} dicts:
      - match: the scene's own stash_id is among StashDB's answer for
        its file's fingerprints.
      - mismatch: exactly ONE other candidate scene came back instead
        -- unambiguous, safe to auto-correct (see apply_fix()).
      - ambiguous: 2+ candidates came back -- can't safely pick one
        automatically, informational only.
      - unverifiable: no local fingerprints yet (unscanned file) OR
        StashDB returned zero candidates at all (no fingerprint data
        registered for this exact file, regardless of which scene it
        might belong to) -- never treated as a mismatch, same "can't
        judge" rule verification.py's verify_downloaded_file() applies
        to a single new hand-off.

    `progress_cb(checked, total)` is called after each batch if given
    (`total` is the checkable count, i.e. excluding scenes already
    bucketed as unverifiable for having no local fingerprints)."""
    checkable = []
    unverifiable = []
    for s in scenes:
        fps = local_fingerprints(s)
        paths = [f["path"] for f in (s.get("files") or [])]
        stashdb_id = next((sid["stash_id"] for sid in s["stash_ids"] if sid["endpoint"] == stashdb_endpoint), None)
        if not fps or not stashdb_id:
            unverifiable.append({"id": s["id"], "title": s["title"], "paths": paths, "reason": "no local fingerprints yet"})
            continue
        checkable.append({"id": s["id"], "title": s["title"], "paths": paths, "stashdb_id": stashdb_id, "fps": fps})

    matches, mismatches, ambiguous = [], [], []
    total = len(checkable)
    for i in range(0, total, batch_size):
        batch = checkable[i:i + batch_size]
        results = stashdb_client.find_scenes_by_fingerprints([c["fps"] for c in batch])
        for entry, found in zip(batch, results):
            found_ids = {f["id"] for f in found}
            if entry["stashdb_id"] in found_ids:
                matches.append(entry)
            elif not found:
                unverifiable.append({
                    "id": entry["id"], "title": entry["title"], "paths": entry["paths"],
                    "reason": f"{provider_name} has no fingerprint match at all for this file",
                })
            elif len(found) == 1:
                mismatches.append({
                    "id": entry["id"], "title": entry["title"], "paths": entry["paths"],
                    "expected_stashdb_id": entry["stashdb_id"], "found_as": found,
                })
            else:
                ambiguous.append({
                    "id": entry["id"], "title": entry["title"], "paths": entry["paths"],
                    "expected_stashdb_id": entry["stashdb_id"], "found_as": found,
                })
        if progress_cb:
            progress_cb(min(i + batch_size, total), total)
        if i + batch_size < total:
            time.sleep(delay)

    return matches, mismatches, ambiguous, unverifiable


def apply_fix(stash_client, scene_id, expected_target_id, create_missing_performers=False, endpoint=None,
              keep_other_endpoints=False):
    """Retags `scene_id` onto `expected_target_id` -- the scrape-by-id
    + conflict pre-check + retag sequence proven live 2026-09-16
    against all 17 real mismatches this session found. Raises
    RuntimeError with a clear reason on conflict or scrape failure;
    callers turn that into a 'failed' finding status. Returns the
    sceneUpdate result dict ({"id", "title", "stash_ids"}) on success.

    The conflict check is always re-done live here rather than trusted
    from whenever the check itself ran -- the library can change
    between a check and an apply.

    `create_missing_performers` (2026-09-19, default False -- preserves
    this tool's existing conservative behavior) is passed straight
    through to stash_client.retag_scene(); file_importer.py's fresh-
    import tagging path opts in, per the user's own request.

    `endpoint` (2026-09-25): which stash-box the id belongs to -- None =
    StashDB; file_importer.py passes ThePornDB's for TPDB imports."""
    conflict = stash_client.find_scene_by_stashdb_id(expected_target_id, endpoint)
    if conflict and str(conflict["id"]) != str(scene_id):
        raise RuntimeError(
            f"Scene id {expected_target_id} is already claimed by local scene "
            f"{conflict['id']} \"{conflict['title']}\""
        )
    scraped = stash_client.scrape_scene_by_stashdb_id(expected_target_id, endpoint)
    if not scraped:
        raise RuntimeError(f"The metadata provider no longer has scene {expected_target_id}")
    return stash_client.retag_scene(scene_id, scraped, expected_target_id,
                                     create_missing_performers=create_missing_performers, endpoint=endpoint,
                                     keep_other_endpoints=keep_other_endpoints)


def recheck_finding(db, stash_client, stashdb_client, finding, endpoint=None, provider_name="StashDB"):
    """"Re-check fingerprints" on a "Needs manual review" finding (v0.238.0): asks the provider (StashDB unless the run used another, v0.243.0) again for what the scene's file(s) are -- someone may have
    registered fingerprints since the run -- and files the answer like a run would: a match closes the finding, one other scene makes it an
    auto-fixable mismatch, several scenes an ambiguous one, nothing leaves it unverifiable. Returns {"result", "message"} or {"error"}."""
    import library_check_repo
    scene = stash_client.scene_for_fingerprint_check(finding["scene_id"])
    if scene is None:
        return {"error": "Stash has no scene with this id any more"}
    matches, mismatches, ambiguous, unverifiable = find_library_mismatches(stashdb_client, [scene], endpoint or stash_client.stashdb_endpoint, delay=0, provider_name=provider_name)
    run_id = finding.get("run_id")
    if matches:
        library_check_repo.delete_finding(db, finding["id"])
        return {"result": "verified", "message": f"{provider_name} now has fingerprints that match this scene's id -- the finding is closed."}
    if mismatches:
        m = mismatches[0]
        library_check_repo.insert_finding(db, run_id, m["id"], m["title"], "mismatch", expected_stashdb_id=m["expected_stashdb_id"],
                                          candidates=m["found_as"], file_paths=m["paths"])
        names = ", ".join(c.get("title") or c["id"] for c in m["found_as"])
        return {"result": "mismatch", "message": f"{provider_name} now identifies the file as {names} -- it moved to the corrections list."}
    if ambiguous:
        a = ambiguous[0]
        library_check_repo.insert_finding(db, run_id, a["id"], a["title"], "ambiguous", expected_stashdb_id=a["expected_stashdb_id"],
                                          candidates=a["found_as"], file_paths=a["paths"])
        return {"result": "ambiguous", "message": f"{provider_name} lists {len(a['found_as'])} scenes for this file: "
                + ", ".join(c.get("title") or c["id"] for c in a["found_as"])}
    u = unverifiable[0] if unverifiable else {"reason": "unknown"}
    return {"result": "unverifiable", "message": f"Still not verifiable: {u['reason']}."}
