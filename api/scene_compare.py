"""Compare scenes (2026-09-30): two Stash scenes side by side -- both playable in the browser -- to decide
visually which one carries the right provider id. Backs Maintenance > Compare scenes.

Pairs live in `compare_pair` (db.py). Reading a pair pulls both scenes from Stash and, per provider id, the
provider's own scene (title/date/studio/performers; cached for the life of the process -- one request per
distinct id, not per view). The only write is removing ONE provider id from ONE scene: the whole scene
(ids, metadata, tags, markers) is appended to <data_dir>/compare_removed_ids.jsonl first and a History entry
is written, so it can be put back.
"""
import film_names
import base64
import json
import os
import subprocess
import threading
import time

import requests

import history
import library_check
import scene_alias
import library_import
import vr_check

STATUSES = ("pending", "done", "skipped")

SCENE_QUERY = """query($id: ID!) { findScene(id: $id) {
  id title code date details director urls rating100 play_count o_counter created_at
  studio { id name } performers { id name } tags { id name } groups { group { name } scene_index }
  scene_markers { seconds primary_tag { name } }
  stash_ids { endpoint stash_id }
  files { id path size width height duration bit_rate video_codec frame_rate fingerprints { type value } }
} }"""

UPDATE = "mutation($i: SceneUpdateInput!) { sceneUpdate(input: $i) { id } }"


def norm_id(value):
    """Scene id as typed by a person: '#23297 ' -> '23297'."""
    return str(value or "").strip().lstrip("#").strip()


def list_pairs(db, status=None):
    with db.connect() as conn:
        sql, args = "SELECT * FROM compare_pair", ()
        if status in STATUSES:
            sql, args = sql + " WHERE status = ?", (status,)
        return [dict(r) for r in conn.execute(sql + " ORDER BY id", args).fetchall()]


def get_pair(db, pair_id):
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM compare_pair WHERE id = ?", (pair_id,)).fetchone()
        return dict(row) if row else None


def add_pairs(db, pairs):
    """pairs = [{"a", "b", "label"}]; existing (a, b) or (b, a) pairs are kept untouched. Returns the number added."""
    added = 0
    with db.connect() as conn:
        for p in pairs:
            a, b = norm_id(p["a"]), norm_id(p["b"])
            if not a or not b or a == b:
                continue
            if conn.execute("SELECT 1 FROM compare_pair WHERE (scene_a = ? AND scene_b = ?) OR (scene_a = ? AND scene_b = ?)",
                            (a, b, b, a)).fetchone():
                continue
            conn.execute("INSERT INTO compare_pair (scene_a, scene_b, label) VALUES (?, ?, ?)", (a, b, p.get("label")))
            added += 1
    return added


def review_marks(db):
    with db.connect() as conn:
        return {r["scene_id"]: dict(r) for r in conn.execute("SELECT * FROM review_mark").fetchall()}


def set_review_mark(db, scene_id, later, note=None):
    """later=True: 'unresolved/later' (hidden from the default Open view); False: back to open."""
    with db.connect() as conn:
        if later:
            conn.execute("INSERT INTO review_mark (scene_id, status, note) VALUES (?, 'later', ?) "
                         "ON CONFLICT(scene_id) DO UPDATE SET note = COALESCE(excluded.note, note), marked_at = datetime('now')",
                         (norm_id(scene_id), note))
        else:
            conn.execute("DELETE FROM review_mark WHERE scene_id = ?", (norm_id(scene_id),))


def set_status(db, pair_id, status, note=None):
    if status not in STATUSES:
        raise ValueError("status must be one of " + ", ".join(STATUSES))
    with db.connect() as conn:
        conn.execute("UPDATE compare_pair SET status = ?, note = COALESCE(?, note), "
                     "resolved_at = CASE WHEN ? = 'pending' THEN NULL ELSE datetime('now') END WHERE id = ?",
                     (status, note, status, pair_id))


def delete_pair(db, pair_id):
    with db.connect() as conn:
        conn.execute("DELETE FROM compare_pair WHERE id = ?", (pair_id,))


_FP_LOCK = threading.Lock()  # the check / apply of the fingerprint results (table fingerprint_check) run one at a time


def fp_rows(db):
    """{scene_id: {state, matches, held_by, note, checked_at, shared_with?}} -- the stored dry-run results of the fingerprint check."""
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM fingerprint_check").fetchall()
    out = {r["scene_id"]: {"state": r["state"], "matches": json.loads(r["matches"] or "[]"), "held_by": r["held_by"], "note": r["note"],
                           "checked_at": r["checked_at"]} for r in rows}
    wanted = {}
    for sid, e in out.items():
        if e["state"] in ("unique", "shared") and e["matches"]:
            wanted.setdefault(e["matches"][0]["id"], []).append(sid)
    for ids in wanted.values():  # the same StashDB scene wanted by two local scenes (copies, parts) is not clear for either
        for sid in ids:
            out[sid]["state"] = "shared" if len(ids) > 1 else "unique"
            out[sid]["shared_with"] = [x for x in ids if x != sid]
    return out


def unid_store(db, rows, in_review):
    """Keeps the list of Unidentified scenes (v0.241.6): it takes a Stash request over every scene without an id (seconds), so it is read once and
    kept until a change takes an entry out (unid_drop), something may have added one (unid_invalidate) or the user reloads."""
    with db.connect() as conn:
        conn.execute("DELETE FROM unidentified_scene")
        conn.executemany("INSERT INTO unidentified_scene (scene_id, payload) VALUES (?, ?)", [(r["id"], json.dumps(r)) for r in rows])
        conn.execute("INSERT INTO maintenance_state (key, value) VALUES ('unidentified_at', datetime('now')) ON CONFLICT(key) DO UPDATE SET value = excluded.value")
        conn.execute("INSERT INTO maintenance_state (key, value) VALUES ('unidentified_in_review', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                     (str(in_review),))


def unid_load(db):
    """(rows, in_review, at) of the kept list, or None when there is none (never listed, or invalidated)."""
    with db.connect() as conn:
        at = conn.execute("SELECT value FROM maintenance_state WHERE key = 'unidentified_at'").fetchone()
        if not at:
            return None
        n = conn.execute("SELECT value FROM maintenance_state WHERE key = 'unidentified_in_review'").fetchone()
        rows = [json.loads(r["payload"]) for r in conn.execute("SELECT payload FROM unidentified_scene").fetchall()]
    rows.sort(key=lambda r: int(r["id"]) if str(r["id"]).isdigit() else 0)
    return rows, int(n["value"]) if n else 0, at["value"]


def unid_drop(db, scene_ids, to_review=False):
    """Takes scenes out of the kept list (identified, sent to Review); `to_review` counts them as "already in Review"."""
    with db.connect() as conn:
        for sid in scene_ids:
            gone = conn.execute("DELETE FROM unidentified_scene WHERE scene_id = ?", (str(sid),)).rowcount
            if gone and to_review:
                conn.execute("UPDATE maintenance_state SET value = CAST(COALESCE(value, '0') AS INTEGER) + 1 WHERE key = 'unidentified_in_review'")


def unid_invalidate(db):
    """The kept list may be wrong now (a scene lost its id or its Review tag): the next reading makes it anew."""
    with db.connect() as conn:
        conn.execute("DELETE FROM maintenance_state WHERE key = 'unidentified_at'")


def fp_put(db, scene_id, entry):
    with db.connect() as conn:
        conn.execute("INSERT OR REPLACE INTO fingerprint_check (scene_id, state, matches, held_by, note, checked_at) VALUES (?, ?, ?, ?, ?, datetime('now'))",
                     (str(scene_id), entry["state"], json.dumps(entry["matches"], ensure_ascii=False), entry.get("held_by"), entry.get("note")))


def fp_delete(db, scene_id):
    with db.connect() as conn:
        conn.execute("DELETE FROM fingerprint_check WHERE scene_id = ?", (str(scene_id),))


class SceneCompare:
    def __init__(self, db, get_stash_client, get_provider_client, data_dir, get_importer=None, to_local=None):
        self.db = db
        self.get_stash = get_stash_client
        self.get_provider = get_provider_client
        self.get_importer = get_importer
        self.to_local = to_local
        self.data_dir = data_dir
        self._provider_cache = {}

    # ---------- reading ----------

    @staticmethod
    def _provider_key(stash, endpoint):
        if endpoint == stash.stashdb_endpoint:
            return "stashdb"
        if endpoint == stash.theporndb_endpoint:
            return "theporndb"
        return None

    @staticmethod
    def _provider_url(provider, pid):
        return {"stashdb": f"https://stashdb.org/scenes/{pid}",
                "theporndb": f"https://theporndb.net/scenes/{pid}"}.get(provider)

    def _provider_scene(self, provider, pid):
        key = (provider, pid)
        if key not in self._provider_cache:
            try:
                client = self.get_provider(provider)
                d = client.get_scene_detail(pid) if client else None
                self._provider_cache[key] = None if not d else {
                    "title": d.get("title"), "date": d.get("date") or d.get("release_date"),
                    "studio": (d.get("studio") or {}).get("name"),
                    "performers": [(p.get("performer") or p).get("name") for p in (d.get("performers") or [])],
                    "details": (d.get("details") or "")[:400],
                }
            except Exception as e:
                return {"error": f"{type(e).__name__}: {e}"}
        return self._provider_cache[key]

    def scene(self, scene_id, with_provider=True):
        stash = self.get_stash()
        scene_id = norm_id(scene_id)
        if not scene_id.isdigit():
            return {"id": scene_id, "missing": True}
        sc = stash._graphql(SCENE_QUERY, {"id": scene_id})["findScene"]
        if not sc:
            return {"id": scene_id, "missing": True}
        files = []
        for f in sc["files"]:
            fp = {x["type"]: x["value"] for x in f["fingerprints"]}
            files.append({"id": f["id"], "path": f["path"], "size": f["size"], "width": f["width"], "height": f["height"],
                          "duration": f["duration"], "bit_rate": f["bit_rate"], "codec": f["video_codec"],
                          "fps": f["frame_rate"], "phash": fp.get("phash"), "oshash": fp.get("oshash")})
        ids = []
        for s in sc["stash_ids"]:
            provider = self._provider_key(stash, s["endpoint"])
            entry = {"endpoint": s["endpoint"], "stash_id": s["stash_id"], "provider": provider,
                     "url": self._provider_url(provider, s["stash_id"]) if provider else None}
            if with_provider and provider:
                entry["provider_scene"] = self._provider_scene(provider, s["stash_id"])
            ids.append(entry)
        return {"id": sc["id"], "title": sc["title"], "code": sc["code"], "date": sc["date"], "details": (sc["details"] or "")[:400],
                "urls": sc["urls"], "rating100": sc["rating100"], "plays": sc["play_count"], "o": sc["o_counter"],
                "created_at": sc["created_at"], "studio": (sc["studio"] or {}).get("name"),
                "performers": [p["name"] for p in sc["performers"]], "tags": [t["name"] for t in sc["tags"]],
                "groups": [f"{g['group']['name']} #{g['scene_index']}" for g in sc["groups"]],
                "markers": [{"seconds": m["seconds"], "tag": (m["primary_tag"] or {}).get("name")} for m in sc["scene_markers"]],
                "stash_ids": ids, "files": files}

    def pair(self, pair_id):
        p = get_pair(self.db, pair_id)
        if not p:
            return None
        a, b = self.scene(p["scene_a"]), self.scene(p["scene_b"])
        shared = sorted({i["stash_id"] for i in a.get("stash_ids", [])} & {i["stash_id"] for i in b.get("stash_ids", [])})
        p.update(a=a, b=b, shared_ids=shared)
        if a.get("files") and b.get("files") and a["files"][0].get("phash") and b["files"][0].get("phash"):
            p["phash_distance"] = bin(int(a["files"][0]["phash"], 16) ^ int(b["files"][0]["phash"], 16)).count("1")
        return p

    # ---------- the one write ----------

    def remove_id(self, pair_id, scene_id, stash_id):
        """Removes ONE provider id from ONE scene of the pair (the rest of the scene stays as it is)."""
        err = self._check_member(pair_id, scene_id)
        if err:
            return {"error": err}
        stash = self.get_stash()
        full = self.scene(scene_id, with_provider=False)
        if full.get("missing"):
            return {"error": "scene not found in Stash"}
        if stash_id not in {i["stash_id"] for i in full["stash_ids"]}:
            return {"error": "the scene does not carry that provider id (already removed?)"}
        backup = os.path.join(self.data_dir, "compare_removed_ids.jsonl")
        with open(backup, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"at": time.strftime("%Y-%m-%d %H:%M:%S"), "pair": pair_id, "removed": stash_id, "scene": full},
                                ensure_ascii=False) + "\n")
        keep = [{"endpoint": i["endpoint"], "stash_id": i["stash_id"]} for i in full["stash_ids"] if i["stash_id"] != stash_id]
        stash._graphql(UPDATE, {"i": {"id": str(scene_id), "stash_ids": keep}})
        history.add("info", "history", "compare", f"Provider id removed after a side-by-side comparison (pair {pair_id})",
                    scene_id=str(scene_id), scene_title=full["title"] or (full["files"][0]["path"].rsplit("/", 1)[-1] if full["files"] else None),
                    details={"removed": stash_id, "backup": backup})
        unid_invalidate(self.db)  # the scene may have no provider id now: a candidate for the Unidentified list
        return {"ok": True, "scene": self.scene(scene_id)}

    # ---------- identify / adopt / reset (what to do with the scene that lost its id) ----------

    def _check_member(self, pair_id, scene_id):
        """pair_id 0 = the Review section (no pair): the scene must carry the 'Review: Identify Scene' tag."""
        if int(pair_id) == 0:
            sc = self.get_stash()._graphql("query($id: ID!) { findScene(id: $id) { tags { name } } }", {"id": str(scene_id)})["findScene"]
            if not sc or not any(t["name"] == self.REVIEW_TAG for t in sc["tags"]):
                return f"scene is not tagged '{self.REVIEW_TAG}'"
            return None
        p = get_pair(self.db, pair_id)
        if not p or str(scene_id) not in (p["scene_a"], p["scene_b"]):
            return "scene is not part of this pair"
        return None

    def _backup(self, pair_id, kind, full, extra=None):
        path = os.path.join(self.data_dir, "compare_removed_ids.jsonl")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"at": time.strftime("%Y-%m-%d %H:%M:%S"), "pair": pair_id, "kind": kind, "scene": full, **(extra or {})},
                                ensure_ascii=False) + "\n")
        return path

    def _describe(self, provider, pid, title=None):
        d = self._provider_scene(provider, pid) or {}
        return {"provider": provider, "provider_id": pid, "url": self._provider_url(provider, pid),
                "title": d.get("title") or title, "date": d.get("date"), "studio": d.get("studio"),
                "performers": d.get("performers") or [], "error": d.get("error")}

    def identify(self, pair_id, scene_id):
        """Exact fingerprint lookup (StashDB first, then ThePornDB) for one scene of the pair."""
        err = self._check_member(pair_id, scene_id)
        if err:
            return {"error": err}
        imp = self.get_importer()
        sc = self.get_stash()._graphql(
            "query($id: ID!) { findScene(id: $id) { files { id fingerprints { type value } } } }", {"id": str(scene_id)})["findScene"]
        if not sc or not sc["files"]:
            return {"error": "scene or file not found"}
        found = imp._identify([{"files": [sc["files"][0]]}]).get(sc["files"][0]["id"]) or {"provider": None, "matches": []}
        held = {}
        for m in found["matches"]:
            other = self.get_stash().find_scene_by_stashdb_id(
                m["id"], self.get_stash().stashdb_endpoint if found["provider"] == "stashdb" else self.get_stash().theporndb_endpoint)
            held[m["id"]] = other["id"] if other and str(other["id"]) != str(scene_id) else None
        return {"provider": found["provider"],
                "matches": [dict(self._describe(found["provider"], m["id"], m.get("title")), held_by_scene=held.get(m["id"]))
                            for m in found["matches"]]}

    def search(self, pair_id, scene_id, query):
        """Free-text search at StashDB and ThePornDB (same call the import name suggestions use)."""
        err = self._check_member(pair_id, scene_id)
        if err:
            return {"error": err}
        query = (query or "").strip()
        if len(query) < 3:
            return {"error": "enter at least 3 characters"}
        imp = self.get_importer()
        out = []
        for prov, client in (("stashdb", imp.stashdb), ("theporndb", imp.theporndb)):
            if client is None:
                continue
            try:
                results = library_import._with_retry(client.search_scenes, query, 15)["results"]
            except Exception as e:
                out.append({"provider": prov, "error": f"{type(e).__name__}: {e}"})
                continue
            for c in results:
                out.append({"provider": prov, "provider_id": c["id"], "url": self._provider_url(prov, c["id"]),
                            "title": c.get("title"), "date": c.get("date"), "studio": (c.get("studio") or {}).get("name"),
                            "performers": [x.get("name") for x in (c.get("performers") or [])][:4],
                            "minutes": round(c["duration"] / 60, 1) if c.get("duration") else None})
        return {"results": out}

    def adopt(self, pair_id, scene_id, provider, provider_id):
        """Puts the provider scene's id and metadata on the scene (title/date/studio/performers/tags), like the import."""
        err = self._check_member(pair_id, scene_id)
        if err:
            return {"error": err}
        stash = self.get_stash()
        endpoint = {"stashdb": stash.stashdb_endpoint, "theporndb": stash.theporndb_endpoint}.get(provider)
        if not endpoint:
            return {"error": "unknown provider"}
        full = self.scene(scene_id, with_provider=False)
        backup = self._backup(pair_id, "before-adopt", full, {"adopt": {"provider": provider, "provider_id": provider_id}})
        try:
            library_check.apply_fix(stash, str(scene_id), provider_id, create_missing_performers=True, endpoint=endpoint,
                                    keep_other_endpoints=True)
        except Exception as e:
            return {"error": str(e)}
        after = self.scene(scene_id, with_provider=False)
        history.add("info", "history", "compare", f"Scene identified after a side-by-side comparison: {provider} {provider_id[:8]}",
                    scene_id=str(scene_id), scene_title=after["title"], details={"provider_id": provider_id, "backup": backup})
        closed = False
        if int(pair_id) == 0:  # identified from the Review list: the case is solved, the scene leaves the list (v0.171.0)
            self._remove_review_tag(scene_id, after, f"Review finished: identified as {provider} {provider_id[:8]}, tag '{self.REVIEW_TAG}' removed")
            closed = True
        return {"ok": True, "scene": after, "review_closed": closed}

    def reset_metadata(self, pair_id, scene_id):
        """For a scene whose provider id was removed: clears what the wrong match had put on it and marks it for review.
        Markers, rating and play history stay. Refuses while the scene still carries a provider id."""
        err = self._check_member(pair_id, scene_id)
        if err:
            return {"error": err}
        stash = self.get_stash()
        full = self.scene(scene_id, with_provider=False)
        if full["stash_ids"]:
            return {"error": "the scene still carries a provider id -- remove it first"}
        backup = self._backup(pair_id, "before-metadata-reset", full)
        tag = str(stash.ensure_tag("Review: Identify Scene"))
        stash._graphql(UPDATE, {"i": {"id": str(scene_id), "title": "", "code": "", "date": None, "details": "", "director": "",
                                      "urls": [], "studio_id": None, "performer_ids": [], "tag_ids": [tag]}})
        history.add("info", "history", "compare", "Metadata of a wrong match reset; scene marked 'Review: Identify Scene'",
                    scene_id=str(scene_id), scene_title=full["title"], details={"backup": backup})
        return {"ok": True, "scene": self.scene(scene_id, with_provider=False)}

    # ---------- real frames from the file (Stash's screenshot is the provider's cover, NOT a frame of the file) ----------

    FRAME_FRACTIONS = (0.1, 0.3, 0.5, 0.7, 0.9)

    def frames(self, scene_id, width=480):
        """Five JPEG frames (as data: URLs) cut from the scene's first file with ffmpeg."""
        sc = self.get_stash()._graphql("query($id: ID!) { findScene(id: $id) { files { path duration } } }",
                                       {"id": str(scene_id)})["findScene"]
        if not sc or not sc["files"]:
            return {"error": "scene or file not found"}
        path = self.to_local(sc["files"][0]["path"]) if self.to_local else sc["files"][0]["path"]
        if not os.path.exists(path):
            return {"error": f"file not found on disk: {path}"}
        duration = sc["files"][0]["duration"] or vr_check._duration(path)
        out = []
        for frac in self.FRAME_FRACTIONS:
            t = duration * frac
            raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.2f}", "-i", path, "-frames:v", "1",
                                  "-vf", f"scale={width}:-2", "-q:v", "5", "-f", "image2", "-c:v", "mjpeg", "-"],
                                 capture_output=True, timeout=120).stdout
            out.append({"t": round(t), "data_url": "data:image/jpeg;base64," + base64.b64encode(raw).decode() if raw else None})
        return {"frames": out}

    # ---------- Review: Identify Scene (single scenes, no pair) ----------

    REVIEW_TAG = "Review: Identify Scene"
    MOVIE_ROOTS = ("/data/Movies/", "/data/movies/")  # the film folders (the default; see _movie_roots): their review scenes belong to the "Movies & groups" tab

    def _movie_roots(self):
        """MOVIE_ROOTS plus the first folder of the movie folder template (Settings > Naming), v0.194.0."""
        from routers.settings import DEFAULTS, get_setting_value
        roots = film_names.movie_roots(get_setting_value(self.db, "whisparr_root_folder_path", "/data/"),
                                       get_setting_value(self.db, "movie_folder_format", DEFAULTS["movie_folder_format"]))
        return tuple(dict.fromkeys((*self.MOVIE_ROOTS, *roots)))

    def review_scenes(self):
        """Every scene carrying the review tag, with a light summary (one Stash request, no provider requests)."""
        stash = self.get_stash()
        tag_id = stash.find_tag_id(self.REVIEW_TAG)
        if not tag_id:
            return {"scenes": []}
        data = stash._graphql("""query($t: [ID!]) { findScenes(scene_filter: {tags: {value: $t, modifier: INCLUDES}}, filter: {per_page: -1, sort: "id"}) {
            scenes { id title date studio { name } performers { name } stash_ids { stash_id } scene_markers { id }
            files { path width height duration size } } } }""", {"t": [str(tag_id)]})
        out = []
        marks = review_marks(self.db)
        for sc in data["findScenes"]["scenes"]:
            f = (sc["files"] or [{}])[0]
            is_movie = any((f.get("path") or "").startswith(root) for root in self._movie_roots())
            out.append({"id": sc["id"], "kind": "movie" if is_movie else "scene", "folder": os.path.dirname(f.get("path") or ""),
                        "status": "later" if sc["id"] in marks else "open", "note": (marks.get(sc["id"]) or {}).get("note"), "title": sc["title"], "date": sc["date"], "studio": (sc["studio"] or {}).get("name"),
                        "performers": [x["name"] for x in sc["performers"]], "provider_ids": len(sc["stash_ids"]),
                        "markers": len(sc["scene_markers"]), "path": f.get("path"), "height": f.get("height"),
                        "minutes": round(f["duration"] / 60, 1) if f.get("duration") else None,
                        "mb": round(f["size"] / 1e6) if f.get("size") else None})
        return {"scenes": out}

    # ---------- unidentified scenes (v0.170.0): scenes without any provider id, outside the film folders ----------

    SKIP_ROOTS = ("/data/Movies/", "/data/movies/", "/data/import/", "/data/Import/")  # film folders (scenes there have no ids by design) + the import folder

    def unidentified_list(self, refresh=False):
        """Scenes with NO provider id (StashDB/ThePornDB) outside the film folders and the import folder, without the review tag. One Stash request,
        made once: the list is kept (unid_store) until a change takes an entry out or `refresh` asks again (v0.241.6). Also returns how many were
        left out because they already carry the review tag."""
        if not refresh:
            kept = unid_load(self.db)
            if kept:
                rows, in_review, at = kept
                stored = fp_rows(self.db)
                return {"scenes": rows, "in_review": in_review, "listed_at": at, "checks": {x["id"]: stored[x["id"]] for x in rows if x["id"] in stored}}
        stash = self.get_stash()
        tag_id = stash.find_tag_id(self.REVIEW_TAG)
        data = stash._graphql("""query { findScenes(scene_filter: {stash_id_count: {value: 0, modifier: EQUALS}}, filter: {per_page: -1, sort: "id"}) {
            scenes { id title tags { id } files { path width height duration size } } } }""", {})
        out, in_review = [], 0
        for sc in data["findScenes"]["scenes"]:
            f = (sc["files"] or [{}])[0]
            path = f.get("path") or ""
            if any(path.startswith(r) for r in (*self._movie_roots(), *self.SKIP_ROOTS)) or not path:
                continue
            if tag_id and any(str(t["id"]) == str(tag_id) for t in sc["tags"]):
                in_review += 1
                continue
            folder = os.path.dirname(path)
            out.append({"id": sc["id"], "title": sc["title"] or "", "file": os.path.basename(path), "folder": folder,
                        "studio_folder": folder.split("/")[3] if folder.count("/") >= 3 else "", "height": f.get("height"),
                        "minutes": round(f["duration"] / 60, 1) if f.get("duration") else None,
                        "mb": round(f["size"] / 1e6) if f.get("size") else None})
        unid_store(self.db, out, in_review)
        kept = unid_load(self.db)
        stored = fp_rows(self.db)
        return {"scenes": out, "in_review": in_review, "listed_at": kept[2] if kept else None,
                "checks": {x["id"]: stored[x["id"]] for x in out if x["id"] in stored}}

    def unidentified_check(self, scene_ids):
        """DRY RUN of the fingerprint assignment (nothing is written): StashDB's exact fingerprint lookup for these scenes (one request per 40), and
        ThePornDB's exact hashes (OSHASH / MD5 -- not its similarity-based PHASH, which stays with the per-scene tool in Review) for those StashDB does
        not know or when no StashDB is set up (v0.243.2). Result per scene:
        unique (exactly one provider scene, no local scene holds it, no other checked scene wants it), ambiguous (several), held (another local
        scene already has it), none. Stored in the table fingerprint_check for unidentified_apply(), which only applies what was seen here."""
        stash, imp = self.get_stash(), self.get_importer()
        scenes = []
        for sid in scene_ids[:200]:
            sc = stash._graphql("query($id: ID!) { findScene(id: $id) { id stash_ids { endpoint } files { id fingerprints { type value } } } }",
                                {"id": str(sid)})["findScene"]
            if sc and not sc["stash_ids"] and sc["files"]:
                scenes.append(sc)
        results = {}
        usable = []
        for sc in scenes:
            fps = [x for x in sc["files"][0]["fingerprints"] if x["type"] in ("oshash", "phash", "md5")]
            if fps:
                usable.append(sc)
            else:
                results[sc["id"]] = {"state": "none", "matches": [], "note": "no fingerprints"}
        # The importer's lookup: StashDB's exact lookup (one request per 40 files) when StashDB is set up, ThePornDB's for what StashDB does not know
        # (or alone, v0.243.2). Each match keeps its provider so "Assign" puts the id on the right endpoint.
        found = imp._identify(usable, exact_only=True) if usable else {}
        for sc in usable:
            hit = found.get(sc["files"][0]["id"]) or {"provider": "stashdb", "matches": []}
            prov, m = hit["provider"], hit["matches"] or []
            endpoint = stash.stashdb_endpoint if prov == "stashdb" else stash.theporndb_endpoint
            entry = {"state": "none", "matches": [{"id": x["id"], "title": x.get("title"), "provider": prov} for x in m]}
            if len(m) > 1:
                entry["state"] = "ambiguous"
            elif len(m) == 1:
                other = stash.find_scene_by_stashdb_id(m[0]["id"], endpoint)
                if other and str(other["id"]) != str(sc["id"]):
                    entry.update(state="held", held_by=other["id"])
                else:
                    entry["state"] = "unique"
            results[sc["id"]] = entry
        with _FP_LOCK:
            for sid, entry in results.items():
                fp_put(self.db, sid, entry)
            stored = fp_rows(self.db)  # recomputes "shared" across everything checked so far
            return {sid: stored[sid] for sid in results}

    def unidentified_apply(self, scene_ids):
        """Assigns the StashDB scene to the scenes whose dry-run result is 'unique' (anything else is refused): backup of the scene first, then the
        same adopt as Review (title, date, studio, performers, tags; performers created where missing). The id is checked against the local
        library again right before (library_check.apply_fix). One scrape request per scene."""
        stash = self.get_stash()
        out = {}
        for sid in scene_ids[:50]:
            sid = str(sid)
            with _FP_LOCK:
                e = fp_rows(self.db).get(sid)
            if not e or e["state"] != "unique":
                out[sid] = {"error": "not a clear match in the last fingerprint check -- check it again"}
                continue
            pid = e["matches"][0]["id"]
            prov = e["matches"][0].get("provider") or "stashdb"
            endpoint = stash.stashdb_endpoint if prov == "stashdb" else stash.theporndb_endpoint
            try:
                full = self.scene(sid, with_provider=False)
                if full.get("stash_ids"):
                    out[sid] = {"error": "the scene has a provider id meanwhile"}
                    continue
                backup = self._backup(0, "before-fingerprint-assign", full, {"adopt": {"provider": prov, "provider_id": pid}})
                library_check.apply_fix(stash, sid, pid, create_missing_performers=True, endpoint=endpoint, keep_other_endpoints=True)
                after = self.scene(sid, with_provider=False)
                history.add("info", "history", "compare", f"Scene identified by exact {'StashDB' if prov == 'stashdb' else 'ThePornDB'} fingerprint: {pid[:8]}",
                            scene_id=sid, scene_title=after["title"], details={"provider_id": pid, "backup": backup})
                with _FP_LOCK:
                    fp_delete(self.db, sid)
                unid_drop(self.db, [sid])
                out[sid] = {"ok": True, "title": after["title"], "date": after.get("date")}
            except Exception as ex:  # noqa: BLE001
                out[sid] = {"error": str(ex)}
        return out

    def review_open_count(self):
        """Number of Review scenes that are still open (not marked 'later') -- ids only, one local Stash request (side-menu badge)."""
        stash = self.get_stash()
        tag_id = stash.find_tag_id(self.REVIEW_TAG)
        if not tag_id:
            return 0
        data = stash._graphql("""query($t: [ID!]) { findScenes(scene_filter: {tags: {value: $t, modifier: INCLUDES}}, filter: {per_page: -1, sort: "id"}) {
            scenes { id } } }""", {"t": [str(tag_id)]})
        marks = review_marks(self.db)
        return sum(1 for sc in data["findScenes"]["scenes"] if sc["id"] not in marks)

    def review_add(self, scene_id):
        """Puts a scene into the Review list: ADDS the review tag (other tags and all data stay). Stash only."""
        stash = self.get_stash()
        sc = stash._graphql("query($id: ID!) { findScene(id: $id) { id title tags { name } } }", {"id": str(scene_id)})["findScene"]
        if not sc:
            return {"error": "scene not found in Stash"}
        if any(t["name"] == self.REVIEW_TAG for t in sc["tags"]):
            unid_drop(self.db, [scene_id])
            return {"ok": True, "already": True}
        tag = str(stash.ensure_tag(self.REVIEW_TAG))
        stash._graphql("mutation($i: BulkSceneUpdateInput!) { bulkSceneUpdate(input: $i) { id } }",
                       {"i": {"ids": [str(scene_id)], "tag_ids": {"ids": [tag], "mode": "ADD"}}})
        history.add("info", "history", "compare", "Scene sent to Review > Identify Scene (tag added)",
                    scene_id=str(scene_id), scene_title=sc["title"])
        unid_drop(self.db, [scene_id], to_review=True)
        return {"ok": True, "already": False}

    def _remove_review_tag(self, scene_id, full, reason):
        """Takes the review tag off (everything else stays), after the caller has made the backup `full`; also drops the "later" mark."""
        stash = self.get_stash()
        tag_id = str(stash.find_tag_id(self.REVIEW_TAG))
        keep = [t["id"] for t in stash._graphql("query($id: ID!) { findScene(id: $id) { tags { id } } }", {"id": str(scene_id)})["findScene"]["tags"]
                if str(t["id"]) != tag_id]
        stash._graphql(UPDATE, {"i": {"id": str(scene_id), "tag_ids": keep}})
        set_review_mark(self.db, scene_id, False)
        unid_invalidate(self.db)  # a scene without an id is a candidate for the Unidentified list again
        history.add("info", "history", "compare", reason, scene_id=str(scene_id), scene_title=full["title"])

    def review_done(self, scene_id):
        """Takes the review tag off the scene (everything else stays). Backed up first."""
        err = self._check_member(0, scene_id)
        if err:
            return {"error": err}
        full = self.scene(scene_id, with_provider=False)
        backup = self._backup(0, "before-review-done", full)
        self._remove_review_tag(scene_id, full, f"Review finished: tag '{self.REVIEW_TAG}' removed (backup {os.path.basename(backup)})")
        return {"ok": True}

    def review_status(self, scene_id, later, note=None):
        """'Later' = looked at, cannot be resolved now: stays tagged, leaves the default Open view."""
        err = self._check_member(0, scene_id)
        if err:
            return {"error": err}
        set_review_mark(self.db, scene_id, bool(later), note)
        return {"ok": True, "status": "later" if later else "open"}

    # ---------- Movies & groups review (v0.95.0) ----------

    DRYRUN_FILES = ("movie_tpdb_dryrun.jsonl", "movie_tpdb_dryrun_noyear.jsonl", "movie_tpdb_dryrun_multi.jsonl", "movie_tpdb_dryrun2.jsonl")

    def _movies(self):
        import library_movies
        return library_movies.Movies(self.get_stash(), lambda key, default=None: default)

    def _folder_scenes(self, scene_id):
        """(folder, scenes) -- the ungrouped scenes that make up THIS film: all parts of a film folder "Title (Year)", but for a loose file
        in a genre folder (/data/movies/Anal/x.avi) only that one scene -- never the other films that lie next to it."""
        import library_movies
        stash = self.get_stash()
        sc = stash._graphql("query($id: ID!) { findScene(id: $id) { id groups { group { id } } files { path duration } } }", {"id": str(scene_id)})["findScene"]
        if not sc or not sc["files"] or sc["groups"]:
            return None, []
        path = sc["files"][0]["path"]
        folder = os.path.dirname(path)
        root = next((r for r in self._movie_roots() if path.startswith(r)), None)
        key, is_film_dir = library_movies.Movies._film_key(path, root) if root else (path, False)
        if not is_film_dir:
            return folder, [{"id": sc["id"], "files": [{"file": os.path.basename(path), "minutes": round((sc["files"][0]["duration"] or 0) / 60, 1)}]}]
        data = stash._graphql("""query($f: SceneFilterType) { findScenes(scene_filter: $f, filter: {per_page: -1}) {
                                 scenes { id title groups { group { id } } files { path duration } } } }""",
                              {"f": {"path": {"value": folder + "/", "modifier": "INCLUDES"}}})["findScenes"]["scenes"]
        out = []
        for x in data:
            if x["groups"] or not any(os.path.dirname(f["path"]) == folder for f in x["files"]):
                continue
            out.append({"id": x["id"], "files": [{"file": os.path.basename(f["path"]), "minutes": round((f["duration"] or 0) / 60, 1)}
                                                 for f in x["files"] if os.path.dirname(f["path"]) == folder]})
        if str(scene_id) not in {x["id"] for x in out}:
            return folder, []
        out.sort(key=lambda x: x["files"][0]["file"].lower())
        return folder, out

    def _title_guess(self, folder, first_file):
        import library_movies
        leaf = folder.rstrip("/").split("/")[-1]
        m = library_movies._FOLDER_RE.match(leaf)
        root = next((r for r in self._movie_roots() if folder.startswith(r.rstrip("/"))), "")
        if m and folder.rstrip("/") != root.rstrip("/"):
            return m.group("title").strip(), m.group("year")
        stem = os.path.splitext(first_file)[0]
        mm = library_movies._FOLDER_RE.match(stem)
        return ((mm.group("title") if mm else stem).strip(), (mm.group("year") if mm else None))

    def movie_info(self, scene_id):
        """The film folder of a review scene: its ungrouped scenes/files, the guessed title/year and what the earlier ThePornDB dry runs
        found for it (no request)."""
        err = self._check_member(0, scene_id)
        if err:
            return {"error": err}
        folder, scenes = self._folder_scenes(scene_id)
        if not scenes:
            groups = self.get_stash()._graphql("query($id: ID!) { findScene(id: $id) { groups { group { id name studio { id name } } } } }",
                                               {"id": str(scene_id)})["findScene"]["groups"]
            if groups:  # already in a Group: only its studio is left to decide
                return {"grouped": [{"id": g["group"]["id"], "name": g["group"]["name"], "studio": (g["group"]["studio"] or {}).get("name")}
                                    for g in groups]}
            return {"error": "the scene (or its folder) is not an ungrouped film any more"}
        title, year = self._title_guess(folder, scenes[0]["files"][0]["file"])
        total = round(sum(f["minutes"] for x in scenes for f in x["files"]), 1)
        stored, seen = [], set()
        ids = {x["id"] for x in scenes}
        for name in self.DRYRUN_FILES:
            path = os.path.join(self.data_dir, name)
            if not os.path.exists(path):
                continue
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    r = json.loads(line)
                    rid = set(r.get("scene_ids") or [r.get("scene_id")])
                    if not (rid & ids):
                        continue
                    for c in ([r["best"]] if r.get("best") else []) + (r.get("candidates") or [])[:3]:
                        if c["id"] not in seen:
                            seen.add(c["id"])
                            stored.append({**{k: c.get(k) for k in ("id", "title", "year", "minutes", "site", "performers")},
                                           "from": f"{name.replace('movie_tpdb_', '').replace('.jsonl', '')}: {r['class']}"})
        import library_movies
        key = library_movies.Movies._norm(title)
        same = [g for g in self.get_stash()._graphql("{ findGroups(filter: {per_page: -1}) { groups { id name date duration scene_count studio { name } } } }", {})
                ["findGroups"]["groups"] if library_movies.Movies._norm(g["name"]) == key]
        return {"scene_ids": [x["id"] for x in scenes], "folder": folder, "videos": [f for x in scenes for f in x["files"]],
                "minutes": total, "title": title, "year": year, "stored": stored[:6],
                "same_title_groups": [{"id": g["id"], "name": g["name"], "year": (g["date"] or "")[:4], "minutes": round((g["duration"] or 0) / 60),
                                       "scenes": g["scene_count"], "studio": (g["studio"] or {}).get("name")} for g in same]}

    def movie_search(self, scene_id, query):
        """ThePornDB movie search for a review film (one request); every hit is compared with the folder's total length."""
        info = self.movie_info(scene_id)
        if info.get("error") or info.get("grouped"):
            return info if info.get("error") else {"error": "the film is already in a Group"}
        query = (query or "").strip() or info["title"]
        client = self.get_provider("theporndb")
        if client is None:
            return {"error": "ThePornDB is not configured"}
        try:
            resp = client._get("/movies", params={"q": query, "per_page": 10})
            resp.raise_for_status()
            data = resp.json().get("data") or []
        except Exception as e:
            return {"error": f"ThePornDB did not answer: {type(e).__name__}: {str(e)[:200]}"}
        total = info["minutes"] or 0
        results = []
        for d in data:
            dm = (d.get("duration") or 0) / 60
            results.append({"id": d["id"], "title": d.get("title"), "year": int((d.get("date") or "0")[:4] or 0), "minutes": round(dm, 1),
                            "site": (d.get("site") or {}).get("name"), "performers": len(d.get("performers") or []),
                            "length_ok": bool(dm and total and abs(dm / total - 1) <= 0.10), "url": d.get("url")})
        return {"query": query, "results": results, "folder_minutes": total}

    def _finish_movie_review(self, scene_ids):
        """Review tag + Later mark off for the scenes that now sit in a Group."""
        stash = self.get_stash()
        tag_id = str(stash.find_tag_id(self.REVIEW_TAG) or "")
        for sid in scene_ids:
            cur = stash._graphql("query($id: ID!) { findScene(id: $id) { tags { id } } }", {"id": str(sid)})["findScene"]
            if cur and tag_id and any(str(t["id"]) == tag_id for t in cur["tags"]):
                stash._graphql(UPDATE, {"i": {"id": str(sid), "tag_ids": [t["id"] for t in cur["tags"] if str(t["id"]) != tag_id]}})
            set_review_mark(self.db, sid, False)

    def group_studio(self, scene_id, name=None, clear=False):
        """For a review film that already sits in a Group: sets the Group's studio to the local studio of that exact name (created when it
        does not exist) or clears it; the review tag and Later mark of the scene are cleared. Backed up first."""
        err = self._check_member(0, scene_id)
        if err:
            return {"error": err}
        stash = self.get_stash()
        groups = stash._graphql("query($id: ID!) { findScene(id: $id) { groups { group { id name } } } }", {"id": str(scene_id)})["findScene"]["groups"]
        if not groups:
            return {"error": "the scene is not in a Group"}
        name = (name or "").strip()
        if not clear and not name:
            return {"error": "a studio name is required (or clear the studio)"}
        self._backup(0, "before-group-studio", self.scene(scene_id, with_provider=False), {"groups": [g["group"] for g in groups], "studio": name or None})
        studio_id, created = None, False
        if not clear:
            index = self._movies().local_studio_index()
            import library_movies
            ids = index.get(library_movies.Movies._norm(name), [])
            if len(ids) == 1:
                studio_id = ids[0]
            elif len(ids) > 1:
                return {"error": f"several local studios are called {name!r}; pick it by hand in Stash"}
            else:
                studio_id = stash._graphql("mutation($i: StudioCreateInput!) { studioCreate(input: $i) { id } }", {"i": {"name": name}})["studioCreate"]["id"]
                created = True
        for g in groups:
            stash._graphql("mutation($i: GroupUpdateInput!) { groupUpdate(input: $i) { id } }", {"i": {"id": g["group"]["id"], "studio_id": studio_id}})
        self._finish_movie_review([scene_id])
        history.add("info", "history", "compare", f"Group studio {'cleared' if clear else 'set to ' + repr(name)}{' (studio created)' if created else ''}",
                    scene_id=str(scene_id), details={"groups": [g["group"]["id"] for g in groups], "studio_id": studio_id})
        return {"ok": True, "studio_id": studio_id, "created": created, "groups": [g["group"]["name"] for g in groups]}

    def group_add(self, scene_id, group_id):
        """Puts the scene(s) of this film (all ungrouped parts of the film folder) into an EXISTING Group; the review marks are cleared."""
        err = self._check_member(0, scene_id)
        if err:
            return {"error": err}
        folder, scenes = self._folder_scenes(scene_id)
        if not scenes:
            return {"error": "the scene (or its folder) is not an ungrouped film any more"}
        ids = [x["id"] for x in scenes]
        for sid in ids:
            self._backup(0, "before-group-add", self.scene(sid, with_provider=False), {"group": group_id})
        res = self._movies().create_group(None, ids, group_id=group_id)
        self._finish_movie_review(ids)
        history.add("info", "history", "compare", f"Film put into the existing Group {res['title']!r} ({len(ids)} scene(s))",
                    scene_id=str(scene_id), details={"group": group_id, "scenes": ids})
        return {"ok": True, "group_id": group_id, "name": res["title"], "scenes": len(ids)}

    def movie_create(self, scene_id, tpdb_id):
        """A Group from the chosen ThePornDB movie (GROUP data only, studio only when a local one exists); every ungrouped scene of the film
        folder goes in, in file-name order. Review marks of those scenes are cleared."""
        err = self._check_member(0, scene_id)
        if err:
            return {"error": err}
        client = self.get_provider("theporndb")
        if client is None:
            return {"error": "ThePornDB is not configured"}
        folder, scenes = self._folder_scenes(scene_id)
        if not scenes:
            return {"error": "the scene (or its folder) is not an ungrouped film any more"}
        ids = [x["id"] for x in scenes]
        movies = self._movies()
        movie = client._get(f"/movies/{tpdb_id}").json().get("data")
        if not movie:
            return {"error": "ThePornDB does not know this movie"}
        for sid in ids:
            self._backup(0, "before-movie-group", self.scene(sid, with_provider=False), {"tpdb": tpdb_id})
        res = movies.create_from_tpdb(movie, ids, movies.local_studio_index())
        self._finish_movie_review(ids)
        history.add("info", "history", "compare", f"Group created from ThePornDB movie {movie.get('title')!r} ({len(ids)} scene(s))",
                    scene_id=str(scene_id), details={"group": res["group_id"], "tpdb": tpdb_id, "scenes": ids})
        return {"ok": True, "group_id": res["group_id"], "name": res["name"], "scenes": len(ids), "notes": res["notes"]}

    def movie_create_manual(self, scene_id, name):
        """A Group without provider data (name only) for a film no provider knows; every ungrouped scene of the folder goes in."""
        err = self._check_member(0, scene_id)
        if err:
            return {"error": err}
        folder, scenes = self._folder_scenes(scene_id)
        if not scenes:
            return {"error": "the scene (or its folder) is not an ungrouped film any more"}
        title, _ = self._title_guess(folder, scenes[0]["files"][0]["file"])
        name = (name or "").strip() or title
        if not name:
            return {"error": "a name is required"}
        ids = [x["id"] for x in scenes]
        for sid in ids:
            self._backup(0, "before-movie-group", self.scene(sid, with_provider=False), {"manual": name})
        res = self._movies().create_group(name, ids)
        self._finish_movie_review(ids)
        history.add("info", "history", "compare", f"Group {name!r} created from the folder name ({len(ids)} scene(s))",
                    scene_id=str(scene_id), details={"group": res["group_id"], "scenes": ids})
        return {"ok": True, "group_id": res["group_id"], "name": res["title"], "scenes": len(ids)}

    UNMATCHED_DIR = "/scenes/_UNMATCHED"

    def move_unmatched(self, scene_id):
        """Parks the scene's file in <root>/scenes/_UNMATCHED/<its old sub folders>/ until it can be identified. The Stash scene
        stays, only the path changes; the review tag is taken off so it leaves the list. Backed up first."""
        err = self._check_member(0, scene_id)
        if err:
            return {"error": err}
        imp = self.get_importer()
        busy = imp.queue_guard()
        if busy:
            return busy
        full = self.scene(scene_id, with_provider=False)
        if len(full.get("files") or []) != 1:
            return {"error": "the scene does not have exactly one file"}
        path = full["files"][0]["path"]
        root = imp._root()
        base = root + self.UNMATCHED_DIR
        if path.startswith(base + "/") or "/_UNMATCHED/" in path:  # (also an _UNMATCHED folder deeper down, e.g. scenes/Websites/_UNMATCHED/)
            return {"error": "the file is already in an _UNMATCHED folder"}
        if not path.startswith(root + "/"):
            return {"error": f"the file is not below {root}"}
        sub = "/".join(path[len(root) + 1:].split("/")[1:])  # drop the library folder (Scenes/, scenes/, Movies/ ...)
        new_path = f"{base}/{sub}"
        item = {"path": path, "new_path": new_path}
        backup = self._backup(0, "before-move-unmatched", full, {"to": new_path})
        try:
            old_dir = imp._move_one(item)
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}"}
        imp._scan_and_wait([new_path.rsplit("/", 1)[0]])
        now = self.get_stash().find_scene_by_path(new_path)
        if now is None or str(now["id"]) != str(scene_id):
            return {"error": f"moved on disk to {new_path}, but Stash does not show the scene there yet (run a scan; backup: {backup})"}
        imp._record_move(path, new_path)
        imp._cleanup_dirs([old_dir])
        history.add("info", "history", "compare", "File parked in _UNMATCHED until the scene can be identified",
                    scene_id=str(scene_id), scene_title=full["title"], details={"old": path, "new": new_path, "backup": backup})
        tag_removed = True
        try:  # parked = settled for the review list (the backup above holds the tags)
            self._remove_review_tag(scene_id, full, f"Review finished: parked in _UNMATCHED, tag '{self.REVIEW_TAG}' removed")
        except Exception as e:
            tag_removed = False
            set_review_mark(self.db, scene_id, True)  # fallback: at least off the Open list
            history.add("warning", "history", "compare", f"Parked in _UNMATCHED, but the review tag could not be removed: {e}",
                        scene_id=str(scene_id), scene_title=full["title"])
        return {"ok": True, "path": new_path, "tag_removed": tag_removed}

    # ---------- faces: Stash-Sense 2 (2026-10-02) ----------

    SENSE_PLUGINS = ("stash-sense2", "stash-sense")

    def sense_url(self):
        """Stash-Sense 2's sidecar URL, read from its own plugin settings in Stash (no setting of ours)."""
        plugins = self.get_stash()._graphql("{ configuration { plugins } }", {})["configuration"]["plugins"] or {}
        for pid in self.SENSE_PLUGINS:
            url = ((plugins.get(pid) or {}).get("sidecarUrl") or "").strip().rstrip("/")
            if url:
                return url
        return None

    def sense_status(self):
        """Is Stash-Sense 2 there? Installed AND enabled in Stash AND its sidecar URL set (a removed plugin keeps its
        configuration entry in Stash, so the setting alone proves nothing). The Review area is only offered then."""
        data = self.get_stash()._graphql("{ plugins { id enabled } configuration { plugins } }", {})
        installed = {p["id"]: p for p in data.get("plugins") or []}
        configured = data["configuration"]["plugins"] or {}
        for pid in self.SENSE_PLUGINS:
            if (installed.get(pid) or {}).get("enabled") and ((configured.get(pid) or {}).get("sidecarUrl") or "").strip():
                return {"available": True, "plugin": pid}
        return {"available": False, "plugin": None}

    def faces(self, image_base64, top_k=5):
        """Face recognition of one frame/crop (base64 JPEG) through Stash-Sense 2's /identify. Each match gets the local
        Stash performer that carries its StashDB id (when there is one)."""
        if not image_base64:
            return {"error": "no image data"}
        url = self.sense_url()
        if not url:
            return {"error": "Stash-Sense 2's URL was not found in the plugin settings of Stash (stash-sense2 > sidecarUrl)"}
        try:
            r = requests.post(f"{url}/identify", json={"image_base64": image_base64, "top_k": int(top_k)}, timeout=120)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            return {"error": f"Stash-Sense 2 ({url}) did not answer: {type(e).__name__}: {str(e)[:200]}"}
        stash = self.get_stash()
        cache = {}
        for face in data.get("faces", []):
            for m in face.get("matches", []):
                sid = m.get("stashdb_id")
                if not sid or m.get("local_performer_id"):
                    continue
                if sid not in cache:
                    found = stash._graphql(
                        "query($f: PerformerFilterType) { findPerformers(performer_filter: $f, filter: {per_page: 1}) { performers { id name } } }",
                        {"f": {"stash_id_endpoint": {"endpoint": stash.stashdb_endpoint, "stash_id": sid, "modifier": "EQUALS"}}})
                    cache[sid] = (found["findPerformers"]["performers"] or [None])[0]
                local = cache[sid]
                if local:
                    m["local_performer_id"], m["local_performer_name"] = local["id"], local["name"]
        return {"faces": data.get("faces", []), "face_count": data.get("face_count", 0)}

    def add_performer(self, pair_id, scene_id, performer_id):
        """Adds ONE existing local performer to the scene (nothing is created). Backed up first."""
        err = self._check_member(pair_id, scene_id)
        if err:
            return {"error": err}
        performer_id = norm_id(performer_id)
        stash = self.get_stash()
        perf = stash._graphql("query($id: ID!) { findPerformer(id: $id) { id name } }", {"id": performer_id})["findPerformer"]
        if not perf:
            return {"error": "performer not found in Stash"}
        full = self.scene(scene_id, with_provider=False)
        current = [p["id"] for p in stash._graphql("query($id: ID!) { findScene(id: $id) { performers { id } } }",
                                                  {"id": str(scene_id)})["findScene"]["performers"]]
        if performer_id in current:
            return {"ok": True, "already": True, "scene": full}
        backup = self._backup(pair_id, "before-add-performer", full, {"performer": perf})
        stash._graphql(UPDATE, {"i": {"id": str(scene_id), "performer_ids": current + [performer_id]}})
        history.add("info", "history", "compare", f"Performer {perf['name']} added after face recognition",
                    scene_id=str(scene_id), scene_title=full["title"], details={"performer": perf, "backup": backup})
        return {"ok": True, "scene": self.scene(scene_id, with_provider=False)}

    # ---------- close resolved pairs ----------

    def resolved_pairs(self):
        """Open ('pending') pairs that need no more decision: one scene is gone (merged/deleted), or both scenes carry a
        provider id, share none and neither is tagged for review, or one scene has an id and the other none and is in Review: Identify Scene
        (the doubled id is gone, the question lives there). 'later' pairs are never touched."""
        stash = self.get_stash()
        out = []
        for p in list_pairs(self.db, "pending"):
            scenes = []
            for sid in (p["scene_a"], p["scene_b"]):
                sc = stash._graphql("query($id: ID!) { findScene(id: $id) { id tags { name } stash_ids { stash_id } } }",
                                    {"id": sid})["findScene"]
                scenes.append(sc)
            if any(sc is None for sc in scenes):
                out.append({"id": p["id"], "label": p["label"], "why": "one scene no longer exists (merged/deleted)"})
                continue
            ids = [{i["stash_id"] for i in sc["stash_ids"]} for sc in scenes]
            if ids[0] & ids[1]:
                continue
            in_review = [any(t["name"] == self.REVIEW_TAG for t in sc["tags"]) for sc in scenes]
            if bool(ids[0]) != bool(ids[1]):
                # One scene kept its provider id, the other has none (v0.240.6): the doubled id that made this a pair is gone. When the one
                # without an id is in Review: Identify Scene, what it is goes on there.
                if in_review[0 if not ids[0] else 1]:
                    out.append({"id": p["id"], "label": p["label"], "why": "the doubled provider id is gone; the scene without an id continues in Review: Identify Scene"})
                continue
            if not ids[0] or not ids[1]:
                continue
            if any(in_review):
                continue
            out.append({"id": p["id"], "label": p["label"], "why": "different provider ids, no review tag"})
        return out

    def close_resolved(self, dry_run=True):
        found = self.resolved_pairs()
        if not dry_run:
            for p in found:
                set_status(self.db, p["id"], "done", f"closed automatically: {p['why']}")
        return {"pairs": found, "closed": 0 if dry_run else len(found), "dry_run": dry_run}

    # ---------- Stash scrapers (AdultEmpire, IAFD, data18 ...) ----------

    # AdultDvdMarketPlace is left out: it starts `google-chrome` inside the Stash container, which is not installed there.
    # ThePornDB is offered as its stash-box (id "stashbox:<endpoint>"), not as the community scraper of the same name: that one reads
    # api.metadataapi.net, whose certificate chain many systems cannot verify (v0.204.3).
    SCRAPER_PICKS = ("AdultEmpire", "IAFD", "data18", "FreeonesCommunity")
    STASHBOX_PREFIX = "stashbox:"

    def scrapers(self):
        """The few scene scrapers worth offering, with what each accepts (NAME search and/or URL)."""
        rows = self.get_stash()._graphql("{ listScrapers(types:[SCENE]) { id name scene { supported_scrapes urls } } }", {})["listScrapers"]
        out = []
        for r in rows:
            if r["id"] in self.SCRAPER_PICKS:
                sup = (r["scene"] or {}).get("supported_scrapes") or []
                out.append({"id": r["id"], "name": r["name"], "name_search": "NAME" in sup, "url": "URL" in sup,
                            "url_examples": ((r["scene"] or {}).get("urls") or [])[:2]})
        out = sorted(out, key=lambda x: self.SCRAPER_PICKS.index(x["id"]))
        endpoint = getattr(self.get_stash(), "theporndb_endpoint", "")
        if endpoint:
            out.append({"id": self.STASHBOX_PREFIX + endpoint, "name": "ThePornDB", "name_search": True, "url": False, "url_examples": []})
        return {"scrapers": out}

    def scrape(self, scraper_id, query):
        """One request through Stash's own scraper. `query` = a title/name (scraper must support NAME) or a page URL."""
        query = (query or "").strip()
        if not query:
            return {"error": "enter a title or a URL"}
        stash = self.get_stash()
        fields = "title code details date director urls image studio { name } performers { name } tags { name } remote_site_id"
        if not query.lower().startswith("http"):
            known = {x["id"]: x for x in self.scrapers()["scrapers"]}
            if scraper_id in known and not known[scraper_id]["name_search"]:
                return {"error": f"{known[scraper_id]['name']} only reads a page URL (no title search) -- paste the scene's page address, or pick a scraper marked (title)"}
        try:
            if query.lower().startswith("http"):
                r = stash._graphql("query($u: String!) { scrapeSceneURL(url: $u) { %s } }" % fields, {"u": query})["scrapeSceneURL"]
                results = [r] if r else []
            else:
                results = stash._graphql(
                    "query($s: ScraperSourceInput!, $i: ScrapeSingleSceneInput!) { scrapeSingleScene(source: $s, input: $i) { %s } }" % fields,
                    {"s": ({"stash_box_endpoint": scraper_id[len(self.STASHBOX_PREFIX):]} if (scraper_id or "").startswith(self.STASHBOX_PREFIX)
                           else {"scraper_id": scraper_id}), "i": {"query": query}})["scrapeSingleScene"] or []
        except Exception as e:
            msg = str(e)
            if "google-chrome" in msg:
                msg = "this scraper needs a browser (Chrome) inside the Stash container, which is not installed there -- pick another scraper"
            elif "timed out" in msg.lower():
                msg = ("Stash's scraper did not answer in time (Stash itself is reachable). A title search makes Stash load the cover image of every hit, "
                       "which can take minutes on a slow site -- paste the scene's page address (URL) instead, or try again later")
            return {"error": msg[:400]}
        return {"results": [{"title": x.get("title"), "date": x.get("date"), "details": (x.get("details") or "")[:500],
                             "urls": x.get("urls") or [], "studio": (x.get("studio") or {}).get("name"),
                             "performers": [p["name"] for p in x.get("performers") or []],
                             "tags": [g["name"] for g in x.get("tags") or []][:12], "image": x.get("image")} for x in results]}

    def apply_scrape(self, pair_id, scene_id, title, date, details, url):
        """Puts title/date/details/url of a scraper result on the scene (studio/performers are left alone). Backed up first."""
        err = self._check_member(pair_id, scene_id)
        if err:
            return {"error": err}
        stash = self.get_stash()
        full = self.scene(scene_id, with_provider=False)
        backup = self._backup(pair_id, "before-apply-scrape", full)
        inp = {"id": str(scene_id)}
        if title:
            inp["title"] = title
        if date:
            inp["date"] = date
        if details:
            inp["details"] = details
        if url:
            inp["urls"] = list(dict.fromkeys((full.get("urls") or []) + [url]))
        stash._graphql(UPDATE, {"i": inp})
        history.add("info", "history", "compare", "Title/date/details applied from a Stash scraper result",
                    scene_id=str(scene_id), scene_title=title or full["title"], details={"backup": backup, "url": url})
        return {"ok": True, "scene": self.scene(scene_id, with_provider=False)}

    # ---------- aliases ----------

    def aliases(self):
        rows = scene_alias.list_all(self.db)
        titles = {}
        ids = {r["local_scene_id"] for r in rows}
        if ids:
            data = self.get_stash()._graphql("query($ids: [Int!]) { findScenes(scene_ids: $ids, filter: {per_page: -1}) { scenes { id title } } }",
                                             {"ids": [int(i) for i in ids if str(i).isdigit()]})
            titles = {str(s["id"]): s["title"] for s in data["findScenes"]["scenes"]}
        for r in rows:
            r["local_title"] = titles.get(str(r["local_scene_id"]))
        return {"aliases": rows}

    # ---------- StashDB duplicate entries (v0.125.0) ----------

    def _duplicate_row(self, row_id):
        with self.db.connect() as conn:
            r = conn.execute("SELECT * FROM scene_alias WHERE id = ?", (row_id,)).fetchone()
        return dict(r) if r else None

    def _local_stashdb_id(self, local_scene_id):
        sc = self.get_stash()._graphql("query($id: ID!) { findScene(id: $id) { id title stash_ids { endpoint stash_id } } }", {"id": str(local_scene_id)})["findScene"]
        if not sc:
            return None, None
        ids = [x["stash_id"] for x in sc.get("stash_ids") or [] if "stashdb" in (x.get("endpoint") or "")]
        return (ids[0] if ids else None), sc["title"]

    def duplicate_resolve_preview(self, row_id):
        """What 'Resolve' would do for a duplicate entry StashDB has merged / removed. Read-only (one Stash request)."""
        row = self._duplicate_row(row_id)
        if not row or row["kind"] != scene_alias.KIND_DUPLICATE:
            return {"error": "that is not a recorded StashDB duplicate entry"}
        if row["status"] == "open":
            return {"error": "StashDB has not merged this entry (yet) -- nothing to resolve"}
        local_id, title = self._local_stashdb_id(row["local_scene_id"])
        retag = bool(row["merged_into"] and local_id and local_id != row["merged_into"])
        text = (f"StashDB merged the entries into {row['merged_into'][:8]}. " if row["merged_into"] else "StashDB no longer has one of the entries. ")
        text += (f"Local scene {row['local_scene_id']} carries {local_id[:8]} and gets {row['merged_into'][:8]} (a backup is written first). " if retag
                 else "The local scene's StashDB id stays as it is. ")
        text += "The entry is removed from the alias list."
        return {"ok": True, "retag": retag, "local_stashdb_id": local_id, "merged_into": row["merged_into"], "text": text}

    def duplicate_resolve(self, row_id):
        """Retags the local scene onto the surviving StashDB id when it carries a merged-away one (library_check.apply_fix, previous state
        in a backup file first), then removes the duplicate entry. Returns what the Undo needs."""
        import library_check
        from film_groups import snapshot
        prev = self.duplicate_resolve_preview(row_id)
        if prev.get("error"):
            return prev
        row = self._duplicate_row(row_id)
        stash = self.get_stash()
        applied = []
        snap = snapshot(stash, row["local_scene_id"]) if prev["retag"] else None
        backup = os.path.join(self.data_dir, f"duplicate_resolve_{row_id}_{time.strftime('%Y%m%d-%H%M%S')}.json")
        with open(backup, "w", encoding="utf-8") as fh:
            json.dump({"row": row, "scene": row["local_scene_id"], "snapshot": snap}, fh, ensure_ascii=False, indent=1)
        if not os.path.exists(backup) or os.path.getsize(backup) == 0:
            return {"error": "the backup file could not be written -- nothing was changed"}
        if prev["retag"]:
            library_check.apply_fix(stash, row["local_scene_id"], row["merged_into"], create_missing_performers=True, endpoint=None,
                                    keep_other_endpoints=True)
            applied.append({"scene_id": str(row["local_scene_id"]), "snapshot": snap})
        scene_alias.remove(self.db, row_id)
        history.add("info", "history", "compare", f"StashDB duplicate entry {row['alias_scene_id'][:8]} resolved (local scene {row['local_scene_id']})",
                    scene_id=str(row["local_scene_id"]), details={"merged_into": row.get("merged_into"), "retagged": bool(applied)})
        return {"ok": True, "backup": os.path.basename(backup), "applied": applied, "row": row}

    def duplicate_resolve_undo(self, applied, row):
        from film_groups import restore_snapshot
        for a in applied:
            restore_snapshot(self.get_stash(), a["scene_id"], a["snapshot"])
        scene_alias.add(self.db, row["alias_scene_id"], row["local_scene_id"], row.get("provider") or "stashdb", row.get("title"), row.get("scene_date"),
                        row.get("studio_stashdb_id"), json.loads(row["performer_stashdb_ids"]) if row.get("performer_stashdb_ids") else None,
                        row.get("note"), row.get("kind") or scene_alias.KIND_ALIAS, row.get("partner_scene_id"))
        with self.db.connect() as conn:
            conn.execute("UPDATE scene_alias SET status = ?, merged_into = ? WHERE provider = ? AND alias_scene_id = ?",
                         (row.get("status") or "open", row.get("merged_into"), row.get("provider") or "stashdb", row["alias_scene_id"]))
        return {"ok": True}

    def add_alias(self, alias_scene_id, local_scene_id, note=None):
        alias_scene_id = (alias_scene_id or "").strip()
        local_scene_id = norm_id(local_scene_id)
        if not local_scene_id.isdigit():
            return {"error": "local scene id must be a number"}
        sc = self.get_stash()._graphql("query($id: ID!) { findScene(id: $id) { id title } }", {"id": str(local_scene_id)})["findScene"]
        if not sc:
            return {"error": "local scene not found"}
        held = self.get_stash().find_scene_by_stashdb_id(alias_scene_id)
        if held and str(held["id"]) != str(local_scene_id) and "alias" not in (held.get("title") or ""):
            return {"error": f"that StashDB scene is already held by local scene #{held['id']}"}
        scene_alias.add_from_provider(self.db, self.get_provider("stashdb"), alias_scene_id, local_scene_id,
                                      note=note or "added by hand")
        history.add("info", "history", "compare", f"Alias id recorded: StashDB scene {alias_scene_id[:8]} counts as owned (local scene {local_scene_id})",
                    scene_id=str(local_scene_id), scene_title=sc["title"], details={"alias": alias_scene_id})
        return {"ok": True}
