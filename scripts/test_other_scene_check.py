"""other_scene_check: a release named after another stash-box scene (studio + date + performer in its cast) is not this scene's (v0.259.0)."""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "api"))
import other_scene_check as o  # noqa: E402

fails = []
def check(name, ok):
    print(("ok   " if ok else "FAIL ") + name)
    if not ok:
        fails.append(name)

check("split: CamelCase studio + yy.mm.dd", o.split_release("StudioOne.13.02.08.Jane.Roe.XXX.1080p.MP4-GRP") == ("StudioOne", "2013-02-08"))
check("split: dotted studio, 4-digit year", o.split_release("Studio.One.2013.02.08.Jane.Roe.1080p") == ("Studio.One", "2013-02-08"))
check("split: no date -> None", o.split_release("Studio.One.Jane.Roe.1080p") is None)
check("split: no studio part -> None", o.split_release("13.02.08.Jane.Roe.1080p") is None)
check("search term from CamelCase", o._search_term("StudioOneTwo") == "Studio One Two")


class Client:
    def __init__(self):
        self.studio_calls = self.scene_calls = 0
        self.fail = False

    def search_studios(self, term, limit=10):
        self.studio_calls += 1
        if self.fail:
            raise RuntimeError("down")
        return {"results": [{"id": "S1", "name": "Studio One's"}, {"id": "S2", "name": "Other"}]}

    def find_scenes_on_date(self, studio_id, date):
        self.scene_calls += 1
        return [{"id": "other-1", "performers": [{"performer": {"name": "Jane Roe"}}]},
                {"id": "other-2", "performers": [{"performer": {"name": "Someone Else"}}]}] if (studio_id, date) == ("S1", "2013-02-08") else []

c = Client()
o.configure(lambda provider: c)
rel = lambda t: {"title": t}
mine = dict(scene_id="mine", scene_date="2013-01-25", performer_names=["Jane Roe"], studio_name="Studio Two")
ours = "StudioOnes.13.02.08.Jane.Roe.XXX.1080p.MP4-GRP"
r = o.titles_of_other_scenes("stashdb", mine["scene_id"], mine["scene_date"], mine["performer_names"], mine["studio_name"], [rel(ours)])
check("studio + date + cast member -> another scene", r == [ours])
o._cache.clear(); c.studio_calls = c.scene_calls = 0
r = o.titles_of_other_scenes("stashdb", "other-1", "2013-02-08", ["Jane Roe"], "Studio One's", [rel(ours)])
check("the release IS this scene (its own studio and day) -> not touched", r == [] and c.studio_calls == 0)
r = o.titles_of_other_scenes("stashdb", "other-1", "2013-01-25", ["Jane Roe"], "Studio Two", [rel(ours)])
check("the studio's scene on that day is this very scene -> kept", r == [])
r = o.titles_of_other_scenes("stashdb", "mine", "2013-01-25", ["Nobody Here"], "Studio Two", [rel(ours)])
check("release names none of our performers -> not looked at", r == [])
o._cache.clear()
r = o.titles_of_other_scenes("stashdb", "mine", "2013-01-25", ["Jane Roe"], "Studio Two", [rel("StudioOnes.13.02.09.Jane.Roe.1080p")])
check("another day: no scene there -> kept", r == [])
o._cache.clear(); c.fail = True
r = o.titles_of_other_scenes("stashdb", "mine", "2013-01-25", ["Jane Roe"], "Studio Two", [rel(ours)])
check("a failing lookup never removes a candidate", r == [])
c.fail = False; o._cache.clear(); c.studio_calls = 0
o.titles_of_other_scenes("stashdb", "mine", "2013-01-25", ["Jane Roe"], "Studio Two", [rel(ours), rel(ours + "2"), rel(ours + "3")])
check("the same studio + date is asked once (cache)", c.studio_calls == 1)
o._cache.clear()
r = o.titles_of_other_scenes("theporndb", "mine", "2013-01-25", ["Jane Roe"], "Studio Two", [rel(ours)])
check("only the stashdb provider is checked", r == [])
o._cache.clear(); c.studio_calls = 0
r = o.titles_of_other_scenes("stashdb", "other-1", "2013-02-08", ["Jane Roe"], "Studio Child", [rel(ours)], also_own=["Studio One's"])
check("parent/site name of the scene counts as own side -> no lookup", r == [] and c.studio_calls == 0)
o._cache.clear()
r = o.titles_of_other_scenes("stashdb", "mine", "2013-01-25", ["Jane Roe"], "Studio Two", [rel("Unknown.Studio.13.02.08.Jane.Roe.1080p")])
check("an unknown studio -> kept", r == [])

# ---- the start-up sweep over stored review candidates
import json, tempfile
os.environ["DATA_DIR"] = tempfile.mkdtemp()
import history, release_repo
from db import Database
history._db = None
db = Database(os.path.join(os.environ["DATA_DIR"], "t.sqlite3"))
cand = json.dumps({"title": ours})
with db.connect() as conn:
    for sid, title in (("mine", "Mine"), ("keep", "Keep")):
        conn.execute("""INSERT INTO release (provider, scene_stashdb_id, entity_type, entity_stashdb_id, title, scene_created, scene_date, studio_name, status, last_search_result,
                        review_candidate, cast_names, discovered_at) VALUES ('stashdb', ?, 'studio', 'e', ?, '2020-01-01T00:00:00Z', '2013-01-25', 'Studio Two', 'sent_to_whisparr',
                        'needs_review', ?, ?, datetime('now'))""", (sid, title, cand if sid == "mine" else json.dumps({"title": "Nobody.13.02.08.Jane.Roe"}),
                                                                      "|Jane Roe|"))
o._cache.clear()
check("sweep removes the one that belongs elsewhere", o.sweep_review_candidates(db) == 1)
with db.connect() as conn:
    rows = {r["scene_stashdb_id"]: dict(r) for r in conn.execute("SELECT * FROM release")}
check("... its candidate is cleared and the result changed", rows["mine"]["review_candidate"] is None and rows["mine"]["last_search_result"] == "title_mismatch_only")
check("... the other one stays", rows["keep"]["review_candidate"] is not None and rows["keep"]["last_search_result"] == "needs_review")

# ---- a review candidate dated more than a year away (any number of performers)
import indexer_search as ix
far_t = "Creator.25.05.31.Elly.Clutch.Chloe.Foxxe.Jak.Knife.Bridesmaids.Threesome.XXX.1080p.MP4-P2P"
pn = ["Elly Clutch", "Chloe Foxxe", "Jak Knife"]
check("review: 3 performers named but dated 17 months before -> not offered",
      ix._select_review_candidate([{"title": far_t, "age": 100}], "Break Up With Your Girlfriend", "2026-10-29", pn, []) is None)
near_t = far_t.replace("25.05.31", "26.10.20")
check("review: same name dated 9 days before -> still offered or plausible (not removed by the year rule)",
      ix._release_date_far_off("2026-10-29", near_t, ix.REVIEW_MAX_DAYS_ANY) is False)
print("FAILED: " + ", ".join(fails) if fails else "ALL OK")
sys.exit(1 if fails else 0)
