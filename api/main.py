"""FastAPI sidecar for StashReleaseManager (formerly stash-release-watcher).

Persistent service (see PROGRESS.md's 2026-09-11 "Backend: persistent
sidecar service" decision) -- runs four background loops for as long
as the process is up:
- scheduled StashDB scan, interval from the `scan_interval_hours`
  setting (also runnable on demand via POST /scan/run). Syncs the
  watchlist from this Stash instance's own Favorite flag first --
  see api/sync.py.
- Whisparr Studio/Performer monitored-flag safety sweep, interval from
  `safety_sweep_interval_hours` -- see PROGRESS.md's "Defensive guard
  against re-triggering the Studio/Performer bug".
- StashDB.org favorites import, interval from
  `stashdb_favorites_sync_interval_hours` (also runnable on demand via
  POST /stashdb-favorites/sync) -- imports the user's own StashDB.org
  account favorites into this Stash instance's local favorite flag, for
  whichever of them exist locally. See api/stashdb_favorites.py and
  PROGRESS.md's "StashDB.org favorites import" section for why this
  exists as a separate step from the watchlist sync above.
- Import-status check, interval from `import_check_interval_hours`
  (also runnable on demand via POST /import-check/run) -- closes the
  loop on a hand-off by confirming whether Whisparr actually downloaded
  and imported it yet, since send_to_whisparr() itself is fire-and-
  forget. See api/import_check.py.
"""
import asyncio
import logging
import signal
import json
import os
import threading
from datetime import datetime, timezone
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import StreamingResponse

from config import settings
from db import Database
from stashdb_client import StashDBClient
from stash_client import StashClient
from whisparr_client import WhisparrClient
from sabnzbd_client import SabnzbdClient
from theporndb_client import ThePornDBClient
from prowlarr_client import ProwlarrClient
import search_sources
from search_sources import FeedSource, IndexersSource, ProwlarrSource, UnavailableSource, older_than_feed
import indexer_repo
import watcher
from watcher import run_feed_scan, run_scan
from sync import sync_watchlist_from_favorites
import watchlist_source
import import_progress
import events
import library_import
import stash_events
import library_movies
import file_naming
import library_organize
import history
import verification
from version import SIDECAR_VERSION
import vr_check
import upgrades
import duration_check
import upgrade_library_check
import handoff as _handoff_module
import file_importer as _file_importer_module
import inactive_check
from stashdb_favorites import import_stashdb_favorites
from whisparr_blocklist_sync import sync_whisparr_blocklist
from theporndb_identify import identify_favorites_against_theporndb
from import_check import check_import_status
from handoff import hand_off, uses_whisparr, attempt_sidecar_search, link_imported_equivalent, feed_has_lead, refresh_scene_from_provider
import indexer_search
from indexer_search import search_and_grab
import download_client_repo
import download_history_cleanup
import metadata_provider_repo
import release_repo
from download_sources import WhisparrQueueSource, SabnzbdSource
from file_importer import run_file_import as _run_file_import_pipeline, verify_created_scene
from recycle_bin import cleanup_recycle_bin

from routers.watchlist import router as watchlist_router, init_watchlist_router
from routers.history import router as history_router
from routers.compare import router as compare_router, init_compare_router
from routers.transfer import router as transfer_router, init_transfer_router
from routers.setup import router as setup_router, init_setup_router
from routers.muted_studios import router as muted_studios_router, init_muted_studios_router
from routers.films import router as films_router, init_films_router
import film_match
import film_import
import film_feed
import film_whisparr
import film_library
import torrent_cleanup
import film_search
import film_repo
import scene_compare
import scene_alias
from routers.encode_worker import router as encode_worker_router, init_encode_worker_router
from routers.convert_api import router as convert_router, init_convert_router, resume as convert_resume
from routers.releases import router as releases_router, init_releases_router, download_progress as compute_download_progress
from routers.scan import router as scan_router, init_scan_router, set_progress as set_scan_progress
from routers.settings import (
    router as settings_router, init_settings_router, DEFAULTS, get_setting_value,
    resolve_whisparr_quality_profiles, resolve_quality_profiles, resolve_whisparr_scope_preset, resolve_file_import_source,
    resolve_search_backend, whisparr_configured,
)
from routers.stashdb_favorites import (
    router as stashdb_favorites_router, init_stashdb_favorites_router,
)
from routers.whisparr_blocklist_sync import (
    router as whisparr_blocklist_sync_router, init_whisparr_blocklist_sync_router,
)
from routers.theporndb_identify import (
    router as theporndb_identify_router, init_theporndb_identify_router,
)
from routers.import_check import router as import_check_router, init_import_check_router
from routers.connections import router as connections_router, init_connections_router
from routers.backlog import router as backlog_router, init_backlog_router
from routers.maintenance import router as maintenance_router, init_maintenance_router
from routers.file_import import router as file_import_router, init_file_import_router
from routers.search import router as search_router, init_search_router
from routers.quality_profiles import router as quality_profiles_router, init_quality_profiles_router
from routers.download_clients import router as download_clients_router, init_download_clients_router
from routers.indexers import router as indexers_router, init_indexers_router
from routers.metadata_providers import router as metadata_providers_router, init_metadata_providers_router
from routers.stash_boxes import router as stash_boxes_router, init_stash_boxes_router
from routers.userscript import router as userscript_router, init_userscript_router
from routers.torrent_health import router as torrent_health_router, init_torrent_health_router
import torrent_health
from routers.indexer_search import router as indexer_search_router, init_indexer_search_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

db = Database(settings.db_path)
history.init(db)


# Every client below is built fresh per call rather than as a shared
# instance -- URL/API key can be edited on the plugin's Settings page
# at any time (see routers/settings.py's get_setting_value()), and none
# of these run often enough (a scan, a safety sweep, a manual
# connection check) for rebuilding a requests.Session() each time to
# matter. This replaced module-level singletons that were only ever
# built once from api/.env at process startup, silently ignoring any
# later Settings-page edit to the Stash/Whisparr connection fields --
# see PROGRESS.md's "Settings-page connections" section.

def get_stash_client():
    # Longer timeout than the default -- Stash's own bulkPerformerUpdate/
    # bulkStudioUpdate mutations are genuinely slow and not perfectly
    # stable (~0.75s/entity in a clean test, but even 50-id/90s-timeout
    # batches occasionally timed out under real load -- confirmed live
    # 2026-09-11, see stash_client.py's _bulk_favorite() docstring). 150s
    # plus the smaller batch_size=25 default there leaves real headroom.
    client = StashClient(
        get_setting_value(db, "stash_url", settings.stash_url),
        get_setting_value(db, "stash_api_key", settings.stash_api_key),
        get_setting_value(db, "stashdb_url", settings.stashdb_url),
        timeout=150,
    )
    client.alias_lookup = lambda ids: scene_alias.local_map(db, ids)
    return client


# Parent studios for the indexer search (v0.103.0): from Stash's own studio hierarchy, one listing cached for 10 minutes.
_parent_cache = {"at": 0.0, "by_id": {}, "by_name": {}}


def _studio_parent_names(studio_id, studio_name):
    now = time.time()
    if now - _parent_cache["at"] > 600:
        try:
            _parent_cache["by_id"], _parent_cache["by_name"] = get_stash_client().local_studio_parents()
        except Exception:
            logger.warning("parent studio lookup failed", exc_info=True)
        _parent_cache["at"] = now
    if studio_id and _parent_cache["by_id"].get(studio_id):
        return _parent_cache["by_id"][studio_id]
    key = "".join(ch for ch in (studio_name or "").lower() if ch.isalnum())
    return _parent_cache["by_name"].get(key, [])


indexer_search.set_parent_resolver(_studio_parent_names)


def _scene_site_urls(provider, scene_id):
    """The scene's provider links for the indexer search (v0.230.0): stored per scene (release.scene_urls), fetched once from StashDB at the
    first search. ThePornDB scenes name their site themselves (studio), so nothing is asked there."""
    cached = release_repo.get_scene_urls(db, provider, scene_id)
    if cached is not None:
        return cached
    if provider != "stashdb":
        return []
    urls = get_stashdb_client().get_scene_urls(scene_id)  # a failure propagates: nothing is stored, the next search asks again
    release_repo.set_scene_urls_for_scene(db, provider, scene_id, urls)
    return urls


indexer_search.set_site_resolver(_scene_site_urls)


def get_whisparr_client():
    return WhisparrClient(
        get_setting_value(db, "whisparr_url", settings.whisparr_url),
        get_setting_value(db, "whisparr_api_key", settings.whisparr_api_key),
    )


def get_sabnzbd_client():
    """Milestone 3 (2026-09-19): resolves from the `download_client`
    table (the first enabled usenet client, same routing
    indexer_search.py uses -- see download_client_repo.pick_for_protocol())
    instead of the old flat sabnzbd_url/sabnzbd_api_key settings, which
    that table now fully replaces. Used by the Connections page's
    SABnzbd history/queue diagnostics -- raises if no usenet client is
    configured, same as any other connection with nothing set."""
    client_row = download_client_repo.pick_for_protocol(db, "usenet")
    if client_row is None:
        raise RuntimeError("no enabled SABnzbd (usenet) download client configured")
    return SabnzbdClient(client_row["url"], client_row["api_key"])


def get_stashdb_client():
    """Also passes the current stashdb_query_delay setting so it applies
    to *every* StashDB request this client makes, including between
    pages within a single entity's pagination -- see stashdb_client.py's
    __init__ and PROGRESS.md's "Missing inter-page delay" section."""
    return StashDBClient(
        get_setting_value(db, "stashdb_api_key", settings.stashdb_api_key),
        url=get_setting_value(db, "stashdb_url", settings.stashdb_url),
        delay=_get_setting("stashdb_query_delay"),
    )


def get_prowlarr_client():
    """Connections tab / "Check connection" button, and (2026-09-22)
    the real search/hand-off pipeline -- see
    get_prowlarr_client_if_configured() below for the latter's own
    entry point, prowlarr_client.py's module docstring for why."""
    return ProwlarrClient(
        get_setting_value(db, "prowlarr_url", settings.prowlarr_url),
        get_setting_value(db, "prowlarr_api_key", settings.prowlarr_api_key),
    )


def get_prowlarr_client_if_configured():
    """None when no Prowlarr URL is configured (DB setting or env
    fallback) -- the search/hand-off call sites (run_indexer_search(),
    _auto_handoff_release(), routers/releases.py's handoff()/
    fix_mismatch(), routers/search.py's handoff_scene()) all pass this
    straight into indexer_search.search_and_grab()'s own
    `prowlarr_client` param, which already treats None as "use
    Whisparr's own /release instead" -- keeps this project working for
    other users with no Prowlarr configured at all, same graceful-
    fallback convention every other optional connection here already
    follows."""
    if not get_setting_value(db, "prowlarr_url", settings.prowlarr_url):
        return None
    return get_prowlarr_client()


def get_search_source():
    """The candidate source for the sidecar's own search, per the
    explicit `search_backend` setting (2026-09-25, see
    search_sources.py and routers/settings.py's resolve_search_backend())
    -- passed as `search_source` into indexer_search.search_and_grab()
    by every search/hand-off call site. `None` = Whisparr's own
    `/release`. A chosen-but-unusable backend comes back as an
    UnavailableSource (recorded as its own search result), never as a
    silent fallback to a different backend."""
    backend = resolve_search_backend(db)
    if backend == "prowlarr":
        client = get_prowlarr_client_if_configured()
        if client is None:
            return UnavailableSource("prowlarr", "no Prowlarr connection configured")
        return ProwlarrSource(client)
    if backend == "indexers":
        rows = indexer_repo.list_enabled(db)
        if not rows:
            return UnavailableSource("indexers", "no enabled indexers configured (Settings > Indexers)")
        return IndexersSource(rows)
    return None


_auto_feed_cache = {"at": 0.0, "feed": None}


def get_auto_hand_off_source():
    """The search source for AUTOMATIC hand-offs (a scan found N new releases). In RSS mode they share one
    cached feed pull (10 min) instead of one indexer search per release; if the feed can't be pulled an empty
    feed is used -- no leads, never a query storm. Manual hand-offs keep the real source (immediate search)."""
    source = get_search_source()
    if (get_setting_value(db, "search_mode", "polling") != "rss" or source is None
            or not getattr(source, "available", False)):
        return source
    if _auto_feed_cache["feed"] is not None and time.time() - _auto_feed_cache["at"] < 600:
        return _auto_feed_cache["feed"]
    try:
        feed = FeedSource.fetch(source)
    except Exception:
        logger.exception("automatic hand-off: RSS feed pull failed -- handing off without a search")
        feed = FeedSource([], {})
    _auto_feed_cache.update(at=time.time(), feed=feed)
    return feed


def get_manual_search_sources():
    """{name: source} for every sidecar-side source usable right now,
    for the manual own-query search (2026-09-25, stage 3). Independent
    of the automatic `search_backend` choice by design (user decision:
    the manual search offers every configured sidecar source, so e.g.
    direct indexers can be tried even while Prowlarr runs the automatic
    search). Whisparr isn't one -- its /release can't take a custom
    query (confirmed live 2026-09-19)."""
    sources = {}
    client = get_prowlarr_client_if_configured()
    if client is not None:
        sources["prowlarr"] = ProwlarrSource(client)
    rows = indexer_repo.list_enabled(db)
    if rows:
        sources["indexers"] = IndexersSource(rows)
    return sources


def get_metadata_provider_clients():
    """One StashDBClient instance per ADDITIONAL, enabled generic
    stash-box provider (Metadata Provider generalization, 2026-09-21,
    see metadata_provider_repo.py) -- StashDBClient is the correct,
    already-generic implementation of the stash-box GraphQL protocol
    every one of these providers speaks (confirmed live 2026-09-20:
    StashDB/ThePornDB/FansDB/PMV Stash/JAVStash all use the same
    protocol), just pointed at a different URL/API key. Returns
    {provider_key: StashDBClient}, meant to be merged into the
    `clients` dict `watcher.run_scan()` dispatches on and into
    connections.py's factory dict -- NOT a dict of factory callables
    itself, since callers here already want real client instances
    (unlike get_stashdb_client() etc., which stay lazy factories so a
    Settings-page edit takes effect on the very next call)."""
    return {
        p["key"]: StashDBClient(p["api_key"], url=p["url"],
                                delay=p["query_delay"] if p.get("query_delay") is not None else _get_setting("stashdb_query_delay"))
        for p in metadata_provider_repo.list_all(db, enabled_only=True)
    }


def get_metadata_provider_factories():
    """Same providers as get_metadata_provider_clients() above, but as
    zero-arg factories instead of already-built instances -- what
    connections.py's dynamic /check needs (builds a fresh client per
    call itself, same reasoning as every other connection factory
    here)."""
    return {
        p["key"]: (lambda url=p["url"], api_key=p["api_key"]: StashDBClient(api_key, url=url))
        for p in metadata_provider_repo.list_all(db, enabled_only=True)
    }


def get_provider_client(provider):
    """Metadata client for a release's provider (2026-09-25, Whisparr-free
    hand-off, see handoff.hand_off()) -- StashDB, ThePornDB, or one of
    the user-added stash-box providers. None for an unknown/disabled one.
    Every one of them has get_scene_detail()/get_scene_fingerprints()
    in the same shape."""
    if provider == "stashdb":
        return get_stashdb_client()
    if provider == "theporndb":
        return get_theporndb_client()
    factory = get_metadata_provider_factories().get(provider)
    return factory() if factory else None


def _provider_scene_duration(provider, scene_id):
    """Seconds the provider has for a scene (the search compares release sizes with it, v0.231.0), None when unknown."""
    client = get_provider_client(provider)
    if client is None or not hasattr(client, "scene_durations"):
        return None
    return (client.scene_durations([scene_id]) or {}).get(str(scene_id))


indexer_search.set_duration_resolver(_provider_scene_duration)


_details_backfill_lock = threading.Lock()


def run_details_backfill():
    """One-off fill of `release.details` (2026-09-26) for 'new' releases
    stored before descriptions were recorded -- one provider lookup per
    scene, paced by each client's own delay. Started in the background
    at startup; a no-op once nothing is missing (new rows get their
    description straight from the scan). A scene the provider no longer
    knows is stored as '' so it isn't retried forever."""
    if not _details_backfill_lock.acquire(blocking=False):
        return
    try:
        missing = release_repo.list_releases_missing_details(db)
        if not missing:
            return
        logger.info("details backfill: %d scene(s) without a stored description", len(missing))
        clients, done = {}, 0
        for provider, scene_id in missing:
            try:
                if provider not in clients:
                    clients[provider] = get_provider_client(provider)
                client = clients[provider]
                if client is None:
                    continue
                detail = client.get_scene_detail(scene_id)
                release_repo.set_details_for_scene(db, provider, scene_id, (detail or {}).get("details") or "")
                done += 1
                if done % 200 == 0:
                    logger.info("details backfill: %d/%d", done, len(missing))
                time.sleep(getattr(client, "delay", 0.3))
            except Exception:
                logger.exception("details backfill: failed for %s scene %s", provider, scene_id)
        logger.info("details backfill: finished, %d/%d filled", done, len(missing))
    finally:
        _details_backfill_lock.release()


_tags_backfill_lock = threading.Lock()
TAGS_BACKFILL_TPDB_SLICE = 100         # ThePornDB scenes per round (no multi-id lookup exists there: one request each)
TAGS_BACKFILL_TPDB_PAUSE = 10 * 60     # seconds between two rounds -- ~600 requests/hour; no limit found (probe 2026-10-03: 200 requests, all 200)


def run_tags_backfill():
    """v0.92.0: fills `release.tags` (and `studio_id`) of 'new' releases stored before tags were recorded. StashDB: all missing scenes at
    once but batched (25 scenes per request). ThePornDB has no batch lookup: slices of TAGS_BACKFILL_TPDB_SLICE scenes with a long pause
    between rounds, newest scenes first, until nothing is missing (a restart simply continues). A scene the provider no longer knows is
    stored with empty tags so it is not retried."""
    if not _tags_backfill_lock.acquire(blocking=False):
        return
    try:
        client = get_provider_client("stashdb")
        missing = release_repo.list_scenes_missing_tags(db, "stashdb") if client else []
        if missing:
            logger.info("tags backfill: %d StashDB scene(s), %d request(s)", len(missing), (len(missing) + 24) // 25)
            for i in range(0, len(missing), 200):
                part = missing[i:i + 200]
                try:
                    found = client.get_scenes_tags_batch(part)
                except Exception:
                    logger.exception("tags backfill: StashDB batch failed")
                    break
                for sid in part:
                    info = found.get(sid)
                    release_repo.set_tags_for_scene(db, "stashdb", sid, (info or {}).get("tags") or [], (info or {}).get("studio_id"))
            logger.info("tags backfill: StashDB done")
        client = get_provider_client("theporndb")
        skipped = set()  # failed this run (network): not retried in a loop, picked up again after the next restart
        while client:
            batch = [sid for sid in release_repo.list_scenes_missing_tags(db, "theporndb", limit=TAGS_BACKFILL_TPDB_SLICE + len(skipped),
                                                                          images=True) if sid not in skipped][:TAGS_BACKFILL_TPDB_SLICE]
            if not batch:
                break
            logger.info("tags backfill: ThePornDB round of %d scene(s)", len(batch))
            for sid in batch:
                try:
                    detail = client.get_scene_detail(sid)
                    if detail is None:  # ThePornDB no longer knows the scene: no tags, and its thumbnail is useless
                        release_repo.set_tags_for_scene(db, "theporndb", sid, [], drop_image=True)
                    else:
                        release_repo.set_tags_for_scene(db, "theporndb", sid, detail.get("tags") or [],
                                                        (detail.get("studio") or {}).get("id"), image_url=detail.get("image_url"))
                except Exception:
                    logger.exception("tags backfill: ThePornDB scene %s failed", sid)
                    skipped.add(sid)
                time.sleep(getattr(client, "delay", 0.5))
            time.sleep(TAGS_BACKFILL_TPDB_PAUSE)
        logger.info("tags backfill: finished")
    finally:
        _tags_backfill_lock.release()


def get_theporndb_client():
    """ThePornDB is a plain REST API (Bearer-token auth), not a
    stash-box GraphQL endpoint like StashDB -- see
    theporndb_client.py's module docstring. Only used for the
    connection-check button today; a real ThePornDB scan/import feature
    would need real query methods added there once actually built."""
    return ThePornDBClient(
        get_setting_value(db, "theporndb_url", settings.theporndb_url),
        get_setting_value(db, "theporndb_api_key", settings.theporndb_api_key),
    )


# Both the scheduled background loop and a manual "Run scan now" call
# this same function, each from its own thread (FastAPI runs sync route
# handlers in a thread-pool executor, same as asyncio.to_thread) -- with
# no guard, they can genuinely run concurrently, e.g. the background
# loop's immediate first tick at container startup racing a manual
# trigger fired moments later. Confirmed live 2026-09-11: this produced
# two threads scanning the same 408-entity watchlist at once, each
# racing the other to read/set the same watermark rows -- not a data
# safety issue (SQLite handles the raw writes fine, INSERT OR IGNORE
# stops duplicate release rows), but it doubles the real StashDB API
# load for no benefit and makes whichever call's summary you happen to
# be looking at misleading (it may report 0 new baselines/releases
# simply because the OTHER concurrent run already claimed them a moment
# earlier). A non-blocking lock turns a would-be second concurrent scan
# into a clean no-op instead.
_scan_lock = threading.Lock()


def _auto_handoff_release(entity_type, entity_stashdb_id, scene_stashdb_id):
    """Wired into watcher.run_scan() as its auto_handoff_callback (see
    PROGRESS.md's "Auto hand-off to Whisparr, per entity" section,
    2026-09-13) -- this is the only place that actually knows how to
    reach Whisparr and resolve the current quality-profile settings,
    same reasoning as get_whisparr_client()/get_stash_client() above
    being main.py's job rather than watcher.py's. Always provider=
    'stashdb': watcher.py only ever calls this for stashdb-sourced
    releases (see its docstring), matching handoff.send_to_whisparr()'s
    own provider constraint."""
    whisparr_quality_profile_id, whisparr_vr_quality_profile_id = resolve_whisparr_quality_profiles(db)
    quality_profile, vr_search_enabled = resolve_quality_profiles(db)
    result = hand_off(
        db, get_whisparr_client(), "stashdb", scene_stashdb_id, entity_type, entity_stashdb_id,
        whisparr_quality_profile_id, whisparr_vr_quality_profile_id, settings.whisparr_root_folder_path,
        quality_profile, vr_search_enabled, search_source=get_auto_hand_off_source(),
        get_provider_client=get_provider_client, get_stash_client=get_stash_client,
        on_start=lambda already_sent: history.scene("info", "wishlist", f"Added to the wishlist automatically (watched {entity_type})",
                                                    "stashdb", scene_stashdb_id),
    )
    if result.get("already_in_library"):
        return result
    logger.info("auto hand-off: put scene %s (%s %s) on the wishlist", scene_stashdb_id, entity_type,
                entity_stashdb_id)
    return result


def _auto_catalog_for_added(added):
    """Setting catalog_on_watch = 'auto': newly watched StashDB / added stash-box entries get their catalog loaded in the background, one entity after the other."""
    if get_setting_value(db, "catalog_on_watch", "off") != "auto":
        return
    todo = [a for a in added if a.get("provider") != "theporndb" and a.get("stashdb_id")]
    if not todo:
        return

    def run():
        from routers.backlog import load_catalog
        for a in todo:
            load_catalog(a["provider"], a["entity_type"], a["stashdb_id"], a.get("name", ""))
    threading.Thread(target=run, daemon=True, name="catalog-on-watch").start()


def sync_and_scan():
    """Watchlist membership follows Stash's Favorite flag or the
    watchlist tag (see watchlist_source.py), not manual editing (see
    PROGRESS.md's "Favorite-driven watchlist" section, 2026-09-11) --
    sync runs immediately before every scan, scheduled or manual, so a
    scan always reflects the current watched set. A failed sync (e.g.
    the watchlist tag missing in tag mode) aborts the scan rather than
    scanning a stale or emptied list."""
    if not _scan_lock.acquire(blocking=False):
        logger.info("sync_and_scan: a scan is already in progress, skipping this trigger")
        return {"skipped": True, "reason": "a scan is already in progress"}
    try:
        set_scan_progress("syncing")
        try:
            synced = sync_watchlist_from_favorites(db, get_stash_client(), get_theporndb_client())
            _auto_catalog_for_added(synced.get("added") or [])
        except Exception:
            set_scan_progress("idle")  # don't leave the progress stuck on "syncing"
            raise
        delay = _get_setting("stashdb_query_delay")
        clients = {"stashdb": get_stashdb_client(), "theporndb": get_theporndb_client(),
                   **get_metadata_provider_clients()}
        # 'feed' (default since 2026-09-25) or the original 'per_entry'
        # scan, see routers/settings.py's scan_mode / watcher.run_feed_scan().
        scan = run_feed_scan if get_setting_value(db, "scan_mode", DEFAULTS["scan_mode"]) == "feed" else run_scan
        result = _logged("scan", lambda: scan(
            db, clients, delay,
            progress_callback=lambda current, total, name: set_scan_progress(
                "scanning", current, total, name,
            ),
            auto_handoff_callback=_auto_handoff_release,
        ))
        set_scan_progress("idle")
        _mark_ran("scan")
        return result
    finally:
        _scan_lock.release()


# Separate lock from _scan_lock -- an overlapping StashDB-favorites
# import and a scan are unrelated operations (different StashDB query
# types, different local Stash mutations), only a second concurrent run
# of *this same* operation needs guarding against, same reasoning as
# _scan_lock above.
_stashdb_favorites_lock = threading.Lock()


def run_stashdb_favorites_import():
    """Imports the user's StashDB.org account-level favorites into this
    Stash instance's local favorite flag, for whichever of them already
    exist locally (see PROGRESS.md's "StashDB.org favorites import"
    section, 2026-09-11) -- one directional, StashDB -> local Stash,
    never the reverse and never removes a local favorite."""
    if not _stashdb_favorites_lock.acquire(blocking=False):
        logger.info("run_stashdb_favorites_import: already in progress, skipping this trigger")
        return {"skipped": True, "reason": "a StashDB favorites import is already in progress"}
    try:
        result = _logged("stashdb_favorites", lambda: import_stashdb_favorites(get_stashdb_client(), get_stash_client()))
        _mark_ran("stashdb_favorites")
        return result
    finally:
        _stashdb_favorites_lock.release()


# Separate lock from the others above -- same reasoning, guards only
# against a second concurrent run of this specific check.
_whisparr_blocklist_sync_lock = threading.Lock()


def run_whisparr_blocklist_sync():
    """Pulls Whisparr's entire blocklist into this sidecar's own local
    `review_rejected_titles` (2026-09-23) -- see
    whisparr_blocklist_sync.py's own module docstring for why this has
    to be a periodic pull, not a live lookup at search time."""
    if not _whisparr_blocklist_sync_lock.acquire(blocking=False):
        logger.info("run_whisparr_blocklist_sync: already in progress, skipping this trigger")
        return {"skipped": True, "reason": "a Whisparr blocklist sync is already in progress"}
    try:
        result = _logged("whisparr_blocklist_sync", lambda: sync_whisparr_blocklist(db, get_whisparr_client()))
        _mark_ran("whisparr_blocklist_sync")
        return result
    finally:
        _whisparr_blocklist_sync_lock.release()


class InactiveCheckJob:
    """The "probably inactive" check (2026-09-26, inactive_check.py):
    run() blocks (the periodic loop), start() runs it in the background
    for the Settings page's "Check now" and returns at once; status()
    reports progress. One run at a time."""

    def __init__(self):
        self._lock = threading.Lock()
        self._status = {"running": False, "phase": None}

    def status(self):
        return dict(self._status)

    def run(self):
        if not self._lock.acquire(blocking=False):
            return {"skipped": True, "reason": "an inactive check is already running"}
        try:
            self._status = {"running": True, "phase": "starting"}
            months = int(get_setting_value(db, "inactive_after_months", DEFAULTS["inactive_after_months"]) or 18)
            try:
                theporndb_client = get_theporndb_client()
            except Exception:
                theporndb_client = None
            return _logged("inactive_check", lambda: inactive_check.run_check(
                db, get_stashdb_client(), theporndb_client, months,
                progress=lambda phase: self._status.update(phase=phase),
            ))
        finally:
            self._status = {"running": False, "phase": None}
            self._lock.release()

    def start(self):
        if self._status["running"]:
            return {"started": False, "reason": "already running", **self.status()}
        threading.Thread(target=self.run, daemon=True).start()
        return {"started": True}


inactive_job = InactiveCheckJob()


def _stash_path_to_local(path):
    """Stash/Whisparr library path (/data/...) -> this sidecar's mount."""
    remote_root = get_setting_value(db, "whisparr_root_folder_path", settings.whisparr_root_folder_path).rstrip("/")
    local_mount = get_setting_value(db, "local_media_mount", settings.local_media_mount).rstrip("/")
    return local_mount + path[len(remote_root):] if remote_root and path.startswith(remote_root) else path


vr_job = vr_check.VrCheckJob(db, get_stash_client, get_provider_client, _stash_path_to_local)
duration_job = duration_check.DurationCheckJob(db, get_stash_client, get_provider_client)
upgrade_library_check_job = upgrade_library_check.UpgradeLibraryCheck(
    db, get_stash_client, duration_job, lambda k, d: get_setting_value(db, k, d), lambda: _cutoff_resolution())


def _stashdb_or_none():
    """StashDB's client, or None when no StashDB key is set (v0.243.2): the library tools then work with ThePornDB alone instead of failing on every StashDB call."""
    client = get_stashdb_client()
    return client if (client.session.headers.get("ApiKey") or "").strip() else None


def _shared_fingerprint_hint(scene, file, provider_scene_id, found, client):
    """verification.shared_hook (v0.246.2): a verified download whose file is registered at several provider scenes. If the length does not single the
    scene out, it goes on the Shared fingerprints list and into History as a hint; the import itself is not stopped. Scenes that only have a SIMILAR picture
    hash do not count (shared_fingerprint.split_exact)."""
    import shared_fingerprint
    local = [(fp["type"], fp["value"]) for fp in file.get("fingerprints") or [] if fp.get("type") in ("oshash", "phash", "md5")]
    exact, near = shared_fingerprint.split_exact(client, local, found, provider_scene_id)
    if len(exact) < 2:
        return
    try:
        durations = client.scene_durations([f["id"] for f in exact]) if hasattr(client, "scene_durations") else {}
    except Exception:
        durations = {}
    verdict = shared_fingerprint.judge(file.get("duration"), provider_scene_id, exact, durations,
                                       kept=shared_fingerprint.is_kept(db, scene["id"], provider_scene_id))
    if verdict["ok"]:
        return
    provider = _organizer().provider_of_endpoint(getattr(client, "url", None)) or "stashdb"
    shared_fingerprint.upsert(db, scene["id"], scene.get("title"), file["path"], file.get("duration"), provider, provider_scene_id,
                              verdict["reason"], verdict["how"], shared_fingerprint.enrich(client, exact, durations, near))
    history.add("warning", "history", "import", f"Fingerprint is not unique: {verdict['how']} -- look at it under Maintenance > Organize library > Shared fingerprints",
                scene_id=str(scene["id"]), scene_title=scene.get("title"), details={"path": file["path"], "candidates": [f["id"] for f in exact]}, dedupe_minutes=24 * 60)


verification.shared_hook = _shared_fingerprint_hint


def _extra_stashboxes():
    """The enabled user-added stash-boxes as the library tools take them (v0.244.0): [{"key", "name", "endpoint", "client"}]."""
    return [{"key": p["key"], "name": p["name"], "endpoint": p["url"], "client": StashDBClient(p["api_key"], url=p["url"])}
            for p in metadata_provider_repo.list_all(db, enabled_only=True)]


def _organizer():
    """Maintenance > Organize library (2026-09-28, library_organize.py)."""
    def setting(key):
        fallback = {"whisparr_root_folder_path": settings.whisparr_root_folder_path}.get(key, DEFAULTS.get(key, ""))
        return get_setting_value(db, key, fallback)
    try:
        theporndb_client = get_theporndb_client()
    except Exception:
        theporndb_client = None
    return library_organize.Organizer(db, get_stash_client(), _stashdb_or_none(), theporndb_client,
                                      _stash_path_to_local, setting, get_whisparr_client=get_whisparr_client,
                                      extra_providers=_extra_stashboxes())
def _importer():
    """Maintenance > Import folder (2026-09-29, library_import.py)."""
    def setting(key):
        fallback = {"whisparr_root_folder_path": settings.whisparr_root_folder_path}.get(key, DEFAULTS.get(key, ""))
        return get_setting_value(db, key, fallback)
    try:
        theporndb_client = get_theporndb_client()
    except Exception:
        theporndb_client = None
    importer = library_import.Importer(db, get_stash_client(), _stashdb_or_none(), theporndb_client,
                                       _stash_path_to_local, setting, get_whisparr_client=get_whisparr_client,
                                       recycle=_recycle_library_file, extra_providers=_extra_stashboxes())
    importer.backup_dir = os.path.join(settings.data_dir, "backups")
    return importer


def _movies():
    """Maintenance > Movies & groups lists (2026-09-29, library_movies.py)."""
    return library_movies.Movies(get_stash_client(), lambda key: get_setting_value(db, key, DEFAULTS.get(key, "")))


vr_check.reapply_thresholds(db)  # thresholds may have changed since the last run


def _recycle_library_file(local_path):
    _file_importer_module._recycle_existing_target(
        local_path, get_setting_value(db, "local_media_mount", settings.local_media_mount),
        get_setting_value(db, "recycle_bin_path", DEFAULTS["recycle_bin_path"]))


def _after_import(scene_id, new_path=None):
    """Post-import (own thread): VR check of the newly imported file only;
    a flat/mono file of a VR scene gets our download blocklisted + an
    upgrade candidate (user decision 2026-09-26, upgrades.flag_2d_import())."""
    verdicts = vr_job.check_scene(scene_id, only_path=new_path) or []
    if any(v in ("mono", "vr_mono") for v in verdicts):
        upgrades.flag_2d_import(db, get_stash_client(), scene_id,
                                blocklist=lambda release: _file_importer_module.blocklist_download(db, release),
                                only_path=new_path)


_file_importer_module.after_import_hook = _after_import
# Upgrade downloads retire the file they replace (upgrades.on_upgrade_imported()).
_file_importer_module.upgrade_import_hook = lambda provider, stash_id, path, verified: upgrades.on_upgrade_imported(
    db, get_stash_client(), _stash_path_to_local, _recycle_library_file, provider, stash_id, path, verified,
    after_import=lambda scene_id, new_path: threading.Thread(target=_after_import, args=(scene_id, new_path),
                                                             daemon=True).start())
_file_importer_module.upgrade_is_pending = lambda provider, stash_id: upgrades.is_upgrade(db, provider, stash_id)


def _cutoff_resolution():
    profile, _ = resolve_quality_profiles(db)
    return (profile or {}).get("cutoff_resolution")


def _upgrade_search_due():
    """The upgrade search runs at most every `upgrade_search_interval_hours` (default 12) -- it rides on
    the indexer-search loop, which also serves the wishlist and runs far more often."""
    try:
        hours = float(get_setting_value(db, "upgrade_search_interval_hours", "12") or 12)
        last = get_setting_value(db, "upgrade_search_last_run", "")
        if not last:
            return True
        return (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds() >= hours * 3600 - 60
    except Exception:
        logger.exception("upgrade search due-check failed -- running it")
        return True


def _upgrade_search_mark_run():
    with db.connect() as conn:
        conn.execute("""INSERT INTO setting (key, value) VALUES ('upgrade_search_last_run', ?)
                        ON CONFLICT (key) DO UPDATE SET value = excluded.value""",
                     (datetime.now(timezone.utc).isoformat(),))


def run_upgrade_search(cand_id, dry_run=True):
    profile, vr_enabled = resolve_quality_profiles(db)
    return upgrades.search_one(db, cand_id, get_provider_client, get_search_source(), profile, vr_enabled,
                               dry_run=dry_run)


def readd_library_scene(stash_scene_id, reason, provider_duration=None, search=True):
    """Fetch a scene again although Stash has a file for it (v0.209.0; Search page "Re-add", Maintenance > Duration mismatch): an upgrade candidate
    of the user's own (reason 'readd' / 'duration'). search=False only queues it (the scheduled upgrade search finds it); search=True also
    searches now -- waits 20 s like the wishlist add, a slower search goes on in the background."""
    res = upgrades.add_manual_candidate(db, get_stash_client(), stash_scene_id, reason, provider_duration)
    if "error" in res:
        return res
    cand_id = res["id"]
    cand = upgrades.get_candidate(db, cand_id)
    if cand["status"] == "grabbed":
        return {"error": "a download for this file is already running", "candidate_id": cand_id}
    if cand["status"] != "wanted":
        upgrades.set_candidate(db, cand_id, status="wanted", last_search_at=None, last_search_result=None, review_candidate=None)
    if not search:
        return {"queued": True, "candidate_id": cand_id}
    out = _handoff_module.search_with_wait(("readd", cand_id), lambda: run_upgrade_search(cand_id, dry_run=False))
    if out.get("running"):
        return {"queued": True, "candidate_id": cand_id, "search_running": True}
    if out.get("error"):
        return {"queued": True, "candidate_id": cand_id, "search_error": out["error"]}
    return {"queued": True, "candidate_id": cand_id, "grabbed": out.get("grab"), "search_result": out.get("result")}


def _fix_wrong_download_for(provider, scene_stashdb_id, entity_type, entity_stashdb_id, file_path=None):
    return _file_importer_module.fix_wrong_download(
        db, get_stash_client(), get_whisparr_client(), provider, scene_stashdb_id, entity_type, entity_stashdb_id,
        get_setting_value(db, "whisparr_root_folder_path", settings.whisparr_root_folder_path),
        get_setting_value(db, "local_media_mount", settings.local_media_mount),
        get_setting_value(db, "recycle_bin_path", DEFAULTS["recycle_bin_path"]),
        file_path=file_path,
    )


# Separate lock from the others above -- same reasoning, guards only
# against a second concurrent run of this specific check.
_theporndb_identify_lock = threading.Lock()


def run_theporndb_identify():
    """Batch-matches currently-favorited Studios/Performers against
    ThePornDB via Stash's own stash-box tagging mechanism (see
    theporndb_identify.py and PROGRESS.md's "ThePornDB identify sync"
    section, 2026-09-11) -- this is what actually populates the
    ThePornDB `stash_ids` the watchlist sync (sync.py) and scan
    (watcher.py) depend on."""
    if not _theporndb_identify_lock.acquire(blocking=False):
        logger.info("run_theporndb_identify: already in progress, skipping this trigger")
        return {"skipped": True, "reason": "a ThePornDB identify run is already in progress"}
    try:
        delay = _get_setting("theporndb_identify_delay_seconds")
        stash_client = get_stash_client()
        result = _logged("theporndb_identify", lambda: identify_favorites_against_theporndb(
            stash_client, delay_seconds=delay,
            fetch_watched=lambda entity_type: watchlist_source.fetch_watched(db, stash_client, entity_type),
        ))
        if not result.get("postponed"):  # a postponed run stays "due": the hourly check tries again
            _mark_ran("theporndb_identify")
        return result
    finally:
        _theporndb_identify_lock.release()


def promote_imported_sibling(db, existing, scene, provider, entity_type, entity_stashdb_id):
    """StashDB caught up with a scene that was imported via ThePornDB (watcher dedup, v0.130.0). When the StashDB scene's own fingerprints
    match the imported file: the local scene gets the StashDB id too (the ThePornDB id stays, title/studio untouched), the ThePornDB rows
    of this scene go, and the StashDB row of this entity is stored as 'imported' (same imported_at). Returns True when done."""
    import verification
    if existing["provider"] != "theporndb" or provider != "stashdb":
        return False
    stash, stashdb = get_stash_client(), get_stashdb_client()
    if stashdb is None:
        return False
    local = stash.find_scene_by_stashdb_id(existing["scene_stashdb_id"], endpoint=stash.theporndb_endpoint)
    if not local:
        return False
    files = stash._graphql("query($i: ID!) { findScene(id: $i) { files { path } } }", {"i": str(local["id"])})["findScene"]["files"]
    verified = any(verification.verify_downloaded_file(stash, stashdb, scene["id"], f["path"])[0] is True for f in files)
    if not verified:
        return False
    note = os.path.join(settings.data_dir, f"promote_{local['id']}_{datetime.now().strftime('%Y%m%d-%H%M%S')}.json")
    before = stash._graphql("query($i: ID!) { findScene(id: $i) { stash_ids { endpoint stash_id } } }", {"i": str(local["id"])})["findScene"]["stash_ids"]
    with open(note, "w", encoding="utf-8") as fh:
        json.dump({"scene": local["id"], "stash_ids_before": before, "theporndb_row": {k: existing.get(k) for k in ("scene_stashdb_id", "status", "imported_at")},
                   "stashdb_scene": scene["id"]}, fh, indent=1)
    if not os.path.exists(note) or os.path.getsize(note) == 0:
        return False
    stash.add_scene_stash_id(local["id"], stash.stashdb_endpoint, scene["id"])
    with db.connect() as conn:
        release_repo.insert_release(db, conn, scene, provider, entity_type, entity_stashdb_id, status="new")
        conn.execute("DELETE FROM release WHERE provider = 'theporndb' AND scene_stashdb_id = ?", (existing["scene_stashdb_id"],))
        conn.execute("UPDATE release SET status = 'imported', imported_at = ?, grabbed_title = ?, grabbed_at = ? "
                     "WHERE provider = 'stashdb' AND scene_stashdb_id = ?",
                     (existing.get("imported_at"), existing.get("grabbed_title"), existing.get("grabbed_at"), scene["id"]))
    history.event("info", "import", f"{scene.get('title')}: StashDB caught up -- the imported ThePornDB scene now carries the StashDB id too",
                  details={"stash_scene": local["id"], "stashdb": scene["id"], "backup": os.path.basename(note)})
    return True


watcher.PROMOTE_HOOK = promote_imported_sibling


search_sources.set_protocol_checker(lambda protocol: download_client_repo.pick_for_protocol(db, protocol) is not None)


def get_film_search_source():
    """The source of the automatic movie search: the configured `search_backend` when it can search by query, else the first sidecar source."""
    src = get_search_source()
    if src is not None and hasattr(src, "search") and not src.__class__.__name__.startswith("Unavailable"):
        return src
    return next(iter(get_manual_search_sources().values()), None)


_film_search_lock = threading.Lock()


def run_film_search():
    """Searches the due movies of the Movies wishlist (film_search.run): at most `film_search_per_run` movies, each only every
    `film_search_min_days` days. Candidates wait in Activity > Needs review."""
    if not _film_search_lock.acquire(blocking=False):
        return {"skipped": True, "reason": "a movie search is already in progress"}
    try:
        try:  # first: a movie whose empty Stash Group was deleted is not wanted any more (film_import.forget_dead_groups)
            film_import.forget_dead_groups(db, get_stash_client())
        except Exception as e:  # noqa: BLE001
            logger.warning("forget_dead_groups failed: %s", e)
        try:  # then: a movie whose Group has a file now needs no download (film_import.adopt_filled_groups)
            film_import.adopt_filled_groups(db, get_stash_client(), film_import.min_film_resolution(db))
        except Exception as e:  # noqa: BLE001
            logger.warning("adopt_filled_groups failed: %s", e)
        result = _logged("film_search", lambda: film_search.run(db, get_film_search_source, int(_get_setting("film_search_per_run")),
                                                                 _get_setting("film_search_min_days"),
                                                                 int(_get_setting("film_search_recent_days")),
                                                                 _get_setting("film_search_recent_min_days"),
                                                                 stash=get_stash_client(), min_res=film_import.min_film_resolution(db)))
        _mark_ran("film_search")
        return result
    finally:
        _film_search_lock.release()


_film_feed_lock = threading.Lock()


def run_film_feed():
    """New movies of the watched ThePornDB studios -> Movies wishlist > Suggestions (film_feed.run): ONE feed request per run."""
    if not _film_feed_lock.acquire(blocking=False):
        return {"skipped": True, "reason": "a movie feed run is already in progress"}
    try:
        client = get_theporndb_client()
        client.timeout = 60  # deep feed pages are slow on ThePornDB (15 s timed out at page 5)
        stash = get_stash_client()
        result = _logged("film_feed", lambda: film_feed.run(db, client, lambda: film_feed.stash_groups(stash),
                                                            int(_get_setting("inactive_after_months")),
                                                            int(_get_setting("film_feed_max_age_days"))))
        _mark_ran("film_feed")
        return result
    finally:
        _film_feed_lock.release()


_duplicate_check_lock = threading.Lock()


def run_duplicate_check():
    """Has StashDB merged/deleted one of the recorded duplicate entries (scene_alias, kind 'duplicate_entry')? One bundled request."""
    if not _duplicate_check_lock.acquire(blocking=False):
        return {"skipped": True, "reason": "a duplicate check is already in progress"}
    try:
        client = get_stashdb_client()
        if client is None:
            return {"skipped": True, "reason": "StashDB is not configured"}
        result = _logged("duplicate_check", lambda: scene_alias.check_duplicates(db, client))
        for r in result["changed"]:
            history.event("warning", "job", f"StashDB {'merged' if r['status'] == 'merged' else 'removed'} a recorded duplicate entry: "
                          f"{r.get('title') or r['alias_scene_id'][:8]} (local scene {r['local_scene_id']}) -- clean it up in Maintenance > Review > Alias ids",
                          details={"alias": r["alias_scene_id"], "partner": r.get("partner_scene_id"), "merged_into": r.get("merged_into")})
        _mark_ran("duplicate_check")
        return result
    finally:
        _duplicate_check_lock.release()


# Separate lock from the others above -- same reasoning, guards only
# against a second concurrent run of this specific check.
_import_check_lock = threading.Lock()


def run_import_check():
    """Closes the loop on a hand-off: send_to_whisparr() is fire-and-
    forget (the download/import happens on Whisparr's own time), so
    this is what actually confirms a "sent_to_whisparr" release turned
    into a real file. Added 2026-09-11 after manually verifying a real
    test hand-off's outcome by hand and realizing there was no way to
    see that result from this project's own UI/DB otherwise -- see
    api/import_check.py and PROGRESS.md."""
    if not _import_check_lock.acquire(blocking=False):
        logger.info("run_import_check: already in progress, skipping this trigger")
        return {"skipped": True, "reason": "an import check is already in progress"}
    try:
        if not whisparr_configured(db):
            return {"skipped": True, "reason": "Whisparr is not configured"}
        return check_import_status(db, get_whisparr_client(), get_stash_client(), get_stashdb_client())
    finally:
        _import_check_lock.release()


# Separate lock from the others above -- same reasoning, guards only
# against a second concurrent run of this specific check.
_file_import_lock = threading.Lock()


def on_scene_created(scene_id):
    """Stash Scene.Create.Post hook -> file_importer.verify_created_scene()
    (2026-09-25). Deliberately NOT under _file_import_lock: it must not
    wait behind a long running import (the whole point is reacting
    immediately), and file_importer's own _finalize_lock already
    serializes the one step both could do at once."""
    return verify_created_scene(db, get_stash_client(), get_stashdb_client(), scene_id,
                                get_provider_client=get_provider_client)


# Set when a SABnzbd completion arrives while an import run is already
# in progress (2026-09-25) -- that run may have listed SABnzbd's
# completed jobs before this one finished, so run_file_import() starts
# one more pass right after it releases the lock instead of leaving the
# download for the 15-min safety-net poll.
_file_import_rerun = threading.Event()


def on_download_complete(nzo_id, status=None):
    """SABnzbd completion webhook (2026-09-25, replaces waiting for the
    next 15-min poll -- see routers/file_import.py's download_complete()
    and sabnzbd/srw_notify.py). Ignores jobs this sidecar didn't
    grab; for ours, starts an import run in the background and returns
    at once, so SABnzbd's post-processing script isn't held up. The
    periodic _file_import_loop stays as the safety net."""
    if not nzo_id or not (release_repo.is_known_download_job(db, nzo_id) or film_repo.is_known_job(db, nzo_id)):
        return {"ours": False}
    logger.info("SABnzbd reports job %s finished (status %s), importing now", nzo_id, status)
    scene = release_repo.find_scene_by_download_job(db, nzo_id)
    if scene:
        import_progress.set_phase(*scene, "waiting_import")  # shown on Activity until import_one() starts
        if str(status or "0") == "0":
            history.scene("info", "download", "Download finished (SABnzbd)", *scene)
        else:
            history.scene("error", "download", f"Download failed in SABnzbd (status {status})", *scene)

    def _worker():
        # SABnzbd runs the post-processing script while the job is still
        # IN post-processing -- its history entry says "Running", not
        # "Completed", until the script returns (found live 2026-09-25,
        # the first real webhook: the import it started saw nothing to
        # import). So wait for this one job to be marked done (10s steps,
        # max 5 min) before importing -- short and tied to the event,
        # not a standing poll.
        _wait_for_sabnzbd_job_done(nzo_id)
        result = run_file_import()
        if result.get("skipped"):
            _file_import_rerun.set()

    threading.Thread(target=_worker, daemon=True).start()
    return {"ours": True, "import": "started"}


def _import_after_encode():
    """A finished (or failed) re-encode: import it right away instead of waiting for the next safety-net tick (v0.219.0)."""
    def _worker():
        result = run_file_import()
        if result.get("skipped"):
            _file_import_rerun.set()
    threading.Thread(target=_worker, daemon=True).start()


_file_importer_module.on_encode_done = _import_after_encode


def _wait_for_sabnzbd_job_done(nzo_id, step=10, max_wait=300):
    """Blocks (on the webhook's own background thread) until SABnzbd's
    history reports this job as Completed/Failed, or `max_wait` passes
    -- then the import runs either way (the safety-net poll covers a
    job that takes even longer). Best-effort: any error just ends the
    wait early."""
    deadline = time.time() + max_wait
    try:
        release = release_repo.find_by_download_job_id(db, nzo_id)
        client_row = download_client_repo.get_one(db, release["download_client_id"]) if release else None
        if client_row is None or client_row["type"] != "sabnzbd":
            return
        client = SabnzbdClient(client_row["url"], client_row["api_key"])
        while time.time() < deadline:
            job = client.find_history_by_nzo_id(nzo_id)
            if job and job.get("status") in ("Completed", "Failed"):
                return
            time.sleep(step)
        logger.info("SABnzbd job %s still not marked done after %ss, importing anyway", nzo_id, max_wait)
    except Exception:
        logger.exception("waiting for SABnzbd job %s failed, importing anyway", nzo_id)


def _film_cfg():
    return {
        "sab_remote_root": get_setting_value(db, "sabnzbd_downloads_root_path", settings.sabnzbd_downloads_root_path),
        "sab_local_mount": get_setting_value(db, "local_downloads_mount", settings.local_downloads_mount),
        "stash_root": get_setting_value(db, "whisparr_root_folder_path", settings.whisparr_root_folder_path),
        "local_media_mount": get_setting_value(db, "local_media_mount", settings.local_media_mount),
        "use_hardlinks": get_setting_value(db, "use_hardlinks", DEFAULTS["use_hardlinks"]) == "true",
        "recycle_bin_path": get_setting_value(db, "recycle_bin_path", DEFAULTS["recycle_bin_path"]),
        "movie_folder_format": get_setting_value(db, "movie_folder_format", DEFAULTS["movie_folder_format"]),
        "movie_naming_format": get_setting_value(db, "movie_naming_format", DEFAULTS["movie_naming_format"]),
        "naming_options": file_naming.naming_options(lambda key: get_setting_value(db, key, "[]")),
        "set_permissions": get_setting_value(db, "set_permissions", DEFAULTS["set_permissions"]) == "true",
        "chmod_folder": get_setting_value(db, "chmod_folder", DEFAULTS["chmod_folder"]),
        "chown_group": get_setting_value(db, "chown_group", DEFAULTS["chown_group"]),
    }


_film_whisparr_lock = threading.Lock()
_film_whisparr_state = {"running": False, "dry_run": None, "result": None, "error": None, "at": None}


def _whisparr_usable():
    """Whisparr counts only when it is configured (url + key) and the scope preset is not 'no_whisparr' -- otherwise it is ignored completely."""
    return (bool(get_setting_value(db, "whisparr_url", settings.whisparr_url)) and bool(get_setting_value(db, "whisparr_api_key", settings.whisparr_api_key))
            and resolve_whisparr_scope_preset(db) != "no_whisparr")


def run_film_whisparr_sync(dry_run=False, all_movies=False):
    """Registers / links the movies of the wishlist in Whisparr per the Whisparr-scope preset (film_whisparr.py)."""
    if not _film_whisparr_lock.acquire(blocking=False):
        return {"skipped": True, "reason": "a Whisparr sync of the movies is already running"}
    try:
        _film_whisparr_state.update(running=True, dry_run=dry_run, error=None, result=None)
        if not _whisparr_usable():
            result = {"skipped": "Whisparr is not configured or the preset is no_whisparr -- Whisparr is ignored", "dry_run": dry_run}
        else:
            normal_profile, _vr = resolve_whisparr_quality_profiles(db)
            result = film_whisparr.run(db, get_whisparr_client(), resolve_whisparr_scope_preset(db), normal_profile,
                                       get_setting_value(db, "whisparr_root_folder_path", settings.whisparr_root_folder_path), dry_run=dry_run, all_movies=all_movies)
            if not dry_run and get_setting_value(db, "film_library_auto", "false") == "true":
                try:  # stage D: movies whose file is already in the library -- only the clear cases, only with the setting on
                    result["library"] = library_apply_clear()
                except Exception:  # noqa: BLE001
                    logger.exception("movie library adoption after the Whisparr sync failed")
        _film_whisparr_state.update(result=result, at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        return result
    except Exception as e:  # noqa: BLE001
        logger.exception("movie Whisparr sync failed")
        _film_whisparr_state["error"] = f"{type(e).__name__}: {e}"
        return {"error": _film_whisparr_state["error"]}
    finally:
        _film_whisparr_state["running"] = False
        _film_whisparr_lock.release()


_library_lock = threading.Lock()


def library_check():
    """Stage D: the wanted movies whose file is already in the library, as {"clear": [...], "manual": [...], "other": n} (one list request to Whisparr
    at most, cached 10 minutes; the rest is local). Nothing is written."""
    if not _whisparr_usable():
        return {"error": "Whisparr is not configured or the preset is no_whisparr -- the library check needs Whisparr's file list"}
    return film_library.check(db, get_whisparr_client(), get_stash_client())


def library_apply(film_ids=None, only_clear=True, force=False):
    """Uses the existing file for the given wanted movies (or all clear ones). `force` is only for a movie the user looked at (length mismatch)."""
    if not _library_lock.acquire(blocking=False):
        return {"error": "a library adoption is already running"}
    try:
        found = library_check()
        if found.get("error"):
            return found
        pool = found["clear"] + ([] if only_clear else found["manual"])
        if film_ids is not None:
            wanted = {int(i) for i in film_ids}
            pool = [v for v in found["clear"] + found["manual"] if v["film_id"] in wanted]
        stash, results = get_stash_client(), []
        for verdict in pool:
            film = film_repo.get(db, verdict["film_id"])
            results.append(film_library.apply_one(db, stash, _movies(), get_provider_client, film, verdict, force=force and film_ids is not None))
        return {"applied": results, "skipped": len(found["clear"]) + len(found["manual"]) - len(pool)}
    finally:
        _library_lock.release()


def library_apply_clear():
    return library_apply(None, True, False)


_film_history_lock = threading.Lock()
_film_history_state = {"running": False, "dry_run": None, "result": None, "error": None, "at": None}


def run_film_whisparr_history(dry_run=False):
    """Stage C: takes over what Whisparr did for the movies (grab / import) from ONE history request (film_whisparr.poll_history). Only where Whisparr
    searches (search_sidecar_import, full_whisparr) and is configured."""
    if not _film_history_lock.acquire(blocking=False):
        return {"skipped": True, "reason": "a Whisparr history check of the movies is already running"}
    try:
        _film_history_state.update(running=True, dry_run=dry_run, error=None, result=None)
        if not _whisparr_usable():
            result = {"skipped": "Whisparr is not configured or the preset is no_whisparr -- Whisparr is ignored", "dry_run": dry_run}
        else:
            result = film_whisparr.poll_history(db, get_whisparr_client(), get_stash_client(), resolve_whisparr_scope_preset(db), dry_run=dry_run)
        _film_history_state.update(result=result, at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        return result
    except Exception as e:  # noqa: BLE001
        logger.exception("movie Whisparr history check failed")
        _film_history_state["error"] = f"{type(e).__name__}: {e}"
        return {"error": _film_history_state["error"]}
    finally:
        _film_history_state["running"] = False
        _film_history_lock.release()


def start_film_whisparr_history(dry_run=True):
    if _film_history_state["running"]:
        return {"error": "a Whisparr history check of the movies is already running"}
    threading.Thread(target=run_film_whisparr_history, args=(dry_run,), daemon=True).start()
    return {"ok": True, "dry_run": dry_run}


def kick_film_whisparr_history():
    """Hourly with the import check: only where Whisparr searches and movies are on, never while one is running."""
    if _film_history_state["running"] or not _scope_allows("movies") or not _whisparr_usable():
        return
    if resolve_whisparr_scope_preset(db) not in film_whisparr.HISTORY_SCOPES:
        return
    threading.Thread(target=run_film_whisparr_history, args=(False,), daemon=True).start()


_film_files_lock = threading.Lock()
_film_files_state = {"running": False, "dry_run": None, "result": None, "error": None, "at": None}


def run_film_whisparr_files(dry_run=False, all_movies=False):
    """Stage B: tells Whisparr the folder of imported movies and lets it take the file over (film_whisparr.sync_files). Only where this sidecar imports
    (bookkeeping_only, search_sidecar_import) and Whisparr is configured."""
    if not _film_files_lock.acquire(blocking=False):
        return {"skipped": True, "reason": "a folder report to Whisparr is already running"}
    try:
        _film_files_state.update(running=True, dry_run=dry_run, error=None, result=None)
        if not _whisparr_usable():
            result = {"skipped": "Whisparr is not configured or the preset is no_whisparr -- Whisparr is ignored", "dry_run": dry_run}
        else:
            result = film_whisparr.sync_files(db, get_whisparr_client(), get_stash_client(), resolve_whisparr_scope_preset(db),
                                              dry_run=dry_run, all_movies=all_movies)
        _film_files_state.update(result=result, at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        return result
    except Exception as e:  # noqa: BLE001
        logger.exception("movie folder report to Whisparr failed")
        _film_files_state["error"] = f"{type(e).__name__}: {e}"
        return {"error": _film_files_state["error"]}
    finally:
        _film_files_state["running"] = False
        _film_files_lock.release()


def start_film_whisparr_files(dry_run=True):
    """The manual catch-up (Maintenance > Movies & groups > Whisparr): ALL imported movies, as a dry run first."""
    if _film_files_state["running"]:
        return {"error": "a folder report to Whisparr is already running"}
    threading.Thread(target=run_film_whisparr_files, args=(dry_run, True), daemon=True).start()
    return {"ok": True, "dry_run": dry_run}


def kick_film_whisparr_files():
    """After a movie import (and on the file-import loop for retries): report the newly imported movies to Whisparr in the background -- ONLY with the
    setting `film_whisparr_file_auto` switched on (off until the user has seen the dry run), only the queued / due ones, never the older stock."""
    if get_setting_value(db, "film_whisparr_file_auto", "false") != "true" or _film_files_state["running"]:
        return
    if not _scope_allows("movies") or not _whisparr_usable() or not film_whisparr.pending_files(db):
        return
    threading.Thread(target=run_film_whisparr_files, args=(False, False), daemon=True).start()


def start_film_whisparr_sync(dry_run=True):
    """The manual catch-up (Maintenance > Movies & groups): ALL movies of the wishlist, as a dry run first."""
    if _film_whisparr_state["running"]:
        return {"error": "a Whisparr sync of the movies is already running"}
    threading.Thread(target=run_film_whisparr_sync, args=(dry_run, True), daemon=True).start()
    return {"ok": True, "dry_run": dry_run}


def kick_film_whisparr(film_ids=()):
    """Event hook (a movie went onto the wishlist): it is queued and told to Whisparr in the background -- only the new ones, never the old stock."""
    if film_ids and _whisparr_usable():
        film_whisparr.queue(db, film_ids)
    if _scope_allows("movies") and _whisparr_usable() and not _film_whisparr_state["running"]:
        threading.Thread(target=run_film_whisparr_sync, args=(False, False), daemon=True).start()


def run_film_import():
    """One pass of the film wishlist's import (film_import.py). Called from run_file_import()."""
    return film_import.run(db, get_stash_client(), _movies(), get_provider_client, _film_cfg(),
                           file_tools=(lambda p: _recycle_library_file(_stash_path_to_local(p)), _stash_path_to_local))


def run_file_import():
    """Milestone 2 (2026-09-17): imports/renames/moves a completed
    Whisparr download itself instead of trusting Whisparr's own
    (disabled) auto-import. See api/file_importer.py and
    api/download_sources.py."""
    if not _file_import_lock.acquire(blocking=False):
        logger.info("run_file_import: already in progress, skipping this trigger")
        return {"skipped": True, "reason": "a file import run is already in progress"}
    try:
        # 2026-09-19: which source depends on the file_import_source
        # switch (see routers/settings.py's DEFAULTS docstring for why
        # this is now a switch rather than always WhisparrQueueSource)
        # -- each source's items are in a different path namespace, so
        # the (source_remote_root, source_local_mount) pair passed to
        # the pipeline has to match.
        file_import_source = resolve_file_import_source(db)
        if file_import_source == "sidecar_sabnzbd":
            source = SabnzbdSource(db)
            source_remote_root = get_setting_value(db, "sabnzbd_downloads_root_path", settings.sabnzbd_downloads_root_path)
            source_local_mount = get_setting_value(db, "local_downloads_mount", settings.local_downloads_mount)
        else:
            source = WhisparrQueueSource(get_whisparr_client())
            source_remote_root = get_setting_value(db, "whisparr_root_folder_path", settings.whisparr_root_folder_path)
            source_local_mount = get_setting_value(db, "local_media_mount", settings.local_media_mount)
        result = _run_file_import_pipeline(
            db, get_whisparr_client(), get_stash_client(), get_stashdb_client(), source,
            source_remote_root, source_local_mount,
            get_setting_value(db, "whisparr_root_folder_path", settings.whisparr_root_folder_path),
            get_setting_value(db, "local_media_mount", settings.local_media_mount),
            get_setting_value(db, "scene_naming_format", DEFAULTS["scene_naming_format"]),
            get_setting_value(db, "scene_folder_format", DEFAULTS["scene_folder_format"]),
            get_setting_value(db, "use_hardlinks", DEFAULTS["use_hardlinks"]) == "true",
            get_setting_value(db, "recycle_bin_path", DEFAULTS["recycle_bin_path"]),
            get_setting_value(db, "set_permissions", DEFAULTS["set_permissions"]) == "true",
            get_setting_value(db, "chmod_folder", DEFAULTS["chmod_folder"]),
            get_setting_value(db, "chown_group", DEFAULTS["chown_group"]),
            get_provider_client=get_provider_client,
        )
        # Films of the film wishlist (v0.110.0): same trigger points as the scenes (loop, SABnzbd webhook, finished scan).
        try:
            result["films"] = run_film_import()
        except Exception:
            logger.exception("film import pass failed")
        try:  # stage B: report newly imported movies (and due retries) to Whisparr -- only with the setting on
            kick_film_whisparr_files()
        except Exception:
            logger.exception("movie folder report to Whisparr could not be started")
        try:  # movies not yet told to Whisparr (the event hook covers most; creators without a hook and failed tries are caught here)
            if _scope_allows("movies") and _whisparr_usable() and film_whisparr.pending(db):
                result["film_whisparr"] = run_film_whisparr_sync(False)
        except Exception:
            logger.exception("movie Whisparr sync (safety net) failed")
        try:  # torrents whose seeding goal is reached (only when the setting is on)
            result["torrent_cleanup"] = torrent_cleanup.run(db)
        except Exception:
            logger.exception("torrent cleanup failed")
        return result
    finally:
        _file_import_lock.release()
        if _file_import_rerun.is_set():
            _file_import_rerun.clear()
            threading.Thread(target=run_file_import, daemon=True).start()


# Separate lock from the others above -- same reasoning, guards only
# against a second concurrent run of this specific check.
_indexer_search_lock = threading.Lock()


def stuck_releases(pending):
    """Releases the 'Search all stuck now' button looks at: on the wishlist, searched before (a result is recorded), nothing grabbed. A scene that was
    found to be a duplicate of one we already have is left out -- there is nothing to find."""
    return [r for r in pending if r.get("last_search_result") and r["last_search_result"] != "duplicate_download"]


IMMEDIATE_TARGETED_PER_TICK = 10  # RSS mode: targeted searches for scenes older than the feed window, per run (the rest follows in the next run)


def _targeted_due(release):
    """RSS mode back-off: does this release get an indexer query of its own now? The feed covers everything
    that is posted from here on; a targeted search only helps for releases already older than the feed window.
    Daily during the first week after hand-off, weekly after that. The hand-off itself made one targeted
    search, so the clock starts at the last targeted search or at the hand-off."""
    from datetime import datetime as _dt
    last = release.get("last_targeted_search_at") or release.get("handoff_at") or release.get("discovered_at")
    try:
        last_dt = _dt.fromisoformat(str(last).replace("Z", "")).replace(tzinfo=timezone.utc)
        first = release.get("handoff_at") or release.get("discovered_at") or last
        age_days = (datetime.now(timezone.utc) - _dt.fromisoformat(str(first).replace("Z", "")).replace(tzinfo=timezone.utc)).days
    except (TypeError, ValueError):
        return False  # unknown timestamps: rely on the feed, never a query storm
    wait_hours = 24 if age_days < 7 else 168
    return (datetime.now(timezone.utc) - last_dt).total_seconds() >= wait_hours * 3600


def run_indexer_search(only_stuck=False):
    """Milestone 3 (2026-09-19): retries indexer search+grab for every
    'sent_to_whisparr' release that hasn't been grabbed yet -- the
    periodic companion to the one-shot attempt send_to_whisparr()
    already makes at hand-off time (see handoff.py). A release with
    nothing found the first time (e.g. not posted to an indexer yet)
    gets picked up again here on the next tick, until either a grab
    succeeds or the user manually falls back to Whisparr's own search
    (see fallback_to_whisparr()).

    2026-09-22 (Whisparr-scope presets): the above is only what happens
    under 'bookkeeping_only' (this sidecar is the one searching).
    - 'full_whisparr': Whisparr does its own search+grab+import
      entirely, this loop has nothing to do -- skip every such release
      outright rather than spending a Whisparr API call on each one
      every tick for no reason (its download_job_id will never be set
      by this sidecar, so it would otherwise show up here forever).
    - 'search_sidecar_import': Whisparr does its own search+grab, but
      this sidecar still does the file import -- instead of searching,
      check whether Whisparr has grabbed something yet
      (whisparr_client.find_grabbed_download_id(), which reads
      Whisparr's own /history and survives even after the item drops
      out of /queue) and, if so, record it exactly as if this sidecar
      had grabbed it itself (release_repo.set_download_job_for_scene())
      -- the existing SabnzbdSource/file_importer pipeline then picks
      it up unmodified on the next file-import tick."""
    if not _indexer_search_lock.acquire(blocking=False):
        logger.info("run_indexer_search: already in progress, skipping this trigger")
        return {"skipped": True, "reason": "an indexer search run is already in progress"}
    # only_stuck (button "Search all stuck now"): ONE targeted indexer query for every stuck release (searched before, nothing usable found), no
    # feed pull, no upgrade search; everything else about the grab stays as in the normal run.
    try:
        whisparr_client = get_whisparr_client()
        search_source = get_search_source()
        scope = resolve_whisparr_scope_preset(db)
        if scope == "full_whisparr":
            return {"grabbed": [], "skipped_scope": scope}
        quality_profile, vr_search_enabled = resolve_quality_profiles(db)
        grabbed = []
        pending = release_repo.list_sent_to_whisparr_without_grab(db)
        # RSS mode (2026-09-30): ONE blank-query pull of the newest releases per tick, searched locally;
        # a release only gets its OWN indexer query when its back-off says so (_targeted_due). If the
        # feed can't be pulled the tick is skipped -- never a fallback to hundreds of targeted queries.
        feed, targeted_used, immediate_targeted = None, 0, 0
        if only_stuck:
            pending = stuck_releases(pending)
        elif (get_setting_value(db, "search_mode", "polling") == "rss" and search_source is not None
                and getattr(search_source, "available", False)):
            try:
                feed = FeedSource.fetch(search_source)
            except Exception:
                logger.exception("run_indexer_search: RSS feed pull failed, skipping this tick")
                return {"grabbed": [], "feed_error": True}
            logger.info("run_indexer_search: RSS mode -- %d feed item(s), %d pending release(s)", len(feed.items), len(pending))
        # Pre-grab library check (2026-09-25, real duplicate found live:
        # "Sweet, Sassy and Slutty" sat on the wishlist since 2026-09-15,
        # but a local scene got its StashDB id afterwards (identify/
        # library fix) -- the search then grabbed a 1080p copy of a scene
        # already in the library in 2160p). One batched Stash query for
        # every pending StashDB scene; anything already there is marked
        # 'imported' and never searched. Best-effort: if Stash can't be
        # reached, search as before rather than blocking every grab.
        in_library = set()
        stashdb_ids = sorted({r["scene_stashdb_id"] for r in pending if r["provider"] == "stashdb"})
        if stashdb_ids:
            try:
                in_library = set(get_stash_client().library_heights_by_stash_ids(stashdb_ids))
            except Exception:
                logger.exception("run_indexer_search: library pre-check failed, searching without it")
        already_in_library = []
        for release in pending:
            scene_stashdb_id = release["scene_stashdb_id"]
            if scene_stashdb_id in already_in_library:
                continue
            if link_imported_equivalent(db, release["provider"], scene_stashdb_id, get_stash_client):
                already_in_library.append(scene_stashdb_id)
                continue
            if release["provider"] == "stashdb" and scene_stashdb_id in in_library:
                if scene_stashdb_id not in already_in_library:
                    release_repo.set_status_for_scene(db, "stashdb", scene_stashdb_id, "imported")
                    already_in_library.append(scene_stashdb_id)
                    logger.info("run_indexer_search: scene %s is already in the library, marked imported "
                                "instead of searching", scene_stashdb_id)
                continue
            try:
                # Whisparr-free releases (2026-09-25: non-StashDB providers,
                # or the 'no_whisparr' preset) -- and a StashDB release
                # Whisparr doesn't track (e.g. put on the wishlist under
                # 'no_whisparr' before the preset was changed back) -- are
                # searched by this sidecar directly, see handoff.py.
                movie = (whisparr_client.find_by_stash_id(scene_stashdb_id)
                         if uses_whisparr(db, release["provider"]) else None)
                source_for_release, targeted = search_source, False
                if only_stuck:
                    targeted = True
                elif feed is not None:
                    targeted = _targeted_due(release)
                    # A scene dated further back than the feed reaches (the provider added it late, e.g. a German-titled entry six days after
                    # the release) cannot be found in the feed: ONE targeted search now instead of after the first day (v0.213.4), at most
                    # IMMEDIATE_TARGETED_PER_TICK of them per run.
                    if (not targeted and not release.get("last_targeted_search_at") and immediate_targeted < IMMEDIATE_TARGETED_PER_TICK
                            and older_than_feed(release.get("scene_date"), feed.window_days())):
                        targeted = True
                        immediate_targeted += 1
                    source_for_release = search_source if targeted else feed
                if movie is None:
                    outcome = attempt_sidecar_search(db, release["provider"], scene_stashdb_id, release,
                                                     quality_profile, vr_search_enabled, source_for_release,
                                                     get_provider_client)
                    if targeted:
                        targeted_used += 1
                        release_repo.mark_targeted_search(db, release["provider"], scene_stashdb_id)
                    if outcome["grab"]:
                        grabbed.append({"scene_stashdb_id": scene_stashdb_id, **outcome["grab"]})
                    continue
                if scope == "search_sidecar_import":
                    download_id = whisparr_client.find_grabbed_download_id(movie["id"])
                    if not download_id:
                        continue  # Whisparr hasn't grabbed anything for this movie yet
                    # Assumes Whisparr's own configured download client is
                    # the same physical instance registered here -- true
                    # for this project's current single-SABnzbd setups.
                    client_row = download_client_repo.pick_for_protocol(db, "usenet")
                    if client_row is None:
                        continue  # nothing configured on this sidecar's side to hand the job to yet
                    release_repo.set_download_job_for_scene(db, release["provider"], scene_stashdb_id,
                                                              client_row["id"], download_id)
                    grabbed.append({"scene_stashdb_id": scene_stashdb_id, "download_job_id": download_id})
                    continue
                # v0.174.0: the provider's CURRENT title / date / cast (a renamed placeholder title searched on its stale name six times in a
                # row), same as the sidecar path; in RSS mode only where the feed has a lead (a lookup costs a provider request).
                if not feed_has_lead(db, release["provider"], scene_stashdb_id, release, source_for_release):
                    continue
                fresh_title, fresh_date, fresh_performers, lookup_ok = refresh_scene_from_provider(
                    db, release["provider"], scene_stashdb_id, release["title"], release["scene_date"], get_provider_client)
                if not lookup_ok:  # provider did not answer: no search on stale data, next run tries again
                    release_repo.set_search_result_for_scene(db, release["provider"], scene_stashdb_id, "provider_lookup_failed")
                    continue
                outcome = search_and_grab(db, whisparr_client, movie["id"], release["provider"],
                                           scene_stashdb_id, fresh_title, fresh_date,
                                           fresh_performers or movie.get("performerNames"), quality_profile,
                                           is_vr=bool(release["is_vr"]), vr_enabled=vr_search_enabled,
                                           search_source=source_for_release, studio_name=release["studio_name"])
                if targeted:
                    targeted_used += 1
                    release_repo.mark_targeted_search(db, release["provider"], scene_stashdb_id)
                if outcome["grab"]:
                    grabbed.append({"scene_stashdb_id": scene_stashdb_id, **outcome["grab"]})
            except Exception:
                logger.exception("run_indexer_search: failed for scene %s", scene_stashdb_id)
        # Automatic upgrade searches (2026-09-26, off by default), a few per
        # run so indexer API limits aren't spent on upgrades alone.
        upgrades_grabbed = []
        if not only_stuck and get_setting_value(db, "upgrade_search_enabled", "false") == "true" and _upgrade_search_due():
            limit = int(float(get_setting_value(db, "upgrade_search_per_run", "20") or 20))
            for cand_id in upgrades.due_candidates(db, limit):
                try:
                    outcome = upgrades.search_one(db, cand_id, get_provider_client, search_source, quality_profile,
                                                  vr_search_enabled, dry_run=False)
                    if outcome.get("grab"):
                        upgrades_grabbed.append({"candidate": cand_id, **outcome["grab"]})
                except Exception:
                    logger.exception("run_indexer_search: upgrade search failed for candidate %s", cand_id)
            _upgrade_search_mark_run()
        result = {"grabbed": grabbed, "already_in_library": already_in_library, "upgrades_grabbed": upgrades_grabbed}
        if feed is not None:
            logger.info("run_indexer_search: RSS tick done -- %d targeted search(es), %d grab(s)", targeted_used, len(grabbed))
            result["rss"] = {"feed_items": len(feed.items), "targeted": targeted_used}
        summary = _job_summary("indexer_search", result)
        if summary:
            history.event(summary[0], "job", summary[1])
        if only_stuck:
            history.event("info", "job", f"Search all stuck: {len(pending)} release(s) searched, {len(grabbed)} grabbed")
        return result
    finally:
        _indexer_search_lock.release()


# Separate lock from the others above -- same reasoning, guards only
# against a second concurrent run of this specific check.
_recycle_bin_cleanup_lock = threading.Lock()


def run_recycle_bin_cleanup():
    """Permanently deletes anything file_importer.py recycled (see
    api/recycle_bin.py) that's older than `recycle_bin_cleanup_days` --
    added 2026-09-19 alongside the recycle bin itself, so it doesn't
    grow forever."""
    if not _recycle_bin_cleanup_lock.acquire(blocking=False):
        logger.info("run_recycle_bin_cleanup: already in progress, skipping this trigger")
        return {"skipped": True, "reason": "a recycle bin cleanup is already in progress"}
    try:
        return _logged("recycle_bin_cleanup", lambda: cleanup_recycle_bin(
            get_setting_value(db, "local_media_mount", settings.local_media_mount),
            get_setting_value(db, "recycle_bin_path", DEFAULTS["recycle_bin_path"]),
            float(get_setting_value(db, "recycle_bin_cleanup_days", DEFAULTS["recycle_bin_cleanup_days"])),
        ))
    finally:
        _recycle_bin_cleanup_lock.release()


init_watchlist_router(db, get_stash_client, inactive_job)
init_encode_worker_router(db)
init_convert_router(db, get_stash_client, _stash_path_to_local, _recycle_library_file)
init_releases_router(db, get_whisparr_client, get_stash_client, get_stashdb_client,
                      get_manual_search_sources, get_search_source, settings,
                      get_provider_client=get_provider_client)
init_scan_router(sync_and_scan)
init_settings_router(
    db, watchlist_guard=lambda key, value: watchlist_source.switch_guard(db, get_stash_client(), key, value))
init_stashdb_favorites_router(run_stashdb_favorites_import)
init_whisparr_blocklist_sync_router(run_whisparr_blocklist_sync)
init_theporndb_identify_router(run_theporndb_identify)
init_import_check_router(run_import_check)
init_connections_router({
    "stash": get_stash_client,
    "stashdb": get_stashdb_client,
    "whisparr": get_whisparr_client,
    "theporndb": get_theporndb_client,
    "prowlarr": get_prowlarr_client,
    "sabnzbd": get_sabnzbd_client,
}, get_dynamic_factories=get_metadata_provider_factories)
init_backlog_router(db, get_provider_client, get_stash_client, get_whisparr_client)
init_maintenance_router(db, get_stash_client, get_stashdb_client, vr_job, _fix_wrong_download_for,
                        upgrade_search=run_upgrade_search, cutoff_resolution=_cutoff_resolution,
                        get_provider_client=get_provider_client, to_local=_stash_path_to_local,
                        organizer_factory=_organizer, importer_factory=_importer, movies_factory=_movies,
                        duration_job=duration_job, readd=readd_library_scene, library_check_job=upgrade_library_check_job)
init_compare_router(db, lambda: scene_compare.SceneCompare(db, get_stash_client, get_provider_client, settings.data_dir,
                                                              get_importer=_importer, to_local=_stash_path_to_local))
init_file_import_router(
    run_file_import, get_whisparr_client,
    lambda: resolve_file_import_source(db),
    run_recycle_bin_cleanup,
    on_scene_created=on_scene_created,
    on_download_complete=on_download_complete,
)
init_search_router(db, get_stashdb_client, get_theporndb_client, get_stash_client, get_whisparr_client,
                    get_search_source, settings, get_provider_client=get_provider_client, readd=readd_library_scene)
init_quality_profiles_router(db)
init_download_clients_router(db, get_prowlarr_client_if_configured)
init_indexers_router(db)
init_metadata_providers_router(db)
init_stash_boxes_router(db)
init_userscript_router(db, get_stash_client, get_stashdb_client,
                       lambda: get_setting_value(db, "stash_url", settings.stash_url))


def _torrent_switch_scene(target):
    """The monitor found a dead torrent: another one for the same scene, if the search finds one (the old torrent is excluded by its hash and
    does not stop the grab). True when a replacement was grabbed."""
    release = target.get("release")
    source = get_search_source()
    if release is None or source is None or not getattr(source, "available", False):
        return False
    quality_profile, vr_search_enabled = resolve_quality_profiles(db)
    result = attempt_sidecar_search(
        db, release["provider"], release["scene_stashdb_id"], release, quality_profile, vr_search_enabled, source, get_provider_client,
        exclude_hashes=[str(target["job_id"]).lower()], replace_job=True)
    if not result.get("grab") and result.get("result") in (indexer_search.RESULT_SEARCH_FAILED, indexer_search.RESULT_SEARCH_BACKEND_UNAVAILABLE):
        return "failed"  # the indexers could not be asked: the monitor tries again at its next look instead of waiting 6 hours
    return bool(result.get("grab"))


_torrent_health_lock = threading.Lock()


def run_torrent_health():
    """One round of the torrent monitor (torrent_health.py); a second trigger while one runs is skipped."""
    if not _torrent_health_lock.acquire(blocking=False):
        return {"skipped": True}
    try:
        return torrent_health.run(db, {"switch_scene": _torrent_switch_scene})
    finally:
        _torrent_health_lock.release()


init_torrent_health_router(db, _torrent_switch_scene, run_torrent_health)
def _stuck_for_button():
    if resolve_whisparr_scope_preset(db) == "full_whisparr":
        return []  # Whisparr searches by itself, nothing for this program to query
    source = get_search_source()
    if source is None or not getattr(source, "available", False):
        return []  # no indexer / Prowlarr to ask
    return stuck_releases(release_repo.list_sent_to_whisparr_without_grab(db))


init_indexer_search_router(run_indexer_search, _stuck_for_button)


def _get_setting(key):
    with db.connect() as conn:
        row = conn.execute("SELECT value FROM setting WHERE key = ?", (key,)).fetchone()
    return float(row["value"]) if row else float(DEFAULTS[key])


def _scope_allows(kind):
    """Settings > "Content": does the plugin/sidecar look after `kind` ('scenes' or 'movies')? 'both' (default) = everything; 'scenes' or
    'movies' switches the background work of the other area off (v0.139.0). Shared work (file import, watchlist sync, recycle bin) is not gated."""
    scope = get_setting_value(db, "content_scope", "both")
    return scope == "both" or scope == kind


# Dev-session convention (see PROGRESS.md/memory "pause scheduled jobs"):
# an interval this high is never a real schedule, it means the job was
# deliberately paused for an active work session. Without this guard, a
# container restart (needed for any backend/schema deploy) makes every
# loop below run its job immediately regardless of the paused setting --
# `interval_hours` is only consulted for the *sleep after*, not whether
# to run at all -- so a mid-session rebuild silently un-pauses everything
# for one lap. Confirmed live 2026-09-13: a v0.20.3 deploy restart fired
# scan + theporndb_identify + stashdb_favorites concurrently again, the
# exact interference pattern the pause exists to prevent.
PAUSED_INTERVAL_THRESHOLD_HOURS = 500


# Restart hygiene (2026-10-04, v0.95.3): the loops used to run their job at every container start (a dev restart
# queued 13-19 Stash "Batch stash-box" jobs, an hour-long scan, ...). A finished run now stores its time in the
# setting table ("last_run:<job>") and a loop only runs when the interval has really passed since then.
def _mark_ran(job):
    with db.connect() as conn:
        conn.execute("INSERT OR REPLACE INTO setting (key, value) VALUES (?, ?)",
                     (f"last_run:{job}", datetime.now(timezone.utc).isoformat()))


def _hours_until_due(job, interval_hours):
    """0 when the job never ran (or its stamp is unreadable) or the interval has passed, else the hours still to wait."""
    with db.connect() as conn:
        row = conn.execute("SELECT value FROM setting WHERE key = ?", (f"last_run:{job}",)).fetchone()
    if not row or not row["value"]:
        return 0.0
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(row["value"])).total_seconds() / 3600
    except ValueError:
        return 0.0
    return max(interval_hours - age, 0.0)


# History/Events log for the background jobs (2026-09-28, v0.79.0, see
# history.py): an entry only when a run actually did something or failed
# (user: no "nothing to do" entries). Wraps both the periodic loops and
# the manual "Run now" endpoints, since both call these run_* functions.
def _job_summary(job, r):
    """(level, message) for a job result worth logging, else None."""
    if not isinstance(r, dict) or r.get("skipped"):
        return None
    if job == "scan":
        failed = len(r.get("failed") or [])
        new, late = r.get("new_releases", 0), r.get("late_additions", 0)
        if not (new or late or failed):
            return None
        text = f"Scan: {new} new release(s), {late} back-catalogue addition(s) to the backlog"
        return ("warning", f"{text}, {failed} entr(y/ies) failed") if failed else ("info", text)
    if job == "stashdb_favorites":
        n = r.get("studios_favorited", 0) + r.get("performers_favorited", 0)
        failed = r.get("studio_batches_failed", 0) + r.get("performer_batches_failed", 0)
        if not (n or failed):
            return None
        return ("warning" if failed else "info",
                f"StashDB favorites: {r.get('studios_favorited', 0)} studio(s), "
                f"{r.get('performers_favorited', 0)} performer(s) newly favorited"
                + (f", {failed} batch(es) failed" if failed else ""))
    if job == "whisparr_blocklist_sync":
        errors = r.get("errors") or 0
        errors = len(errors) if isinstance(errors, list) else errors
        return ("warning", f"Whisparr blocklist sync: {errors} error(s)") if errors else None
    if job == "theporndb_identify":
        n = r.get("studios_matched", 0) + r.get("performers_matched", 0)
        errors = len(r.get("errors") or [])
        if not (n or errors):
            return None
        return ("warning" if errors else "info",
                f"ThePornDB identify: {r.get('studios_matched', 0)} studio(s), "
                f"{r.get('performers_matched', 0)} performer(s) matched"
                + (f", {errors} error(s)" if errors else ""))
    if job == "inactive_check":
        errors = len(r.get("errors") or [])
        return (("warning" if errors else "info"),
                f"Inactive check: {r.get('proposed', 0)} entr(y/ies) proposed as probably inactive"
                + (f", {errors} error(s)" if errors else ""))
    if job == "indexer_search":
        grabbed, known = len(r.get("grabbed") or []), len(r.get("already_in_library") or [])
        upgrades = len(r.get("upgrades_grabbed") or [])
        if not (grabbed or known or upgrades):
            return None
        return ("info", f"Indexer search: {grabbed} grabbed, {upgrades} upgrade(s) grabbed, "
                        f"{known} already in the library")
    if job == "film_feed":
        added = r.get("added", 0)
        if r.get("incomplete"):
            return ("warning", f"Movie feed: did not reach the last position, {added} new movie(s) suggested (some may be missed)")
        if not added:
            return None
        names = ", ".join(r.get("added_titles", [])[:3]) + (", ..." if added > 3 else "")
        return "info", f"Movie feed: {added} new movie(s) of watched studios in the Suggestions ({names})"
    if job == "film_search":
        found, searched = r.get("found", 0), r.get("searched", 0)
        errors = sum(1 for x in r.get("results", []) if x.get("result") == "error")
        if not (found or errors):
            return None
        return ("warning" if errors else "info", f"Movie search: {searched} movie(s) searched, {found} candidate(s) to review"
                + (f", {errors} failed" if errors else ""))
    if job == "recycle_bin_cleanup":
        deleted, errors = len(r.get("deleted") or []), len(r.get("errors") or [])
        if not (deleted or errors):
            return None
        return ("warning" if errors else "info",
                f"Recycle bin: {deleted} file(s) permanently deleted" + (f", {errors} error(s)" if errors else ""))
    return None


_JOB_LABELS = {
    "scan": "Scan", "stashdb_favorites": "StashDB favorites import",
    "whisparr_blocklist_sync": "Whisparr blocklist sync", "theporndb_identify": "ThePornDB identify",
    "inactive_check": "Inactive check", "indexer_search": "Indexer search",
    "recycle_bin_cleanup": "Recycle bin cleanup", "duplicate_check": "StashDB duplicate check", "film_search": "Movie search", "film_feed": "Movie feed",
}


def _logged(job, fn):
    try:
        result = fn()
    except Exception as e:
        history.event("error", "job", f"{_JOB_LABELS.get(job, job)} failed: {type(e).__name__}: {e}"[:500],
                      dedupe_minutes=60)
        raise
    try:
        summary = _job_summary(job, result)
        if summary:
            details = None
            if job == "scan" and isinstance(result, dict) and result.get("failed"):
                # WHICH entries failed and why (v0.232.2): the line only said "1 entr(y/ies) failed" and the reason was in the container log
                details = {"failed": [{k: str(f.get(k) or "")[:200] for k in ("provider", "entity_type", "name", "error")}
                                      for f in result["failed"][:25]]}
            history.event(summary[0], "job", summary[1], details=details)
    except Exception:
        logger.exception("history: couldn't summarize %s", job)
    return result



# A fresh installation has no connections yet (they are entered in Settings > Connections). The scheduled jobs that need them wait quietly
# instead of failing with a traceback every hour (found by the install rehearsal on 2026-10-09); they start within minutes of the entry.
_not_ready_logged = set()


def _connections_ready(job, *keys):
    """True when every named setting (stash_url, stashdb_api_key, theporndb_api_key, ...) has a value (database setting or .env)."""
    missing = [k for k in keys if not str(get_setting_value(db, k, getattr(settings, k, "")) or "").strip()]
    if not missing:
        _not_ready_logged.discard(job)
        return True
    if job not in _not_ready_logged:
        _not_ready_logged.add(job)
        where = {"stash_url": "Stash URL: open Release Manager in Stash once (the setup guide shows it), or set STASH_URL",
                 "stash_api_key": "Stash API key: generate one in Stash > Settings > Security (only needed when Stash has a login), then open Release Manager once",
                 "stashdb_api_key": "StashDB API key: add the StashDB endpoint in Stash > Settings > Metadata Providers, or enter it in the setup guide"}
        logger.info("%s waits: not entered yet: %s", job,
                    "; ".join(where.get(k, f"{k}: Settings > Connections in the plugin") for k in missing))
    return False


async def _scan_loop():
    while True:
        interval_hours = _get_setting("scan_interval_hours")
        if interval_hours < PAUSED_INTERVAL_THRESHOLD_HOURS and _scope_allows("scenes"):
            if not _connections_ready("scheduled scan", "stash_url", "stashdb_api_key"):
                await asyncio.sleep(300)
                continue
            wait = _hours_until_due("scan", interval_hours)
            if wait > 0:
                await asyncio.sleep(max(wait, 0.01) * 3600)
                continue
            try:
                await asyncio.to_thread(sync_and_scan)
            except Exception:
                logger.exception("scheduled scan failed")
        await asyncio.sleep(min(max(interval_hours, 0.1), 1) * 3600)  # re-check hourly: a changed/un-paused interval takes effect without a restart


async def _safety_sweep_loop():
    while True:
        interval_hours = _get_setting("safety_sweep_interval_hours")
        if interval_hours < PAUSED_INTERVAL_THRESHOLD_HOURS and _scope_allows("scenes") and whisparr_configured(db):
            try:
                reset = await asyncio.to_thread(get_whisparr_client().safety_sweep)
                for resource in ("studio", "performer"):
                    succeeded = reset[resource]["succeeded"]
                    failed = reset[resource]["failed"]
                    if succeeded:
                        logger.warning("safety sweep reset monitored/moviesMonitored on %d %s(s): %s",
                                        len(succeeded), resource, succeeded)
                        history.event("warning", "job", f"Safety sweep: reset monitored on {len(succeeded)} "
                                      f"Whisparr {resource}(s)", details=succeeded)
                    if failed:
                        # Still monitored/moviesMonitored after a GET-modify-PUT
                        # attempt -- exactly the failure mode this whole guard
                        # exists to catch, see whisparr_client.py's
                        # _unmonitor_each() docstring. Loud on purpose.
                        logger.error("safety sweep FAILED to clear monitored/moviesMonitored on %d %s(s), "
                                     "still dangerous: %s", len(failed), resource, failed)
                        history.event("error", "job", f"Safety sweep FAILED to clear monitored on {len(failed)} "
                                      f"Whisparr {resource}(s)", details=failed)
            except Exception as e:
                logger.exception("safety sweep failed")
                history.event("error", "job", f"Safety sweep failed: {type(e).__name__}: {e}"[:500], dedupe_minutes=60)
        await asyncio.sleep(max(interval_hours, 0.1) * 3600)


async def _stashdb_favorites_loop():
    while True:
        interval_hours = _get_setting("stashdb_favorites_sync_interval_hours")
        if interval_hours < PAUSED_INTERVAL_THRESHOLD_HOURS:
            if not _connections_ready("StashDB favorites import", "stash_url", "stashdb_api_key"):
                await asyncio.sleep(300)
                continue
            wait = _hours_until_due("stashdb_favorites", interval_hours)
            if wait > 0:
                await asyncio.sleep(max(wait, 0.01) * 3600)
                continue
            try:
                await asyncio.to_thread(run_stashdb_favorites_import)
            except Exception:
                logger.exception("StashDB favorites import failed")
        await asyncio.sleep(min(max(interval_hours, 0.1), 1) * 3600)  # re-check hourly: a changed/un-paused interval takes effect without a restart


def run_box_favorites_import(provider):
    """Same import as run_stashdb_favorites_import(), for one ADDED stash-box (FansDB, JAVStash, ...): its account favorites -> the local Favorite flag
    of the entities that carry that box's endpoint. One direction, never removes a favorite."""
    row = metadata_provider_repo.get_one(db, provider)
    if row is None or not row["enabled"]:
        return {"skipped": True, "reason": "provider not found or disabled"}
    client = get_provider_client(provider)
    result = _logged("stashdb_favorites", lambda: import_stashdb_favorites(client, get_stash_client(), endpoint=row["url"]))
    _mark_ran(f"favorites:{provider}")
    return result


async def _box_favorites_loop():
    """Added stash-boxes: favorites import every `favorites_interval` hours (empty = the StashDB interval setting). A failing box is logged and skipped."""
    while True:
        stashdb_hours = _get_setting("stashdb_favorites_sync_interval_hours")
        if _connections_ready("Stash-box favorites import", "stash_url"):
            for row in metadata_provider_repo.list_all(db, enabled_only=True):
                hours = row.get("favorites_interval") if row.get("favorites_interval") is not None else stashdb_hours
                if hours >= PAUSED_INTERVAL_THRESHOLD_HOURS or _hours_until_due(f"favorites:{row['key']}", hours) > 0:
                    continue
                try:
                    await asyncio.to_thread(run_box_favorites_import, row["key"])
                except Exception:
                    logger.exception("favorites import failed for stash-box %s", row["key"])
                    _mark_ran(f"favorites:{row['key']}")  # do not retry every hour while the box keeps failing
        await asyncio.sleep(3600)


async def _whisparr_blocklist_sync_loop():
    while True:
        interval_hours = _get_setting("whisparr_blocklist_sync_interval_hours")
        if interval_hours < PAUSED_INTERVAL_THRESHOLD_HOURS and _scope_allows("scenes") and whisparr_configured(db):
            wait = _hours_until_due("whisparr_blocklist_sync", interval_hours)
            if wait > 0:
                await asyncio.sleep(max(wait, 0.01) * 3600)
                continue
            try:
                await asyncio.to_thread(run_whisparr_blocklist_sync)
            except Exception:
                logger.exception("Whisparr blocklist sync failed")
        await asyncio.sleep(min(max(interval_hours, 0.1), 1) * 3600)  # re-check hourly: a changed/un-paused interval takes effect without a restart


async def _inactive_check_loop():
    """Weekly by default. Unlike the other loops it doesn't run right
    after a container restart: it checks the last run's timestamp and
    only runs once the interval has really passed (a full check is ~50
    StashDB requests plus some ThePornDB ones -- no reason to repeat it
    on every dev restart)."""
    while True:
        interval_hours = _get_setting("inactive_check_interval_hours")
        if interval_hours < PAUSED_INTERVAL_THRESHOLD_HOURS:
            last = (inactive_check.last_run(db) or {}).get("checked_at")
            due = True
            if last:
                try:
                    age = (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds() / 3600
                    due = age >= interval_hours
                except ValueError:
                    pass
            if due:
                try:
                    await asyncio.to_thread(inactive_job.run)
                except Exception:
                    logger.exception("inactive check failed")
        await asyncio.sleep(min(max(interval_hours, 0.1), 6) * 3600)


async def _film_search_loop():
    while True:
        interval_hours = _get_setting("film_search_interval_hours")
        enabled = str(get_setting_value(db, "film_search_enabled", "false")).lower() == "true"
        if enabled and interval_hours < PAUSED_INTERVAL_THRESHOLD_HOURS and _scope_allows("movies"):
            wait = _hours_until_due("film_search", interval_hours)
            if wait > 0:
                await asyncio.sleep(max(wait, 0.01) * 3600)
                continue
            try:
                await asyncio.to_thread(run_film_search)
            except Exception:
                logger.exception("movie search failed")
        await asyncio.sleep(min(max(interval_hours, 0.1), 1) * 3600)  # re-check hourly: switching it on/off takes effect without a restart


async def _film_feed_loop():
    while True:
        interval_hours = _get_setting("film_feed_interval_hours")
        if interval_hours < PAUSED_INTERVAL_THRESHOLD_HOURS and get_setting_value(db, "theporndb_api_key", settings.theporndb_api_key) and _scope_allows("movies"):
            wait = _hours_until_due("film_feed", interval_hours)
            if wait > 0:
                await asyncio.sleep(max(wait, 0.01) * 3600)
                continue
            try:
                await asyncio.to_thread(run_film_feed)
            except Exception:
                logger.exception("movie feed failed")
        await asyncio.sleep(min(max(interval_hours, 0.1), 6) * 3600)


async def _duplicate_check_loop():
    while True:
        interval_hours = _get_setting("stashdb_duplicate_check_interval_hours")
        if interval_hours < PAUSED_INTERVAL_THRESHOLD_HOURS and _scope_allows("scenes"):
            wait = _hours_until_due("duplicate_check", interval_hours)
            if wait > 0:
                await asyncio.sleep(max(wait, 0.01) * 3600)
                continue
            try:
                await asyncio.to_thread(run_duplicate_check)
            except Exception:
                logger.exception("StashDB duplicate check failed")
        await asyncio.sleep(min(max(interval_hours, 0.1), 1) * 3600)  # re-check hourly, like the other loops


async def _theporndb_identify_loop():
    while True:
        interval_hours = _get_setting("theporndb_identify_interval_hours")
        if interval_hours < PAUSED_INTERVAL_THRESHOLD_HOURS and _scope_allows("scenes"):
            if not _connections_ready("ThePornDB identify", "stash_url", "theporndb_api_key"):
                await asyncio.sleep(300)
                continue
            wait = _hours_until_due("theporndb_identify", interval_hours)
            if wait > 0:
                await asyncio.sleep(max(wait, 0.01) * 3600)
                continue
            try:
                await asyncio.to_thread(run_theporndb_identify)
            except Exception:
                logger.exception("ThePornDB identify failed")
        await asyncio.sleep(min(max(interval_hours, 0.1), 1) * 3600)  # re-check hourly: a changed/un-paused interval takes effect without a restart


async def _import_check_loop():
    while True:
        interval_hours = _get_setting("import_check_interval_hours")
        try:
            result = await asyncio.to_thread(run_import_check)
            if result.get("imported"):
                logger.info("import check: %d release(s) confirmed imported", len(result["imported"]))
        except Exception:
            logger.exception("import check failed")
        try:  # movies (stage C): what Whisparr grabbed / imported -- one history request, only where Whisparr searches
            kick_film_whisparr_history()
        except Exception:
            logger.exception("Whisparr history check of the movies could not be started")
        await asyncio.sleep(max(interval_hours, 0.1) * 3600)


async def _file_import_loop():
    while True:
        interval_hours = _get_setting("file_import_interval_hours")
        try:
            result = await asyncio.to_thread(run_file_import)
            if result.get("imported"):
                logger.info("file import: %d download(s) imported", len(result["imported"]))
            removed = await asyncio.to_thread(download_history_cleanup.clean_all, db)  # "Remove completed" (v0.215.0)
            if removed:
                logger.info("file import: %d finished download(s) removed from the client's history", removed)
            purge_days = int(get_setting_value(db, "sab_archive_purge_days", "30") or 0)
            if purge_days > 0 and _hours_until_due("sab_archive_purge", 24) == 0:  # SABnzbd's Archive: once a day (v0.222.3)
                await asyncio.to_thread(download_history_cleanup.purge_archives, db, purge_days)
                _mark_ran("sab_archive_purge")
        except Exception:
            logger.exception("file import failed")
        await asyncio.sleep(max(interval_hours, 0.1) * 3600)


async def _torrent_health_loop():
    """The torrent monitor (v0.231.0, torrent_health.py): every `torrent_health_minutes` (0 = off)."""
    while True:
        minutes = _get_setting("torrent_health_minutes")
        if minutes > 0:
            try:
                result = await asyncio.to_thread(run_torrent_health)
                if result.get("switched") or result.get("problems"):
                    logger.info("torrent health: %s", result)
            except Exception:
                logger.exception("torrent health failed")
        await asyncio.sleep(max(minutes, 5) * 60 if minutes > 0 else 600)


async def _indexer_search_loop():
    while True:
        interval_hours = _get_setting("indexer_search_interval_hours")
        if interval_hours < PAUSED_INTERVAL_THRESHOLD_HOURS and _scope_allows("scenes"):
            try:
                result = await asyncio.to_thread(run_indexer_search)
                if result.get("grabbed"):
                    logger.info("indexer search: grabbed %d release(s)", len(result["grabbed"]))
            except Exception:
                logger.exception("indexer search failed")
            # RSS mode ticks on its own, much shorter clock (one feed pull per tick).
            if get_setting_value(db, "search_mode", "polling") == "rss":
                try:
                    interval_hours = max(float(get_setting_value(db, "rss_interval_minutes", "30") or 30), 5) / 60
                except ValueError:
                    interval_hours = 0.5
        await asyncio.sleep(max(interval_hours, 0.1) * 3600)


async def _recycle_bin_cleanup_loop():
    while True:
        interval_hours = _get_setting("recycle_bin_cleanup_interval_hours")
        try:
            result = await asyncio.to_thread(run_recycle_bin_cleanup)
            if result.get("deleted"):
                logger.info("recycle bin cleanup: %d file(s) permanently deleted", len(result["deleted"]))
        except Exception:
            logger.exception("recycle bin cleanup failed")
        await asyncio.sleep(max(interval_hours, 0.1) * 3600)


async def _history_prune_loop():
    """Daily: drops History/Events entries older than
    history_retention_days (2026-09-28, v0.79.0)."""
    while True:
        try:
            days = int(get_setting_value(db, "history_retention_days", "14") or 14)
            removed = await asyncio.to_thread(history.prune, days)
            if removed:
                logger.info("history: pruned %d entr(y/ies) older than %d day(s)", removed, days)
        except Exception:
            logger.exception("history prune failed")
        await asyncio.sleep(24 * 3600)


# Live push (2026-09-26, v0.78.0, see events.py): recomputes the
# download/import progress map only while a browser is connected --
# every LIVE_PROGRESS_SECONDS while something is downloading/importing,
# otherwise asleep until woken (release change, import phase, new
# subscriber). Replaces the pages' own 5-10 s polling.
LIVE_PROGRESS_SECONDS = 3


async def _live_progress_loop():
    last = None
    active = False
    while True:
        await events.wait_for_work(LIVE_PROGRESS_SECONDS if active else None)
        if events.subscriber_count() == 0:
            last, active = None, False
            continue
        try:
            data = await asyncio.to_thread(compute_download_progress)
        except Exception:
            logger.exception("live progress: computing the progress map failed")
            await asyncio.sleep(LIVE_PROGRESS_SECONDS)
            continue
        active = any(data.values())
        if data != last or events.take_snapshot_request():
            events.publish("progress", data=data)
            last = data
        # At most one recompute per second, however often woken (a copy
        # reports progress per chunk).
        await asyncio.sleep(1)


_last_scan_kick = {"at": 0.0}


def _kick_file_import_after_scan():
    """A Stash scan job just finished: continue pending import verifications now instead of at the next
    15-minute poll (debounced, run in a thread)."""
    if time.time() - _last_scan_kick["at"] < 20:
        return
    _last_scan_kick["at"] = time.time()

    def _run():
        time.sleep(5)  # let Stash finish writing the scanned scenes
        try:
            run_file_import()
        except Exception:
            logger.exception("event-driven file import failed")

    threading.Thread(target=_run, daemon=True).start()


job_watcher = stash_events.StashJobWatcher(
    lambda: (get_setting_value(db, "stash_url", settings.stash_url),
             get_setting_value(db, "stash_api_key", settings.stash_api_key)),
    _kick_file_import_after_scan,
    lambda: {str(j) for j in verification._phash_regen_jobs.values()})


@asynccontextmanager
async def lifespan(app: FastAPI):
    events.attach_loop(asyncio.get_running_loop())
    history.event("info", "system", f"Sidecar started (version {SIDECAR_VERSION})" + (" in DEVELOPMENT MODE: no background jobs" if settings.dev_mode else ""))
    tasks = [asyncio.create_task(_history_prune_loop())]  # (only this instance's own database housekeeping)
    if settings.dev_mode:
        logger.warning("DEVELOPMENT MODE (SRM_DEV_MODE): no scan, search, import, sync or clean-up job and no Stash event connection is started "
                       "in this instance; manual actions in the UI still work")
    else:
        tasks += [asyncio.create_task(_live_progress_loop()), asyncio.create_task(job_watcher.run())]
        # One-off description backfill (2026-09-26), background, no-op once done.
        threading.Thread(target=run_details_backfill, daemon=True).start()
        threading.Thread(target=run_tags_backfill, daemon=True).start()
        threading.Thread(target=convert_resume, daemon=True).start()  # Convert rows that were being checked / swapped when the sidecar stopped
        tasks += [asyncio.create_task(loop()) for loop in (
            _scan_loop, _safety_sweep_loop, _stashdb_favorites_loop, _box_favorites_loop, _whisparr_blocklist_sync_loop, _theporndb_identify_loop,
            _inactive_check_loop, _duplicate_check_loop, _film_search_loop, _film_feed_loop, _import_check_loop, _file_import_loop, _torrent_health_loop,
            _indexer_search_loop, _recycle_bin_cleanup_loop)]
    app.state.job_tasks = tasks
    yield
    for task in tasks:
        task.cancel()


app = FastAPI(title="StashReleaseManager", lifespan=lifespan)

app.include_router(watchlist_router)
app.include_router(userscript_router)
app.include_router(releases_router)
app.include_router(encode_worker_router)
app.include_router(convert_router)
app.include_router(scan_router)
app.include_router(settings_router)
app.include_router(stashdb_favorites_router)
app.include_router(whisparr_blocklist_sync_router)
app.include_router(theporndb_identify_router)
app.include_router(import_check_router)
app.include_router(connections_router)
app.include_router(backlog_router)
app.include_router(maintenance_router)
app.include_router(file_import_router)
app.include_router(search_router)
app.include_router(quality_profiles_router)
app.include_router(download_clients_router)
app.include_router(indexers_router)
app.include_router(metadata_providers_router)
app.include_router(stash_boxes_router)
app.include_router(torrent_health_router)
app.include_router(indexer_search_router)
app.include_router(history_router)
app.include_router(compare_router)
init_transfer_router(db, settings.data_dir)
app.include_router(transfer_router)
init_setup_router(db)
app.include_router(setup_router)
app.include_router(muted_studios_router)
init_muted_studios_router(db, get_provider_client)
film_seed_job = film_match.SeedJob(db, lambda: get_provider_client("theporndb"))
app.include_router(films_router)
init_films_router(db, get_provider_client, settings.data_dir, _movies, film_seed_job, get_stash_client, get_film_search_source,
                  file_tools=(lambda p: _recycle_library_file(_stash_path_to_local(p)), _stash_path_to_local),
                  whisparr={"kick": kick_film_whisparr, "start": start_film_whisparr_sync, "status": lambda: dict(_film_whisparr_state),
                  "start_files": start_film_whisparr_files, "files_status": lambda: dict(_film_files_state),
                  "start_history": start_film_whisparr_history, "history_status": lambda: dict(_film_history_state)},
                  library={"check": library_check, "apply": library_apply, "keep": lambda fid: film_library.keep(db, fid)},
                  run_feed=run_film_feed)


@app.websocket("/events/ws")
async def live_events_ws(websocket: WebSocket):
    """Live events for the plugin's pages, see events.py. Any origin may
    connect (user decision 2026-09-26): the browser connects from Stash's
    origin, and the stream carries only scene ids, statuses and progress
    numbers -- never keys or URLs."""
    await websocket.accept()
    await events.serve_websocket(websocket)


@app.get("/events")
async def live_events(request: Request):
    """The same stream as Server-Sent Events (curl -N, or a page without
    Stash's CSP), see events.py."""
    return StreamingResponse(events.stream(request), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",  # nginx-style proxies: don't buffer the stream
        "Access-Control-Allow-Origin": "*",
    })


@app.get("/stash-queue")
def stash_queue():
    """Stash's job queue as mirrored from its jobsSubscribe events (connected = the event channel is up)."""
    return job_watcher.snapshot()


@app.post("/system/restart")
def system_restart():
    """Restarts this sidecar (v0.213.3): the process ends and Docker brings the container back (compose `restart: unless-stopped`, which
    restarts after ANY exit that was not a manual stop). Only inside a container: a sidecar started by hand would just stop. The answer goes out
    first, the signal one second later; uvicorn then shuts down gracefully (--timeout-graceful-shutdown 3)."""
    if not os.path.exists("/.dockerenv"):
        return {"error": "this sidecar does not run in a container -- restart it by hand"}

    def _stop():
        time.sleep(1.0)
        logger.warning("restart requested from the UI -- stopping, Docker starts the container again")
        os.kill(os.getpid(), signal.SIGTERM)

    threading.Thread(target=_stop, daemon=True, name="restart").start()
    return {"ok": True, "restarting": True}


@app.get("/health")
def health():
    # `dev_mode`: this instance runs no background job (SRM_DEV_MODE); `background_jobs`: how many loops are running.
    return {"status": "ok", "version": SIDECAR_VERSION, "dev_mode": settings.dev_mode, "background_jobs": len(getattr(app.state, "job_tasks", []))}
