#!/usr/bin/env python3
"""Backend bridge for the StashReleaseManager plugin.

Proxies requests from the plugin's JS to the sidecar API to bypass
browser CSP restrictions -- same pattern as stash-sense2's
stash_sense_backend.py, scaled down to this project's much smaller
mode set.
"""
import json
import sys
from urllib.parse import quote, urlencode

import requests


def main():
    input_data = json.load(sys.stdin)
    args = input_data.get("args", {})
    mode = args.get("mode", "")

    # Stash-triggered hook call (see stash-release-manager.yml's
    # `hooks:` section) -- has no `mode` of its own, carries a
    # `hookContext` instead. Handled as its own path, before the
    # sidecar-connection sync below (a hook invocation isn't guaranteed
    # to carry this plugin's own native settings the same way a runOp()
    # call always does).
    if not mode and "hookContext" in args:
        _handle_hook(input_data, args)
        return

    sidecar_url = args.get("sidecar_url", "http://localhost:6961").rstrip("/")
    # Stash's own API key, StashDB/ThePornDB keys and the other stash-boxes are read from Stash here and handed to the sidecar
    # (see _sync_from_stash()); the page's first calls (setup status, header connection check) carry it, so a new installation
    # needs nothing typed in.
    sync_report = _sync_from_stash(sidecar_url, input_data, args) if mode in _SYNC_MODES else None

    if mode == "stash_sync":
        result = sync_report
    elif mode == "health":
        result = _get(sidecar_url, "/health", timeout=10)
    elif mode == "watchlist_list":
        params = {"provider": args.get("provider"), "entity_type": args.get("entity_type")}
        qs = "&".join(f"{k}={v}" for k, v in params.items() if v is not None)
        result = _get(sidecar_url, f"/watchlist?{qs}")
    elif mode == "watchlist_add":
        result = _post(sidecar_url, "/watchlist", {
            "provider": args.get("provider", "stashdb"),
            "entity_type": args.get("entity_type"),
            "stashdb_id": args.get("stashdb_id"),
            "name": args.get("name"),
        })
    elif mode == "watchlist_remove":
        path = f"/watchlist/{args.get('provider')}/{args.get('entity_type')}/{args.get('stashdb_id')}"
        result = _delete(sidecar_url, path)
    elif mode == "watchlist_set_auto_handoff":
        path = f"/watchlist/{args.get('provider')}/{args.get('entity_type')}/{args.get('stashdb_id')}/auto-handoff"
        result = _post(sidecar_url, path, {
            "enabled": args.get("enabled", False),
            "from_date": args.get("from_date"),
            "until_date": args.get("until_date"),
        })
    elif mode == "watchlist_pending_decisions":
        result = _get(sidecar_url, "/watchlist/pending-decisions", timeout=15)
    elif mode == "watchlist_resolve_pending_decision":
        path = f"/watchlist/pending-decisions/{args.get('provider')}/{args.get('entity_type')}/{args.get('stashdb_id')}/resolve"
        result = _post(sidecar_url, path, {"action": args.get("action")}, timeout=30)
    elif mode == "watchlist_watch_state":
        path = f"/watchlist/watch-state/{args.get('entity_type')}/{args.get('local_id')}"
        result = _get(sidecar_url, path, timeout=30)
    elif mode == "watchlist_set_watch_state":
        path = f"/watchlist/watch-state/{args.get('entity_type')}/{args.get('local_id')}"
        result = _put(sidecar_url, path, {"watched": bool(args.get("watched"))}, timeout=60)
    elif mode == "watchlist_inactive_list":
        result = _get(sidecar_url, "/watchlist/inactive", timeout=30)
    elif mode == "watchlist_inactive_run":
        result = _post(sidecar_url, "/watchlist/inactive/run", {}, timeout=30)
    elif mode == "watchlist_inactive_keep":
        path = f"/watchlist/inactive/{args.get('entity_type')}/{args.get('local_id')}/keep"
        result = _post(sidecar_url, path, {"reasons": args.get("reasons") or []}, timeout=15)
    elif mode == "watchlist_migrate":
        qs = "?dry_run=true" if args.get("dry_run") else ""
        result = _post(sidecar_url, f"/watchlist/migrate-to-tag{qs}", {}, timeout=120)
    elif mode == "watchlist_migrate_status":
        result = _get(sidecar_url, "/watchlist/migrate-to-tag/status", timeout=15)
    elif mode == "releases_list":
        params = {
            "status": args.get("status", "new"),
            "entity_type": args.get("entity_type"),
            "entity_stashdb_id": args.get("entity_stashdb_id"),
            "limit": args.get("limit", 200),
            "offset": args.get("offset", 0),
        }
        qs = "&".join(f"{k}={v}" for k, v in params.items() if v is not None)
        result = _get(sidecar_url, f"/releases?{qs}")
    elif mode == "releases_download_progress":
        # "Activity display" (2026-09-20) -- one batched SABnzbd queue
        # snapshot for every currently-downloading 'sent_to_whisparr'
        # release, see routers/releases.py's download_progress().
        result = _get(sidecar_url, "/releases/download-progress")
    elif mode == "releases_handoff":
        path = (f"/releases/{args.get('provider')}/{args.get('entity_type')}/"
                f"{args.get('entity_stashdb_id')}/{args.get('scene_stashdb_id')}/handoff")
        if args.get("search") is False:
            path += "?search=false"  # "Add" without "Add + search": the scheduled search finds it later
        result = _post(sidecar_url, path, {}, timeout=90)
    elif mode == "releases_fix_mismatch":
        # See routers/releases.py's fix_mismatch()/handoff.py's
        # fix_mismatched_download() -- deletes the wrong file from
        # Whisparr (and disk) and re-adds the movie fresh, so give it
        # a generous timeout like handoff() below (this does a real
        # DELETE + re-add round trip against Whisparr, not just a
        # quick status flip).
        path = (f"/releases/{args.get('provider')}/{args.get('entity_type')}/"
                f"{args.get('entity_stashdb_id')}/{args.get('scene_stashdb_id')}/fix-mismatch")
        result = _post(sidecar_url, path, {}, timeout=60)
    elif mode == "releases_fix_wrong_download":
        # See routers/releases.py's fix_wrong_download()/file_importer.py's
        # fix_wrong_download() -- for an 'import_failed' release with
        # import_error_code 'fingerprint_mismatch' (this sidecar's own
        # pipeline imported the wrong file). Same generous timeout class
        # as releases_fix_mismatch above (deletes a Stash scene, moves a
        # file, may also hit Whisparr's own blocklist endpoint).
        path = (f"/releases/{args.get('provider')}/{args.get('entity_type')}/"
                f"{args.get('entity_stashdb_id')}/{args.get('scene_stashdb_id')}/fix-wrong-download")
        result = _post(sidecar_url, path, {}, timeout=60)
    elif mode == "muted_studios_list":
        result = _get(sidecar_url, "/muted-studios", timeout=30)
    elif mode == "muted_studios_children":
        result = _get(sidecar_url, f"/muted-studios/children?studio_id={quote(str(args.get('studio_id')))}", timeout=60)
    elif mode == "muted_studios_mute":
        result = _post(sidecar_url, "/muted-studios",
                       {k: args.get(k) for k in ("studio_id", "name", "provider", "parent_id", "scene_id")}, timeout=60)
    elif mode == "muted_studios_unmute":
        result = _post(sidecar_url, "/muted-studios/unmute", {"studio_id": args.get("studio_id")}, timeout=30)
    elif mode == "releases_dismiss":
        # Scene-scoped, not entity-scoped -- see routers/releases.py's
        # dismiss() docstring.
        path = f"/releases/{args.get('provider')}/{args.get('scene_stashdb_id')}/dismiss"
        result = _post(sidecar_url, path, {})
    elif mode == "releases_restore":
        # Undo of any release Dismiss: exact previous statuses (v0.104.0), see routers/releases.py's restore_dismissed().
        result = _post(sidecar_url, "/releases/restore", {"rows": args.get("rows") or []}, timeout=30)
    elif mode == "watchlist_reopen_pending_decision":
        path = f"/watchlist/pending-decisions/{args.get('provider')}/{args.get('entity_type')}/{args.get('stashdb_id')}/reopen"
        result = _post(sidecar_url, path, {"name": args.get("name") or "", "open_release_count": args.get("open_release_count") or 0,
                                           "rows": args.get("rows") or []}, timeout=30)
    elif mode == "releases_confirm_file":
        result = _post(sidecar_url, f"/releases/{args.get('provider')}/{args.get('scene_stashdb_id')}/confirm-file",
                       {"submit": bool(args.get("submit"))}, timeout=180)
    elif mode == "releases_recheck_fingerprints":
        result = _post(sidecar_url, f"/releases/{args.get('provider')}/{args.get('scene_stashdb_id')}/recheck-fingerprints", {}, timeout=180)
    elif mode == "films_list":
        keys = ("statuses", "q", "page", "per_page", "order")
        qs = "&".join(f"{k}={quote(str(args[k]))}" for k in keys if args.get(k) not in (None, ""))
        result = _get(sidecar_url, f"/films?{qs}", timeout=30)
    elif mode == "films_search_tpdb":
        result = _get(sidecar_url, f"/films/search-tpdb?q={quote(str(args.get('q') or ''))}" + (f"&limit={int(args['limit'])}" if args.get("limit") else ""), timeout=40)
    elif mode == "films_add":
        result = _post(sidecar_url, "/films/add", {k: args.get(k) for k in ("movie", "title", "year") if args.get(k) is not None}, timeout=30)
    elif mode == "films_set_status":
        result = _post(sidecar_url, f"/films/{args.get('id')}/status", {"status": args.get("status")}, timeout=15)
    elif mode == "films_dismiss_delete_group":
        result = _post(sidecar_url, f"/films/{args.get('id')}/dismiss-delete-group",
                       {"dry_run": bool(args.get("dry_run", True)), "force": bool(args.get("force"))}, timeout=120)
    elif mode == "films_link":
        result = _post(sidecar_url, f"/films/{args.get('id')}/link", {"movie": args.get("movie")}, timeout=30)
    elif mode == "films_grab":
        result = _post(sidecar_url, f"/films/{args.get('id')}/grab", {"candidate": args.get("candidate") or {}, "as_scene": bool(args.get("as_scene"))}, timeout=120)
    elif mode == "films_check_stashdb":
        result = _post(sidecar_url, f"/films/{args.get('id')}/check-stashdb", {}, timeout=90)
    elif mode == "films_use_scenes":
        result = _post(sidecar_url, f"/films/{args.get('id')}/use-scenes", {}, timeout=60)
    elif mode == "films_group_info":
        result = _get(sidecar_url, f"/films/group/{args.get('group_id')}", timeout=60)
    elif mode == "films_group_series":
        result = _get(sidecar_url, f"/films/group/{args.get('group_id')}/series", timeout=90)
    elif mode == "films_group_series_add":
        result = _post(sidecar_url, f"/films/group/{args.get('group_id')}/series-add", {"tpdb_ids": args.get("tpdb_ids") or []}, timeout=60)
    elif mode == "films_remove_new":
        result = _post(sidecar_url, "/films/remove-new", {"ids": args.get("ids") or []}, timeout=30)
    elif mode == "films_group_want":
        result = _post(sidecar_url, f"/films/group/{args.get('group_id')}/want", {}, timeout=60)
    elif mode == "films_group_use_scenes":
        result = _post(sidecar_url, f"/films/group/{args.get('group_id')}/use-scenes", {}, timeout=60)
    elif mode == "films_groups":
        result = _get(sidecar_url, "/films/groups", timeout=120)
    elif mode == "films_group_check":
        result = _post(sidecar_url, f"/films/groups/{args.get('group_id')}/check-stashdb", {}, timeout=120)
    elif mode == "films_group_name_preview":
        result = _get(sidecar_url, f"/films/groups/{args.get('group_id')}/name-preview", timeout=120)
    elif mode == "films_group_name_apply":
        result = _post(sidecar_url, f"/films/groups/{args.get('group_id')}/name-apply", {"items": args.get("items") or []}, timeout=180)
    elif mode == "films_group_assign":
        result = _post(sidecar_url, f"/films/groups/{args.get('group_id')}/assign", {"items": args.get("items") or []}, timeout=300)
    elif mode == "films_group_assign_undo":
        result = _post(sidecar_url, "/films/groups/assign-undo", {"applied": args.get("applied") or []}, timeout=300)
    elif mode == "films_group_name_undo":
        result = _post(sidecar_url, "/films/groups/name-undo", {"applied": args.get("applied") or []}, timeout=180)
    elif mode == "films_imported":
        result = _get(sidecar_url, "/films/imported", timeout=30)
    elif mode == "releases_local_scenes":
        result = _post(sidecar_url, "/releases/local-scenes", {"scenes": args.get("scenes") or []}, timeout=60)
    elif mode == "releases_retry_encode":
        result = _post(sidecar_url, f"/releases/{args.get('provider')}/{args.get('scene_stashdb_id')}/retry-encode", {}, timeout=30)
    elif mode == "films_stuck_preview":
        result = _get(sidecar_url, "/films/stuck", timeout=30)
    elif mode == "films_search_stuck":
        result = _post(sidecar_url, "/films/search-stuck", {}, timeout=30)
    elif mode == "films_search":
        result = _post(sidecar_url, f"/films/{args.get('id')}/search", {}, timeout=120)
    elif mode == "films_review_accept":
        result = _post(sidecar_url, f"/films/{args.get('id')}/review-accept", {"as_scene": bool(args.get("as_scene"))}, timeout=120)
    elif mode == "films_upgrade_keep":
        result = _post(sidecar_url, f"/films/{args.get('id')}/upgrade-keep", {}, timeout=30)
    elif mode == "films_parts_done":
        result = _post(sidecar_url, f"/films/{args.get('id')}/parts-done", {}, timeout=30)
    elif mode == "films_review_reject":
        result = _post(sidecar_url, f"/films/{args.get('id')}/review-reject", {}, timeout=30)
    elif mode == "films_review_restore":
        result = _post(sidecar_url, f"/films/{args.get('id')}/review-restore", {"candidate": args.get("candidate") or {}}, timeout=30)
    elif mode == "films_activity":
        result = _get(sidecar_url, "/films/activity", timeout=30)
    elif mode == "films_group_scene_names":
        src = args.get("source") or ""
        result = _get(sidecar_url, f"/films/groups/{args.get('group_id')}/scene-names" + (f"?source={src}" if src in ("adultempire", "iafd") else ""), timeout=90)
    elif mode == "films_group_meta_preview":
        result = _get(sidecar_url, f"/films/groups/{args.get('group_id')}/metadata-preview", timeout=120)
    elif mode == "films_group_meta_apply":
        result = _post(sidecar_url, f"/films/groups/{args.get('group_id')}/metadata-apply", {"keys": args.get("keys") or []}, timeout=180)
    elif mode == "films_group_meta_undo":
        result = _post(sidecar_url, "/films/groups/metadata-undo", {"applied": args.get("applied") or {}}, timeout=180)
    elif mode == "films_finalize":
        result = _post(sidecar_url, f"/films/{args.get('id')}/finalize", {"force": bool(args.get("force"))}, timeout=300)
    elif mode == "films_discard":
        result = _post(sidecar_url, f"/films/{args.get('id')}/discard", {"dismiss": bool(args.get("dismiss"))}, timeout=300)
    elif mode == "films_use_candidate":
        result = _post(sidecar_url, f"/films/{args.get('id')}/use-candidate", {"tpdb_id": args.get("tpdb_id")}, timeout=30)
    elif mode == "films_bulk_status":
        result = _post(sidecar_url, "/films/bulk-status", {"ids": args.get("ids") or [], "status": args.get("status"), "from_status": args.get("from_status") or ""}, timeout=60)
    elif mode == "films_feed_run":
        result = _post(sidecar_url, "/films/feed/run", {}, timeout=150)
    elif mode == "films_whisparr_sync":
        result = _post(sidecar_url, "/films/whisparr/sync", {"dry_run": bool(args.get("dry_run", True))}, timeout=30)
    elif mode == "films_whisparr_status":
        result = _get(sidecar_url, "/films/whisparr/status", timeout=15)
    elif mode == "films_whisparr_files_sync":
        result = _post(sidecar_url, "/films/whisparr/files/sync", {"dry_run": bool(args.get("dry_run", True))}, timeout=30)
    elif mode == "films_whisparr_files_status":
        result = _get(sidecar_url, "/films/whisparr/files/status", timeout=15)
    elif mode == "films_library_check":
        result = _get(sidecar_url, "/films/library", timeout=150)
    elif mode == "films_library_apply":
        result = _post(sidecar_url, "/films/library/apply", {"film_ids": args.get("film_ids"), "force": bool(args.get("force", False))}, timeout=150)
    elif mode == "films_library_keep":
        result = _post(sidecar_url, f"/films/{int(args.get('id'))}/library/keep", {}, timeout=15)
    elif mode == "films_whisparr_history_check":
        result = _post(sidecar_url, "/films/whisparr/history/check", {"dry_run": bool(args.get("dry_run", True))}, timeout=30)
    elif mode == "films_whisparr_history_status":
        result = _get(sidecar_url, "/films/whisparr/history/status", timeout=15)
    elif mode == "films_rematch":
        result = _post(sidecar_url, "/films/rematch", {}, timeout=30)
    elif mode == "films_seed_match":
        result = _post(sidecar_url, "/films/seed-match", {"source": args.get("source") or "deleted-groups"}, timeout=30)
    elif mode == "films_seed_match_status":
        result = _get(sidecar_url, "/films/seed-match/status", timeout=15)
    elif mode == "releases_imported_file":
        result = _get(sidecar_url, f"/releases/{args.get('provider')}/{args.get('scene_stashdb_id')}/imported-file", timeout=30)
    elif mode == "releases_undismiss":
        path = f"/releases/{args.get('provider')}/{args.get('scene_stashdb_id')}/undismiss"
        result = _post(sidecar_url, path, {})
    elif mode == "releases_retry_failed":
        path = f"/releases/{args.get('provider')}/{args.get('scene_stashdb_id')}/retry-failed"
        result = _post(sidecar_url, path, {}, timeout=30)
    elif mode == "history_list":
        # History/Events log (2026-09-28, v0.79.0), see api/routers/history.py.
        keys = ("q", "levels", "kinds", "categories", "provider", "scene_id", "date_from", "date_until",
                "page", "per_page")
        params = {k: (",".join(args[k]) if isinstance(args[k], list) else args[k]) for k in keys if args.get(k)}
        result = _get(sidecar_url, f"/history?{urlencode(params)}", timeout=30)
    elif mode == "history_add":
        result = _post(sidecar_url, "/history", {k: args.get(k) for k in ("level", "title", "message", "details")})
    elif mode == "releases_query":
        # Server-side filtered/paged listing (2026-09-26), see
        # routers/releases.py's query().
        keys = ("statuses", "q", "search_description", "search_tags", "entity_type", "vr", "providers",
                "date_from", "date_until", "page", "per_page", "ids_only", "entities", "late_addition", "order", "upgrades")
        result = _post(sidecar_url, "/releases/query", {k: args[k] for k in keys if k in args}, timeout=60)
    elif mode == "releases_bulk_status":
        # Calendar/List "Dismiss N shown" + its Undo, see
        # routers/releases.py's bulk_status().
        result = _post(sidecar_url, "/releases/bulk-status",
                       {"action": args.get("action"), "scenes": args.get("scenes") or []})
    elif mode == "releases_library_status":
        # One batched Stash query for the whole list, plus one or two
        # small, fixed-size Whisparr quality-profile lookups (not a
        # per-scene Whisparr call -- see routers/releases.py's
        # library_status() docstring for why that was dropped).
        # `scenes` is [{scene_stashdb_id, is_vr}, ...].
        result = _post(sidecar_url, "/releases/library-status",
                        {"scenes": args.get("scenes") or []}, timeout=60)
    elif mode == "scan_run":
        result = _post(sidecar_url, "/scan/run", {}, timeout=600)
    elif mode == "scan_status":
        result = _get(sidecar_url, "/scan/status")
    elif mode == "stashdb_favorites_sync":
        # Can legitimately take 10+ minutes on a large favorites list --
        # Stash's own bulkPerformerUpdate mutation is slow (~0.75s per
        # entity, confirmed live 2026-09-11). A long timeout here just
        # avoids a premature "connection failed" while it's still
        # genuinely working; the sidecar keeps running the import
        # server-side regardless of whether this call gives up waiting.
        result = _post(sidecar_url, "/stashdb-favorites/sync", {}, timeout=900)
    elif mode == "stashdb_favorites_status":
        result = _get(sidecar_url, "/stashdb-favorites/status")
    elif mode == "whisparr_blocklist_sync":
        # Reads Whisparr's own /blocklist (up to 5000 entries in one
        # call, see whisparr_client.py's get_blocklist()) then resolves
        # each entry's movieId -> StashDB scene id -- generous timeout
        # for the same reason as stashdb_favorites_sync above.
        result = _post(sidecar_url, "/whisparr-blocklist-sync/sync", {}, timeout=300)
    elif mode == "theporndb_identify_sync":
        # Can take a while on a large favorites list -- each unmatched
        # entity is a real ThePornDB search, and Stash's own batch job
        # runs them one at a time (see PROGRESS.md's "ThePornDB
        # identify sync" section). Long timeout for the same reason as
        # stashdb_favorites_sync above.
        result = _post(sidecar_url, "/theporndb-identify/sync", {}, timeout=900)
    elif mode == "theporndb_identify_status":
        result = _get(sidecar_url, "/theporndb-identify/status")
    elif mode == "import_check_run":
        result = _post(sidecar_url, "/import-check/run", {}, timeout=120)
    elif mode == "import_check_status":
        result = _get(sidecar_url, "/import-check/status")
    elif mode == "settings_get_all":
        result = _get(sidecar_url, "/settings")
    elif mode == "settings_get_effective_connections":
        result = _get(sidecar_url, "/settings/connections/effective")
    elif mode == "settings_search_backend":
        result = _get(sidecar_url, "/settings/search-backend")
    elif mode == "settings_naming_preview":
        # An example path for the given (maybe not yet saved) formats / rules, see routers/settings.py's naming_preview().
        result = _post(sidecar_url, "/settings/naming-preview", {k: args.get(k) for k in ("folder_format", "filename_format", "movie_folder_format", "movie_filename_format", "name_replacements", "tag_folders")
                                                                  if args.get(k) is not None}, timeout=15)
    elif mode == "settings_set":
        result = _put(sidecar_url, f"/settings/{args.get('key')}", {"value": args.get("value")})
    elif mode == "connections_check":
        # Each provider gets its own real request (Stash/StashDB/
        # Whisparr GraphQL or REST calls, ThePornDB's stash-box GraphQL)
        # -- can legitimately take a few seconds total, not just one.
        qs = f"?only={quote(args['only'])}" if args.get("only") else ""
        result = _get(sidecar_url, f"/connections/check{qs}", timeout=30)
    elif mode == "whisparr_quality_profiles":
        result = _get(sidecar_url, "/connections/whisparr/quality-profiles", timeout=15)
    elif mode == "sabnzbd_history":
        qs = f"?search={quote(args['search'])}" if args.get("search") else ""
        result = _get(sidecar_url, f"/connections/sabnzbd/history{qs}", timeout=15)
    elif mode == "sabnzbd_queue":
        result = _get(sidecar_url, "/connections/sabnzbd/queue", timeout=15)
    elif mode == "backlog_years":
        # Cache-first server-side (see routers/backlog.py's years()) --
        # only actually hits StashDB (~12 lightweight count-only
        # requests, see stashdb_client.py's count_scenes_by_year()) when
        # nothing's cached yet or `refresh` is explicitly set. Generous
        # timeout regardless, since a live check is still real network I/O.
        path = f"/backlog/{args.get('provider')}/{args.get('entity_type')}/{args.get('entity_stashdb_id')}/years"
        if args.get("refresh"):
            path += "?refresh=true"
        result = _get(sidecar_url, path, timeout=60)
    elif mode == "backlog_load_catalog":
        # starts a background load of the whole catalog (inside the date window) -- returns at once
        path = f"/backlog/{args.get('provider')}/{args.get('entity_type')}/{args.get('entity_stashdb_id')}/load-catalog"
        result = _post(sidecar_url, path, {"name": args.get("name") or ""}, timeout=60)
    elif mode == "backlog_fetch_year":
        # A single year's worth of scenes for one entity -- bounded,
        # but a genuinely prolific year can still mean several pages;
        # generous timeout, same reasoning as the sync jobs above.
        path = (f"/backlog/{args.get('provider')}/{args.get('entity_type')}/"
                f"{args.get('entity_stashdb_id')}/fetch-year/{args.get('year')}")
        result = _post(sidecar_url, path, {}, timeout=300)
    elif mode == "backlog_set_window":
        # Per-entity date window (2026-09-26), see routers/backlog.py's set_window().
        path = f"/backlog/{args.get('provider')}/{args.get('entity_type')}/{args.get('entity_stashdb_id')}/window"
        result = _put(sidecar_url, path, {"from_date": args.get("from_date") or None,
                                          "until_date": args.get("until_date") or None}, timeout=30)
    elif mode == "backlog_dismiss_outside_window":
        path = (f"/backlog/{args.get('provider')}/{args.get('entity_type')}/"
                f"{args.get('entity_stashdb_id')}/dismiss-outside-window")
        result = _post(sidecar_url, path, {}, timeout=30)
    elif mode == "backlog_pairings":
        # Reads the cast genders of this entity's loaded scenes (<= 200 per call), see routers/backlog.py's pairings().
        path = f"/backlog/{args.get('provider')}/{args.get('entity_type')}/{args.get('entity_stashdb_id')}/pairings"
        result = _post(sidecar_url, path, {}, timeout=120)
    elif mode == "backlog_completeness":
        # A handful of cheap StashDB/Stash/Whisparr calls (year total,
        # one VR-ratio count, one quality-profile lookup, one local
        # resolution count) -- see routers/backlog.py's completeness().
        path = (f"/backlog/{args.get('provider')}/{args.get('entity_type')}/"
                f"{args.get('entity_stashdb_id')}/completeness?local_id={args.get('local_id')}")
        result = _get(sidecar_url, path, timeout=60)
    elif mode == "maintenance_library_paths":
        result = _get(sidecar_url, "/maintenance/library-paths", timeout=15)
    elif mode == "maintenance_directory_browse":
        # Directory names routinely contain spaces (e.g. "Lord Of
        # Asses 1") -- quote() so those don't break the query string,
        # unlike every other id/enum-shaped param elsewhere in this
        # file that never needed it.
        qs = f"?path={quote(args['path'])}" if args.get("path") else ""
        result = _get(sidecar_url, f"/maintenance/directory-browse{qs}", timeout=15)
    elif mode == "maintenance_check_run":
        # Returns almost immediately (just kicks off a background
        # thread server-side, see routers/maintenance.py's run_now())
        # -- a full library check itself can take several minutes, but
        # this call doesn't wait for it, the frontend polls
        # maintenance_check_status instead.
        result = _post(sidecar_url, "/maintenance/library-check/run", {"paths": args.get("paths") or [], "provider": args.get("provider") or "stashdb"}, timeout=30)
    elif mode == "maintenance_check_providers":
        result = _get(sidecar_url, "/maintenance/library-check/providers", timeout=30)
    elif mode == "maintenance_check_status":
        result = _get(sidecar_url, "/maintenance/library-check/status", timeout=15)
    elif mode == "maintenance_check_findings":
        params = {"status": args.get("status", "pending"), "kind": args.get("kind")}
        qs = "&".join(f"{k}={v}" for k, v in params.items() if v is not None)
        result = _get(sidecar_url, f"/maintenance/library-check/findings?{qs}", timeout=30)
    elif mode == "maintenance_check_apply":
        # A real scrape + sceneUpdate round trip against Stash, same
        # generous timeout class as releases_fix_mismatch above.
        result = _post(sidecar_url, f"/maintenance/library-check/findings/{args.get('finding_id')}/apply", {}, timeout=60)
    elif mode == "vr_check_run":
        qs = f"?mode={args.get('mode', 'quick')}" + ("&force=true" if args.get("force") else "")
        result = _post(sidecar_url, f"/maintenance/vr-check/run{qs}", {}, timeout=30)
    elif mode == "upgrades_duration_sync":
        result = _post(sidecar_url, "/maintenance/upgrades/duration/sync",
                       {k: args.get(k) for k in ("threshold", "run_check", "force") if args.get(k) is not None}, timeout=30)
    elif mode == "upgrades_duration_status":
        result = _get(sidecar_url, "/maintenance/upgrades/duration/status", timeout=15)
    elif mode == "duration_check_run":
        result = _post(sidecar_url, "/maintenance/duration-check/run" + ("?force=true" if args.get("force") else ""), {}, timeout=30)
    elif mode == "duration_check_status":
        result = _get(sidecar_url, "/maintenance/duration-check/status", timeout=15)
    elif mode == "duration_check_findings":
        result = _get(sidecar_url, f"/maintenance/duration-check/findings?threshold={float(args.get('threshold') or 5)}"
                                   f"&direction={quote(str(args.get('direction') or ''))}&ignored={'true' if args.get('ignored') else 'false'}&groups={'only' if args.get('groups') == 'only' else 'exclude'}", timeout=60)
    elif mode == "duration_check_ignore":
        result = _post(sidecar_url, f"/maintenance/duration-check/{quote(str(args.get('scene_id')), safe='')}/ignore"
                                    + ("?restore=true" if args.get("restore") else ""), {}, timeout=15)
    elif mode == "duration_check_readd":
        # Creates the upgrade candidate and, with search, runs one real indexer search (the sidecar answers after 20 s at the latest).
        result = _post(sidecar_url, f"/maintenance/duration-check/{quote(str(args.get('scene_id')), safe='')}/readd"
                                    + ("" if args.get("search", True) else "?search=false"), {}, timeout=90)
    elif mode == "search_readd_scene":
        result = _post(sidecar_url, f"/search/{args.get('provider')}/scenes/readd" + ("" if args.get("search", True) else "?search=false"),
                       args.get("scene") or {}, timeout=90)
    elif mode == "vr_check_status":
        result = _get(sidecar_url, "/maintenance/vr-check/status", timeout=15)
    elif mode == "vr_check_findings":
        result = _get(sidecar_url, f"/maintenance/vr-check/findings?status={args.get('status', 'pending')}", timeout=30)
    elif mode == "maintenance_counts":
        result = _get(sidecar_url, "/maintenance/compare/counts", timeout=30)
    elif mode == "maintenance_counts_slow":
        result = _get(sidecar_url, "/maintenance/slow-counts", timeout=120)
    elif mode == "compare_pairs":
        # Maintenance > Compare scenes (2026-09-30), see api/scene_compare.py.
        result = _get(sidecar_url, f"/maintenance/compare/pairs?status={quote(str(args.get('status') or ''))}", timeout=120)
    elif mode == "compare_sense":
        # Is the separate Stash-Sense 2 plugin present? (Maintenance shows the Review area only then)
        result = _get(sidecar_url, "/maintenance/compare/sense", timeout=15)
    elif mode == "compare_add":
        result = _post(sidecar_url, "/maintenance/compare/pairs",
                       {"a": args.get("a"), "b": args.get("b"), "label": args.get("label")}, timeout=30)
    elif mode == "compare_pair":
        result = _get(sidecar_url, f"/maintenance/compare/pair/{args.get('id')}", timeout=120)
    elif mode == "compare_status":
        result = _post(sidecar_url, f"/maintenance/compare/pair/{args.get('id')}/status",
                       {"status": args.get("status"), "note": args.get("note")}, timeout=30)
    elif mode == "compare_identify":
        result = _post(sidecar_url, f"/maintenance/compare/pair/{args.get('id')}/identify", {"scene_id": args.get("scene_id")}, timeout=180)
    elif mode == "compare_search":
        result = _post(sidecar_url, f"/maintenance/compare/pair/{args.get('id')}/search",
                       {"scene_id": args.get("scene_id"), "query": args.get("query")}, timeout=180)
    elif mode == "compare_adopt":
        result = _post(sidecar_url, f"/maintenance/compare/pair/{args.get('id')}/adopt",
                       {k: args.get(k) for k in ("scene_id", "provider", "provider_id")}, timeout=180)
    elif mode == "compare_reset_metadata":
        result = _post(sidecar_url, f"/maintenance/compare/pair/{args.get('id')}/reset-metadata", {"scene_id": args.get("scene_id")}, timeout=60)
    elif mode == "compare_frames":
        result = _get(sidecar_url, f"/maintenance/compare/scene/{args.get('scene_id')}/frames", timeout=600)
    elif mode == "compare_close_resolved":
        result = _post(sidecar_url, "/maintenance/compare/close-resolved", {"dry_run": args.get("dry_run", True) not in (False, "false", "False", 0)}, timeout=300)
    elif mode == "compare_scrapers":
        result = _get(sidecar_url, "/maintenance/compare/scrapers", timeout=60)
    elif mode == "compare_scrape":
        result = _post(sidecar_url, "/maintenance/compare/scrape", {"scraper_id": args.get("scraper_id"), "query": args.get("query")}, timeout=180)
    elif mode == "compare_apply_scrape":
        result = _post(sidecar_url, f"/maintenance/compare/pair/{args.get('id')}/apply-scrape",
                       {k: args.get(k) for k in ("scene_id", "title", "date", "details", "url")}, timeout=60)
    elif mode == "compare_review_list":
        result = _get(sidecar_url, "/maintenance/compare/review", timeout=120)
    elif mode == "compare_review_scene":
        result = _get(sidecar_url, f"/maintenance/compare/review/{quote(str(args.get('scene_id')))}", timeout=120)
    elif mode == "compare_review_status":
        result = _post(sidecar_url, f"/maintenance/compare/review/{quote(str(args.get('scene_id')))}/status",
                       {"status": args.get("status"), "note": args.get("note")}, timeout=30)
    elif mode == "compare_review_unmatched":
        result = _post(sidecar_url, f"/maintenance/compare/review/{quote(str(args.get('scene_id')))}/move-unmatched", {}, timeout=900)
    elif mode == "compare_movie_info":
        result = _get(sidecar_url, f"/maintenance/compare/review/{quote(str(args.get('scene_id')))}/movie", timeout=60)
    elif mode == "compare_movie_search":
        result = _post(sidecar_url, f"/maintenance/compare/review/{quote(str(args.get('scene_id')))}/movie-search", {"q": args.get("q")}, timeout=90)
    elif mode == "compare_movie_create":
        result = _post(sidecar_url, f"/maintenance/compare/review/{quote(str(args.get('scene_id')))}/movie-create", {"tpdb_id": args.get("tpdb_id")}, timeout=300)
    elif mode == "compare_movie_create_manual":
        result = _post(sidecar_url, f"/maintenance/compare/review/{quote(str(args.get('scene_id')))}/movie-create-manual", {"name": args.get("name")}, timeout=120)
    elif mode == "compare_group_studio":
        result = _post(sidecar_url, f"/maintenance/compare/review/{quote(str(args.get('scene_id')))}/group-studio",
                       {"name": args.get("name"), "clear": bool(args.get("clear"))}, timeout=60)
    elif mode == "compare_group_add":
        result = _post(sidecar_url, f"/maintenance/compare/review/{quote(str(args.get('scene_id')))}/group-add", {"group_id": args.get("group_id")}, timeout=120)
    elif mode == "compare_review_done":
        result = _post(sidecar_url, f"/maintenance/compare/review/{quote(str(args.get('scene_id')))}/done", {}, timeout=60)
    elif mode == "compare_faces":
        result = _post(sidecar_url, "/maintenance/compare/faces", {"image_base64": args.get("image_base64")}, timeout=150)
    elif mode == "compare_add_performer":
        result = _post(sidecar_url, f"/maintenance/compare/pair/{args.get('id')}/add-performer",
                       {"scene_id": args.get("scene_id"), "performer_id": args.get("performer_id")}, timeout=60)
    elif mode == "compare_aliases":
        result = _get(sidecar_url, "/maintenance/compare/aliases", timeout=60)
    elif mode == "compare_alias_add":
        result = _post(sidecar_url, "/maintenance/compare/aliases",
                       {"alias_scene_id": args.get("alias_scene_id"), "local_scene_id": args.get("local_scene_id"), "note": args.get("note")}, timeout=60)
    elif mode == "compare_alias_remove":
        result = _delete(sidecar_url, f"/maintenance/compare/aliases/{args.get('row_id')}", timeout=30)
    elif mode == "compare_alias_resolve_preview":
        result = _get(sidecar_url, f"/maintenance/compare/aliases/{args.get('row_id')}/resolve-preview", timeout=60)
    elif mode == "compare_alias_resolve":
        result = _post(sidecar_url, f"/maintenance/compare/aliases/{args.get('row_id')}/resolve", {}, timeout=180)
    elif mode == "compare_alias_resolve_undo":
        result = _post(sidecar_url, "/maintenance/compare/aliases/resolve-undo", {"applied": args.get("applied") or [], "row": args.get("row") or {}}, timeout=180)
    elif mode == "compare_remove_id":
        result = _post(sidecar_url, f"/maintenance/compare/pair/{args.get('id')}/remove-id",
                       {"scene_id": args.get("scene_id"), "stash_id": args.get("stash_id")}, timeout=60)
    elif mode == "organize_shared":
        result = _get(sidecar_url, f"/maintenance/organize/shared?status={args.get('status') or 'open'}", timeout=60)
    elif mode == "organize_shared_keep_safe":
        result = _post(sidecar_url, "/maintenance/organize/shared/keep-safe", {"dry": bool(args.get("dry"))}, timeout=300)
    elif mode == "organize_shared_scan":
        result = _post(sidecar_url, "/maintenance/organize/shared/scan", {}, timeout=30)
    elif mode == "organize_shared_use":
        result = _post(sidecar_url, f"/maintenance/organize/shared/{args.get('scene_id')}/use", {"candidate_id": args.get("candidate_id")}, timeout=120)
    elif mode in ("organize_shared_keep", "organize_shared_dismiss"):
        result = _post(sidecar_url, f"/maintenance/organize/shared/{args.get('scene_id')}/{mode.rsplit('_', 1)[1]}", {}, timeout=30)
    elif mode == "convert_list":
        result = _get(sidecar_url, "/maintenance/convert", timeout=30)
    elif mode == "convert_history":
        result = _get(sidecar_url, f"/maintenance/convert/history?status={args.get('status') or 'done'}", timeout=30)
    elif mode == "convert_refresh":
        result = _post(sidecar_url, "/maintenance/convert/refresh", {}, timeout=300)
    elif mode in ("convert_queue", "convert_cancel", "convert_dismiss"):
        body = {"ids": args.get("ids") or []}
        if mode == "convert_dismiss":
            body["restore"] = bool(args.get("restore"))
        result = _post(sidecar_url, "/maintenance/convert/" + mode.split("_", 1)[1], body, timeout=60)
    elif mode == "upgrades_list":
        result = _get(sidecar_url, "/maintenance/upgrades", timeout=30)
    elif mode == "upgrades_refresh":
        result = _post(sidecar_url, "/maintenance/upgrades/refresh", {}, timeout=300)
    elif mode == "upgrades_bulk":
        # Bulk actions on Upgrade wanted (v0.237.0), see routers/maintenance.py's upgrades_bulk(); a bulk search runs up to 10 real searches.
        result = _post(sidecar_url, "/maintenance/upgrades/bulk",
                       {k: args[k] for k in ("ids", "action", "statuses", "profile_id", "previous") if k in args}, timeout=900)
    elif mode == "upgrades_library_check_start":
        result = _post(sidecar_url, f"/maintenance/upgrades/library-check/start?force={'true' if args.get('force') else 'false'}", {}, timeout=30)
    elif mode == "upgrades_library_check_status":
        result = _get(sidecar_url, "/maintenance/upgrades/library-check/status", timeout=30)
    elif mode == "upgrades_search":
        # Real grab only when dry_run is explicitly false (bool or string).
        qs = "?dry_run=false" if str(args.get("dry_run")).lower() in ("false", "0") else "?dry_run=true"
        result = _post(sidecar_url, f"/maintenance/upgrades/{args.get('id')}/search{qs}", {}, timeout=300)
    elif mode == "releases_reviews":
        # Activity > Needs review (2026-09-28), see routers/releases.py's reviews().
        result = _get(sidecar_url, "/releases/reviews", timeout=30)
    elif mode == "upgrades_review_accept":
        result = _post(sidecar_url, f"/maintenance/upgrades/{args.get('id')}/review/accept", {}, timeout=120)
    elif mode == "upgrades_review_reject":
        result = _post(sidecar_url, f"/maintenance/upgrades/{args.get('id')}/review/reject", {}, timeout=30)
    elif mode == "organize_studios":
        # Maintenance > Organize library (2026-09-28), see api/library_organize.py.
        qs = "?refresh=true" if args.get("refresh") else ""
        result = _get(sidecar_url, f"/maintenance/organize/studios{qs}", timeout=300)
    elif mode == "organize_plan":
        result = _get(sidecar_url, f"/maintenance/organize/plan?studio_id={quote(str(args.get('studio_id')))}"
                                   f"{'&fresh=true' if args.get('fresh') else ''}", timeout=900)
    elif mode == "organize_apply":
        result = _post(sidecar_url, "/maintenance/organize/apply",
                       {k: args.get(k) for k in ("studio_id", "file_ids", "kinds", "ignore_renamers")}, timeout=3600)
    elif mode == "organize_recount_start":
        result = _post(sidecar_url, "/maintenance/organize/studios/recount", {}, timeout=30)
    elif mode == "organize_recount_status":
        result = _get(sidecar_url, "/maintenance/organize/studios/recount-status", timeout=15)
    elif mode == "organize_check_file":
        result = _post(sidecar_url, "/maintenance/organize/check-file", {"scene_id": args.get("scene_id")}, timeout=120)
    elif mode == "organize_movies_plan":
        result = _get(sidecar_url, "/maintenance/organize/movies/plan", timeout=900)
    elif mode == "organize_movies_apply":
        result = _post(sidecar_url, "/maintenance/organize/movies/apply",
                       {k: args.get(k) for k in ("file_ids", "kinds", "ignore_renamers")}, timeout=3600)
    elif mode == "sanity_check":
        # The safety check of the moving/renaming functions, see api/renamer_check.py.
        result = _get(sidecar_url, "/maintenance/sanity" + ("?renamers=true" if args.get("renamers") else ""), timeout=60)
    elif mode == "import_sources":
        # Maintenance > Import folder (2026-09-29), see api/library_import.py.
        result = _get(sidecar_url, "/maintenance/import/sources", timeout=120)
    elif mode == "import_analyse":
        result = _get(sidecar_url, f"/maintenance/import/analyse?root={quote(str(args.get('root')))}", timeout=1800)
    elif mode == "import_apply":
        result = _post(sidecar_url, "/maintenance/import/apply",
                       {k: args.get(k) for k in ("root", "scene_ids", "picks", "ignore_renamers", "trust")}, timeout=3600)
    elif mode == "import_add_check":
        result = _get(sidecar_url, f"/maintenance/import/add-check?path={quote(str(args.get('path')))}", timeout=300)
    elif mode == "import_add":
        result = _post(sidecar_url, "/maintenance/import/add", {"path": args.get("path"), "confirm": True}, timeout=600)
    elif mode == "import_remove":
        result = _post(sidecar_url, "/maintenance/import/remove", {"root": args.get("root")}, timeout=60)
    elif mode == "import_finish_check":
        result = _get(sidecar_url, f"/maintenance/import/finish-check?root={quote(str(args.get('root')))}", timeout=300)
    elif mode == "import_finish":
        result = _post(sidecar_url, "/maintenance/import/finish", {"root": args.get("root"), "confirm": True}, timeout=600)
    elif mode == "movies_no_group":
        result = _get(sidecar_url, "/maintenance/movies/no-group" + ("?refresh=true" if args.get("refresh") else ""), timeout=300)
    elif mode == "movies_tpdb_search":
        result = _post(sidecar_url, "/maintenance/movies/tpdb-search", {"q": args.get("q"), "minutes": args.get("minutes")}, timeout=60)
    elif mode == "movies_link_tpdb":
        result = _post(sidecar_url, "/maintenance/movies/link-tpdb", {"group_id": args.get("group_id"), "tpdb_id": args.get("tpdb_id")}, timeout=120)
    elif mode == "movies_create_group":
        result = _post(sidecar_url, "/maintenance/movies/create-group",
                       {k: args.get(k) for k in ("title", "scene_ids", "group_id", "scrape_url")}, timeout=300)
    elif mode == "movies_compare_groups":
        result = _get(sidecar_url, "/maintenance/movies/compare-groups" + ("?refresh=true" if args.get("refresh") else ""), timeout=300)
    elif mode == "movies_compare_groups_ignore":
        result = _post(sidecar_url, "/maintenance/movies/compare-groups/ignore",
                       {"folder_key": args.get("folder_key"), "group_id": args.get("group_id"), "undo": bool(args.get("undo"))}, timeout=30)
    elif mode == "movies_group_sources":
        result = _get(sidecar_url, "/maintenance/movies/group-sources", timeout=60)
    elif mode == "movies_group_search":
        result = _get(sidecar_url, f"/maintenance/movies/group-search?source={quote(args.get('source', ''))}&q={quote(args.get('q', ''))}", timeout=60)
    elif mode == "movies_performers_empty":
        result = _get(sidecar_url, "/maintenance/movies/performers-empty", timeout=300)
    elif mode == "movies_performer_unwatch":
        result = _post(sidecar_url, f"/maintenance/movies/performers/{quote(str(args.get('performer_id')))}/unwatch", {}, timeout=60)
    elif mode == "movies_performer_delete":
        result = _post(sidecar_url, f"/maintenance/movies/performers/{quote(str(args.get('performer_id')))}/delete", {}, timeout=60)
    elif mode == "movies_group_namesakes":
        result = _get(sidecar_url, "/maintenance/movies/group-namesakes" + ("?refresh=true" if args.get("refresh") else ""), timeout=300)
    elif mode == "movies_group_namesakes_dismiss":
        result = _post(sidecar_url, "/maintenance/movies/group-namesakes/dismiss",
                       {"group_id": args.get("group_id"), "namesake_ids": args.get("namesake_ids") or [], "undo": bool(args.get("undo"))}, timeout=30)
    elif mode == "movies_group_delete":
        result = _post(sidecar_url, f"/maintenance/movies/groups/{quote(str(args.get('group_id')), safe='')}/delete", {}, timeout=60)
    elif mode == "movies_groups_empty":
        result = _get(sidecar_url, "/maintenance/movies/groups-empty" + ("?refresh=true" if args.get("refresh") else ""), timeout=300)
    elif mode == "import_suggest":
        result = _post(sidecar_url, "/maintenance/import/suggest",
                       {k: args.get(k) for k in ("root", "scene_ids")}, timeout=900)
    elif mode == "unknown_quality_list":
        # Maintenance: file names with "Unknown" as quality (2026-09-28).
        result = _get(sidecar_url, "/maintenance/unknown-quality", timeout=120)
    elif mode == "unknown_quality_apply":
        result = _post(sidecar_url, "/maintenance/unknown-quality/apply", {"paths": args.get("paths") or []},
                       timeout=1800)
    elif mode == "upgrades_manual_grab":
        result = _post(sidecar_url, f"/maintenance/upgrades/{args.get('id')}/manual-grab", {"candidate": args.get("candidate") or {}}, timeout=120)
    elif mode == "upgrades_dismiss":
        result = _post(sidecar_url, f"/maintenance/upgrades/{args.get('id')}/dismiss", {}, timeout=15)
    elif mode == "upgrades_restore":
        result = _post(sidecar_url, f"/maintenance/upgrades/{args.get('id')}/restore", {"status": args.get("status")}, timeout=15)
    elif mode == "vr_check_restore":
        result = _post(sidecar_url, f"/maintenance/vr-check/findings/{args.get('finding_id')}/restore", {"status": args.get("status")}, timeout=15)
    elif mode == "maintenance_check_restore":
        result = _post(sidecar_url, f"/maintenance/library-check/findings/{args.get('finding_id')}/restore", {"status": args.get("status")}, timeout=15)
    elif mode == "vr_check_set_verdict":
        result = _post(sidecar_url, f"/maintenance/vr-check/findings/{args.get('finding_id')}/verdict", {"verdict": args.get("verdict")}, timeout=15)
    elif mode == "vr_check_restore_verdict":
        result = _post(sidecar_url, f"/maintenance/vr-check/findings/{args.get('finding_id')}/verdict-restore",
                       {"verdict": args.get("verdict"), "manual": bool(args.get("manual")), "finding_status": args.get("finding_status") or "pending"}, timeout=15)
    elif mode == "compare_unidentified":
        result = _get(sidecar_url, "/maintenance/compare/unidentified" + ("?refresh=true" if args.get("refresh") else ""), timeout=120)
    elif mode == "compare_unidentified_check":
        result = _post(sidecar_url, "/maintenance/compare/unidentified/check", {"scene_ids": args.get("scene_ids") or []}, timeout=300)
    elif mode == "compare_unidentified_apply":
        result = _post(sidecar_url, "/maintenance/compare/unidentified/apply", {"scene_ids": args.get("scene_ids") or []}, timeout=300)
    elif mode == "setup_status":
        result = _get(sidecar_url, "/setup/status", timeout=15)
    elif mode == "sidecar_health":
        result = _get(sidecar_url, "/health", timeout=10)
    elif mode == "transfer_groups":
        result = _get(sidecar_url, "/transfer/groups", timeout=60)
    elif mode == "transfer_export":
        result = _post(sidecar_url, "/transfer/export", {"groups": args.get("groups") or []}, timeout=300)
    elif mode == "transfer_import_check":
        result = _post(sidecar_url, "/transfer/import/check", {"name": args.get("name"), "groups": args.get("groups")}, timeout=300)
    elif mode == "releases_fill_cast":
        result = _post(sidecar_url, "/releases/fill-cast", {"scenes": args.get("scenes") or []}, timeout=120)
    elif mode == "system_restart":
        result = _post(sidecar_url, "/system/restart", {}, timeout=15)
    elif mode == "transfer_import_apply":
        result = _post(sidecar_url, "/transfer/import/apply", {"name": args.get("name"), "groups": args.get("groups")}, timeout=600)
    elif mode == "compare_review_add":
        result = _post(sidecar_url, f"/maintenance/compare/review/{quote(str(args.get('scene_id')))}/add", {}, timeout=60)
    elif mode == "vr_check_frame":
        result = _get(sidecar_url, f"/maintenance/vr-check/findings/{args.get('finding_id')}/frame", timeout=180)
    elif mode == "vr_check_dismiss":
        result = _post(sidecar_url, f"/maintenance/vr-check/findings/{args.get('finding_id')}/dismiss", {}, timeout=15)
    elif mode == "vr_check_fix":
        result = _post(sidecar_url, f"/maintenance/vr-check/findings/{args.get('finding_id')}/fix", {}, timeout=120)
    elif mode == "maintenance_check_recheck":
        result = _post(sidecar_url, f"/maintenance/library-check/findings/{args.get('finding_id')}/recheck", {}, timeout=120)
    elif mode == "maintenance_check_confirm":
        result = _post(sidecar_url, f"/maintenance/library-check/findings/{args.get('finding_id')}/confirm",
                       {"submit": bool(args.get("submit"))}, timeout=180)
    elif mode == "maintenance_check_dismiss":
        result = _post(sidecar_url, f"/maintenance/library-check/findings/{args.get('finding_id')}/dismiss", {}, timeout=15)
    elif mode == "file_import_run":
        # A real move+scan+verify round trip per completed download,
        # not just a status flip -- same generous-timeout reasoning as
        # releases_fix_mismatch/maintenance_check_apply above.
        result = _post(sidecar_url, "/file-import/run", {}, timeout=120)
    elif mode == "file_import_status":
        result = _get(sidecar_url, "/file-import/status", timeout=15)
    elif mode == "file_import_source_status":
        result = _get(sidecar_url, "/file-import/source-status", timeout=15)
    elif mode == "file_import_sync_source":
        result = _post(sidecar_url, "/file-import/sync-source", {}, timeout=15)
    elif mode == "recycle_bin_cleanup_run":
        result = _post(sidecar_url, "/file-import/recycle-bin/cleanup", {}, timeout=60)
    elif mode == "recycle_bin_cleanup_status":
        result = _get(sidecar_url, "/file-import/recycle-bin/status", timeout=15)
    elif mode == "search_performers":
        result = _get(sidecar_url, f"/search/{args.get('provider')}/performers?term={quote(args.get('term', ''))}", timeout=60)
    elif mode == "search_studios":
        result = _get(sidecar_url, f"/search/{args.get('provider')}/studios?term={quote(args.get('term', ''))}", timeout=60)
    elif mode == "search_scenes":
        result = _get(sidecar_url, f"/search/{args.get('provider')}/scenes?term={quote(args.get('term', ''))}", timeout=60)
    elif mode == "search_tags":
        result = _get(sidecar_url, f"/search/{args.get('provider')}/tags?term={quote(args.get('term', ''))}", timeout=60)
    elif mode == "search_scenes_by_tag":
        result = _get(sidecar_url, f"/search/{args.get('provider')}/scenes-by-tag?tag_ids={quote(','.join(str(t) for t in (args.get('tag_ids') or [args.get('tag_id', '')])), safe=',')}&exclude_ids={quote(','.join(str(t) for t in (args.get('exclude_ids') or [])), safe=',')}", timeout=120)
    elif mode == "search_favorite_performer":
        path = f"/search/{args.get('provider')}/performers/{quote(args.get('stashdb_id'), safe='')}/favorite"
        result = _post(sidecar_url, path, {"name": args.get("name", "")}, timeout=20)
    elif mode == "search_favorite_studio":
        path = f"/search/{args.get('provider')}/studios/{quote(args.get('stashdb_id'), safe='')}/favorite"
        result = _post(sidecar_url, path, {"name": args.get("name", "")}, timeout=20)
    elif mode == "search_entity_state":
        # The Search page's cards use the browser userscript's own endpoints (v0.240.20): is it in Stash, is it on the watchlist.
        kind = "studio" if args.get("entity_type") == "studio" else "performer"
        result = _get(sidecar_url, f"/userscript/entity-state?provider={quote(str(args.get('provider')))}&kind={kind}&id={quote(str(args.get('stashdb_id')), safe='')}", timeout=30)
    elif mode == "search_entity_watch":
        kind = "studio" if args.get("entity_type") == "studio" else "performer"
        result = _post(sidecar_url, "/userscript/watch", {"provider": args.get("provider"), "kind": kind, "id": args.get("stashdb_id"),
                                                          "name": args.get("name", ""), "watched": bool(args.get("watched"))}, timeout=60)
    elif mode == "search_handoff_scene":
        # A real Whisparr add-movie + indexer-search round trip, same
        # generous timeout class as releases_handoff.
        result = _post(sidecar_url, f"/search/{args.get('provider')}/scenes/handoff" + ("?search=false" if args.get("search") is False else ""),
                       args.get("scene") or {}, timeout=90)
    elif mode == "releases_whisparr_fallback":
        # Milestone 3 (2026-09-19): manual fallback to Whisparr's own
        # automatic search+grab, same generous timeout class as
        # releases_handoff (a real Whisparr search+grab round trip).
        # Scene-scoped, same reasoning as releases_dismiss above.
        path = f"/releases/{args.get('provider')}/{args.get('scene_stashdb_id')}/whisparr-fallback"
        result = _post(sidecar_url, path, {}, timeout=60)
    elif mode == "encode_run_local":
        result = _post(sidecar_url, "/encode-worker/run-local",
                       {"provider": args.get("provider"), "scene_stashdb_id": args.get("scene_stashdb_id")}, timeout=30)
    elif mode == "releases_review_candidate_accept":
        # RESULT_NEEDS_REVIEW (2026-09-19) -- the user's own explicit
        # "yes, get this one" decision on a candidate that matched every
        # credited performer but not date/title closely enough for an
        # automatic grab. Same generous timeout class as releases_handoff
        # (a real download-client add round trip).
        path = f"/releases/{args.get('provider')}/{args.get('scene_stashdb_id')}/review-candidate/accept"
        result = _post(sidecar_url, path, {}, timeout=60)
    elif mode == "releases_review_candidate_reject":
        path = f"/releases/{args.get('provider')}/{args.get('scene_stashdb_id')}/review-candidate/reject"
        result = _post(sidecar_url, path, {})
    elif mode == "releases_review_candidate_restore":
        path = f"/releases/{args.get('provider')}/{args.get('scene_stashdb_id')}/review-candidate/restore"
        result = _post(sidecar_url, path, {})
    elif mode == "releases_manual_search":
        # Manual search with a user-typed query (2026-09-22) over Prowlarr
        # or (2026-09-25) the direct indexers, picked via `source` -- a
        # real live indexer query; 30s since direct indexers are queried
        # in parallel but each can take up to its own 20s timeout.
        path = f"/releases/{args.get('provider')}/{args.get('scene_stashdb_id')}/manual-search"
        result = _post(sidecar_url, path, {"query": args.get("query", ""), "source": args.get("source")}, timeout=30)
    elif mode == "releases_last_search":
        path = f"/releases/{args.get('provider')}/{args.get('scene_stashdb_id')}/last-search"
        result = _get(sidecar_url, path, timeout=15)
    elif mode == "releases_manual_search_grab":
        # Same generous timeout class as releases_handoff -- a real
        # download-client add round trip.
        path = f"/releases/{args.get('provider')}/{args.get('scene_stashdb_id')}/manual-search/grab"
        result = _post(sidecar_url, path, {"candidate": args.get("candidate") or {}, "replace": bool(args.get("replace"))}, timeout=60)
    elif mode == "releases_last_search_grab":
        # Re-finds a result of the search log by title + indexer + size and grabs it (replacing a running torrent), v0.231.3
        path = f"/releases/{args.get('provider')}/{args.get('scene_stashdb_id')}/last-search/grab"
        c = args.get("candidate") or {}
        result = _post(sidecar_url, path, {"title": c.get("title"), "indexer": c.get("indexer"), "size": c.get("size"), "info_hash": c.get("info_hash")}, timeout=120)
    elif mode == "torrents_health":
        # The torrent monitor's list (stalled / stuck / error / missing torrents), v0.231.0
        result = _get(sidecar_url, "/torrent-health", timeout=15)
    elif mode == "torrents_check_now":
        result = _post(sidecar_url, "/torrent-health/check-now", {}, timeout=15)
    elif mode in ("torrents_switch", "torrents_remove", "torrents_readd", "torrents_keep"):
        # A search for another torrent can take a while (several indexer queries + the grab)
        result = _post(sidecar_url, "/torrent-health/" + mode.split("_", 1)[1],
                       {"client_id": args.get("client_id"), "job_id": args.get("job_id")}, timeout=90 if mode == "torrents_switch" else 30)
    elif mode == "quality_profiles_list":
        result = _get(sidecar_url, "/quality-profiles", timeout=15)
    elif mode == "quality_profiles_create":
        result = _post(sidecar_url, "/quality-profiles", {
            "name": args.get("name"), "resolutions": args.get("resolutions") or [],
            "cutoff_resolution": args.get("cutoff_resolution"),
        }, timeout=15)
    elif mode == "quality_profiles_update":
        result = _put(sidecar_url, f"/quality-profiles/{args.get('id')}", {
            "name": args.get("name"), "resolutions": args.get("resolutions") or [],
            "cutoff_resolution": args.get("cutoff_resolution"),
        }, timeout=15)
    elif mode == "quality_profiles_delete":
        result = _delete(sidecar_url, f"/quality-profiles/{args.get('id')}", timeout=15)
    elif mode == "metadata_providers_registry":
        result = _get(sidecar_url, "/metadata-providers/registry", timeout=15)
    elif mode == "metadata_providers_list":
        result = _get(sidecar_url, "/metadata-providers", timeout=15)
    elif mode == "metadata_providers_create":
        result = _post(sidecar_url, "/metadata-providers", {
            "key": args.get("key"), "name": args.get("name"),
            "url": args.get("url"), "api_key": args.get("api_key") or "",
        }, timeout=15)
    elif mode == "metadata_providers_update":
        result = _put(sidecar_url, f"/metadata-providers/{args.get('key')}", {
            "name": args.get("name"), "url": args.get("url"), "api_key": args.get("api_key"), "color": args.get("color"),
            "query_delay": args.get("query_delay"), "favorites_interval": args.get("favorites_interval"),
        }, timeout=15)
    elif mode == "metadata_providers_delete":
        result = _delete(sidecar_url, f"/metadata-providers/{args.get('key')}", timeout=15)
    elif mode == "download_clients_list":
        result = _get(sidecar_url, "/download-clients", timeout=15)
    elif mode == "indexer_search_stuck_preview":
        result = _get(sidecar_url, "/indexer-search/stuck", timeout=30)
    elif mode == "indexer_search_run_stuck":
        result = _post(sidecar_url, "/indexer-search/run-stuck", {}, timeout=30)
    elif mode == "download_clients_from_prowlarr":
        result = _get(sidecar_url, "/download-clients/from-prowlarr", timeout=30)
    elif mode == "download_clients_create":
        result = _post(sidecar_url, "/download-clients", _download_client_body(args), timeout=15)
    elif mode == "download_clients_update":
        result = _put(sidecar_url, f"/download-clients/{args.get('id')}", _download_client_body(args), timeout=15)
    elif mode == "download_clients_delete":
        result = _delete(sidecar_url, f"/download-clients/{args.get('id')}", timeout=15)
    elif mode == "indexers_list":
        result = _get(sidecar_url, "/indexers", timeout=15)
    elif mode == "indexers_create":
        result = _post(sidecar_url, "/indexers", _indexer_body(args), timeout=15)
    elif mode == "indexers_update":
        result = _put(sidecar_url, f"/indexers/{args.get('id')}", _indexer_body(args), timeout=15)
    elif mode == "indexers_delete":
        result = _delete(sidecar_url, f"/indexers/{args.get('id')}", timeout=15)
    elif mode == "indexers_check":
        result = _post(sidecar_url, f"/indexers/{args.get('id')}/check", {}, timeout=30)
    elif mode == "download_clients_clean_history":
        result = _post(sidecar_url, f"/download-clients/{args.get('id')}/clean-history?dry_run={'false' if args.get('dry_run') is False else 'true'}", {}, timeout=300)
    elif mode == "download_clients_check":
        result = _post(sidecar_url, f"/download-clients/{args.get('id')}/check", {}, timeout=15)
    elif mode == "download_clients_test":
        # The details as typed into the add / edit form (nothing saved).
        result = _post(sidecar_url, "/download-clients/check", {
            "type": args.get("type", "sabnzbd"), "url": args.get("url", ""), "api_key": args.get("api_key", ""),
            "username": args.get("username", ""), "password": args.get("password", ""),
        }, timeout=30)
    elif mode == "indexer_search_run":
        # A real Whisparr /release round trip per pending release, same
        # generous-timeout reasoning as file_import_run.
        result = _post(sidecar_url, "/indexer-search/run", {}, timeout=120)
    elif mode == "indexer_search_status":
        result = _get(sidecar_url, "/indexer-search/status", timeout=15)
    else:
        result = {"error": f"Unknown mode: {mode}"}

    print(json.dumps({"output": result}))


def _handle_hook(input_data, args):
    """Stash-triggered hook call (Performer.Update.Post/Studio.Update.Post
    -- see stash-release-manager.yml's `hooks:` section), 2026-09-21 --
    instant favorite-change detection, step 2 (step 1 was recording the
    raw payload live to confirm its real shape, see PROGRESS.md/
    project_roadmap_todo.md memory for that investigation).

    Confirmed live shape: `hookContext` = {"id": <local numeric id>,
    "type": "Performer.Update.Post"|"Studio.Update.Post", "input": {...
    every possible field, only some non-null...}, "inputFields": [...
    which fields THIS update actually touched...]} -- `inputFields` is
    what makes "favorite genuinely changed" distinguishable from "some
    unrelated field changed and favorite just happens to echo back
    null/its old value", rather than guessing from `input` alone.

    A hook call carries none of the usual runOp() args (no sidecar_url/
    stash_url/stash_api_key) -- only `server_connection` (Stash's own
    connect info + a session cookie), since this is Stash calling the
    script directly, not our own JS. Resolves the real configured
    sidecarUrl via a self-call to Stash's own GraphQL (same
    `configuration { plugins }` query core.js's getSettings() already
    uses) rather than assuming a default that's very likely wrong for
    any real deployment (confirmed live 2026-09-21: this instance's own
    sidecarUrl is a LAN IP, not the "http://localhost:6961" fallback
    every other mode here defaults to)."""
    try:
        hook_context = args.get("hookContext") or {}
        hook_type = hook_context.get("type", "")
        if hook_type == "Scene.Create.Post":
            # Event-driven import verification (2026-09-25) -- see
            # routers/file_import.py's scene_created(). Forwarded
            # unconditionally; the sidecar decides (cheaply) whether this
            # scene is one of its pending imports.
            if hook_context.get("id") is not None:
                sidecar_url = _resolve_sidecar_url(input_data)
                if sidecar_url:
                    _post(sidecar_url, "/file-import/scene-created",
                          {"scene_id": hook_context.get("id")}, timeout=60)
            print(json.dumps({"output": {"ok": True}}))
            return
        if hook_type == "Scene.Update.Post":
            # Only an update that sets `organized` to true goes on; the sidecar re-reads the scene and checks its own setting.
            fields = hook_context.get("inputFields") or []
            if "organized" in fields and (hook_context.get("input") or {}).get("organized") is True and hook_context.get("id") is not None:
                sidecar_url = _resolve_sidecar_url(input_data)
                if sidecar_url:
                    _post(sidecar_url, "/maintenance/import/organized-hook", {"scene_id": hook_context.get("id")}, timeout=600)
            print(json.dumps({"output": {"ok": True}}))
            return
        if hook_type.startswith("Performer."):
            entity_type = "performer"
        elif hook_type.startswith("Studio."):
            entity_type = "studio"
        else:
            entity_type = None
        input_fields = hook_context.get("inputFields") or []
        local_id = hook_context.get("id")
        # 2026-09-26: forwards Favorite AND tag changes -- which one
        # matters depends on the sidecar's watchlist_source setting
        # (Favorites or the watchlist tag), decided there, see
        # routers/watchlist.py's entity_changed(). Any other update
        # (name, image, ...) never leaves this process.
        if entity_type and local_id is not None and ({"favorite", "tag_ids"} & set(input_fields)):
            sidecar_url = _resolve_sidecar_url(input_data)
            if sidecar_url:
                _post(sidecar_url, "/watchlist/entity-changed", {
                    "entity_type": entity_type, "local_id": local_id, "fields": list(input_fields),
                }, timeout=15)
    except Exception:
        pass  # best-effort -- a hook failure shouldn't surface as a Stash task error
    print(json.dumps({"output": {"ok": True}}))


def _stash_graphql(input_data, query):
    """Stash's own GraphQL, called back via the session cookie a plugin call's `server_connection` carries (no API key needed);
    returns the `data` dict, or None when it cannot be reached or answers with errors. `Host` in `server_connection` is Stash's
    own BIND address (a wildcard such as "all interfaces"), not a connect target, so this always targets `localhost`: the exec script runs as a
    subprocess of Stash itself, on the same host/container, same convention other Stash plugins use for their own self-calls."""
    conn = input_data.get("server_connection") or {}
    port = conn.get("Port")
    if not port:
        return None
    scheme = conn.get("Scheme", "http")
    cookie = conn.get("SessionCookie") or {}
    cookies = {cookie["Name"]: cookie["Value"]} if cookie.get("Name") and cookie.get("Value") else {}
    try:
        # verify=False: a loopback call to the Stash process that started this script (its certificate is for its public name).
        resp = requests.post(f"{scheme}://localhost:{port}/graphql", json={"query": query}, cookies=cookies, timeout=10,
                             verify=(scheme != "https"))
        resp.raise_for_status()
        body = resp.json()
        if body.get("errors"):
            return None
        return body.get("data") or None
    except Exception:
        return None


def _resolve_sidecar_url(input_data):
    """The configured sidecarUrl, from this plugin's own settings in Stash (same `configuration { plugins }` query core.js's
    getSettings() uses) -- for hook calls, which carry none of the usual runOp() args."""
    data = _stash_graphql(input_data, "query { configuration { plugins } }")
    if data is None:
        return None
    plugins = (data.get("configuration") or {}).get("plugins") or {}
    sidecar_url = (plugins.get("stash-release-manager") or {}).get("sidecarUrl") or "http://localhost:6961"
    return sidecar_url.rstrip("/")


# Modes that carry a sync from Stash: the page's first calls, so nothing has to be typed in on a new installation.
_SYNC_MODES = {"setup_status", "connections_check", "stash_sync"}


def _discover_from_stash(input_data):
    """{api_key, boxes: [{name, endpoint, api_key}]} as Stash holds them (Settings > Security, Settings > Metadata Providers >
    Stash-box endpoints), or None when Stash cannot be asked. An empty api_key means Stash has none generated."""
    data = _stash_graphql(input_data, "query { configuration { general { apiKey stashBoxes { name endpoint api_key } } } }")
    if data is None:
        return None
    general = (data.get("configuration") or {}).get("general") or {}
    boxes = [{"name": b.get("name") or "", "endpoint": b.get("endpoint") or "", "api_key": b.get("api_key") or ""}
             for b in (general.get("stashBoxes") or []) if isinstance(b, dict)]
    return {"api_key": general.get("apiKey") or "", "boxes": boxes}


def _sync_from_stash(sidecar_url, input_data, args):
    """Hands what Stash knows to the sidecar (POST /stash-boxes/sync) and returns the sidecar's report -- which never contains a
    key, so it can go back to the browser. `add` (endpoints to add as providers) and `overwrite` come from the setup guide's
    buttons; `stash_url_suggested` is the address the browser uses for Stash, taken over only while the sidecar has none.
    Done on every call of a sync mode, not once: the sidecar's background jobs need a persistently stored key, and a key
    regenerated in Stash is picked up the next time the page is open. `_post()` swallows connection errors into an
    {"error": ...} dict, so a hiccup here cannot fail the requested mode."""
    found = _discover_from_stash(input_data)
    body = {
        "stash_api_key": found["api_key"] if found else "",
        "boxes": found["boxes"] if found else [],
        "stash_url_suggested": args.get("stash_url_suggested") or "",
        "add": args.get("add") or [],
        "overwrite": bool(args.get("overwrite")),
    }
    report = _post(sidecar_url, "/stash-boxes/sync", body, timeout=15)
    if isinstance(report, dict) and "error" not in report:
        report["discovered"] = found is not None
    return report


def _indexer_body(args):
    return {
        "name": args.get("name") or "", "type": args.get("type") or "newznab", "url": args.get("url") or "",
        "api_path": args.get("api_path") or "/api", "api_key": args.get("api_key") or "",
        "categories": args.get("categories") or "6000", "priority": args.get("priority", 25),
        "enabled": args.get("enabled", True),
        # Torznab only (*arr defaults: at least 1 seeder; seed ratio / seed time empty = the download client's own limits)
        "minimum_seeders": args.get("minimum_seeders", 1), "seed_ratio": args.get("seed_ratio"), "seed_time": args.get("seed_time"),
    }


def _download_client_body(args):
    return {
        "name": args.get("name"), "type": args.get("type", "sabnzbd"),
        "protocol": args.get("protocol", "usenet"), "priority": args.get("priority", 25),
        "enabled": args.get("enabled", True), "url": args.get("url", ""),
        "api_key": args.get("api_key", ""), "username": args.get("username", ""),
        "password": args.get("password", ""), "category": args.get("category", ""),
        "options": args.get("options") or {},
    }


def _err(prefix, e):
    return {"error": f"{prefix}: {e}"}


def _get(sidecar_url, path, timeout=30):
    try:
        resp = requests.get(f"{sidecar_url}{path}", timeout=timeout)
        return resp.json() if resp.ok else {"error": f"HTTP {resp.status_code}: {resp.text[:300]}"}
    except requests.RequestException as e:
        return _err("Connection failed - is the sidecar running?", e)


def _post(sidecar_url, path, data, timeout=30):
    try:
        resp = requests.post(f"{sidecar_url}{path}", json=data, timeout=timeout)
        return resp.json() if resp.ok else {"error": f"HTTP {resp.status_code}: {resp.text[:300]}"}
    except requests.RequestException as e:
        return _err("Connection failed - is the sidecar running?", e)


def _put(sidecar_url, path, data, timeout=30):
    try:
        resp = requests.put(f"{sidecar_url}{path}", json=data, timeout=timeout)
        return resp.json() if resp.ok else {"error": f"HTTP {resp.status_code}: {resp.text[:300]}"}
    except requests.RequestException as e:
        return _err("Connection failed - is the sidecar running?", e)


def _delete(sidecar_url, path, timeout=30):
    try:
        resp = requests.delete(f"{sidecar_url}{path}", timeout=timeout)
        return resp.json() if resp.ok else {"error": f"HTTP {resp.status_code}: {resp.text[:300]}"}
    except requests.RequestException as e:
        return _err("Connection failed - is the sidecar running?", e)


if __name__ == "__main__":
    main()
