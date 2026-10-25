"""Minimal Stash GraphQL client.

Only what this project needs: seeding the watchlist from studios/
performers Stash already has scenes for, and cross-checking whether a
scene already exists locally (with a file) before treating a StashDB
release as still-missing -- see PROGRESS.md's note that Whisparr's own
`hasFile` can be stale and Stash's own current scene list is the
source of truth for that check.
"""
import logging
import re
import time

import requests

from http_deadline import call_with_deadline

logger = logging.getLogger(__name__)

FIND_STUDIOS_QUERY = """
query FindStudios($filter: FindFilterType) {
  findStudios(filter: $filter) {
    studios { id name scene_count stash_ids { endpoint stash_id } }
  }
}
"""

FIND_PERFORMERS_QUERY = """
query FindPerformers($filter: FindFilterType) {
  findPerformers(filter: $filter) {
    performers { id name scene_count stash_ids { endpoint stash_id } }
  }
}
"""

# Studio and Performer use DIFFERENT filter field names for the same
# "starred" concept -- confirmed live against this Stash version's
# schema 2026-09-11 (StudioFilterType.favorite vs.
# PerformerFilterType.filter_favorites). Don't assume symmetry here if
# porting to a different Stash version -- re-check via introspection.
# (Used by favorited_studios()/favorited_performers() below.)

FIND_SCENE_BY_STASH_ID_QUERY = """
query FindScenes($scene_filter: SceneFilterType) {
  findScenes(scene_filter: $scene_filter, filter: {per_page: 1}) {
    scenes { id files { path } }
  }
}
"""

FIND_SCENES_BY_STASH_IDS_QUERY = """
query FindScenesByStashIds($scene_filter: SceneFilterType, $filter: FindFilterType) {
  findScenes(scene_filter: $scene_filter, filter: $filter) {
    count
    scenes { id files { height } stash_ids { endpoint stash_id } }
  }
}
"""

STUDIO_STASH_IDS_QUERY = """
query StudioStashIds($id: ID!) { findStudio(id: $id) { stash_ids { endpoint stash_id } } }
"""

STUDIO_SET_STASH_IDS_MUTATION = """
mutation StudioSetStashIds($input: StudioUpdateInput!) { studioUpdate(input: $input) { id } }
"""

FIND_STUDIOS_BY_REGEX_QUERY = """
query FindStudiosByRegex($filter: StudioFilterType) {
  findStudios(studio_filter: $filter, filter: {per_page: 5}) { studios { id name } }
}
"""

SCENE_FILE_PATHS_QUERY = """
query SceneFilePaths($id: ID!) {
  findScene(id: $id) { files { path } }
}
"""

FIND_SCENE_BY_PATH_QUERY = """
query FindSceneByPath($scene_filter: SceneFilterType) {
  findScenes(scene_filter: $scene_filter, filter: {per_page: 1}) {
    scenes { id title created_at stash_ids { endpoint stash_id } files { id path created_at fingerprints { type value } } }
  }
}
"""

SCENE_DESTROY_MUTATION = """
mutation SceneDestroy($id: ID!, $delete_file: Boolean!, $delete_generated: Boolean!) {
  sceneDestroy(input: {id: $id, delete_file: $delete_file, delete_generated: $delete_generated})
}
"""

FIND_SCENES_WITH_STASHDB_ID_QUERY = """
query FindScenesWithStashdbId($scene_filter: SceneFilterType, $filter: FindFilterType) {
  findScenes(scene_filter: $scene_filter, filter: $filter) {
    count
    scenes {
      id
      title
      stash_ids { endpoint stash_id }
      files { path fingerprints { type value } }
    }
  }
}
"""

BULK_STUDIO_FAVORITE_MUTATION = """
mutation($input: BulkStudioUpdateInput!) {
  bulkStudioUpdate(input: $input) { id }
}
"""

BULK_PERFORMER_FAVORITE_MUTATION = """
mutation($input: BulkPerformerUpdateInput!) {
  bulkPerformerUpdate(input: $input) { id }
}
"""

# Same mechanism Stash's own "Tagger" UI uses to batch-match local
# Studios/Performers against a configured Stash-box endpoint (see
# stash_client.py's theporndb_endpoint / PROGRESS.md's "ThePornDB scan
# integration" section) -- `createParent: false` is required by this
# schema version even though it has no bearing when `exclude_fields`
# already excludes "parent" (confirmed live 2026-09-11: the mutation
# fails GraphQL validation entirely if createParent is omitted).
BATCH_TAG_STUDIO_MUTATION = """
mutation($input: StashBoxBatchTagInput!) {
  stashBoxBatchStudioTag(input: $input)
}
"""

BATCH_TAG_PERFORMER_MUTATION = """
mutation($input: StashBoxBatchTagInput!) {
  stashBoxBatchPerformerTag(input: $input)
}
"""

FIND_JOB_QUERY = """
query($id: ID!) { findJob(input: {id: $id}) { id status error } }
"""

# Backs file_importer.py's scoped rescan of the exact path it just
# moved a file into (Milestone 2, 2026-09-17) -- confirmed live via
# schema introspection `ScanMetadataInput` takes a bare `paths: [String]`
# list. `scanGeneratePhashes: true` explicitly forced regardless of
# Stash's own global scan settings, since verification.py's
# verify_downloaded_file() depends on a phash existing for the file
# right after this scan finishes, not on whatever the next scheduled
# full scan happens to generate.
SCAN_METADATA_MUTATION = """
mutation($input: ScanMetadataInput!) { metadataScan(input: $input) }
"""

# Backs file_importer.py's post-import thumbnail/preview generation
# (2026-09-19, added after the user pointed out a freshly-imported
# scene had no cover/preview/sprite even once correctly scanned+tagged
# -- metadataScan's own `scanGeneratePhashes` only covers the phash
# fingerprint verify_downloaded_file() needs, not the actual UI media).
# `sceneIDs` scopes generation to just the one new scene, confirmed
# live via schema introspection alongside `paths` -- preferred here
# since file_importer.py already has the scene's own local id from its
# find_scene_by_path() lookup, no second path-based match needed.
GENERATE_METADATA_MUTATION = """
mutation($input: GenerateMetadataInput!) { metadataGenerate(input: $input) }
"""

# Backs fix_library_mismatches.py's retagging of a local scene that
# audit_library_fingerprints.py found pointing at the wrong StashDB
# scene id -- passing the target scene's own stash-box UUID as
# `query` does an exact ID lookup against that stash-box (confirmed
# live 2026-09-16 against a real mismatch case: querying "Playful
# Blonde"'s id returned exactly that scene, not a fuzzy text search),
# the same mechanism as pasting a stash-box scene URL/id into Stash's
# own "Scrape with..." UI. Distinct from a *local* scene_id lookup
# (this input also supports that, unused here).
SCRAPE_SCENE_BY_STASHDB_ID_QUERY = """
query($source: ScraperSourceInput!, $input: ScrapeSingleSceneInput!) {
  scrapeSingleScene(source: $source, input: $input) {
    title
    date
    details
    studio { stored_id name remote_site_id }
    performers {
      stored_id name gender birthdate ethnicity country eye_color height
      measurements fake_tits penis_length circumcised career_start career_end
      tattoos piercings aliases hair_color death_date images remote_site_id
    }
    tags { stored_id name }
    remote_site_id
  }
}
"""

# Pre-check for fix_library_mismatches.py: is the CORRECT scene id
# already claimed by some OTHER local scene? (No global uniqueness
# constraint on stash_id across different local scenes was found --
# only UNIQUE(scene_id, endpoint) per scene, confirmed live
# 2026-09-15 -- so this has to be checked explicitly rather than
# relying on the database to reject a collision.) Confirmed live
# 2026-09-16: none of the 17 real mismatches found by the audit hit
# this case, but a future run against a different library could.
FIND_SCENE_BY_STASHDB_ID_QUERY = """
query($stashdb_id: String!, $endpoint: String!) {
  findScenes(scene_filter: {stash_id_endpoint: {endpoint: $endpoint, stash_id: $stashdb_id, modifier: EQUALS}}) {
    scenes { id title }
  }
}
"""

SCENE_UPDATE_MUTATION = """
mutation($input: SceneUpdateInput!) {
  sceneUpdate(input: $input) { id title stash_ids { endpoint stash_id } }
}
"""

# Backs the Maintenance page's directory-picker quick-jump shortcuts
# (2026-09-16) -- Stash's own configured top-level library
# directories. Confirmed live: this instance has 7 (/data/Scenes,
# /data/Movies, /data/PicSets, /data/VR, /data/movies, /data/scenes,
# /data/import).
LIST_LIBRARY_PATHS_QUERY = """
query { configuration { general { stashes { path } } } }
"""

# The exact query behind Stash's own native "Select folders" dialog
# (Scan/Clean/Generate's "Selective..." buttons) -- confirmed live via
# schema introspection (`directory(path: String) { path parent
# directories }`) after the user asked for the Maintenance page's own
# directory picker to match Stash's real one, which browses/adds
# arbitrary subdirectories rather than only offering the top-level
# configured paths as flat checkboxes.
BROWSE_DIRECTORY_QUERY = """
query($path: String) { directory(path: $path) { path parent directories } }
"""


# Rough, human-readable resolution labels (2026-09-13, for
# library_heights_by_stash_ids() below) -- same buckets Whisparr's own
# quality names use (e.g. "WEBDL-1080p"), close enough for an at-a-
# glance "is this worth upgrading" judgment without needing Whisparr's
# actual quality-profile taxonomy. Not underscore-prefixed -- shared
# across modules (routers/releases.py), not private to this one.
def quality_label(height):
    if height >= 2160:
        return "4K"
    if height >= 1440:
        return "1440p"
    if height >= 1080:
        return "1080p"
    if height >= 720:
        return "720p"
    if height >= 480:
        return "480p"
    return f"{height}p"


class StashClient:
    def __init__(self, url, api_key, stashdb_endpoint="https://stashdb.org/graphql",
                 theporndb_endpoint="https://theporndb.net/graphql", timeout=30):
        self.endpoint = f"{url.rstrip('/')}/graphql"
        self.stashdb_endpoint = stashdb_endpoint
        # The stash-box-shim endpoint local Stash itself is configured
        # against for ThePornDB identification (Settings > Metadata
        # Providers > Stash-box Endpoints) -- confirmed live 2026-09-11.
        # Not a Settings-page-editable value like the other connections:
        # it's an internal detail of how local `stash_ids` are tagged,
        # not something the user thinks of as a separate "connection".
        self.theporndb_endpoint = theporndb_endpoint
        self.session = requests.Session()
        if api_key:
            self.session.headers["ApiKey"] = api_key
        self.timeout = timeout
        # Optional callable(list of StashDB scene ids) -> {alias id: local scene id} (scene_alias.py): provider scenes whose
        # video is already in the library under another local scene count as owned. Set by main.get_stash_client().
        self.alias_lookup = None

    def _graphql(self, query, variables):
        """Wrapped with a hard wall-clock deadline (self.timeout + a
        buffer) independent of `requests`' own timeout= -- see
        http_deadline.py's docstring for why that alone isn't a
        reliable cap (confirmed live 2026-09-12 as the likely cause of
        a scan thread hanging indefinitely, see PROGRESS.md's "Scan
        appeared to hang permanently" section)."""
        resp = call_with_deadline(
            self.session.post, self.endpoint, json={"query": query, "variables": variables},
            timeout=self.timeout, deadline=self.timeout + 15,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("errors"):
            raise RuntimeError(f"Stash GraphQL error: {data['errors']}")
        return data["data"]

    def _stashdb_id(self, stash_ids):
        return self._stash_id_for_endpoint(stash_ids, self.stashdb_endpoint)

    def _stash_id_for_endpoint(self, stash_ids, endpoint):
        for sid in stash_ids:
            if sid.get("endpoint") == endpoint:
                return sid.get("stash_id")
        return None

    def ping(self):
        """Cheapest possible authenticated call -- a 1-result findStudios
        -- used only to verify a URL/API key pair actually works (see the
        Releases page's "Check connections" button). Raises on any HTTP
        or GraphQL error."""
        self._graphql(FIND_STUDIOS_QUERY, {"filter": {"per_page": 1}})

    def studios_with_scenes(self, min_scenes=1):
        """Studios currently with at least `min_scenes` scenes tracked in
        Stash, each carrying its StashDB id -- used to seed the
        watchlist. Approximates the "≥1 scene with a file" rule used to
        build watched_studios.yaml; not cross-checked per-scene for a
        file here since this only runs as a one-time convenience seed."""
        data = self._graphql(FIND_STUDIOS_QUERY, {"filter": {"per_page": -1}})
        out = []
        for s in data["findStudios"]["studios"]:
            if s["scene_count"] < min_scenes:
                continue
            stashdb_id = self._stashdb_id(s["stash_ids"])
            if stashdb_id:
                out.append({"name": s["name"], "stashdb_id": stashdb_id})
        return out

    def performers_with_scenes(self, min_scenes=1):
        data = self._graphql(FIND_PERFORMERS_QUERY, {"filter": {"per_page": -1}})
        out = []
        for p in data["findPerformers"]["performers"]:
            if p["scene_count"] < min_scenes:
                continue
            stashdb_id = self._stashdb_id(p["stash_ids"])
            if stashdb_id:
                out.append({"name": p["name"], "stashdb_id": stashdb_id})
        return out

    def favorited_studios(self, extra_endpoints=None):
        """Studios currently marked Favorite in Stash -- the watchlist's
        actual source of truth as of 2026-09-11 (see PROGRESS.md's
        "Favorite-driven watchlist" section): watching a studio/performer
        means starring it in Stash, nothing else. Each entry carries the
        local Stash `id` and whichever provider ids this studio has
        already been matched to locally -- `theporndb_id` is None until
        the studio has actually been identified against ThePornDB (see
        PROGRESS.md's "ThePornDB scan integration" section). Every
        favorited entity is included, even with neither provider id set
        yet -- unlike an earlier version of this method, which dropped
        those entirely; the local `id` on an as-yet-unmatched entity is
        exactly what theporndb_identify.py needs to attempt a match.

        `extra_endpoints` (Metadata Provider generalization, 2026-09-21)
        is an optional {provider_key: endpoint_url} map for ADDITIONAL
        generic stash-box providers beyond StashDB/ThePornDB -- their
        ids land in the new `provider_ids` dict alongside 'stashdb'/
        'theporndb' (also mirrored there for a uniform lookup), which
        sync.py's generic provider loop reads. The existing top-level
        `stashdb_id`/`theporndb_id` fields are unchanged, still always
        present, so stashdb_favorites.py/theporndb_identify.py (which
        read those directly, never `extra_endpoints`) keep working
        untouched."""
        return self._list_entities("studio", {"favorite": True}, extra_endpoints)

    def favorited_performers(self, extra_endpoints=None):
        return self._list_entities("performer", {"filter_favorites": True}, extra_endpoints)

    def tagged_studios(self, tag_id, extra_endpoints=None):
        """Studios carrying `tag_id` or any of its child tags (depth -1)
        -- the 'tag' watchlist source (2026-09-26, see
        watchlist_source.py). Same entry shape as favorited_studios()."""
        return self._list_entities("studio", {"tags": self.tag_criterion(tag_id)}, extra_endpoints)

    def tagged_performers(self, tag_id, extra_endpoints=None):
        return self._list_entities("performer", {"tags": self.tag_criterion(tag_id)}, extra_endpoints)

    @staticmethod
    def tag_criterion(tag_id):
        return {"value": [str(tag_id)], "modifier": "INCLUDES", "depth": -1}

    def _list_entities(self, entity_type, entity_filter, extra_endpoints=None):
        """Every Studio/Performer matching `entity_filter` (a
        StudioFilterType/PerformerFilterType dict), each with its local
        `id`, name and per-provider ids -- shared by the favorites and
        the tag watchlist source."""
        extra_endpoints = extra_endpoints or {}
        root = "findStudios" if entity_type == "studio" else "findPerformers"
        items_key = "studios" if entity_type == "studio" else "performers"
        filter_arg = "studio_filter: $f" if entity_type == "studio" else "performer_filter: $f"
        filter_type = "StudioFilterType" if entity_type == "studio" else "PerformerFilterType"
        data = self._graphql(
            f"query($f: {filter_type}, $filter: FindFilterType) {{ {root}({filter_arg}, filter: $filter) "
            f"{{ {items_key} {{ id name stash_ids {{ endpoint stash_id }} }} }} }}",
            {"f": entity_filter, "filter": {"per_page": -1}},
        )
        out = []
        for e in data[root][items_key]:
            stashdb_id = self._stash_id_for_endpoint(e["stash_ids"], self.stashdb_endpoint)
            theporndb_id = self._stash_id_for_endpoint(e["stash_ids"], self.theporndb_endpoint)
            provider_ids = {"stashdb": stashdb_id, "theporndb": theporndb_id}
            for key, endpoint in extra_endpoints.items():
                provider_ids[key] = self._stash_id_for_endpoint(e["stash_ids"], endpoint)
            out.append({
                "id": e["id"], "name": e["name"],
                "stashdb_id": stashdb_id, "theporndb_id": theporndb_id, "provider_ids": provider_ids,
            })
        return out

    def count_entities(self, entity_type, entity_filter):
        root = "findStudios" if entity_type == "studio" else "findPerformers"
        filter_arg = "studio_filter: $f" if entity_type == "studio" else "performer_filter: $f"
        filter_type = "StudioFilterType" if entity_type == "studio" else "PerformerFilterType"
        data = self._graphql(
            f"query($f: {filter_type}) {{ {root}({filter_arg}, filter: {{per_page: 0}}) {{ count }} }}",
            {"f": entity_filter},
        )
        return data[root]["count"]

    # --- Watchlist tag (2026-09-26, see watchlist_source.py) ---

    def find_tag_id(self, name):
        """Exact (case-insensitive, Stash's own EQUALS semantics) name
        match, or None."""
        data = self._graphql(
            "query($n: String!) { findTags(tag_filter: {name: {value: $n, modifier: EQUALS}}, "
            "filter: {per_page: 1}) { tags { id name } } }",
            {"n": name},
        )
        tags = data["findTags"]["tags"]
        return tags[0]["id"] if tags else None

    def ensure_tag(self, name, description=None):
        tag_id = self.find_tag_id(name)
        if tag_id:
            return tag_id
        tag_input = {"name": name}
        if description:
            tag_input["description"] = description
        data = self._graphql("mutation($i: TagCreateInput!) { tagCreate(input: $i) { id } }", {"i": tag_input})
        return data["tagCreate"]["id"]

    def tag_with_descendants(self, tag_id):
        """`tag_id` plus every child tag below it, as a set of id strings
        -- what "watched" means in tag mode (child tags count too)."""
        data = self._graphql(
            "query($t: [ID!]) { findTags(tag_filter: {parents: {value: $t, modifier: INCLUDES, depth: -1}}, "
            "filter: {per_page: -1}) { tags { id } } }",
            {"t": [str(tag_id)]},
        )
        return {str(tag_id)} | {t["id"] for t in data["findTags"]["tags"]}

    def entity_state(self, entity_type, local_id):
        """Current favorite flag + tag ids of one Studio/Performer, read
        fresh (the plugin hook's own payload isn't trusted for this, see
        routers/watchlist.py's entity_changed()). None if not found."""
        root = "findStudio" if entity_type == "studio" else "findPerformer"
        data = self._graphql(
            f"query($id: ID!) {{ {root}(id: $id) {{ id name favorite tags {{ id }} }} }}",
            {"id": str(local_id)},
        )
        entity = data[root]
        if not entity:
            return None
        return {"id": entity["id"], "name": entity["name"], "favorite": entity["favorite"],
                "tag_ids": {t["id"] for t in entity["tags"]}}

    def update_entity_tags(self, entity_type, local_ids, tag_ids, mode, batch_size=25):
        """Adds (mode 'ADD') or removes (mode 'REMOVE') `tag_ids` on the
        given Studios/Performers via bulk*Update, in batches -- same
        batching/error handling as _bulk_favorite() (see its docstring:
        each id fires Stash's *.Update.Post hook, so only pass ids that
        actually change). Returns (succeeded_batches, failed_batches)."""
        mutation = BULK_STUDIO_FAVORITE_MUTATION if entity_type == "studio" else BULK_PERFORMER_FAVORITE_MUTATION
        succeeded, failed = 0, 0
        for i in range(0, len(local_ids), batch_size):
            batch = local_ids[i:i + batch_size]
            try:
                self._graphql(mutation, {"input": {
                    "ids": [str(x) for x in batch],
                    "tag_ids": {"ids": [str(t) for t in tag_ids], "mode": mode},
                }})
                succeeded += 1
            except Exception:
                logger.warning("bulk tag %s batch failed (%s ids %s)", mode, entity_type, batch, exc_info=True)
                failed += 1
        return succeeded, failed

    def batch_tag_studios(self, ids, stash_box_endpoint, exclude_fields, refresh=False):
        return self._batch_tag(BATCH_TAG_STUDIO_MUTATION, "stashBoxBatchStudioTag",
                                ids, stash_box_endpoint, exclude_fields, refresh)

    def batch_tag_performers(self, ids, stash_box_endpoint, exclude_fields, refresh=False):
        return self._batch_tag(BATCH_TAG_PERFORMER_MUTATION, "stashBoxBatchPerformerTag",
                                ids, stash_box_endpoint, exclude_fields, refresh)

    def _batch_tag(self, mutation, result_key, ids, stash_box_endpoint, exclude_fields, refresh):
        """Triggers Stash's own batch stash-box tagging job -- the same
        mechanism its "Tagger" UI uses -- against the given local Stash
        `ids`, matching them against `stash_box_endpoint` (e.g.
        ThePornDB's, see theporndb_identify.py). Returns a job id string;
        poll it with wait_for_job(). refresh=False only attempts entities
        with no existing match at this endpoint yet -- cheap/safe to
        call repeatedly (an interval, a manual "Sync now" button).
        `createParent: false` is a required field for this mutation in
        this Stash schema version even when irrelevant (confirmed live
        2026-09-11: omitting it fails GraphQL validation outright)."""
        variables = {"input": {
            "stash_box_endpoint": stash_box_endpoint,
            "ids": [str(i) for i in ids],
            "refresh": refresh,
            "createParent": False,
            "exclude_fields": exclude_fields,
        }}
        data = self._graphql(mutation, variables)
        return data[result_key]

    def scan_paths(self, paths):
        """Triggers Stash's own metadataScan job scoped to `paths` --
        used after file_importer.py moves a file into place, instead of
        waiting for Stash's own next scheduled full scan to notice it.
        Returns a job id (file_importer.py no longer waits on it since
        2026-09-25 -- the Scene.Create.Post hook signals completion)."""
        variables = {"input": {"paths": paths, "scanGeneratePhashes": True}}
        data = self._graphql(SCAN_METADATA_MUTATION, variables)
        return data["metadataScan"]

    def generate_metadata_for_scenes(self, scene_ids):
        """Triggers Stash's own metadataGenerate job (covers, sprites,
        previews) scoped to `scene_ids` -- deliberately NOT covering
        `phashes` (already produced by scan_paths()'s own
        `scanGeneratePhashes`) or `transcodes`/`markers`/heatmaps (no
        reason for this project's own fresh-import path to force those).
        Fire-and-forget by design: unlike scan_paths(), the caller
        should NOT wait_for_job() on this one -- Stash runs its job
        queue strictly one-at-a-time (confirmed live, see PROGRESS.md's
        "library-wide Generate backlog" investigation, where a single
        library's marker/plugin-driven Generate queue reached over
        11,000 pending jobs), so blocking file_importer.py's own import
        loop on this job finishing could stall it for hours behind
        unrelated work that happens to be queued ahead of it. Returns
        the job id purely for logging."""
        variables = {"input": {
            "sceneIDs": [str(i) for i in scene_ids],
            "covers": True, "sprites": True, "previews": True,
        }}
        data = self._graphql(GENERATE_METADATA_MUTATION, variables)
        return data["metadataGenerate"]

    def submit_fingerprints(self, scene_id, endpoint=None):
        """Submits the fingerprints (phash, oshash, md5 + duration) of the scene's files to StashDB with Stash's own stash-box account --
        the same mutation as the tagger's "Submit fingerprints". PUBLIC and not undoable; the scene must carry a StashDB id. (v0.106.7)"""
        data = self._graphql("mutation($input: StashBoxFingerprintSubmissionInput!) { submitStashBoxFingerprints(input: $input) }",
                             {"input": {"scene_ids": [str(scene_id)], "stash_box_endpoint": endpoint or self.stashdb_endpoint}})
        return bool(data["submitStashBoxFingerprints"])

    def regenerate_phash(self, scene_id):
        """Recomputes the phash of one scene's files, overwriting the stored
        one (2026-09-26, see verification.py's stale-fingerprint check).
        Returns the job id."""
        data = self._graphql(GENERATE_METADATA_MUTATION, {"input": {
            "sceneIDs": [str(scene_id)], "phashes": True, "overwrite": True}})
        return data["metadataGenerate"]

    def job_finished(self, job_id):
        """True once a job is no longer queued/running (a finished job may
        also vanish from findJob, see wait_for_job())."""
        job = self._graphql(FIND_JOB_QUERY, {"id": str(job_id)})["findJob"]
        return job is None or job["status"] in ("FINISHED", "CANCELLED", "FAILED")

    def wait_queue_idle(self, max_wait=300, poll_interval=5):
        """True once Stash's job queue has no RUNNING/READY job; False when it is still busy after `max_wait` seconds. Stash runs one job
        at a time, so a job submitted behind a busy queue only starts when the others are done -- callers with their own wait_for_job
        timeout check this first so the time spent waiting in the queue does not count against that timeout (v0.123.1)."""
        deadline = time.time() + max_wait
        while True:
            queue = self._graphql("{ jobQueue { id status } }", {})["jobQueue"] or []
            if not [j for j in queue if j["status"] in ("RUNNING", "READY")]:
                return True
            if time.time() >= deadline:
                return False
            time.sleep(poll_interval)

    def wait_for_job(self, job_id, poll_interval=2, timeout=600):
        """Polls Stash's job queue until the given job id finishes.
        Confirmed live 2026-09-11: a completed job can disappear from
        findJob (returns null) shortly after finishing, not just flip to
        FINISHED -- treated the same as FINISHED here, not an error."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            data = self._graphql(FIND_JOB_QUERY, {"id": str(job_id)})
            job = data["findJob"]
            if job is None or job["status"] in ("FINISHED", "CANCELLED"):
                return
            if job["status"] == "FAILED":
                raise RuntimeError(job.get("error") or f"job {job_id} failed")
            time.sleep(poll_interval)
        raise TimeoutError(f"job {job_id} did not finish within {timeout}s")

    def local_studio_ids_by_stashdb_id(self, endpoint=None):
        """{stashdb_id: local Stash id} for every studio Stash currently
        knows about, regardless of scene count -- used to match
        StashDB.org favorites against what actually exists locally (see
        PROGRESS.md's "StashDB.org favorites import" section). Not
        filtered by scene_count like studios_with_scenes(), since a
        studio can exist locally with 0 scenes and still be a valid
        favorite target."""
        data = self._graphql(FIND_STUDIOS_QUERY, {"filter": {"per_page": -1}})
        out = {}
        for s in data["findStudios"]["studios"]:
            stashdb_id = self._stash_id_for_endpoint(s["stash_ids"], endpoint or self.stashdb_endpoint)
            if stashdb_id:
                out[stashdb_id] = int(s["id"])
        return out

    def local_studio_lookup(self):
        """({stash-box studio id: local id}, {normalised name: local id}) over every local studio, any stash-box endpoint -- one request.
        Lets a release row link the SCENE's studio (not only the watched entity) to its Stash page; the name map is the fallback for
        studios without a matching stash id."""
        data = self._graphql(FIND_STUDIOS_QUERY, {"filter": {"per_page": -1}})
        by_id, by_name = {}, {}
        for s in data["findStudios"]["studios"]:
            for sid in s.get("stash_ids") or []:
                if sid.get("stash_id"):
                    by_id.setdefault(sid["stash_id"], int(s["id"]))
            key = "".join(ch for ch in (s.get("name") or "").lower() if ch.isalnum())
            if key:
                by_name.setdefault(key, int(s["id"]))
        return by_id, by_name

    def local_studio_parents(self):
        """({stash-box studio id: [parent names, nearest first]}, {normalised studio name: [parent names]}) from Stash's own studio
        hierarchy, one request. Release names often carry the PARENT studio ("Private.26.09.22..." for the sub-studio Anal
        Introductions), so the search tries those names too (v0.103.0)."""
        query = """query($filter: FindFilterType) { findStudios(filter: $filter) { studios { id name stash_ids { endpoint stash_id }
            parent_studio { name parent_studio { name parent_studio { name } } } } } }"""
        data = self._graphql(query, {"filter": {"per_page": -1}})
        by_id, by_name = {}, {}
        for s in data["findStudios"]["studios"]:
            chain, node = [], s.get("parent_studio")
            while node and len(chain) < 3:
                if node.get("name"):
                    chain.append(node["name"])
                node = node.get("parent_studio")
            if not chain:
                continue
            for sid in s.get("stash_ids") or []:
                if sid.get("stash_id"):
                    by_id.setdefault(sid["stash_id"], chain)
            key = "".join(ch for ch in (s.get("name") or "").lower() if ch.isalnum())
            if key:
                by_name.setdefault(key, chain)
        return by_id, by_name

    def local_performer_lookup(self):
        """({stash-box performer id: local id}, {normalised name: local id}), see local_studio_lookup()."""
        data = self._graphql(FIND_PERFORMERS_QUERY, {"filter": {"per_page": -1}})
        by_id, by_name = {}, {}
        for p in data["findPerformers"]["performers"]:
            for sid in p.get("stash_ids") or []:
                if sid.get("stash_id"):
                    by_id.setdefault(sid["stash_id"], int(p["id"]))
            key = "".join(ch for ch in (p.get("name") or "").lower() if ch.isalnum())
            if key:
                by_name.setdefault(key, int(p["id"]))
        return by_id, by_name

    def local_performer_ids_by_stashdb_id(self, endpoint=None):
        data = self._graphql(FIND_PERFORMERS_QUERY, {"filter": {"per_page": -1}})
        out = {}
        for p in data["findPerformers"]["performers"]:
            stashdb_id = self._stash_id_for_endpoint(p["stash_ids"], endpoint or self.stashdb_endpoint)
            if stashdb_id:
                out[stashdb_id] = int(p["id"])
        return out

    def endpoint_for_provider(self, provider):
        """Resolves this project's own provider name ('stashdb'/
        'theporndb') to the actual stash-box endpoint URL a local
        `stash_ids` entry is tagged with -- see search.py/routers/search.py,
        added 2026-09-19 for the plugin's own Search page."""
        return self.stashdb_endpoint if provider == "stashdb" else self.theporndb_endpoint

    def find_performer_by_stash_id(self, stashdb_id, endpoint):
        """Looks up a Performer already known to this Stash instance by
        its (provider, id) -- distinct from local_performer_ids_by_stashdb_id()
        above, which bulk-fetches every performer at once for the
        favorites-sync job; this is a single targeted lookup for the
        Search page's "favorite" action, which only ever needs one
        entity at a time. Returns None if not found locally yet."""
        data = self._graphql(
            "query($endpoint: String!, $stash_id: String!) { findPerformers(performer_filter: "
            "{stash_id_endpoint: {endpoint: $endpoint, stash_id: $stash_id, modifier: EQUALS}}) "
            "{ performers { id name favorite tags { id } } } }",
            {"endpoint": endpoint, "stash_id": stashdb_id},
        )
        results = data["findPerformers"]["performers"]
        return results[0] if results else None

    def find_studio_by_stash_id(self, stashdb_id, endpoint):
        data = self._graphql(
            "query($endpoint: String!, $stash_id: String!) { findStudios(studio_filter: "
            "{stash_id_endpoint: {endpoint: $endpoint, stash_id: $stash_id, modifier: EQUALS}}) "
            "{ studios { id name favorite tags { id } } } }",
            {"endpoint": endpoint, "stash_id": stashdb_id},
        )
        results = data["findStudios"]["studios"]
        return results[0] if results else None

    def find_studio_by_name(self, name):
        """A local studio with exactly this name (Stash allows one studio per name), with its provider ids, or None."""
        data = self._graphql(
            "query($n: String!) { findStudios(studio_filter: {name: {value: $n, modifier: EQUALS}}) "
            "{ studios { id name favorite tags { id } stash_ids { endpoint stash_id } } } }", {"n": name})
        results = data["findStudios"]["studios"]
        return results[0] if results else None

    def create_or_favorite_performer(self, stashdb_id, name, endpoint, watch_tag_id=None):
        """Backs the Search page's "Favorite" action for a Performer
        this Stash instance may or may not already know about --
        2026-09-19. If it already exists locally (matched by
        `stash_ids`), just flips its `favorite` flag; otherwise creates
        it fresh, tagged with the same `stash_ids` a normal Tagger
        "create" would leave, with `favorite: true` set in the same
        call (both fields are directly supported on `PerformerCreateInput`,
        confirmed live via introspection). Either way, the existing
        Favorite-driven watchlist sync (see sync.py) picks this up
        automatically before the next scan -- no separate wiring
        needed here.

        `watch_tag_id` (2026-09-26, 'tag' watchlist source): when given,
        adds that tag instead of setting Favorite -- the watchlist then
        follows the tag, Favorite stays untouched."""
        return self._create_or_watch("performer", stashdb_id, name, endpoint, watch_tag_id)

    def create_or_favorite_studio(self, stashdb_id, name, endpoint, watch_tag_id=None):
        return self._create_or_watch("studio", stashdb_id, name, endpoint, watch_tag_id)

    def _create_or_watch(self, entity_type, stashdb_id, name, endpoint, watch_tag_id):
        find = self.find_studio_by_stash_id if entity_type == "studio" else self.find_performer_by_stash_id
        existing = find(stashdb_id, endpoint)
        if existing:
            if watch_tag_id:
                if str(watch_tag_id) not in {t["id"] for t in existing.get("tags") or []}:
                    self.update_entity_tags(entity_type, [existing["id"]], [watch_tag_id], "ADD")
            elif not existing["favorite"]:
                update = "studioUpdate" if entity_type == "studio" else "performerUpdate"
                self._graphql(
                    f"mutation($id: ID!) {{ {update}(input: {{id: $id, favorite: true}}) {{ id }} }}",
                    {"id": existing["id"]},
                )
            return {"id": existing["id"], "name": existing["name"], "created": False}
        if entity_type == "studio" and name:
            # Stash allows ONE studio per name: creating a second one with the same name fails (HTTP 500 on the Search page before v0.240.18).
            # A studio of that name without this provider id is the same studio -- the id is added to it and it is watched.
            same = self.find_studio_by_name(name)
            if same:
                mine = [s for s in same.get("stash_ids") or [] if s["endpoint"] == endpoint]
                if mine and mine[0]["stash_id"] != stashdb_id:
                    raise ValueError(f"A studio named \"{name}\" already exists in Stash with another id at this provider "
                                     f"({mine[0]['stash_id'][:8]}…). Fix or remove that one first.")
                if not mine:
                    keep = [{"endpoint": s["endpoint"], "stash_id": s["stash_id"]} for s in same.get("stash_ids") or []]
                    self._graphql("mutation($i: StudioUpdateInput!) { studioUpdate(input: $i) { id } }",
                                  {"i": {"id": same["id"], "stash_ids": keep + [{"endpoint": endpoint, "stash_id": stashdb_id}]}})
                if watch_tag_id:
                    if str(watch_tag_id) not in {t["id"] for t in same.get("tags") or []}:
                        self.update_entity_tags("studio", [same["id"]], [watch_tag_id], "ADD")
                elif not same["favorite"]:
                    self._graphql("mutation($id: ID!) { studioUpdate(input: {id: $id, favorite: true}) { id } }", {"id": same["id"]})
                return {"id": same["id"], "name": same["name"], "created": False, "linked": not mine}
        create_input = {"name": name, "stash_ids": [{"endpoint": endpoint, "stash_id": stashdb_id}]}
        if watch_tag_id:
            create_input["tag_ids"] = [str(watch_tag_id)]
        else:
            create_input["favorite"] = True
        create, input_type = (("studioCreate", "StudioCreateInput") if entity_type == "studio"
                              else ("performerCreate", "PerformerCreateInput"))
        data = self._graphql(
            f"mutation($input: {input_type}!) {{ {create}(input: $input) {{ id }} }}", {"input": create_input},
        )
        return {"id": data[create]["id"], "name": name, "created": True}

    def set_favorite(self, entity_type, local_id, favorite):
        update = "studioUpdate" if entity_type == "studio" else "performerUpdate"
        self._graphql(
            f"mutation($id: ID!, $f: Boolean) {{ {update}(input: {{id: $id, favorite: $f}}) {{ id }} }}",
            {"id": str(local_id), "f": bool(favorite)},
        )

    def set_studios_favorite(self, local_ids, batch_size=25):
        return self._bulk_favorite(BULK_STUDIO_FAVORITE_MUTATION, local_ids, batch_size)

    def set_performers_favorite(self, local_ids, batch_size=25):
        return self._bulk_favorite(BULK_PERFORMER_FAVORITE_MUTATION, local_ids, batch_size)

    def _bulk_favorite(self, mutation, local_ids, batch_size):
        """Batched, not one giant mutation call. Confirmed live
        2026-09-11: Stash's own bulkPerformerUpdate is genuinely slow --
        measured ~0.75s per performer, roughly linear in a clean test
        (10 ids: 7.3s, 50: 35.7s, 200: 163.1s) -- a real cost in Stash's
        own server, not something wrong in this client or a network
        issue. Under actual concurrent load (this sidecar's own
        background loops running at the same time, repeated container
        restarts during development) even 50-id/90s-timeout batches
        still occasionally timed out -- Stash's per-request latency
        isn't perfectly stable, so batch_size=25 leaves more headroom,
        and each batch's own exception is now caught and logged rather
        than aborting every remaining batch: a single slow/failed batch
        no longer throws away all the batches after it, and the next
        scheduled/manual run picks up from wherever favorite flags
        still don't match (this call doesn't track that itself, but a
        bulk update on an already-favorite id is a harmless no-op *as
        far as Stash's own favorite flag is concerned*, so re-running
        is still a fine retry strategy for THIS call's own purpose --
        caller should still avoid calling this for ids that are already
        favorited when it can, though: confirmed live 2026-09-13 that
        the mutation fires Stash's `Performer.Update.Post` hook
        regardless of whether the value actually changed, and at least
        one installed plugin (stashdb performer gallery) reacts to that
        hook by queueing its own per-performer task -- see
        stashdb_favorites.py's import_stashdb_favorites() for the fix
        that filters already-favorited ids out before calling this).
        Returns (succeeded_batch_count, failed_batch_count)."""
        succeeded, failed = 0, 0
        for i in range(0, len(local_ids), batch_size):
            batch = local_ids[i:i + batch_size]
            try:
                self._graphql(mutation, {"input": {"ids": [str(x) for x in batch], "favorite": True}})
                succeeded += 1
            except Exception:
                logger.warning("bulk favorite batch failed (ids %s)", batch, exc_info=True)
                failed += 1
        return succeeded, failed

    def scene_has_file(self, stashdb_id):
        """True if a scene with this StashDB id currently exists in
        Stash with at least one file. Whisparr's own hasFile flag can be
        stale (no rescan since import) -- this is the authoritative
        check."""
        variables = {
            "scene_filter": {
                "stash_id_endpoint": {
                    "stash_id": stashdb_id, "endpoint": self.stashdb_endpoint, "modifier": "EQUALS",
                }
            }
        }
        data = self._graphql(FIND_SCENE_BY_STASH_ID_QUERY, variables)
        scenes = data["findScenes"]["scenes"]
        return bool(scenes and scenes[0]["files"])

    def library_heights_by_stash_ids(self, stashdb_ids):
        """Batched "already in library, at what resolution" check
        (2026-09-13, backs the backlog/releases "In Library" display
        and the "satisfied" quality comparison) -- one query for many
        StashDB scene ids at once instead of one round-trip per scene.
        Returns {stashdb_id: height} (raw pixel height, e.g. 1080) for
        every id that currently exists in Stash with at least one file;
        an id with no local file at all is simply absent from the
        result. Renamed from library_status_by_stash_ids() (which
        returned a formatted label like "1080p") once a caller needed
        the raw number to compare against a quality-profile cutoff --
        callers that just want a label can use quality_label(height).

        Deliberately Stash-only, no Whisparr lookup involved (dropped
        2026-09-13 after the user pointed out Stash's own file metadata
        already has the answer once a scene has actually landed in the
        library -- one connection instead of two, and consistent with
        this module's existing "Stash's own scene list is the source of
        truth" stance, see scene_has_file()'s docstring above).

        Verified live 2026-09-13: `stash_ids_endpoint`'s plural
        `StashIDsCriterionInput` only accepts `EQUALS` as a modifier
        here (`INCLUDES`/`INCLUDES_ALL` are rejected by Stash's own
        validation), but EQUALS with a list matches any scene whose
        stash_ids include *any* of the given ids -- confirmed against a
        real scene plus a deliberately-bogus id in the same call,
        returning only the real match. Also confirmed the real scene's
        file height (1080) matches what Whisparr itself reported for
        the same file ("WEBDL-1080p") before the Whisparr check was
        dropped."""
        if not stashdb_ids:
            return {}
        ids = list(stashdb_ids)
        variables = {
            "scene_filter": {
                "stash_ids_endpoint": {
                    "stash_ids": ids, "endpoint": self.stashdb_endpoint, "modifier": "EQUALS",
                }
            },
            "filter": {"per_page": len(ids)},
        }
        data = self._graphql(FIND_SCENES_BY_STASH_IDS_QUERY, variables)
        wanted = set(ids)
        result = {}
        for s in data["findScenes"]["scenes"]:
            files = s["files"]
            if not files:
                continue
            height = max((f.get("height") or 0) for f in files)
            for sid in s["stash_ids"]:
                if sid["endpoint"] == self.stashdb_endpoint and sid["stash_id"] in wanted:
                    result[sid["stash_id"]] = height
        missing = wanted - set(result)
        if missing and self.alias_lookup is not None:
            try:
                aliases = self.alias_lookup(list(missing))
                heights = self.heights_by_scene_ids(set(aliases.values())) if aliases else {}
                for alias_id, local_id in aliases.items():
                    if str(local_id) in heights:
                        result[alias_id] = heights[str(local_id)]
            except Exception:
                logger.exception("alias lookup failed (ignored)")
        return result

    def heights_by_scene_ids(self, local_scene_ids):
        """{local scene id (str): highest file height} for Stash scene ids (only scenes with a file)."""
        ids = [int(i) for i in local_scene_ids if str(i).isdigit()]
        if not ids:
            return {}
        data = self._graphql("query($ids: [Int!]) { findScenes(scene_ids: $ids, filter: {per_page: -1}) "
                             "{ scenes { id files { height } } } }", {"ids": ids})
        out = {}
        for s in data["findScenes"]["scenes"]:
            if s["files"]:
                out[str(s["id"])] = max((f.get("height") or 0) for f in s["files"])
        return out

    def scene_file_paths(self, scene_id):
        """Every file path of one local scene (2026-09-25) -- the
        Scene.Create.Post hook only hands over the new scene's id; see
        file_importer.verify_created_scene(). Empty list for an unknown id."""
        data = self._graphql(SCENE_FILE_PATHS_QUERY, {"id": str(scene_id)})
        scene = data.get("findScene")
        return [f["path"] for f in (scene or {}).get("files") or []]

    def find_scene_by_path(self, path):
        """Exact-path lookup (2026-09-14, extended 2026-09-15 to also
        carry each file's own fingerprints) -- backs the download
        verification check: Whisparr can report `hasFile: true` for a
        scene while that exact file, once scanned into Stash, actually
        turns out to be the wrong content entirely (see PROGRESS.md's
        "Boosting Morale"/"Summer Camp Rules" investigation, and
        verification.py's verify_downloaded_file() for the fingerprint-
        based check this now feeds). Returns the found scene's own
        `{id, title, stash_ids, files}` dict (each file carrying its own
        `path`/`fingerprints`, since a scene can have more than one
        file -- see verify_downloaded_file() for why matching the exact
        path among them matters, not just using the scene's first/any
        file), or None if Stash hasn't scanned this exact path at all
        yet (still just "awaiting rescan", not a mismatch).

        `modifier: EQUALS` on a single path string is a literal exact
        match, confirmed live -- unlike `INCLUDES`, which tokenizes and
        matches on any word (a bare `INCLUDES` search for a full path
        returned 263 unrelated scenes sharing common words)."""
        variables = {"scene_filter": {"path": {"value": path, "modifier": "EQUALS"}}}
        data = self._graphql(FIND_SCENE_BY_PATH_QUERY, variables)
        scenes = data["findScenes"]["scenes"]
        return scenes[0] if scenes else None

    # --- Upgrade replacement (2026-09-26, upgrades.on_upgrade_imported()) ---

    def scene_files(self, scene_id):
        data = self._graphql("query($id: ID!) { findScene(id: $id) { id files { id path } } }", {"id": str(scene_id)})
        scene = data["findScene"]
        return scene["files"] if scene else None

    def merge_scene_into(self, source_scene_id, destination_scene_id):
        """Stash's own scene merge: the source's files move to the
        destination, the destination keeps its metadata, play and O history
        are carried over, the source scene is deleted."""
        self._graphql(
            "mutation($i: SceneMergeInput!) { sceneMerge(input: $i) { id } }",
            {"i": {"source": [str(source_scene_id)], "destination": str(destination_scene_id),
                   "play_history": True, "o_history": True}})

    def set_primary_file(self, scene_id, file_id):
        self._graphql("mutation($i: SceneUpdateInput!) { sceneUpdate(input: $i) { id } }",
                      {"i": {"id": str(scene_id), "primary_file_id": str(file_id)}})

    def clean_paths(self, paths):
        """Stash's Clean task limited to `paths`: drops file entries whose
        file no longer exists (after it went to the recycle bin). Returns
        the job id."""
        data = self._graphql("mutation($i: CleanMetadataInput!) { metadataClean(input: $i) }",
                             {"i": {"paths": list(paths), "dryRun": False}})
        return data["metadataClean"]

    def destroy_scene(self, scene_id, delete_file=False, delete_generated=True):
        """Removes a scene's own Stash DB record -- `delete_file=False`
        by default (2026-09-23, added for the "Fix wrong download"
        retry action, see file_importer.py/handoff.py): a wrongly-
        matched import gets its Stash scene record deleted here, then
        its actual video file moved to the recycle bin separately (the
        same `_recycle_existing_target()` mechanism used elsewhere),
        rather than a hard `delete_file=True` -- reversible, matching
        why the recycle bin exists at all. `delete_generated=True`
        still cleans up thumbnails/sprites/previews Stash generated for
        the wrong scene, which aren't worth keeping either way."""
        self._graphql(SCENE_DESTROY_MUTATION, {
            "id": scene_id, "delete_file": delete_file, "delete_generated": delete_generated,
        })

    def count_scenes_meeting_resolution(self, local_entity_type, local_id, min_height):
        """Counts this LOCAL Studio/Performer's own scenes (identified
        by their local Stash id, not a StashDB one) that carry a
        StashDB stash_id AND have at least one file at or above
        `min_height` -- backs the "Missing from Catalog" completeness
        numerator (2026-09-13): how many of the entity's owned scenes
        are both sourced from StashDB and already at the desired
        quality.

        Two-step (a cheap count-only call, then one real fetch at
        exactly that per_page) rather than guessing a large per_page
        up front, since an entity's local library size isn't known in
        advance -- mirrors library_heights_by_stash_ids()'s per-scene
        approach, just scoped by local entity id instead of a list of
        StashDB scene ids, since there's no live-catalog scene-id list
        to check against here (that's the whole point of the
        count-based design -- see stashdb_client.py's
        count_scenes_by_year() docstring for the matching StashDB-side
        reasoning).

        Counts unique StashDB scene ids meeting the resolution, not raw
        local Scene rows -- found live 2026-09-13 (user reported the
        completeness ratio didn't add up, 152 in_library + 13 missing
        != 160 total) that the same real StashDB scene can exist as
        *more than one* local Stash scene (5 genuine duplicates on one
        studio, e.g. "Schoolgirl Therapy" imported once at 2160p and
        again at 1080p, both kept as separate library entries) --
        counting raw rows overcounted by exactly that many (152 vs the
        correct 147, 147 + 13 = 160). A scene existing at *any*
        qualifying local copy is enough to count it once, even if
        another duplicate copy is below the target quality."""
        filter_field = "studios" if local_entity_type == "studio" else "performers"
        scene_filter = {
            filter_field: {"value": [str(local_id)], "modifier": "INCLUDES"},
            "stash_id_endpoint": {"endpoint": endpoint or self.stashdb_endpoint, "modifier": "NOT_NULL"},
        }
        count_data = self._graphql(FIND_SCENES_BY_STASH_IDS_QUERY, {"scene_filter": scene_filter, "filter": {"per_page": 1}})
        total = count_data["findScenes"]["count"]
        if total == 0:
            return 0
        data = self._graphql(FIND_SCENES_BY_STASH_IDS_QUERY, {"scene_filter": scene_filter, "filter": {"per_page": total}})
        scenes = data["findScenes"]["scenes"]
        qualifying_ids = set()
        for s in scenes:
            if not any((f.get("height") or 0) >= min_height for f in s["files"]):
                continue
            for sid in s["stash_ids"]:
                if sid["endpoint"] == self.stashdb_endpoint:
                    qualifying_ids.add(sid["stash_id"])
        return len(qualifying_ids)

    def scene_for_fingerprint_check(self, scene_id):
        """One scene in the shape the library check works with (id, title, stash_ids, files with fingerprints), or None when Stash has none (v0.238.0)."""
        data = self._graphql(
            "query($ids: [Int!]) { findScenes(scene_ids: $ids) { scenes { id title stash_ids { endpoint stash_id } "
            "files { path fingerprints { type value } } } } }", {"ids": [int(scene_id)]})
        scenes = data["findScenes"]["scenes"]
        return scenes[0] if scenes else None

    def all_scenes_with_stashdb_id(self, endpoint=None):
        """Every local scene carrying a StashDB stash_id, with its
        files' own fingerprints included -- feeds
        scripts/audit_library_fingerprints.py's retroactive check of
        whether a scene's file(s) actually match what StashDB has
        registered for that scene id (see stashdb_client.py's
        find_scenes_by_fingerprints()).

        Same count-then-fetch-at-exact-per_page approach as
        count_scenes_meeting_resolution() -- confirmed live 2026-09-15
        there are 10,787 such scenes, so this is one bulk local
        GraphQL call, not paginated (unlike the batched StashDB-side
        reverse lookup, which StashDB itself caps -- this is Stash's
        own DB on the same box, not a rate-limited external API)."""
        scene_filter = {
            "stash_id_endpoint": {"endpoint": self.stashdb_endpoint, "modifier": "NOT_NULL"},
        }
        count_data = self._graphql(FIND_SCENES_WITH_STASHDB_ID_QUERY, {"scene_filter": scene_filter, "filter": {"per_page": 1}})
        total = count_data["findScenes"]["count"]
        if total == 0:
            return []
        data = self._graphql(FIND_SCENES_WITH_STASHDB_ID_QUERY, {"scene_filter": scene_filter, "filter": {"per_page": total}})
        return data["findScenes"]["scenes"]

    def scrape_scene_by_stashdb_id(self, stashdb_id, endpoint=None):
        """Full ScrapedScene metadata for this exact StashDB scene id,
        via Stash's own stash-box scraper (same one the "Scrape
        with..." UI action and Tagger use) -- title/date/details plus
        studio/performers/tags already resolved to LOCAL stored_id
        where Stash recognizes them by their own stash_ids (None where
        it doesn't -- see fix_library_mismatches.py for why those
        fields are then left alone rather than auto-created).

        Returns None if StashDB has no such scene (shouldn't happen
        for an id that just came out of find_scenes_by_fingerprints(),
        but scrape results are still an external, unverified source --
        confirms `remote_site_id` matches what was asked for before
        the caller trusts anything else in the result)."""
        # `endpoint` (2026-09-25): any configured stash-box, e.g.
        # ThePornDB's -- confirmed live its scrape resolves a TPDB scene
        # uuid the same way (remote_site_id == the uuid). Default StashDB.
        variables = {
            "source": {"stash_box_endpoint": endpoint or self.stashdb_endpoint},
            "input": {"query": stashdb_id},
        }
        data = self._graphql(SCRAPE_SCENE_BY_STASHDB_ID_QUERY, variables)
        results = data["scrapeSingleScene"] or []
        for r in results:
            if r.get("remote_site_id") == stashdb_id:
                return r
        return None

    def local_scene_ids_by_stash_ids(self, pairs, chunk=30):
        """{(provider, stash id): local scene id} for [(provider, stash id)] -- ONE request to the local Stash per `chunk` ids (aliased
        findScenes). 'stashdb' ids also count through the alias table. Backs the "Open scene" buttons (v0.128.5)."""
        import json as _json
        endpoints = {"stashdb": self.stashdb_endpoint, "theporndb": self.theporndb_endpoint}
        out, pairs = {}, [p for p in pairs if p[0] in endpoints]
        for i in range(0, len(pairs), chunk):
            part = pairs[i:i + chunk]
            fields = "\n".join(
                f's{n}: findScenes(scene_filter: {{stash_id_endpoint: {{endpoint: {_json.dumps(endpoints[prov])}, stash_id: {_json.dumps(sid)}, modifier: EQUALS}}}}, '
                f'filter: {{per_page: 1}}) {{ scenes {{ id }} }}' for n, (prov, sid) in enumerate(part))
            data = self._graphql("query {\n" + fields + "\n}", {})
            for n, pair in enumerate(part):
                found = (data.get(f"s{n}") or {}).get("scenes") or []
                if found:
                    out[pair] = str(found[0]["id"])
        if self.alias_lookup is not None:
            missing = [sid for prov, sid in pairs if prov == "stashdb" and (prov, sid) not in out]
            if missing:
                try:
                    for sid, local in self.alias_lookup(missing).items():
                        out[("stashdb", sid)] = str(local)
                except Exception:
                    logger.exception("alias lookup failed (ignored)")
        return out

    def find_scene_by_stashdb_id(self, stashdb_id, endpoint=None):
        """The local scene (if any) already carrying this exact
        StashDB stash_id -- pre-flight check before retagging a
        mismatched scene onto it, since Stash's schema only enforces
        UNIQUE(scene_id, endpoint) per LOCAL scene, not per stash_id
        value (confirmed live 2026-09-15/16) -- nothing stops two
        different local scenes from both claiming the same external
        id unless this is checked explicitly first."""
        data = self._graphql(FIND_SCENE_BY_STASHDB_ID_QUERY,
                             {"stashdb_id": stashdb_id, "endpoint": endpoint or self.stashdb_endpoint})
        scenes = data["findScenes"]["scenes"]
        if scenes:
            return scenes[0]
        if self.alias_lookup is not None and (endpoint or self.stashdb_endpoint) == self.stashdb_endpoint:
            try:
                local_id = self.alias_lookup([stashdb_id]).get(stashdb_id)
            except Exception:
                logger.exception("alias lookup failed (ignored)")
                local_id = None
            if local_id:
                # the video is owned under another title (alias): report that scene as the holder
                return {"id": str(local_id), "title": f"(alias: same video as local scene {local_id})"}
        return None

    def create_performer_from_scraped(self, scraped_performer):
        """Creates a local performer from a `scrapeSingleScene`
        `ScrapedPerformer` entry (see SCRAPE_SCENE_BY_STASHDB_ID_QUERY,
        widened 2026-09-19 to request the full field set below rather
        than just `stored_id name`) -- backs `retag_scene()`'s optional
        `create_missing_performers`, added the same day per the user's
        own explicit request ("ich hätte gerne dass die Performer gemäß
        StashDB.org Eintrag angelegt werden, wenn sie lokal noch nicht
        vorhanden sind") after noticing a freshly-tagged scene silently
        dropped a performer StashDB knows about but Stash didn't yet.
        Deliberately does NOT set `favorite` (unlike
        create_or_favorite_performer(), the Search page's own "Favorite"
        action) -- a performer pulled in as a side effect of tagging one
        scene shouldn't silently join the watchlist favorites, a
        different and much bigger commitment. `height` arrives as a
        bare string (StashDB's own convention, already centimeters) --
        converted to `height_cm` best-effort, dropped if it doesn't
        parse rather than failing the whole create. `aliases` arrives
        as one comma-joined string, split into `alias_list`. `image`
        takes the first of `images` (Stash's create input only takes
        one)."""
        name = scraped_performer.get("name")
        height_cm = None
        try:
            if scraped_performer.get("height"):
                height_cm = int(scraped_performer["height"])
        except (TypeError, ValueError):
            pass
        aliases = scraped_performer.get("aliases")
        images = scraped_performer.get("images") or []
        input_fields = {
            "name": name,
            "gender": scraped_performer.get("gender"),
            "birthdate": scraped_performer.get("birthdate"),
            "ethnicity": scraped_performer.get("ethnicity"),
            "country": scraped_performer.get("country"),
            "eye_color": scraped_performer.get("eye_color"),
            "height_cm": height_cm,
            "measurements": scraped_performer.get("measurements"),
            "fake_tits": scraped_performer.get("fake_tits"),
            "penis_length": scraped_performer.get("penis_length"),
            "circumcised": scraped_performer.get("circumcised"),
            "career_start": scraped_performer.get("career_start"),
            "career_end": scraped_performer.get("career_end"),
            "tattoos": scraped_performer.get("tattoos"),
            "piercings": scraped_performer.get("piercings"),
            "hair_color": scraped_performer.get("hair_color"),
            "death_date": scraped_performer.get("death_date"),
            "alias_list": [a.strip() for a in aliases.split(",") if a.strip()] if aliases else [],
            "image": images[0] if images else None,
            "stash_ids": [{"endpoint": self.stashdb_endpoint, "stash_id": scraped_performer.get("remote_site_id")}],
        }
        variables = {"input": {k: v for k, v in input_fields.items() if v is not None}}
        data = self._graphql(
            "mutation($input: PerformerCreateInput!) { performerCreate(input: $input) { id } }",
            variables,
        )
        return data["performerCreate"]["id"]

    def retag_scene(self, scene_id, scraped, stashdb_id, create_missing_performers=False, endpoint=None,
                    keep_other_endpoints=False):
        """Applies a scrape_scene_by_stashdb_id() result to a LOCAL
        scene that audit_library_fingerprints.py found mismatched --
        full retag (title/date/details/stash_ids), not just an id
        overlay, since the scene's whole identity was wrong, not just
        missing an id (unlike theporndb_identify.py's deliberately
        ID-only tagging of already-correct scenes). `stash_ids` is
        REPLACED outright (Stash has no "add one id" mutation, see
        FIND_SCENE_BY_PATH_QUERY-adjacent research from the original
        Boosting Morale investigation) -- fine here since a mismatched
        scene's existing stash_id was wrong anyway, nothing worth
        keeping alongside it.

        studio_id/tag_ids are only ever set from a scrape entry that
        already carries a local `stored_id` -- entities StashDB knows
        about but Stash doesn't yet recognize locally are left alone
        rather than silently auto-created, same conservative boundary
        this project already draws elsewhere for stash-box tagging.
        `performer_ids` follows the same rule UNLESS
        `create_missing_performers=True` (file_importer.py's fresh-
        import tagging path opts in; the Library Check "Fix mismatch"
        maintenance tool does not, by default, preserving its existing
        conservative behavior for a flow that can touch many scenes at
        once) -- a scrape entry missing `stored_id` then gets created
        fresh via create_performer_from_scraped() instead of being
        dropped. Best-effort per performer: one creation failing (a
        malformed field, a transient error) logs and skips that
        performer rather than failing the whole retag."""
        update = {
            "id": scene_id,
            "title": scraped.get("title"),
            "date": scraped.get("date"),
            "details": scraped.get("details"),
            "stash_ids": [{"endpoint": endpoint or self.stashdb_endpoint, "stash_id": stashdb_id}],
        }
        if keep_other_endpoints:
            # Fresh imports (2026-09-28, found live: "Bendy Mistress" had its
            # ThePornDB id replaced by the StashDB one when the StashDB copy
            # was verified): ids of OTHER stash-boxes stay, only this
            # endpoint's id is (re)set.
            update["stash_ids"] = self._merged_stash_ids(scene_id, endpoint or self.stashdb_endpoint, stashdb_id)
        studio = scraped.get("studio")
        if studio and studio.get("stored_id"):
            update["studio_id"] = studio["stored_id"]
        elif studio and studio.get("name"):
            # Name fallback (2026-09-25, found live: ThePornDB's
            # "IntimatePOV" vs the local "Intimate POV", which only
            # carried a StashDB id -- Stash's scraper links a studio
            # solely by stash-box id, so stored_id came back empty and the
            # imported scene got no studio). Only a single, unambiguous
            # name/alias match is used.
            studio_id = self.find_studio_id_by_name(studio["name"])
            if studio_id:
                update["studio_id"] = studio_id
                # Teach the local studio this stash-box's id too, so
                # Stash links it by itself from now on (scraper,
                # Tagger, ThePornDB identify -- whose own name search
                # can't bridge "Intimate POV" vs "IntimatePOV" either).
                # Added, never replacing the studio's existing ids.
                if studio.get("remote_site_id"):
                    try:
                        self.add_studio_stash_id(studio_id, endpoint or self.stashdb_endpoint,
                                                 studio["remote_site_id"])
                    except Exception:
                        logger.exception("retag_scene: couldn't add stash-box id to studio %s", studio_id)
        performer_ids = [p["stored_id"] for p in (scraped.get("performers") or []) if p.get("stored_id")]
        if create_missing_performers:
            for p in (scraped.get("performers") or []):
                if p.get("stored_id"):
                    continue
                try:
                    performer_ids.append(self.create_performer_from_scraped(p))
                except Exception:
                    logger.exception("retag_scene: failed to auto-create missing performer %r", p.get("name"))
        if performer_ids:
            update["performer_ids"] = performer_ids
        tag_ids = [t["stored_id"] for t in (scraped.get("tags") or []) if t.get("stored_id")]
        if tag_ids:
            update["tag_ids"] = tag_ids
        data = self._graphql(SCENE_UPDATE_MUTATION, {"input": update})
        return data["sceneUpdate"]

    def _merged_stash_ids(self, scene_id, endpoint, stash_id):
        data = self._graphql("query($id: ID!) { findScene(id: $id) { stash_ids { endpoint stash_id } } }",
                             {"id": str(scene_id)})
        existing = (data.get("findScene") or {}).get("stash_ids") or []
        merged = [{"endpoint": s["endpoint"], "stash_id": s["stash_id"]} for s in existing if s["endpoint"] != endpoint]
        merged.append({"endpoint": endpoint, "stash_id": stash_id})
        return merged

    def add_scene_stash_id(self, scene_id, endpoint, stash_id):
        """Adds (or replaces) this endpoint's id on a scene, keeping the ids
        of every other stash-box (2026-09-28: a scene already imported via
        ThePornDB gets its new StashDB id too)."""
        self._graphql("mutation($i: SceneUpdateInput!) { sceneUpdate(input: $i) { id } }",
                      {"i": {"id": str(scene_id), "stash_ids": self._merged_stash_ids(scene_id, endpoint, stash_id)}})

    def stash_ids_in_library(self, endpoint, stash_ids):
        """Which of these provider scene ids are in Stash with at least one file? Returns {provider id: local Stash scene id} (v0.207.2,
        the Search page links to the scene). One query per 100 ids. Same EQUALS-with-a-list filter as library_heights_by_stash_ids(),
        but for any stash-box endpoint."""
        wanted = [str(i) for i in stash_ids if i]
        found = {}
        for start in range(0, len(wanted), 100):
            chunk = wanted[start:start + 100]
            data = self._graphql(FIND_SCENES_BY_STASH_IDS_QUERY, {
                "scene_filter": {"stash_ids_endpoint": {"stash_ids": chunk, "endpoint": endpoint, "modifier": "EQUALS"}},
                "filter": {"per_page": len(chunk)},
            })
            for sc in data["findScenes"]["scenes"]:
                if not sc["files"]:
                    continue
                for sid in sc["stash_ids"]:
                    if sid["endpoint"] == endpoint and sid["stash_id"] in chunk:
                        found.setdefault(sid["stash_id"], sc["id"])
        return found

    def find_scene_ids_by_stash_id(self, endpoint, stash_id):
        data = self._graphql(
            "query($f: SceneFilterType) { findScenes(scene_filter: $f, filter: {per_page: 5}) { scenes { id } } }",
            {"f": {"stash_id_endpoint": {"endpoint": endpoint, "stash_id": stash_id, "modifier": "EQUALS"}}})
        return [s["id"] for s in data["findScenes"]["scenes"]]

    def add_studio_stash_id(self, studio_id, endpoint, stash_id):
        """Appends {endpoint, stash_id} to a local studio's stash_ids
        unless it already has an id for that endpoint (2026-09-25).
        Returns True if it added one."""
        data = self._graphql(STUDIO_STASH_IDS_QUERY, {"id": str(studio_id)})
        existing = (data.get("findStudio") or {}).get("stash_ids") or []
        if any(s["endpoint"] == endpoint for s in existing):
            return False
        stash_ids = [{"endpoint": s["endpoint"], "stash_id": s["stash_id"]} for s in existing]
        stash_ids.append({"endpoint": endpoint, "stash_id": stash_id})
        self._graphql(STUDIO_SET_STASH_IDS_MUTATION, {"input": {"id": str(studio_id), "stash_ids": stash_ids}})
        logger.info("linked local studio %s to %s id %s", studio_id, endpoint, stash_id)
        return True

    def studio_stash_ids(self, studio_id):
        data = self._graphql(STUDIO_STASH_IDS_QUERY, {"id": str(studio_id)})
        return (data.get("findStudio") or {}).get("stash_ids") or []

    def find_studio_id_by_name(self, name):
        """Local studio whose name or one of its aliases equals `name`
        ignoring case, spaces and punctuation ("IntimatePOV" == "Intimate
        POV"), or None when there's no match OR more than one (ambiguous
        -- better no studio than a wrong one). Uses Stash's own
        MATCHES_REGEX filter so it doesn't have to list every studio."""
        chars = [c for c in (name or "").lower() if c.isalnum()]
        if not chars:
            return None
        sep = "[^\\p{L}\\p{N}]*"
        pattern = "(?i)^" + sep + sep.join(re.escape(c) for c in chars) + sep + "$"
        ids = set()
        for field in ("name", "aliases"):
            data = self._graphql(FIND_STUDIOS_BY_REGEX_QUERY, {
                "filter": {field: {"value": pattern, "modifier": "MATCHES_REGEX"}},
            })
            ids.update(s["id"] for s in data["findStudios"]["studios"])
        return ids.pop() if len(ids) == 1 else None

    def list_library_paths(self):
        """Stash's own configured top-level library directories --
        shown as quick-jump shortcuts above the Maintenance page's
        directory browser (see browse_directory() below), the same
        starting points Stash's native Scan/Clean/Generate task
        dialogs offer. Order as Stash itself returns it (not sorted)."""
        data = self._graphql(LIST_LIBRARY_PATHS_QUERY, {})
        return [s["path"] for s in data["configuration"]["general"]["stashes"]]

    def library_config(self):
        """The full library list with its flags: [{path, excludeVideo, excludeImage}]."""
        data = self._graphql("query { configuration { general { stashes { path excludeVideo excludeImage } } } }", {})
        return [dict(s) for s in data["configuration"]["general"]["stashes"]]

    def set_library_config(self, stashes):
        """Replaces the WHOLE library list (configureGeneral takes the complete list) and returns
        what Stash reports afterwards. Callers pass library_config() with entries removed/added."""
        self._graphql("mutation($i: ConfigGeneralInput!) { configureGeneral(input: $i) { stashes { path } } }",
                      {"i": {"stashes": [{"path": s["path"], "excludeVideo": bool(s.get("excludeVideo")),
                                          "excludeImage": bool(s.get("excludeImage"))} for s in stashes]}})
        return self.library_config()

    def browse_directory(self, path=None):
        """One level of Stash's own server-side filesystem browser --
        exactly the mechanism behind its native "Select folders"
        dialog (Scan/Clean/Generate's "Selective..." buttons): pass
        `path`, get that directory's own immediate subdirectories back
        (`path=None` uses Stash's own default starting point, confirmed
        live 2026-09-16 to be the server process's home directory).
        Backs the Maintenance page's directory picker, replacing an
        earlier flat "all 7 configured top-level paths, checked by
        default" checkbox design that couldn't select a subdirectory
        at all -- confirmed live via GraphQL introspection that Stash
        exposes this exact query (`directory(path: String) { path
        parent directories }`) for its own dialog to use, so the new
        picker can browse/add paths the identical way.

        Returns `{"path", "parent", "directories"}` -- `parent` is
        None only at the filesystem root (no "Up a directory" then);
        `directories` are full absolute child paths, not bare names."""
        data = self._graphql(BROWSE_DIRECTORY_QUERY, {"path": path})
        return data["directory"]
