"""Maintenance: organize the existing library -- rename / move files to the
naming scheme, like the *arrs' "Organize"/"Rename files" (2026-09-28,
v0.82.0, user decisions):

- Only the Whisparr library folder (lowercase `<root>/scenes/`); Scenes/,
  Movies/, movies/, _VR/ are separate topics.
- Target = the naming scheme (scene_folder_format / scene_naming_format)
  with the scene's OWN studio in StashDB's spelling (not the network).
- Only FINGERPRINT-VERIFIED files are offered: a file is renamed/moved only
  if its fingerprints match the scene it's tagged with (StashDB reverse
  lookup, phash within 8 bits as fallback; ThePornDB by phash). Others go
  to "needs check" -- a wrong tag must not be burned into a file name.
- Scenes with several files are listed separately, never touched.
- Two phases in the UI: renames inside the same folder first, then moves.
- Extras: files with the same name stem (.funscript, .nfo, subtitles) are
  renamed along; when the file is the only video in its folder, the whole
  folder content (folder.jpg, cover, ...) moves with it and the empty old
  folder is removed.
- Byte-identical duplicates (same oshash elsewhere in the library) are
  processed one at a time with their own scan -- in one batch Stash may
  swap them between scenes (seen 2026-09-28).
- Stash keeps the scene when a file is renamed or moved (tested on a
  throwaway scene 2026-09-28): the target folders are scanned afterwards
  and every scene is checked at its new path. Everything goes to History.

Quality part of the new name: the source tag found in the current name
(WEBDL, WEBRip, Bluray, ...) or WEBDL; the resolution from the file
(below 480 -> "480p" like the library); VR files keep the resolution
token they already have (open question whether to switch them to the
width class).
"""
import json
import logging
import os
import re
import shutil
import threading
import time
from collections import defaultdict

import file_naming
import film_names
import history
import quality_detect
import renamer_check
import shared_fingerprint

logger = logging.getLogger(__name__)

# Stash's own default video extensions (m4v, mp4, mov, wmv, avi, mpg, mpeg, rmvb, rm, flv, asf, mkv, webm, f4v) plus .ts; .asf & co. were
# missing, so the import counted them as "unknown" files (and "Finish source" refused while they lay there).
VIDEO_EXT = {".mp4", ".mkv", ".avi", ".wmv", ".mov", ".m4v", ".mpg", ".mpeg", ".flv", ".webm", ".ts", ".asf", ".rmvb", ".rm", ".f4v"}
_SOURCE_RE = re.compile(r"(WEBDL|WEB-DL|WEBRip|Bluray|BluRay|HDTV|DVD|Remux)", re.IGNORECASE)
_SOURCE_NAME = {"webdl": "WEBDL", "web-dl": "WEBDL", "webrip": "WEBRip", "bluray": "Bluray", "hdtv": "HDTV",
                "dvd": "DVD", "remux": "Remux"}
_RES_TOKEN_RE = re.compile(r"(?<![0-9A-Za-z])(\d{3,4}p|\dK)(?![0-9A-Za-z])")
PHASH_TOLERANCE = 8
_FP_BATCH = 40

_SCENES_QUERY = """
query($f: FindFilterType, $s: SceneFilterType) {
  findScenes(filter: $f, scene_filter: $s) {
    scenes {
      id title date
      studio { id name stash_ids { endpoint stash_id } }
      tags { name }
      stash_ids { endpoint stash_id }
      files { id path width height duration mod_time fingerprints { type value } }
    }
  }
}
"""

_MOVIE_SCENES_QUERY = """
query($f: FindFilterType, $s: SceneFilterType) {
  findScenes(filter: $f, scene_filter: $s) {
    scenes {
      id title
      files { id path width height duration }
      groups { scene_index group { id name date scene_count studio { name } } }
    }
  }
}
"""

_overview_cache = {"at": 0, "data": None}  # the rough local count, before the first Recount


def stored_overview(db):
    """The checked studio list from the stored plans (organize_plan), sorted by what is left to do; None before the first Recount."""
    with db.connect() as conn:
        if not conn.execute("SELECT 1 FROM organize_state WHERE key = 'recount_at'").fetchone():
            return None
        rows = conn.execute("SELECT studio_id, studio, plan_json FROM organize_plan").fetchall()
    out = []
    for r in rows:
        plan = json.loads(r["plan_json"])
        rename, move = len(plan.get("rename", [])), len(plan.get("move", []))
        other = len(plan.get("needs_check", [])) + len(plan.get("conflict", []))
        if rename + move + other:
            out.append({"studio_id": r["studio_id"], "studio": r["studio"], "rename": rename, "move": move, "other": other, "total": rename + move + other})
    out.sort(key=lambda x: (-x["total"], x["studio"].lower()))
    return out


def stored_checked_at(db):
    with db.connect() as conn:
        row = conn.execute("SELECT value FROM organize_state WHERE key = 'recount_at'").fetchone()
    return row["value"] if row else None
_oshash_cache = {"at": 0, "data": None}
# The background "Recount" (v0.240.17): every studio with local candidates gets its real plan (provider data + fingerprints), so only studios with
# something to do stay in the list.
_shared_scan = {"running": False, "done": 0, "total": 0, "found": 0, "provider": "", "error": None, "finished_at": None}
_recount = {"running": False, "done": 0, "total": 0, "studio": "", "phase": "", "error": None, "finished_at": None}


def _older_than_release(file, scene_date, slack_days=60):
    from datetime import datetime, timedelta
    try:
        modified = datetime.fromisoformat((file.get("mod_time") or "").replace("Z", "+00:00")).date()
        released = datetime.fromisoformat(scene_date).date()
    except (TypeError, ValueError):
        return False
    return modified < released - timedelta(days=slack_days)


class Organizer:
    extra_providers = ()  # user-added stash-boxes, see __init__
    def __init__(self, db, stash_client, stashdb_client, theporndb_client, to_local, settings_getter,
                 get_whisparr_client=None, extra_providers=None):
        self.get_whisparr = get_whisparr_client
        self.extra_providers = list(extra_providers or [])  # user-added stash-boxes: [{"key", "name", "endpoint", "client"}] (v0.244.0)
        self.db = db
        self.stash = stash_client
        self.stashdb = stashdb_client
        self.theporndb = theporndb_client
        self.to_local = to_local
        self.setting = settings_getter  # (key) -> value

    # ---------- the providers the fingerprint tools ask (v0.244.0) ----------

    def endpoint_of(self, provider):
        """The Stash endpoint a provider's scene ids live on (StashDB, ThePornDB or a user-added stash-box); None when unknown."""
        if provider == "stashdb":
            return self.stash.stashdb_endpoint
        if provider == "theporndb":
            return self.stash.theporndb_endpoint
        return next((e["endpoint"] for e in self.extra_providers if e["key"] == provider), None)

    def client_of(self, provider):
        if provider == "stashdb":
            return self.stashdb
        if provider == "theporndb":
            return self.theporndb
        return next((e["client"] for e in self.extra_providers if e["key"] == provider), None)

    def name_of(self, provider):
        return {"stashdb": "StashDB", "theporndb": "ThePornDB"}.get(provider) or next(
            (e["name"] for e in self.extra_providers if e["key"] == provider), provider)

    def provider_of_endpoint(self, endpoint):
        """Key of the provider with this endpoint, among those that can be asked (StashDB without a key does not count)."""
        for key in ("stashdb", "theporndb", *[e["key"] for e in self.extra_providers]):
            if self.client_of(key) is not None and self.endpoint_of(key) == endpoint:
                return key
        return None

    def boxes(self):
        """[(key, client)] of the stash-boxes asked many files per request: StashDB (when set up), then the user-added ones."""
        rows = [("stashdb", self.stashdb)] if self.stashdb is not None else []
        return rows + [(e["key"], e["client"]) for e in self.extra_providers]

    # ---------- sanity: other renamers, files moved again ----------

    def renamer_guard(self, ignore=False):
        """None when it is fine to move files; else {"error", "renamers"}: another ENABLED renaming plugin would move them again after the scan
        (see renamer_check.py). `ignore` = the user confirmed it knows (the warning banner's checkbox)."""
        if ignore:
            return None
        active = renamer_check.active_renamers(self.stash)
        if not active:
            return None
        return {"error": renamer_check.conflict_text(active), "renamers": [r["id"] for r in active]}

    DRIFT_WATCH_SECONDS = 90

    def _watch_drift(self, items):
        """A few minutes after a move: is each file still where we put it? Something else (a renamer plugin, Whisparr) moving it again is
        written to History -- and found by the sanity check."""
        pairs = [(str(i["scene_id"]), i["new_path"], i.get("title") or "") for i in items if i.get("scene_id") and i.get("new_path")]
        if pairs and self.DRIFT_WATCH_SECONDS:
            timer = threading.Timer(self.DRIFT_WATCH_SECONDS, self._check_drift, args=(pairs,))
            timer.daemon = True
            timer.start()

    def _scene_paths(self, scene_id):
        scene = self.stash._graphql("query($id: ID!) { findScene(id: $id) { files { path } } }", {"id": str(scene_id)})["findScene"]
        return [f["path"] for f in (scene or {}).get("files") or []]

    def _check_drift(self, pairs):
        for scene_id, expected, title in pairs:
            try:
                paths = self._scene_paths(scene_id)
            except Exception:
                logger.exception("drift check: couldn't read scene %s", scene_id)
                continue
            if paths and expected not in paths:
                history.add("warning", "history", "organize", "Moved again by something else: the file this plugin placed was found elsewhere",
                            scene_title=title, details={"stash_scene": scene_id, "placed": expected, "now": paths})

    # ---------- expected path ----------

    def _root(self):
        return (self.setting("whisparr_root_folder_path") or "/data").rstrip("/")

    def _in_scope(self, path):
        return path.startswith(self._root() + "/scenes/")

    def _quality(self, scene, file, name):
        m = _SOURCE_RE.search(name)
        source = _SOURCE_NAME[m.group(1).lower()] if m else quality_detect.DEFAULT_SOURCE
        width, height = file.get("width") or 0, file.get("height") or 0
        is_vr = any((t.get("name") or "").lower() == "vr" for t in scene.get("tags") or [])
        if is_vr or quality_detect.vr_label(width, height):
            token = _RES_TOKEN_RE.search(re.sub(r"\.[A-Za-z0-9]+$", "", name))
            resolution = token.group(1) if token else quality_detect.vr_label(width, height, True) \
                or quality_detect.resolution_label(height)
        else:
            resolution = quality_detect.resolution_label(height)
        return f"{source}-{resolution}"

    def expected_path(self, scene, file, studio_name):
        name = file["path"].rsplit("/", 1)[1]
        ext = os.path.splitext(name)[1]
        detail = {"title": scene["title"], "date": scene["date"], "studio": {"name": studio_name},
                  "performers": []}
        tokens = file_naming.build_tokens(detail, self._quality(scene, file, name), tags=[t.get("name") for t in scene.get("tags") or []],
                                          options=file_naming.naming_options(lambda key: self.setting(key) or "[]"))
        rel = file_naming.render_path(self.setting("scene_folder_format"), self.setting("scene_naming_format"),
                                      tokens, ext)
        return f"{self._root()}/{rel.lstrip('/')}"

    # ---------- overview (no fingerprints) ----------

    def _all_scenes(self):
        return self.stash._graphql(_SCENES_QUERY, {"f": {"per_page": -1}})["findScenes"]["scenes"]

    def overview(self, refresh=False):
        """The studio list: after a Recount (start_recount) the checked list read from the stored plans (organize_plan) -- only studios whose real plan
        has something to do, kept until the next Recount and lowered by every rename / move; before the first Recount the rough local count
        (_local_overview), cached 10 minutes."""
        stored = stored_overview(self.db)
        if stored is not None:
            return stored
        if _overview_cache["data"] is not None and not refresh and time.time() - _overview_cache["at"] < 600:
            return _overview_cache["data"]
        data = self._local_overview()
        _overview_cache.update(at=time.time(), data=data, verified=False)
        return data

    # ---------- the stored plans (v0.240.27) ----------

    def _store_plan(self, studio_id, plan):
        local = plan.get("local_studio") or ""
        with self.db.connect() as conn:
            conn.execute(
                """INSERT INTO organize_plan (studio_id, studio, plan_json, checked_at) VALUES (?, ?, ?, datetime('now'))
                   ON CONFLICT(studio_id) DO UPDATE SET studio = excluded.studio, plan_json = excluded.plan_json, checked_at = excluded.checked_at""",
                (str(studio_id), local, json.dumps(plan)))

    def _load_plan(self, studio_id):
        """(plan, checked_at 'YYYY-MM-DD HH:MM:SS' UTC) of a studio, or None when none is stored."""
        with self.db.connect() as conn:
            row = conn.execute("SELECT plan_json, checked_at FROM organize_plan WHERE studio_id = ?", (str(studio_id),)).fetchone()
        return (json.loads(row["plan_json"]), row["checked_at"]) if row else None

    def plan_for_studio(self, studio_id, fresh=False):
        """The studio's plan: the stored one (instant) unless `fresh` or none is stored -- then it is made (StashDB: fingerprints, titles, dates,
        studio) and stored. The answer carries `checked_at`."""
        if not fresh:
            stored = self._load_plan(studio_id)
            if stored:
                return {**stored[0], "checked_at": stored[1]}
        plan = self.plan(studio_id)
        if plan.get("studio") is not None:
            self._store_plan(studio_id, plan)
        stored = self._load_plan(studio_id)
        return {**plan, "checked_at": stored[1] if stored else None}

    def _drop_stored(self, items):
        """What was just renamed / moved leaves the stored plans of its studios (v0.240.27): the plan stays right without a new check."""
        by_studio = defaultdict(set)
        for item in items:
            if item.get("studio_id") is not None:
                by_studio[str(item["studio_id"])].add(str(item["file_id"]))
        for studio_id, ids in by_studio.items():
            stored = self._load_plan(studio_id)
            if not stored:
                continue
            plan, _ = stored
            for k in ("rename", "move", "conflict", "needs_check"):
                plan[k] = [x for x in plan.get(k, []) if str(x["file_id"]) not in ids]
            with self.db.connect() as conn:
                conn.execute("UPDATE organize_plan SET plan_json = ? WHERE studio_id = ?", (json.dumps(plan), studio_id))

    def _local_overview(self):
        """Per studio: how many files would be renamed / moved by the LOCAL title, date and studio name -- no StashDB, so it also counts files that
        the provider's data would leave where they are."""
        per = defaultdict(lambda: {"rename": 0, "move": 0})
        names = {}
        for scene in self._all_scenes():
            if not (scene["stash_ids"] and scene["title"] and scene["date"] and scene["studio"]):
                continue
            if len(scene["files"]) != 1 or not self._in_scope(scene["files"][0]["path"]):
                continue
            file = scene["files"][0]
            expected = self.expected_path(scene, file, scene["studio"]["name"])
            if expected == file["path"]:
                continue
            sid = scene["studio"]["id"]
            names[sid] = scene["studio"]["name"]
            same_folder = expected.rsplit("/", 1)[0] == file["path"].rsplit("/", 1)[0]
            per[sid]["rename" if same_folder else "move"] += 1
        data = sorted(({"studio_id": k, "studio": names[k], **v, "total": v["rename"] + v["move"]}
                       for k, v in per.items()), key=lambda x: (-x["total"], x["studio"].lower()))
        return data

    def start_recount(self):
        if _recount["running"]:
            return {"started": False, "running": True}
        threading.Thread(target=self._recount_run, daemon=True, name="organize-recount").start()
        return {"started": True}

    @staticmethod
    def recount_status():
        return dict(_recount)

    def _recount_run(self):
        _recount.update(running=True, done=0, total=0, studio="", phase="counting the files by their local names", error=None)
        try:
            local = self._local_overview()
            _recount.update(total=len(local), phase="reading the library's fingerprints")
            dup = self._oshash_counts()
            _recount["phase"] = "checking the studios"
            scanned = set()
            for i, row in enumerate(local):
                _recount.update(done=i, studio=row["studio"])
                try:
                    p = self.plan(row["studio_id"], dup=dup)
                    if p.get("studio") is not None:
                        p["local_studio"] = p.get("local_studio") or row["studio"]
                        self._store_plan(row["studio_id"], p)
                        scanned.add(str(row["studio_id"]))
                except Exception:
                    logger.exception("organize recount: plan of %s failed, it keeps its stored plan (if any)", row["studio"])
                    scanned.add(str(row["studio_id"]))
            with self.db.connect() as conn:
                # Studios that no longer have a local candidate leave the store; the state marks "a Recount has run".
                for (sid,) in conn.execute("SELECT studio_id FROM organize_plan").fetchall():
                    if sid not in scanned:
                        conn.execute("DELETE FROM organize_plan WHERE studio_id = ?", (sid,))
                conn.execute("INSERT INTO organize_state (key, value) VALUES ('recount_at', datetime('now')) "
                             "ON CONFLICT(key) DO UPDATE SET value = excluded.value")
            _recount["done"] = len(local)
        except Exception as e:  # noqa: BLE001
            logger.exception("organize recount failed")
            _recount["error"] = f"{type(e).__name__}: {e}"
        finally:
            _recount.update(running=False, finished_at=time.time())

    # ---------- files registered at several provider scenes (v0.246.0) ----------

    def start_shared_scan(self):
        if _shared_scan["running"]:
            return {"started": False, "running": True}
        threading.Thread(target=self._shared_scan_run, daemon=True, name="shared-fingerprint-scan").start()
        return {"started": True}

    @staticmethod
    def shared_scan_status():
        return dict(_shared_scan)

    def _shared_scan_run(self):
        """Asks every set-up stash-box for the fingerprints of ALL tagged scenes and puts the ones that need a decision on the review list
        (shared_fingerprint.py). Scenes whose length confirms the tag are not listed; open rows the scan finds fine now are settled."""
        _shared_scan.update(running=True, done=0, total=0, found=0, provider="", error=None)
        try:
            for box, client in self.boxes():
                endpoint = self.endpoint_of(box)
                _shared_scan.update(provider=self.name_of(box), done=0)
                scenes = self.stash.all_scenes_with_stashdb_id(endpoint)
                rows = []
                for s in scenes:
                    tag = next((x["stash_id"] for x in s["stash_ids"] if x["endpoint"] == endpoint), None)
                    f = next((f for f in s.get("files") or [] if f.get("fingerprints")), None)
                    fps = [{"hash": x["value"], "algorithm": x["type"].upper()} for x in (f or {}).get("fingerprints", []) if x["type"] in ("oshash", "phash", "md5")]
                    if tag and fps:
                        rows.append((s, tag, f, fps))
                _shared_scan["total"] = len(rows)
                seen = set()
                for i in range(0, len(rows), _FP_BATCH):
                    chunk = rows[i:i + _FP_BATCH]
                    try:
                        results = client.find_scenes_by_fingerprints([r[3] for r in chunk])
                    except Exception:
                        logger.exception("shared scan: %s lookup failed", self.name_of(box))
                        continue
                    lengths = self._lengths(client, [m["id"] for found in results if found and len(found) > 1 for m in found])
                    for (s, tag, f, _), found in zip(chunk, results):
                        if found and len(found) > 1 and any(m["id"] == tag for m in found):
                            ok, _how = self._judge_shared(box, client, s, f, tag, found, lengths)
                            if not ok:
                                seen.add(str(s["id"]))
                                _shared_scan["found"] += 1
                    _shared_scan["done"] = min(i + _FP_BATCH, len(rows))
                    time.sleep(0.5)
                shared_fingerprint.close_unseen(self.db, seen, box)
            with self.db.connect() as conn:
                conn.execute("INSERT INTO organize_state (key, value) VALUES ('shared_scan_at', datetime('now')) "
                             "ON CONFLICT(key) DO UPDATE SET value = excluded.value")
        except Exception as e:  # noqa: BLE001
            logger.exception("shared fingerprint scan failed")
            _shared_scan["error"] = f"{type(e).__name__}: {e}"
        finally:
            _shared_scan.update(running=False, finished_at=time.time())

    def shared_overview(self, status="open"):
        with self.db.connect() as conn:
            at = conn.execute("SELECT value FROM organize_state WHERE key = 'shared_scan_at'").fetchone()
        return {"rows": shared_fingerprint.rows(self.db, status), "counts": shared_fingerprint.counts(self.db), "scanned_at": at["value"] if at else None,
                "scan": self.shared_scan_status()}

    # ---------- per-studio plan ----------

    def _studio_folder_name(self, studio):
        stashdb_id = next((s["stash_id"] for s in studio.get("stash_ids") or []
                           if s["endpoint"] == self.stash.stashdb_endpoint), None)
        if stashdb_id and self.stashdb is not None:
            try:
                return self.stashdb.get_studio_name(stashdb_id) or studio["name"]
            except Exception:
                logger.exception("organize: StashDB name for studio %s", studio["name"])
        return studio["name"]

    def _oshash_counts(self):
        """{oshash: number of files} over the whole library, for the "identical copy elsewhere" mark. One query over every scene: kept for 10 minutes
        (v0.240.26), so a rename right after a plan does not read the library again."""
        if _oshash_cache["data"] is not None and time.time() - _oshash_cache["at"] < 600:
            return _oshash_cache["data"]
        counts = defaultdict(int)
        for scene in self._all_scenes():
            for f in scene["files"]:
                for fp in f["fingerprints"]:
                    if fp["type"] == "oshash":
                        counts[fp["value"]] += 1
        _oshash_cache.update(at=time.time(), data=counts)
        return counts

    def _verify(self, candidates):
        """{file_id: (verified, how)} for [(scene, file)]."""
        result = {}
        by_box = {}
        for scene, file in candidates:
            ids = {s["endpoint"]: s["stash_id"] for s in scene["stash_ids"]}
            box = next((k for k, _ in self.boxes() if self.endpoint_of(k) in ids), None)
            if box is None and self.stash.stashdb_endpoint in ids:
                result[file["id"]] = (False, "StashDB is not set up")
            elif box is not None:
                by_box.setdefault(box, []).append((scene, file, ids[self.endpoint_of(box)]))
            elif self.stash.theporndb_endpoint in ids and self.theporndb is not None:
                result[file["id"]] = self._verify_by_phash(self.theporndb, ids[self.stash.theporndb_endpoint], file)
            else:
                result[file["id"]] = (False, "no StashDB/ThePornDB id")
        for box, pairs in by_box.items():
            client = self.client_of(box)
            for i in range(0, len(pairs), _FP_BATCH):
                chunk = pairs[i:i + _FP_BATCH]
                sets = [[{"hash": fp["value"], "algorithm": fp["type"].upper()} for fp in f["fingerprints"]]
                        for _, f, _ in chunk]
                try:
                    matches = client.find_scenes_by_fingerprints(sets)
                except Exception:
                    logger.exception("organize: %s fingerprint lookup failed", self.name_of(box))
                    matches = [None] * len(chunk)
                lengths = self._lengths(client, [m["id"] for found in matches if found and len(found) > 1 for m in found])
                for (scene, file, provider_id), found in zip(chunk, matches):
                    if found and any(m.get("id") == provider_id for m in found):
                        if len(found) > 1:  # registered at several scenes: the tag being one of them proves little (v0.246.0)
                            result[file["id"]] = self._judge_shared(box, client, scene, file, provider_id, found, lengths)
                        else:
                            result[file["id"]] = (True, "fingerprint")
                    else:
                        result[file["id"]] = self._verify_by_phash(client, provider_id, file,
                                                                   other=[m.get("title") for m in found or []])
        return result

    def _lengths(self, client, ids):
        """{provider scene id: seconds or None}; empty when the client cannot say."""
        ids = sorted(set(ids))
        if not ids or not hasattr(client, "scene_durations"):
            return {}
        out = {}
        try:
            for i in range(0, len(ids), 40):
                out.update(client.scene_durations(ids[i:i + 40]) or {})
        except Exception:
            logger.exception("organize: provider lengths failed")
        return out

    def _judge_shared(self, box, client, scene, file, provider_id, found, lengths):
        """(ok, how) for a file registered at several provider scenes; what the length cannot settle goes to the review list."""
        verdict = shared_fingerprint.judge(file.get("duration"), provider_id, found, lengths,
                                           kept=shared_fingerprint.is_kept(self.db, scene["id"], provider_id))
        if not verdict["ok"]:
            shared_fingerprint.upsert(self.db, scene["id"], scene.get("title"), file["path"], file.get("duration"), box, provider_id,
                                      verdict["reason"], verdict["how"], shared_fingerprint.enrich(client, found, lengths))
        return (verdict["ok"], verdict["how"])

    def _provider_metadata(self, scene):
        """{title, date, studio} of the scene at StashDB (or ThePornDB), or None. `studio` is the scene's OWN studio at StashDB (v0.240.23: a scene
        of a child studio such as "Brazzers Exxtra" belongs in that folder although its local studio is the parent); None for ThePornDB, whose
        site names are not the spelling the library uses."""
        ids = {s["endpoint"]: s["stash_id"] for s in scene["stash_ids"]}
        for endpoint, client in ((self.stash.stashdb_endpoint, self.stashdb),
                                 *[(e["endpoint"], e["client"]) for e in self.extra_providers],
                                 (self.stash.theporndb_endpoint, self.theporndb)):
            if endpoint in ids and client is not None:
                try:
                    detail = client.get_scene_detail(ids[endpoint])
                except Exception:
                    logger.exception("organize: metadata lookup failed for scene %s", scene["id"])
                    return None
                if detail and detail.get("title") and detail.get("date"):
                    studio = (detail.get("studio") or {}).get("name") if endpoint == self.stash.stashdb_endpoint else None
                    return {"title": detail["title"], "date": detail["date"], "studio": studio}
                return None
        return None

    def _verify_by_phash(self, client, provider_scene_id, file, other=None):
        local = next((fp["value"] for fp in file["fingerprints"] if fp["type"] == "phash"), None)
        try:
            remote = [fp["hash"] for fp in client.get_scene_fingerprints(provider_scene_id)
                      if fp.get("algorithm", "").lower() == "phash" and fp.get("hash")]
        except Exception:
            return (False, "fingerprint lookup failed")
        if not remote:
            return (False, "the provider has no fingerprints for this scene")
        if not local:
            return (False, "no local phash yet")
        best = min(bin(int(h, 16) ^ int(local, 16)).count("1") for h in remote)
        if best <= PHASH_TOLERANCE:
            return (True, f"phash (distance {best})")
        hint = f"; matches {other[0]!r} instead" if other else ""
        return (False, f"fingerprints don't match (phash distance {best}{hint})")

    def plan(self, studio_id, scene_id=None, dup=None, only_files=None):
        """Per-studio plan: {studio, rename, move, needs_check, multi_file, conflict}. With `scene_id` only that one scene is planned (the
        Organized hook, v0.203.0): same checks, but no scan of the whole library for byte-identical copies."""
        if scene_id is not None:
            scenes = self.stash._graphql(_SCENES_QUERY, {"f": {"per_page": 1}, "s": {"id": {"value": int(scene_id), "modifier": "EQUALS"}}})["findScenes"]["scenes"]
            scenes = [s for s in scenes if s["studio"]]
        else:
            scenes = self.stash._graphql(_SCENES_QUERY, {
                "f": {"per_page": -1},
                "s": {"studios": {"value": [str(studio_id)], "modifier": "INCLUDES"}},
            })["findScenes"]["scenes"]
            scenes = [s for s in scenes if s["studio"] and str(s["studio"]["id"]) == str(studio_id)]
            if only_files is not None:
                # Applying chosen files (v0.240.26): only their scenes are planned and verified, not the whole studio.
                scenes = [s for s in scenes if any(str(f["id"]) in only_files for f in s["files"])]
        if not scenes:
            return {"studio": None, "rename": [], "move": [], "needs_check": [], "multi_file": [], "conflict": []}
        folder_studio = self._studio_folder_name(scenes[0]["studio"])
        dup = {} if scene_id is not None else (dup if dup is not None else self._oshash_counts())
        plan = {"studio": folder_studio, "local_studio": scenes[0]["studio"]["name"], "rename": [], "move": [], "needs_check": [], "multi_file": [], "conflict": []}
        candidates = []
        for scene in scenes:
            if not (scene["stash_ids"] and scene["title"] and scene["date"]):
                continue
            files = [f for f in scene["files"] if self._in_scope(f["path"])]
            if not files:
                continue
            expected = [self.expected_path(scene, f, folder_studio) for f in files]
            if all(e == f["path"] for e, f in zip(expected, files)):
                continue
            if len(scene["files"]) > 1:
                plan["multi_file"].append({"scene_id": scene["id"], "title": scene["title"],
                                           "paths": [f["path"] for f in scene["files"]]})
                continue
            candidates.append((scene, files[0], expected[0]))
        verified = self._verify([(s, f) for s, f, _ in candidates])
        for scene, file, expected in candidates:
            ok, how = verified.get(file["id"], (False, "not checked"))
            fresh = None
            if ok:
                # Title/date from the provider, like an import (2026-09-28:
                # some Stash scenes carry the file name as title and a scan
                # date -- "Blow Job Frenzy.flv - 2024-08-16").
                fresh = self._provider_metadata(scene)
                if fresh:
                    expected = self.expected_path({**scene, "title": fresh["title"], "date": fresh["date"]}, file, fresh.get("studio") or folder_studio)
                # A phash NEAR-match is weaker evidence (low-res videos with the
                # same studio intro can land within a few bits): a file older
                # than the scene's release can't be that scene (2026-09-28: a
                # 2008 Dancing Bear .flv "matched" a 2024 scene at distance 4).
                # (an EXACT phash match, distance 0, is the same video -- an old rip
                # of a scene the provider dated later; 2026-09-29, 19 such files)
                if not how.startswith("fingerprint") and not how.startswith("phash (distance 0)") \
                        and _older_than_release(file, (fresh or scene).get("date")):
                    ok, how = False, f"{how}, but the file is older than the scene's release date"
                if expected == file["path"]:
                    continue
            oshash = next((fp["value"] for fp in file["fingerprints"] if fp["type"] == "oshash"), None)
            stashdb_id = next((s["stash_id"] for s in scene["stash_ids"]
                               if s["endpoint"] == self.stash.stashdb_endpoint), None)
            item = {"scene_id": scene["id"], "file_id": file["id"], "studio_id": (scene.get("studio") or {}).get("id"), "title": (fresh or scene)["title"],
                    "stashdb_id": stashdb_id, "date": (fresh or scene).get("date"),
                    "path": file["path"], "new_path": expected, "check": how,
                    "duplicate": bool(oshash and dup.get(oshash, 0) > 1)}
            if not ok:
                plan["needs_check"].append(item)
            elif os.path.exists(self.to_local(expected)):
                # 2026-09-28: two versions of one scene in a folder -- the
                # target name is taken; the user decides which one to keep.
                plan["conflict"].append({**item, "reason": "target name already exists"})
            elif expected.rsplit("/", 1)[0] == file["path"].rsplit("/", 1)[0]:
                plan["rename"].append(item)
            else:
                plan["move"].append(item)
        # Two planned files that want the same target name also collide.
        by_target = defaultdict(list)
        for key in ("rename", "move"):
            for x in plan[key]:
                by_target[x["new_path"].lower()].append((key, x))
        for group in by_target.values():
            if len(group) > 1:
                for key, x in group:
                    plan[key].remove(x)
                    plan["conflict"].append({**x, "reason": "another file of this plan has the same target name"})
        for key in ("rename", "move", "needs_check", "conflict"):
            plan[key].sort(key=lambda x: x["path"].lower())
        return plan

    # ---------- existing films against the movie templates (v0.202.0) ----------

    def movie_roots(self):
        return [r.rstrip("/") for r in film_names.movie_roots(self._root(), self.setting("movie_folder_format"))]

    def _in_movie_roots(self, path):
        return any(path.startswith(r + "/") for r in self.movie_roots())

    def expected_movie_path(self, group, file, part_number):
        """Where the movie templates (Settings > Import & Naming > Movies/Groups) put this file: title/year from the Stash group, the quality from the
        file, the part number for a film of several scenes -- the same path the film import and the Import folder's movie kind build."""
        name = file["path"].rsplit("/", 1)[1]
        quality = f"{quality_detect.guess_source(name)}-{quality_detect.resolution_label(file.get('height') or 0)}"
        title, year = film_names.split_title_year(group["name"], group.get("date"))
        tokens = film_names.movie_tokens(title, year, part_number, quality, (group.get("studio") or {}).get("name") or "",
                                         file_naming.naming_options(lambda key: self.setting(key) or "[]"))
        rel = film_names.render_movie_path(self.setting("movie_folder_format"), self.setting("movie_naming_format"), tokens,
                                           os.path.splitext(name)[1])
        return f"{self._root()}/{rel.lstrip('/')}"

    @staticmethod
    def _movie_diff(old_path, new_path):
        """Why a film file differs from the template, so the list can be worked class by class: 'file name' (same folder, only the file name),
        'edition note in the name', 'not in a film folder' (a genre folder / loose file), 'year', 'capitals/punctuation' (the title differs only in those), 'title'."""
        old_dir, new_dir = old_path.rsplit("/", 1)[0], new_path.rsplit("/", 1)[0]
        if old_dir == new_dir:
            return "file name"
        old_leaf, new_leaf = old_dir.rsplit("/", 1)[1], new_dir.rsplit("/", 1)[1]
        year = re.compile(r"\s*\((\d{4})\)\s*$")
        if film_names.strip_edition_tags(old_leaf) != old_leaf:
            return "edition note in the name"  # e.g. "(DVD + Blu-ray Combo)" in the folder name on disk
        if not year.search(old_leaf):
            return "not in a film folder"
        old_title, new_title = year.sub("", old_leaf), year.sub("", new_leaf)
        norm = lambda t: re.sub(r"[^0-9a-z]", "", t.lower())  # noqa: E731
        if norm(old_title) == norm(new_title):
            ym_old, ym_new = year.search(old_leaf), year.search(new_leaf)
            same_year = (ym_old.group(1) if ym_old else None) == (ym_new.group(1) if ym_new else None)
            if old_title == new_title and not same_year:
                return "year"
            return "capitals/punctuation" if same_year else "year"
        return "title"

    def movies_plan(self):
        """Every film file in the movie folders that does not sit where the templates put it: {rename, move, conflict, needs_check, multi_file,
        in_place}. Read-only. The group is the film's identity (made by hand or by a scraper), so there is no fingerprint check here -- a group
        that is wrong shows up in the list and is simply not applied."""
        scenes = self.stash._graphql(_MOVIE_SCENES_QUERY, {"f": {"per_page": -1}, "s": {"groups": {"value": [], "modifier": "NOT_NULL"}}})["findScenes"]["scenes"]
        plan = {"rename": [], "move": [], "conflict": [], "needs_check": [], "multi_file": [], "in_place": 0}
        for scene in scenes:
            files = [f for f in scene["files"] if self._in_movie_roots(f["path"])]
            if not files:
                continue
            title = (scene["groups"][0]["group"]["name"] if scene["groups"] else None) or scene["title"] or f"Scene {scene['id']}"
            base = {"scene_id": scene["id"], "title": title}
            if len(scene["files"]) > 1:
                plan["multi_file"].append({**base, "paths": [f["path"] for f in scene["files"]]})
                continue
            if len(scene["groups"]) != 1:
                plan["needs_check"].append({**base, "file_id": files[0]["id"], "path": files[0]["path"], "check": f"the scene is in {len(scene['groups'])} groups"})
                continue
            entry, file = scene["groups"][0], files[0]
            group = entry["group"]
            part = None
            if (group.get("scene_count") or 1) > 1:
                if entry.get("scene_index") is None:
                    plan["needs_check"].append({**base, "file_id": file["id"], "path": file["path"],
                                                "check": f"the group has {group['scene_count']} scenes but this scene has no scene number"})
                    continue
                part = entry["scene_index"]
            expected = self.expected_movie_path(group, file, part)
            if expected == file["path"]:
                plan["in_place"] += 1
                continue
            item = {**base, "file_id": file["id"], "path": file["path"], "new_path": expected, "check": f"group \"{group['name']}\"",
                    "duplicate": False, "stashdb_id": None, "diff": self._movie_diff(file["path"], expected)}
            if os.path.exists(self.to_local(expected)):
                plan["conflict"].append({**item, "reason": "target name already exists"})
            elif expected.rsplit("/", 1)[0] == file["path"].rsplit("/", 1)[0]:
                plan["rename"].append(item)
            else:
                plan["move"].append(item)
        by_target = defaultdict(list)
        for key in ("rename", "move"):
            for x in plan[key]:
                by_target[x["new_path"].lower()].append((key, x))
        for same in by_target.values():
            if len(same) > 1:
                for key, x in same:
                    plan[key].remove(x)
                    plan["conflict"].append({**x, "reason": "another file of this plan has the same target name"})
        for key in ("rename", "move", "needs_check", "conflict"):
            plan[key].sort(key=lambda x: x["path"].lower())
        return plan

    def _cleanup_movie_dirs(self, dirs):
        roots = [self.to_local(r) for r in self.movie_roots()]
        for d in sorted(set(dirs), key=len, reverse=True):
            cur = d
            while cur and any(cur.startswith(r + "/") for r in roots) and os.path.isdir(cur):
                try:
                    os.rmdir(cur)  # only succeeds when empty
                except OSError:
                    break
                cur = os.path.dirname(cur)

    def movies_apply(self, file_ids, kinds=("rename", "move"), force=False, ignore_renamers=False):
        """Recomputes the movie plan (never trusts the browser's paths) and applies the requested items, like apply() does for scenes."""
        busy = self.queue_guard(force)
        if busy:
            return busy
        conflict = self.renamer_guard(ignore_renamers)
        if conflict:
            return conflict
        wanted = {str(i) for i in file_ids or []}
        plan = self.movies_plan()
        items = [i for k in kinds for i in plan.get(k, []) if str(i["file_id"]) in wanted]
        done, failed, old_dirs, folders = [], [], [], []
        for item in items:
            try:
                old_dirs.append(self._move_one(item))
            except Exception as e:
                failed.append({**item, "reason": str(e)})
                continue
            folders.append(item["new_path"].rsplit("/", 1)[0])
            done.append(item)
        confirmed = list(done)  # like the scene side (v0.240.27): the scan is started and not waited for, a background check confirms later
        for item in confirmed:
            self._record_move(item["path"], item["new_path"])
            history.add("info", "history", "organize",
                        f"{'Moved' if item['path'].rsplit('/', 1)[0] != item['new_path'].rsplit('/', 1)[0] else 'Renamed'} film file: "
                        f"{item['path'].rsplit('/', 1)[1]} -> {item['new_path'][len(self._root()):]}",
                        scene_title=item["title"],
                        details={"stash_scene": item["scene_id"], "old": item["path"], "new": item["new_path"], "extras": item.get("extras_moved")})
        self._cleanup_movie_dirs(old_dirs)
        if confirmed or failed:
            history.event("warning" if failed else "info", "organize", f"Organize movies: {len(confirmed)} done, {len(failed)} failed (Stash scan started)")
        self._scan_then_confirm(confirmed, folders)
        self._watch_drift(confirmed)
        return {"done": confirmed, "failed": failed, "scan": "started" if confirmed else None}

    # ---------- the Organized hook for a scene of the Whisparr library folder (v0.203.0) ----------

    def organized_library_scene(self, scene, mode):
        """A scene marked Organized that lies in the lowercase library folder (scenes/) and carries a provider id: the same plan the studio-by-studio
        Organize library makes for it -- fingerprint-verified, title/date from the provider, the studio's own spelling. 'preview' only writes the
        plan to History, 'on' moves it. Returns {"handled": False, ...} when this is not a scene for this path (the caller says why), else the result."""
        path = scene["files"][0]["path"]
        if not path.startswith(self._root() + "/scenes/") or not scene.get("stash_ids"):
            return {"handled": False}
        title = scene.get("title") or f"scene {scene['id']}"
        if not scene.get("studio"):
            return {"handled": True, "note": "has no studio in Stash -- not moved"}
        plan = self.plan(None, scene_id=scene["id"])
        item = next((i for k in ("rename", "move") for i in plan[k]), None)
        if item is None:
            why = next((i.get("reason") or i.get("check") for k in ("conflict", "needs_check") for i in plan[k]), None)
            return {"handled": True, "note": f"was not moved: {why}" if why else "is already where it belongs"}
        rel = lambda p: p[len(self._root()):]  # noqa: E731
        if mode == "preview":
            history.add("info", "history", "organize", f"Organized preview (nothing moved): {rel(path)} -> {rel(item['new_path'])}",
                        scene_title=title, details={"stash_scene": scene["id"], "old": path, "new": item["new_path"], "kind": "library"})
            return {"handled": True, "preview": item}
        conflict = self.renamer_guard(False)
        if conflict:
            history.add("warning", "history", "organize", f"Organized preview (NOT moved, another renamer is enabled): {rel(path)} -> {rel(item['new_path'])}",
                        scene_title=title, details={"stash_scene": scene["id"], "old": path, "new": item["new_path"], "renamers": conflict["renamers"]})
            return {"handled": True, "blocked": conflict["error"], "preview": item}
        busy = self.queue_guard(False)
        if busy:
            return {"handled": True, "note": "was not moved: " + busy["error"]}
        result = self._organize_items([item], f"{plan['studio']} (Organized)")
        return {"handled": True, **result}

    # ---------- apply ----------

    def _move_one(self, item):
        """Moves/renames the video and its extras on disk. Returns the old
        local folder (for the empty-folder cleanup)."""
        old_local, new_local = self.to_local(item["path"]), self.to_local(item["new_path"])
        if os.path.exists(new_local):
            raise FileExistsError(f"target already exists: {item['new_path']}")
        if not os.path.exists(old_local):
            raise FileNotFoundError(f"file not found on disk: {item['path']}")
        old_dir, old_name = os.path.split(old_local)
        new_dir, new_name = os.path.split(new_local)
        old_stem, new_stem = os.path.splitext(old_name)[0], os.path.splitext(new_name)[0]
        siblings = [n for n in os.listdir(old_dir) if n != old_name]
        other_videos = [n for n in siblings if os.path.splitext(n)[1].lower() in VIDEO_EXT]
        os.makedirs(new_dir, exist_ok=True)
        shutil.move(old_local, new_local)
        moved = []
        for n in siblings:
            src = os.path.join(old_dir, n)
            same_stem = n.startswith(old_stem + ".")
            action = self._extra_action(n, same_stem, has_other_videos=bool(other_videos),
                                        folder_changes=old_dir != new_dir)
            if action == "move":
                if not os.path.isfile(src):
                    continue  # NEVER drag a folder along (2026-09-29: a loose video in a studio folder took all 284 scene folders with it)
                dst = os.path.join(new_dir, new_stem + n[len(old_stem):]) if same_stem else os.path.join(new_dir, n)
                if os.path.exists(dst):
                    continue
                shutil.move(src, dst)
                moved.append(n)
            elif action == "recycle" and os.path.isfile(src):
                self.recycle(src)
        item["extras_moved"] = moved
        return old_dir

    def _record_move(self, old_path, new_path):
        """Tables of this sidecar that are keyed by file path must follow a moved/renamed file
        (2026-09-30: after the phase-2 moves, upgrade candidates and VR-check results still pointed at
        the old paths -- an upgrade silently failed to merge)."""
        try:
            with self.db.connect() as conn:
                conn.execute("UPDATE OR IGNORE vr_file_check SET file_path = ? WHERE file_path = ?", (new_path, old_path))
                conn.execute("UPDATE OR IGNORE upgrade_candidate SET file_path = ? WHERE file_path = ?", (new_path, old_path))
        except Exception:
            logger.exception("organize: could not update path-keyed rows for %s", old_path)

    def _extra_action(self, name, same_stem, has_other_videos, folder_changes):
        """What happens to a non-video file next to a moved video: 'move',
        'recycle' or 'leave'. Organize: a same-stem extra is renamed along; folder
        content (folder.jpg, ...) follows the only video."""
        if same_stem:
            return "move"
        if not has_other_videos and folder_changes:
            return "move"
        return "leave"

    def recycle(self, local_path):  # set by the Importer; the plain Organizer never recycles
        raise NotImplementedError

    def _cleanup_roots(self):
        """Local folders below which empty folders are removed (the roots
        themselves stay)."""
        return [self.to_local(self._root() + "/scenes")]

    def _cleanup_dirs(self, dirs):
        roots = self._cleanup_roots()
        for d in sorted(set(dirs), key=len, reverse=True):
            cur = d
            while cur and any(cur.startswith(r + "/") for r in roots) and os.path.isdir(cur):
                try:
                    os.rmdir(cur)  # only succeeds when empty
                except OSError:
                    break
                cur = os.path.dirname(cur)

    def queue_guard(self, force=False):
        """None when it is fine to move files; else {"error", "queue"}. Moves are followed by a Stash scan,
        and Stash runs one job at a time -- behind a Generate task (hours) the scan, and with it every
        confirmation, would wait for hours (2026-09-30)."""
        if force:
            return None
        try:
            queue = self.stash._graphql("{ jobQueue { id status description progress } }", {})["jobQueue"] or []
        except Exception:
            logger.exception("organize: couldn't read Stash's job queue")
            return None
        active = [j for j in queue if j["status"] in ("RUNNING", "READY")]
        generating = [j for j in active if "enerat" in (j.get("description") or "")]
        if generating or len(active) >= 6:
            return {"error": f"Stash's job queue is busy ({len(active)} job(s){'; a Generate task is running' if generating else ''}) "
                             "-- the scan after the moves would wait behind it. Stop the Generate task (Stash > Settings > "
                             "Tasks) or try again when the queue is empty.",
                    "queue": [f"{j['description']} ({j['status']})" for j in active][:8]}
        return None

    def _scan_and_wait(self, folders):
        if not folders:
            return
        job = self.stash.scan_paths(sorted(set(folders)))
        try:
            self.stash.wait_for_job(job, poll_interval=5, timeout=6 * 3600)
        except Exception:
            logger.exception("organize: Stash scan didn't finish in time")
        time.sleep(2)

    def sync_whisparr(self, item):
        """Tells Whisparr about a renamed/moved file (user, 2026-09-28: keep
        Whisparr usable in case it's brought back into the process): a new
        folder is set on the movie first (moveFiles=false, Whisparr moves
        nothing), then RescanMovie takes the file over in place. Returns a
        short status; never raises."""
        if not self.get_whisparr or not item.get("stashdb_id"):
            return "not in Whisparr (no StashDB id)"
        try:
            whisparr = self.get_whisparr()
            movie = whisparr.find_by_stash_id(item["stashdb_id"])
            if movie is None:
                return "not in Whisparr"
            new_folder = item["new_path"].rsplit("/", 1)[0]
            if (movie.get("path") or "").rstrip("/") != new_folder:
                owner = next((m for m in whisparr.movies_in_folder(new_folder) if m["id"] != movie["id"]), None)
                if owner is not None:
                    # The folder already belongs to ANOTHER Whisparr movie: the
                    # Stash tag and Whisparr disagree about what this file is
                    # (found live: a file of one title tagged as
                    # another title). Don't move Whisparr's movie --
                    # let the folder's owner take the file and report it.
                    whisparr.rescan_movie(owner["id"])
                    history.add("warning", "history", "organize",
                                f"Tag conflict: the Stash tag says {movie.get('title')!r}, Whisparr has this folder "
                                f"as {owner.get('title')!r} -- check the tag (Library Check)",
                                scene_title=item["title"], details={"folder": new_folder})
                    return f"tag conflict: Whisparr has this folder as {owner.get('title')!r}"
                whisparr.set_movie_path(movie["id"], new_folder)
            status = whisparr.rescan_movie(movie["id"])
            after = whisparr.get_movie(movie["id"])
            known = ((after.get("movieFile") or {}).get("relativePath") or "")
            if known == item["new_path"].rsplit("/", 1)[1]:
                return "updated"
            history.add("warning", "history", "organize", f"Whisparr didn't take over the new file ({status})",
                        scene_title=item["title"], details={"whisparr_movie": movie["id"], "known": known})
            return f"not updated ({status}, knows {known or 'no file'})"
        except Exception as e:
            logger.exception("organize: Whisparr update failed for %s", item["new_path"])
            return f"Whisparr update failed: {e}"

    def apply(self, studio_id, file_ids, kinds=("rename", "move"), force=False, ignore_renamers=False):
        """Recomputes the plan (never trusts the browser's paths) and
        applies the requested verified items. Returns {done, failed}."""
        busy = self.queue_guard(force)
        if busy:
            return busy
        conflict = self.renamer_guard(ignore_renamers)
        if conflict:
            return conflict
        wanted = {str(i) for i in file_ids or []}
        # The plan that was checked (fingerprints, StashDB's title / date / studio) and stored is used as it is -- no new StashDB round trip (v0.240.27);
        # _move_one still refuses a file that is gone or whose target exists. Without a stored plan the chosen files are planned.
        stored = self._load_plan(studio_id)
        plan = stored[0] if stored else self.plan(studio_id, only_files=wanted)
        items, refused = [], []
        for k in kinds:
            for i in plan.get(k if k != "check" else "needs_check", []):
                if str(i["file_id"]) not in wanted:
                    continue
                # "check" (v0.240.15): the user says an unconfirmed file is the right one and renames / moves it anyway; the plan did not test the
                # target name for these (it does for verified files), so it is tested here.
                if k == "check" and os.path.exists(self.to_local(i["new_path"])):
                    refused.append({**i, "reason": "target name already exists"})
                    continue
                items.append(i)
        result = self._organize_items(items, plan["studio"]) if items else {"done": [], "failed": []}
        result["failed"] = list(result.get("failed") or []) + refused
        return result

    def _organize_items(self, items, label):
        """Moves/renames the planned items and returns at once, like Whisparr's Stash hook (v0.240.27): sidecar tables and Whisparr follow, the stored
        plans lose the items, ONE Stash scan is started and NOT waited for, and a background check later confirms each scene at its new path
        (a failure goes to History). Returns {done, failed}."""
        done, failed, old_dirs, batch_folders = [], [], [], []
        for item in sorted(items, key=lambda x: x["duplicate"]):
            try:
                old_dirs.append(self._move_one(item))
            except Exception as e:
                failed.append({**item, "reason": str(e)})
                continue
            if item["duplicate"]:
                # Byte-identical copy elsewhere: Stash must see it alone first, or it could take the other copy's scene.
                self._scan_and_wait([item["new_path"].rsplit("/", 1)[0]])
            else:
                batch_folders.append(item["new_path"].rsplit("/", 1)[0])
            done.append(item)
        for item in done:
            item["whisparr"] = self.sync_whisparr(item)
            self._record_move(item["path"], item["new_path"])
            history.add("info", "history", "organize",
                        f"{'Moved' if item['path'].rsplit('/', 1)[0] != item['new_path'].rsplit('/', 1)[0] else 'Renamed'}: "
                        f"{item['path'].rsplit('/', 1)[1]} -> {item['new_path'][len(self._root()):]}",
                        scene_title=item["title"],
                        details={"stash_scene": item["scene_id"], "old": item["path"], "new": item["new_path"], "extras": item.get("extras_moved")})
        self._cleanup_dirs(old_dirs)
        self._drop_stored(done)
        if done or failed:
            history.event("warning" if failed else "info", "organize", f"Organize {label}: {len(done)} done, {len(failed)} failed (Stash scan started)")
        self._scan_then_confirm(done, batch_folders)
        self._watch_drift(done)
        return {"done": done, "failed": failed, "scan": "started" if done else None}

    def _scan_then_confirm(self, done, folders):
        """Starts the Stash scan of the folders (not waited for) and, in the background, checks afterwards that each scene stands at its new path."""
        if not done:
            return
        job = None
        try:
            if folders:
                job = self.stash.scan_paths(sorted(set(folders)))
        except Exception:
            logger.exception("organize: the Stash scan could not be started")
            history.add("warning", "history", "organize", "Moved on disk, the Stash scan could not be started (scan the library in Stash)",
                        details={"files": len(done)})

        def confirm():
            try:
                if job is not None:
                    self.stash.wait_for_job(job, poll_interval=5, timeout=6 * 3600)
                time.sleep(2)
            except Exception:
                logger.exception("organize: Stash scan didn't finish in time")
            for item in done:
                try:
                    scene = self.stash.find_scene_by_path(item["new_path"])
                except Exception:
                    logger.exception("organize: could not look for %s in Stash", item["new_path"])
                    continue
                if scene is None or str(scene["id"]) != str(item["scene_id"]):
                    history.add("warning", "history", "organize", "Moved on disk, Stash does not show the scene there (scan again)",
                                scene_title=item["title"], details={"old": item["path"], "new": item["new_path"], "stash_scene": item["scene_id"]})
        threading.Thread(target=confirm, daemon=True, name="organize-confirm").start()
