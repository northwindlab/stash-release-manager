"""StashDB GraphQL client for the "what's new" watermark query.

Ported from an earlier standalone script, generalized from
studio-only to studio-or-performer (see PROGRESS.md's 2026-09-11 scope
decision to cover Performers symmetrically with Studios). The query
shape and the "sort by CREATED_AT, not DATE" reasoning are unchanged --
see that file's module docstring for the full rationale, verified live
against StashDB's schema on 2026-09-10.
"""
import time

import requests

from http_deadline import call_with_deadline

STASHDB_URL = "https://stashdb.org/graphql"

QUERY_SCENES = """
query($input: SceneQueryInput!) {
  queryScenes(input: $input) {
    count
    scenes { id title date created details images { url } studio { id name } tags { name } performers { performer { name gender } } }
  }
}
"""

# Global "recently created" feed (2026-09-25, "Neuheiten-Feed", see
# fetch_recent_scenes()) -- same fields as QUERY_SCENES plus the
# performer ids, needed to match a scene to watched performers locally.
QUERY_SCENES_FEED = """
query($input: SceneQueryInput!) {
  queryScenes(input: $input) {
    count
    scenes { id title date created details images { url } studio { id name parent { id parent { id } } } tags { name } performers { performer { id name gender } } }
  }
}
"""


class FeedIncomplete(Exception):
    """The feed ran out of results before reaching `since_created` --
    the caller can't be sure nothing was missed and must fall back to
    per-entry scanning for this provider (see watcher.run_feed_scan())."""


# The one and only VR tag StashDB uses -- confirmed live 2026-09-12 by
# checking several scenes from a known VR studio (all 5/5 carried
# exactly this tag, no variants); Scene has no dedicated VR field of
# its own (checked via GraphQL introspection), just this tag. Its own
# StashDB id (looked up live via queryTags 2026-09-13, backs
# is_vr_dominant() below -- a queryScenes tag filter needs the id, not
# the name).
VR_TAG_NAME = "Virtual Reality"
VR_TAG_ID = "6458e5cf-4f65-400b-9067-582141e2a329"

FIND_SCENE_IMAGE_QUERY = """
query($id: ID!) { findScene(id: $id) { id images { url } } }
"""

FIND_SCENE_FINGERPRINTS_QUERY = """
query($id: ID!) { findScene(id: $id) { id fingerprints { hash algorithm } } }
"""

# Backs file_naming.py's naming tokens (Milestone 2, 2026-09-17) --
# QUERY_SCENES (used for watchlist scanning) only ever fetches
# `performers { id }`, not name/gender, so this is a genuinely separate
# query. `performer { name gender }` confirmed live via schema
# introspection (Scene.performers -> [PerformerAppearance!]! ->
# PerformerAppearance.performer -> Performer.gender, e.g. "FEMALE").
FIND_SCENE_DETAIL_QUERY = """
query($id: ID!) {
  findScene(id: $id) {
    id
    title
    details
    date
    studio { id name }
    performers { as performer { name gender aliases } }
  }
}
"""

# The links of one scene with their site type ("Studio" = the studio's own page for the scene, StashDB's convention). Backs the indexer search:
# a scene whose StashDB studio is a performer-named creator ("Some Creator") is released under the SITE's name ("ExampleSite.Jane.Roe...").
FIND_SCENE_URLS_QUERY = """
query($id: ID!) { findScene(id: $id) { id urls { url site { name } } } }
"""

FIND_SCENES_BY_FINGERPRINTS_QUERY = """
query($fps: [[FingerprintQueryInput!]!]!) {
  findScenesBySceneFingerprints(fingerprints: $fps) { id title }
}
"""

# Confirmed live 2026-09-15: 40 fingerprint-sets per
# find_scenes_by_fingerprints() call succeeds, 45+ fails with StashDB's
# own "too many scenes" error -- no documented limit found, just
# measured directly.
MAX_FINGERPRINT_BATCH = 40

QUERY_FAVORITE_PERFORMERS = """
query($input: PerformerQueryInput!) {
  queryPerformers(input: $input) {
    count
    performers { id }
  }
}
"""

QUERY_FAVORITE_STUDIOS = """
query($input: StudioQueryInput!) {
  queryStudios(input: $input) {
    count
    studios { id }
  }
}
"""

# StashDB's SceneQueryInput filter field name per entity type.
_FILTER_FIELD = {"studio": "studios", "performer": "performers"}


class StashDBClient:
    def __init__(self, api_key, url=STASHDB_URL, page_size=50, timeout=30, delay=0.3):
        self.url = url
        self.session = requests.Session()
        self.session.headers["ApiKey"] = api_key
        self.page_size = page_size
        self.timeout = timeout
        # Between-page delay, not just between-entity -- see
        # PROGRESS.md's "Missing inter-page delay" section: a single
        # entity (or the favorites list itself) can span dozens of
        # pages, and without a delay *within* that pagination loop, the
        # per-entity `delay` in watcher.py's outer loop (or the one-off
        # call site for the favorites list) never applies to it -- a
        # burst of unthrottled requests against StashDB for exactly the
        # large-catalog entities most likely to need many pages.
        self.delay = delay

    def _post(self, json):
        """All StashDB requests go through here, wrapped with a hard
        wall-clock deadline (self.timeout + a buffer) independent of
        `requests`' own timeout= -- see http_deadline.py's docstring for
        why that alone isn't a reliable cap (confirmed live 2026-09-12
        as the likely cause of a scan thread hanging indefinitely, see
        PROGRESS.md's "Scan appeared to hang permanently" section)."""
        return call_with_deadline(
            self.session.post, self.url, json=json, timeout=self.timeout,
            deadline=self.timeout + 15,
        )

    def _query_favorite_ids(self, query, result_key, item_key, per_page=100):
        """Paginates a queryPerformers/queryStudios(is_favorite: true)
        call and returns the full set of StashDB ids -- these are
        favorited on the user's StashDB.org account itself
        (PerformerQueryInput/StudioQueryInput's `is_favorite`), a
        separate thing from this Stash instance's own `favorite` flag.
        See PROGRESS.md's "StashDB.org favorites import" section."""
        ids = []
        page = 1
        while True:
            variables = {"input": {"is_favorite": True, "page": page, "per_page": per_page}}
            resp = self._post({"query": query, "variables": variables})
            resp.raise_for_status()
            data = resp.json()
            if data.get("errors"):
                raise RuntimeError(str(data["errors"]))
            items = data["data"][result_key][item_key]
            if not items:
                return ids
            ids.extend(item["id"] for item in items)
            page += 1
            time.sleep(self.delay)

    def ping(self):
        """Cheapest possible authenticated call against this endpoint --
        a single page=1/per_page=1 queryScenes with no entity filter --
        used only to verify a URL/API key pair actually works (see the
        Releases page's "Check connections" button). Reused as-is for
        ThePornDB, which speaks the same stash-box GraphQL protocol as
        StashDB. Raises on any HTTP or GraphQL error; the caller turns
        that into a per-provider ok/error status."""
        variables = {"input": {"page": 1, "per_page": 1}}
        resp = self._post({"query": QUERY_SCENES, "variables": variables})
        resp.raise_for_status()
        data = resp.json()
        if data.get("errors"):
            raise RuntimeError(str(data["errors"]))

    def favorited_performer_ids(self):
        return self._query_favorite_ids(QUERY_FAVORITE_PERFORMERS, "queryPerformers", "performers")

    def favorited_studio_ids(self):
        return self._query_favorite_ids(QUERY_FAVORITE_STUDIOS, "queryStudios", "studios")

    def fetch_newest_scene_created(self, entity_type, stashdb_id):
        """Single request, page 1 / per_page 1 -- just the `created`
        timestamp of this entity's single newest scene (None if it has
        none), for establishing a first-run baseline. Deliberately NOT
        `list(fetch_new_scenes(..., since_created=None))[0]`: that drains
        the *entire* catalog via full pagination just to look at
        `scenes[0]`, since the baseline branch never has a watermark to
        stop early against -- fine for a modest catalog, but for a
        prolific studio/performer (thousands of scenes) it means
        hundreds of throwaway pages. Confirmed live 2026-09-11: a scan
        against the newly favorite-imported watchlist (883 performers,
        370 studios, many via StashDB.org's real account favorites, not
        the earlier hand-picked local set) stalled for many minutes
        establishing baselines this way before this fix."""
        newest = None
        for entity_filter in self._entity_filters(entity_type, stashdb_id):
            variables = {"input": {**entity_filter, "page": 1, "per_page": 1,
                                   "sort": "CREATED_AT", "direction": "DESC"}}
            resp = self._post({"query": QUERY_SCENES, "variables": variables})
            resp.raise_for_status()
            data = resp.json()
            if data.get("errors"):
                raise RuntimeError(str(data["errors"]))
            scenes = data["data"]["queryScenes"]["scenes"]
            if scenes and (newest is None or scenes[0]["created"] > newest):
                newest = scenes[0]["created"]
        return newest

    @staticmethod
    def _entity_filters(entity_type, stashdb_id):
        """SceneQueryInput filter(s) for one watched entity. A studio also
        gets `parentStudio` (2026-09-26): network studios (Brazzers,
        BangBros, ...) have their scenes on child studios on StashDB --
        watching the network means watching those too, as in the *arr
        apps. (Direct children only here; the feed scan also covers
        grandchildren.)"""
        filters = [{_FILTER_FIELD[entity_type]: {"value": [stashdb_id], "modifier": "INCLUDES"}}]
        if entity_type == "studio":
            filters.append({"parentStudio": stashdb_id})
        return filters

    def fetch_new_scenes(self, entity_type, stashdb_id, since_created):
        """Yields scenes for this studio/performer newer than
        since_created (None = no baseline yet -- caller should treat
        that as "first run" and not report anything), newest first.
        Stops paginating as soon as it reaches a scene at/before the
        watermark, since results are sorted CREATED_AT DESC. For a
        studio, its own scenes and its child studios' scenes are merged
        (see _entity_filters())."""
        filters = self._entity_filters(entity_type, stashdb_id)
        if len(filters) == 1:
            yield from self._fetch_new_scenes_for(filters[0], since_created)
            return
        merged = {}
        for entity_filter in filters:
            for s in self._fetch_new_scenes_for(entity_filter, since_created):
                merged.setdefault(s["id"], s)
        yield from sorted(merged.values(), key=lambda s: s["created"], reverse=True)

    def _fetch_new_scenes_for(self, entity_filter, since_created):
        page = 1
        while True:
            variables = {
                "input": {
                    **entity_filter,
                    "page": page, "per_page": self.page_size,
                    "sort": "CREATED_AT", "direction": "DESC",
                }
            }
            resp = self._post({"query": QUERY_SCENES, "variables": variables})
            resp.raise_for_status()
            data = resp.json()
            if data.get("errors"):
                raise RuntimeError(str(data["errors"]))
            scenes = data["data"]["queryScenes"]["scenes"]
            if not scenes:
                return
            for s in scenes:
                if since_created is not None and s["created"] <= since_created:
                    return
                # Flattened here rather than left as the raw `images`
                # array -- theporndb_client.py's equivalent scene dict
                # already carries a single top-level `image_url`
                # (ThePornDB's REST API returns one directly, no array),
                # so this keeps the two providers' normalized scene
                # shape consistent for insert_release() and the plugin's
                # own Tagger-style thumbnail (2026-09-12).
                s["image_url"] = (s.get("images") or [{}])[0].get("url")
                s["is_vr"] = any(t.get("name") == VR_TAG_NAME for t in (s.get("tags") or []))
                yield s
            page += 1
            time.sleep(self.delay)

    def fetch_recent_scenes(self, since_created, per_page=100, max_pages=300):
        """Every scene created after `since_created` on this stash-box,
        newest first, regardless of studio/performer (2026-09-25, the
        "Neuheiten-Feed": ~480 new StashDB scenes/day measured live, so a
        6-hour scan needs ~2 pages instead of one request per watchlist
        entry). Same scene dict as fetch_new_scenes() plus
        `_studio_ids`/`_performer_ids` (sets of stash-box ids) for local
        matching. Raises FeedIncomplete if the results (or `max_pages`)
        run out before a scene at/before `since_created` shows up."""
        page = 1
        while page <= max_pages:
            variables = {"input": {"page": page, "per_page": per_page, "sort": "CREATED_AT", "direction": "DESC"}}
            resp = self._post({"query": QUERY_SCENES_FEED, "variables": variables})
            resp.raise_for_status()
            data = resp.json()
            if data.get("errors"):
                raise RuntimeError(str(data["errors"]))
            scenes = data["data"]["queryScenes"]["scenes"]
            if not scenes:
                break
            for s in scenes:
                if s["created"] <= since_created:
                    return
                s["image_url"] = (s.get("images") or [{}])[0].get("url")
                s["is_vr"] = any(t.get("name") == VR_TAG_NAME for t in (s.get("tags") or []))
                # Parent + grandparent too (2026-09-26): a watched network
                # studio (Brazzers, ...) matches its child studios' scenes.
                s["_studio_ids"] = set()
                studio = s.get("studio")
                while studio:
                    s["_studio_ids"].add(studio["id"])
                    studio = studio.get("parent")
                s["_performer_ids"] = {p["performer"]["id"] for p in (s.get("performers") or [])
                                       if (p.get("performer") or {}).get("id")}
                yield s
            page += 1
            time.sleep(self.delay)
        raise FeedIncomplete(f"feed didn't reach {since_created} within {page - 1} page(s)")

    def fetch_activity(self, entity_type, stashdb_ids, batch_size=20):
        """{id: {newest_scene_date, scene_count, career_end_year,
        death_date, deleted}} for the "probably inactive" check
        (2026-09-26, inactive_check.py). Batched with GraphQL aliases --
        `batch_size` entities per request (a findPerformer/findStudio
        plus a 1-scene queryScenes sorted by release DATE each), so the
        whole watchlist costs ~50 requests instead of ~2500. Studios have
        no career fields on StashDB, only `deleted`. An id StashDB no
        longer knows at all comes back with deleted=True."""
        filter_field = _FILTER_FIELD[entity_type]
        find = "findStudio" if entity_type == "studio" else "findPerformer"
        fields = "deleted" if entity_type == "studio" else "deleted career_end_year death_date"
        out = {}
        for i in range(0, len(stashdb_ids), batch_size):
            batch = stashdb_ids[i:i + batch_size]
            parts, variables, decls = [], {}, []
            for n, sid in enumerate(batch):
                decls.append(f"$id{n}: ID!")
                variables[f"id{n}"] = sid
                parts.append(f"e{n}: {find}(id: $id{n}) {{ {fields} }}")
                parts.append(
                    f's{n}: queryScenes(input: {{{filter_field}: {{value: [$id{n}], modifier: INCLUDES}}, '
                    f'page: 1, per_page: 1, sort: DATE, direction: DESC}}) {{ count scenes {{ date }} }}')
                if entity_type == "studio":
                    # Network/parent studios (Brazzers, BangBros, ...) have
                    # (almost) no scenes of their own on StashDB -- they sit
                    # on the child studios. `parentStudio` covers those
                    # (found live 2026-09-26: Brazzers 0 vs 14678).
                    parts.append(
                        f'c{n}: queryScenes(input: {{parentStudio: $pid{n}, page: 1, per_page: 1, '
                        f'sort: DATE, direction: DESC}}) {{ count scenes {{ date }} }}')
                    decls.append(f"$pid{n}: String")
                    variables[f"pid{n}"] = sid
            query = f"query({', '.join(decls)}) {{ {' '.join(parts)} }}"
            resp = self._post({"query": query, "variables": variables})
            resp.raise_for_status()
            data = resp.json()
            if data.get("errors") and not data.get("data"):
                raise RuntimeError(str(data["errors"]))
            # Aliases that errored are skipped entirely (left out of the
            # result) -- a null there means "unknown", not "deleted".
            failed = {str((err.get("path") or [""])[0]) for err in data.get("errors") or []}
            data = data.get("data") or {}
            for n, sid in enumerate(batch):
                if failed & {f"e{n}", f"s{n}", f"c{n}"}:
                    continue
                entity = data.get(f"e{n}")
                dates, count = [], 0
                for key in (f"s{n}", f"c{n}"):
                    scenes = data.get(key) or {}
                    count += scenes.get("count") or 0
                    if scenes.get("scenes") and scenes["scenes"][0].get("date"):
                        dates.append(scenes["scenes"][0]["date"])
                out[sid] = {
                    "newest_scene_date": max(dates) if dates else None,
                    "scene_count": count,
                    "career_end_year": (entity or {}).get("career_end_year"),
                    "death_date": (entity or {}).get("death_date"),
                    "deleted": entity is None or bool(entity.get("deleted")),
                }
            if i + batch_size < len(stashdb_ids):
                time.sleep(self.delay)
        return out

    def is_vr_dominant(self, entity_type, stashdb_id):
        """True if more than half of this entity's StashDB catalog
        carries the "Virtual Reality" tag -- 2026-09-13, backs the
        "Missing from Catalog" completeness check's choice of which
        Whisparr quality profile (normal vs VR) applies to the whole
        entity, without needing to know each individual scene's VR
        status (which would mean fetching the full catalog, not just
        counts).

        One additional cheap count-only query beyond the entity's
        already-known total. Verified live against real studios: a
        known VR studio (18VR) was 100% VR-tagged (482/482), a regular
        studio (Academy POV) was 0% (0/160) -- studios are never
        genuinely mixed in practice. A performer *can* be, so this is
        a >50% majority call for that case, not a guarantee -- see
        PROGRESS.md's "Missing from Catalog" completeness section for
        the user's explicit tradeoff call on this."""
        def count(tag_filter=None):
            # Summed over the studio's own + child studios' scenes
            # (2026-09-26, see _entity_filters()).
            total = 0
            for entity_filter in self._entity_filters(entity_type, stashdb_id):
                variables = {
                    "input": {
                        **entity_filter,
                        "page": 1, "per_page": 1,
                        "sort": "DATE", "direction": "ASC",
                    }
                }
                if tag_filter:
                    variables["input"]["tags"] = tag_filter
                resp = self._post({"query": QUERY_SCENES, "variables": variables})
                resp.raise_for_status()
                data = resp.json()
                if data.get("errors"):
                    raise RuntimeError(str(data["errors"]))
                total += data["data"]["queryScenes"]["count"]
            return total

        total = count()
        if total == 0:
            return False
        vr_count = count({"value": [VR_TAG_ID], "modifier": "INCLUDES"})
        return vr_count > total / 2

    def count_scenes_by_year(self, entity_type, stashdb_id):
        """Cheap per-year scene counts for this entity's *entire*
        catalog -- used to preview a backlog before actually fetching
        it (2026-09-13, see PROGRESS.md's "On-demand backlog browsing"
        section). Only ever requests `count` with `per_page: 1`, never
        actual scene rows, so this stays cheap regardless of how large
        the catalog is. Returns {year: count}, empty if the entity has
        no scenes at all.

        Verified live against StashDB 2026-09-13: `count` reflects the
        full filtered match total regardless of `per_page` (already
        exploited by fetch_newest_scene_created() above), and
        `CriterionModifier.GREATER_THAN`/`LESS_THAN` on the `date`
        field are plain strict inequalities (not inclusive) -- e.g.
        `date > "2019-12-31"` counted exactly the scenes dated 2020 or
        later. `date` only takes one bound at a time (no BETWEEN
        modifier in StashDB's schema), so per-year counts are derived
        from a running cumulative "count of scenes in year Y or later"
        rather than one query per year directly.
        """
        totals = {}
        for entity_filter in self._entity_filters(entity_type, stashdb_id):
            for year, n in self._count_scenes_by_year_for(entity_filter).items():
                totals[year] = totals.get(year, 0) + n
        return dict(sorted(totals.items()))

    def _count_scenes_by_year_for(self, entity_filter):
        """count_scenes_by_year() for one SceneQueryInput filter (a
        studio's own scenes, or its child studios' -- disjoint sets, so
        the caller can simply add them up)."""
        def count(date_filter=None):
            variables = {
                "input": {
                    **entity_filter,
                    "page": 1, "per_page": 1,
                    "sort": "DATE", "direction": "ASC",
                }
            }
            if date_filter:
                variables["input"]["date"] = date_filter
            resp = self._post({"query": QUERY_SCENES, "variables": variables})
            resp.raise_for_status()
            data = resp.json()
            if data.get("errors"):
                raise RuntimeError(str(data["errors"]))
            return data["data"]["queryScenes"]

        total = count()
        if total["count"] == 0:
            return {}

        def edge_year(direction):
            variables = {
                "input": {
                    **entity_filter,
                    "page": 1, "per_page": 1,
                    "sort": "DATE", "direction": direction,
                }
            }
            resp = self._post({"query": QUERY_SCENES, "variables": variables})
            resp.raise_for_status()
            data = resp.json()
            if data.get("errors"):
                raise RuntimeError(str(data["errors"]))
            scenes = data["data"]["queryScenes"]["scenes"]
            date = scenes[0].get("date") if scenes else None
            return int(date[:4]) if date else None

        oldest_year = edge_year("ASC")
        newest_year = edge_year("DESC")
        if oldest_year is None or newest_year is None:
            return {}

        # cumulative_from[y] = count of scenes dated in year y or later.
        # The sentinel at newest_year + 1 anchors the top of the range
        # (should be 0, confirmed by construction since nothing is
        # dated after the newest scene) so the loop below needs no
        # special-casing for the most recent year.
        cumulative_from = {}
        for year in range(oldest_year, newest_year + 2):
            cumulative_from[year] = count({"value": f"{year - 1}-12-31", "modifier": "GREATER_THAN"})["count"]
            time.sleep(self.delay)

        return {
            year: cumulative_from[year] - cumulative_from[year + 1]
            for year in range(oldest_year, newest_year + 1)
        }

    def fetch_scenes_for_year(self, entity_type, stashdb_id, year):
        """Yields every scene for this entity dated within calendar
        `year` (an int), for on-demand backlog browsing (2026-09-13) --
        unlike fetch_new_scenes(), this is never watermark-based and
        may paginate through a large slice of the entity's catalog.
        Since StashDB's `date` filter only accepts one bound per query
        (see count_scenes_by_year()'s docstring), only the lower bound
        (`date > (year-1)-12-31`) is applied server-side; sorted DATE
        ascending, this yields scenes for `year` and beyond, so pagination
        stops client-side as soon as a scene's date rolls past
        `year`-12-31 -- the same "stop once past a threshold" pattern
        fetch_new_scenes() already uses for its `since_created`
        watermark, just against `date` instead of `created`."""
        for entity_filter in self._entity_filters(entity_type, stashdb_id):
            yield from self._fetch_scenes_for_year_for(entity_filter, year)

    def _fetch_scenes_for_year_for(self, entity_filter, year):
        lower_bound = f"{year - 1}-12-31"
        upper_bound = f"{year}-12-31"
        page = 1
        while True:
            variables = {
                "input": {
                    **entity_filter,
                    "date": {"value": lower_bound, "modifier": "GREATER_THAN"},
                    "page": page, "per_page": self.page_size,
                    "sort": "DATE", "direction": "ASC",
                }
            }
            resp = self._post({"query": QUERY_SCENES, "variables": variables})
            resp.raise_for_status()
            data = resp.json()
            if data.get("errors"):
                raise RuntimeError(str(data["errors"]))
            scenes = data["data"]["queryScenes"]["scenes"]
            if not scenes:
                return
            for s in scenes:
                if (s.get("date") or "") > upper_bound:
                    return
                s["image_url"] = (s.get("images") or [{}])[0].get("url")
                s["is_vr"] = any(t.get("name") == VR_TAG_NAME for t in (s.get("tags") or []))
                yield s
            page += 1
            time.sleep(self.delay)

    def fetch_scene_image(self, scene_id):
        """One-off per-scene lookup, not part of the regular scan path --
        used only by the image_url backfill for releases discovered
        before that column existed (see db.py's
        `_migrate_release_image_url()`)."""
        resp = self._post({"query": FIND_SCENE_IMAGE_QUERY, "variables": {"id": scene_id}})
        resp.raise_for_status()
        data = resp.json()
        if data.get("errors"):
            raise RuntimeError(str(data["errors"]))
        scene = data["data"]["findScene"]
        if not scene:
            return None
        return (scene.get("images") or [{}])[0].get("url")

    def _fetch_entity_image(self, query_name, entity_id):
        resp = self._post({"query": f"query($id: ID!) {{ {query_name}(id: $id) {{ id images {{ url }} }} }}", "variables": {"id": entity_id}})
        resp.raise_for_status()
        data = resp.json()
        if data.get("errors"):
            raise RuntimeError(str(data["errors"]))
        item = (data.get("data") or {}).get(query_name)
        return ((item or {}).get("images") or [{}])[0].get("url")

    def fetch_performer_image(self, performer_id):
        """The profile picture address of one performer on this stash-box (v0.256.0, scene_covers.py), or None."""
        return self._fetch_entity_image("findPerformer", performer_id)

    def fetch_studio_image(self, studio_id):
        """The logo address of one studio on this stash-box (v0.256.0, scene_covers.py), or None."""
        return self._fetch_entity_image("findStudio", studio_id)

    def find_scenes_by_fingerprints(self, fingerprint_sets):
        """`fingerprint_sets` is a list of lists of {"hash", "algorithm"}
        dicts -- one inner list per file/scene being looked up (a file
        can have multiple fingerprint types, all describing the same
        content, so they're grouped together in one StashDB "scene"
        slot rather than one call per fingerprint). Returns a list the
        same length and order as the input, each entry a (possibly
        empty) list of {"id", "title"} matches -- StashDB's own reverse
        lookup, "which scene(s) is this fingerprint registered against"
        (confirmed live 2026-09-15 while designing the "audit the
        existing library" tool: given a mismatched local file's own
        fingerprint, this is how to find out what it actually is, not
        just that it isn't what it's currently labeled as).

        Raises if given more than MAX_FINGERPRINT_BATCH sets at once --
        callers with more than that must chunk themselves (see
        scripts/audit_library_fingerprints.py for the batching/delay
        pattern used against the whole library)."""
        if len(fingerprint_sets) > MAX_FINGERPRINT_BATCH:
            raise ValueError(
                f"find_scenes_by_fingerprints: {len(fingerprint_sets)} sets requested, "
                f"StashDB rejects more than {MAX_FINGERPRINT_BATCH} per call"
            )
        resp = self._post({
            "query": FIND_SCENES_BY_FINGERPRINTS_QUERY,
            "variables": {"fps": fingerprint_sets},
        })
        resp.raise_for_status()
        data = resp.json()
        if data.get("errors"):
            raise RuntimeError(str(data["errors"]))
        return data["data"]["findScenesBySceneFingerprints"]

    def get_scene_fingerprints(self, scene_id):
        """This scene's own crowd-sourced oshash/phash fingerprints, as
        registered on StashDB itself -- the source of truth this
        project's download verification checks a local file against
        (see verification.py's verify_downloaded_file()), rather than
        just comparing against whatever else happens to already be in
        the local library (which only ever caught a mislabeled release
        colliding with an *already-owned* scene, not one that's simply
        wrong and unrelated to anything else locally -- see
        PROGRESS.md's "Boosting Morale"/"Summer Camp Rules"
        investigation for why the narrower check wasn't enough).
        Confirmed live 2026-09-14 this query works directly against a
        real scene id (used ad hoc, before this became a proper
        method, to compare "Boosting Morale" and "Summer Camp Rules"'s
        own registered fingerprints and find zero overlap between
        them, while the actual downloaded file matched Summer Camp
        Rules' list exactly). Returns a list of {"hash", "algorithm"}
        dicts (algorithm e.g. "OSHASH"/"PHASH", uppercase) -- empty
        list if the scene has none registered or doesn't exist."""
        resp = self._post({"query": FIND_SCENE_FINGERPRINTS_QUERY, "variables": {"id": scene_id}})
        resp.raise_for_status()
        data = resp.json()
        if data.get("errors"):
            raise RuntimeError(str(data["errors"]))
        scene = data["data"]["findScene"]
        return (scene or {}).get("fingerprints") or []

    def get_scene_detail(self, scene_id):
        """Title/date/studio/performers(with gender) for one scene --
        backs file_naming.py's token builder for Milestone 2's own
        import/rename step. Returns None if StashDB doesn't have this
        scene (deleted, or an id that never existed)."""
        resp = self._post({"query": FIND_SCENE_DETAIL_QUERY, "variables": {"id": scene_id}})
        resp.raise_for_status()
        data = resp.json()
        if data.get("errors"):
            raise RuntimeError(str(data["errors"]))
        return data["data"]["findScene"]

    def get_scene_full(self, scene_id):
        """One scene in the shape search_scenes() returns (id/title/date/created/duration/studio/performers/image_url/is_vr), so it can go
        into the wishlist hand-off unchanged (the userscript sends only the id). None for an unknown scene."""
        resp = self._post({
            "query": "query($id: ID!) { findScene(id: $id) { id title date created details duration studio { id name } "
                      "performers { performer { id name } } images { url } tags { name } } }",
            "variables": {"id": scene_id},
        })
        resp.raise_for_status()
        data = resp.json()
        if data.get("errors"):
            raise RuntimeError(str(data["errors"]))
        scene = data["data"]["findScene"]
        return self._normalize_search_scenes([scene])[0] if scene else None

    def get_scene_urls(self, scene_id):
        """[{"url", "site"}] of one scene (site = the link type, e.g. "Studio"). Raises on a failed request; [] for an unknown scene."""
        resp = self._post({"query": FIND_SCENE_URLS_QUERY, "variables": {"id": scene_id}})
        resp.raise_for_status()
        data = resp.json()
        if data.get("errors"):
            raise RuntimeError(str(data["errors"]))
        scene = data["data"]["findScene"] or {}
        return [{"url": u.get("url") or "", "site": (u.get("site") or {}).get("name") or ""} for u in scene.get("urls") or []]

    def get_child_studios(self, studio_id):
        """Sub-studios of a studio, with their own children (one request): [{"id", "name", "parent_id"}] -- v0.93.0, backs the studio
        panel's sub-studio checklist."""
        query = ("query($id: ID!) { findStudio(id: $id) { id child_studios { id name child_studios { id name } } } }")
        resp = self._post({"query": query, "variables": {"id": studio_id}})
        resp.raise_for_status()
        data = resp.json()
        if data.get("errors"):
            raise RuntimeError(str(data["errors"]))
        studio = (data.get("data") or {}).get("findStudio") or {}
        out = []
        for child in studio.get("child_studios") or []:
            out.append({"id": child["id"], "name": child["name"], "parent_id": studio_id})
            for grand in child.get("child_studios") or []:
                out.append({"id": grand["id"], "name": grand["name"], "parent_id": child["id"]})
        return out

    def get_scenes_tags_batch(self, scene_ids, chunk=25):
        """{scene id: {"tags": [names], "studio_id", "studio_name"}} for many scenes with ONE request per `chunk` ids (aliased findScene
        fields in a single query -- v0.92.0 tags backfill). A scene StashDB no longer knows is simply missing from the result."""
        out = {}
        ids = list(scene_ids)
        for i in range(0, len(ids), chunk):
            part = ids[i:i + chunk]
            fields = "\n".join(f's{n}: findScene(id: "{sid}") {{ id studio {{ id name }} tags {{ name }} }}' for n, sid in enumerate(part))
            resp = self._post({"query": "query {\n" + fields + "\n}", "variables": {}})
            resp.raise_for_status()
            data = resp.json()
            if data.get("errors") and not data.get("data"):
                raise RuntimeError(str(data["errors"]))
            for sc in (data.get("data") or {}).values():
                if sc:
                    out[sc["id"]] = {"tags": [t["name"] for t in (sc.get("tags") or []) if t.get("name")],
                                     "studio_id": (sc.get("studio") or {}).get("id"), "studio_name": (sc.get("studio") or {}).get("name")}
            time.sleep(self.delay)
        return out

    def get_scenes_cast_batch(self, scene_ids, chunk=25):
        """{scene id: [performer dicts with gender]} with ONE request per `chunk` ids (aliased findScene, like get_scenes_tags_batch).
        A scene StashDB no longer knows is missing from the result. Backs the "scene pairing" filter (v0.182.0)."""
        out = {}
        ids = list(scene_ids)
        for i in range(0, len(ids), chunk):
            part = ids[i:i + chunk]
            fields = "\n".join(f's{n}: findScene(id: "{sid}") {{ id performers {{ performer {{ name gender }} }} }}' for n, sid in enumerate(part))
            resp = self._post({"query": "query {\n" + fields + "\n}", "variables": {}})
            resp.raise_for_status()
            data = resp.json()
            if data.get("errors") and not data.get("data"):
                raise RuntimeError(str(data["errors"]))
            for sc in (data.get("data") or {}).values():
                if sc:
                    out[sc["id"]] = sc.get("performers") or []
            time.sleep(self.delay)
        return out

    def get_scenes_state_batch(self, scene_ids, chunk=25):
        """{asked id: {"state": "ok" | "redirect" | "gone", "id": the id StashDB answered with}} with ONE request per `chunk` ids (aliased
        findScene). "redirect" = StashDB answered with a DIFFERENT scene (the asked one was merged into it); "gone" = unknown or flagged
        deleted. Backs the StashDB-duplicate watch (scene_alias.check_duplicates)."""
        out = {}
        ids = list(scene_ids)
        for i in range(0, len(ids), chunk):
            part = ids[i:i + chunk]
            fields = "\n".join(f's{n}: findScene(id: "{sid}") {{ id deleted }}' for n, sid in enumerate(part))
            resp = self._post({"query": "query {\n" + fields + "\n}", "variables": {}})
            resp.raise_for_status()
            data = resp.json()
            if data.get("errors") and not data.get("data"):
                raise RuntimeError(str(data["errors"]))
            got = data.get("data") or {}
            for n, sid in enumerate(part):
                sc = got.get(f"s{n}")
                if not sc or sc.get("deleted"):
                    out[sid] = {"state": "gone", "id": None}
                elif sc["id"] != sid:
                    out[sid] = {"state": "redirect", "id": sc["id"]}
                else:
                    out[sid] = {"state": "ok", "id": sid}
            time.sleep(self.delay)
        return out

    def search_performers(self, term, limit=100):
        """Free-text performer search (2026-09-19, backs the plugin's
        own Search page -- see PROGRESS.md/roadmap memory: this
        replaces the previous StashDB.org-website + Stasharr browser
        workflow for finding a Performer/Studio/Scene not yet on the
        watchlist). `searchPerformers` is a distinct top-level query
        from `queryPerformers` -- confirmed live 2026-09-19 that a
        blank/placeholder API key gets "not authorized" on EITHER, so
        don't mistake that for evidence this needs elevated StashDB
        access; a real key works fine. `is_favorite` here is the
        user's own StashDB.org-*account* favorite (separate from this
        Stash instance's local Favorite flag, which is what actually
        drives this project's watchlist -- see stash_client.py's
        create_or_favorite_performer()).

        Returns `{"total", "results"}`, not a plain list -- confirmed
        live 2026-09-19 that StashDB's own result ordering for a broad
        term is NOT relevance-ranked (a common first name alone can
        rank the actual person being searched for far past position 25,
        e.g. "Hazel" ranked "Hazel Moore" 50th out of 74 real matches),
        so a `limit` cutoff routinely hides someone a more specific
        search would have found instantly. `total` (StashDB's own
        `count`) lets the Search page tell the user their results were
        truncated instead of the previous silent "here are 25 results,
        good luck" (confirmed live: this project's own initial version
        just returned the bare list, no way to tell 25 back then from
        25-of-874)."""
        resp = self._post({
            "query": "query($term: String!, $limit: Int!) { searchPerformers(term: $term, limit: $limit) "
                      "{ count performers { id name disambiguation is_favorite images { url } } } }",
            "variables": {"term": term, "limit": limit},
        })
        resp.raise_for_status()
        data = resp.json()
        if data.get("errors"):
            raise RuntimeError(str(data["errors"]))
        wrapper = data["data"]["searchPerformers"]
        results = wrapper["performers"]
        for p in results:
            p["image_url"] = (p.pop("images", None) or [{}])[0].get("url")
        return {"total": wrapper["count"], "results": results}

    def find_scenes_on_date(self, studio_id, date):
        """The scenes of one studio dated exactly `date` (YYYY-MM-DD), with their cast: [{"id", "title", "date", "studio", "performers"}]
        (other_scene_check.py: which scene does a release name with studio + date belong to)."""
        variables = {"input": {"studios": {"value": [studio_id], "modifier": "INCLUDES"}, "date": {"value": date, "modifier": "EQUALS"},
                               "page": 1, "per_page": 40}}
        resp = self._post({"query": QUERY_SCENES, "variables": variables})
        resp.raise_for_status()
        data = resp.json()
        if data.get("errors"):
            raise RuntimeError(str(data["errors"]))
        return data["data"]["queryScenes"]["scenes"]

    def get_studio_name(self, studio_id):
        """The studio's name on this stash-box, or None (2026-09-28: StashDB's
        spelling wins for file/folder names, see file_importer)."""
        resp = self._post({"query": "query($id: ID!) { findStudio(id: $id) { name } }", "variables": {"id": studio_id}})
        resp.raise_for_status()
        data = resp.json()
        if data.get("errors"):
            raise RuntimeError(str(data["errors"]))
        return ((data.get("data") or {}).get("findStudio") or {}).get("name")

    def search_studios(self, term, limit=100):
        """`searchStudio` (singular -- not a typo, StashDB's own naming)
        returns the list directly, no `{count, studios}` wrapper like
        the performer/scene search queries -- confirmed live 2026-09-19
        via introspection, so `total` is always None here (no way to
        tell whether more exist beyond `limit`, unlike
        search_performers()/search_scenes() above)."""
        resp = self._post({
            "query": "query($term: String!, $limit: Int!) { searchStudio(term: $term, limit: $limit) "
                      "{ id name is_favorite parent { id name } images { url } } }",
            "variables": {"term": term, "limit": limit},
        })
        resp.raise_for_status()
        data = resp.json()
        if data.get("errors"):
            raise RuntimeError(str(data["errors"]))
        results = data["data"]["searchStudio"]
        for s in results:
            s["image_url"] = (s.pop("images", None) or [{}])[0].get("url")
        return {"total": None, "results": results}

    def search_scenes(self, term, limit=100):
        """Field selection matches QUERY_SCENES' own normalized shape
        (id/title/date/created/image_url/is_vr) plus `performers`, so a
        result here can go straight into release_repo.insert_release()
        unmodified when the user hands it off to Whisparr (see
        routers/search.py) -- no separate normalization step needed.
        Returns `{"total", "results"}` -- see search_performers()'s
        docstring for why."""
        resp = self._post({
            "query": "query($term: String!, $limit: Int!) { searchScenes(term: $term, limit: $limit) "
                      "{ count scenes { id title date created details duration studio { id name } "
                      "performers { performer { id name } } images { url } tags { name } } } }",
            "variables": {"term": term, "limit": limit},
        })
        resp.raise_for_status()
        data = resp.json()
        if data.get("errors"):
            raise RuntimeError(str(data["errors"]))
        wrapper = data["data"]["searchScenes"]
        return {"total": wrapper["count"], "results": self._normalize_search_scenes(wrapper["scenes"])}

    @staticmethod
    def _normalize_search_scenes(results):
        for s in results:
            s["image_url"] = (s.pop("images", None) or [{}])[0].get("url")
            s["is_vr"] = any(t.get("name") == VR_TAG_NAME for t in (s.pop("tags", None) or []))
            s["performers"] = [p["performer"] for p in (s.get("performers") or [])]
        return results

    def scene_durations(self, scene_ids):
        """{scene id: seconds or None} -- the length the provider has for each scene, many per request (aliased findScene; v0.209.0).
        None when the scene is unknown or has no length maintained."""
        ids = [str(i) for i in scene_ids]
        if not ids:
            return {}
        query = "query{" + " ".join(f's{n}: findScene(id: "{i}") {{ id duration }}' for n, i in enumerate(ids)) + "}"
        resp = self._post({"query": query})
        resp.raise_for_status()
        data = resp.json()
        if data.get("errors") and not data.get("data"):
            raise RuntimeError(str(data["errors"]))
        out = {i: None for i in ids}
        for n, i in enumerate(ids):
            scene = (data.get("data") or {}).get(f"s{n}") or {}
            out[i] = scene.get("duration") or None
        return out

    def search_tags(self, term, limit=30):
        """Tag search for the Search page's "Tags" tab: `{"total", "results": [{id, name, description}]}`. Tags are looked up by name
        (queryTags), the scenes of one tag come from search_scenes_by_tag()."""
        resp = self._post({
            "query": "query($input: TagQueryInput!) { queryTags(input: $input) { count tags { id name description } } }",
            "variables": {"input": {"name": term, "per_page": limit, "sort": "NAME", "direction": "ASC"}},
        })
        resp.raise_for_status()
        data = resp.json()
        if data.get("errors"):
            raise RuntimeError(str(data["errors"]))
        wrapper = data["data"]["queryTags"]
        return {"total": wrapper["count"], "results": wrapper["tags"]}

    EXCLUDE_MAX_PAGES = 10  # with excluded tags: at most this many pages of 100 are read to fill the list

    def search_scenes_by_tag(self, tag_ids, limit=100, exclude_ids=()):
        """The newest scenes carrying ALL the given tags and none of `exclude_ids` (v0.210.0 / v0.211.0), same normalized shape as
        search_scenes() (so Add / Add + search work on them).

        StashDB's queryScenes takes ONE tags criterion, so "has A and B but not C" cannot be asked in one query: the include tags are asked
        for, the excluded ones are filtered out here, reading further pages (EXCLUDE_MAX_PAGES) until `limit` scenes are left. `total` is the
        provider's count without excludes; with excludes it is the number of kept scenes when every page was read, else None (unknown)."""
        tag_ids = [tag_ids] if isinstance(tag_ids, str) else list(tag_ids)
        exclude = {str(i) for i in (exclude_ids or ())}
        kept, page, count = [], 1, 0
        while True:
            resp = self._post({
                "query": "query($input: SceneQueryInput!) { queryScenes(input: $input) "
                          "{ count scenes { id title date created details duration studio { id name } "
                          "performers { performer { id name } } images { url } tags { id name } } } }",
                "variables": {"input": {"tags": {"value": tag_ids, "modifier": "INCLUDES_ALL" if len(tag_ids) > 1 else "INCLUDES"},
                                        "page": page, "per_page": 100, "sort": "DATE", "direction": "DESC"}},
            })
            resp.raise_for_status()
            data = resp.json()
            if data.get("errors"):
                raise RuntimeError(str(data["errors"]))
            wrapper = data["data"]["queryScenes"]
            count = wrapper["count"]
            scenes = wrapper["scenes"]
            kept += [sc for sc in scenes if not exclude or not exclude & {str(t.get("id")) for t in (sc.get("tags") or [])}]
            exhausted = page * 100 >= count or not scenes
            if not exclude or exhausted or len(kept) >= limit or page >= self.EXCLUDE_MAX_PAGES:
                break
            page += 1
        kept = kept[:limit]
        total = count if not exclude else (len(kept) if exhausted and len(kept) <= limit else None)
        return {"total": total, "results": self._normalize_search_scenes(kept)}
