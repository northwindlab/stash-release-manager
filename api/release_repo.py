"""CRUD/query for discovered releases."""
import events
import json
import re
import unicodedata


def _changed(provider, scene_stashdb_id, change):
    """Live push (2026-09-26, events.py): tells connected browsers that a
    release's state changed -- called AFTER the write's commit."""
    events.publish("release_changed", provider=provider, scene_id=scene_stashdb_id, change=change)


def _normalize_title(title):
    """Title identity across providers (v0.101.3): case, accents, "&" vs "and" and all punctuation are ignored, so "Episode 208:
    Anna And Ben" (ThePornDB) and "Episode 208: Anna & Ben" (StashDB) are the same scene. Unicode letters/digits are kept
    (a non-Latin title is not reduced to nothing)."""
    t = unicodedata.normalize("NFKD", title or "").lower()
    t = "".join(ch for ch in t if not unicodedata.combining(ch))
    t = t.replace("&", " and ")
    return " ".join(re.sub(r"[\W_]+", " ", t).split())


def find_matching_release(db, provider, entity_type, entity_stashdb_id, scene_date, title):
    """Cross-provider dedup (2026-09-11, see PROGRESS.md's "Cross-provider
    dedup" section): finds an existing release row for this same watched
    entity, at the given provider, whose scene_date matches exactly and
    whose title matches after whitespace/case normalization -- the
    closest thing to a shared identity two different providers' scene
    ids can have, since there's no real shared id to match on. Returns
    None (never attempts a match) when scene_date is missing -- matching
    on title alone across a whole entity's history would risk false
    merges."""
    if not scene_date:
        return None
    norm = _normalize_title(title)
    if not norm:
        return None  # nothing to compare -- never match two empty titles
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM release WHERE provider = ? AND entity_type = ? AND entity_stashdb_id = ? AND scene_date = ?",
            (provider, entity_type, entity_stashdb_id, scene_date),
        ).fetchall()
    for r in rows:
        if _normalize_title(r["title"]) == norm:
            return dict(r)
    return None


def delete_release(db, provider, scene_stashdb_id, entity_type, entity_stashdb_id):
    with db.connect() as conn:
        conn.execute(
            "DELETE FROM release WHERE provider = ? AND scene_stashdb_id = ? AND entity_type = ? AND entity_stashdb_id = ?",
            (provider, scene_stashdb_id, entity_type, entity_stashdb_id),
        )


def tags_text(tags):
    """'|a|b|' for a provider scene's tags (dicts with a name, or plain strings); None when the scene carries no tag information at all
    (so the backfill fetches it), '' when it has none."""
    if tags is None:
        return None
    names = []
    for t in tags:
        n = (t.get("name") if isinstance(t, dict) else t) or ""
        n = str(n).strip().replace("|", " ")
        if n and n not in names:
            names.append(n)
    return "|" + "|".join(names) + "|" if names else ""


# Scene pairing (v0.182.0): the cast of a scene by gender, the way the scene scene calls it: B = male, G = female, T = trans / non-binary /
# intersex, ? = no gender given. 'B/G', 'G/G', 'B/G/G' ...; five or more performers are just 'Group'. NULL = not read yet, '' = no cast.
_GENDER_LETTER = {"MALE": "B", "FEMALE": "G", "TRANSGENDER_MALE": "T", "TRANSGENDER_FEMALE": "T", "INTERSEX": "T", "NON_BINARY": "T"}
_LETTER_ORDER = "BGT?"
PAIRING_GROUP_FROM = 5


def pairing_code(performers):
    """`performers`: StashDB's [{"performer": {"gender": ...}}, ...] (or plain [{"gender": ...}]); None when the list was not fetched."""
    if performers is None:
        return None
    letters = []
    for p in performers:
        person = (p or {}).get("performer", p) or {}
        letters.append(_GENDER_LETTER.get(person.get("gender") or "", "?"))
    if len(letters) >= PAIRING_GROUP_FROM:
        return "Group"
    return "/".join(sorted(letters, key=_LETTER_ORDER.index))


def names_text(performers):
    """'|Name A|Name B|' of a scene's performers (StashDB's [{"performer": {"name": ...}}]); None when not fetched, '' when no cast."""
    if performers is None:
        return None
    names = []
    for p in performers:
        n = str(((p or {}).get("performer", p) or {}).get("name") or "").strip().replace("|", " ")
        if n and n not in names:
            names.append(n)
    return "|" + "|".join(names) + "|" if names else ""


def _scene_fully_dismissed(conn, provider, scene_id):
    """True when the scene has rows and every one of them is dismissed by the user (not hidden-studio rows: those come back with the studio,
    so a performer hit of the same scene stays visible)."""
    rows = conn.execute("SELECT status, muted FROM release WHERE provider = ? AND scene_stashdb_id = ?", (provider, scene_id)).fetchall()
    return bool(rows) and all(r["status"] == "dismissed" and not r["muted"] for r in rows)


def insert_release(db, conn, scene, provider, entity_type, entity_stashdb_id, status="new", late_addition=False):
    """Insert one (provider, scene, entity) release row within an
    already-open connection (called from watcher.run_scan, which batches
    many inserts per connection for efficiency). INSERT OR IGNORE -- a
    scene can already be recorded against this same entity from a prior
    partial run (or already tracked at some other status, e.g. a
    backlog fetch shouldn't clobber a scene already 'new'/'dismissed'/
    'sent_to_whisparr' -- INSERT OR IGNORE leaves the existing row
    untouched in that case). `scene` is a normalized dict regardless of
    provider -- see stashdb_client.py/theporndb_client.py's
    fetch_new_scenes(). `status` defaults to 'new' for the regular scan;
    the on-demand backlog fetch (api/routers/backlog.py, 2026-09-13)
    passes 'backlog' instead. `late_addition` (2026-09-26): the scan
    routed a back-catalogue addition to 'backlog', see
    watcher._store_new_scenes()."""
    studio_id = (scene.get("studio") or {}).get("id")
    muted, prev_status = 0, None
    # A hidden studio (muted_studio) stays out of the Calendar: stored as 'dismissed' + muted, so un-hiding can bring it back.
    # Only releases that came in through the STUDIO; upgrade rows and performer hits are never hidden this way.
    if (studio_id and entity_type == "studio" and not str(entity_stashdb_id).startswith(("upgrade:", "film:"))
            and status in ("new", "backlog")
            and conn.execute("SELECT 1 FROM muted_studio WHERE studio_id = ?", (studio_id,)).fetchone()):
        muted, prev_status, status = 1, status, "dismissed"
    # A scene the user already dismissed stays dismissed when another watched entity reports it later (v0.211.2): Dismiss covers the rows
    # that exist at that moment, and this new entity key would otherwise bring the scene back as 'new'.
    elif status == "new" and not str(entity_stashdb_id).startswith(("upgrade:", "film:")) and _scene_fully_dismissed(conn, provider, scene["id"]):
        status = "dismissed"
    conn.execute(
        """INSERT OR IGNORE INTO release
           (provider, scene_stashdb_id, entity_type, entity_stashdb_id, title, scene_date,
            scene_created, studio_name, image_url, is_vr, status, details, late_addition,
            studio_id, muted, muted_prev_status, tags, cast_pairing, cast_names)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            provider, scene["id"], entity_type, entity_stashdb_id, scene.get("title") or "",
            scene.get("date"), scene["created"],
            (scene.get("studio") or {}).get("name") or "", scene.get("image_url"),
            int(bool(scene.get("is_vr"))), status,
            # 2026-09-26: description for the "incl. description" filter;
            # '' (not NULL) when the provider has none, so the backfill
            # doesn't re-fetch it.
            scene.get("details") or "",
            int(bool(late_addition)),
            studio_id, muted, prev_status, tags_text(scene.get("tags")),
            pairing_code(scene.get("performers")), names_text(scene.get("performers")),
        ),
    )


def list_releases_missing_details(db, statuses=("new",)):
    """(provider, scene id) pairs whose description was never fetched
    (details IS NULL -- rows from before 2026-09-26), deduped by scene,
    for main.py's one-off details backfill. Only 'new' by default --
    that's what the Releases page filters over."""
    placeholders = ",".join("?" * len(statuses))
    with db.connect() as conn:
        rows = conn.execute(
            f"""SELECT DISTINCT provider, scene_stashdb_id FROM release
                WHERE details IS NULL AND status IN ({placeholders})""",
            tuple(statuses),
        ).fetchall()
    return [(r["provider"], r["scene_stashdb_id"]) for r in rows]


def set_details_for_scene(db, provider, scene_stashdb_id, details):
    with db.connect() as conn:
        conn.execute("UPDATE release SET details = ? WHERE provider = ? AND scene_stashdb_id = ?",
                     (details or "", provider, scene_stashdb_id))


def clear_image_urls_for_scenes(db, provider, scene_stashdb_ids):
    """Nulls out image_url for every release row referencing any of
    these scenes, regardless of entity or status -- called once a
    scene is confirmed to already be in the local Stash library
    (2026-09-13, user's request to keep the database smaller): the
    thumbnail only exists to help decide whether a release is worth
    handing off, and that decision is moot once the file is already
    there. Scene-scoped like set_status_for_scene() -- the same real
    scene can appear under more than one watched entity. The `image_url
    IS NOT NULL` guard just avoids a pointless write for a scene
    that's already cleared, not a correctness requirement."""
    if not scene_stashdb_ids:
        return
    with db.connect() as conn:
        placeholders = ",".join("?" for _ in scene_stashdb_ids)
        conn.execute(
            f"""UPDATE release SET image_url = NULL
                WHERE provider = ? AND scene_stashdb_id IN ({placeholders}) AND image_url IS NOT NULL""",
            (provider, *scene_stashdb_ids),
        )


def set_image_url(db, provider, scene_stashdb_id, entity_type, entity_stashdb_id, image_url):
    """Used only by the one-off image_url backfill for releases
    discovered before that column existed -- see db.py's
    `_migrate_release_image_url()`. Regular scans set it directly via
    insert_release() instead."""
    with db.connect() as conn:
        conn.execute(
            """UPDATE release SET image_url = ?
               WHERE provider = ? AND scene_stashdb_id = ? AND entity_type = ? AND entity_stashdb_id = ?""",
            (image_url, provider, scene_stashdb_id, entity_type, entity_stashdb_id),
        )


def list_releases(db, status="new", entity_type=None, entity_stashdb_id=None, limit=200, offset=0):
    # LEFT JOINed against watchlist for `entity_local_id` -- this Stash
    # instance's own numeric id for the watched entity (added 2026-09-12,
    # see PROGRESS.md's "Releases page: default to Calendar, link to
    # local Studio/Performer pages" section), so the plugin can link a
    # release to its local Studio/Performer page without a second
    # round-trip per row. LEFT, not INNER: a release whose watchlist row
    # has since been removed (unfavorited) should still show, just
    # without a working local link.
    #
    # `entity_name` (2026-09-20) -- the watched entity's own name
    # (watchlist.name), distinct from `release.studio_name` (the scene's
    # own studio, always populated regardless of which entity type
    # actually triggered the match). A release matched via a watched
    # PERFORMER needs the performer's name for the List view's entity
    # link -- `studio_name` alone can't provide that.
    query = """
        SELECT {cols}, watchlist.local_id AS entity_local_id, watchlist.name AS entity_name
        FROM release
        LEFT JOIN watchlist
            ON watchlist.provider = release.provider
           AND watchlist.entity_type = release.entity_type
           AND watchlist.stashdb_id = release.entity_stashdb_id
        WHERE release.status = ?
    """
    params = [status]
    if entity_type:
        query += " AND release.entity_type = ?"
        params.append(entity_type)
    if entity_stashdb_id:
        query += " AND release.entity_stashdb_id = ?"
        params.append(entity_stashdb_id)
    query += " ORDER BY release.scene_created DESC LIMIT ? OFFSET ?"
    params += [limit, offset]
    with db.connect() as conn:
        rows = conn.execute(query.replace("{cols}", _list_columns(conn)), params).fetchall()
        return [_listed(r) for r in rows]


def _list_columns(conn):
    """`release.*` without the search log: the SELECT list of the listings, so the log is not even read from the database (see _listed())."""
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(release)") if r["name"] != "last_search_details"]
    return ", ".join(f"release.{c}" for c in cols)


def _listed(row):
    """A release row as the listings hand it out: without the search log (`last_search_details`, up to a few hundred results per scene). The
    page asks for it on demand (GET /releases/<provider>/<scene>/last-search); in every row of every list it would be megabytes."""
    d = dict(row)
    d.pop("last_search_details", None)
    return d


_RELEASE_WITH_ENTITY = """
    SELECT {cols}, watchlist.local_id AS entity_local_id, watchlist.name AS entity_name
    FROM release
    LEFT JOIN watchlist
        ON watchlist.provider = release.provider
       AND watchlist.entity_type = release.entity_type
       AND watchlist.stashdb_id = release.entity_stashdb_id
"""
# A release's calendar day: its scene date, or its created date if the
# provider has none -- the same fallback the List/Calendar views group by.
_DAY_EXPR = "COALESCE(release.scene_date, substr(release.scene_created, 1, 10))"


def query_releases(db, statuses=("new",), q="", search_description=False, entity_type=None, vr=None,
                   providers=None, date_from=None, date_until=None, page=1, per_page=100, ids_only=False,
                   entities=None, late_addition=None, order="day", search_tags=False, upgrades=None):
    """Server-side filtered + paged release listing (2026-09-26, List view
    paging -- replaces loading up to list_view_limit rows and filtering
    in the browser). Filters apply per release ROW, exactly like the old
    client-side filterListReleases(); paging is over distinct SCENES
    (one scene can have a row per watched entity), newest day first.

    Returns {"total": matching scene count, "rows": every matching row of
    the scenes on this page} -- rows, not merged scenes, so the frontend's
    groupByScene() keeps working unchanged. `ids_only` returns
    {"total", "scenes": [{provider, scene_stashdb_id}]} for ALL matches
    instead (no paging) -- backs "Dismiss all N matching".
    `per_page=0` = no paging (the Calendar's one-month query).

    `entities` (2026-09-26, paging everywhere): optional list of
    (entity_type, entity_stashdb_id) pairs -- only releases of those
    watched entities (an entity page's panel passes one pair per linked
    provider id). `late_addition`: True/False to show only / hide
    back-catalogue additions (see watcher._is_late_addition()). `order`:
    'day' (scene day, newest first -- default), 'handoff' (newest
    hand-off first, for the wishlist section) or 'imported'. `upgrades`: 'only' / 'hide' rows that replace a library file (release.upgrade_of)."""
    where = [f"release.status IN ({','.join('?' * len(statuses))})"]
    params = list(statuses)
    if q and q.strip():
        text = "release.title || ' ' || release.studio_name"
        if search_description:
            text += " || ' ' || COALESCE(release.details, '')"
        if search_tags:  # v0.92.0: tag names ('|a|b|' -> separated by spaces so a word never matches across two tags)
            text += " || ' ' || replace(COALESCE(release.tags, ''), '|', ' ')"
        where.append(f"instr(lower({text}), ?) > 0")
        params.append(q.strip().lower())
    if entity_type in ("studio", "performer"):
        where.append("release.entity_type = ?")
        params.append(entity_type)
    if vr == "vr":
        where.append("release.is_vr = 1")
    elif vr == "non_vr":
        where.append("release.is_vr = 0")
    if providers is not None:
        if not providers:
            return {"total": 0, "scenes": []} if ids_only else {"total": 0, "rows": []}
        where.append(f"release.provider IN ({','.join('?' * len(providers))})")
        params += list(providers)
    if entities is not None:
        pairs = [(t, i) for t, i in entities if t in ("studio", "performer") and i]
        if not pairs:
            return {"total": 0, "scenes": []} if ids_only else {"total": 0, "rows": []}
        where.append("(" + " OR ".join("(release.entity_type = ? AND release.entity_stashdb_id = ?)" for _ in pairs) + ")")
        for pair in pairs:
            params += list(pair)
    if upgrades == "only":  # v0.237.0: rows that replace a library file (upgrades / re-adds) -- On Wishlist's "Only upgrades"
        where.append("release.upgrade_of IS NOT NULL")
    elif upgrades == "hide":
        where.append("release.upgrade_of IS NULL")
    if late_addition is not None:
        where.append("release.late_addition = ?")
        params.append(1 if late_addition else 0)
    if date_from:
        where.append(f"{_DAY_EXPR} >= ?")
        params.append(date_from)
    if date_until:
        where.append(f"{_DAY_EXPR} <= ?")
        params.append(date_until)
    where_sql = " AND ".join(where)
    scenes_sql = f"""SELECT release.provider, release.scene_stashdb_id,
                            MAX({_DAY_EXPR}) AS day, MAX(release.scene_created) AS created,
                            MAX(COALESCE(release.handoff_at, '')) AS handoff,
                            MAX(COALESCE(release.imported_at, '')) AS imported
                     FROM release WHERE {where_sql}
                     GROUP BY release.provider, release.scene_stashdb_id"""
    with db.connect() as conn:
        total = conn.execute(f"SELECT COUNT(*) AS n FROM ({scenes_sql})", params).fetchone()["n"]
        if ids_only:
            rows = conn.execute(scenes_sql, params).fetchall()
            return {"total": total, "scenes": [{"provider": r["provider"], "scene_stashdb_id": r["scene_stashdb_id"]}
                                               for r in rows]}
        order_sql = {"handoff": "handoff DESC, day DESC", "imported": "imported DESC, day DESC"}.get(
            order, "day DESC, created DESC")
        page_sql = scenes_sql + f" ORDER BY {order_sql}"
        page_params = list(params)
        if per_page:
            page_sql += " LIMIT ? OFFSET ?"
            page_params += [per_page, (max(page, 1) - 1) * per_page]
        # Rows of this page's scenes via a JOIN on the paged scene query
        # itself -- NOT an "(provider = ? AND scene = ?) OR ..." chain: a
        # whole Calendar month (per_page=0, hundreds of scenes) blew
        # SQLite's expression-depth limit that way (found live 2026-09-26).
        release_select = _RELEASE_WITH_ENTITY.replace("{cols}", _list_columns(conn))
        rows = conn.execute(
            f"""{release_select}
                JOIN ({page_sql}) AS page_scene
                  ON page_scene.provider = release.provider
                 AND page_scene.scene_stashdb_id = release.scene_stashdb_id
                WHERE {where_sql}
                ORDER BY release.scene_created DESC""",
            page_params + params,
        ).fetchall()
    return {"total": total, "rows": [_listed(r) for r in rows]}


def get_release(db, provider, scene_stashdb_id, entity_type, entity_stashdb_id):
    with db.connect() as conn:
        row = conn.execute(
            """SELECT * FROM release
               WHERE provider = ? AND scene_stashdb_id = ? AND entity_type = ? AND entity_stashdb_id = ?""",
            (provider, scene_stashdb_id, entity_type, entity_stashdb_id),
        ).fetchone()
        return dict(row) if row else None


def set_status(db, provider, scene_stashdb_id, entity_type, entity_stashdb_id, status):
    """Update status for one release row. Stamps handoff_at/imported_at
    alongside the matching status transitions."""
    with db.connect() as conn:
        _update_status(conn,
            "WHERE provider = ? AND scene_stashdb_id = ? AND entity_type = ? AND entity_stashdb_id = ?",
            (provider, scene_stashdb_id, entity_type, entity_stashdb_id), status)
    _changed(provider, scene_stashdb_id, "status")

def set_status_for_scene(db, provider, scene_stashdb_id, status):
    """Update status for every release row referencing this scene (across
    all watched entities that surfaced it) -- used when a hand-off action
    is triggered from one entity's page but the same scene also showed up
    under another watched entity. Still scoped by provider: a scene id is
    only unique within its own provider's id space."""
    with db.connect() as conn:
        _update_status(conn, "WHERE provider = ? AND scene_stashdb_id = ?", (provider, scene_stashdb_id), status)
    _changed(provider, scene_stashdb_id, "status")

_KEY_COLS = "provider, scene_stashdb_id, entity_type, entity_stashdb_id, status"


def _snapshot(conn, where, params):
    """Rows (natural key + current status) that a dismiss is about to change -- the exact state restore_rows() puts back (v0.104.0)."""
    return [dict(r) for r in conn.execute(f"SELECT {_KEY_COLS} FROM release WHERE {where}", params)]


def snapshot_scene_rows(db, provider, scene_stashdb_id):
    with db.connect() as conn:
        return _snapshot(conn, "provider = ? AND scene_stashdb_id = ? AND status != 'dismissed'", (provider, scene_stashdb_id))


def restore_rows(db, rows):
    """Undo of a dismiss: every given row that is STILL dismissed gets its old status back (a row that moved on meanwhile is left
    alone). Returns the number restored."""
    restored, scenes = 0, set()
    with db.connect() as conn:
        for r in rows:
            if r["status"] == "dismissed":
                continue
            cur = conn.execute(
                "UPDATE release SET status = ? WHERE provider = ? AND scene_stashdb_id = ? AND entity_type = ? "
                "AND entity_stashdb_id = ? AND status = 'dismissed'",
                (r["status"], r["provider"], r["scene_stashdb_id"], r["entity_type"], r["entity_stashdb_id"]))
            restored += cur.rowcount
            if cur.rowcount:
                scenes.add((r["provider"], r["scene_stashdb_id"]))
    if restored:
        _changed(None, None, "bulk_status")
    return restored


def _update_status(conn, where_clause, where_params, status):
    if status in ("sent_to_whisparr", "imported"):  # the studio got something wanted: its dismiss streak starts again (dismiss_streak.py)
        conn.execute(f"UPDATE studio_dismiss_streak SET count = 0, asked_at_count = 0 WHERE studio_id IN "
                     f"(SELECT DISTINCT studio_id FROM release {where_clause} AND studio_id IS NOT NULL)", where_params)
    if status == "sent_to_whisparr":
        conn.execute(
            f"UPDATE release SET status = ?, handoff_at = datetime('now') {where_clause}",
            (status, *where_params),
        )
    elif status == "imported":
        conn.execute(
            f"UPDATE release SET status = ?, imported_at = datetime('now') {where_clause}",
            (status, *where_params),
        )
    else:
        conn.execute(
            f"UPDATE release SET status = ? {where_clause}",
            (status, *where_params),
        )


def bulk_set_status_for_scenes(db, scenes, from_status, to_status):
    """Scene-scoped status change for many scenes in one transaction --
    the Calendar/List "Dismiss N shown" bulk action and its Undo
    (2026-09-24, user request). Same scene scoping as
    set_status_for_scene(), but only touches rows currently in
    `from_status`: a filtered view can include scenes that moved on in
    the meantime (searched/imported), and a bulk dismiss must never
    pull those back out of the pipeline. `scenes` is a list of
    (provider, scene_stashdb_id) pairs. Returns the number of release
    rows changed (can exceed len(scenes), one row per watched entity)."""
    changed = 0
    with db.connect() as conn:
        for provider, scene_stashdb_id in scenes:
            cur = conn.execute(
                "UPDATE release SET status = ? WHERE provider = ? AND scene_stashdb_id = ? AND status = ?",
                (to_status, provider, scene_stashdb_id, from_status),
            )
            changed += cur.rowcount
    if changed:
        _changed(None, None, "bulk_status")
    return changed


_OPEN_STATUSES = ("new", "sent_to_whisparr")


def count_open_for_entity(db, provider, entity_type, entity_stashdb_id):
    """How many of this entity's releases are still 'open' (not yet
    imported/dismissed/failed) -- used by the favorite-changed hook
    handler to decide whether an unfavorited entity is even worth
    flagging a keep-or-dismiss decision for for (2026-09-21, see
    pending_favorite_decision_repo.py)."""
    with db.connect() as conn:
        placeholders = ",".join("?" * len(_OPEN_STATUSES))
        row = conn.execute(
            f"""SELECT COUNT(*) AS n FROM release
                WHERE provider = ? AND entity_type = ? AND entity_stashdb_id = ?
                AND status IN ({placeholders})""",
            (provider, entity_type, entity_stashdb_id, *_OPEN_STATUSES),
        ).fetchone()
        return row["n"]


def dismiss_all_open_for_entity(db, provider, entity_type, entity_stashdb_id):
    """The "Dismiss" half of the favorite-changed banner's decision
    (2026-09-21) -- only touches still-open releases, same set
    count_open_for_entity() above counts; an already-imported release
    is left alone regardless (dismissing it would mean nothing anyway,
    see db.py's `status` CHECK -- 'dismissed' is a pre-hand-off state)."""
    with db.connect() as conn:
        placeholders = ",".join("?" * len(_OPEN_STATUSES))
        rows = _snapshot(conn, f"provider = ? AND entity_type = ? AND entity_stashdb_id = ? AND status IN ({placeholders})",
                         (provider, entity_type, entity_stashdb_id, *_OPEN_STATUSES))
        conn.execute(
            f"""UPDATE release SET status = 'dismissed'
                WHERE provider = ? AND entity_type = ? AND entity_stashdb_id = ?
                AND status IN ({placeholders})""",
            (provider, entity_type, entity_stashdb_id, *_OPEN_STATUSES),
        )
    _changed(provider, None, "bulk_status")
    return rows  # for the Undo


def list_sent_to_whisparr(db):
    """Releases currently awaiting import confirmation -- used by the
    periodic/manual import-status check (see api/import_check.py). Only
    ever provider='stashdb' in practice today (ThePornDB-sourced releases
    can't be sent to Whisparr yet, see handoff.py), but not filtered
    explicitly since a theporndb row simply never reaches this status."""
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM release WHERE status = 'sent_to_whisparr'").fetchall()
        return [dict(r) for r in rows]


def set_import_error_for_scene(db, provider, scene_stashdb_id, code, detail):
    """Records why an import attempt failed (2026-09-23) -- scene-scoped
    like set_status_for_scene()/set_download_job_for_scene() above.
    Deliberately NOT bundled with a status change here: some callers
    (the 'source_missing' failure path in file_importer.py) want the
    reason recorded WITHOUT promoting status to 'import_failed', so the
    existing automatic retry loop keeps running -- see this function's
    own callers for which combination each failure path uses. `code` is
    one of 'fingerprint_mismatch'/'scene_gone'/'source_missing'; `None`
    for both args clears a previously-recorded error (used by the "Fix
    wrong download" action once it's done cleaning up)."""
    with db.connect() as conn:
        conn.execute(
            "UPDATE release SET import_error_code = ?, import_error_detail = ? "
            "WHERE provider = ? AND scene_stashdb_id = ?",
            (code, detail, provider, scene_stashdb_id),
        )
    _changed(provider, scene_stashdb_id, "import_error")

def set_download_job_for_scene(db, provider, scene_stashdb_id, download_client_id, download_job_id):
    """Records which configured download_client a grab went to, and
    that client's own job id (e.g. SABnzbd's nzo_id) -- Milestone 3,
    see indexer_search.py. Scene-scoped like set_status_for_scene() (a
    scene can have more than one release row, one per watched entity
    that surfaced it). Replaces the old approach of re-deriving this
    from Whisparr's own 'grabbed' history event, now that Whisparr no
    longer does the grab itself (see handoff.py)."""
    with db.connect() as conn:
        conn.execute(
            """UPDATE release SET download_client_id = ?, download_job_id = ?
               WHERE provider = ? AND scene_stashdb_id = ?""",
            (download_client_id, download_job_id, provider, scene_stashdb_id),
        )
    _changed(provider, scene_stashdb_id, "download_job")

def set_grabbed_for_scene(db, provider, scene_stashdb_id, title):
    """Records the release name just sent to the download client (2026-09-26,
    see db.py's _migrate_release_grabbed_title())."""
    with db.connect() as conn:
        conn.execute(
            """UPDATE release SET grabbed_title = ?, grabbed_at = strftime('%Y-%m-%d %H:%M:%S', 'now')
               WHERE provider = ? AND scene_stashdb_id = ?""",
            (title, provider, scene_stashdb_id),
        )


def scene_has_active_download(db, provider, scene_stashdb_id):
    """True while a download for this scene is running or waiting for its
    import (on the wishlist WITH a download job) -- see indexer_search's
    RESULT_ALREADY_DOWNLOADING."""
    with db.connect() as conn:
        row = conn.execute(
            """SELECT 1 FROM release WHERE provider = ? AND scene_stashdb_id = ?
               AND status = 'sent_to_whisparr' AND download_job_id IS NOT NULL LIMIT 1""",
            (provider, scene_stashdb_id)).fetchone()
    return row is not None


def find_imported_equivalent(db, provider, scene_stashdb_id):
    """The same scene already imported via ANOTHER provider (2026-09-28,
    found live: "Bendy Mistress" was imported from ThePornDB on 09-25;
    when StashDB published it on 09-27 it was a brand-new scene id, and
    the per-entity cross-provider dedup couldn't see it -- the TPDB row
    hung on the TPDB performer entry, the new rows on StashDB entries --
    so it was downloaded again). Matched like find_matching_release():
    same release date and same title after normalization, plus the studio
    name ignoring spaces/punctuation ("IntimatePOV" == "Intimate POV")
    when both rows have one. Returns that row, or None."""
    rows = list_rows_for_scene(db, provider, scene_stashdb_id)
    if not rows or not rows[0].get("scene_date"):
        return None
    me = rows[0]
    if not _normalize_title(me["title"]):
        return None
    compact = lambda s: re.sub(r"[^a-z0-9]+", "", (s or "").lower())
    with db.connect() as conn:
        candidates = conn.execute(
            """SELECT * FROM release WHERE provider != ? AND scene_date = ? AND status = 'imported'""",
            (provider, me["scene_date"])).fetchall()
    for c in candidates:
        if _normalize_title(c["title"]) != _normalize_title(me["title"]):
            continue
        if c["studio_name"] and me.get("studio_name") and compact(c["studio_name"]) != compact(me["studio_name"]):
            continue
        return dict(c)
    return None


def set_title_for_scene(db, provider, scene_stashdb_id, title, scene_date=None):
    """The provider renamed/corrected a scene (e.g. a placeholder
    title turned into the real one) -- keep our stored title (and date) current."""
    with db.connect() as conn:
        if scene_date:
            conn.execute("UPDATE release SET title = ?, scene_date = ? WHERE provider = ? AND scene_stashdb_id = ?",
                         (title, scene_date, provider, scene_stashdb_id))
        else:
            conn.execute("UPDATE release SET title = ? WHERE provider = ? AND scene_stashdb_id = ?",
                         (title, provider, scene_stashdb_id))
    _changed(provider, scene_stashdb_id, "title")


def grabbed_titles_of_other_scenes(db, provider, scene_stashdb_id):
    """{grabbed_title: other scene's title} for releases another scene
    currently holds -- downloading/importing, or imported (and not proven
    wrong). Such a release can't also be THIS scene (2026-09-26: one
    release was grabbed for two scenes in the same run). A download that
    failed the fingerprint check for its scene ('import_failed') doesn't
    count: it's the wrong video there, so it may well be this one."""
    with db.connect() as conn:
        rows = conn.execute(
            """SELECT DISTINCT grabbed_title, title FROM release
               WHERE grabbed_title IS NOT NULL
                 AND NOT (provider = ? AND scene_stashdb_id = ?)
                 AND (status = 'imported' OR (status = 'sent_to_whisparr' AND download_job_id IS NOT NULL))""",
            (provider, scene_stashdb_id)).fetchall()
    return {r["grabbed_title"]: r["title"] for r in rows}


def set_search_result_for_scene(db, provider, scene_stashdb_id, result):
    """Records the outcome of an indexer_search.search_and_grab()
    attempt -- "search visibility" (Milestone 3, 2026-09-19, user's own
    request right after the first real grab: no Whisparr-style Activity
    page exists here to see what a search did/didn't find). Scene-scoped
    like set_status_for_scene()/set_download_job_for_scene() above.
    Called on EVERY attempt, not just a successful one, so `result` is
    one of indexer_search.py's `_RESULT_*` constants (e.g.
    'no_candidates', 'title_mismatch_only', 'grabbed')."""
    with db.connect() as conn:
        before = conn.execute("SELECT last_search_result FROM release WHERE provider = ? AND scene_stashdb_id = ? LIMIT 1",
                              (provider, scene_stashdb_id)).fetchone()
        conn.execute(
            """UPDATE release SET last_search_at = datetime('now'), last_search_result = ?
               WHERE provider = ? AND scene_stashdb_id = ?""",
            (result, provider, scene_stashdb_id),
        )
    # Live push (v0.246.14): the Activity tabs (Stuck / Needs review) moved only after a reload, because a changed search result sent no event.
    # Only a CHANGED result is announced: a scan that finds the same "nothing usable" again must not make every open page reload.
    if before is not None and before["last_search_result"] != result:
        _changed(provider, scene_stashdb_id, "search_result")


def mark_targeted_search(db, provider, scene_stashdb_id):
    """Records that this scene was just searched for by its own indexer query (RSS mode back-off)."""
    with db.connect() as conn:
        conn.execute(
            """UPDATE release SET last_targeted_search_at = datetime('now')
               WHERE provider = ? AND scene_stashdb_id = ?""", (provider, scene_stashdb_id))


def find_by_download_job_id(db, download_job_id):
    """One release row carrying this download-client job id, or None
    (2026-09-25, SABnzbd webhook -- to find which client the job ran on)."""
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM release WHERE download_job_id = ? LIMIT 1", (download_job_id,)).fetchone()
    return dict(row) if row else None


def is_known_download_job(db, download_job_id):
    """True if some release row recorded this download-client job id
    (e.g. a SABnzbd nzo_id) -- lets the SABnzbd completion webhook
    (2026-09-25) ignore jobs this sidecar didn't grab itself."""
    with db.connect() as conn:
        row = conn.execute("SELECT 1 FROM release WHERE download_job_id = ? LIMIT 1", (download_job_id,)).fetchone()
    return row is not None


def find_scene_by_download_job(db, download_job_id):
    """(provider, scene_stashdb_id) of the release that recorded this
    download-client job id, or None (2026-09-26, import progress)."""
    with db.connect() as conn:
        row = conn.execute(
            "SELECT provider, scene_stashdb_id FROM release WHERE download_job_id = ? LIMIT 1", (download_job_id,),
        ).fetchone()
    return (row["provider"], row["scene_stashdb_id"]) if row else None


def set_search_details_for_scene(db, provider, scene_stashdb_id, details):
    """Search log (2026-09-25) -- see db.py's
    _migrate_release_search_details(). `details` is a plain dict,
    stored as JSON, scene-scoped like set_search_result_for_scene()."""
    with db.connect() as conn:
        conn.execute(
            "UPDATE release SET last_search_details = ? WHERE provider = ? AND scene_stashdb_id = ?",
            (json.dumps(details), provider, scene_stashdb_id),
        )


def get_search_details_for_scene(db, provider, scene_stashdb_id):
    with db.connect() as conn:
        row = conn.execute(
            """SELECT last_search_details, last_search_at, last_search_result FROM release
               WHERE provider = ? AND scene_stashdb_id = ? AND last_search_details IS NOT NULL LIMIT 1""",
            (provider, scene_stashdb_id),
        ).fetchone()
    if row is None:
        return None
    return {**json.loads(row["last_search_details"]), "searched_at": row["last_search_at"],
            "result": row["last_search_result"]}


def set_review_candidate_for_scene(db, provider, scene_stashdb_id, candidate):
    """Records (or clears, when `candidate` is None) the single
    best "needs review" candidate found for this scene -- Milestone 3's
    RESULT_NEEDS_REVIEW outcome (see indexer_search.py's
    `_select_review_candidate()`): all of the movie's own credited
    performers matched, but not closely enough on date/title to
    auto-grab, so it's surfaced for a human decision instead. `candidate`
    is a plain dict (title/indexer/protocol/downloadUrl/size/age) --
    stored as JSON, same "just a string column" approach as everywhere
    else in this schema. Scene-scoped like set_search_result_for_scene()
    above. Cleared (not left stale) whenever a real grab succeeds or a
    later search no longer finds this same review-worthy candidate --
    see indexer_search.py's search_and_grab()."""
    new = json.dumps(candidate) if candidate else None
    with db.connect() as conn:
        before = conn.execute("SELECT review_candidate FROM release WHERE provider = ? AND scene_stashdb_id = ? LIMIT 1",
                              (provider, scene_stashdb_id)).fetchone()
        conn.execute(
            """UPDATE release SET review_candidate = ?
               WHERE provider = ? AND scene_stashdb_id = ?""",
            (new, provider, scene_stashdb_id),
        )
    if before is not None and before["review_candidate"] != new:
        _changed(provider, scene_stashdb_id, "review_candidate")  # v0.246.14: the Needs review tab shows it without a reload


def get_review_candidate_for_scene(db, provider, scene_stashdb_id):
    """The stored "needs review" candidate dict for this scene (any one
    of its rows -- see set_review_candidate_for_scene()'s docstring),
    or `None` if there isn't one. Scene-scoped read backing the accept/
    reject review-candidate endpoints (routers/releases.py), which only
    have a scene id to work with, same as dismiss()/whisparr_fallback()."""
    with db.connect() as conn:
        row = conn.execute(
            "SELECT review_candidate FROM release WHERE provider = ? AND scene_stashdb_id = ? LIMIT 1",
            (provider, scene_stashdb_id),
        ).fetchone()
    if row is None or not row["review_candidate"]:
        return None
    return json.loads(row["review_candidate"])


def get_review_rejected_titles(db, provider, scene_stashdb_id):
    """The raw candidate titles this scene's review candidate has
    already been explicitly rejected as, across every watched-entity row
    for this scene -- `_select_review_candidate()` skips proposing any
    of these again. `[]` when nothing's ever been rejected (column is
    NULL) or when there's no row at all yet."""
    with db.connect() as conn:
        row = conn.execute(
            "SELECT review_rejected_titles FROM release WHERE provider = ? AND scene_stashdb_id = ? LIMIT 1",
            (provider, scene_stashdb_id),
        ).fetchone()
    if row is None or not row["review_rejected_titles"]:
        return []
    return json.loads(row["review_rejected_titles"])


def remove_review_rejected_title(db, provider, scene_stashdb_id, title):
    """The inverse of add_review_rejected_title(): takes `title` off the scene's blocklist again (every release row of the scene), for the Undo of
    "Not this one" / Reject (v0.236.3). Returns how many rows had it."""
    removed = 0
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT rowid, review_rejected_titles FROM release WHERE provider = ? AND scene_stashdb_id = ?",
            (provider, scene_stashdb_id),
        ).fetchall()
        for row in rows:
            existing = json.loads(row["review_rejected_titles"]) if row["review_rejected_titles"] else []
            if title in existing:
                existing.remove(title)
                conn.execute("UPDATE release SET review_rejected_titles = ? WHERE rowid = ?",
                             (json.dumps(existing) if existing else None, row["rowid"]))
                removed += 1
    return removed


def add_review_rejected_title(db, provider, scene_stashdb_id, title, exact=False):
    """Appends `title` to every release row's own `review_rejected_titles`
    for this scene (scene-scoped, same reasoning as
    set_status_for_scene() -- more than one row can reference the same
    scene) and clears `review_candidate`, so the next search proposes a
    DIFFERENT candidate if one exists instead of immediately re-flagging
    the one the user just said no to. `exact=True` (v0.189.0): only THIS release name is blocked, not its other encodes -- for a download whose
    FILE was unusable (below the quality profile, unreadable); see indexer_search.EXACT_PREFIX."""
    if exact:
        title = f"exact:{title}"
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT rowid, review_rejected_titles FROM release WHERE provider = ? AND scene_stashdb_id = ?",
            (provider, scene_stashdb_id),
        ).fetchall()
        for row in rows:
            existing = json.loads(row["review_rejected_titles"]) if row["review_rejected_titles"] else []
            if title not in existing:
                existing.append(title)
            conn.execute(
                "UPDATE release SET review_rejected_titles = ?, review_candidate = NULL WHERE rowid = ?",
                (json.dumps(existing), row["rowid"]),
            )


def _dedupe_by_scene(rows):
    """One row per scene, keeping the first -- `release` has one row
    per (provider, scene, entity) triple, so a scene favorited via more
    than one watched entity produces duplicates here. Same dedup
    reasoning as download_sources.SabnzbdSource's own (now-removed)
    dedup step -- see PROGRESS.md's Milestone 2 cutover investigation
    for the real data-loss bug this pattern exists to prevent."""
    seen = set()
    result = []
    for r in rows:
        row = dict(r)
        if row["scene_stashdb_id"] in seen:
            continue
        seen.add(row["scene_stashdb_id"])
        result.append(row)
    return result


def list_sent_to_whisparr_without_grab(db):
    """`sent_to_whisparr` releases with no download job recorded yet --
    what the periodic indexer re-scan loop (main.py's run_indexer_search())
    retries. Deduped by scene."""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM release WHERE status = 'sent_to_whisparr' AND download_job_id IS NULL",
        ).fetchall()
    return _dedupe_by_scene(rows)


def list_grabbed_awaiting_import(db):
    """`sent_to_whisparr` releases that DO have a recorded download job
    -- what download_sources.SabnzbdSource polls to see if that job has
    finished. Deduped by scene.

    Excludes releases whose file was already moved (`pending_import_path`
    set, 2026-09-25): since import_one() stopped waiting for Stash's scan
    (v0.61.0), a moved-but-not-yet-verified release keeps this status
    until the Scene.Create.Post hook / retry_pending_verifications()
    finish it -- without this filter the next import run would try to
    import it AGAIN, find the download folder gone and flag it
    'source_missing'. fix_wrong_download() clears pending_import_path
    when it sends a release back to searching, so a replacement
    download is picked up normally."""
    with db.connect() as conn:
        rows = conn.execute(
            """SELECT * FROM release WHERE status = 'sent_to_whisparr' AND download_job_id IS NOT NULL
               AND pending_import_path IS NULL""",
        ).fetchall()
    return _dedupe_by_scene(rows)


def set_pending_import_path_for_scene(db, provider, scene_stashdb_id, path):
    """Records (or clears, when `path` is None) the exact local library
    path file_importer.py's import_one() already moved this scene's
    file to, before its own scan+verify finishes -- backs
    file_importer.retry_pending_verifications(), added 2026-09-19
    (v0.41.12) so a release stuck in Stash's own "moved but not yet
    fingerprinted" limbo can be found and retried later without needing
    the original (by-then-deleted) download-folder source item again.
    Scene-scoped like set_search_result_for_scene() above."""
    with db.connect() as conn:
        conn.execute(
            "UPDATE release SET pending_import_path = ? WHERE provider = ? AND scene_stashdb_id = ?",
            (path, provider, scene_stashdb_id),
        )
    _changed(provider, scene_stashdb_id, "pending_import")

def list_unverified_imports(db):
    """'imported' releases whose provider had no fingerprints yet at
    import time (import_error_code 'unverified', 2026-09-25) --
    re-checked by file_importer.recheck_unverified_imports() until the
    provider has fingerprints. pending_import_path stays set on these
    for exactly that. Deduped by scene."""
    with db.connect() as conn:
        rows = conn.execute(
            """SELECT * FROM release WHERE status = 'imported' AND import_error_code = 'unverified'
               AND pending_import_path IS NOT NULL""",
        ).fetchall()
    return _dedupe_by_scene(rows)


def list_pending_sidecar_verifications(db):
    """`sent_to_whisparr` releases whose file this pipeline already
    moved into place but couldn't yet confirm (see
    set_pending_import_path_for_scene()) -- what
    file_importer.retry_pending_verifications() retries. Deduped by
    scene. Empty for anyone running Whisparr's own native Completed
    Download Handling instead (`file_import_source = "whisparr_built_in"`)
    -- that path never sets `pending_import_path` at all, since this
    sidecar's own import_one() never runs for it."""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM release WHERE status = 'sent_to_whisparr' AND pending_import_path IS NOT NULL",
        ).fetchall()
    return _dedupe_by_scene(rows)


def reclassify_late_additions(db, days, dry_run=True):
    """One-off (2026-09-26): moves already-stored 'new' releases that are
    back-catalogue additions (release date more than `days` before the
    scene was added to its provider -- same rule as
    watcher._is_late_addition()) to 'backlog' with late_addition = 1.
    Only 'new' rows: anything already on the wishlist, imported or
    dismissed stays as it is. Returns {"count", "sample"}."""
    where = ("status = 'new' AND scene_date IS NOT NULL AND scene_date != '' "
             "AND julianday(substr(scene_created, 1, 10)) - julianday(scene_date) > ?")
    with db.connect() as conn:
        count = conn.execute(f"SELECT COUNT(*) FROM release WHERE {where}", (days,)).fetchone()[0]
        sample = [dict(r) for r in conn.execute(
            f"SELECT title, scene_date, scene_created, studio_name FROM release WHERE {where} "
            "ORDER BY scene_created DESC LIMIT 10", (days,))]
        if not dry_run and count:
            conn.execute(f"UPDATE release SET status = 'backlog', late_addition = 1 WHERE {where}", (days,))
    return {"count": count, "sample": sample, "dry_run": dry_run}


def open_releases_outside_window(db, entity_pairs, window_from, window_until, dismiss=False, rows_out=None):
    """Open ('new'/'backlog') releases of the given (provider, entity_type,
    entity_stashdb_id) rows whose release date lies outside the window
    (2026-09-26) -- counted, or dismissed with `dismiss`. Releases
    without a date are never touched (same rule as
    watchlist_repo.in_window())."""
    if not entity_pairs or not (window_from or window_until):
        return 0
    conds, params = [], []
    if window_from:
        conds.append("substr(scene_date, 1, 10) < ?")
        params.append(window_from)
    if window_until:
        conds.append("substr(scene_date, 1, 10) > ?")
        params.append(window_until)
    ent = " OR ".join("(provider = ? AND entity_type = ? AND entity_stashdb_id = ?)" for _ in entity_pairs)
    for pair in entity_pairs:
        params += list(pair)
    where = (f"status IN ('new', 'backlog') AND scene_date IS NOT NULL AND scene_date != '' "
             f"AND ({' OR '.join(conds)}) AND ({ent})")
    with db.connect() as conn:
        if dismiss:
            if rows_out is not None:
                rows_out.extend(_snapshot(conn, where, params))
            return conn.execute(f"UPDATE release SET status = 'dismissed' WHERE {where}", params).rowcount
        return conn.execute(f"SELECT COUNT(*) FROM release WHERE {where}", params).fetchone()[0]


def adopt_imported_status(db, provider, scene_stashdb_id):
    """Another row of this scene (another watched entity) is already 'imported': rows of the same scene that are still 'new'/'backlog'
    become 'imported' too (same imported_at / grabbed fields), so the scene does not show up as new under its other entity (v0.130.0).
    Returns how many rows changed."""
    with db.connect() as conn:
        done = conn.execute("SELECT imported_at, grabbed_title, grabbed_at FROM release WHERE provider = ? AND scene_stashdb_id = ? "
                            "AND status = 'imported' LIMIT 1", (provider, scene_stashdb_id)).fetchone()
        if not done:
            return 0
        cur = conn.execute("UPDATE release SET status = 'imported', imported_at = ?, grabbed_title = COALESCE(grabbed_title, ?), "
                           "grabbed_at = COALESCE(grabbed_at, ?) WHERE provider = ? AND scene_stashdb_id = ? AND status IN ('new', 'backlog')",
                           (done["imported_at"], done["grabbed_title"], done["grabbed_at"], provider, scene_stashdb_id))
        changed = cur.rowcount
    if changed:
        _changed(provider, scene_stashdb_id, "status")
    return changed


_WISHLIST_STATE_ORDER = ("imported", "sent_to_whisparr", "import_failed", "dismissed")


def wishlist_states(db, provider, scene_ids, include_open=False):
    """{scene id: 'imported' | 'sent_to_whisparr' | 'import_failed' | 'dismissed'} for the scenes of `scene_ids` this program already knows,
    one query (v0.206.0, Search page: a scene that is on the wishlist is shown as such). A scene with several rows (one per watched studio /
    performer) gets the most advanced state; scenes that are only 'new' / 'backlog' / 'late' are left out, unless `include_open`
    (the userscript, v0.233.2): then they come back as 'known' (found by a scan, not sent to the wishlist yet)."""
    ids = [str(i) for i in scene_ids if i]
    out = {}
    if not ids:
        return out
    with db.connect() as conn:
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            marks = ",".join("?" for _ in chunk)
            for r in conn.execute(f"SELECT scene_stashdb_id, status FROM release WHERE provider = ? AND scene_stashdb_id IN ({marks})",
                                  [provider] + chunk):
                status = r["status"]
                if include_open and status in ("new", "backlog", "late"):
                    status = "known"
                if status in _WISHLIST_STATE_ORDER or status == "known":
                    cur = out.get(r["scene_stashdb_id"])
                    rank = _WISHLIST_STATE_ORDER + ("known",)
                    if cur is None or rank.index(status) < rank.index(cur):
                        out[r["scene_stashdb_id"]] = status
    return out


def list_rows_for_scene(db, provider, scene_stashdb_id):
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM release WHERE provider = ? AND scene_stashdb_id = ?", (provider, scene_stashdb_id))]


# ---------- hidden studios (v0.91.0) ----------

def list_muted_studios(db):
    with db.connect() as conn:
        rows = conn.execute("""SELECT m.*, (SELECT COUNT(*) FROM release r WHERE r.muted = 1 AND
                                           (r.studio_id = m.studio_id OR (r.studio_id IS NULL AND r.studio_name = m.name))) AS hidden
                               FROM muted_studio m ORDER BY lower(m.name)""").fetchall()
    return [dict(r) for r in rows]


def mute_studio(db, studio_id, name, provider="stashdb", parent_id=None):
    """Hides a studio: its existing 'new'/'backlog' releases that came in through the STUDIO (not upgrade rows, not performer hits)
    become 'dismissed' + muted (old status remembered); new ones are stored that way from now on. Releases of a scene that was
    stored before studio ids were recorded are found by studio name. Returns the number of releases hidden now."""
    with db.connect() as conn:
        conn.execute("INSERT OR REPLACE INTO muted_studio (studio_id, provider, name, parent_id) VALUES (?, ?, ?, ?)",
                     (studio_id, provider, name or "", parent_id))
        conn.execute("DELETE FROM studio_dismiss_streak WHERE studio_id = ?", (studio_id,))
        cur = conn.execute(
            """UPDATE release SET muted_prev_status = status, status = 'dismissed', muted = 1, studio_id = ?
               WHERE entity_type = 'studio' AND entity_stashdb_id NOT LIKE 'upgrade:%' AND entity_stashdb_id NOT LIKE 'film:%' AND muted = 0
                 AND status IN ('new', 'backlog')
                 AND (studio_id = ? OR (studio_id IS NULL AND ? != '' AND studio_name = ?))""",
            (studio_id, studio_id, name or "", name or ""))
        return cur.rowcount


def unmute_studio(db, studio_id):
    """Shows a studio again: releases hidden for it get their old status back (only if still 'dismissed' -- one the user dismissed
    by hand afterwards stays dismissed). Returns the number restored."""
    with db.connect() as conn:
        cur = conn.execute(
            """UPDATE release SET status = COALESCE(muted_prev_status, 'new'), muted = 0, muted_prev_status = NULL
               WHERE muted = 1 AND studio_id = ? AND status = 'dismissed'""", (studio_id,))
        restored = cur.rowcount
        conn.execute("UPDATE release SET muted = 0, muted_prev_status = NULL WHERE muted = 1 AND studio_id = ?", (studio_id,))
        conn.execute("DELETE FROM muted_studio WHERE studio_id = ?", (studio_id,))
        return restored


def studio_of_scene(db, provider, scene_stashdb_id):
    """(studio_id, studio_name) a stored release row knows for this scene, or (None, name)."""
    with db.connect() as conn:
        r = conn.execute("SELECT studio_id, studio_name FROM release WHERE provider = ? AND scene_stashdb_id = ? "
                         "ORDER BY studio_id IS NULL LIMIT 1", (provider, scene_stashdb_id)).fetchone()
    return (r["studio_id"], r["studio_name"]) if r else (None, None)


def get_scene_urls(db, provider, scene_stashdb_id):
    """[{url, site}] stored for this scene, or None when they were never fetched."""
    with db.connect() as conn:
        r = conn.execute("SELECT scene_urls FROM release WHERE provider = ? AND scene_stashdb_id = ? AND scene_urls IS NOT NULL LIMIT 1",
                         (provider, scene_stashdb_id)).fetchone()
    return json.loads(r["scene_urls"]) if r else None


def set_scene_urls_for_scene(db, provider, scene_stashdb_id, urls):
    with db.connect() as conn:
        conn.execute("UPDATE release SET scene_urls = ? WHERE provider = ? AND scene_stashdb_id = ?",
                     (json.dumps(urls or []), provider, scene_stashdb_id))


# ---------- tags backfill (v0.92.0) ----------

def list_scenes_missing_tags(db, provider, statuses=("new",), limit=None, images=False):
    """Scene ids of releases whose tags were never fetched, newest scene day first (what the Calendar shows first). `images=True`
    (ThePornDB, v0.93.1) also returns scenes whose stored thumbnail still points at the source site's own CDN instead of ThePornDB's
    mirror -- the same single request per scene fixes both."""
    placeholders = ",".join("?" * len(statuses))
    cond = "tags IS NULL"
    if images:
        cond = ("(tags IS NULL OR (image_url IS NOT NULL AND image_url != '' AND image_url NOT LIKE '%theporndb.net%'))")
    sql = f"""SELECT scene_stashdb_id, MAX({_DAY_EXPR}) AS day FROM release
              WHERE provider = ? AND {cond} AND status IN ({placeholders})
              GROUP BY scene_stashdb_id ORDER BY day DESC"""
    params = [provider, *statuses]
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    with db.connect() as conn:
        return [r["scene_stashdb_id"] for r in conn.execute(sql, params).fetchall()]


def set_tags_for_scene(db, provider, scene_id, tags, studio_id=None, image_url=None, drop_image=False):
    """Stores the tags on every release row of the scene; the studio id too where it was never recorded. `image_url` replaces a
    thumbnail that is still set (a cleared one -- scene already in the library -- stays cleared); `drop_image` clears it (the provider
    no longer knows the scene)."""
    with db.connect() as conn:
        conn.execute("UPDATE release SET tags = ? WHERE provider = ? AND scene_stashdb_id = ?",
                     (tags_text(tags) or "", provider, scene_id))
        if studio_id:
            conn.execute("UPDATE release SET studio_id = ? WHERE provider = ? AND scene_stashdb_id = ? AND studio_id IS NULL",
                         (studio_id, provider, scene_id))
        if image_url:
            conn.execute("UPDATE release SET image_url = ? WHERE provider = ? AND scene_stashdb_id = ? "
                         "AND image_url IS NOT NULL AND image_url != ''", (image_url, provider, scene_id))
        elif drop_image:
            conn.execute("UPDATE release SET image_url = NULL WHERE provider = ? AND scene_stashdb_id = ?", (provider, scene_id))


def list_scenes_missing_pairing(db, provider, entity_type, entity_id, statuses, limit=None):
    """Scene ids of this entity's releases whose cast was never read (cast_pairing IS NULL), newest first."""
    marks = ",".join("?" * len(statuses))
    sql = f"""SELECT scene_stashdb_id, MAX({_DAY_EXPR}) AS day FROM release
              WHERE provider = ? AND entity_type = ? AND entity_stashdb_id = ? AND (cast_pairing IS NULL OR cast_names IS NULL) AND status IN ({marks})
              GROUP BY scene_stashdb_id ORDER BY day DESC"""
    params = [provider, entity_type, entity_id, *statuses]
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    with db.connect() as conn:
        return [r["scene_stashdb_id"] for r in conn.execute(sql, params).fetchall()]


def set_pairing_for_scene(db, provider, scene_id, code, names=""):
    """Stores the pairing and the performers' names on every row of the scene ('' = StashDB lists no cast or no longer knows the scene:
    not asked again)."""
    with db.connect() as conn:
        conn.execute("UPDATE release SET cast_pairing = ?, cast_names = ? WHERE provider = ? AND scene_stashdb_id = ?",
                     (code or "", names or "", provider, scene_id))


def studio_release_counts(db):
    """{studio_id: n} and {studio name: n} of releases that came in through a studio and are 'new'/'backlog' or hidden -- what a
    sub-studio checklist shows next to each entry (rows from before studio ids were recorded are counted by name)."""
    by_id, by_name = {}, {}
    with db.connect() as conn:
        for r in conn.execute("""SELECT studio_id, studio_name, COUNT(DISTINCT scene_stashdb_id) AS n FROM release
                                 WHERE entity_type = 'studio' AND entity_stashdb_id NOT LIKE 'upgrade:%' AND entity_stashdb_id NOT LIKE 'film:%'
                                   AND (status IN ('new', 'backlog') OR muted = 1)
                                 GROUP BY studio_id, studio_name""").fetchall():
            if r["studio_id"]:
                by_id[r["studio_id"]] = by_id.get(r["studio_id"], 0) + r["n"]
            else:
                by_name[r["studio_name"]] = by_name.get(r["studio_name"], 0) + r["n"]
    return by_id, by_name
