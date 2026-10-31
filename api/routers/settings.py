"""Key-value settings backed by the `setting` table -- covers things a
user can change from the plugin's Settings page (currently just the
scan interval; more can be added the same way without a schema
change). Distinct from api/config.py's Settings, which is
environment-based and fixed for the life of the process (connection
info the sidecar needs at startup)."""
import json
import re

from fastapi import APIRouter, HTTPException

import category_filter
import file_naming
import film_names
import indexer_repo
import quality_profile_repo
from config import settings as env_settings

router = APIRouter(prefix="/settings", tags=["settings"])

_db = None

# How much of search/grab/file-import stays with Whisparr vs. this
# sidecar (2026-09-22, "Whisparr scope" presets designed with the user
# 2026-09-21 -- see project roadmap memory for the full 5-preset design;
# presets 1/2 (fully standalone, Prowlarr-direct search) depend on work
# not built yet and aren't offered here). See handoff.py's
# send_to_whisparr() and main.py's run_indexer_search() for where this
# actually branches behavior.
WHISPARR_SCOPE_PRESETS = ("bookkeeping_only", "search_sidecar_import", "full_whisparr", "no_whisparr")
# 'no_whisparr' (2026-09-25): Whisparr isn't touched at all -- this
# sidecar searches (search_backend must then be prowlarr/indexers),
# downloads and imports every release itself, see handoff.hand_off().
# Non-StashDB providers (ThePornDB, generic stash-boxes) always take
# that Whisparr-free path regardless of preset, since Whisparr can only
# look up StashDB ids -- except under 'full_whisparr', where the sidecar
# doesn't import at all and they're rejected with a clear message.

# Where the sidecar's OWN search gets its candidates (2026-09-25,
# replaces the old implicit "Prowlarr whenever a Prowlarr URL is
# configured" rule -- see search_sources.py). Only consulted when the
# sidecar searches at all, i.e. only under the 'bookkeeping_only' scope
# preset -- under the other two Whisparr runs its own search. 'indexers' = this
# sidecar's own direct Newznab indexers (Settings > Indexers).
SEARCH_BACKENDS = ("whisparr", "prowlarr", "indexers")

WATCHLIST_SOURCES = ("favorites", "tag")


def _is_iso_date(value):
    from datetime import date
    try:
        date.fromisoformat(value)
        return len(value) == 10
    except ValueError:
        return False

# Set by main.py (needs a Stash client, which this router doesn't own):
# callable(key, new_value) -> error text or None. Blocks a switch to an
# empty/missing watchlist tag, see DEFAULTS' `watchlist_source` comment.
_watchlist_guard = None

DEFAULTS = {
    "scan_interval_hours": "6",
    "safety_sweep_interval_hours": "24",
    # Empty default -- falls back to config.py's env-based
    # STASHDB_API_KEY (see get_stashdb_api_key()) when unset here, so
    # setting it via this Settings page isn't required, just preferred
    # (editable without touching api/.env / rebuilding the container).
    "stashdb_api_key": "",
    # Same empty-falls-back-to-env pattern as stashdb_api_key above, now
    # extended to every connection the sidecar needs (see
    # get_setting_value() and PROGRESS.md's "Settings-page connections"
    # section) -- Stash/Whisparr/StashDB/ThePornDB URL+key all editable
    # here without a container rebuild.
    "stash_url": "",
    "stash_api_key": "",
    "stashdb_url": "",
    "whisparr_url": "",
    "whisparr_api_key": "",
    "theporndb_url": "",
    "theporndb_api_key": "",
    # Prowlarr (2026-09-20) -- real Settings-page fields, replacing the
    # PROWLARR_URL/PROWLARR_API_KEY .env-only vars used during the
    # exploratory live investigation (see config.py's own comment and
    # PROGRESS.md). Same empty-falls-back-to-env pattern as every other
    # connection above. Not yet wired into indexer_search.py's own
    # candidate selection -- this is Connections-tab/config plumbing
    # only, the actual search/hand-off integration is its own separate
    # design item.
    "prowlarr_url": "",
    "prowlarr_api_key": "",
    # SABnzbd connection info moved to the `download_client` table
    # (Milestone 3, 2026-09-19, see download_client_repo.py) -- no
    # longer a flat setting here. An existing install's old
    # sabnzbd_url/sabnzbd_api_key values are migrated into a seeded
    # download_client row on first startup (db.py's
    # `_migrate_seed_download_client_from_legacy_sabnzbd()`), so this
    # section intentionally no longer defines those keys.
    #
    # This project's OWN quality-profile concept (Milestone 3) --
    # id of the profile (quality_profile table) used for indexer
    # selection. Distinct from whisparr_quality_profile_id below, which
    # Whisparr's own add-movie call still needs for its own bookkeeping.
    "quality_profile_id": "",
    # Plain on/off switch, NOT a second quality_profile id -- see
    # resolve_quality_profiles()'s docstring for why: a VR-flagged
    # release is matched by Whisparr's own dedicated "vr" quality tag,
    # never by resolution, so there's no meaningful "profile" to build
    # for it beyond "search for VR content or don't".
    "vr_search_enabled": "false",
    # How often the periodic indexer re-scan loop (main.py's
    # run_indexer_search()) retries a 'sent_to_whisparr' release that
    # hasn't been grabbed yet -- shorter than the sync/scan intervals by
    # default since indexer availability for adult content can change
    # within hours, not days.
    "indexer_search_interval_hours": "2",
    # Whisparr quality profile id used for a hand-off -- empty falls
    # back to api/.env's WHISPARR_QUALITY_PROFILE_ID (see config.py),
    # same override pattern as the connection fields above. Picked from
    # a live-fetched dropdown on the Settings page, not typed by hand,
    # since Whisparr supports fully custom profiles with arbitrary ids.
    "whisparr_quality_profile_id": "",
    # Separate profile for VR-flagged releases (see db.py's
    # `_migrate_release_is_vr()` for how that flag is determined) --
    # 2026-09-12, user's own Whisparr instance already has a dedicated
    # "VR" profile, but a normal-content profile is usually the wrong
    # resolution/tag set for VR files. No env fallback: empty means
    # "not configured yet", handoff.py falls back to the normal profile
    # above rather than failing the hand-off over it.
    "whisparr_vr_quality_profile_id": "",
    # Whisparr-scope preset (2026-09-22) -- see WHISPARR_SCOPE_PRESETS
    # above. "bookkeeping_only" is today's live default: Whisparr is
    # only informed of a hand-off (register_existing(), monitored=False,
    # never searches), this sidecar's own indexer_search.py does
    # search+grab+import. Drives file_import_source as a derived value
    # (see the Settings-page UI's buildWhisparrScopeSection()) rather
    # than that being an independent choice -- one less pair of controls
    # that can drift out of sync.
    "whisparr_scope_preset": "bookkeeping_only",
    # See SEARCH_BACKENDS above. Empty = not chosen explicitly yet, which
    # resolves to exactly the pre-2026-09-25 implicit behavior (see
    # resolve_search_backend()), so an upgrade changes nothing by itself.
    "search_backend": "",
    # How the periodic scan finds new releases (2026-09-25): 'feed' reads
    # each provider's global "recently created" feed and matches it to
    # the watchlist locally (~24 requests instead of one per entry);
    # 'per_entry' is the original one-request-per-watchlist-entry scan,
    # kept as a switch-back option. See watcher.run_feed_scan().
    "scan_mode": "feed",
    # How often to pull Whisparr's own blocklist into this sidecar's
    # own local review_rejected_titles (2026-09-23, see
    # whisparr_blocklist_sync.py's own module docstring for the full
    # reasoning -- polling only, Whisparr has no blocklist-change
    # webhook). Same 24h default as stashdb_favorites_sync_interval_hours
    # below -- blocklisting is a deliberate, infrequent action, not
    # something that needs near-real-time sync.
    "whisparr_blocklist_sync_interval_hours": "24",
    "stashdb_favorites_sync_interval_hours": "24",
    # How often to re-run the ThePornDB batch-identify job against
    # currently-favorited Studios/Performers (see theporndb_identify.py)
    # -- cheap to run often since it only ever attempts entities with no
    # ThePornDB match yet (refresh=False), but a full day by default is
    # plenty since new favorites don't appear that often.
    "theporndb_identify_interval_hours": "24",
    # Seconds paused between each 20-entity batch-tag chunk during a
    # ThePornDB identify run (see theporndb_identify.py) -- Stash's own
    # batch-tag job has no request-pacing parameter of its own, so this
    # is the only lever to stay under ThePornDB's documented stash-box
    # rate limit (240 req/min, same figure cited for stashdb_query_delay
    # below -- the only one found this session, checked 2026-09-11).
    "theporndb_identify_delay_seconds": "3.0",
    # How often to check whether a "sent_to_whisparr" release has
    # actually been downloaded and imported by Whisparr yet -- a much
    # shorter interval than the sync/scan ones by default, since a
    # download can complete anywhere from seconds to hours after
    # hand-off and the whole point is closing that feedback loop
    # promptly. See api/import_check.py.
    "import_check_interval_hours": "1",
    # Seconds between per-entity StashDB queries during a scan -- see
    # release_watcher.py's original --delay reasoning (be a polite,
    # low-rate API client). No officially published StashDB.org rate
    # limit is known (checked 2026-09-11); the only documented
    # comparable figure is a sibling stash-box instance (ThePornDB)
    # enforcing 240 req/min. Editable here instead of only via
    # api/.env's STASHDB_QUERY_DELAY so it can be tuned per-scan
    # without a rebuild -- e.g. raised before a large first scan
    # against a freshly-grown watchlist.
    "stashdb_query_delay": "0.3",
    # Milestone 2 (2026-09-17): this sidecar imports/renames/moves
    # completed downloads itself instead of trusting Whisparr's own
    # auto-import (which must be turned off on the real Whisparr
    # instance -- Settings > Media Management > Completed Download
    # Handling -- for a completed download to sit still long enough
    # for this project's own poll to see it). Defaults seeded from the
    # Whisparr Media Management format strings already configured on
    # the real instance at the time this shipped, so the visible
    # naming doesn't change on cutover -- see file_naming.py.
    "scene_naming_format": "{Scene Title} - {Release Date} {Quality Full}",
    "scene_folder_format": "scenes/{Studio Title}/{Scene Title} - {Release Date}",
    # Hardlink instead of copy+delete when the source and destination
    # are on the same filesystem -- same reasoning/default as
    # Whisparr's own "Use Hardlinks instead of Copy" setting.
    "use_hardlinks": "true",
    "organized_hook": "off",
    "name_replacements": "[]",
    "movie_folder_format": "Movies/{Movie Title}< ({Movie Year})>",
    "movie_naming_format": "{Movie Title}< ({Movie Year})>< - part {Part Number}> {Quality Full}",
    "tag_folders": "[]",
    # This sidecar's own local mount of the same physical share
    # Whisparr/Stash call `/data` on their end (see docker-compose.yml's
    # own comment) -- file_importer.py swaps a Whisparr-reported path's
    # `whisparr_root_folder_path` prefix for this one before touching
    # anything locally. No env fallback needed beyond config.py's own
    # default ("/media") since this is specific to how THIS sidecar's
    # own container happens to be set up, not a connection detail.
    "local_media_mount": "/media",
    # Which source file_import_run() reads completed downloads from --
    # added 2026-09-19 as a real switch (see routers/file_import.py's
    # /source endpoint and PROGRESS.md's Milestone 2 cutover
    # investigation) after finding "whisparr_built_in" doesn't actually
    # produce importPending queue entries while Whisparr's own
    # enableCompletedDownloadHandling is off -- kept switchable rather
    # than deleting the old path outright, so this can be rolled back to
    # Whisparr's own built-in import at any time (e.g. before every
    # milestone is done) without a code change.
    # - "sidecar_sabnzbd": SabnzbdSource, matches a release straight to
    #   its SABnzbd job (see download_sources.py) -- the one that
    #   actually works today.
    # - "whisparr_built_in": WhisparrQueueSource, the original Milestone
    #   2 design -- only useful with enableCompletedDownloadHandling
    #   back ON (in which case Whisparr imports the file itself, and
    #   this project's own file-import loop has nothing left to do).
    "file_import_source": "sidecar_sabnzbd",
    # SABnzbd's own container path for the completed-downloads folder
    # (its `storage` field in history, e.g. "/downloads/...") and this
    # sidecar's own local mount of that same physical share -- same
    # prefix-swap pattern as whisparr_root_folder_path/local_media_mount
    # above, just for the download-staging share instead of the
    # library. Only used when file_import_source is "sidecar_sabnzbd".
    "sabnzbd_downloads_root_path": "/downloads",
    "local_downloads_mount": "/downloads",
    # How often to check Whisparr's queue for a completed-but-not-yet-
    # imported download -- short by default, same reasoning as
    # import_check_interval_hours (a download can finish at any time,
    # and this loop is now what actually gets the file into the
    # library, not just a status check).
    "file_import_interval_hours": "0.25",
    # Recycle bin (2026-09-19, added directly in response to that same
    # day's real data-loss bug -- see PROGRESS.md's "Milestone 2
    # cutover: RESOLVED" entry) -- file_importer._recycle_existing_target()
    # moves a file it's about to replace here instead of letting it be
    # silently overwritten. Path is relative to `local_media_mount`
    # (the user's own choice, 2026-09-19: inside the library share, no
    # extra mount needed, keeps a same-filesystem hardlink possible).
    "recycle_bin_path": ".recycle-bin",
    # How old a recycled file must be (by the time it was recycled, not
    # its original content date) before recycle_bin.cleanup_recycle_bin()
    # permanently deletes it -- long enough to notice and recover from a
    # bad replacement, short enough the bin doesn't grow forever. 7 days
    # picked as a reasonable middle ground (no strong signal either way
    # from Whisparr's own equivalent setting, which defaults to
    # "never"/disabled) -- adjust freely.
    "recycle_bin_cleanup_days": "7",
    # How often to run that cleanup sweep -- daily by default, same
    # reasoning as safety_sweep_interval_hours (nothing here is
    # time-critical, a stale entry sitting an extra few hours is fine).
    "recycle_bin_cleanup_interval_hours": "24",
    # Scenes per page in the Releases page's List view (2026-09-26 --
    # replaces `list_view_limit` from 2026-09-24, a load cap for the old
    # browser-side filtering; filtering and paging are server-side now,
    # see release_repo.query_releases()).
    "list_view_page_size": "100",
    # Saved Releases-page filter combinations (2026-09-24, user request
    # -- e.g. a "PMV" preset instead of retyping the search every time).
    # JSON list of {name, search, type, vr, providers, date_from,
    # date_until}, shared by the List and Calendar views (Calendar just
    # ignores the date fields). One setting row, no schema change needed.
    "filter_presets": "[]",
    # Last-resort resolutions (v0.218.0): once a scene is this many days old (0 = never) and nothing in the quality profile exists, a 2160p release
    # (scaled down to 1080p HEVC at the import, transcode.py) and, with fallback_accept_sd, an SD one is taken. transcode_quality: x265 CRF
    # (VAAPI: QP+1), lower = better, a quality target (variable bitrate); transcode_hw: 'auto' (VAAPI when a test encode works) or 'off'.
    "fallback_resolution_days": "10",
    "fallback_accept_sd": "true",
    "transcode_quality": "24",
    "transcode_hw": "auto",
    "encode_target": "nas",          # 'nas' (this sidecar encodes) or 'worker' (an encode worker on another machine, worker/srm_encode_worker.py)
    "encode_worker_wait_hours": "0", # with 'worker': after this many hours without a worker the sidecar encodes itself (0 = never)
    # Where the encode worker machine mounts the folders the sidecar knows (sent to the worker with every job, so nothing has to be repeated on its
    # command line): the library (the sidecar's local_media_mount) and the download folder (local_downloads_mount). Empty = the worker's --map decides.
    "encode_worker_media_path": "",
    "encode_worker_downloads_path": "",
    "transcode_codec": "h265",   # 'h265' (smaller files) or 'h264' (bigger, but the only hardware encoder some chips have)
    "transcode_preset": "fast",  # CPU speed: 'medium' (best), 'fast', 'veryfast'

    # Saved tag combinations of the Search page's "By tag" (v0.210.0): JSON list of {name, provider, tags: [{id, name}]}.
    "search_tag_presets": "[]",
    # Permissions (2026-09-19) -- Whisparr's own Media Management >
    # Permissions section (Set Permissions / chmod Folder / chown
    # Group). Off by default (empty/false), same reasoning as Whisparr's
    # own default -- most setups don't need this, and a wrong chmod/
    # chown value is easy to get subtly wrong, so it should be an
    # explicit opt-in rather than silently applied. See
    # file_importer._apply_permissions().
    "set_permissions": "false",
    "chmod_folder": "755",
    "chown_group": "",
    # Watchlist source (2026-09-26): 'favorites' = Stash's Favorite flag
    # on Studios/Performers drives watchlist membership (the original
    # behavior), 'tag' = a dedicated Stash tag does (child tags count
    # too), so Favorite is just a normal favorite again. See
    # watchlist_source.py. Switching to 'tag' goes through the migration
    # (POST /watchlist/migrate-to-tag), which tags every current favorite
    # first -- set_one() below refuses a direct switch to an empty tag,
    # since the next sync would otherwise drop the whole watchlist.
    "watchlist_source": "favorites",
    "watchlist_tag_name": "Watchlist",
    # "Probably inactive" review list (stage 2): no new scene for this
    # many months.
    "inactive_after_months": "18",
    # How often the inactive check refreshes its data (weekly default;
    # only runs once the interval has really passed, not on restart).
    "inactive_check_interval_hours": "168",
    # Watch for a StashDB merge of the duplicate entries recorded in scene_alias (kind 'duplicate_entry'): ONE bundled StashDB request
    # per run for all of them (v0.125.0). 999 = paused.
    "stashdb_duplicate_check_interval_hours": "24",
    # Automatic search for the movies on the Movies wishlist (v0.128.0, film_search.py): proposes candidates (Activity > Needs review),
    # never grabs by itself. Off until switched on (Settings > Schedule & Import).
    # What the plugin and the sidecar look after (v0.139.0): 'both', 'scenes' or 'movies' (movies = movies/groups). The switch hides the other
    # area in the plugin (Releases page, Maintenance, the Releases tab on studio/performer or group pages) and keeps the background work of
    # the other area (scan, searches, feeds) from running.
    "content_scope": "both",
    # Torrents (v0.135.0): remove a finished torrent (with its files) from the client once its seeding goal is reached -- the *arr "Remove
    # Completed" option, off by default (torrent_cleanup.py).
    "remove_completed_downloads": "false",
    "film_search_enabled": "false",
    "film_search_interval_hours": "6",
    "film_search_per_run": "5",
    "film_search_min_days": "7",
    # Newer movies are searched more often (v0.131.0): a movie released within `film_search_recent_days` (or in the future) is searched
    # every `film_search_recent_min_days` days instead of `film_search_min_days`.
    "film_search_recent_days": "60",
    "film_search_recent_min_days": "1",
    # New movies of the watched ThePornDB studios -> Movies wishlist > Suggestions (v0.131.0, film_feed.py): ONE feed request per run. 999 = paused.
    "film_feed_interval_hours": "6",
    # A feed movie dated older than this many days (or in the future: kept) is a catalogue entry added late, not a new release (v0.137.1).
    "film_feed_max_age_days": "120",
    # History/Events log retention (2026-09-28, v0.79.0, history.py) --
    # days; 0 keeps everything. 14 by default: the log also takes the
    # background events (grabs, imports, job results), not just pop-ups.
    "history_retention_days": "14",
    # SABnzbd's Archive (4.2+) keeps deleted history entries and its duplicate detection reads them ("Duplicate NZB"); entries older than this many
    # days are removed once a day (v0.222.3, download_history_cleanup.purge_archive). 0 = never.
    "sab_archive_purge_days": "30",
    # Size cap for non-VR releases (2026-09-28, see indexer_search's
    # DEFAULT_SIZE_LIMITS for the library numbers behind them), GB; 0 = none.
    # Torrents (v0.231.0): choice by seeders, and the health check of running torrents (torrent_health.py)
    "torrent_min_seeders": "1",            # fewer seeders than this = last resort
    "torrent_plenty_seeders": "10",        # the best resolution is taken without comparison when it has at least this many
    "torrent_lower_res_factor": "3",       # a lower resolution wins when it has this many times the seeders of the best one
    "torrent_health_minutes": "30",        # how often the running torrents are checked (0 = never)
    "torrent_stall_hours": "1",            # no progress for this long = stalled
    "torrent_real_progress_percent": "5",  # a stalled torrent with at least this much is never given up
    "torrent_alt_search_hours": "6",       # a stuck torrent looks for an alternative at most this often
    "max_file_gb": "8",
    "max_file_gb_2160": "12",
    # Back-catalogue additions (2026-09-26): a scene the scan finds whose
    # release date is more than this many days before it was added to
    # StashDB/ThePornDB goes to the backlog (marked "New" there) instead
    # of New Releases, and is never auto-sent. 0 = off.
    "late_addition_days": "90",
    # Global default "From" date (2026-09-26): every watchlist entry
    # without its own From only stores/loads scenes released on or after
    # this date (YYYY-MM-DD, '' = no limit). See watchlist_repo's
    # effective_window().
    "default_window_from": "",
    # What happens when a Studio/Performer newly lands on the watchlist (v0.198.1): 'off' = nothing (the catalog is loaded by hand per year in the
    # entity panel), 'ask' = the Search page asks after Watch/Favorite, 'auto' = the sidecar loads the catalog in the background for every new
    # entry (from the date window's From on). StashDB and added stash-boxes, not ThePornDB.
    "catalog_on_watch": "off",
    # VR limits (2026-09-26, user: Quest 3 / Steam Frame -- 6K is the
    # sweet spot, 7K/8K at 60 fps stutters; max 15 GB per scene, target
    # ~10 GB). Used by indexer_search.vr_candidate_problem() for fresh
    # grabs and by the upgrade search.
    "vr_max_height": "3456",
    "vr_max_file_gb": "15",
    "vr_target_file_gb": "10",
    "vr_min_file_gb": "1.5",
    # Upgrade search (2026-09-26, upgrades.py). VR files count as "low
    # quality" below this total width or bitrate; automatic upgrade
    # searches are off until switched on, at most N per indexer-search run.
    # A stereo VR file below this bitrate (Mbit/s) is an upgrade candidate (v0.237.4; the width rule is gone).
    "vr_upgrade_min_mbps": "8",
    "vr_upgrade_min_width": "3840",  # no longer used (kept so old settings load)
    # Submit the fingerprints of a file you confirmed ("Correct + submit to StashDB" on Import failed) to StashDB. Public and not
    # undoable, so off until you switch it on (v0.106.7).
    "fingerprint_upload_enabled": "false",
    "upgrade_search_enabled": "false",
    # A "Dry run" button on Upgrade wanted rows (shows what a search would grab, downloads nothing): off by default (v0.237.0).
    "upgrade_dry_run_button": "false",
    "upgrade_search_per_run": "20",
    # The upgrade search rides on the indexer-search loop (every 2 h) but only runs this often
    # (2026-09-29, user: 1-2 runs a day are plenty). Wishlist searches keep their own interval.
    "upgrade_search_interval_hours": "12",
    # Hard cap on NZB grabs per day of ANY kind (2026-09-29, after a loop bug hammered the indexer).
    "max_grabs_per_day": "30",
    # Colour of the built-in providers in lists, badges and the calendar (#rrggbb, empty = default) -- v0.152.0.
    # Stage B of Whisparr for movies (v0.158.0): after a movie import tell Whisparr the folder + rescan, automatically. Off until the dry run was seen.
    "film_whisparr_file_auto": "false",
    # Stage D (v0.160.0): after the Whisparr sync, movies whose file is already in the library are used automatically (clear cases only). Off until seen.
    "film_library_auto": "false",
    "provider_color_stashdb": "",
    "provider_color_theporndb": "",
    # Wishlist search: "polling" = every release gets its own indexer queries each tick (indexer_search_interval_hours);
    # "rss" = one blank-query feed pull per tick (rss_interval_minutes), searched locally, with a daily/weekly
    # back-off for own queries (2026-09-30, the indexer complained about the query volume).
    "search_mode": "polling",
    "rss_interval_minutes": "30",
    # Result categories that are dropped from every search (comma separated ids, see category_filter.py): Pack 6050, Imageset 6060 by default.
    "excluded_categories": "6050,6060",
}


def init_settings_router(db, watchlist_guard=None):
    global _db, _watchlist_guard
    _db = db
    _watchlist_guard = watchlist_guard


def get_setting_value(db, key, env_fallback):
    """DB-stored value (editable from the plugin's own Settings page)
    takes precedence over the env-based one from api/.env -- lets any
    connection's URL/key be changed without a container rebuild. Not
    cached: every call site here is either infrequent (a scan, a
    manual connection check) or itself only runs once per request, so
    reading one row each time is fine. Generalizes what used to be
    get_stashdb_api_key()-only (kept below for backward compatibility)
    to every connection setting -- see PROGRESS.md's "Settings-page
    connections" section."""
    with db.connect() as conn:
        row = conn.execute("SELECT value FROM setting WHERE key = ?", (key,)).fetchone()
    value = row["value"] if row else ""
    return value or env_fallback


def get_stashdb_api_key(db, env_fallback):
    return get_setting_value(db, "stashdb_api_key", env_fallback)


def resolve_whisparr_quality_profiles(db):
    """(normal, vr) quality profile ids for a hand-off -- shared by the
    manual hand-off route (routers/releases.py) and the scan loop's
    auto hand-off (main.py), 2026-09-13, so both resolve the exact same
    settings the same way instead of duplicating this. `vr` is None
    when unset (no env fallback for it, see DEFAULTS above) --
    handoff.py's send_to_whisparr() falls back to the normal profile in
    that case."""
    quality_profile_id = int(get_setting_value(db, "whisparr_quality_profile_id", env_settings.whisparr_quality_profile_id))
    vr_setting = get_setting_value(db, "whisparr_vr_quality_profile_id", "")
    vr_quality_profile_id = int(vr_setting) if vr_setting else None
    return quality_profile_id, vr_quality_profile_id


def resolve_quality_profiles(db):
    """(profile, vr_enabled) for indexer_search.py's own selection --
    this project's OWN quality-profile concept (Milestone 3,
    2026-09-19), distinct from resolve_whisparr_quality_profiles()
    above (which resolves WHISPARR's profile ids, still needed for its
    own add-movie bookkeeping, see handoff.py). `profile` is None when
    nothing's configured yet -- send_to_whisparr() logs and skips the
    grab rather than failing the whole hand-off over it.

    `vr_enabled` is a plain bool, NOT a second quality_profile -- fixed
    2026-09-19 (v0.40.2) after the user pointed out the original design
    (assign a `quality_profile` row to a `quality_profile_vr_id`
    setting, same shape as the normal one) was actively misleading: a
    VR-flagged release is matched purely by Whisparr's own `source:
    "vr"` quality tag (see indexer_search.py's module docstring), never
    by resolution, so asking the user to fill in "resolutions" for a
    VR profile has no effect on anything and just invites confusion
    (real VR content is often 5K/8K, not 1080/2160 -- numbers that
    would never even be consulted). A plain on/off switch is the
    honest shape of what VR search actually needs."""
    profile_id = get_setting_value(db, "quality_profile_id", "")
    profile = quality_profile_repo.get_one(db, int(profile_id)) if profile_id else None
    vr_enabled = get_setting_value(db, "vr_search_enabled", "false") == "true"
    return profile, vr_enabled


def whisparr_configured(db):
    """Whisparr counts as configured only with URL and API key (DB setting or .env)."""
    return bool(get_setting_value(db, "whisparr_url", env_settings.whisparr_url)) \
        and bool(get_setting_value(db, "whisparr_api_key", env_settings.whisparr_api_key))


def resolve_whisparr_scope_preset(db):
    """Current Whisparr-scope preset, defaulting to 'bookkeeping_only'
    (today's live behavior) for a fresh/unset install -- shared by
    handoff.py and main.py's run_indexer_search() so both branch on the
    exact same value the same way. See WHISPARR_SCOPE_PRESETS above.
    Without a configured Whisparr (url + key) it is always 'no_whisparr'
    (v0.144.0): whatever is stored is ignored, this sidecar does everything."""
    if not whisparr_configured(db):
        return "no_whisparr"
    return get_setting_value(db, "whisparr_scope_preset", "bookkeeping_only")


def resolve_file_import_source(db):
    """The effective file import source: without a configured Whisparr always this sidecar's own SABnzbd source, otherwise the stored
    (preset-derived) `file_import_source`."""
    if not whisparr_configured(db):
        return "sidecar_sabnzbd"
    return get_setting_value(db, "file_import_source", DEFAULTS["file_import_source"])


def resolve_search_backend(db):
    """The effective search backend: the explicit `search_backend`
    setting if one was saved, otherwise the old implicit rule
    ('prowlarr' when a Prowlarr URL is configured, else 'whisparr')."""
    explicit = get_setting_value(db, "search_backend", "")
    if explicit in SEARCH_BACKENDS:
        return explicit
    return "prowlarr" if get_setting_value(db, "prowlarr_url", env_settings.prowlarr_url) else "whisparr"


@router.get("/search-backend")
def search_backend_status():
    """For the Settings > Indexers tab: the effective backend, whether
    it was chosen explicitly, which backends are usable right now, and
    the scope preset (the choice only matters under 'bookkeeping_only')."""
    status = {
        "backend": resolve_search_backend(_db),
        "explicit": get_setting_value(_db, "search_backend", "") in SEARCH_BACKENDS,
        "available": {
            "whisparr": bool(get_setting_value(_db, "whisparr_url", env_settings.whisparr_url)),
            "prowlarr": bool(get_setting_value(_db, "prowlarr_url", env_settings.prowlarr_url)),
            "indexers": bool(indexer_repo.list_enabled(_db)),
        },
        "scope_preset": resolve_whisparr_scope_preset(_db),
    }
    # Sources the manual own-query search can use (stage 3) -- every
    # configured sidecar-side one, see main.py's
    # get_manual_search_sources(). Whisparr can't take a custom query.
    status["manual_sources"] = [k for k in ("prowlarr", "indexers") if status["available"][k]]
    return status


@router.get("")
def get_all():
    with _db.connect() as conn:
        rows = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM setting")}
    return {**DEFAULTS, **rows}


@router.get("/connections/effective")
def get_effective_connections():
    """Resolved (Settings-page override, or api/.env fallback) values
    for the four connections -- distinct from the plain values GET
    above, which only ever returns what's been explicitly saved *here*
    (empty otherwise). Added 2026-09-12: the Settings page's connection
    fields were showing a generic example (e.g. "http://localhost:9999")
    as their placeholder whenever nothing had been saved via this page
    yet -- easy to mistake for the actually-active value, when the real
    one (from .env, e.g. a real LAN address) could be completely
    different. URLs are returned as-is; API keys only as a boolean
    (whether *something* is configured for it, from either source) --
    no need to also echo an .env-sourced secret back verbatim just to
    show a "configured" hint, unlike an explicitly-saved one (which the
    plain GET above already returns in full, since the user just typed
    it into this same page)."""
    def url(key, env_value):
        return get_setting_value(_db, key, env_value)

    def key_is_set(key, env_value):
        return bool(get_setting_value(_db, key, env_value))

    return {
        "stash_url": url("stash_url", env_settings.stash_url),
        "stash_api_key_set": key_is_set("stash_api_key", env_settings.stash_api_key),
        "stashdb_url": url("stashdb_url", env_settings.stashdb_url),
        "stashdb_api_key_set": key_is_set("stashdb_api_key", env_settings.stashdb_api_key),
        "whisparr_url": url("whisparr_url", env_settings.whisparr_url),
        "whisparr_api_key_set": key_is_set("whisparr_api_key", env_settings.whisparr_api_key),
        "theporndb_url": url("theporndb_url", env_settings.theporndb_url),
        "theporndb_api_key_set": key_is_set("theporndb_api_key", env_settings.theporndb_api_key),
        "prowlarr_url": url("prowlarr_url", env_settings.prowlarr_url),
        "prowlarr_api_key_set": key_is_set("prowlarr_api_key", env_settings.prowlarr_api_key),
    }


def _json_list(value, key, limit=60):
    try:
        data = json.loads(value or "[]")
    except ValueError:
        raise HTTPException(400, f"{key} must be a JSON list")
    if not isinstance(data, list) or len(data) > limit:
        raise HTTPException(400, f"{key} must be a JSON list of at most {limit} entries")
    return data


@router.post("/naming-preview")
def naming_preview(body: dict):
    """An example of what the given formats (default: the saved ones) produce, with the saved replacement rules and tag folders -- shown under the
    format fields so a template can be tried before it is used. Writes nothing."""
    folder_format = body.get("folder_format") if body.get("folder_format") is not None else get_setting_value(_db, "scene_folder_format", DEFAULTS["scene_folder_format"])
    file_format = body.get("filename_format") if body.get("filename_format") is not None else get_setting_value(_db, "scene_naming_format", DEFAULTS["scene_naming_format"])
    options = file_naming.naming_options(lambda key: get_setting_value(_db, key, "[]"))
    for key in ("name_replacements", "tag_folders"):  # a not yet saved edit can be previewed too
        if isinstance(body.get(key), list):
            options[key if key == "tag_folders" else "replacements"] = body[key]
    detail = {"title": "Example Scene Title", "date": "2026-03-14", "studio": {"name": "Example Studio"},
              "performers": [{"performer": {"name": "Jane Doe", "gender": "FEMALE"}}, {"performer": {"name": "John Roe", "gender": "MALE"}}]}

    def example(tags):
        tokens = file_naming.build_tokens(detail, "WEBDL-1080p", tags=tags, options=options)
        return file_naming.render_path(folder_format, file_format, tokens, ".mp4")

    first_tag = next((str(r.get("tag")) for r in options["tag_folders"] if str(r.get("tag") or "").strip()), None)
    movie_folder = body.get("movie_folder_format") if body.get("movie_folder_format") is not None else get_setting_value(_db, "movie_folder_format", DEFAULTS["movie_folder_format"])
    movie_file = body.get("movie_filename_format") if body.get("movie_filename_format") is not None else get_setting_value(_db, "movie_naming_format", DEFAULTS["movie_naming_format"])

    def movie(part, year="2011"):
        tokens = film_names.movie_tokens("Example Film Title", year, part, "WEBDL-1080p", "Example Studio", options)
        return film_names.render_movie_path(movie_folder, movie_file, tokens, ".mkv")

    return {"example": example([]), "example_with_tag": example([first_tag]) if first_tag else None, "tag": first_tag,
            "movie_example": movie(None), "movie_example_part": movie(2), "movie_example_no_year": movie(None, "")}


@router.get("/{key}")
def get_one(key: str):
    with _db.connect() as conn:
        row = conn.execute("SELECT value FROM setting WHERE key = ?", (key,)).fetchone()
    return {"key": key, "value": row["value"] if row else DEFAULTS.get(key)}


@router.put("/{key}")
def set_one(key: str, body: dict):
    value = str(body.get("value", ""))
    if key == "whisparr_scope_preset" and value not in WHISPARR_SCOPE_PRESETS:
        raise HTTPException(400, f"whisparr_scope_preset must be one of {WHISPARR_SCOPE_PRESETS}, got {value!r}")
    if key == "content_scope" and value not in ("both", "scenes", "movies"):
        raise HTTPException(400, f"content_scope must be 'both', 'scenes' or 'movies', got {value!r}")
    if key == "catalog_on_watch" and value not in ("off", "ask", "auto"):
        raise HTTPException(400, f"catalog_on_watch must be 'off', 'ask' or 'auto', got {value!r}")
    if key == "scan_mode" and value not in ("feed", "per_entry"):
        raise HTTPException(400, f"scan_mode must be 'feed' or 'per_entry', got {value!r}")
    if key in ("provider_color_stashdb", "provider_color_theporndb") and value and not re.match(r"^#[0-9a-fA-F]{6}$", value):
        raise HTTPException(400, f"{key} must be #rrggbb or empty, got {value!r}")
    if key in ("film_whisparr_file_auto", "film_library_auto") and value not in ("true", "false"):
        raise HTTPException(400, f"{key} must be 'true' or 'false', got {value!r}")
    if key in ("upgrade_search_enabled", "upgrade_dry_run_button") and value not in ("true", "false"):
        raise HTTPException(400, f"{key} must be 'true' or 'false', got {value!r}")
    if key == "vr_upgrade_min_mbps":
        try:
            ok = 1 <= float(value) <= 200
        except ValueError:
            ok = False
        if not ok:
            raise HTTPException(400, f"vr_upgrade_min_mbps must be a number between 1 and 200, got {value!r}")
    if key == "upgrade_search_per_run" and not (value.isdigit() and 1 <= int(value) <= 200):
        raise HTTPException(400, f"upgrade_search_per_run must be a whole number between 1 and 200, got {value!r}")
    if key == "upgrade_search_interval_hours" and not (value.isdigit() and 1 <= int(value) <= 168):
        raise HTTPException(400, f"upgrade_search_interval_hours must be a whole number between 1 and 168, got {value!r}")
    if key in ("movie_folder_format", "movie_naming_format"):  # v0.194.0, see film_names.py
        if not value.strip():
            raise HTTPException(400, f"{key} must not be empty")
        if value.strip().startswith("/") or ".." in value.split("/") or "\\" in value:
            raise HTTPException(400, f"{key} is relative to the library folder: no leading '/', no '..', no backslash")
        if key == "movie_naming_format" and "/" in value:
            raise HTTPException(400, "movie_naming_format is the FILE name: no '/' (folders belong in movie_folder_format)")
    if key == "name_replacements":  # [{"find", "replace", "mode": text|word|regex}] -- v0.193.0, see file_naming.apply_replacements()
        rules = _json_list(value, key)
        for r in rules:
            if not isinstance(r, dict) or not str(r.get("find") or "").strip() or r.get("mode", "text") not in file_naming.REPLACE_MODES:
                raise HTTPException(400, "name_replacements: every rule needs a 'find' text and a mode of text, word or regex")
            if r.get("mode") == "regex":
                try:
                    re.compile(r["find"])
                except re.error as e:
                    raise HTTPException(400, f"name_replacements: the regular expression {r['find']!r} is invalid: {e}")
    if key == "tag_folders":  # [{"tag", "folder"}], the order is the priority -- see file_naming.tag_folder_for()
        rules = _json_list(value, key)
        for r in rules:
            if not isinstance(r, dict) or not str(r.get("tag") or "").strip():
                raise HTTPException(400, "tag_folders: every entry needs a tag")
            if re.search(r'[<>:"/\\|?*]', str(r.get("folder") or "")):
                raise HTTPException(400, f"tag_folders: the folder name {r.get('folder')!r} contains characters a folder name cannot have")
    if key == "organized_hook" and value not in ("off", "preview", "on"):
        raise HTTPException(400, f"organized_hook must be 'off', 'preview' or 'on', got {value!r}")
    if key == "search_mode" and value not in ("polling", "rss"):
        raise HTTPException(400, f"search_mode must be 'polling' or 'rss', got {value!r}")
    if key == "max_grabs_per_day" and not (value.isdigit() and 1 <= int(value) <= 1000):
        raise HTTPException(400, f"max_grabs_per_day must be a whole number between 1 and 1000, got {value!r}")
    if key == "rss_interval_minutes" and not (value.isdigit() and 10 <= int(value) <= 1440):
        # NZBFinder (2026-10): "Setting your RSS fetch interval below 10 minutes will result in a ban" -- the feed is cached anyway
        raise HTTPException(400, f"rss_interval_minutes must be a whole number between 10 and 1440, got {value!r}")
    if key == "excluded_categories" and not category_filter.valid(value):
        raise HTTPException(400, f"excluded_categories must be category ids between 6000 and 6999, comma separated, got {value!r}")
    if key == "search_backend" and value not in SEARCH_BACKENDS:
        raise HTTPException(400, f"search_backend must be one of {SEARCH_BACKENDS}, got {value!r}")
    if key == "fallback_resolution_days" and not (value.isdigit() and 0 <= int(value) <= 365):
        raise HTTPException(400, f"fallback_resolution_days must be a whole number of days between 0 and 365, got {value!r}")
    if key == "fallback_accept_sd" and value not in ("true", "false"):
        raise HTTPException(400, f"fallback_accept_sd must be 'true' or 'false', got {value!r}")
    if key == "transcode_quality" and not (value.isdigit() and 16 <= int(value) <= 32):
        raise HTTPException(400, f"transcode_quality must be a whole number between 16 and 32, got {value!r}")
    if key == "encode_target" and value not in ("nas", "worker"):
        raise HTTPException(400, f"encode_target must be 'nas' or 'worker', got {value!r}")
    if key == "encode_worker_wait_hours":
        try:
            ok = 0 <= float(value) <= 720
        except ValueError:
            ok = False
        if not ok:
            raise HTTPException(400, f"encode_worker_wait_hours must be a number between 0 and 720, got {value!r}")
    if key == "transcode_codec" and value not in ("h265", "h264"):
        raise HTTPException(400, f"transcode_codec must be 'h265' or 'h264', got {value!r}")
    if key == "transcode_preset" and value not in ("medium", "fast", "veryfast"):
        raise HTTPException(400, f"transcode_preset must be 'medium', 'fast' or 'veryfast', got {value!r}")
    if key == "transcode_hw" and value not in ("auto", "off"):
        raise HTTPException(400, f"transcode_hw must be 'auto' or 'off', got {value!r}")
    if key == "filter_presets":
        try:
            parsed = json.loads(value)
        except ValueError:
            parsed = None
        if not isinstance(parsed, list) or not all(isinstance(p, dict) and p.get("name") for p in parsed):
            raise HTTPException(400, "filter_presets must be a JSON list of objects with a 'name'")
    if key == "search_tag_presets":
        try:
            parsed = json.loads(value)
        except ValueError:
            parsed = None
        if not isinstance(parsed, list) or not all(isinstance(p, dict) and p.get("name") and isinstance(p.get("tags"), list) for p in parsed):
            raise HTTPException(400, "search_tag_presets must be a JSON list of objects with a 'name' and a 'tags' list")
    if key == "watchlist_source" and value not in WATCHLIST_SOURCES:
        raise HTTPException(400, f"watchlist_source must be one of {WATCHLIST_SOURCES}, got {value!r}")
    if key == "watchlist_tag_name" and not value.strip():
        raise HTTPException(400, "watchlist_tag_name must not be empty")
    if key == "inactive_after_months" and not (value.isdigit() and 1 <= int(value) <= 240):
        raise HTTPException(400, f"inactive_after_months must be a whole number between 1 and 240, got {value!r}")
    if _watchlist_guard is not None and key in ("watchlist_source", "watchlist_tag_name"):
        problem = _watchlist_guard(key, value.strip())
        if problem:
            raise HTTPException(409, problem)
    if key == "default_window_from" and value and not _is_iso_date(value):
        raise HTTPException(400, f"default_window_from must be YYYY-MM-DD or empty, got {value!r}")
    if key == "late_addition_days" and not (value.isdigit() and int(value) <= 3650):
        raise HTTPException(400, f"late_addition_days must be a whole number between 0 and 3650, got {value!r}")
    if key == "list_view_page_size" and not (value.isdigit() and 20 <= int(value) <= 1000):
        raise HTTPException(400, f"list_view_page_size must be a whole number between 20 and 1000, got {value!r}")
    _torrent_ranges = {"torrent_min_seeders": (0, 1000), "torrent_plenty_seeders": (1, 100000), "torrent_lower_res_factor": (1, 1000),
                       "torrent_health_minutes": (0, 1440), "torrent_stall_hours": (0.5, 720),
                       "torrent_real_progress_percent": (0, 100), "torrent_alt_search_hours": (0.5, 720)}
    if key in _torrent_ranges:
        low, high = _torrent_ranges[key]
        try:
            if not low <= float(value) <= high:
                raise ValueError
        except ValueError:
            raise HTTPException(400, f"{key} must be a number between {low} and {high}, got {value!r}")
    if key in ("max_file_gb", "max_file_gb_2160"):
        try:
            if not 0 <= float(value) <= 1000:
                raise ValueError
        except ValueError:
            raise HTTPException(400, f"{key} must be a number of GB between 0 and 1000, got {value!r}")
    if key == "sab_archive_purge_days" and not (value.isdigit() and int(value) <= 3650):
        raise HTTPException(400, f"sab_archive_purge_days must be a whole number between 0 and 3650, got {value!r}")
    if key == "history_retention_days" and not (value.isdigit() and int(value) <= 3650):
        raise HTTPException(400, f"history_retention_days must be a whole number between 0 and 3650, got {value!r}")
    with _db.connect() as conn:
        row = conn.execute("SELECT value FROM setting WHERE key = ?", (key,)).fetchone()
        old = row["value"] if row else DEFAULTS.get(key)
        conn.execute(
            """INSERT INTO setting (key, value) VALUES (?, ?)
               ON CONFLICT (key) DO UPDATE SET value = excluded.value""",
            (key, value),
        )
    _log_setting_change(key, old, value)
    return {"ok": True}


# Setting keys whose VALUES never go into the log (user, 2026-09-28: log
# settings changes, but secrets only as "changed").
_SECRET_MARKERS = ("key", "password", "token", "secret", "apikey")


def _log_setting_change(key, old, new):
    if (old or "") == (new or ""):
        return
    import history
    if any(m in key.lower() for m in _SECRET_MARKERS):
        history.event("info", "settings", f"Setting {key} changed")
        return
    shorten = lambda v: (v or "(empty)") if len(v or "") <= 120 else f"{v[:117]}..."
    history.event("info", "settings", f"Setting {key}: {shorten(old)} -> {shorten(new)}")
