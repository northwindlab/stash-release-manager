import json
import logging
import os
import time
from urllib.parse import quote
import history
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import download_client_repo
import encode_queue
import import_progress
import file_importer
import release_repo
import torrent_health
from handoff import hand_off, fix_mismatched_download, fallback_to_whisparr, uses_whisparr
import dismiss_streak
import indexer_search
import qbittorrent_client
from qbittorrent_client import QbittorrentClient
from indexer_search import grab_candidate, safe_info_url, RESULT_TITLE_MISMATCH_ONLY
from sabnzbd_client import SabnzbdClient
from stash_client import quality_label
from routers.settings import resolve_whisparr_quality_profiles, resolve_quality_profiles, get_setting_value, DEFAULTS
from verification import verify_downloaded_file

router = APIRouter(prefix="/releases", tags=["releases"])

logger = logging.getLogger(__name__)
_db = None
_get_whisparr_client = None
_get_stash_client = None
_get_stashdb_client = None
_get_manual_search_sources = None
_get_search_source = None
_get_provider_client = None
_settings = None


def init_releases_router(db, get_whisparr_client, get_stash_client, get_stashdb_client,
                          get_manual_search_sources, get_search_source, settings, get_provider_client=None):
    """get_whisparr_client/get_stash_client/get_stashdb_client/
    get_manual_search_sources: zero-arg factories, not fixed
    instances -- called fresh inside handoff()/library_status() below
    so a Settings-page edit to a connection's URL/API key takes effect
    on the very next call, same reasoning as main.py's
    get_stash_client()/get_whisparr_client(). `get_manual_search_
    sources` (2026-09-25) returns {name: source} for manual_search(),
    see main.py. `get_search_source` (2026-09-25) is what the automatic search/
    hand-off call sites pass into indexer_search.py's `search_source`
    param, see main.py's get_search_source(). `settings`
    (the env-based config.Settings) is now only the *fallback* for the
    (normal) quality profile -- read fresh from the DB-backed setting
    on every hand-off below, same reasoning again."""
    global _db, _get_whisparr_client, _get_stash_client, _get_stashdb_client, _get_search_source
    global _get_manual_search_sources, _settings
    _db, _get_whisparr_client, _get_stash_client, _get_stashdb_client = (
        db, get_whisparr_client, get_stash_client, get_stashdb_client
    )
    _get_manual_search_sources, _settings = get_manual_search_sources, settings
    _get_search_source = get_search_source
    global _get_provider_client
    _get_provider_client = get_provider_client


@router.get("")
def list_releases(status: str = "new", entity_type: str | None = None,
                   entity_stashdb_id: str | None = None, limit: int = 200, offset: int = 0):
    return release_repo.list_releases(_db, status, entity_type, entity_stashdb_id, limit, offset)


_lookup_cache = {"at": 0.0, "studio": ({}, {}), "performer": ({}, {})}
LOOKUP_TTL = 600  # seconds


def _local_lookups():
    """Cached local studio/performer maps (v0.102.8). A row found via a watched PERFORMER shows its scene's studio, which links to
    Stash only when that studio exists locally; and a row whose watched entity has left the watchlist (no entity_local_id from the
    join) can still link to the entity if it exists in Stash. Best effort -- without Stash everything stays plain text."""
    now = time.time()
    if now - _lookup_cache["at"] > LOOKUP_TTL:
        try:
            stash = _get_stash_client()
            _lookup_cache["studio"] = stash.local_studio_lookup()
            _lookup_cache["performer"] = stash.local_performer_lookup()
        except Exception:
            logger.warning("releases: local studio/performer lookup failed", exc_info=True)
        _lookup_cache["at"] = now
    return _lookup_cache["studio"], _lookup_cache["performer"]


def _norm_name(name):
    return "".join(ch for ch in (name or "").lower() if ch.isalnum())


def _cast_links(cast_names, p_name):
    """[{name, local_id}] of '|A|B|' (local_id = the performer's page in this Stash, by name; None when there is none)."""
    return [{"name": n, "local_id": p_name.get(_norm_name(n))} for n in (cast_names or "").split("|") if n]


def _add_studio_local_ids(result):
    rows = result.get("rows") if isinstance(result, dict) else None
    if not rows:
        return result
    (s_id, s_name), (p_id, p_name) = _local_lookups()
    for r in rows:
        if r.get("cast_names"):  # the performers, shown next to a studio-matched row (v0.213.8)
            r["cast"] = _cast_links(r["cast_names"], p_name)
        # The scene's own studio (shown next to a performer-matched row).
        if r.get("entity_type") != "studio" and r.get("studio_name"):
            r["studio_local_id"] = (s_id.get(r.get("studio_id")) if r.get("studio_id") else None) or s_name.get(_norm_name(r["studio_name"]))
        # The watched entity itself, when the join found no watchlist row (entity since removed from the watchlist).
        if not r.get("entity_local_id"):
            if r.get("entity_type") == "studio":
                r["entity_local_id"] = s_id.get(r.get("entity_stashdb_id")) or s_id.get(r.get("studio_id")) or s_name.get(_norm_name(r.get("studio_name")))
            elif r.get("entity_type") == "performer":
                r["entity_local_id"] = p_id.get(r.get("entity_stashdb_id")) or p_name.get(_norm_name(r.get("entity_name")))
    return result


@router.post("/query")
def query(body: dict):
    """Server-side filtered/paged listing for the List view and the
    Calendar's one-month view (2026-09-26), see
    release_repo.query_releases() for the parameters."""
    providers = body.get("providers")
    return _add_studio_local_ids(release_repo.query_releases(
        _db, statuses=tuple(body.get("statuses") or ["new"]), q=body.get("q") or "",
        search_description=bool(body.get("search_description")), search_tags=bool(body.get("search_tags")), entity_type=body.get("entity_type"),
        vr=body.get("vr"), providers=list(providers) if providers is not None else None,
        date_from=body.get("date_from") or None, date_until=body.get("date_until") or None,
        page=int(body.get("page") or 1), per_page=int(body.get("per_page") if body.get("per_page") is not None else 100),
        ids_only=bool(body.get("ids_only")),
        entities=[tuple(e) for e in body["entities"]] if body.get("entities") is not None else None,
        late_addition=body.get("late_addition"),
        order=body.get("order") or "day",
        upgrades=body.get("upgrades") if body.get("upgrades") in ("only", "hide") else None,
    ))


def _eta(seconds):
    if seconds is None or seconds < 0 or seconds >= 8640000:  # qBittorrent reports 8640000 for "unknown"
        return None
    return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def _client_slots(client_row):
    """{job id: {status, percentage, timeleft, mb, mbleft}} of what the client is still DOWNLOADING (SABnzbd queue / qBittorrent torrents below
    100 %), in one shape. A finished torrent that keeps seeding is not listed (it waits for the import, then seeds on). Best effort: an
    unreachable client gives {}."""
    if client_row is None:
        return {}
    try:
        if client_row["type"] == "sabnzbd":
            return {s["nzo_id"]: s for s in SabnzbdClient(client_row["url"], client_row["api_key"]).get_queue()}
        if client_row["type"] == "qbittorrent":
            client = QbittorrentClient(client_row["url"], client_row["username"], client_row["password"], client_row["category"])
            out = {}
            for t in client.get_torrents():
                if (t.get("progress") or 0) >= 1 and qbittorrent_client.status_of(t) != "error":
                    continue
                out[t["hash"]] = {"status": qbittorrent_client.status_of(t).capitalize(), "percentage": str(int((t.get("progress") or 0) * 100)),
                                  "timeleft": _eta(t.get("eta")), "mb": str(round((t.get("size") or 0) / 1e6, 2)),
                                  "mbleft": str(round((t.get("amount_left") or 0) / 1e6, 2)),
                                  "_torrent": t}  # the client's own numbers, for the live swarm / stall display (_overlay_torrent_health)
            return out
    except Exception:
        return {}
    return {}


@router.get("/download-progress")
def download_progress():
    """Live download progress for every 'sent_to_whisparr' release with
    a recorded download job (release_repo.list_grabbed_awaiting_import()
    -- deduped by scene, same scope download_sources.SabnzbdSource
    itself polls to notice a finished job) -- added 2026-09-20 per the
    user's own request for an Activity-style progress display, since
    this project's own "search visibility" (see indexer_search.py) only
    ever showed a before/after result label, never a live in-progress
    percentage the way Whisparr's own Activity page would.

    One SABnzbd queue fetch per distinct `download_client_id`, not one
    per release -- typically a single configured SABnzbd client, but
    stays correct if more than one exists. Best-effort per client: a
    client that's unreachable or not a `sabnzbd` type just contributes
    no data for its own releases rather than failing the whole
    response.

    Returns `{scene_stashdb_id: {status, percentage, timeleft, mb,
    mbleft} | None}` -- `None` means SABnzbd's own live queue no longer
    has this `nzo_id` (already finished and moved to history, or
    genuinely gone); the frontend treats that the same as "no progress
    data yet", not an error, since a release also reaches this endpoint
    for the brief window between grab and the next file-import poll
    picking up its completion."""
    releases = release_repo.list_grabbed_awaiting_import(_db)
    result = {}
    live = import_progress.snapshot()
    queues_by_client = {}
    for release in releases:
        client_id = release["download_client_id"]
        if client_id not in queues_by_client:
            slots_by_nzo = _client_slots(download_client_repo.get_one(_db, client_id))
            queues_by_client[client_id] = slots_by_nzo
        scene_id = release["scene_stashdb_id"]
        slot = queues_by_client[client_id].get(release["download_job_id"])
        if scene_id in live:
            result[scene_id] = _import_phase(live[scene_id], scene_id)
        elif slot is None:
            # Gone from the client's live queue: finished (or post-
            # processing) and not picked up by an import run yet.
            result[scene_id] = {"phase": "waiting_import"}
        else:
            result[scene_id] = {
                "phase": "downloading",
                "status": slot.get("status"),
                "percentage": slot.get("percentage"),
                "timeleft": slot.get("timeleft"),
                "mb": slot.get("mb"),
                "mbleft": slot.get("mbleft"),
            }
    # Movies on the Movies wishlist (v0.126.0): key "film:<id>", the same shapes. A one-file movie that is in the library and waits
    # for Stash's scan shows "verifying"; one whose download left the live queue "waiting_import".
    try:
        import film_repo
        for film in film_repo.list_films(_db, ["grabbed"], per_page=0)["rows"]:
            if (film.get("mode") or "file") != "file":
                continue  # put together from scenes: those scenes are the progress items
            key = f"film:{film['id']}"
            if film.get("pending_import_path"):
                result[key] = {"phase": "verifying"}
                continue
            client_id = film.get("download_client_id")
            if client_id not in queues_by_client:
                slots_by_nzo = _client_slots(download_client_repo.get_one(_db, client_id) if client_id else None)
                queues_by_client[client_id] = slots_by_nzo
            slot = queues_by_client[client_id].get(film.get("download_job_id"))
            if slot is None:
                result[key] = {"phase": "waiting_import"}
            else:
                result[key] = {"phase": "downloading", "status": slot.get("status"), "percentage": slot.get("percentage"),
                               "timeleft": slot.get("timeleft"), "mb": slot.get("mb"), "mbleft": slot.get("mbleft")}
    except Exception:
        logger.exception("download_progress: movies skipped")
    # Moved into the library, Stash scan + verification still pending
    # (2026-09-26) -- from the database, so it survives a restart.
    for release in release_repo.list_pending_sidecar_verifications(_db):
        result.setdefault(release["scene_stashdb_id"], {"phase": "verifying"})
    # Import phases of releases the queries above don't cover.
    for scene_id, entry in live.items():
        if entry.get("phase") == "waiting_import" and scene_id not in result:
            # The download-finished note of a release that has no download job any more (sent back to searching meanwhile): nothing will
            # ever import it, so drop the note instead of counting it as running (v0.246.11).
            import_progress.clear(entry.get("provider"), scene_id)
            continue
        result.setdefault(scene_id, _import_phase(entry, scene_id))
    _overlay_torrent_health(result, queues_by_client)
    return result


def _overlay_torrent_health(result, queues_by_client=None):
    """What the torrent monitor knows (v0.231.0): a torrent that is stalled / stuck gets its note and swarm numbers; one in error or gone from the
    client is no longer shown as "waiting for the import"."""
    try:
        now, cfg, interval = time.time(), torrent_health.load_cfg(_db), torrent_health.check_interval_s(_db)
        for r in torrent_health.list_rows(_db):
            key = r["scene_id"] if r["kind"] == "scene" else f"film:{r['film_id']}"
            # The client's numbers of this moment (the poll that feeds the progress bars, no extra request); without them the stored look's note
            # is carried on by the time since that look.
            slot = ((queues_by_client or {}).get(r["client_id"]) or {}).get(r["job_id"]) or {}
            live = torrent_health.live_view(r, slot.get("_torrent"), now, cfg)
            if live:
                r = {**r, **{k: live[k] for k in ("state", "detail", "seeds", "peers", "availability", "raw_state")}}
            else:
                r["detail"] = torrent_health.live_detail(r, now, cfg, interval)
            entry = result.get(key)
            if entry is None or entry.get("phase") not in ("downloading", "waiting_import"):
                continue
            info = {"health": r["state"], "health_detail": r["detail"], "seeds": r["seeds"], "peers": r["peers"], "availability": r["availability"]}
            if r["state"] in (torrent_health.MISSING, torrent_health.ERROR):
                result[key] = {"phase": "torrent_problem", **info}
            elif live or r["state"] != torrent_health.OK or r["detail"]:
                entry.update(info)  # with the client's live numbers the swarm (seeds / peers) is always shown
    except Exception:
        logger.exception("download_progress: torrent health skipped")


def _import_phase(entry, scene_id=None):
    """import_progress entry -> the progress shape the plugin renders."""
    out = {"phase": entry["phase"]}
    for key in ("method", "can_local", "percentage", "bytes_done", "bytes_total", "speed_bps", "speed_x", "seconds_left"):
        if entry.get(key) is not None:
            out[key] = entry[key]
    if entry["phase"] == "waiting_encode":
        # Who is expected to encode, and since when it waits (v0.232.1): the row says "waiting for the encode worker -- offline" instead of
        # looking like a stuck import. Constant values only (queued_at, online, name), so the live push does not fire every few seconds.
        worker = encode_queue.worker_status()
        out["worker_online"] = bool(worker["online"])
        if worker["name"]:
            out["worker_name"] = worker["name"]
        queued = encode_queue.queued_at(scene_id) if scene_id else None
        out["queued_at"] = int(queued if queued else entry.get("since") or 0) or None
    return out


@router.get("/reviews")
def reviews():
    """Every open "possible match" decision (2026-09-28, Activity > Needs
    review): wishlist scenes whose search found a candidate that needs a
    human yes/no, and upgrade candidates with a stored one."""
    wishlist = []
    seen = set()
    with _db.connect() as conn:
        for r in conn.execute(
                """SELECT * FROM release WHERE status = 'sent_to_whisparr' AND download_job_id IS NULL
                   AND last_search_result = 'needs_review' AND review_candidate IS NOT NULL
                   ORDER BY last_search_at DESC"""):
            key = (r["provider"], r["scene_stashdb_id"])
            if key in seen:
                continue
            seen.add(key)
            row = dict(r)
            row.pop("last_search_details", None)  # the search log is loaded when "Last" is opened, not with every row
            try:
                row["review_candidate"] = json.loads(row["review_candidate"])
            except (TypeError, ValueError):
                continue
            wishlist.append(row)
        upgrades_rows = []
        for r in conn.execute(
                """SELECT * FROM upgrade_candidate WHERE status = 'wanted' AND review_candidate IS NOT NULL
                   ORDER BY last_search_at DESC"""):
            row = dict(r)
            try:
                row["review_candidate"] = json.loads(row["review_candidate"])
            except (TypeError, ValueError):
                continue
            upgrades_rows.append(row)
    # A 2160p candidate is scaled down to 1080p before the import unless the profile wants 2160p itself (file_importer._maybe_reencode).
    profile, _ = resolve_quality_profiles(_db)
    wants_2160 = 2160 in ((profile or {}).get("resolutions") or [])
    for row in wishlist:
        res = (((row["review_candidate"].get("quality") or {}).get("quality") or {}).get("resolution"))
        row["review_candidate"]["reencode"] = bool(res == 2160 and not wants_2160 and not row.get("is_vr"))
    # Never hand the download URL (carries the indexer's API key) to the browser.
    for row in wishlist + upgrades_rows:
        row["review_candidate"].pop("downloadUrl", None)
        row["review_candidate"]["infoUrl"] = safe_info_url(row["review_candidate"].get("infoUrl"))
    return {"wishlist": wishlist, "upgrades": upgrades_rows}


@router.post("/{provider}/{entity_type}/{entity_stashdb_id}/{scene_stashdb_id}/handoff")
def handoff(provider: str, entity_type: str, entity_stashdb_id: str, scene_stashdb_id: str, search: bool = True):
    """Explicit hand-off to Whisparr -- the frontend must have already
    shown a confirmation step before calling this (see PROGRESS.md's
    confirm-first hand-off UX decision); this endpoint performs the
    action unconditionally once called. Any provider since 2026-09-25
    -- see handoff.hand_off() for which path a release takes."""
    whisparr_quality_profile_id, whisparr_vr_quality_profile_id = resolve_whisparr_quality_profiles(_db)
    quality_profile, vr_search_enabled = resolve_quality_profiles(_db)
    try:
        result = hand_off(
            _db, _get_whisparr_client(), provider, scene_stashdb_id, entity_type, entity_stashdb_id,
            whisparr_quality_profile_id, whisparr_vr_quality_profile_id, _settings.whisparr_root_folder_path,
            quality_profile, vr_search_enabled, search_source=_get_search_source(),
            get_provider_client=_get_provider_client, get_stash_client=_get_stash_client, search=search,
            on_start=lambda already_sent: history.scene(
                "info", "wishlist", "Search now (searched again)" if already_sent and search
                else ("Added to the wishlist (search follows by schedule)" if not search else "Added to the wishlist"),
                provider, scene_stashdb_id),
        )
    except ValueError as e:
        raise HTTPException(404, str(e))
    return result


@router.post("/{provider}/{scene_stashdb_id}/whisparr-fallback")
def whisparr_fallback(provider: str, scene_stashdb_id: str):
    """Manual fallback to Whisparr's own automatic search+grab (Milestone
    3, 2026-09-19) -- for a release this project's own indexer search
    hasn't found anything acceptable for yet. Scene-scoped, not entity-
    scoped, same reasoning as dismiss()/undismiss() above -- Whisparr's
    own movie record is per-scene regardless of which watched entity
    surfaced it. See handoff.py's fallback_to_whisparr()."""
    try:
        result = fallback_to_whisparr(_db, _get_whisparr_client(), provider, scene_stashdb_id)
    except ValueError as e:
        raise HTTPException(404, str(e))
    return result


@router.get("/{provider}/{scene_stashdb_id}/last-search")
def last_search(provider: str, scene_stashdb_id: str):
    """Search log (2026-09-25, Indexers-tab stage 4): what the last
    automatic search sent where, and every candidate with its verdict
    -- see indexer_search._explain_candidates(). 404 when no search has
    been logged for this scene yet (searches before this feature
    existed only recorded a result code)."""
    details = release_repo.get_search_details_for_scene(_db, provider, scene_stashdb_id)
    if details is None:
        raise HTTPException(404, "no search logged for this release yet")
    return details


def _tried_reason(title, events):
    """Why a manual-search result is marked: what the scene's history last says about this release name."""
    for ev in events:  # newest first
        msg = ev["message"] or ""
        if title and title in msg:
            if "rejected (blocklisted)" in msg or "Possible match rejected" in msg:
                return "declined in Needs review on " + ev["at"][:10]
            if "failed" in msg.lower() or msg.startswith("Blocklisted"):
                return "download failed or blocklisted on " + ev["at"][:10]
    return "blocked for this scene before"


@router.post("/{provider}/{scene_stashdb_id}/manual-search")
def manual_search(provider: str, scene_stashdb_id: str, body: dict):
    """Manual search with a user-typed query (2026-09-22, Prowlarr only
    until 2026-09-25; now any sidecar source -- `source` in the body
    picks 'prowlarr' or 'indexers', see main.py's
    get_manual_search_sources(); omitted = the first available) --
    for a release the automatic search (see indexer_search.py's
    search_and_grab()) found nothing acceptable for. Scene-scoped, not
    entity-scoped, same reasoning as whisparr-fallback above. Results
    are deliberately UNFILTERED -- no quality-profile/VR/plausibility
    gate, unlike the automatic path -- the whole point of a manual
    query is the human already decided to override automatic matching;
    each result's title is still shown in full so they can judge.
    Sorted by indexer priority, then age, then size, for display only."""
    query = (body.get("query") or "").strip()
    if not query:
        raise HTTPException(400, "query is required")
    sources = _get_manual_search_sources()
    if not sources:
        raise HTTPException(400, "No Prowlarr or direct indexers configured (Settings > Connections / Indexers)")
    name = body.get("source") or next(iter(sources))
    source = sources.get(name)
    if source is None:
        raise HTTPException(400, f"search source {name!r} isn't configured")
    try:
        results = source.search(query)
    except Exception as e:
        raise HTTPException(502, f"{name} search failed: {e}")
    priorities = source.indexer_priorities()
    results.sort(key=lambda r: (priorities.get(r["indexerId"], 25), -(r.get("seeders") or 0),
                                 r["age"] if r["age"] is not None else float("inf"), -(r["size"] or 0)))
    # Torrents (v0.231.0): the resolution judged from the scene's length when the name has none, and a size that cannot be right for it
    duration_s = indexer_search.scene_duration(provider, scene_stashdb_id) if not str(scene_stashdb_id).startswith("film-") else None
    for r in results:
        r.update(indexer_search.torrent_facts(r, duration_s))
    # Releases this scene has been through already (declined in Needs review, a download that failed) are marked, so they are not picked
    # again. Only the release's own name counts: its other encodes (another resolution) were never tried.
    blocked = {indexer_search._exact_key(t[len(indexer_search.EXACT_PREFIX):] if str(t).startswith(indexer_search.EXACT_PREFIX) else t)
               for t in release_repo.get_review_rejected_titles(_db, provider, scene_stashdb_id)}
    film_blocked = set()
    if provider == "theporndb":  # the manual search of a MOVIE (Movies > Stuck / Upgrade) asks with the movie's ThePornDB id or "film-<id>"
        with _db.connect() as conn:
            film = conn.execute("SELECT rejected_titles FROM film_want WHERE tpdb_id = ? OR 'film-' || id = ? LIMIT 1",
                                (scene_stashdb_id, scene_stashdb_id)).fetchone()
        try:
            film_blocked = {indexer_search._exact_key(t) for t in (json.loads(film["rejected_titles"]) if film and film["rejected_titles"] else [])}
        except ValueError:
            film_blocked = set()
    dead_hashes = {str(t)[len(indexer_search.HASH_PREFIX):].lower() for t in release_repo.get_review_rejected_titles(_db, provider, scene_stashdb_id)
                   if str(t).startswith(indexer_search.HASH_PREFIX)}
    for r in results:  # a torrent given up before (dead): marked, so it is not picked again by mistake
        if dead_hashes and indexer_search.release_hash(r) in dead_hashes:
            r["tried"] = "this torrent was given up before (no progress)"
    if blocked or film_blocked:
        with _db.connect() as conn:
            events = conn.execute("SELECT at, message FROM history WHERE provider = ? AND scene_id = ? ORDER BY at DESC",
                                  (provider, scene_stashdb_id)).fetchall()
        for r in results:
            key = indexer_search._exact_key(r.get("title"))
            if r.get("tried"):
                continue
            if key in blocked:
                r["tried"] = _tried_reason(r.get("title"), events)
            elif key in film_blocked:
                r["tried"] = "rejected or failed for this movie before"
    return {"results": results, "source": name}


@router.post("/{provider}/{scene_stashdb_id}/manual-search/grab")
def manual_search_grab(provider: str, scene_stashdb_id: str, body: dict):
    """Grabs a candidate the user picked from manual_search()'s own
    results -- same grab_candidate() every other candidate-accepting
    path here already uses (review-candidate/accept above, and
    indexer_search.py's own automatic grab), so download-client
    routing/recording is identical regardless of how the candidate was
    found."""
    candidate = body.get("candidate") or {}
    if not indexer_search.candidate_url(candidate):
        raise HTTPException(400, "candidate.downloadUrl is required")
    # "replace": the flagged torrent of this scene is replaced by the picked one (v0.231.0); only when the monitor knows such a torrent
    old = torrent_health.row_for_scene(_db, provider, scene_stashdb_id) if body.get("replace") else None
    result = grab_candidate(_db, provider, scene_stashdb_id, candidate, replace_job=old is not None)
    if result.get("grab"):
        history.scene("info", "grab", f"Picked by hand from the manual search: {candidate.get('title')}",
                      provider, scene_stashdb_id)
        if old is not None:
            torrent_health.finish_replacement(_db, old)
    return result


@router.post("/{provider}/{scene_stashdb_id}/last-search/grab")
def last_search_grab(provider: str, scene_stashdb_id: str, body: dict):
    """Picks a result of the search log ("Last") by hand, also while another download of the scene is running (v0.231.3). The log keeps no
    download links (they carry the indexer's key), so the result is found again by asking the indexers the logged queries and matching
    title + indexer (+ size). A running torrent of the scene is replaced: removed with its data and never used again (its hash is
    blocklisted); a running Usenet download is left alone and the grab is refused."""
    title, indexer, size = body.get("title"), body.get("indexer"), body.get("size")
    if not title:
        raise HTTPException(400, "title is required")
    details = release_repo.get_search_details_for_scene(_db, provider, scene_stashdb_id)
    if details is None:
        raise HTTPException(404, "no search logged for this release yet")
    queries = [q.get("query") for q in (details.get("queries") or []) if q.get("query")]
    if not queries and details.get("query"):
        queries = [details["query"]]
    sources = _get_manual_search_sources()
    source = sources.get(details.get("backend")) or next(iter(sources.values()), None)
    if source is None or not queries:
        raise HTTPException(400, "this search cannot be repeated (no indexer search source or no logged query)")
    found = None
    for query in queries:
        try:
            results = source.search(query)
        except Exception as e:
            logger.warning("last-search grab: %r failed: %s", query, e)
            continue
        found = next((r for r in results if r.get("title") == title and (indexer is None or r.get("indexer") == indexer)
                      and (size is None or r.get("size") in (None, size))), None)
        if found:
            break
    info_hash = str(body.get("info_hash") or "").lower()
    if found is None and indexer_search._HEX40_RE.match(info_hash):
        # a torrent the indexers do not list (or do not answer for): added from its info hash as a magnet link, which needs no indexer
        magnet = f"magnet:?xt=urn:btih:{info_hash}&dn={quote(str(title), safe='')}"
        found = {"title": title, "indexer": indexer, "protocol": "torrent", "size": size, "infoHash": info_hash, "magnetUrl": magnet, "downloadUrl": magnet}
    if found is None:
        raise HTTPException(404, "the indexers no longer list this result (run the search again or use Manual)")
    old = torrent_health.current_for_scene(_db, provider, scene_stashdb_id)
    if old is not None and indexer_search.release_hash(found) == str(old["job_id"]).lower():
        raise HTTPException(400, "this is the torrent that is downloading already")
    result = grab_candidate(_db, provider, scene_stashdb_id, found, replace_job=old is not None)
    if result.get("grab"):
        history.scene("info", "grab", f"Picked by hand from the search log: {found.get('title')}", provider, scene_stashdb_id)
        if old is not None:
            torrent_health.finish_replacement(_db, old)
    return result


@router.post("/{provider}/{entity_type}/{entity_stashdb_id}/{scene_stashdb_id}/fix-mismatch")
def fix_mismatch(provider: str, entity_type: str, entity_stashdb_id: str, scene_stashdb_id: str):
    """For a release library_status() flagged as `whisparr_file_mismatch`
    -- Whisparr's file doesn't actually match this scene once cross-
    checked against Stash's own scan (see that endpoint's docstring).
    Clears Whisparr's wrong state and re-adds the movie fresh, see
    handoff.py's fix_mismatched_download() for the full sequence. Same
    confirm-first UX contract as handoff() above -- the frontend must
    already have shown a confirmation step, since this deletes a file
    from disk."""
    whisparr_quality_profile_id, whisparr_vr_quality_profile_id = resolve_whisparr_quality_profiles(_db)
    quality_profile, vr_search_enabled = resolve_quality_profiles(_db)
    try:
        result = fix_mismatched_download(
            _db, _get_whisparr_client(), provider, scene_stashdb_id, entity_type, entity_stashdb_id,
            whisparr_quality_profile_id, whisparr_vr_quality_profile_id, _settings.whisparr_root_folder_path,
            quality_profile, vr_search_enabled, search_source=_get_search_source(),
        )
    except ValueError as e:
        raise HTTPException(404, str(e))
    return result


@router.post("/{provider}/{entity_type}/{entity_stashdb_id}/{scene_stashdb_id}/fix-wrong-download")
def fix_wrong_download(provider: str, entity_type: str, entity_stashdb_id: str, scene_stashdb_id: str):
    """"Retry" action for an 'import_failed' release whose
    `import_error_code` is 'fingerprint_mismatch' -- this sidecar's own
    pipeline imported a file that turned out not to match StashDB's own
    fingerprints for the scene (see file_importer.py's `verified is
    False` branch). Distinct from fix-mismatch above (that one is for
    Whisparr's own `hasFile: true` pointing at wrong content). Same
    confirm-first UX contract -- the frontend must already have shown a
    confirmation step, since this deletes a Stash scene record and
    moves a file. See file_importer.fix_wrong_download() for the full
    sequence (blocklist, safe file cleanup via the recycle bin, reset
    to 'backlog')."""
    try:
        result = file_importer.fix_wrong_download(
            _db, _get_stash_client(), _get_whisparr_client(), provider, scene_stashdb_id, entity_type,
            entity_stashdb_id,
            get_setting_value(_db, "whisparr_root_folder_path", _settings.whisparr_root_folder_path),
            get_setting_value(_db, "local_media_mount", _settings.local_media_mount),
            get_setting_value(_db, "recycle_bin_path", DEFAULTS["recycle_bin_path"]),
        )
    except ValueError as e:
        raise HTTPException(404, str(e))
    return result


@router.post("/{provider}/{scene_stashdb_id}/review-candidate/accept")
def accept_review_candidate(provider: str, scene_stashdb_id: str):
    """Grabs the stored RESULT_NEEDS_REVIEW candidate for this scene
    (see indexer_search.py's `_select_review_candidate()`/
    `grab_candidate()`) -- the user's own explicit "yes, get this one"
    decision after reviewing it on the Releases page (Milestone 3,
    2026-09-19), for a candidate that matched every one of the movie's
    credited performers but not closely enough on date/title for this
    project to grab automatically. Scene-scoped, same reasoning as
    dismiss()/whisparr_fallback() above."""
    candidate = release_repo.get_review_candidate_for_scene(_db, provider, scene_stashdb_id)
    if candidate is None:
        raise HTTPException(404, f"No review candidate stored for scene {scene_stashdb_id}")
    history.scene("info", "review", f"Possible match accepted: {candidate.get('title')}", provider, scene_stashdb_id)
    return grab_candidate(_db, provider, scene_stashdb_id, candidate)


@router.post("/{provider}/{scene_stashdb_id}/review-candidate/reject")
def reject_review_candidate(provider: str, scene_stashdb_id: str):
    """The user's own explicit "no, not this one" decision on a
    RESULT_NEEDS_REVIEW candidate -- remembers the exact candidate title
    as rejected (release_repo.add_review_rejected_title()) so the next
    search doesn't immediately re-propose the same one, then re-records
    this scene's search result as a plain title mismatch (accurate: by
    definition nothing else cleared the auto-grab bar either). A later
    search can still propose a genuinely different candidate for this
    scene if the indexers ever post one."""
    candidate = release_repo.get_review_candidate_for_scene(_db, provider, scene_stashdb_id)
    if candidate is None:
        raise HTTPException(404, f"No review candidate stored for scene {scene_stashdb_id}")
    release_repo.add_review_rejected_title(_db, provider, scene_stashdb_id, candidate["title"])
    release_repo.set_search_result_for_scene(_db, provider, scene_stashdb_id, RESULT_TITLE_MISMATCH_ONLY)
    history.scene("warning", "blocklist", f"Possible match rejected (blocklisted): {candidate['title']}",
                  provider, scene_stashdb_id)
    _rejected_candidates[(provider, scene_stashdb_id)] = candidate  # for the Undo (the candidate carries a download link: kept here, not sent around)
    return {"ok": True}


# The candidate of the last rejection per scene, so that Undo can bring it back (in memory: the Undo card lives for seconds; gone after a restart).
_rejected_candidates = {}


@router.post("/{provider}/{scene_stashdb_id}/review-candidate/restore")
def restore_review_candidate(provider: str, scene_stashdb_id: str):
    """Undo of "Not this one" / Reject: the title leaves the scene's blocklist and the candidate is back for review."""
    candidate = _rejected_candidates.pop((provider, scene_stashdb_id), None)
    if candidate is None:
        raise HTTPException(404, "Nothing to undo for this scene (the rejection is too old, or the sidecar was restarted since)")
    release_repo.remove_review_rejected_title(_db, provider, scene_stashdb_id, candidate["title"])
    release_repo.set_review_candidate_for_scene(_db, provider, scene_stashdb_id, candidate)
    release_repo.set_search_result_for_scene(_db, provider, scene_stashdb_id, indexer_search.RESULT_NEEDS_REVIEW)
    history.scene("info", "blocklist", f"Rejection undone, the possible match is back for review: {candidate['title']}", provider, scene_stashdb_id)
    return {"ok": True, "title": candidate["title"]}


@router.post("/{provider}/{scene_stashdb_id}/confirm-file")
def confirm_file(provider: str, scene_stashdb_id: str, body: dict = None):
    """"File is correct" for an Import-failed release (fingerprint_mismatch) -- see file_importer.confirm_mismatched_file().
    body {"submit": true} also submits the fingerprints to StashDB when the setting fingerprint_upload_enabled is on."""
    result = file_importer.confirm_mismatched_file(
        _db, _get_stash_client(), _get_stashdb_client(), provider, scene_stashdb_id, get_provider_client=_get_provider_client,
        submit=bool((body or {}).get("submit")),
        upload_enabled=get_setting_value(_db, "fingerprint_upload_enabled", "false") == "true")
    if result.get("error"):
        raise HTTPException(400, result["error"])
    return result


@router.post("/{provider}/{scene_stashdb_id}/recheck-fingerprints")
def recheck_fingerprints(provider: str, scene_stashdb_id: str):
    """"Re-check fingerprints" on an Import-failed row: asks the provider again, imports on an exact match, otherwise only reports what it found
    -- see file_importer.recheck_failed_import()."""
    result = file_importer.recheck_failed_import(_db, _get_stash_client(), _get_stashdb_client(), provider, scene_stashdb_id,
                                                 get_provider_client=_get_provider_client)
    if result.get("error"):
        raise HTTPException(400, result["error"])
    return result


class LocalScenesRequest(BaseModel):
    scenes: list[dict]


@router.post("/local-scenes")
def local_scenes(body: LocalScenesRequest):
    """{"provider:scene id": local Stash scene id} for the scenes that are in the library (the "Open scene" buttons of one page of a list)."""
    pairs = [(x.get("provider") or "stashdb", x.get("scene_stashdb_id")) for x in body.scenes if x.get("scene_stashdb_id")]
    found = _get_stash_client().local_scene_ids_by_stash_ids(pairs)
    return {"scenes": {f"{prov}:{sid}": local for (prov, sid), local in found.items()}}


@router.get("/{provider}/{scene_stashdb_id}/imported-file")
def imported_file(provider: str, scene_stashdb_id: str):
    """The Stash scene that holds the file this pipeline imported for the release (Activity > Import failed: "Open file in
    Stash" to look at the downloaded video). Found by the recorded path -- one Stash request."""
    paths = [r["pending_import_path"] for r in release_repo.list_rows_for_scene(_db, provider, scene_stashdb_id)
             if r.get("pending_import_path")]
    if not paths:
        raise HTTPException(404, "no imported file is recorded for this release")
    scene = _get_stash_client().find_scene_by_path(paths[0])
    if scene is None:
        raise HTTPException(404, "Stash has not scanned this file (any more)")
    return {"scene_id": scene["id"], "path": paths[0]}


@router.post("/fill-cast")
def fill_cast(body: dict):
    """The performers of wishlist scenes whose cast was never read (v0.213.7): a studio hit shows the studio only, so the Stuck / On Wishlist
    rows get the cast next to it. {"scenes": [{provider, scene_stashdb_id}]} (at most 100) -> {"cast": {"provider:id": {cast_names, cast_pairing}}}
    for every asked scene. StashDB reads in batches, other providers one scene at a time (at most 30 per call). Only a cache of public provider
    data in our own table; a scene the provider no longer knows is stored as '' (not asked again)."""
    asked = [(x.get("provider"), x.get("scene_stashdb_id")) for x in (body.get("scenes") or [])[:100] if x.get("provider") and x.get("scene_stashdb_id")]
    out, missing = {}, {}
    with _db.connect() as conn:
        for prov, sid in asked:
            row = conn.execute("SELECT cast_names, cast_pairing FROM release WHERE provider = ? AND scene_stashdb_id = ? LIMIT 1", (prov, sid)).fetchone()
            if row is None:
                continue
            if row["cast_names"] is None:
                missing.setdefault(prov, []).append(sid)
            else:
                out[f"{prov}:{sid}"] = {"cast_names": row["cast_names"], "cast_pairing": row["cast_pairing"]}
    for prov, ids in missing.items():
        try:
            if prov == "stashdb":
                found = _get_stashdb_client().get_scenes_cast_batch(ids)
            else:
                client = _get_provider_client(prov) if _get_provider_client else None
                found = {}
                for sid in ids[:30]:
                    detail = client.get_scene_detail(sid) if client else None
                    if detail:
                        found[sid] = detail.get("performers") or []
        except Exception:
            logger.exception("fill-cast: reading the cast from %s failed", prov)
            continue
        for sid in ids:
            if prov != "stashdb" and sid not in found and ids.index(sid) >= 30:
                continue  # beyond this call's limit: asked again next time
            cast = found.get(sid)
            names = release_repo.names_text(cast) if cast is not None else ""
            code = release_repo.pairing_code(cast) if cast is not None else ""
            release_repo.set_pairing_for_scene(_db, prov, sid, code, names)
            out[f"{prov}:{sid}"] = {"cast_names": names or "", "cast_pairing": code or ""}
    (_, _), (_, p_name) = _local_lookups()
    for v in out.values():
        v["cast"] = _cast_links(v.get("cast_names"), p_name)
    return {"cast": out}


@router.post("/{provider}/{scene_stashdb_id}/dismiss")
def dismiss(provider: str, scene_stashdb_id: str):
    """Scene-scoped, not entity-scoped -- a scene surfaced via more than
    one watched entity (e.g. a watched Studio AND a watched Performer)
    gets one release row per entity (see db.py's schema docstring), and
    the Releases page now shows those as a single merged entry (see
    PROGRESS.md's "Merge same-scene duplicate releases" section).
    Dismissing it needs to clear *every* underlying row for this scene,
    same reasoning already applied to handoff.py's
    set_status_for_scene() -- an entity-scoped dismiss would leave a
    "ghost" duplicate still showing as new under the other entity."""
    previous = release_repo.snapshot_scene_rows(_db, provider, scene_stashdb_id)
    release_repo.set_status_for_scene(_db, provider, scene_stashdb_id, "dismissed")
    history.scene("info", "dismiss", "Dismissed", provider, scene_stashdb_id)
    out = {"ok": True, "previous": previous}
    try:  # "dismissed 10 of this studio in a row": the card offers to hide the studio (dismiss_streak.py); never blocks the dismiss
        suggest = dismiss_streak.on_dismiss(_db, provider, scene_stashdb_id, previous)
        if suggest:
            out["suggest_hide"] = suggest
    except Exception:
        logger.exception("dismiss: streak bookkeeping failed for %s", scene_stashdb_id)
    # A scene that was on the wishlist is registered in Whisparr (no file): when it is MONITORED there (only after "Search via Whisparr"),
    # Whisparr would keep searching for what the user just dismissed -- unmonitor it (v0.213.5). Best effort, never blocks the dismiss.
    if any(r.get("status") == "sent_to_whisparr" for r in previous) and uses_whisparr(_db, provider):
        try:
            movie = _get_whisparr_client().find_by_stash_id(scene_stashdb_id)
            if movie and movie.get("monitored") and not movie.get("hasFile"):
                _get_whisparr_client().set_movie_monitored(movie["id"], False)
                out["whisparr_unmonitored"] = True
                history.scene("info", "dismiss", "Unmonitored in Whisparr", provider, scene_stashdb_id)
        except Exception:
            logger.exception("dismiss: could not unmonitor %s in Whisparr (the dismiss itself is done)", scene_stashdb_id)
    return out


class RestoreRow(BaseModel):
    provider: str
    scene_stashdb_id: str
    entity_type: str
    entity_stashdb_id: str
    status: str


class RestoreRowsRequest(BaseModel):
    rows: list[RestoreRow]


@router.post("/restore")
def restore_dismissed(body: RestoreRowsRequest):
    """The Undo of every release Dismiss (v0.104.0): puts each row back to the status it had BEFORE the dismiss (new / backlog /
    import_failed ...), unlike undismiss() which sets 'new'."""
    restored = release_repo.restore_rows(_db, [r.model_dump() for r in body.rows])
    if restored:
        try:
            dismiss_streak.on_restore(_db, [r.model_dump() for r in body.rows])
        except Exception:
            logger.exception("restore: streak bookkeeping failed")
    if restored:
        history.event("info", "dismiss", f"Dismiss undone: {restored} release row(s) restored")
    return {"ok": True, "restored": restored}


@router.post("/{provider}/{scene_stashdb_id}/undismiss")
def undismiss(provider: str, scene_stashdb_id: str):
    """Undo a dismiss -- moves the release back to 'new' so it reappears
    in the main list/Calendar. Scene-scoped for the same reason as
    dismiss() above. Added 2026-09-12 after the user asked what Dismiss
    actually does and found there was no way back once clicked."""
    release_repo.set_status_for_scene(_db, provider, scene_stashdb_id, "new")
    history.scene("info", "dismiss", "Dismiss undone (back to new)", provider, scene_stashdb_id)
    return {"ok": True}


class BulkScene(BaseModel):
    provider: str
    scene_stashdb_id: str


class BulkStatusRequest(BaseModel):
    action: str  # "dismiss" | "undismiss"
    scenes: list[BulkScene]


@router.post("/{provider}/{scene_stashdb_id}/retry-failed")
def retry_failed(provider: str, scene_stashdb_id: str):
    """"Search again" for an 'import_failed' release that has no automatic
    fix (2026-09-26 -- e.g. failures from before reasons were recorded):
    back on the wishlist with the download job/import state cleared, so
    the next indexer-search run looks for it again."""
    rows = release_repo.list_rows_for_scene(_db, provider, scene_stashdb_id)
    if not rows:
        raise HTTPException(404, "release not found")
    release_repo.set_status_for_scene(_db, provider, scene_stashdb_id, "sent_to_whisparr")
    release_repo.set_download_job_for_scene(_db, provider, scene_stashdb_id, None, None)
    release_repo.set_import_error_for_scene(_db, provider, scene_stashdb_id, None, None)
    release_repo.set_pending_import_path_for_scene(_db, provider, scene_stashdb_id, None)
    history.scene("info", "wishlist", "Search again: back on the wishlist", provider, scene_stashdb_id)
    return {"ok": True, "status": "sent_to_whisparr"}


@router.post("/{provider}/{scene_stashdb_id}/retry-encode")
def retry_encode(provider: str, scene_stashdb_id: str):
    """"Retry the encode" for an 'import_failed' release whose re-encode failed (import_error_code 'encode_failed'): the download is still in
    place, so it goes back to the import (job kept) and an import run starts, which queues the encode again."""
    rows = release_repo.list_rows_for_scene(_db, provider, scene_stashdb_id)
    if not rows or rows[0].get("import_error_code") != "encode_failed" or not rows[0].get("download_job_id"):
        raise HTTPException(400, "only a release whose re-encode failed (download still in place) can be retried")
    release_repo.set_status_for_scene(_db, provider, scene_stashdb_id, "sent_to_whisparr")
    release_repo.set_import_error_for_scene(_db, provider, scene_stashdb_id, None, None)
    history.scene("info", "import", "Retry the re-encode", provider, scene_stashdb_id)
    if file_importer.on_encode_done:
        file_importer.on_encode_done()  # starts an import run
    return {"ok": True, "status": "sent_to_whisparr"}


@router.post("/reclassify-late-additions")
def reclassify_late_additions(dry_run: bool = True):
    """One-off move of already-stored back-catalogue additions from
    'new' to 'backlog' (2026-09-26, see release_repo's function of the
    same name). Dry run by default."""
    from routers.settings import get_setting_value
    days = get_setting_value(_db, "late_addition_days", "90")
    days = int(days) if str(days).isdigit() else 90
    if days == 0:
        raise HTTPException(409, "late_addition_days is 0 (rule disabled)")
    return {"days": days, **release_repo.reclassify_late_additions(_db, days, dry_run=dry_run)}


@router.post("/bulk-status")
def bulk_status(body: BulkStatusRequest):
    """Bulk dismiss/undismiss for the Calendar/List "Dismiss N shown"
    button and its Undo (2026-09-24, user request -- e.g. clearing out
    every "PMV" title a text filter turned up). Only 'new' -> 'dismissed'
    and back, see release_repo.bulk_set_status_for_scenes() for why."""
    if body.action == "dismiss":
        from_status, to_status = "new", "dismissed"
    elif body.action == "undismiss":
        from_status, to_status = "dismissed", "new"
    else:
        raise HTTPException(400, "action must be 'dismiss' or 'undismiss'")
    scenes = [(s.provider, s.scene_stashdb_id) for s in body.scenes]
    changed = release_repo.bulk_set_status_for_scenes(_db, scenes, from_status, to_status)
    if changed:
        history.event("info", "dismiss", f"Bulk {body.action}: {len(scenes)} scene(s)")
    return {"ok": True, "changed": changed}


class LibraryStatusScene(BaseModel):
    scene_stashdb_id: str
    is_vr: bool = False


class LibraryStatusRequest(BaseModel):
    scenes: list[LibraryStatusScene]


def _whisparr_file_is_gone(remote_path):
    """True when Whisparr's file really is not on disk any more. Careful: an unmounted or empty media share must NOT look like "every
    file is gone" -- the top folder of the path (e.g. /media/scenes) has to exist and contain entries."""
    root = (get_setting_value(_db, "whisparr_root_folder_path", _settings.whisparr_root_folder_path) or "/data").rstrip("/")
    mount = (get_setting_value(_db, "local_media_mount", _settings.local_media_mount) or "/media").rstrip("/")
    try:
        local = file_importer._to_local_path(remote_path, root, mount)
    except ValueError:
        return False
    parts = local[len(mount):].lstrip("/").split("/")
    top = os.path.join(mount, parts[0]) if parts and parts[0] else mount
    try:
        if not os.path.isdir(top) or not os.listdir(top):
            return False
    except OSError:
        return False
    return not os.path.exists(local)


@router.post("/library-status")
def library_status(body: LibraryStatusRequest):
    """For a list of StashDB scenes, reports whether each is already in
    the local Stash library, at what quality, and whether that quality
    already `satisfied` the applicable Whisparr profile's cutoff --
    2026-09-13, so the Backlog/New releases lists can show "already
    have this" (or hide the entry entirely once nothing's left to do)
    instead of inviting a pointless duplicate hand-off (user's report:
    a backlog scene, "Mean Girls", turned out to already be a local
    Stash scene).

    Stash-only for the "in library"/quality part -- user pointed out
    that once a scene is actually in the library, Stash's own file
    metadata already has the quality answer, so there's no need for a
    second connection there. One single batched Stash query for the
    whole list (see stash_client.py's library_heights_by_stash_ids()).
    Whisparr *is* still consulted here, but only for the small,
    fixed-size quality-profile definition (to resolve its cutoff to a
    resolution, see whisparr_client.py's
    get_quality_profile_cutoff_resolution()) -- not the heavier
    per-scene `/movie?stashId=` lookup that was dropped from this same
    endpoint earlier, which scaled with Whisparr's own movie-library
    size.

    `is_vr` per scene (already known from the release row's own is_vr
    column, set at discovery/backlog-fetch time) picks which of the
    two configured profiles' cutoff applies, mirroring handoff.py's
    own normal/VR profile choice.

    Side effect (2026-09-13, user's request to keep the database
    smaller): a scene confirmed in_library also has its stored
    image_url cleared here -- the thumbnail only exists to help decide
    on a hand-off, which is moot once the file's already there. Runs on
    every call, not just the first time a scene is found in-library --
    release_repo.clear_image_urls_for_scenes() is a cheap no-op once
    already cleared.

    Also reports `whisparr_exists`/`whisparr_has_file` per scene
    (2026-09-13) -- the per-scene `/movie?stashId=` lookup this same
    endpoint had dropped earlier for the in-library/quality check (see
    above) is back, but for a different question that only Whisparr can
    answer: is this scene already tracked there at all, and does it
    have a file yet. The frontend uses this to choose between "Send to
    Whisparr" (not tracked yet) and "Search in Whisparr" (tracked, no
    file -- the add already happened, possibly outside this project's
    own flow, see handoff.py's MovieAlreadyExistsError handling; a
    fresh search might now find something the original one didn't).
    Skipped for a scene already `satisfied` -- that row gets hidden by
    the frontend regardless, so there's nothing to decide a label for.
    Re-verified live 2026-09-13 that the filtered lookup itself is fast
    (~15ms) regardless of Whisparr's total movie count, unlike the
    unfiltered `GET /movie` this project avoids everywhere else -- one
    call per not-yet-satisfied scene in the currently-displayed list
    (bounded to that entity's own remaining backlog, not the whole
    catalog) is cheap. Best-effort: a Whisparr hiccup here just leaves
    both flags false, defaulting the button to "Send to Whisparr"."""
    if not body.scenes:
        return {}
    is_vr_by_id = {s.scene_stashdb_id: s.is_vr for s in body.scenes}
    scene_ids = list(is_vr_by_id.keys())

    stash_client = _get_stash_client()
    height_by_id = stash_client.library_heights_by_stash_ids(scene_ids)
    if height_by_id:
        release_repo.clear_image_urls_for_scenes(_db, "stashdb", list(height_by_id.keys()))

    quality_profile_id, vr_quality_profile_id = resolve_whisparr_quality_profiles(_db)
    whisparr_client = _get_whisparr_client()

    def cutoff_for(profile_id):
        if not profile_id:
            return None
        try:
            return whisparr_client.get_quality_profile_cutoff_resolution(profile_id)
        except Exception:
            return None  # best-effort -- a Whisparr hiccup just means nothing gets marked "satisfied"

    normal_cutoff = cutoff_for(quality_profile_id)
    vr_cutoff = cutoff_for(vr_quality_profile_id) if vr_quality_profile_id else None

    result = {}
    for scene_id in scene_ids:
        height = height_by_id.get(scene_id)
        in_library = height is not None
        applicable_cutoff = vr_cutoff if (is_vr_by_id[scene_id] and vr_cutoff is not None) else normal_cutoff
        satisfied = bool(in_library and applicable_cutoff is not None and height >= applicable_cutoff)
        whisparr_exists = False
        whisparr_has_file = False
        whisparr_file_mismatch = False
        mismatch_title = None
        mismatch_scene_id = None
        if not satisfied:
            try:
                movie = whisparr_client.find_by_stash_id(scene_id)
            except Exception:
                movie = None  # best-effort, same reasoning as cutoff_for() above
            if movie is not None:
                whisparr_exists = True
                whisparr_has_file = bool(movie.get("hasFile"))
                # Self-healing (v0.122.0): Whisparr still lists a file that is gone from disk (moved to the recycle bin as a duplicate, deleted
                # by hand, ...) -- it would say "awaiting Stash rescan" for ever. Only its record is dropped, never a file.
                stale_path = (movie.get("movieFile") or {}).get("path")
                if whisparr_has_file and stale_path and _whisparr_file_is_gone(stale_path):
                    try:
                        whisparr_client.delete_movie_file(movie["movieFile"]["id"])
                        whisparr_has_file = False
                        history.event("info", "wishlist", f"Whisparr's file record for {movie.get('title')} removed -- the file is no longer on disk",
                                      details={"path": stale_path})
                    except Exception:
                        logger.exception("library_status: could not drop the stale Whisparr file record of movie %s", movie.get("id"))
                # 2026-09-14, generalized 2026-09-15 to check against
                # StashDB's own registered fingerprints for this scene
                # (see verification.py) rather than just whatever else
                # is already in the local library -- Whisparr's own
                # hasFile doesn't mean the file is actually THIS scene
                # (see PROGRESS.md's "Boosting Morale"/"Summer Camp
                # Rules" investigation), and the narrower "does it
                # collide with an existing scene" check only ever
                # caught that by coincidence. Best-effort, only for the
                # (small) set of not-yet-satisfied scenes that already
                # claim to have a file.
                if whisparr_has_file:
                    file_path = (movie.get("movieFile") or {}).get("path")
                    if file_path:
                        verified, found_title = verify_downloaded_file(
                            stash_client, _get_stashdb_client(), scene_id, file_path,
                        )
                        if verified is False:
                            whisparr_file_mismatch = True
                            mismatch_title = found_title
                            try:  # the Stash scene that holds that file, so the UI can link to it (v0.121.0)
                                holder = stash_client.find_scene_by_path(file_path)
                                mismatch_scene_id = holder["id"] if holder else None
                            except Exception:
                                mismatch_scene_id = None
        result[scene_id] = {
            "in_library": in_library,
            "quality": quality_label(height) if height else None,
            "satisfied": satisfied,
            "whisparr_exists": whisparr_exists,
            "whisparr_has_file": whisparr_has_file,
            "whisparr_file_mismatch": whisparr_file_mismatch,
            "mismatch_title": mismatch_title,
            "mismatch_scene_id": mismatch_scene_id,
        }
    return result
