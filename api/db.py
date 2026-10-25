"""SQLite storage for the watchlist, per-entity provider watermarks, and
discovered releases. One file, no server process -- see PROGRESS.md's
"DB: SQLite, confirmed" decision (2026-09-11).

Schema:
  watchlist(provider, entity_type, stashdb_id, native_id) -- studios and
    performers being watched, entity_type is 'studio' or 'performer'.
    `stashdb_id` is the entity's id *at this provider's stash_ids entry
    in local Stash* -- for provider='stashdb' that's the StashDB UUID
    itself; for provider='theporndb' it's the id under ThePornDB's own
    stash-box-shim endpoint (also a UUID, a *different* id space from
    ThePornDB's numeric REST-API ids). `native_id` is only populated for
    provider='theporndb': the numeric id ThePornDB's real scene-listing
    REST API needs, resolved once via ThePornDBClient.resolve_native_id()
    at sync time (see sync.py) since the REST API rejects the UUID
    directly. See PROGRESS.md's "ThePornDB scan integration" section for
    why two different ThePornDB id spaces exist at all.
  watermark(provider, entity_type, stashdb_id, last_created) -- newest
    `created` timestamp already seen for that entity at that provider,
    mirrors release_watcher_state.json's role in the prior-art script.
  release(provider, scene_stashdb_id, entity_type, entity_stashdb_id, ...)
    -- a discovered new scene, one row per (provider, scene, entity)
    triple since a scene can be new-relative-to more than one watched
    entity (e.g. a watched studio AND a watched performer) at once, and
    the same underlying scene could in principle be surfaced separately
    by both providers (not deduplicated across providers -- a known,
    accepted limitation, see PROGRESS.md).
  metadata_provider(key, name, url, api_key, enabled) -- ADDITIONAL
    stash-box GraphQL providers beyond the two built-in ones (StashDB,
    ThePornDB), which stay outside this table entirely and keep reading
    their connection info from their own existing settings keys (see
    this table's own CREATE statement below for why). `key` doubles as
    the `provider` value used in watchlist/watermark/release above.
"""
import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS watchlist (
    provider           TEXT NOT NULL DEFAULT 'stashdb',
    entity_type        TEXT NOT NULL CHECK (entity_type IN ('studio', 'performer')),
    stashdb_id         TEXT NOT NULL,
    native_id          TEXT,
    local_id           INTEGER,
    name               TEXT NOT NULL,
    added_at           TEXT NOT NULL DEFAULT (datetime('now')),
    auto_handoff       INTEGER NOT NULL DEFAULT 0,
    auto_handoff_from  TEXT,
    auto_handoff_until TEXT,
    PRIMARY KEY (provider, entity_type, stashdb_id)
);

CREATE TABLE IF NOT EXISTS watermark (
    provider     TEXT NOT NULL DEFAULT 'stashdb',
    entity_type  TEXT NOT NULL CHECK (entity_type IN ('studio', 'performer')),
    stashdb_id   TEXT NOT NULL,
    last_created TEXT NOT NULL,
    updated_at   TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (provider, entity_type, stashdb_id)
);

CREATE TABLE IF NOT EXISTS release (
    provider           TEXT NOT NULL DEFAULT 'stashdb',
    scene_stashdb_id   TEXT NOT NULL,
    entity_type        TEXT NOT NULL CHECK (entity_type IN ('studio', 'performer')),
    entity_stashdb_id  TEXT NOT NULL,
    title               TEXT NOT NULL DEFAULT '',
    scene_date          TEXT,
    scene_created       TEXT NOT NULL,
    studio_name          TEXT NOT NULL DEFAULT '',
    status              TEXT NOT NULL DEFAULT 'new'
        CHECK (status IN ('new', 'sent_to_whisparr', 'imported', 'import_failed', 'dismissed', 'backlog')),
    discovered_at       TEXT NOT NULL DEFAULT (datetime('now')),
    handoff_at          TEXT,
    imported_at         TEXT,
    image_url           TEXT,
    is_vr               INTEGER NOT NULL DEFAULT 0,
    review_candidate        TEXT,
    review_rejected_titles  TEXT,
    PRIMARY KEY (provider, scene_stashdb_id, entity_type, entity_stashdb_id)
);

CREATE TABLE IF NOT EXISTS setting (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Caches count_scenes_by_year()'s result per entity (2026-09-13, see
-- PROGRESS.md's "Cache the backlog year overview" section) -- so
-- reopening the Backlog panel doesn't re-run ~12 StashDB requests
-- every single time; only an explicit "Refresh" click does. A brand
-- new table, not an ALTER of an existing one, so no rename-rebuild
-- migration is needed regardless of schema version.
CREATE TABLE IF NOT EXISTS backlog_overview (
    provider           TEXT NOT NULL DEFAULT 'stashdb',
    entity_type        TEXT NOT NULL CHECK (entity_type IN ('studio', 'performer')),
    entity_stashdb_id  TEXT NOT NULL,
    years_json         TEXT NOT NULL,
    checked_at         TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (provider, entity_type, entity_stashdb_id)
);

CREATE INDEX IF NOT EXISTS idx_release_status ON release(status);
CREATE INDEX IF NOT EXISTS idx_release_created ON release(scene_created);

-- Upgrade search (2026-09-26, see upgrades.py): library files that should
-- be replaced by a better version. reason: 'vr_2d' (VR scene, flat
-- file), 'vr_mono' (monoscopic 360/180 VR), 'vr_low_quality' (VR below
-- 4K total width or below the minimum bitrate), 'below_cutoff' (non-VR
-- file below the quality profile's cutoff). status: 'wanted', 'grabbed'
-- (an upgrade download is running), 'done' (replaced; the old file went to
-- the recycle bin), 'review' (upgrade imported but couldn't be verified --
-- old file kept), 'dismissed'.
CREATE TABLE IF NOT EXISTS upgrade_candidate (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    scene_id           TEXT NOT NULL,
    file_path          TEXT NOT NULL,
    scene_title        TEXT NOT NULL DEFAULT '',
    provider           TEXT,
    provider_scene_id  TEXT,
    reason             TEXT NOT NULL,
    cur_width          INTEGER,
    cur_height         INTEGER,
    cur_size           INTEGER,
    cur_mbps           REAL,
    status             TEXT NOT NULL DEFAULT 'wanted',
    last_search_at     TEXT,
    last_search_result TEXT,
    detail             TEXT,
    created_at         TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at         TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (scene_id, file_path)
);

-- Convert (v0.245.0, see convert.py): library files no browser plays directly (old video codec or container) that an encode worker turns into
-- H.264 + AAC in an MP4. One row per file: it is the candidate AND the job. mode: 'reencode' | 'remux'. status: 'wanted' (listed), 'queued' (the
-- user asked), 'running' (a worker has it), 'verifying' / 'replacing' (the sidecar checks the file / swaps it into the scene), 'done', 'failed',
-- 'dismissed' (the user does not want it converted).
CREATE TABLE IF NOT EXISTS convert_candidate (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    scene_id       TEXT NOT NULL,
    file_id        TEXT,
    file_path      TEXT NOT NULL,
    scene_title    TEXT NOT NULL DEFAULT '',
    container      TEXT,
    video_codec    TEXT,
    audio_codec    TEXT,
    width          INTEGER,
    height         INTEGER,
    size           INTEGER,
    bit_rate       INTEGER,
    duration       REAL,
    mode           TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'wanted',
    error          TEXT,
    new_path       TEXT,
    size_after     INTEGER,
    worker         TEXT,
    percentage     INTEGER,
    speed_x        REAL,
    seconds_left   INTEGER,
    beat_at        REAL,
    queued_at      TEXT,
    started_at     TEXT,
    finished_at    TEXT,
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (scene_id, file_path)
);
CREATE INDEX IF NOT EXISTS idx_convert_status ON convert_candidate(status);

-- Maintenance check "VR scene, but the file is 2D" (2026-09-26, see
-- vr_check.py) -- one row per checked file: analysis cache (size +
-- mod_time decide whether it needs re-checking) and finding at once.
-- finding_status: 'none' (stereo / nothing to show), 'pending',
-- 'dismissed' (user says it's fine), 'fixed' (Fix wrong download ran), 'handled' (our own flat import: blocklisted + upgrade candidate at once).
CREATE TABLE IF NOT EXISTS vr_file_check (
    scene_id           TEXT NOT NULL,
    file_path          TEXT NOT NULL,
    scene_title        TEXT NOT NULL DEFAULT '',
    file_size          INTEGER,
    file_mod_time      TEXT,
    width              INTEGER,
    height             INTEGER,
    lr_score           REAL,
    tb_score           REAL,
    verdict            TEXT NOT NULL,
    error              TEXT,
    phash_match        INTEGER,
    provider           TEXT,
    provider_scene_id  TEXT,
    finding_status     TEXT NOT NULL DEFAULT 'none',
    checked_at         TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (scene_id, file_path)
);

-- Film wishlist (v0.107.0): films/groups the user WANTS, separate from the scene releases (those are scene-centric and tied to watched
-- entities). status: 'suggested' (found by the seed match, waiting for the user), 'wanted', 'grabbed' (download running),
-- 'imported', 'import_failed', 'dismissed'. `tpdb_json` = the ThePornDB movie record (group creation at import time).
CREATE TABLE IF NOT EXISTS film_want (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    title              TEXT NOT NULL,
    year               INTEGER,
    release_date       TEXT,
    studio_name        TEXT,
    duration_min       REAL,
    performers         TEXT,
    image_url          TEXT,
    tpdb_id            TEXT,
    tpdb_json          TEXT,
    source             TEXT NOT NULL DEFAULT 'manual',
    source_ref         TEXT,
    match_class        TEXT,
    stash_group_id     TEXT,
    status             TEXT NOT NULL DEFAULT 'wanted'
        CHECK (status IN ('suggested', 'wanted', 'grabbed', 'imported', 'import_failed', 'dismissed')),
    download_client_id INTEGER,
    download_job_id    TEXT,
    grabbed_title      TEXT,
    grabbed_at         TEXT,
    last_search_at     TEXT,
    last_search_result TEXT,
    pending_import_path TEXT,
    import_error       TEXT,
    imported_group_id  TEXT,
    note               TEXT,
    created_at         TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at         TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_film_want_tpdb ON film_want(tpdb_id) WHERE tpdb_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_film_want_status ON film_want(status);

-- Backlog years the user has loaded via "Missing from Catalog"
-- (2026-09-26) -- persisted explicitly so a year stays marked "loaded"
-- even once all its scenes were dismissed/imported (it used to be
-- derived from the remaining release rows only). New table.
CREATE TABLE IF NOT EXISTS backlog_loaded_year (
    provider           TEXT NOT NULL,
    entity_type        TEXT NOT NULL CHECK (entity_type IN ('studio', 'performer')),
    entity_stashdb_id  TEXT NOT NULL,
    year               INTEGER NOT NULL,
    loaded_at          TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (provider, entity_type, entity_stashdb_id, year)
);

-- "Probably inactive" review list (2026-09-26, watchlist-by-tag stage 2,
-- see inactive_check.py) -- one row per watchlist entity (keyed like the
-- watchlist itself, per provider), refreshed by the periodic inactive
-- check. `newest_scene_date` = the entity's newest scene by RELEASE date
-- at that provider (not the created-at watermark the scan uses: a studio
-- whose back catalogue is still being added would look active by that).
-- `kept_reasons` = the reasons shown when the user chose "Keep" -- the
-- entity is only proposed again once a NEW reason shows up. New table,
-- no rebuild migration needed.
CREATE TABLE IF NOT EXISTS watchlist_activity (
    provider           TEXT NOT NULL,
    entity_type        TEXT NOT NULL CHECK (entity_type IN ('studio', 'performer')),
    stashdb_id         TEXT NOT NULL,
    newest_scene_date  TEXT,
    scene_count        INTEGER,
    career_end_year    INTEGER,
    death_date         TEXT,
    deleted            INTEGER NOT NULL DEFAULT 0,
    checked_at         TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (provider, entity_type, stashdb_id)
);

-- History/Events log (2026-09-28, v0.79.0, see history.py): what
-- happened to a scene (kind 'history') and to the system (kind 'event'),
-- like the *arrs' History and System > Events. Pruned by date
-- (history_retention_days).
CREATE TABLE IF NOT EXISTS history (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now')),
    level        TEXT NOT NULL CHECK (level IN ('info', 'warning', 'error')),
    kind         TEXT NOT NULL CHECK (kind IN ('history', 'event')),
    category     TEXT NOT NULL,
    message      TEXT NOT NULL,
    details      TEXT,
    provider     TEXT,
    scene_id     TEXT,
    scene_title  TEXT
);
CREATE INDEX IF NOT EXISTS history_at ON history (at);
CREATE INDEX IF NOT EXISTS history_scene ON history (provider, scene_id);

CREATE TABLE IF NOT EXISTS inactive_review (
    entity_type   TEXT NOT NULL CHECK (entity_type IN ('studio', 'performer')),
    local_id      INTEGER NOT NULL,
    kept_reasons  TEXT NOT NULL DEFAULT '[]',
    kept_at       TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (entity_type, local_id)
);

-- Instant favorite-change detection (2026-09-21, see
-- stash-release-manager.yml's `hooks:` section) -- a Studio/Performer
-- just unfavorited in Stash, with at least one still-open ('new'/
-- 'sent_to_whisparr') release, lands here until the user decides via
-- the Releases page's banner whether to keep those releases around or
-- dismiss them. One row per (provider, entity) -- an entity favorited
-- against more than one provider gets one row each, since "keep or
-- dismiss" is a genuinely separate decision per provider's own
-- releases. New table, no rename-rebuild migration needed.
CREATE TABLE IF NOT EXISTS pending_favorite_decision (
    provider           TEXT NOT NULL,
    entity_type        TEXT NOT NULL CHECK (entity_type IN ('studio', 'performer')),
    stashdb_id         TEXT NOT NULL,
    name               TEXT NOT NULL,
    open_release_count INTEGER NOT NULL DEFAULT 0,
    flagged_at         TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (provider, entity_type, stashdb_id)
);

-- Milestone 3 (2026-09-19): this project's own quality-profile concept
-- for indexer_search.py's release selection, deliberately much smaller
-- than Whisparr's own (no custom formats, no per-quality preferred-size
-- target -- see PROGRESS.md's Milestone 3 planning research) -- just an
-- ordered list of allowed resolutions (best first, JSON-encoded) plus a
-- cutoff. New table, no rename-rebuild migration needed.
CREATE TABLE IF NOT EXISTS quality_profile (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    name                TEXT NOT NULL,
    resolutions_json    TEXT NOT NULL,
    cutoff_resolution   INTEGER,
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Milestone 3: a configured download client this sidecar can grab a
-- chosen release to directly, modeled on Whisparr's own
-- DownloadClientProvider (type/protocol/priority routing -- see
-- PROGRESS.md's Milestone 3 planning research), collapsed to a single
-- `url` field rather than Whisparr's split host/port/useSsl, matching
-- this project's own existing connection-setting convention
-- (stash_url/whisparr_url/etc.) instead. `type` is 'sabnzbd' only for
-- now -- a torrent client type can be added later without a schema
-- change, per the user's explicit request to keep this extensible.
CREATE TABLE IF NOT EXISTS download_client (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    type        TEXT NOT NULL DEFAULT 'sabnzbd' CHECK (type IN ('sabnzbd', 'qbittorrent')),
    protocol    TEXT NOT NULL DEFAULT 'usenet' CHECK (protocol IN ('usenet', 'torrent')),
    priority    INTEGER NOT NULL DEFAULT 25,
    enabled     INTEGER NOT NULL DEFAULT 1,
    url         TEXT NOT NULL DEFAULT '',
    api_key     TEXT NOT NULL DEFAULT '',
    username    TEXT NOT NULL DEFAULT '',
    password    TEXT NOT NULL DEFAULT '',
    category    TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    options     TEXT NOT NULL DEFAULT '{}'
);

-- Direct Newznab indexers (2026-09-25, Indexers tab stage 2) -- this
-- sidecar's own indexer connections, searched when the `search_backend`
-- setting is 'indexers' (see search_sources.py's IndexersSource).
-- Newznab only for now: Torznab waits until a torrent download client
-- exists (user decision 2026-09-25). `categories` is a comma-separated
-- Newznab category list (6000 = XXX). `priority`: lower = preferred,
-- same convention as download_client/Whisparr/Prowlarr.
CREATE TABLE IF NOT EXISTS indexer (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    type        TEXT NOT NULL DEFAULT 'newznab' CHECK (type IN ('newznab', 'torznab')),
    url         TEXT NOT NULL DEFAULT '',
    api_path    TEXT NOT NULL DEFAULT '/api',
    api_key     TEXT NOT NULL DEFAULT '',
    categories  TEXT NOT NULL DEFAULT '6000',
    priority    INTEGER NOT NULL DEFAULT 25,
    enabled     INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    minimum_seeders INTEGER NOT NULL DEFAULT 1,
    seed_ratio  REAL,
    seed_time   INTEGER
);

-- Metadata Provider generalization (2026-09-21) -- ADDITIONAL stash-box
-- GraphQL providers beyond the two built-in ones (StashDB, ThePornDB),
-- which deliberately stay OUTSIDE this table and keep reading their
-- connection info from their own existing `stashdb_url`/`stashdb_api_key`/
-- `theporndb_url`/`theporndb_api_key` settings keys -- no functional
-- reason to move already-working config, and ThePornDB specifically
-- also needs its own REST-scan special case regardless (see
-- theporndb_client.py). A row here is a plain stash-box GraphQL
-- endpoint (same protocol as StashDB/FansDB/PMV Stash/JAVStash,
-- confirmed live 2026-09-20) -- `key` doubles as the `provider` value
-- used everywhere else (watchlist/watermark/release), so it must be a
-- valid, stable slug once created (enforced at the API layer, not
-- here -- see metadata_provider_repo.py).
CREATE TABLE IF NOT EXISTS metadata_provider (
    key         TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    url         TEXT NOT NULL,
    api_key     TEXT NOT NULL DEFAULT '',
    enabled     INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Backs the Maintenance page's "Library Check" (2026-09-16, see
-- PROGRESS.md's "Maintenance page" section) -- one row per check run
-- (full library or a directory-scoped subset), with live progress
-- fields updated while a background thread is still working so
-- /maintenance/library-check/status can be polled without waiting for
-- the whole run to finish. New tables, not an ALTER of an existing
-- one, so no rename-rebuild migration is needed, same reasoning as
-- backlog_overview above.
CREATE TABLE IF NOT EXISTS library_check_run (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at          TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at         TEXT,
    scope_paths         TEXT NOT NULL DEFAULT '[]',
    status              TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'done', 'failed')),
    current             INTEGER NOT NULL DEFAULT 0,
    total               INTEGER NOT NULL DEFAULT 0,
    match_count         INTEGER NOT NULL DEFAULT 0,
    mismatch_count      INTEGER NOT NULL DEFAULT 0,
    unverifiable_count  INTEGER NOT NULL DEFAULT 0,
    error               TEXT
);

-- `kind`: 'mismatch' (exactly one StashDB candidate -- actionable),
-- 'ambiguous' (2+ candidates), 'unverifiable' (0 candidates / no
-- fingerprint data at all) -- only 'mismatch' findings are ever
-- selectable/applicable in the UI, the other two are informational
-- only (see PROGRESS.md's audit section for why: nothing safe to
-- automate for either).
CREATE TABLE IF NOT EXISTS library_check_finding (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id              INTEGER NOT NULL REFERENCES library_check_run(id),
    scene_id            TEXT NOT NULL,
    scene_title         TEXT NOT NULL,
    kind                TEXT NOT NULL CHECK (kind IN ('mismatch', 'ambiguous', 'unverifiable')),
    expected_stashdb_id TEXT,
    candidates_json     TEXT,
    file_paths          TEXT,
    reason              TEXT,
    status              TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'applied', 'failed', 'dismissed')),
    applied_title       TEXT,
    error               TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    resolved_at         TEXT
);
CREATE INDEX IF NOT EXISTS idx_library_check_finding_status ON library_check_finding(status);

-- Compare scenes (2026-09-30, scene_compare.py): two Stash scenes shown side by side to decide visually which
-- one carries the right provider id / which file to keep. Filled from the Organize name-conflict list, the
-- Maintenance page (add pair by scene ids) or scripts.
CREATE TABLE IF NOT EXISTS compare_pair (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    scene_a     TEXT NOT NULL,
    scene_b     TEXT NOT NULL,
    label       TEXT,
    status      TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'done', 'skipped')),
    note        TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    resolved_at TEXT,
    UNIQUE (scene_a, scene_b)
);

-- Hidden studios (2026-10-03, v0.91.0): a (sub-)studio the user does not want in the Calendar/List although its parent studio is
-- watched. Releases that came in through the STUDIO are set to 'dismissed' with release.muted = 1 (and the old status in
-- muted_prev_status) so every existing status filter / search / hand-off already ignores them; un-hiding restores the old status.
CREATE TABLE IF NOT EXISTS muted_studio (
    studio_id  TEXT PRIMARY KEY,
    provider   TEXT NOT NULL DEFAULT 'stashdb',
    name       TEXT NOT NULL DEFAULT '',
    parent_id  TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Review: Identify Scene (2026-10-02): scenes the user looked at and could not resolve yet ("later" / unresolved) --
-- they stay tagged in Stash but drop out of the default "Open" view. A scene without a row here is "open".
CREATE TABLE IF NOT EXISTS review_mark (
    scene_id   TEXT PRIMARY KEY,
    status     TEXT NOT NULL DEFAULT 'later' CHECK (status IN ('later')),
    note       TEXT,
    marked_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Fingerprint check of unidentified scenes (v0.170.2): the dry-run result per scene, so it survives a page reload and a restart.
-- state: unique / ambiguous / held / shared / none; matches = JSON [{id, title}]. Deleted when the scene is assigned.
CREATE TABLE IF NOT EXISTS fingerprint_check (
    scene_id   TEXT PRIMARY KEY,
    state      TEXT NOT NULL,
    matches    TEXT NOT NULL DEFAULT '[]',
    held_by    TEXT,
    note       TEXT,
    checked_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Alias provider ids (2026-10-02, scene_alias.py): a provider scene (StashDB id) whose video is already in the
-- library under ANOTHER local scene -- the producer sold the same video under two titles and the two local
-- scenes were merged into one (a local scene holds only ONE id per stash-box). The library checks ("in library"
-- in Missing from Catalog, the releases list, the RSS search, completeness) count an alias as owned.
CREATE TABLE IF NOT EXISTS scene_alias (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    provider             TEXT NOT NULL DEFAULT 'stashdb',
    alias_scene_id       TEXT NOT NULL,
    local_scene_id       TEXT NOT NULL,
    title                TEXT,
    scene_date           TEXT,
    studio_stashdb_id    TEXT,
    performer_stashdb_ids TEXT,
    note                 TEXT,
    created_at           TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (provider, alias_scene_id)
);
"""


class Database:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _init_schema(self):
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            self._migrate(conn)

    def _migrate(self, conn):
        """One-off, idempotent migrations for existing databases -- SQLite
        can't ALTER a CHECK constraint or a PRIMARY KEY in place, so each
        step here rebuilds a table (rename-old, create-new, copy,
        drop-old) when it's missing something added since. Detected by
        inspecting the stored CREATE TABLE text rather than a separate
        schema-version table."""
        self._migrate_release_imported_status(conn)
        self._migrate_provider_column(conn)
        self._migrate_watchlist_local_id(conn)
        self._migrate_release_image_url(conn)
        self._migrate_release_is_vr(conn)
        self._migrate_watchlist_auto_handoff(conn)
        self._migrate_release_backlog_status(conn)
        self._migrate_library_check_finding_file_paths(conn)
        self._migrate_library_check_run_provider(conn)
        self._migrate_release_download_job(conn)
        self._migrate_seed_download_client_from_legacy_sabnzbd(conn)
        self._migrate_release_search_status(conn)
        self._migrate_release_review_candidate(conn)
        self._migrate_release_pending_import_path(conn)
        self._migrate_provider_check_constraint(conn)
        self._migrate_release_import_error(conn)
        self._migrate_release_search_details(conn)
        self._migrate_release_scene_urls(conn)
        self._migrate_release_details(conn)
        self._migrate_release_late_addition(conn)
        self._migrate_release_upgrade_of(conn)
        self._migrate_release_grabbed_title(conn)
        self._migrate_upgrade_review(conn)
        self._migrate_release_targeted_search(conn)
        self._migrate_release_studio_mute(conn)
        self._migrate_release_tags(conn)
        self._migrate_release_cast_pairing(conn)
        self._migrate_vr_verdict_manual(conn)
        self._migrate_film_want_scenes(conn)
        self._migrate_film_want_candidates(conn)
        self._migrate_film_want_parts(conn)
        self._migrate_film_want_upgrade(conn)
        self._migrate_scene_alias_duplicates(conn)
        self._migrate_film_want_search(conn)
        self._migrate_torrent_support(conn)
        self._migrate_film_want_whisparr(conn)
        self._migrate_metadata_provider_color(conn)
        self._migrate_metadata_provider_query_delay(conn)
        self._migrate_metadata_provider_favorites_interval(conn)
        self._migrate_film_want_whisparr_file(conn)
        self._migrate_film_want_dups(conn)
        self._migrate_duration_check(conn)
        self._migrate_organize_plan(conn)
        self._migrate_group_match_ignore(conn)
        self._migrate_downloaded_release(conn)
        self._migrate_studio_dismiss_streak(conn)
        self._migrate_torrent_health(conn)
        self._migrate_upgrade_profile(conn)
        self._migrate_upgrade_check_status(conn)

    def _migrate_torrent_support(self, conn):
        """Torrent support (v0.133.0): download_client.type allows 'qbittorrent' (+ an `options` JSON column for the client's *arr-style settings),
        indexer.type allows 'torznab' (+ minimum_seeders / seed_ratio / seed_time, the *arr indexer settings). SQLite cannot alter a CHECK, so the
        two tables are rebuilt (column order = the live tables, new columns last)."""
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='download_client'").fetchone()
        if row and "'qbittorrent'" not in row["sql"]:
            conn.executescript("""
                ALTER TABLE download_client RENAME TO download_client_old;
                CREATE TABLE download_client (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
                    type TEXT NOT NULL DEFAULT 'sabnzbd' CHECK (type IN ('sabnzbd', 'qbittorrent')),
                    protocol TEXT NOT NULL DEFAULT 'usenet' CHECK (protocol IN ('usenet', 'torrent')),
                    priority INTEGER NOT NULL DEFAULT 25, enabled INTEGER NOT NULL DEFAULT 1, url TEXT NOT NULL DEFAULT '',
                    api_key TEXT NOT NULL DEFAULT '', username TEXT NOT NULL DEFAULT '', password TEXT NOT NULL DEFAULT '',
                    category TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    options TEXT NOT NULL DEFAULT '{}'
                );
                INSERT INTO download_client (id, name, type, protocol, priority, enabled, url, api_key, username, password, category, created_at)
                    SELECT id, name, type, protocol, priority, enabled, url, api_key, username, password, category, created_at FROM download_client_old;
                DROP TABLE download_client_old;
            """)
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='indexer'").fetchone()
        if row and "'torznab'" not in row["sql"]:
            conn.executescript("""
                ALTER TABLE indexer RENAME TO indexer_old;
                CREATE TABLE indexer (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
                    type TEXT NOT NULL DEFAULT 'newznab' CHECK (type IN ('newznab', 'torznab')),
                    url TEXT NOT NULL DEFAULT '', api_path TEXT NOT NULL DEFAULT '/api', api_key TEXT NOT NULL DEFAULT '',
                    categories TEXT NOT NULL DEFAULT '6000', priority INTEGER NOT NULL DEFAULT 25, enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    minimum_seeders INTEGER NOT NULL DEFAULT 1, seed_ratio REAL, seed_time INTEGER
                );
                INSERT INTO indexer (id, name, type, url, api_path, api_key, categories, priority, enabled, created_at)
                    SELECT id, name, type, url, api_path, api_key, categories, priority, enabled, created_at FROM indexer_old;
                DROP TABLE indexer_old;
            """)

    def _migrate_provider_check_constraint(self, conn):
        """Relaxes the `provider` CHECK (was `IN ('stashdb', 'theporndb')`)
        on watchlist/watermark/release/backlog_overview to a plain TEXT
        column -- Metadata Provider generalization (2026-09-21, see
        metadata_provider_repo.py), so a new provider's key (e.g. a
        user-added stash-box endpoint) is a valid `provider` value here
        too. SQLite can't ALTER a CHECK constraint in place, so this is
        the same rename-rebuild dance as every other CHECK-constraint
        change in this file -- column set/order matches exactly what's
        live today (confirmed via PRAGMA table_info against the real
        database before writing this), so `INSERT INTO x SELECT * FROM
        x_old` is safe. Validation of what a *new* provider key is
        allowed to be moves to the API layer (metadata_provider_repo.py)
        instead of the schema."""
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='watchlist'"
        ).fetchone()
        if row and "CHECK (provider" not in row["sql"]:
            return
        conn.executescript("""
            ALTER TABLE watchlist RENAME TO watchlist_old;
            CREATE TABLE watchlist (
                provider           TEXT NOT NULL DEFAULT 'stashdb',
                entity_type        TEXT NOT NULL CHECK (entity_type IN ('studio', 'performer')),
                stashdb_id         TEXT NOT NULL,
                native_id          TEXT,
                name               TEXT NOT NULL,
                added_at           TEXT NOT NULL DEFAULT (datetime('now')),
                local_id           INTEGER,
                auto_handoff       INTEGER NOT NULL DEFAULT 0,
                auto_handoff_from  TEXT,
                auto_handoff_until TEXT,
                PRIMARY KEY (provider, entity_type, stashdb_id)
            );
            INSERT INTO watchlist SELECT * FROM watchlist_old;
            DROP TABLE watchlist_old;

            ALTER TABLE watermark RENAME TO watermark_old;
            CREATE TABLE watermark (
                provider     TEXT NOT NULL DEFAULT 'stashdb',
                entity_type  TEXT NOT NULL CHECK (entity_type IN ('studio', 'performer')),
                stashdb_id   TEXT NOT NULL,
                last_created TEXT NOT NULL,
                updated_at   TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (provider, entity_type, stashdb_id)
            );
            INSERT INTO watermark SELECT * FROM watermark_old;
            DROP TABLE watermark_old;

            ALTER TABLE release RENAME TO release_old;
            CREATE TABLE release (
                provider           TEXT NOT NULL DEFAULT 'stashdb',
                scene_stashdb_id   TEXT NOT NULL,
                entity_type        TEXT NOT NULL CHECK (entity_type IN ('studio', 'performer')),
                entity_stashdb_id  TEXT NOT NULL,
                title               TEXT NOT NULL DEFAULT '',
                scene_date          TEXT,
                scene_created       TEXT NOT NULL,
                studio_name          TEXT NOT NULL DEFAULT '',
                status              TEXT NOT NULL DEFAULT 'new'
                    CHECK (status IN ('new', 'sent_to_whisparr', 'imported', 'import_failed', 'dismissed', 'backlog')),
                discovered_at       TEXT NOT NULL DEFAULT (datetime('now')),
                handoff_at          TEXT,
                imported_at         TEXT,
                image_url           TEXT,
                is_vr               INTEGER NOT NULL DEFAULT 0,
                download_client_id  INTEGER,
                download_job_id     TEXT,
                last_search_at      TEXT,
                last_search_result  TEXT,
                review_candidate        TEXT,
                review_rejected_titles  TEXT,
                pending_import_path     TEXT,
                PRIMARY KEY (provider, scene_stashdb_id, entity_type, entity_stashdb_id)
            );
            INSERT INTO release SELECT * FROM release_old;
            DROP TABLE release_old;
            CREATE INDEX IF NOT EXISTS idx_release_status ON release(status);
            CREATE INDEX IF NOT EXISTS idx_release_created ON release(scene_created);

            ALTER TABLE backlog_overview RENAME TO backlog_overview_old;
            CREATE TABLE backlog_overview (
                provider           TEXT NOT NULL DEFAULT 'stashdb',
                entity_type        TEXT NOT NULL CHECK (entity_type IN ('studio', 'performer')),
                entity_stashdb_id  TEXT NOT NULL,
                years_json         TEXT NOT NULL,
                checked_at         TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (provider, entity_type, entity_stashdb_id)
            );
            INSERT INTO backlog_overview SELECT * FROM backlog_overview_old;
            DROP TABLE backlog_overview_old;
        """)

    def _migrate_release_details(self, conn):
        """Adds `release.details` (2026-09-26, user request: filter by
        words in a scene's DESCRIPTION, e.g. "PMV", not only title/
        studio). The scene's own description text from its provider
        (StashDB `details`, ThePornDB `description`), stored by
        release_repo.insert_release(); '' = provider has none, NULL = not
        fetched yet (older rows -- see release_repo.list_releases_missing_details()
        and main.py's backfill). Plain nullable ADD COLUMN."""
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(release)").fetchall()}
        if "details" not in cols:
            conn.execute("ALTER TABLE release ADD COLUMN details TEXT")

    def _migrate_metadata_provider_color(self, conn):
        """metadata_provider.color -- the colour of an additional provider in lists, badges and the calendar (v0.152.0); '' = automatic."""
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(metadata_provider)").fetchall()}
        if cols and "color" not in cols:
            conn.execute("ALTER TABLE metadata_provider ADD COLUMN color TEXT NOT NULL DEFAULT ''")

    def _migrate_metadata_provider_query_delay(self, conn):
        """metadata_provider.query_delay -- seconds between requests to this stash-box during a scan; NULL = use the global stashdb_query_delay (v0.198.1)."""
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(metadata_provider)").fetchall()}
        if cols and "query_delay" not in cols:
            conn.execute("ALTER TABLE metadata_provider ADD COLUMN query_delay REAL")

    def _migrate_metadata_provider_favorites_interval(self, conn):
        """metadata_provider.favorites_interval -- hours between two imports of the account favorites of this stash-box; NULL = the StashDB setting (v0.199.0)."""
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(metadata_provider)").fetchall()}
        if cols and "favorites_interval" not in cols:
            conn.execute("ALTER TABLE metadata_provider ADD COLUMN favorites_interval REAL")

    def _migrate_film_want_whisparr_file(self, conn):
        """film_want.whisparr_file_state (queued / synced / conflict / not_taken / error) and whisparr_file_checked_at -- whether Whisparr was told the
        folder of an imported movie and took its file over (stage B of Whisparr for movies, v0.158.0). Plain ADD COLUMNs."""
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(film_want)").fetchall()}
        for name in ("whisparr_file_state", "whisparr_file_checked_at"):
            if cols and name not in cols:
                conn.execute(f"ALTER TABLE film_want ADD COLUMN {name} TEXT")

    def _migrate_film_want_dups(self, conn):
        """film_want.whisparr_note (why a movie is 'adopted' / 'conflict' in Whisparr) and library_choice ('keep' = the user decided to keep searching
        although the movie is already in the library) -- stage D of Whisparr for movies, v0.160.0. Plain ADD COLUMNs."""
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(film_want)").fetchall()}
        for name in ("whisparr_note", "library_choice"):
            if cols and name not in cols:
                conn.execute(f"ALTER TABLE film_want ADD COLUMN {name} TEXT")

    def _migrate_film_want_parts(self, conn):
        """film_want.as_scene (the grabbed file is ONE scene of the movie, not the whole movie) and parts_imported (scenes added to its Group that
        way so far), v0.221.0. Plain ADD COLUMNs."""
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(film_want)").fetchall()}
        for name in ("as_scene", "parts_imported"):
            if cols and name not in cols:
                conn.execute(f"ALTER TABLE film_want ADD COLUMN {name} INTEGER NOT NULL DEFAULT 0")

    def _migrate_film_want_upgrade(self, conn):
        """film_want.upgrade_wanted (the owned file is below the quality profile: the movie is listed under Upgrade, not on the wishlist) and
        owned_height (the best file's height when that was decided), v0.221.12. Plain ADD COLUMNs."""
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(film_want)").fetchall()}
        for name in ("upgrade_wanted", "owned_height", "upgrading"):
            if cols and name not in cols:  # upgrading (v0.222.0): the running download is the better version of the owned file
                conn.execute(f"ALTER TABLE film_want ADD COLUMN {name} INTEGER" + (" NOT NULL DEFAULT 0" if name != "owned_height" else ""))

    def _migrate_film_want_search(self, conn):
        """film_want.review_candidate (JSON of the indexer result the automatic movie search found and the user has to accept/reject) and
        rejected_titles (JSON list of release names the user rejected for this movie), v0.128.0. Plain ADD COLUMNs."""
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(film_want)").fetchall()}
        for name in ("review_candidate", "rejected_titles"):
            if cols and name not in cols:
                conn.execute(f"ALTER TABLE film_want ADD COLUMN {name} TEXT")

    def _migrate_film_want_whisparr(self, conn):
        """film_want.whisparr_id (the movie's id in Whisparr), whisparr_state (registered / linked / not_found / no_tpdb / error) and
        whisparr_checked_at -- the Whisparr bookkeeping of the movie wishlist, v0.140.0. Plain ADD COLUMNs."""
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(film_want)").fetchall()}
        for name, kind in (("whisparr_id", "INTEGER"), ("whisparr_state", "TEXT"), ("whisparr_checked_at", "TEXT")):
            if cols and name not in cols:
                conn.execute(f"ALTER TABLE film_want ADD COLUMN {name} {kind}")

    def _migrate_scene_alias_duplicates(self, conn):
        """scene_alias.kind ('alias' = the producer sold one video under two titles, 'duplicate_entry' = StashDB lists the SAME scene twice
        under the same title), status ('open' / 'merged' / 'gone', only used for duplicates), partner_scene_id (the StashDB id the local scene
        carries), merged_into (the surviving StashDB id when StashDB redirected one of the two), checked_at, v0.125.0. Plain ADD COLUMNs."""
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(scene_alias)").fetchall()}
        if not cols:
            return
        for name, ddl in (("kind", "TEXT NOT NULL DEFAULT 'alias'"), ("status", "TEXT NOT NULL DEFAULT 'open'"), ("partner_scene_id", "TEXT"),
                          ("merged_into", "TEXT"), ("checked_at", "TEXT")):
            if name not in cols:
                conn.execute(f"ALTER TABLE scene_alias ADD COLUMN {name} {ddl}")

    def _migrate_film_want_candidates(self, conn):
        """film_want.candidates_json (the best ThePornDB candidates of the last match, with their records, for "Use this candidate") and
        seed_json (what we knew BEFORE the match: name, year, length, studio -- shown next to the match), v0.116.0. Plain ADD COLUMN."""
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(film_want)").fetchall()}
        for name in ("candidates_json", "seed_json"):
            if cols and name not in cols:
                conn.execute(f"ALTER TABLE film_want ADD COLUMN {name} TEXT")

    def _migrate_film_want_scenes(self, conn):
        """film_want.mode ('file' = one downloaded film file, 'scenes' = put together from StashDB scenes), scene_ids (JSON list of
        {id, k} = StashDB scene id + its "Scene k") and stashdb_json (the last "Check StashDB scenes" result), v0.111.0. Plain ADD COLUMN."""
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(film_want)").fetchall()}
        if cols and "mode" not in cols:
            conn.execute("ALTER TABLE film_want ADD COLUMN mode TEXT NOT NULL DEFAULT 'file'")
        if cols and "scene_ids" not in cols:
            conn.execute("ALTER TABLE film_want ADD COLUMN scene_ids TEXT")
        if cols and "stashdb_json" not in cols:
            conn.execute("ALTER TABLE film_want ADD COLUMN stashdb_json TEXT")

    def _migrate_vr_verdict_manual(self, conn):
        """Adds `vr_file_check.verdict_manual` (v0.105.0): 1 = the user set the verdict by hand (2D / VR mono / unclear); it survives
        re-judging with new thresholds and re-analysis of the same (unchanged) file. Plain ADD COLUMN."""
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(vr_file_check)").fetchall()}
        if cols and "verdict_manual" not in cols:
            conn.execute("ALTER TABLE vr_file_check ADD COLUMN verdict_manual INTEGER NOT NULL DEFAULT 0")

    def _migrate_release_studio_mute(self, conn):
        """Adds `release.studio_id` (provider studio id of the scene's own studio -- sub-studios are recognised by id, not by
        name), `muted` (1 = hidden because its studio is in muted_studio) and `muted_prev_status`. Plain ADD COLUMN, no rebuild."""
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(release)").fetchall()}
        if "studio_id" not in cols:
            conn.execute("ALTER TABLE release ADD COLUMN studio_id TEXT")
        if "muted" not in cols:
            conn.execute("ALTER TABLE release ADD COLUMN muted INTEGER NOT NULL DEFAULT 0")
        if "muted_prev_status" not in cols:
            conn.execute("ALTER TABLE release ADD COLUMN muted_prev_status TEXT")

    def _migrate_release_tags(self, conn):
        """Adds `release.tags` (v0.92.0): the scene's provider tags as '|tag one|tag two|' (lower-case search is done in SQL);
        NULL = never fetched (rows from before, filled by main.run_tags_backfill), '' = the provider has none."""
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(release)").fetchall()}
        if "tags" not in cols:
            conn.execute("ALTER TABLE release ADD COLUMN tags TEXT")

    def _migrate_release_cast_pairing(self, conn):
        """Adds `release.cast_pairing` (v0.182.0): who is in the scene by gender, as 'B/G', 'G/G', 'B/G/G' ... (release_repo.pairing_code);
        NULL = never read (rows from before; filled on demand by routers/backlog.py's pairings()), '' = the provider lists no cast."""
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(release)").fetchall()}
        if "cast_pairing" not in cols:
            conn.execute("ALTER TABLE release ADD COLUMN cast_pairing TEXT")
        if "cast_names" not in cols:  # the performers' names, '|Name A|Name B|' (same shape as `tags`), for the co-performer filter; NULL = not read
            conn.execute("ALTER TABLE release ADD COLUMN cast_names TEXT")

    def _migrate_release_late_addition(self, conn):
        """Adds `release.late_addition` (2026-09-26): 1 = the scan found
        this scene only because it was added to StashDB/ThePornDB long
        after its release date (back-catalogue addition), so it went to
        'backlog' instead of 'new' (see watcher._store_new_scenes()) --
        the UI marks those as "New" in the backlog. Plain ADD COLUMN
        with a default, no rebuild."""
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(release)")}
        if "late_addition" not in cols:
            conn.execute("ALTER TABLE release ADD COLUMN late_addition INTEGER NOT NULL DEFAULT 0")

    def _migrate_release_upgrade_of(self, conn):
        """Adds `release.upgrade_of` (2026-09-26, upgrade search): the Stash
        path of the library file an upgrade download will replace -- set
        while the upgrade is searched/downloaded, used after the verified
        import to move the old file to the recycle bin. Plain nullable
        ADD COLUMN."""
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(release)")}
        if "upgrade_of" not in cols:
            conn.execute("ALTER TABLE release ADD COLUMN upgrade_of TEXT")

    def _migrate_organize_plan(self, conn):
        """v0.240.27: the plan of every studio of Organize library (what would be renamed / moved, what waits for a check) is kept here until a rename /
        move or a reload changes it, so opening it again reads this table instead of asking StashDB. organize_state: 'recount_at' = a Recount has run."""
        conn.execute("""CREATE TABLE IF NOT EXISTS organize_plan (
            studio_id   TEXT PRIMARY KEY,
            studio      TEXT NOT NULL DEFAULT '',
            plan_json   TEXT NOT NULL,
            checked_at  TEXT NOT NULL DEFAULT (datetime('now'))
        )""")
        conn.execute("CREATE TABLE IF NOT EXISTS organize_state (key TEXT PRIMARY KEY, value TEXT)")
        # v0.241.6: the list of Maintenance > Unidentified scenes (scenes without a provider id) is kept until a change or a reload makes it new.
        conn.execute("CREATE TABLE IF NOT EXISTS unidentified_scene (scene_id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
        conn.execute("CREATE TABLE IF NOT EXISTS maintenance_state (key TEXT PRIMARY KEY, value TEXT)")

    def _migrate_duration_check(self, conn):
        """v0.209.0: the "Duration mismatch" check (duration_check.py). One row per local scene: the primary file's length against the
        length the provider (StashDB / ThePornDB) has for the scene, plus the two new upgrade_candidate columns the re-add reasons use."""
        conn.execute("""CREATE TABLE IF NOT EXISTS duration_check (
            scene_id          TEXT PRIMARY KEY,
            file_path         TEXT NOT NULL,
            file_size         INTEGER,
            scene_title       TEXT NOT NULL DEFAULT '',
            provider          TEXT NOT NULL,
            provider_scene_id TEXT NOT NULL,
            file_sec          REAL,
            provider_sec      REAL,
            status            TEXT NOT NULL DEFAULT 'checked',
            checked_at        TEXT NOT NULL DEFAULT (datetime('now'))
        )""")
        dcols = {r["name"] for r in conn.execute("PRAGMA table_info(duration_check)")}
        if "in_group" not in dcols:  # v0.240.0: a scene that belongs to a movie group is checked on its own page (Movies)
            conn.execute("ALTER TABLE duration_check ADD COLUMN in_group INTEGER NOT NULL DEFAULT 0")
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(upgrade_candidate)")}
        if "cur_duration" not in cols:
            conn.execute("ALTER TABLE upgrade_candidate ADD COLUMN cur_duration REAL")
        if "provider_duration" not in cols:
            conn.execute("ALTER TABLE upgrade_candidate ADD COLUMN provider_duration REAL")
        if "is_vr" not in cols:
            conn.execute("ALTER TABLE upgrade_candidate ADD COLUMN is_vr INTEGER")

    def _migrate_torrent_health(self, conn):
        """v0.231.0: what the torrent monitor (torrent_health.py) knows about each running torrent download. One row per client + torrent hash.
        Times are epoch seconds. Additive table: an older version simply ignores it."""
        conn.execute("""CREATE TABLE IF NOT EXISTS torrent_health (
            client_id               INTEGER NOT NULL,
            job_id                  TEXT NOT NULL,
            kind                    TEXT NOT NULL DEFAULT 'scene',
            provider                TEXT,
            scene_id                TEXT,
            film_id                 INTEGER,
            title                   TEXT NOT NULL DEFAULT '',
            state                   TEXT NOT NULL DEFAULT 'ok',
            detail                  TEXT NOT NULL DEFAULT '',
            progress                REAL NOT NULL DEFAULT 0,
            max_progress            REAL NOT NULL DEFAULT 0,
            first_seen_at           REAL NOT NULL,
            last_checked_at         REAL NOT NULL,
            last_progress_change_at REAL NOT NULL,
            stalled_since           REAL,
            stalled_s               REAL NOT NULL DEFAULT 0,
            last_alt_search_at      REAL,
            keep_until              REAL,
            seeds                   INTEGER,
            peers                   INTEGER,
            availability            REAL,
            raw_state               TEXT,
            PRIMARY KEY (client_id, job_id)
        )""")

    def _migrate_studio_dismiss_streak(self, conn):
        """v0.216.0: dismiss streak per studio (see dismiss_streak.py); a new table is filled once from the History."""
        import dismiss_streak
        existed = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'studio_dismiss_streak'").fetchone()
        dismiss_streak.ensure_table(conn)
        if not existed:
            dismiss_streak.seed_from_history(conn)

    def _migrate_downloaded_release(self, conn):
        """v0.215.0: the ledger of releases that were downloaded and whose entry was then removed from the download client's history
        ("Remove completed", like the *arr applications). The duplicate guard and the blocklist read it where they used to read that history."""
        conn.execute("""CREATE TABLE IF NOT EXISTS downloaded_release (
            name_norm  TEXT PRIMARY KEY,
            title      TEXT NOT NULL,
            nzo_id     TEXT,
            client_id  INTEGER,
            removed_at TEXT NOT NULL DEFAULT (datetime('now'))
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_downloaded_release_nzo ON downloaded_release (nzo_id)")
        cols = {r[1] for r in conn.execute("PRAGMA table_info(downloaded_release)")}
        if "outcome" not in cols:
            # v0.218.1: 'imported' (the release is in the library: no scene may fetch it again) or 'rejected' (blocklisted / quality-rejected for ITS
            # scene: only that scene's own blocklist applies, another scene may well be the right one for this release).
            conn.execute("ALTER TABLE downloaded_release ADD COLUMN outcome TEXT NOT NULL DEFAULT 'imported'")
            import download_history_cleanup
            download_history_cleanup.backfill_outcome(conn)

    def _migrate_group_match_ignore(self, conn):
        """v0.213.0: Maintenance > Movies > Compare groups, "Not this": a suggested (movie folder, group) pair the user rejected."""
        conn.execute("""CREATE TABLE IF NOT EXISTS group_match_ignore (
            folder_key TEXT NOT NULL,
            group_id   TEXT NOT NULL,
            PRIMARY KEY (folder_key, group_id)
        )""")

    def _migrate_upgrade_profile(self, conn):
        """`upgrade_candidate.quality_profile_id` (v0.237.0): a quality profile for THIS candidate (set in bulk on Upgrade wanted); NULL = the
        profile chosen in Settings. VR candidates ignore the resolutions of a profile (they follow the VR rules), so it matters for non-VR ones."""
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(upgrade_candidate)")}
        if "quality_profile_id" not in cols:
            conn.execute("ALTER TABLE upgrade_candidate ADD COLUMN quality_profile_id INTEGER")

    def _migrate_upgrade_check_status(self, conn):
        """v0.237.8, once: wrong-length candidates the library check added before the status 'check' existed (never searched) wait for a look
        instead of being picked up by the automatic upgrade search."""
        done = conn.execute("SELECT 1 FROM setting WHERE key = 'migrated:upgrade_check_status'").fetchone()
        if done:
            return
        conn.execute("UPDATE upgrade_candidate SET status = 'check' WHERE reason = 'duration' AND status = 'wanted' AND last_search_at IS NULL")
        conn.execute("INSERT INTO setting (key, value) VALUES ('migrated:upgrade_check_status', '1')")

    def _migrate_upgrade_review(self, conn):
        """Adds `upgrade_candidate.review_candidate` / `rejected_titles`
        (2026-09-28, v0.80.0): the upgrade search used to drop its "needs
        review" candidate (only the result code was kept, nothing to
        decide on); now it's kept for Activity > Needs review, and a
        rejected one is excluded from later searches of that file."""
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(upgrade_candidate)")}
        if "review_candidate" not in cols:
            conn.execute("ALTER TABLE upgrade_candidate ADD COLUMN review_candidate TEXT")
        if "rejected_titles" not in cols:
            conn.execute("ALTER TABLE upgrade_candidate ADD COLUMN rejected_titles TEXT")

    def _migrate_release_targeted_search(self, conn):
        """2026-09-30 (RSS search mode): when a release was last searched for by its OWN indexer query.
        The feed pass touches last_search_at every tick, so the back-off for targeted searches needs its own
        column."""
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(release)")}
        if "last_targeted_search_at" not in cols:
            conn.execute("ALTER TABLE release ADD COLUMN last_targeted_search_at TEXT")

    def _migrate_release_grabbed_title(self, conn):
        """Adds `release.grabbed_title`/`grabbed_at` (2026-09-26, v0.77.7):
        which release name was downloaded for this scene, and when. Real
        case: the same "Performer.A.Some.Series.With.Host.#03" release was
        grabbed for TWO different "Some Series with Host" scenes in one
        run -- nothing recorded the grabbed name, so nothing could say
        "that release is already another scene's". Also names the
        download in an import-failure message. Backfilled once from the
        search log's 'grabbed' candidate for rows that still hold a
        download (or were imported)."""
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(release)")}
        if "grabbed_title" in cols:
            return
        conn.execute("ALTER TABLE release ADD COLUMN grabbed_title TEXT")
        conn.execute("ALTER TABLE release ADD COLUMN grabbed_at TEXT")
        import json as _json
        rows = conn.execute(
            """SELECT rowid, last_search_details, last_search_at FROM release
               WHERE last_search_details IS NOT NULL
                 AND (download_job_id IS NOT NULL OR status IN ('imported', 'import_failed'))""").fetchall()
        for row in rows:
            try:
                details = _json.loads(row["last_search_details"])
            except (TypeError, ValueError):
                continue
            grabbed = next((c.get("title") for c in details.get("candidates") or []
                            if c.get("verdict") == "grabbed"), None)
            if grabbed:
                conn.execute("UPDATE release SET grabbed_title = ?, grabbed_at = ? WHERE rowid = ?",
                             (grabbed, row["last_search_at"], row["rowid"]))

    def _migrate_release_search_details(self, conn):
        """Adds `release.last_search_details` (2026-09-25, Indexers-tab
        stage 4, "search log") -- JSON of the last automatic search: the
        backend, the query actually sent, and every candidate with its
        verdict (grabbed / review / lower rank / why rejected). See
        indexer_search._explain_candidates(). Plain nullable ADD COLUMN,
        same as the others above; only written by search_and_grab() and
        read on demand via GET /releases/{provider}/{scene}/last-search
        (not part of the list payload -- it can be several KB per row)."""
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(release)").fetchall()}
        if "last_search_details" not in cols:
            conn.execute("ALTER TABLE release ADD COLUMN last_search_details TEXT")

    def _migrate_release_scene_urls(self, conn):
        """Adds `release.scene_urls` (v0.230.0): JSON list [{url, site}] of the scene's provider links, NULL = not fetched yet. Filled lazily by the
        first indexer search of a StashDB scene (indexer_search.studio_site_names) so the site behind a performer-named studio is asked once."""
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(release)").fetchall()}
        if "scene_urls" not in cols:
            conn.execute("ALTER TABLE release ADD COLUMN scene_urls TEXT")

    def _migrate_release_import_error(self, conn):
        """Adds `release.import_error_code`/`import_error_detail`
        (2026-09-23) -- persists WHY an import failed, for the first
        time. Design worked out with the user over several rounds (see
        project_roadmap_todo.md memory's "Import-failed reason +
        conditional Retry" entry for the full reasoning): three
        distinct failure paths in file_importer.py all write these two
        columns now, but only 'fingerprint_mismatch' promotes status to
        'import_failed' AND gets a real "Fix wrong download" retry
        action; 'scene_gone' also promotes to 'import_failed' but gets
        no retry (permanent); 'source_missing' deliberately does NOT
        change status (stays 'sent_to_whisparr' so the existing
        automatic per-cycle retry keeps running) -- these two columns
        just make an already-happening-but-invisible condition visible.
        Plain nullable columns, same simple ADD COLUMN as the others
        above -- no CHECK constraint change needed, `status`'s existing
        allowed values already cover every case this uses."""
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(release)").fetchall()}
        if "import_error_code" not in cols:
            conn.execute("ALTER TABLE release ADD COLUMN import_error_code TEXT")
        if "import_error_detail" not in cols:
            conn.execute("ALTER TABLE release ADD COLUMN import_error_detail TEXT")

    def _migrate_release_download_job(self, conn):
        """Adds `release.download_client_id`/`download_job_id` (Milestone
        3, 2026-09-19) -- records which configured download_client a
        grab went to and that client's own job id (e.g. SABnzbd's
        nzo_id), set by indexer_search.py at grab time. Replaces the old
        approach of re-deriving this from Whisparr's own 'grabbed'
        history event, now that Whisparr no longer does the grab itself
        (see handoff.py). Plain nullable columns, no CHECK constraint,
        same simple ADD COLUMN as `library_check_finding.file_paths`
        above."""
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(release)").fetchall()}
        if "download_client_id" not in cols:
            conn.execute("ALTER TABLE release ADD COLUMN download_client_id INTEGER")
        if "download_job_id" not in cols:
            conn.execute("ALTER TABLE release ADD COLUMN download_job_id TEXT")

    def _migrate_release_search_status(self, conn):
        """Adds `release.last_search_at`/`last_search_result` (Milestone
        3 "search visibility", 2026-09-19, user's own request right
        after the first real grab succeeded -- "wo sehe ich das in der
        UI"/no Whisparr-style Activity page exists here). Records the
        outcome of every indexer_search.search_and_grab() attempt for a
        release, not just a successful one -- see indexer_search.py's
        `_RESULT_*` constants for the possible values (e.g.
        'no_candidates', 'title_mismatch_only', 'grabbed'). Plain
        nullable columns, same simple ADD COLUMN as the others above."""
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(release)").fetchall()}
        if "last_search_at" not in cols:
            conn.execute("ALTER TABLE release ADD COLUMN last_search_at TEXT")
        if "last_search_result" not in cols:
            conn.execute("ALTER TABLE release ADD COLUMN last_search_result TEXT")

    def _migrate_release_review_candidate(self, conn):
        """Adds `release.review_candidate`/`review_rejected_titles`
        (2026-09-19, v0.41.8) -- the "needs review" outcome added
        alongside `_performer_plausible()`'s all-performers variant
        (see indexer_search.py's `RESULT_NEEDS_REVIEW`): a candidate
        with every one of the movie's own credited performers named in
        its filename, but whose date doesn't match closely enough to
        auto-grab (real case found live, 2026-09-19: Whisparr's own
        `releaseDate` for a movie was 17 days off from the actual
        indexer release), is surfaced for a human decision instead of
        either auto-grabbing (the exact risk v0.41.7 just fixed) or
        silently landing in the same bucket as a genuine non-match.
        `review_candidate` is a JSON object (title/indexer/protocol/
        downloadUrl/size/age) or NULL; `review_rejected_titles` is a
        JSON array of raw candidate titles the user has already said no
        to for this scene, so re-running the search doesn't keep
        re-proposing the same rejected one (a different, not-yet-seen
        candidate can still be proposed later). Plain nullable columns,
        same simple ADD COLUMN as the others above."""
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(release)").fetchall()}
        if "review_candidate" not in cols:
            conn.execute("ALTER TABLE release ADD COLUMN review_candidate TEXT")
        if "review_rejected_titles" not in cols:
            conn.execute("ALTER TABLE release ADD COLUMN review_rejected_titles TEXT")

    def _migrate_release_pending_import_path(self, conn):
        """Adds `release.pending_import_path` (2026-09-19, v0.41.12) --
        purely additive companion to the existing Milestone 1 check
        (import_check.py's check_import_status(), left untouched: it
        reads Whisparr's own `movieFileId`, correct for anyone running
        this project with Whisparr's native Completed Download Handling
        back ON, i.e. `file_import_source = "whisparr_built_in"`, where
        Whisparr does its own import and this sidecar's pipeline never
        runs at all). Under the `sidecar_sabnzbd` default, Whisparr's
        CDH is off and never learns about a file this sidecar moved
        itself, so `movieFileId` never gets set -- found live
        2026-09-19: a release whose file was already correctly moved
        into the library stayed stuck at 'sent_to_whisparr' forever,
        because nothing could re-locate it once verify_downloaded_file()
        came back `None` (Stash's own phash generation lagging behind
        its scan job) and the original SABnzbd source item was already
        gone (file_importer.py's own cleanup runs right after the
        move). `pending_import_path` is set by file_importer.py's
        import_one() right after computing the target path (before
        scan/verify), and cleared once verification reaches a terminal
        answer -- see file_importer.py's retry_pending_verifications()
        for the periodic retry this backs. Plain nullable column, same
        simple ADD COLUMN as the others above."""
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(release)").fetchall()}
        if "pending_import_path" not in cols:
            conn.execute("ALTER TABLE release ADD COLUMN pending_import_path TEXT")

    def _migrate_seed_download_client_from_legacy_sabnzbd(self, conn):
        """Milestone 3: `download_client` is now the sole source of
        truth for SABnzbd connection info (see download_client_repo.py),
        replacing the old flat `sabnzbd_url`/`sabnzbd_api_key` settings.
        Seeds one row from those old settings (if they were ever set) so
        an existing install doesn't lose its working connection on
        upgrade. One-shot: only runs when `download_client` is
        completely empty, so it never clobbers a client the user has
        since edited/added through the new Settings-page UI."""
        existing = conn.execute("SELECT COUNT(*) AS n FROM download_client").fetchone()["n"]
        if existing:
            return
        url_row = conn.execute("SELECT value FROM setting WHERE key = 'sabnzbd_url'").fetchone()
        key_row = conn.execute("SELECT value FROM setting WHERE key = 'sabnzbd_api_key'").fetchone()
        url = url_row["value"] if url_row else ""
        api_key = key_row["value"] if key_row else ""
        if not url:
            return  # nothing to migrate -- legacy setting was never set (e.g. a brand-new install)
        conn.execute(
            """INSERT INTO download_client (name, type, protocol, priority, enabled, url, api_key, category)
               VALUES ('SABnzbd', 'sabnzbd', 'usenet', 25, 1, ?, ?, '')""",
            (url, api_key),
        )

    def _migrate_library_check_finding_file_paths(self, conn):
        """Adds `library_check_finding.file_paths` (2026-09-16) -- the
        scene's own local file path(s), JSON-encoded like
        candidates_json, so the Maintenance page's "Needs manual
        review" list can show where a flagged file actually lives on
        disk (user request: "hätte ich gerne die Anzeige des Pfads zu
        der Datei damit ich diese im Dateisystem prüfen kann"). Plain
        nullable column, no CHECK constraint, so a simple ADD COLUMN
        suffices, same as release.image_url/watchlist.local_id above."""
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(library_check_finding)").fetchall()}
        if "file_paths" not in cols:
            conn.execute("ALTER TABLE library_check_finding ADD COLUMN file_paths TEXT")

    def _migrate_library_check_run_provider(self, conn):
        """Adds `library_check_run.provider` (v0.243.0): which metadata provider the run asked ('stashdb' for every run before). Its findings
        belong to that provider (library_check_finding.run_id)."""
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(library_check_run)").fetchall()}
        if "provider" not in cols:
            conn.execute("ALTER TABLE library_check_run ADD COLUMN provider TEXT NOT NULL DEFAULT 'stashdb'")

    def _migrate_release_backlog_status(self, conn):
        """Adds the 'backlog' status (2026-09-13, see PROGRESS.md's
        "On-demand backlog browsing, bypassing Whisparr's Monitored
        flag" section) -- SQLite can't ALTER a CHECK constraint in
        place, so this is the same rename-rebuild dance as
        `_migrate_release_imported_status()`. A backlog-fetched scene
        starts at this status; hand-off/dismiss already transition it
        via the existing generic `set_status_for_scene()`, same as any
        other status, no further schema change needed for those."""
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='release'"
        ).fetchone()
        if row and "'backlog'" in row["sql"]:
            return
        conn.executescript("""
            ALTER TABLE release RENAME TO release_old;
            CREATE TABLE release (
                provider           TEXT NOT NULL DEFAULT 'stashdb' CHECK (provider IN ('stashdb', 'theporndb')),
                scene_stashdb_id   TEXT NOT NULL,
                entity_type        TEXT NOT NULL CHECK (entity_type IN ('studio', 'performer')),
                entity_stashdb_id  TEXT NOT NULL,
                title               TEXT NOT NULL DEFAULT '',
                scene_date          TEXT,
                scene_created       TEXT NOT NULL,
                studio_name          TEXT NOT NULL DEFAULT '',
                status              TEXT NOT NULL DEFAULT 'new'
                    CHECK (status IN ('new', 'sent_to_whisparr', 'imported', 'import_failed', 'dismissed', 'backlog')),
                discovered_at       TEXT NOT NULL DEFAULT (datetime('now')),
                handoff_at          TEXT,
                imported_at         TEXT,
                image_url           TEXT,
                is_vr               INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (provider, scene_stashdb_id, entity_type, entity_stashdb_id)
            );
            INSERT INTO release SELECT * FROM release_old;
            DROP TABLE release_old;
            CREATE INDEX IF NOT EXISTS idx_release_status ON release(status);
            CREATE INDEX IF NOT EXISTS idx_release_created ON release(scene_created);
        """)

    def _migrate_watchlist_auto_handoff(self, conn):
        """Adds `watchlist.auto_handoff`/`auto_handoff_from`/
        `auto_handoff_until` -- 2026-09-13, see PROGRESS.md's "Auto
        hand-off to Whisparr, per entity" section. Opt-in per studio/
        performer (default off -- deliberately not automatic for the
        whole favorite list, given its size) with an optional scene-date
        window so enabling it for an entity doesn't immediately hand off
        its entire existing backlog. Plain nullable columns, no CHECK
        constraint beyond the boolean-as-INTEGER default, so a simple
        ADD COLUMN suffices for each."""
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(watchlist)").fetchall()}
        if "auto_handoff" not in cols:
            conn.execute("ALTER TABLE watchlist ADD COLUMN auto_handoff INTEGER NOT NULL DEFAULT 0")
        if "auto_handoff_from" not in cols:
            conn.execute("ALTER TABLE watchlist ADD COLUMN auto_handoff_from TEXT")
        if "auto_handoff_until" not in cols:
            conn.execute("ALTER TABLE watchlist ADD COLUMN auto_handoff_until TEXT")

    def _migrate_release_is_vr(self, conn):
        """Adds `release.is_vr` -- 2026-09-12, see PROGRESS.md's
        "Per-content-type Whisparr quality profiles (VR)" section.
        StashDB-sourced rows are flagged via a real signal (the
        "Virtual Reality" tag StashDB scenes carry, confirmed live
        against several known VR studios); ThePornDB-sourced rows fall
        back to a studio-name heuristic since ThePornDB exposes no
        structured VR indicator on scenes or sites (checked live,
        `format` is unpopulated and no VR tag/site-flag exists). Plain
        nullable-by-default INTEGER, no CHECK constraint, simple ADD
        COLUMN. Existing rows default to 0 (not VR) -- same "no cheap
        per-scene backfill" situation as image_url, not worth a one-off
        script for a UX-only quality-profile choice on already-handed-
        off-or-still-pending releases."""
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(release)").fetchall()}
        if "is_vr" not in cols:
            conn.execute("ALTER TABLE release ADD COLUMN is_vr INTEGER NOT NULL DEFAULT 0")

    def _migrate_release_image_url(self, conn):
        """Adds `release.image_url` -- the scene's own preview image from
        StashDB/ThePornDB, 2026-09-12, see PROGRESS.md's "Entity panel:
        scene thumbnails" section (requested to match Stash's own Tagger
        view). Plain nullable column, no CHECK constraint, so a simple
        ADD COLUMN suffices. Existing rows stay NULL -- unlike
        watchlist.local_id, there's no cheap way to backfill this from a
        routine sync (it needs a per-scene lookup, not a per-entity one),
        so a one-off backfill script handled the existing backlog once
        instead of building that into the regular sync path."""
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(release)").fetchall()}
        if "image_url" not in cols:
            conn.execute("ALTER TABLE release ADD COLUMN image_url TEXT")

    def _migrate_watchlist_local_id(self, conn):
        """Adds `watchlist.local_id` (this Stash instance's own numeric id
        for the entity, e.g. for /studios/<id> URLs) -- 2026-09-12, see
        PROGRESS.md's "Watchlist: merge duplicates, filter, link to
        Stash" section. A plain nullable column with no CHECK constraint,
        so a simple ADD COLUMN is enough, unlike the rename-rebuild
        dance the other migrations above need. Existing rows backfill
        to NULL until the next scan's sync re-touches them (see
        sync.py's now-unconditional add_to_watchlist() call per current
        favorite, not just newly-added ones)."""
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(watchlist)").fetchall()}
        if "local_id" not in cols:
            conn.execute("ALTER TABLE watchlist ADD COLUMN local_id INTEGER")

    def _migrate_release_imported_status(self, conn):
        """Adds the 'imported'/'import_failed' statuses and `imported_at`
        column (2026-09-11, see PROGRESS.md)."""
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='release'"
        ).fetchone()
        if row and "'imported'" in row["sql"]:
            return
        conn.executescript("""
            ALTER TABLE release RENAME TO release_old;
            CREATE TABLE release (
                scene_stashdb_id   TEXT NOT NULL,
                entity_type        TEXT NOT NULL CHECK (entity_type IN ('studio', 'performer')),
                entity_stashdb_id  TEXT NOT NULL,
                title               TEXT NOT NULL DEFAULT '',
                scene_date          TEXT,
                scene_created       TEXT NOT NULL,
                studio_name          TEXT NOT NULL DEFAULT '',
                status              TEXT NOT NULL DEFAULT 'new'
                    CHECK (status IN ('new', 'sent_to_whisparr', 'imported', 'import_failed', 'dismissed')),
                discovered_at       TEXT NOT NULL DEFAULT (datetime('now')),
                handoff_at          TEXT,
                imported_at         TEXT,
                PRIMARY KEY (scene_stashdb_id, entity_type, entity_stashdb_id)
            );
            INSERT INTO release (scene_stashdb_id, entity_type, entity_stashdb_id, title, scene_date,
                                  scene_created, studio_name, status, discovered_at, handoff_at)
                SELECT scene_stashdb_id, entity_type, entity_stashdb_id, title, scene_date,
                       scene_created, studio_name, status, discovered_at, handoff_at FROM release_old;
            DROP TABLE release_old;
            CREATE INDEX IF NOT EXISTS idx_release_status ON release(status);
            CREATE INDEX IF NOT EXISTS idx_release_created ON release(scene_created);
        """)

    def _migrate_provider_column(self, conn):
        """Adds `provider` (watchlist/watermark/release) and `native_id`
        (watchlist) for the ThePornDB integration (2026-09-11, see
        PROGRESS.md's "ThePornDB scan integration" section) -- every
        existing row is implicitly StashDB's, so it defaults/backfills to
        provider='stashdb'."""
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='watchlist'"
        ).fetchone()
        if row and "provider" in row["sql"]:
            return
        conn.executescript("""
            ALTER TABLE watchlist RENAME TO watchlist_old;
            CREATE TABLE watchlist (
                provider    TEXT NOT NULL DEFAULT 'stashdb' CHECK (provider IN ('stashdb', 'theporndb')),
                entity_type TEXT NOT NULL CHECK (entity_type IN ('studio', 'performer')),
                stashdb_id  TEXT NOT NULL,
                native_id   TEXT,
                name        TEXT NOT NULL,
                added_at    TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (provider, entity_type, stashdb_id)
            );
            INSERT INTO watchlist (entity_type, stashdb_id, name, added_at)
                SELECT entity_type, stashdb_id, name, added_at FROM watchlist_old;
            DROP TABLE watchlist_old;

            ALTER TABLE watermark RENAME TO watermark_old;
            CREATE TABLE watermark (
                provider     TEXT NOT NULL DEFAULT 'stashdb' CHECK (provider IN ('stashdb', 'theporndb')),
                entity_type  TEXT NOT NULL CHECK (entity_type IN ('studio', 'performer')),
                stashdb_id   TEXT NOT NULL,
                last_created TEXT NOT NULL,
                updated_at   TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (provider, entity_type, stashdb_id)
            );
            INSERT INTO watermark (entity_type, stashdb_id, last_created, updated_at)
                SELECT entity_type, stashdb_id, last_created, updated_at FROM watermark_old;
            DROP TABLE watermark_old;

            ALTER TABLE release RENAME TO release_old;
            CREATE TABLE release (
                provider           TEXT NOT NULL DEFAULT 'stashdb' CHECK (provider IN ('stashdb', 'theporndb')),
                scene_stashdb_id   TEXT NOT NULL,
                entity_type        TEXT NOT NULL CHECK (entity_type IN ('studio', 'performer')),
                entity_stashdb_id  TEXT NOT NULL,
                title               TEXT NOT NULL DEFAULT '',
                scene_date          TEXT,
                scene_created       TEXT NOT NULL,
                studio_name          TEXT NOT NULL DEFAULT '',
                status              TEXT NOT NULL DEFAULT 'new'
                    CHECK (status IN ('new', 'sent_to_whisparr', 'imported', 'import_failed', 'dismissed')),
                discovered_at       TEXT NOT NULL DEFAULT (datetime('now')),
                handoff_at          TEXT,
                imported_at         TEXT,
                PRIMARY KEY (provider, scene_stashdb_id, entity_type, entity_stashdb_id)
            );
            INSERT INTO release (scene_stashdb_id, entity_type, entity_stashdb_id, title, scene_date,
                                  scene_created, studio_name, status, discovered_at, handoff_at, imported_at)
                SELECT scene_stashdb_id, entity_type, entity_stashdb_id, title, scene_date,
                       scene_created, studio_name, status, discovered_at, handoff_at, imported_at FROM release_old;
            DROP TABLE release_old;
            CREATE INDEX IF NOT EXISTS idx_release_status ON release(status);
            CREATE INDEX IF NOT EXISTS idx_release_created ON release(scene_created);
        """)

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()
