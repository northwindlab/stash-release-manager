"""Folder import (2026-09-29): brings the files of the temporary source
folders (the setting `import_sources`; added and removed in Maintenance > Import folder) into the library.

The sources are Stash library paths, so Stash already knows their files
(fingerprints included). What is left is to IDENTIFY them, adopt the provider's
metadata and move them to the naming scheme -- the Organize engine's job:

    ready     scene already carries a provider id and the fingerprints verify it
    identify  no provider id, but StashDB knows the file's fingerprint EXACTLY
              (one scene) -> the id is assigned, then it is placed like "ready"
    review    id present but the fingerprints don't verify it / several matches
    unknown   nothing found -- stays where it is

    movie     (v0.190.0) no provider id, but the scene is in exactly ONE Stash group and that is confirmed: the group carries a
              ThePornDB movie link, or you marked the scene "Organized" -> it goes to the movie template of
              Settings > Naming (default <library>/Movies/<Title> (<Year>)/<Title> (<Year>)[ - part N] <Quality>.<ext>), the layout the film import uses. The group is the identity, no
              fingerprint lookup (a DVD scene you grouped is not looked up as a single scene at the providers).

Only `ready`, `movie` and `identify` are ever moved. User rules (2026-09-29): of the
files next to a video only those Stash uses (.funscript, subtitles) move
along; known junk (jpg/nfo/txt/url/rar ...) goes to the recycle bin (7 days),
never a hard delete; empty folders are removed, the source ROOT stays.
"""
import logging
import os
import re
import time
from collections import defaultdict

import file_naming
import film_names
import history
import library_check
import quality_detect
from library_organize import Organizer, VIDEO_EXT, _FP_BATCH, _older_than_release

logger = logging.getLogger(__name__)


_IMPORT_SCENES_QUERY = """
query($f: FindFilterType, $s: SceneFilterType) {
  findScenes(filter: $f, scene_filter: $s) {
    scenes {
      id title date organized
      studio { id name stash_ids { endpoint stash_id } }
      tags { name }
      stash_ids { endpoint stash_id }
      groups { scene_index group { id name date scene_count urls studio { name } } }
      files { id path width height duration mod_time fingerprints { type value } }
    }
  }
}
"""


def _with_retry(fn, *args, attempts=4):
    """Provider calls with a backoff on HTTP 429 (2026-09-29: ThePornDB rate-limited the import and
    scenes fell back to "unknown"/"review" for no real reason)."""
    import requests
    delay = 8
    for n in range(attempts):
        try:
            return fn(*args)
        except requests.HTTPError as e:
            code = e.response.status_code if e.response is not None else None
            if code == 429 and n < attempts - 1:
                try:
                    wait = int(e.response.headers.get("Retry-After") or delay)
                except ValueError:
                    wait = delay
                time.sleep(min(max(wait, 1), 60))
                delay *= 2
                continue
            raise

# Reasons of a failed fingerprint check that the user may overrule after looking at the file (v0.228.0); the other review reasons
# (a provider scene another scene holds, several files, name taken) are not about the check. "A near phash match, but the file is older
# than the release" (v0.228.1) is the user's call too: the item carries can_trust itself (see _plan_target).
_TRUSTABLE = ("the provider has no fingerprints", "fingerprints don't match", "no local phash", "fingerprint lookup failed", "no exact fingerprint")
REVIEW_TAG = "Review: Identify Scene"  # scene_compare.REVIEW_TAG (that module imports this one)
DEFAULT_SOURCES = ["/data/Scenes", "/data/import", "/data/_VR"]
KEEP_EXTS = {".funscript", ".srt", ".vtt"}
JUNK_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".nfo", ".txt", ".url", ".rar", ".sfv", ".nzb",
             ".par2", ".srr", ".md5", ".diz", ".html", ".htm", ".lnk", ".db", ".ini", ".sample"}
_JUNK_RE = re.compile(r"\.(r\d\d|\d{3}|part\d+\.rar)$", re.IGNORECASE)  # .r00 / .001 / .part01.rar
VR_TAG_NAME = "Virtual Reality"


def is_junk(name):
    return os.path.splitext(name)[1].lower() in JUNK_EXTS or bool(_JUNK_RE.search(name))


class Importer(Organizer):
    def __init__(self, *args, recycle=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._recycle = recycle

    # ---------- sources / scope ----------

    def roots(self):
        """The source folders. A saved list (even an empty one) is used as it is; before the first save the old default
        folders count, but only those that exist, so a new installation starts with none."""
        raw = self.setting("import_sources")
        if isinstance(raw, str) and raw.strip():
            try:
                import json
                raw = json.loads(raw)
            except ValueError:
                raw = [x.strip() for x in raw.split(",") if x.strip()]
        if isinstance(raw, list):
            return [r.rstrip("/") for r in raw if str(r).strip()]
        return [r for r in DEFAULT_SOURCES if os.path.isdir(self.to_local(r))]

    def save_roots(self, roots):
        import json
        value = json.dumps(sorted({r.rstrip("/") for r in roots}))
        with self.db.connect() as conn:
            conn.execute("""INSERT INTO setting (key, value) VALUES ('import_sources', ?)
                            ON CONFLICT (key) DO UPDATE SET value = excluded.value""", (value,))

    # ---------- add / remove a source folder (v0.226.0) ----------

    def add_check(self, path):
        """What adding `path` as a source would do, and what stands in the way. Changes nothing."""
        path = (path or "").rstrip("/")
        report = {"path": path, "problems": [], "videos": 0, "covered_by": None, "will_add_library": False, "can_add": False}
        if not path.startswith("/") or path == "":
            report["problems"].append("not an absolute folder path")
            return report
        if path in self.roots():
            report["problems"].append("already an import source")
        library = self._root() + "/scenes"
        protected = [library, *self.movie_roots()]
        for p in protected:
            if path == p or path.startswith(p + "/"):
                report["problems"].append(f"{p} is the library itself, never an import source")
            elif p.startswith(path + "/"):
                report["problems"].append(f"the folder contains the library folder {p}")
        local = self.to_local(path)
        if not os.path.isdir(local):
            report["problems"].append("the sidecar cannot see this folder (check the media mount)")
        else:
            report["videos"] = sum(1 for _, _, fs in os.walk(local) for f in fs if os.path.splitext(f)[1].lower() in VIDEO_EXT)
            if not report["videos"]:
                report["problems"].append("no video files in the folder")
        libs = [x["path"].rstrip("/") for x in self.stash.library_config()]
        covered = next((x for x in libs if path == x or path.startswith(x + "/")), None)
        inside = [x for x in libs if x.startswith(path + "/") and x not in protected]
        if inside:
            report["problems"].append(f"Stash already lists folders below it as libraries ({inside[0]}); add those one by one")
        report["covered_by"] = covered
        report["will_add_library"] = covered is None
        report["can_add"] = not report["problems"]
        return report

    def add_source(self, path, backup_dir):
        """Makes `path` an import source: Stash gets it as a library path when it has none covering it (the old list is saved first), then
        Stash scans it (with phashes). Nothing is moved; Analyse is the next step."""
        report = self.add_check(path)
        if not report["can_add"]:
            return {**report, "done": False}
        path = report["path"]
        backup = None
        if report["will_add_library"]:
            import json
            libs = self.stash.library_config()
            os.makedirs(backup_dir, exist_ok=True)
            backup = os.path.join(backup_dir, f"stash_libraries_before_add_{int(time.time())}.json")
            with open(backup, "w") as f:
                json.dump(libs, f, indent=1)
            wanted = libs + [{"path": path, "excludeVideo": False, "excludeImage": True}]
            after = self.stash.set_library_config(wanted)
            if not any(x["path"].rstrip("/") == path for x in after) or len(after) != len(wanted):
                raise RuntimeError(f"Stash's library list looks wrong after adding {path}; the old list is in {backup}")
        self.save_roots([*self.roots(), path])
        job = self.stash.scan_paths([path])
        history.add("info", "history", "import", f"Source folder added: {path} ({report['videos']} video file(s)); "
                    + ("added to Stash's libraries, " if report["will_add_library"] else f"already in Stash's library {report['covered_by']}, ")
                    + "scan started", details={"backup": backup, "scan_job": job})
        return {**report, "done": True, "backup": backup, "scan_job": job}

    def remove_source(self, root):
        """Takes the folder off the list. Stash's library list and the files stay as they are."""
        root = root.rstrip("/")
        if root not in self.roots():
            return {"removed": False, "error": "not an import source"}
        self.save_roots([r for r in self.roots() if r != root])
        history.add("info", "history", "import", f"Source folder removed from the list: {root} (Stash's libraries and the files are untouched)")
        return {"removed": True}

    def _in_scope(self, path):
        return any(path.startswith(r + "/") for r in self.roots())

    def _root_of(self, path):
        return next((r for r in self.roots() if path.startswith(r + "/")), None)

    def _cleanup_roots(self):
        return [self.to_local(r) for r in self.roots()]

    def recycle(self, local_path):
        if self._recycle is None:
            raise RuntimeError("no recycle bin configured")
        self._recycle(local_path)

    # ---------- what happens to the files next to a video ----------

    def _extra_action(self, name, same_stem, has_other_videos, folder_changes):
        ext = os.path.splitext(name)[1].lower()
        if ext in KEEP_EXTS and same_stem:
            return "move"  # renamed along with the video
        if ext in VIDEO_EXT:
            return "leave"
        if is_junk(name) and not has_other_videos:
            return "recycle"  # goes with the only video of the folder
        return "leave"

    def _cleanup_dirs(self, dirs):
        """After the moves: in every source folder that holds no video any
        more, junk goes to the recycle bin and empty folders are removed
        (bottom-up, never the source root). Returns the leftovers."""
        roots = self._cleanup_roots()
        leftovers = []
        for d in sorted(set(dirs), key=len, reverse=True):
            cur = d
            while cur and any(cur.startswith(r + "/") for r in roots) and os.path.isdir(cur):
                names = os.listdir(cur)
                sub_videos = any(os.path.splitext(f)[1].lower() in VIDEO_EXT
                                 for _, _, fs in os.walk(cur) for f in fs)
                if not sub_videos:
                    for n in names:
                        p = os.path.join(cur, n)
                        if os.path.isfile(p) and is_junk(n):
                            try:
                                self.recycle(p)
                            except Exception:
                                logger.exception("import: recycling %s failed", p)
                try:
                    os.rmdir(cur)  # only succeeds when empty
                except OSError:
                    left = [n for n in os.listdir(cur)] if os.path.isdir(cur) else []
                    if left:
                        leftovers.append({"folder": cur, "files": left})
                    break
                cur = os.path.dirname(cur)
        return leftovers

    # ---------- analysis ----------

    def _all_scenes(self):
        return self.stash._graphql(_IMPORT_SCENES_QUERY, {"f": {"per_page": -1}})["findScenes"]["scenes"]

    def _scenes_in(self, root):
        return [s for s in self._all_scenes()
                if any(f["path"].startswith(root + "/") for f in s["files"])]

    def sources(self):
        """[{root, scenes, with_id}] -- cheap overview for the UI."""
        scenes = self._all_scenes()
        out = []
        for root in self.roots():
            mine = [s for s in scenes if any(f["path"].startswith(root + "/") for f in s["files"])]
            out.append({"root": root, "scenes": len(mine), "with_id": sum(1 for s in mine if s["stash_ids"]),
                        "exists": os.path.isdir(self.to_local(root)), "signature": self.signature(mine)})
        return out

    def _identify(self, scenes, exact_only=False):
        """{file_id: {"provider", "matches"}} -- StashDB's reverse lookup, then ThePornDB's for what StashDB does not know. `exact_only` leaves out
        ThePornDB's PHASH lookup (similarity-based): a bulk assignment must rest on exact hashes (v0.243.2)."""
        pairs = []
        for s in scenes:
            f = s["files"][0]
            fps = [{"hash": x["value"], "algorithm": x["type"].upper()}
                   for x in f["fingerprints"] if x["type"] in ("oshash", "phash", "md5")]
            if fps:
                pairs.append((f["id"], fps))
        found = {}
        for i in range(0, len(pairs) if self.stashdb is not None else 0, _FP_BATCH):  # no StashDB set up (ThePornDB only): straight to ThePornDB
            chunk = pairs[i:i + _FP_BATCH]
            try:
                matches = self.stashdb.find_scenes_by_fingerprints([p[1] for p in chunk])
            except Exception:
                logger.exception("import: StashDB fingerprint lookup failed")
                matches = [None] * len(chunk)
            for (fid, _), m in zip(chunk, matches):
                found[fid] = {"provider": "stashdb", "matches": m or []}
            time.sleep(0.5)
        # ThePornDB for everything StashDB doesn't know (oshash first, then phash)
        if self.theporndb is not None:
            for s in scenes:
                f = s["files"][0]
                if found.get(f["id"], {}).get("matches"):
                    continue
                fp = {x["type"]: x["value"] for x in f["fingerprints"]}
                for hash_type, key in (("OSHASH", "oshash"),) if exact_only else (("OSHASH", "oshash"), ("PHASH", "phash")):
                    try:
                        m = _with_retry(self.theporndb.find_scenes_by_hash, fp.get(key), hash_type)
                    except Exception:
                        logger.exception("import: ThePornDB hash lookup failed for %s", f["path"])
                        m = []
                    if m:
                        found[f["id"]] = {"provider": "theporndb", "matches": m}
                        break
        return found

    def _provider_of(self, scene, stashdb_id=None):
        """(provider, provider_scene_id): StashDB first, then ThePornDB."""
        ids = {x["endpoint"]: x["stash_id"] for x in scene["stash_ids"]}
        if stashdb_id:
            return "stashdb", stashdb_id
        if self.stash.stashdb_endpoint in ids:
            return "stashdb", ids[self.stash.stashdb_endpoint]
        if self.stash.theporndb_endpoint in ids and self.theporndb is not None:
            return "theporndb", ids[self.stash.theporndb_endpoint]
        return None, None

    def _provider_scene(self, scene, stashdb_id=None):
        """(detail, studio_folder_name): title/date/studio from the scene's provider, the
        studio in the spelling the file names use (StashDB's wins, see file_importer)."""
        provider, sid = self._provider_of(scene, stashdb_id)
        if not sid:
            return None, None
        cache = self.__dict__.setdefault("_detail_cache", {})
        key = (provider, sid)
        if key in cache:
            return cache[key]
        client = self.stashdb if provider == "stashdb" else self.theporndb
        if client is None:
            return None, None
        detail = _with_retry(client.get_scene_detail, sid)
        if not detail or not detail.get("title") or not detail.get("date"):
            return detail, None
        studio = detail.get("studio") or {}
        name = None
        try:
            if provider == "stashdb":
                if studio.get("id"):
                    name = self.stashdb.get_studio_name(studio["id"])
            else:
                import file_importer
                name = file_importer._studio_name_for_naming(self.stash, self.stashdb, "theporndb",
                                                             self.theporndb, detail)
        except Exception:
            logger.exception("import: studio name for %s", studio)
        cache[key] = (detail, name or studio.get("name"))
        return cache[key]

    @staticmethod
    def signature(scenes):
        """A short fingerprint of everything an analysis depends on locally (files, ids, groups and their links, Organized), so a saved
        analysis can be told stale without running it again."""
        import hashlib
        parts = sorted(
            (str(s["id"]), tuple(sorted(f["path"] for f in s["files"])), bool(s.get("organized")),
             tuple(sorted(x["stash_id"] for x in s["stash_ids"])), any((t.get("name") or "") == REVIEW_TAG for t in s.get("tags") or []),
             tuple(sorted((e["group"]["id"], e.get("scene_index"), tuple(sorted(e["group"].get("urls") or []))) for e in s.get("groups") or [])))
            for s in scenes)
        return hashlib.sha1(repr(parts).encode()).hexdigest()[:16]

    def analyse(self, root):
        """Per-file classification of one source folder (see the module docstring)."""
        root = root.rstrip("/")
        scenes = self._scenes_in(root)
        out = self._analyse_scenes(root, scenes)
        out["signature"] = self.signature(scenes)
        return out

    def _analyse_scenes(self, root, candidates, identify=True):
        """The classification itself, over the given scenes (a source folder's, or the one scene of the "Organized" hook, which skips the
        provider fingerprint lookup: `identify=False`)."""
        scenes = [s for s in candidates if len(s["files"]) == 1]
        multi = [s for s in candidates if len(s["files"]) != 1]
        with_id = [s for s in scenes if any(x["endpoint"] in (self.stash.stashdb_endpoint, self.stash.theporndb_endpoint)
                                            for x in s["stash_ids"])]
        movie_scenes = [s for s in scenes if s not in with_id and len(s.get("groups") or []) == 1]
        without_id = [s for s in scenes if s not in with_id and s not in movie_scenes]
        verified = self._verify([(s, s["files"][0]) for s in with_id])
        found = self._identify(without_id) if identify else {}
        items = []
        for s in scenes:
            f = s["files"][0]
            item = {"scene_id": s["id"], "file_id": f["id"], "title": s["title"] or os.path.basename(f["path"]),
                    "path": f["path"], "new_path": None, "kind": "unknown", "how": "", "stashdb_id": None,
                    "provider": None, "provider_id": None, "vr": self._is_vr(s, f, root),
                    "in_review": any((t.get("name") or "") == REVIEW_TAG for t in s.get("tags") or [])}
            if s in movie_scenes:
                self._plan_movie(item, s, f)
            elif s in with_id:
                ok, how = verified.get(f["id"], (False, "not checked"))
                item["how"] = how
                item["kind"] = "ready" if ok else "review"
                # the provider cannot confirm the file by fingerprint: you may confirm it yourself ("Import anyway")
                item["can_trust"] = (not ok) and how.startswith(_TRUSTABLE)
                if item["can_trust"]:  # what the override would write over the scene, for the confirm
                    try:
                        detail, studio_name = self._provider_scene(s)
                        if detail:
                            item.update(provider_title=detail.get("title"), provider_studio=studio_name, provider_date=detail.get("date"))
                    except Exception:
                        logger.exception("import: provider lookup for the override hint failed (scene %s)", s["id"])
                item["provider"], item["provider_id"] = self._provider_of(s)
                if item["provider"] == "stashdb":
                    item["stashdb_id"] = item["provider_id"]
            else:
                hit = found.get(f["id"]) or {"provider": None, "matches": []}
                m, prov = hit["matches"], hit["provider"]
                label = "StashDB" if prov == "stashdb" else "ThePornDB"
                if len(m) == 1:
                    item.update(kind="identify", how=f"fingerprint ({label})", provider=prov, provider_id=m[0]["id"],
                                stashdb_id=m[0]["id"] if prov == "stashdb" else None,
                                title=m[0].get("title") or item["title"])
                elif len(m) > 1:
                    item.update(kind="review", how=f"{len(m)} {label} scenes share this fingerprint")
                else:
                    item["how"] = "no exact fingerprint match"
            if item["kind"] in ("ready", "identify"):
                self._plan_target(item, s, f)
            items.append(item)
        self._guard_duplicates(items)
        for s in multi:
            items.append({"scene_id": s["id"], "file_id": None, "title": s["title"] or "",
                          "path": s["files"][0]["path"], "kind": "review", "how": f"scene has {len(s['files'])} files",
                          "new_path": None, "stashdb_id": None, "vr": False})
        order = {"ready": 0, "movie": 1, "identify": 2, "review": 3, "unknown": 4}
        items.sort(key=lambda x: (order[x["kind"]], x["path"].lower()))
        counts = defaultdict(int)
        for i in items:
            counts[i["kind"]] += 1
        return {"root": root, "counts": dict(counts), "items": items}

    def _guard_duplicates(self, items):
        """Several files claiming the same provider scene (a compilation, episodes, copies) or a
        provider scene another LOCAL scene already holds must not be moved blindly -> review."""
        placed = [i for i in items if i["kind"] in ("ready", "identify") and i["provider_id"]]  # (movies have no provider id)
        by_pid = defaultdict(list)
        for i in placed:
            by_pid[(i["provider"], i["provider_id"])].append(i)
        for group in by_pid.values():
            if len(group) > 1:
                for i in group:
                    i.update(kind="review", how=f"{len(group)} files match the same provider scene "
                                                 f"({group[0]['title'][:40]!r}) -- copies, parts or a compilation?")
        by_path = defaultdict(list)
        for i in items:
            if i["kind"] in ("ready", "identify", "movie") and i["new_path"] and i["new_path"] != i["path"]:
                by_path[i["new_path"].lower()].append(i)
        for group in by_path.values():
            if len(group) > 1:
                for i in group:
                    i.update(kind="review", how="another file of this import has the same target name")
        for i in items:
            if i["kind"] != "identify" and not (i["kind"] == "ready" and i["provider_id"]):
                continue
            endpoint = self.stash.stashdb_endpoint if i["provider"] == "stashdb" else self.stash.theporndb_endpoint
            try:
                other = self.stash.find_scene_by_stashdb_id(i["provider_id"], endpoint)
            except Exception:
                continue
            if other and str(other["id"]) != str(i["scene_id"]):
                i.update(kind="review", how=f"the provider scene is already held by local scene {other['id']} "
                                             f"\"{other.get('title')}\" -- a duplicate?"
                                             + self._held_note(i, other["id"]))

    _HELD_FILES_QUERY = "query($id: ID!) { findScene(id: $id) { files { width height bit_rate } } }"

    @staticmethod
    def _quality_key(f):
        return ((f.get("width") or 0) * (f.get("height") or 0), f.get("bit_rate") or 0)

    def _held_note(self, item, held_id):
        """' (1920x1080 8.2 Mbit/s here, 1280x720 3.1 Mbit/s held: this one looks better)': pixels first, the bit rate for equal sizes.
        Empty when either side has no size. Only a hint, nothing is decided by it."""
        try:
            def files(scene_id):
                return (self.stash._graphql(self._HELD_FILES_QUERY, {"id": str(scene_id)}).get("findScene") or {}).get("files") or []
            mine = [f for f in files(item["scene_id"]) if f.get("width")]
            held = [f for f in files(held_id) if f.get("width")]
        except Exception:
            return ""
        if not mine or not held:
            return ""
        a, b = max(mine, key=self._quality_key), max(held, key=self._quality_key)

        def fmt(f):
            return f"{f['width']}x{f['height']}" + (f" {f['bit_rate'] / 1e6:.1f} Mbit/s" if f.get("bit_rate") else "")
        verdict = ("this one looks better" if self._quality_key(a) > self._quality_key(b)
                   else "this one is not better" if self._quality_key(a) < self._quality_key(b) else "equal quality")
        return f" ({fmt(a)} here, {fmt(b)} held: {verdict})"

    def _plan_movie(self, item, scene, file):
        """A scene in exactly one group (see the module docstring): kind 'movie' with its target path, or 'unknown'/'review' with the reason."""
        entry = scene["groups"][0]
        group = entry["group"]
        item["how"] = f"group \"{group['name']}\""
        tpdb_link = any(re.search(r"theporndb\.net/movies/", u or "") for u in group.get("urls") or [])
        item["group_id"] = group["id"]
        item["minutes"] = round((file.get("duration") or 0) / 60, 1)
        if not (tpdb_link or scene.get("organized")):
            item["how"] += " has no ThePornDB movie link and the scene is not marked Organized -- mark it Organized to confirm"
            return
        item["title"] = group["name"]
        count = group.get("scene_count") or 1
        part_number = None
        if count > 1:
            if entry.get("scene_index") is None:
                item.update(kind="review", how=item["how"] + f" has {count} scenes but this one has no scene number")
                return
            part_number = entry["scene_index"]
        name = file["path"].rsplit("/", 1)[1]
        quality = f"{quality_detect.guess_source(name)}-{quality_detect.resolution_label(file.get('height') or 0)}"
        title, year = film_names.split_title_year(group["name"], group.get("date"))
        tokens = film_names.movie_tokens(title, year, part_number, quality, (group.get("studio") or {}).get("name") or "",
                                         file_naming.naming_options(lambda key: self.setting(key) or "[]"))
        rel = film_names.render_movie_path(self.setting("movie_folder_format"), self.setting("movie_naming_format"), tokens, os.path.splitext(name)[1])
        item["new_path"] = f"{self._root()}/{rel.lstrip('/')}"
        item["kind"] = "movie"
        item["how"] += " (ThePornDB movie link)" if tpdb_link else " (marked Organized)"
        if item["new_path"] == file["path"]:
            item["how"] += " -- already in place"
        elif os.path.exists(self.to_local(item["new_path"])):
            item.update(kind="review", how=item["how"] + " -- the target name already exists")

    def _plan_target(self, item, scene, file):
        """Fills new_path (provider title/date/studio); demotes to review when it can't be built or is taken."""
        # A scene identified only at ThePornDB (no StashDB id, no id on the scene yet) would lose its provider here:
        # hand the matched ThePornDB id to the lookup.
        lookup = scene
        if item.get("provider") == "theporndb" and item.get("provider_id") and not item["stashdb_id"]:
            lookup = {**scene, "stash_ids": list(scene["stash_ids"]) + [{"endpoint": self.stash.theporndb_endpoint,
                                                                          "stash_id": item["provider_id"]}]}
        try:
            detail, studio = self._provider_scene(lookup, item["stashdb_id"])
        except Exception:
            logger.exception("import: provider lookup failed for scene %s", scene["id"])
            detail, studio = None, None
        if not detail or not studio or not detail.get("title") or not detail.get("date"):
            item.update(kind="review", how=(item["how"] + "; " if item["how"] else "") + "provider has no title/date/studio")
            return
        # A NEAR phash match is weaker evidence: a file older than the scene's release
        # can't be that scene (see Organizer.plan); an exact one (distance 0) can.
        how = item["how"]
        if how.startswith("phash") and not how.startswith("phash (distance 0)") \
                and _older_than_release(file, detail["date"]):
            item.update(kind="review", how=f"{how}, but the file is older than the scene's release date", can_trust=True,
                        provider_title=detail["title"], provider_studio=studio, provider_date=detail["date"])
            return
        item["title"] = detail["title"]
        item["studio"] = studio
        item["new_path"] = self.expected_path({"title": detail["title"], "date": detail["date"], "tags": scene["tags"]},
                                              file, studio)
        if item["new_path"] == file["path"]:
            item.update(kind="ready", how=item["how"] + " (already in place)")
        elif os.path.exists(self.to_local(item["new_path"])):
            item.update(kind="review", how="target name already exists")

    def _is_vr(self, scene, file, root):
        if any((t.get("name") or "").lower() in (VR_TAG_NAME.lower(), "vr") for t in scene.get("tags") or []):
            return True
        if "vr" in os.path.basename(root).lower():
            return True
        return bool(quality_detect.vr_label(file.get("width"), file.get("height")))

    # ---------- finish a source (temporary folder is done) ----------

    def finish_check(self, root):
        """What would "finish this source" do, and what stands in the way? Never changes anything."""
        root = root.rstrip("/")
        report = {"root": root, "videos": [], "junk": [], "unknown": [], "dirs": 0, "stash_scenes": 0,
                  "problems": [], "can_finish": False}
        if root not in self.roots():
            report["problems"].append("not an import source")
            return report
        if os.path.basename(root) == "scenes" or root.rstrip("/") == self._root() + "/scenes":
            report["problems"].append("this is the library itself (scenes/), never a temporary folder")
            return report
        local = self.to_local(root)
        if not os.path.isdir(local):
            report["problems"].append("folder not found")
            return report
        for dirpath, dirs, files in os.walk(local):
            report["dirs"] += len(dirs)
            for n in files:
                rel = os.path.join(dirpath, n)[len(local) + 1:]
                if os.path.splitext(n)[1].lower() in VIDEO_EXT:
                    report["videos"].append(rel)
                elif is_junk(n):
                    report["junk"].append(rel)
                else:
                    report["unknown"].append(rel)  # .funscript/.srt without their video count as leftovers too
        report["stash_scenes"] = len(self._scenes_in(root))
        if report["videos"]:
            report["problems"].append(f"{len(report['videos'])} video file(s) still in the folder")
        if report["unknown"]:
            report["problems"].append(f"{len(report['unknown'])} file(s) of a type I don't know how to handle")
        if report["stash_scenes"]:
            report["problems"].append(f"Stash still lists {report['stash_scenes']} scene(s) below it (run Clean)")
        report["can_finish"] = not report["problems"]
        return report

    def finish_source(self, root, backup_dir):
        """Junk to the recycle bin, empty subfolders removed, the path removed from Stash's libraries
        (the old list is saved first), the empty folder itself moved into the recycle bin."""
        report = self.finish_check(root)
        if not report["can_finish"]:
            return {**report, "done": False}
        root = report["root"]
        local = self.to_local(root)
        for rel in report["junk"]:
            self.recycle(os.path.join(local, rel))
        for dirpath, _dirs, _files in os.walk(local, topdown=False):
            if dirpath != local:
                os.rmdir(dirpath)  # only succeeds when empty
        if os.listdir(local):
            raise RuntimeError(f"{root} is not empty after cleaning: {os.listdir(local)[:5]}")
        libs = self.stash.library_config()
        import json
        os.makedirs(backup_dir, exist_ok=True)
        backup = os.path.join(backup_dir, f"stash_libraries_before_finish_{int(time.time())}.json")
        with open(backup, "w") as f:
            json.dump(libs, f, indent=1)
        remaining = [s for s in libs if s["path"].rstrip("/") != root]
        after = self.stash.set_library_config(remaining)
        if any(s["path"].rstrip("/") == root for s in after) or len(after) != len(remaining):
            raise RuntimeError(f"Stash's library list looks wrong after removing {root}; the old list is in {backup}")
        self.recycle(local)  # the empty folder itself
        self.save_roots([r for r in self.roots() if r != root])
        history.add("info", "history", "import", f"Source folder finished: {root} removed from Stash's libraries, "
                    f"the empty folder is in the recycle bin", details={"backup": backup, "junk_recycled": len(report["junk"])})
        return {**report, "done": True, "backup": backup, "libraries_left": [s["path"] for s in after]}

    # ---------- name-based suggestions (files no fingerprint knows) ----------

    _NOISE = re.compile(r"(?i)\b(\d{3,4}p|\dk|xxx|mp4|wmv|avi|flv|mkv|mpg|mpeg|asf|hdtv|dvdrip|web-?dl|webrip|hevc|x26[45]|h26[45]|"
                        r"cd\s?\d|part\s?\d+|pt\s?\d+|disc\s?\d|www|com|net|org|hd|full|scene|video|by|vol)\b")
    _STOP = {"the", "and", "with", "for", "her", "his", "from", "that", "this", "into", "over"}

    @classmethod
    def query_tokens(cls, path, root):
        """Search words from the file name and the folders below the source root."""
        rel = os.path.splitext(path[len(root):].strip("/"))[0]
        parts = [p for p in rel.split("/") if p and p.lower() not in ("__moving", "moving")]
        text = " ".join(parts[-2:])  # folder + file name
        text = re.sub(r"[._\-\[\]\(\)]+", " ", text)
        text = cls._NOISE.sub(" ", text)
        text = re.sub(r"\b[0-9a-f]{12,}\b|\b\d{5,}\b", " ", text)  # hashes / long numbers
        words = [w for w in re.findall(r"[^\W_]{2,}", text.lower())]
        seen, out = set(), []
        for w in words:
            if w not in seen and w not in cls._STOP:
                seen.add(w)
                out.append(w)
        return out

    @staticmethod
    def _cand_words(c):
        text = " ".join([c.get("title") or "", (c.get("studio") or {}).get("name") or "",
                         " ".join(p.get("name") or "" for p in c.get("performers") or [])])
        return {w for w in re.findall(r"[^\W_]{2,}", text.lower())}

    def _score(self, tokens, c, file_minutes):
        """Overlap of the DISTINCTIVE words (not the studio's own words) with title/performers;
        a matching studio only adds a little, a matching duration adds more. 0 when no distinctive
        word matches at all -- a studio match alone says nothing about which scene it is."""
        if not tokens:
            return 0.0
        studio_words = {w for w in re.findall(r"[^\W_]{2,}", ((c.get("studio") or {}).get("name") or "").lower())}
        studio_hit = bool(studio_words) and (studio_words <= set(tokens) or
                                             "".join(sorted(studio_words)) in "".join(sorted(tokens)))
        rest = [t for t in tokens if t not in studio_words and not any(t in w or w in t for w in studio_words if len(w) > 3)]
        words = self._cand_words({**c, "studio": {}})
        if not rest:
            return 0.1 if studio_hit else 0.0
        hit = sum(1 for t in rest if t in words) / len(rest)
        if hit == 0:
            return 0.0
        score = hit * 0.7 + (0.1 if studio_hit else 0)
        dur = c.get("duration")
        if dur and file_minutes:
            ratio = abs(file_minutes * 60 - dur) / dur
            score += 0.3 if ratio <= 0.03 else 0.15 if ratio <= 0.10 else -0.25 if ratio > 0.35 else 0
        return round(max(score, 0.0), 2)

    def suggest(self, root, scene_ids):
        """{scene_id: [candidates]} for files nothing identified exactly: search StashDB and
        ThePornDB with words from the file/folder name, rank by word overlap and duration."""
        root = root.rstrip("/")
        by_id = {str(s["id"]): s for s in self._scenes_in(root)}
        out = {}
        for sid in scene_ids or []:
            s = by_id.get(str(sid))
            if not s:
                continue
            f = s["files"][0]
            tokens = self.query_tokens(f["path"], root)
            minutes = round((f.get("duration") or 0) / 60, 1) if f.get("duration") else None
            cands = []
            if tokens:
                term = " ".join(tokens[:6])
                for prov, client in (("stashdb", self.stashdb), ("theporndb", self.theporndb)):
                    if client is None:
                        continue
                    try:
                        results = _with_retry(client.search_scenes, term, 25)["results"]
                    except Exception:
                        logger.exception("import: %s search failed for %r", prov, term)
                        continue
                    for c in results:
                        cands.append({"provider": prov, "provider_id": c["id"], "title": c.get("title"), "date": c.get("date"),
                                      "studio": (c.get("studio") or {}).get("name"),
                                      "performers": [p.get("name") for p in (c.get("performers") or [])][:4],
                                      "minutes": round(c["duration"] / 60, 1) if c.get("duration") else None,
                                      "score": self._score(tokens, c, minutes)})
            cands = [c for c in cands if c["score"] > 0]
            cands.sort(key=lambda x: -x["score"])
            out[str(sid)] = {"query": " ".join(tokens), "minutes": minutes, "candidates": cands[:4]}
            time.sleep(0.3)
        return out

    # ---------- apply ----------

    _SCENE_BACKUP_QUERY = """query($id: ID!) { findScene(id: $id) { id title code date details urls rating100 organized
        studio { id name } performers { id name } tags { id name } stash_ids { endpoint stash_id }
        groups { scene_index group { id name } } files { path } } }"""

    def _backup_scene(self, scene_id, why):
        """The scene's metadata as it is BEFORE the provider's data is applied (v0.228.2: an "Import anyway" overwrote a hand-made
        title/performer set and nothing could bring it back). One JSON file per scene and time in the backups folder; its path goes to
        History. Never stops the import: a failed backup is logged and the import goes on only when `why` is not a user override."""
        backup_dir = getattr(self, "backup_dir", None)
        if not backup_dir:
            return None
        scene = self.stash._graphql(self._SCENE_BACKUP_QUERY, {"id": str(scene_id)}).get("findScene")
        os.makedirs(backup_dir, exist_ok=True)
        path = os.path.join(backup_dir, f"scene_{scene_id}_before_{why}_{int(time.time())}.json")
        import json
        with open(path, "w") as f:
            json.dump(scene, f, indent=1, ensure_ascii=False)
        return path

    def _adopt(self, item):
        """Applies the provider's metadata (title/date/studio/performers/ids) and, for VR
        files, the VR tag. Idempotent for scenes that already carry the right id. The scene's metadata is saved first (_backup_scene)."""
        scene_id = item["scene_id"]
        item["backup"] = self._backup_scene(scene_id, "import-trusted" if item.get("trusted_reason") else "import")
        endpoint = self.stash.stashdb_endpoint if item["provider"] == "stashdb" else self.stash.theporndb_endpoint
        library_check.apply_fix(self.stash, scene_id, item["provider_id"], create_missing_performers=True,
                                endpoint=endpoint, keep_other_endpoints=True)
        if item.get("vr"):
            tag_id = self.stash.ensure_tag(VR_TAG_NAME)
            cur = self.stash._graphql("query($id: ID!) { findScene(id: $id) { tags { id } } }",
                                      {"id": str(scene_id)})["findScene"]["tags"]
            ids = [t["id"] for t in cur]
            if str(tag_id) not in ids:
                self.stash._graphql("mutation($i: SceneUpdateInput!) { sceneUpdate(input: $i) { id } }",
                                    {"i": {"id": str(scene_id), "tag_ids": ids + [str(tag_id)]}})

    def apply(self, root, scene_ids, picks=None, force=False, ignore_renamers=False, trust=None):
        """Re-analyses (never trusts the browser), then adopts + moves the requested
        ready/identify scenes. `picks` = {scene_id: {"provider", "provider_id"}}: matches YOU
        confirmed from the name suggestions for files no fingerprint identified. Returns
        {done, failed, leftovers}. `trust` = scene ids YOU checked by hand although the provider cannot confirm the file by
        fingerprint (v0.228.0, "Import anyway"): such a scene (a provider id on it, a failed fingerprint check as the only problem)
        is placed like a verified one and the waiver is written to History."""
        busy = self.queue_guard(force)
        if busy:
            return busy
        conflict = self.renamer_guard(ignore_renamers)
        if conflict:
            return conflict
        wanted = {str(i) for i in scene_ids or []}
        plan = self.analyse(root)
        if picks:
            scenes = {str(s["id"]): s for s in self._scenes_in(plan["root"])}
            for item in plan["items"]:
                pick = (picks or {}).get(str(item["scene_id"]))
                if not pick or item["kind"] not in ("unknown", "review") or str(item["scene_id"]) not in scenes:
                    continue
                if pick.get("provider") not in ("stashdb", "theporndb") or not pick.get("provider_id"):
                    continue
                s = scenes[str(item["scene_id"])]
                item.update(kind="identify", provider=pick["provider"], provider_id=pick["provider_id"],
                            stashdb_id=pick["provider_id"] if pick["provider"] == "stashdb" else None,
                            how="confirmed by you (name match)")
                self._plan_target(item, s, s["files"][0])
                wanted.add(str(item["scene_id"]))
        trusted = []
        if trust:
            by_id = {str(s["id"]): s for s in self._scenes_in(plan["root"])}
            for item in plan["items"]:
                sid = str(item["scene_id"])
                if sid not in {str(t) for t in trust} or not item.get("can_trust") or item["kind"] != "review" or sid not in by_id:
                    continue
                reason = item["how"]
                item.update(kind="ready", how="confirmed by you (fingerprint not verified)", trusted_reason=reason)
                self._plan_target(item, by_id[sid], by_id[sid]["files"][0])
                if item["kind"] == "ready" and item["new_path"]:
                    wanted.add(sid)
                    trusted.append(item)
        items = [i for i in plan["items"] if i["kind"] in ("ready", "identify", "movie")
                 and str(i["scene_id"]) in wanted and i["new_path"]]
        result = self._apply_items(plan["root"], items)
        done_ids = {str(i["scene_id"]) for i in result.get("done", [])}
        for item in trusted:
            if str(item["scene_id"]) in done_ids:
                history.add("warning", "history", "import",
                            f"Imported without fingerprint check (you confirmed it): {item['path'].rsplit('/', 1)[1]} -- {item['trusted_reason']}",
                            scene_title=item["title"], details={"stash_scene": item["scene_id"], "provider": item.get("provider"),
                                                                "provider_id": item.get("provider_id"), "backup": item.get("backup")})
        return result

    def _apply_items(self, root_label, items):
        """Adopts (not for movie scenes) and moves the planned items, scans, records. Returns {done, failed, leftovers}."""
        done, failed, old_dirs, batch_folders = [], [], [], []
        for item in items:
            try:
                if item["kind"] != "movie":  # a movie scene keeps the metadata you gave it (scraper / group)
                    self._adopt(item)
                if item["new_path"] != item["path"]:
                    old_dirs.append(self._move_one(item))
                    batch_folders.append(item["new_path"].rsplit("/", 1)[0])
            except Exception as e:
                logger.exception("import: %s failed", item["path"])
                failed.append({**item, "reason": str(e)})
                continue
            done.append(item)
        self._scan_and_wait(batch_folders)
        confirmed = []
        for item in done:
            scene = self.stash.find_scene_by_path(item["new_path"]) if item["new_path"] != item["path"] else {"id": item["scene_id"]}
            if scene is not None and str(scene["id"]) == str(item["scene_id"]):
                item["whisparr"] = self.sync_whisparr(item) if item.get("stashdb_id") else "not in Whisparr"
                self._record_move(item["path"], item["new_path"])
                confirmed.append(item)
                history.add("info", "history", "import",
                            f"Imported from {root_label}: {item['path'].rsplit('/', 1)[1]} -> "
                            f"{item['new_path'][len(self._root()):]}"
                            + {"identify": " (identified by fingerprint)", "movie": " (movie scene, by its group)"}.get(item["kind"], ""),
                            scene_title=item["title"],
                            details={"stash_scene": item["scene_id"], "old": item["path"], "new": item["new_path"],
                                     "extras": item.get("extras_moved"), "vr_tag": item.get("vr")})
            else:
                failed.append({**item, "reason": "moved on disk, but Stash doesn't show the scene there yet"})
        leftovers = self._cleanup_dirs(old_dirs)
        self._watch_drift([i for i in confirmed if i["new_path"] != i["path"]])
        history.event("warning" if failed else "info", "import",
                      f"Import {root_label}: {len(confirmed)} done, {len(failed)} failed")
        return {"done": confirmed, "failed": failed, "leftovers": leftovers}

    # ---------- the "Organized" hook (v0.191.0) ----------

    def organized_scene(self, scene_id, mode):
        """You marked a scene Organized in Stash (plugin hook Scene.Update.Post, see routers/maintenance.py): with the setting organized_hook
        'preview' the planned move is only written to History, with 'on' it is done. Handled: a movie scene (one group, Organized counts as
        the confirmation) anywhere in the library, and a source-folder scene that is 'ready' (provider id + matching fingerprints). Everything
        else is left alone and History says why. The scene is re-read here (the event is never trusted) and must still be Organized."""
        if mode not in ("preview", "on"):
            return {"skipped": "off"}
        scene = self._scene_by_id(scene_id)
        if scene is None or not scene.get("organized"):
            return {"skipped": "not organized"}
        title = scene.get("title") or f"scene {scene_id}"
        if len(scene["files"]) != 1:
            return self._hook_note(scene, "has several files -- not moved", mode)
        path = scene["files"][0]["path"]
        if not path.startswith(self._root() + "/") or "/_UNMATCHED/" in path:
            return self._hook_note(scene, "is outside the library or parked in _UNMATCHED -- not moved", mode)
        root = next((r for r in self.roots() if path.startswith(r + "/")), path.rsplit("/", 1)[0])
        plan = self._analyse_scenes(root, [scene], identify=False)
        item = plan["items"][0]
        if item["kind"] not in ("movie", "ready"):
            # a scene of the Whisparr library folder (scenes/) with a provider id: the Organize library engine plans it (v0.203.0)
            plain = Organizer(self.db, self.stash, self.stashdb, self.theporndb, self.to_local, self.setting, get_whisparr_client=self.get_whisparr)
            plain.DRIFT_WATCH_SECONDS = self.DRIFT_WATCH_SECONDS
            handled = plain.organized_library_scene(scene, mode)
            if handled.get("handled"):
                if handled.get("note"):
                    return self._hook_note(scene, handled["note"], mode)
                return handled
            return self._hook_note(scene, f"was not moved: {item['how'] or item['kind']}", mode)
        if not item["new_path"] or item["new_path"] == path:
            return self._hook_note(scene, "is already where it belongs", mode)
        if mode == "preview":
            history.add("info", "history", "import", f"Organized preview (nothing moved): {path[len(self._root()):]} -> {item['new_path'][len(self._root()):]}",
                        scene_title=title, details={"stash_scene": scene_id, "old": path, "new": item["new_path"], "kind": item["kind"]})
            return {"preview": item}
        conflict = self.renamer_guard(False)
        if conflict:  # another renamer is enabled: the automatic move is held back, the plan stays visible in History
            history.add("warning", "history", "import", f"Organized preview (NOT moved, another renamer is enabled): {path[len(self._root()):]} -> "
                        f"{item['new_path'][len(self._root()):]}", scene_title=title,
                        details={"stash_scene": scene_id, "old": path, "new": item["new_path"], "renamers": conflict["renamers"]})
            return {"blocked": conflict["error"], "preview": item}
        busy = self.queue_guard(False)
        if busy:
            self._hook_note(scene, "was not moved: " + busy["error"], mode)
            return busy
        return self._apply_items(f"Organized ({root})", [item])

    def _hook_note(self, scene, text, mode):
        history.add("info", "history", "import", f"Marked Organized, {text}", scene_title=scene.get("title") or f"scene {scene['id']}",
                    details={"stash_scene": scene["id"], "mode": mode})
        return {"note": text}

    def _scene_by_id(self, scene_id):
        found = self.stash._graphql(_IMPORT_SCENES_QUERY, {"f": {"per_page": 1}, "s": {"id": {"value": int(scene_id), "modifier": "EQUALS"}}})["findScenes"]["scenes"]
        return found[0] if found else None
