"""Tests (v0.246.0): a file registered at SEVERAL provider scenes is not "verified" just because the tag is one of them (shared_fingerprint.py,
Organizer._verify, library_check.find_library_mismatches, the review list). Fake clients and a throwaway DB. Run: venv/bin/python scripts/test_shared_fingerprint.py"""
import os, sys, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))
os.environ["DATA_DIR"] = tempfile.mkdtemp()
import library_check, library_organize, shared_fingerprint as sf
from db import Database

FAILS = []
def check(name, cond):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond: FAILS.append(name)

DB = Database(os.path.join(tempfile.mkdtemp(), "t.sqlite3"))
M = lambda i, t="x": {"id": i, "title": t}  # noqa: E731

# ---- judge
D = {"tag": 2278, "wank": 3065, "na": None, "other": 2270}
check("one scene only: unique", sf.judge(2261, "tag", [M("tag")], D)["reason"] == "unique")
v = sf.judge(2261, "tag", [M("tag"), M("wank")], D)
check("several scenes, the length fits only the tag: ok", v["ok"] and v["reason"] == "length_fits" and v["how"].startswith("fingerprint"))
v = sf.judge(2261, "wank", [M("wank"), M("tag"), M("na")], D)
check("the tag's length does not fit, another scene's does: not ok (other_fits)", not v["ok"] and v["reason"] == "other_fits")
v = sf.judge(2261, "na", [M("na"), M("wank")], D)
check("the tag has no length at the provider: not ok (no_length)", not v["ok"] and v["reason"] == "no_length")
v = sf.judge(2261, "tag", [M("tag"), M("other")], D)
check("the length fits the tag AND another scene: not ok (also_fits)", not v["ok"] and v["reason"] == "also_fits")
v = sf.judge(5000, "tag", [M("tag"), M("wank")], D)
check("the length fits nothing: not ok (tag_off)", not v["ok"] and v["reason"] == "tag_off")
check("the user kept the tag: ok", sf.judge(5000, "tag", [M("tag"), M("wank")], D, kept=True)["ok"])
check("how of a not-ok verdict never starts with 'fingerprint' (Organize skips the release-date check only for exact matches)", not sf.judge(5000, "tag", [M("tag"), M("wank")], D)["how"].startswith("fingerprint"))
check("tolerance: 6 % is fine, 8 % is not", sf.fits(1000, 940) and not sf.fits(1000, 920))

# ---- the review list
sf.upsert(DB, "6407", "Scene", "/data/a.mp4", 2261, "stashdb", "wank", "other_fits", "how", [{"id": "wank"}, {"id": "tag"}])
check("upsert: open", [r["scene_id"] for r in sf.rows(DB)] == ["6407"] and sf.open_count(DB) == 1)
sf.set_status(DB, "6407", "kept")
sf.upsert(DB, "6407", "Scene", "/data/a.mp4", 2261, "stashdb", "wank", "other_fits", "how", [])
check("a scene the user kept for this tag stays settled", sf.counts(DB) == {"kept": 1} and sf.is_kept(DB, "6407", "wank"))
sf.upsert(DB, "6407", "Scene", "/data/a.mp4", 2261, "stashdb", "tag", "other_fits", "how", [])
check("a new tag reopens it", sf.counts(DB) == {"open": 1})
sf.upsert(DB, "7", "Other", "/data/b.mp4", 100, "stashdb", "t", "no_length", "h", [])
check("close_unseen settles open rows a scan did not find again", sf.close_unseen(DB, {"7"}, "stashdb") == 1 and sf.counts(DB) == {"open": 1, "resolved": 1})

sf.upsert(DB, "9", "Nine", "/data/9.mp4", 100, "stashdb", "old", "no_length", "h", [])
sf.set_status(DB, "9", "retagged", note="old", tag_id="new")
sf.upsert(DB, "9", "Nine", "/data/9.mp4", 100, "stashdb", "new", "no_length", "h", [])
check("after 'Use this scene' the row stays settled for the NEW tag (and is trusted by Organize)", sf.counts(DB).get("retagged") == 1 and sf.is_kept(DB, "9", "new") and not sf.is_kept(DB, "9", "old"))

# ---- library_check.find_library_mismatches with the judge
class Client:
    def __init__(self, table): self.table = table
    def find_scenes_by_fingerprints(self, sets): return [self.table.get(s[0]["hash"], []) for s in sets]
    def scene_durations(self, ids): return {i: D.get(i) for i in ids}
    def get_scene_detail(self, i): return {"title": i, "date": "2020-01-01", "studio": {"name": "S-" + i}}
def scene(i, tag, h, secs): return {"id": i, "title": f"t{i}", "stash_ids": [{"endpoint": "ep", "stash_id": tag}], "files": [{"path": f"/data/{i}.mp4", "duration": secs, "fingerprints": [{"type": "oshash", "value": h}]}]}
client = Client({"h1": [M("tag"), M("wank")], "h2": [M("wank"), M("tag"), M("na")], "h3": [M("tag")]})
scenes = [scene("1", "tag", "h1", 2261), scene("2", "wank", "h2", 2261), scene("3", "tag", "h3", 2261)]
judge = sf.make_judge(DB, client, "stashdb")
matches, mismatches, ambiguous, unver = library_check.find_library_mismatches(client, scenes, "ep", delay=0, judge=judge)
check("check page: unique and length-confirmed files are matches", sorted(m["id"] for m in matches) == ["1", "3"])
check("check page: the shared one the length cannot confirm is 'ambiguous' with the reason", [a["id"] for a in ambiguous] == ["2"] and "does not fit" in ambiguous[0]["reason"])
check("check page: it also lands on the review list, with studio and length of the candidates", any(r["scene_id"] == "2" and r["candidates"][0]["studio"] for r in sf.rows(DB)))
matches, _, ambiguous, _ = library_check.find_library_mismatches(client, scenes, "ep", delay=0)
check("without a judge the old behaviour stays (tag among the matches = match)", len(matches) == 3 and not ambiguous)

# ---- Organizer._verify
class Stash:
    stashdb_endpoint = "ep"; theporndb_endpoint = "tp"
org = library_organize.Organizer.__new__(library_organize.Organizer)
org.stash, org.stashdb, org.theporndb, org.db, org.extra_providers = Stash(), client, None, DB, []
def vscene(i, tag): return {"id": i, "title": f"t{i}", "stash_ids": [{"endpoint": "ep", "stash_id": tag}]}
def vfile(i, h, secs): return {"id": f"f{i}", "path": f"/data/{i}.mp4", "duration": secs, "fingerprints": [{"type": "oshash", "value": h}]}
res = org._verify([(vscene("1", "tag"), vfile("1", "h1", 2261)), (vscene("2", "wank"), vfile("2", "h2", 2261)), (vscene("3", "tag"), vfile("3", "h3", 2261))])
check("Organize: a unique match is verified", res["f3"] == (True, "fingerprint"))
check("Organize: shared, the length confirms the tag: verified", res["f1"][0] and res["f1"][1].startswith("fingerprint"))
check("Organize: shared, the length does not: NOT verified (goes to 'needs check')", res["f2"][0] is False and "ambiguous" in res["f2"][1])

# ---- the scan
class ScanStash(Stash):
    def all_scenes_with_stashdb_id(self, endpoint): return scenes
org.stash = ScanStash()
org.boxes = lambda: [("stashdb", client)]
org.endpoint_of = lambda k: "ep"
org.name_of = lambda k: "StashDB"
library_organize.time.sleep = lambda s: None
with DB.connect() as conn:
    conn.execute("CREATE TABLE IF NOT EXISTS organize_state (key TEXT PRIMARY KEY, value TEXT)")
org._shared_scan_run()
st = org.shared_scan_status()
check("scan: finished without error, found the one scene", st["error"] is None and not st["running"] and st["found"] == 1)
ov = org.shared_overview()
check("scan: overview has the row, the counts and the time", [r["scene_id"] for r in ov["rows"]] == ["2"] and ov["scanned_at"])

# ---- the download verification (v0.246.2): the verdict stays, a hint is recorded when the file is registered at several scenes
import verification
class DlStash:
    def find_scene_by_path(self, p):
        return {"id": 5, "title": "Dl", "created_at": "2026-01-01T00:00:00Z", "files": [{"id": "f", "path": p, "duration": 2261, "created_at": "2026-01-01T00:00:00Z",
                                                                                          "fingerprints": [{"type": "oshash", "value": "o"}, {"type": "phash", "value": "ff00"}]}]}
class DlClient:
    def __init__(self, found): self.found = found
    def get_scene_fingerprints(self, sid): return [{"algorithm": "OSHASH", "hash": "o"}]
    def find_scenes_by_fingerprints(self, sets): return [self.found]
    def scene_durations(self, ids): return {i: D.get(i) for i in ids}
    def get_scene_detail(self, i): return {"title": i, "date": "2020-01-01", "studio": {"name": "S"}}
calls = []
verification.shared_hook = lambda scene, file, tag, found, client: calls.append((scene["id"], tag, [f["id"] for f in found]))
v = verification.verify_downloaded_file(DlStash(), DlClient([M("wank"), M("tag")]), "wank", "/media/x.mp4")
check("download: still verified, and the hook was told about the other scene", v[0] is True and calls == [(5, "wank", ["wank", "tag"])])
calls.clear()
verification.verify_downloaded_file(DlStash(), DlClient([M("wank")]), "wank", "/media/x.mp4")
check("download: a unique match does not call the hook", calls == [])
verification.shared_hook = None
check("download: no hook set = old behaviour", verification.verify_downloaded_file(DlStash(), DlClient([M("wank"), M("tag")]), "wank", "/media/x.mp4")[0] is True)
print("\nALL OK" if not FAILS else f"\n{len(FAILS)} FAILED")
sys.exit(1 if FAILS else 0)
