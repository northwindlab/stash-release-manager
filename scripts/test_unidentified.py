"""Tests of the fingerprint assignment for unidentified scenes (v0.170.0): SceneCompare.unidentified_list / _check / _apply with a FAKED Stash and
StashDB, a throwaway data dir (nothing real is touched). Run:  venv/bin/python scripts/test_unidentified.py"""
import json
import os
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))
os.environ["DATA_DIR"] = tempfile.mkdtemp()

import library_check  # noqa: E402
import library_import  # noqa: E402
import scene_compare  # noqa: E402
from db import Database  # noqa: E402

FAILS = []
def check(name, cond):  # noqa: E302
    print(("ok   " if cond else "FAIL ") + name)
    if not cond: FAILS.append(name)


FP = lambda v: [{"type": "oshash", "value": v}, {"type": "phash", "value": v + "p"}]  # noqa: E731
SCENES = {  # id -> (path, stash_ids, fingerprint seed, tags)
    "1": ("/data/scenes/A/one.mp4", [], "h1", []),            # unique
    "2": ("/data/scenes/A/two.mp4", [], "h2", []),            # ambiguous
    "3": ("/data/scenes/A/three.mp4", [], "h3", []),          # none
    "4": ("/data/scenes/A/four.mp4", [], "h4", []),           # held by scene 99
    "5": ("/data/scenes/A/five.mp4", [], "h5", []),           # shares its match with 6
    "6": ("/data/scenes/A/six.mp4", [], "h5", []),
    "7": ("/data/movies/Film/seven.mp4", [], "h7", []),       # film folder: left out
    "8": ("/data/import/eight.mp4", [], "h8", []),            # import folder: left out
    "9": ("/data/scenes/A/nine.mp4", [], "h9", [{"id": "T"}]),  # review tag: left out
    "10": ("/data/scenes/A/ten.mp4", [{"endpoint": "x"}], "h10", []),  # has an id: not listed by the Stash filter (faked below)
}
MATCH = {"h1": [{"id": "S1", "title": "One"}], "h2": [{"id": "S2a", "title": "Two A"}, {"id": "S2b", "title": "Two B"}], "h3": [],
         "h4": [{"id": "S4", "title": "Four"}], "h5": [{"id": "S5", "title": "Five"}], "h9": [{"id": "S9", "title": "Nine"}]}
CALLS = {"lookup": 0, "adopted": []}


class Stash:
    stashdb_endpoint = "https://stashdb.org/graphql"
    theporndb_endpoint = "https://theporndb.net/graphql"
    def find_tag_id(self, name): return "T"
    def find_scene_by_stashdb_id(self, pid, endpoint): return {"id": "99", "title": "held"} if pid == "S4" else None
    def _graphql(self, q, v):
        if "stash_id_count" in q:
            return {"findScenes": {"scenes": [{"id": i, "title": "", "tags": t, "files": [{"path": p, "width": 1, "height": 1, "duration": 60, "size": 1000}]}
                                              for i, (p, ids, h, t) in SCENES.items() if not ids]}}
        sid = str((v or {}).get("id"))
        if "fingerprints" in q:
            p, ids, h, t = SCENES[sid]
            return {"findScene": {"id": sid, "stash_ids": ids, "files": [{"id": f"f{sid}", "fingerprints": FP(h)}]}}
        return {"findScene": None}


class StashDB:
    def find_scenes_by_fingerprints(self, sets):
        CALLS["lookup"] += 1
        out = []
        for s in sets:
            h = next(x["hash"] for x in s if x["algorithm"] == "OSHASH")
            out.append(MATCH.get(h, []))
        return out


def importer(stashdb, theporndb=None):  # the real Importer._identify, built without its constructor
    imp = library_import.Importer.__new__(library_import.Importer)
    imp.stashdb, imp.theporndb, imp.stash = stashdb, theporndb, Stash()
    return imp
library_import.time.sleep = lambda s: None

DB = Database(os.path.join(tempfile.mkdtemp(), "t.sqlite3"))
comp = scene_compare.SceneCompare(DB, lambda: Stash(), lambda p: None, tempfile.mkdtemp(), get_importer=lambda: importer(StashDB()))
comp.scene = lambda sid, with_provider=False: {"id": sid, "title": "Adopted", "date": "2011-01-28", "stash_ids": []}
def fake_apply_fix(stash, sid, pid, **kw): CALLS["adopted"].append((sid, pid))  # noqa: E302
library_check.apply_fix = fake_apply_fix
scene_compare.time.sleep = lambda s: None

lst = comp.unidentified_list()
ids = [x["id"] for x in lst["scenes"]]
check("list: film folder, import folder and review-tagged scenes are left out", ids == ["1", "2", "3", "4", "5", "6", "10"] or ids == ["1", "2", "3", "4", "5", "6"])
check("list: the review-tagged one is counted", lst["in_review"] == 1)

r = comp.unidentified_check(["1", "2", "3", "4", "5", "10"])  # 6 is checked in a LATER call: the "shared" state must still be found
check("check: exactly one match = unique", r["1"]["state"] == "unique" and r["1"]["matches"][0]["id"] == "S1")
check("check: several matches = ambiguous", r["2"]["state"] == "ambiguous" and len(r["2"]["matches"]) == 2)
check("check: no match = none", r["3"]["state"] == "none")
check("check: held by another local scene = held", r["4"]["state"] == "held" and r["4"]["held_by"] == "99")
check("check: alone so far, 5 is unique", r["5"]["state"] == "unique")
r6 = comp.unidentified_check(["6"])
check("check: two scenes wanting the same match = shared, neither unique (even across two calls)", r6["6"]["state"] == "shared" and r6["6"]["shared_with"] == ["5"])
comp2 = scene_compare.SceneCompare(DB, lambda: Stash(), lambda p: None, comp.data_dir, get_importer=lambda: importer(StashDB()))  # a "new request" / a reload
checks = comp2.unidentified_list()["checks"]
check("list: the stored results come back with the list (reload / restart)", checks["1"]["state"] == "unique" and checks["5"]["state"] == "shared" and checks["2"]["state"] == "ambiguous")
check("check: a scene that has an id meanwhile is not looked up", "10" not in r)
check("check: batched (one lookup call for the first 5 scenes, one for the later one)", CALLS["lookup"] == 2)
check("check: nothing was written", CALLS["adopted"] == [])

a = comp.unidentified_apply(["1", "2", "3", "4", "5", "6", "7"])
check("apply: the unique one is assigned", a["1"].get("ok") and CALLS["adopted"] == [("1", "S1")])
check("apply: ambiguous / none / held / shared / never checked are refused", all("error" in a[k] for k in ("2", "3", "4", "5", "6", "7")))
check("apply: the backup file was written", os.path.exists(os.path.join(comp.data_dir, "compare_removed_ids.jsonl")))
b = comp.unidentified_apply(["1"])
check("apply: the stored result is gone after the assignment", "1" not in comp2.unidentified_list()["checks"])
check("apply: a second call for the same scene is refused (result consumed)", "error" in b["1"] and len(CALLS["adopted"]) == 1)

# Review adopt (pair 0): the scene leaves the Review list (tag removed) -- faked tag handling
TAG = {"removed": []}
comp._check_member = lambda pair_id, sid: None
comp._backup = lambda *a, **k: "/tmp/x.jsonl"
comp._remove_review_tag = lambda sid, full, reason: TAG["removed"].append((sid, reason))
Stash.stashdb_endpoint  # (the adopt uses the stash client's endpoint)
r = comp.adopt(0, "20", "stashdb", "S20")
check("adopt from Review (pair 0) closes the review case", r.get("ok") and r.get("review_closed") and TAG["removed"][0][0] == "20")
r = comp.adopt(5, "21", "stashdb", "S21")
check("adopt inside a compare pair does not touch the review tag", r.get("ok") and not r.get("review_closed") and len(TAG["removed"]) == 1)


# ThePornDB only (v0.243.2): no StashDB set up -> exact hashes at ThePornDB, never its similarity-based PHASH, the id goes on ThePornDB's endpoint
class TPDB:
    asked = []
    def find_scenes_by_hash(self, h, t):
        TPDB.asked.append(t)
        return [{"id": "T20", "title": "Twenty"}] if (h, t) == ("h1", "OSHASH") else []
CALLS["adopted"].clear()
comp3 = scene_compare.SceneCompare(Database(os.path.join(tempfile.mkdtemp(), "t3.sqlite3")), lambda: Stash(), lambda p: None, tempfile.mkdtemp(),
                                   get_importer=lambda: importer(None, TPDB()))
comp3.scene = comp.scene
comp3._backup = lambda *a, **k: "/tmp/x.jsonl"
captured = {}
def capture(stash, sid, pid, **kw): captured.update(sid=sid, pid=pid, endpoint=kw.get("endpoint"))  # noqa: E302
library_check.apply_fix = capture
t = comp3.unidentified_check(["1", "3"])
check("ThePornDB only: an exact oshash hit is a clear match, with its provider", t["1"]["state"] == "unique" and t["1"]["matches"][0]["provider"] == "theporndb")
check("ThePornDB only: no hit = none", t["3"]["state"] == "none")
check("ThePornDB only: PHASH is never asked", "PHASH" not in TPDB.asked and "OSHASH" in TPDB.asked)
comp3.unidentified_apply(["1"])
check("ThePornDB only: the id is put on ThePornDB's endpoint", captured.get("endpoint") == Stash.theporndb_endpoint and captured.get("pid") == "T20")

# A user-added stash-box (v0.244.0): StashDB does not know the file, the extra box does -> its key, its endpoint, one batch call
EXTRA_EP = "https://box.example/graphql"
class Box:
    calls = 0
    def find_scenes_by_fingerprints(self, sets):
        Box.calls += 1
        return [[{"id": "X1", "title": "Extra one"}] if any(x["hash"] == "h1" for x in s) else [] for s in sets]
def importer_extra():
    imp = importer(StashDB(), TPDB())
    imp.extra_providers = [{"key": "box", "name": "My box", "endpoint": EXTRA_EP, "client": Box()}]
    return imp
MATCH["h1"] = []  # StashDB: nothing
CALLS["adopted"].clear()
comp4 = scene_compare.SceneCompare(Database(os.path.join(tempfile.mkdtemp(), "t4.sqlite3")), lambda: Stash(), lambda p: None, tempfile.mkdtemp(),
                                   get_importer=importer_extra)
comp4.scene = comp.scene
comp4._backup = lambda *a, **k: "/tmp/x.jsonl"
captured.clear()
x = comp4.unidentified_check(["1", "3"])
check("extra stash-box: found there when StashDB does not know the file", x["1"]["state"] == "unique" and x["1"]["matches"][0]["provider"] == "box")
check("extra stash-box: asked only for what StashDB left open (one batch)", Box.calls == 1)
check("extra stash-box: no hit anywhere = none", x["3"]["state"] == "none")
comp4.unidentified_apply(["1"])
check("extra stash-box: the id goes on the box's endpoint", captured.get("endpoint") == EXTRA_EP and captured.get("pid") == "X1")
imp = importer_extra()
check("extra stash-box: endpoint / name / client lookups", imp.endpoint_of("box") == EXTRA_EP and imp.name_of("box") == "My box" and imp.client_of("box") is not None)
check("extra stash-box: a scene carrying only the box's id is recognised", imp._provider_of({"stash_ids": [{"endpoint": EXTRA_EP, "stash_id": "X9"}]}) == ("box", "X9"))
check("extra stash-box: link to the box's own page", comp4._provider_url("box", "X1") == "https://box.example/scenes/X1")
print("\nALL OK" if not FAILS else f"\n{len(FAILS)} FAILED")
sys.exit(1 if FAILS else 0)
