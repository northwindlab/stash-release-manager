"""Tests (v0.248.0): list of scenes with a provider id but not Organized, and marking them Organized. Fake Stash, no network. Run:
venv/bin/python scripts/test_unorganized.py"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))
import scene_compare  # noqa: E402

FAILS = []
def check(name, cond):  # noqa: E302
    print(("ok   " if cond else "FAIL ") + name)
    if not cond: FAILS.append(name)

class FakeStash:
    def __init__(self):
        self.scenes = {"1": {"id": "1", "title": "", "organized": False, "stash_ids": [{"endpoint": "E1", "stash_id": "abc"}]},
                       "2": {"id": "2", "title": "x", "organized": False, "stash_ids": []},
                       "3": {"id": "3", "title": "y", "organized": True, "stash_ids": [{"endpoint": "E1", "stash_id": "d"}]}}
        self.mutations = []
    def _graphql(self, q, v):
        if "findScenes" in q:
            return {"findScenes": {"scenes": [{**s, "date": None, "studio": None, "files": [{"path": "/data/scenes/A/f.mp4", "height": 1080, "duration": 600, "size": 5e8}]}
                                              for s in self.scenes.values() if s["stash_ids"] and not s["organized"]]}}
        if "findScene(" in q:
            return {"findScene": self.scenes.get(v["id"])}
        self.mutations.append(v["i"])
        self.scenes[v["i"]["id"]]["organized"] = True
        return {"sceneUpdate": {"id": v["i"]["id"]}}

class Imp:
    def provider_of_endpoint(self, e): return "stashdb" if e == "E1" else None
    def name_of(self, k): return "StashDB"

fake = FakeStash()
c = scene_compare.SceneCompare.__new__(scene_compare.SceneCompare)
c.get_stash = lambda: fake
c.get_importer = lambda: Imp()
out = c.unorganized_list()["scenes"]
check("only the scene with an id and no Organized flag is listed", [x["id"] for x in out] == ["1"])
check("provider name and id are given", out[0]["providers"] == [{"key": "stashdb", "name": "StashDB", "id": "abc"}])
res = c.organized_set(["1", "2", "3", "9"])
check("marks the one with an id", res["1"] == {"ok": True} and fake.mutations == [{"id": "1", "organized": True}])
check("refuses a scene without an id", "error" in res["2"])
check("an organized scene is left alone", res["3"].get("already") is True and len(fake.mutations) == 1)
check("unknown scene reports an error", "error" in res["9"])
import kept_lists  # noqa: E402
import sqlite3, json  # noqa: E402,E401
class DB:
    def __init__(self): self.c = sqlite3.connect(":memory:"); self.c.row_factory = sqlite3.Row; self.c.execute("CREATE TABLE maintenance_state (key TEXT PRIMARY KEY, value TEXT)")
    def connect(self):
        c = self.c
        class Ctx:
            def __enter__(s): return c
            def __exit__(s, *a): c.commit()
        return Ctx()
db = DB()
calls = []
get = lambda refresh=False: kept_lists.get(db, "scenes_unorganized", lambda: calls.append(1) or [{"id": "1"}, {"id": "5"}], refresh)  # noqa: E731
get(); get()
check("the list is read once and kept", len(calls) == 1)
kept_lists.update(db, "scenes_unorganized", lambda rows: [r for r in rows if r["id"] != "1"])
check("a marked scene leaves the kept list without a new read", get()[0] == [{"id": "5"}] and len(calls) == 1)
get(True)
check("reload reads again", len(calls) == 2)
kept_lists.update(db, "nothing_kept", lambda rows: rows)
check("update of a list that is not kept does nothing", True)
import library_import  # noqa: E402
class Imp2(library_import.Importer):
    def __init__(self, scene, hook, result): self._scene, self._hook, self._result, self.written = scene, hook, result, []
    def setting(self, k): return self._hook
    def _root(self): return "/data"
    def _scene_by_id(self, i): return self._scene
    def organized_scene(self, sid, mode): self.written.append(mode); return self._result
sc = {"id": "7", "files": [{"path": "/data/scenes/A/old.mp4"}]}
p = Imp2(sc, "on", {"preview": {"new_path": "/data/scenes/Studio/new.mp4"}, "blocked": None}).organized_plan("7")
check("plan gives old and new relative to the library", p["old"] == "scenes/A/old.mp4" and p["new"] == "scenes/Studio/new.mp4" and p["note"] is None)
i = Imp2(sc, "off", {"preview": {"new_path": "/data/scenes/Studio/new.mp4"}})
p = i.organized_plan("7")
check("hook off: plan still shown, with the hint, asked in dry mode", p["new"] and "hook is off" in p["note"] and i.written == ["dry"])
p = Imp2(sc, "on", {"note": "has several files -- not moved"}).organized_plan("7")
check("no move: the reason is given", p["new"] is None and "several files" in p["note"])
p = Imp2(sc, "on", {"preview": {"new_path": "/data/scenes/A/old.mp4"}}).organized_plan("7")
check("same path: nothing to do", p["new"] is None and p["note"])
p = Imp2({**sc, "organized": True}, "on", {"preview": {"new_path": "/data/x/y.mp4"}}).organized_plan("7")
check("a scene marked Organized meanwhile is reported, nothing planned", p.get("already_organized") is True and p["new"] is None)
print("ALL OK" if not FAILS else f"{len(FAILS)} FAILED")
sys.exit(1 if FAILS else 0)
