"""A wrongly downloaded file is identified (which provider scene it is) and can be placed for that scene when it is on the wishlist (v0.260.0)."""
import json, os, sys, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "api"))
os.environ["DATA_DIR"] = tempfile.mkdtemp()
import file_importer as fi, release_repo, history  # noqa: E402
from db import Database  # noqa: E402

fails = []
def check(name, ok):
    print(("ok   " if ok else "FAIL ") + name)
    if not ok:
        fails.append(name)

history._db = None
db = Database(os.path.join(os.environ["DATA_DIR"], "t.sqlite3"))
PATH = "/media/scenes/A/wrong.mp4"

def add(sid, status, **kw):
    with db.connect() as conn:
        conn.execute("""INSERT INTO release (provider, scene_stashdb_id, entity_type, entity_stashdb_id, title, scene_created, scene_date, studio_name, status,
                        discovered_at, pending_import_path, import_error_code, grabbed_title) VALUES ('stashdb', ?, 'studio', 'e', ?, '2020-01-01T00:00:00Z', '2020-01-01',
                        'S', ?, datetime('now'), ?, ?, ?)""", (sid, kw.get("title", sid), status, kw.get("path"), kw.get("code"), kw.get("grabbed")))

add("A", "import_failed", path=PATH, code="fingerprint_mismatch", grabbed="Wrong.Release.1080p")
add("B", "sent_to_whisparr", title="Scene B")
add("C", "imported", title="Scene C")

class Stash:
    stashdb_endpoint = "https://stashdb.org/graphql"
    in_library = {}
    def stash_ids_in_library(self, endpoint, ids): return {i: self.in_library[i] for i in ids if i in self.in_library}
    def find_scene_by_path(self, path):
        return {"id": "77", "files": [{"path": PATH, "fingerprints": [{"type": "oshash", "value": "ab"}, {"type": "phash", "value": "cd"}, {"type": "duration", "value": "9"}]}]}

class Provider:
    def __init__(self, matches): self.matches, self.calls = matches, []
    def find_scenes_by_fingerprints(self, sets):
        self.calls.append(sets)
        return [self.matches]

release_repo.set_grabbed_info_for_scene(db, "stashdb", "A", {"title": "Wrong.Release.1080p", "indexer": "Idx", "protocol": "usenet", "downloadUrl": "http://i/get?apikey=SECRET", "infoUrl": "http://i/d", "size": 123})
p = Provider([{"id": "A", "title": "Scene A"}, {"id": "C", "title": "Scene C"}, {"id": "B", "title": "Scene B", "studio": {"name": "Studio B"}}])
found = fi.identify_wrong_file(db, Stash(), p, "stashdb", "A", PATH)
check("the scene we are looking for is preferred, the scene itself is skipped", found == {"id": "B", "title": "Scene B", "studio": "Studio B", "list": "wishlist", "source": "provider"})
check("only oshash / phash / md5 are sent", [x["algorithm"] for x in p.calls[0][0]] == ["OSHASH", "PHASH"])
row = release_repo.list_rows_for_scene(db, "stashdb", "A")[0]
check("stored on the release", json.loads(row["mismatch_found"])["id"] == "B")
check("a known scene that is not wanted: list None", fi.identify_wrong_file(db, Stash(), Provider([{"id": "C", "title": "Scene C"}]), "stashdb", "A", PATH)["list"] is None)
class OtherStash(Stash):
    def find_scene_by_path(self, path):
        return {"id": "78", "files": [{"path": PATH, "fingerprints": [{"type": "oshash", "value": "new1"}, {"type": "phash", "value": "new2"}]}]}
check("nothing found -> None", fi.identify_wrong_file(db, OtherStash(), Provider([{"id": "A", "title": "x"}]), "stashdb", "A", PATH) is None)
neg = json.loads(release_repo.list_rows_for_scene(db, "stashdb", "A")[0]["mismatch_found"])
check("an empty check is stored too (id None, checked, name_fits is a bool)", neg["id"] is None and neg["checked"] and isinstance(neg["name_fits"], bool))
check("stored_found ignores an empty check", fi.stored_found({"mismatch_found": json.dumps(neg)}) is None)
# the provider knows nothing, but we know a release with the same hashes (our own table)
own = fi.identify_wrong_file(db, Stash(), Provider([]), "stashdb", "A", PATH)
check("own known releases: same hashes -> that scene, source 'known'", own and own["id"] == "B" and own["source"] == "known" and own["list"] == "wishlist")

import known_release
kr = known_release.for_scene(db, "stashdb", "B")
check("the wrong file is collected as a known release of the scene it is", len(kr) == 1 and kr[0]["release_title"] == "Wrong.Release.1080p"
      and kr[0]["oshash"] == "ab" and kr[0]["phash"] == "cd" and kr[0]["source"] == "mismatch")
fi.identify_wrong_file(db, Stash(), p, "stashdb", "A", PATH)
check("how it was fetched is kept too (indexer, URL, size)", kr[0]["indexer"] == "Idx" and kr[0]["download_url"].endswith("SECRET") and kr[0]["release_size"] == 123 and kr[0]["protocol"] == "usenet")
check("the grab info is not a column of the release rows (they go to the browser)", all("SECRET" not in str(v) for v in release_repo.list_rows_for_scene(db, "stashdb", "A")[0].values()))
check("asking again does not duplicate it", len(known_release.for_scene(db, "stashdb", "B")) == 1)
check("nothing is collected without a release name", known_release.record(db, Stash(), "stashdb", "B", PATH, None, "import") is False)

# place the file for B
fi.identify_wrong_file(db, Stash(), p, "stashdb", "A", PATH)
tagged, organized = [], []
fi.library_check.apply_fix = lambda stash, scene_id, target, **kw: tagged.append((scene_id, target))
fi.organize_scene_hook = lambda sid: organized.append(sid) or {"done": [{"path": "x"}], "failed": []}
fi._trigger_generate = lambda *a, **k: None
fi._after_import = lambda *a, **k: None
fi._provider_client = lambda provider, sdb, getp: p
res = fi.place_file_for_other_scene(db, Stash(), None, "stashdb", "A")
check("placed: ok", res.get("ok") and res["title"] == "Scene B")
check("the Stash scene is tagged with scene B and organised", tagged == [("77", "B")] and organized == ["77"])
a, b = release_repo.list_rows_for_scene(db, "stashdb", "A")[0], release_repo.list_rows_for_scene(db, "stashdb", "B")[0]
check("B counts as imported", b["status"] == "imported")
check("A is back on the wishlist, the wrong download blocklisted, nothing pending",
      a["status"] == "sent_to_whisparr" and a["pending_import_path"] is None and a["import_error_code"] is None and a["mismatch_found"] is None
      and "Wrong.Release.1080p" in (release_repo.get_review_rejected_titles(db, "stashdb", "A") or []))
res = fi.place_file_for_other_scene(db, Stash(), None, "stashdb", "A")
check("a second click refuses (no failed import any more)", "error" in res)

# not wanted, but in the library already -> refused; not in the library either -> the file is placed (v0.260.18)
add("D", "import_failed", path=PATH, code="fingerprint_mismatch")
release_repo.set_mismatch_found_for_scene(db, "stashdb", "D", {"id": "C", "title": "Scene C", "list": None})
class HasC(Stash):
    in_library = {"C": "5"}
check("a scene that already has a file is refused", "error" in fi.place_file_for_other_scene(db, HasC(), None, "stashdb", "D"))
tagged.clear()
res = fi.place_file_for_other_scene(db, Stash(), None, "stashdb", "D")
check("a scene that is neither wanted nor in the library takes the file", res.get("ok") and tagged == [("77", "C")])
check("the failed row is back on the wishlist", release_repo.list_rows_for_scene(db, "stashdb", "D")[0]["status"] == "sent_to_whisparr")
print("FAILED: " + ", ".join(fails) if fails else "ALL OK")
sys.exit(1 if fails else 0)
