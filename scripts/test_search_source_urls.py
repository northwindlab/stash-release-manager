"""Search page: source_url per hit, and the tag search of the stash-box client (v0.207.0)."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))
from fastapi import HTTPException
from routers import search
import stashdb_client

class Box:  # stands in for a stash-box client
    url = "https://stash.example/graphql"

res = []
def check(name, cond):
    res.append(cond); print(("ok   " if cond else "FAIL ") + name)

r = search._add_source_urls("stashdb", Box(), "scene", [{"id": "s1"}])
check("stash-box scene -> site/scenes/id", r[0]["source_url"] == "https://stash.example/scenes/s1")
r = search._add_source_urls("fansdb", Box(), "studio", [{"id": "x"}])
check("stash-box studio -> site/studios/id", r[0]["source_url"] == "https://stash.example/studios/x")
r = search._add_source_urls("theporndb", None, "scene", [{"id": "uuid", "slug": "some-slug"}])
check("theporndb scene by slug", r[0]["source_url"] == "https://theporndb.net/scenes/some-slug")
r = search._add_source_urls("theporndb", None, "studio", [{"id": "uuid-1"}])
check("theporndb studio = sites/uuid", r[0]["source_url"] == "https://theporndb.net/sites/uuid-1")
r = search._add_source_urls("x", object(), "scene", [{"id": "1"}])
check("no site address -> no link", "source_url" not in r[0])

norm = stashdb_client.StashDBClient._normalize_search_scenes([{"id": "1", "images": [{"url": "u"}], "tags": [{"name": "VR"}], "performers": [{"performer": {"id": "p", "name": "N"}}]}])
check("scene normalization (image, performers flattened)", norm[0]["image_url"] == "u" and norm[0]["performers"] == [{"id": "p", "name": "N"}])

class Tpdb: pass
search._client_for = lambda provider: Tpdb()
try:
    search.search_tags("theporndb", "x"); check("theporndb tag search refused", False)
except HTTPException as e:
    check("theporndb tag search refused with a clear message", e.status_code == 400)
# several tags (v0.210.0): INCLUDES_ALL for 2+, INCLUDES for one; the router splits the comma list
sent = []
class Fake(stashdb_client.StashDBClient):
    def __init__(self): pass
    def _post(self, payload):
        sent.append(payload["variables"]["input"]["tags"])
        class R:
            def raise_for_status(self): pass
            def json(self): return {"data": {"queryScenes": {"count": 0, "scenes": []}}}
        return R()
f = Fake()
f.search_scenes_by_tag(["a", "b"]); f.search_scenes_by_tag("a")
check("two tags -> INCLUDES_ALL", sent[0] == {"value": ["a", "b"], "modifier": "INCLUDES_ALL"})
check("one tag -> INCLUDES", sent[1] == {"value": ["a"], "modifier": "INCLUDES"})
# excluded tags are filtered out client-side over several pages (StashDB takes one tags criterion)
def mk(i, tags): return {"id": str(i), "title": "t", "images": [], "performers": [], "tags": [{"id": t, "name": t} for t in tags]}
pages = {1: [mk(i, ["x"] if i % 2 else []) for i in range(100)], 2: [mk(100 + i, []) for i in range(30)]}
class Fake2(stashdb_client.StashDBClient):
    def __init__(self): self.calls = 0
    def _post(self, payload):
        self.calls += 1
        pg = payload["variables"]["input"]["page"]
        class R:
            def raise_for_status(self): pass
            def json(self): return {"data": {"queryScenes": {"count": 130, "scenes": pages.get(pg, [])}}}
        return R()
f2 = Fake2(); out = f2.search_scenes_by_tag(["a"], limit=100, exclude_ids=["x"])
check("exclude: reads on to page 2 to fill the list", f2.calls == 2 and len(out["results"]) == 80 and out["total"] == 80)
check("exclude: no excluded scene left", all(r["id"] not in {str(i) for i in range(100) if i % 2} for r in out["results"]))
f3 = Fake2(); out = f3.search_scenes_by_tag(["a"], limit=100)
check("no exclude: one page, provider total", f3.calls == 1 and out["total"] == 130 and len(out["results"]) == 100)
got = []
class Rec:
    def search_scenes_by_tag(self, ids, exclude_ids=()): got.append(ids); return {"results": []}
search._client_for = lambda provider: Rec()
search._decorate_scenes = lambda provider, client, found: found
search.search_scenes_by_tag("stashdb", tag_ids="x,y")
search.search_scenes_by_tag("stashdb", tag_id="z")
check("router: tag_ids split, tag_id still works", got == [["x", "y"], ["z"]])
try:
    search.search_scenes_by_tag("stashdb"); check("no tag refused", False)
except HTTPException as e:
    check("no tag refused", e.status_code == 400)
# SD releases without "480p" in the name count as 480p (v0.211.6)
import indexer_search as isr
def reso(t): return isr.normalize_prowlarr_release({"title": t})["quality"]["quality"]["resolution"]
check("group SD variant -> 480", reso("Site.26.09.12.Name.XXX.MP4-GRPX") == 480)
check("SD segment -> 480", reso("Site.26.09.12.Name.SD.XXX.MP4-GRP") == 480)
check("normal group stays unknown", reso("Nympho.26.10.11.Lily.XXX.MP4-P0RNL0V3R") is None)
check("explicit 1080p wins over SD marker", reso("Site.26.09.12.Name.1080p.MP4-GRPX") == 1080)
check("lowercase 'sd' in a word is no marker", reso("Site.Madison.sd.Name.XXX.MP4-GRP") is None)
# standard-definition heights in the name (0.258.3): 360p / 540p / 576p are the 480p class, the stated height is kept for display
check("540p in the name -> 480 class", reso("Site.26.09.12.Name.XXX.540p.MP4-GRP") == 480)
check("576p / 360p -> 480 class", reso("Site.Name.576p.MP4-GRP") == 480 and reso("Site.Name.360p.MP4-GRP") == 480)
check("720p stays 720", reso("Site.Name.720p.MP4-GRP") == 720)
check("named height 540 reported", isr.torrent_facts({"title": "Site.Name.540p.MP4-GRP"}).get("named_height") == 540)
check("no named height for 480p / 1080p", "named_height" not in isr.torrent_facts({"title": "Site.Name.480p.MP4"}) and "named_height" not in isr.torrent_facts({"title": "Site.Name.1080p.MP4"}))
# scenes older than the RSS feed reaches (v0.213.4)
import datetime as _dt
import search_sources as ss
today = _dt.date(2026, 10, 16)
check("scene 6 days old, feed reaches 1 day -> older than the feed", ss.older_than_feed("2026-10-10", 1, today))
check("scene 2 days old, feed reaches 1 day -> still within the slack", not ss.older_than_feed("2026-10-14", 1, today))
check("scene 6 days old, feed reaches 7 days -> not older", not ss.older_than_feed("2026-10-10", 7, today))
check("no / bad date -> False", not ss.older_than_feed(None, 1, today) and not ss.older_than_feed("x", 1, today))
check("feed window = age of the oldest item, at least 1", ss.FeedSource([{"title": "a", "age": 3}, {"title": "b", "age": 0}], {}).window_days() == 3
      and ss.FeedSource([], {}).window_days() == 1)
# compilation rule (v0.213.5): exact date + 2 title words + nothing foreign
import indexer_search as _ix
T = "FeetishPOV Compilation - Your Tight & Slender Favorites"
P = ["Ada Lapiedra", "Peach Lollypop", "Lia Lin", "Elena Lux", "Sybil A", "Matty Mila Perez"]
check("compilation: studio.date.Compilation release accepted", _ix._candidate_plausible(T, "2026-08-13", P, "FeetishPOV.26.08.13.Compilation.XXX.1080p.MP4-GRP"))
check("compilation: another scene of the studio and day (names a performer) refused", not _ix._candidate_plausible(T, "2026-08-13", P, "FeetishPOV.26.08.13.Jane.Doe.XXX.1080p.MP4-GRP"))
check("compilation: same words, other date refused", not _ix._candidate_plausible(T, "2026-08-13", P, "FeetishPOV.26.07.02.Compilation.XXX.1080p.MP4-GRP"))
check("compilation: studio and date only (one title word) refused", not _ix._candidate_plausible(T, "2026-08-13", P, "FeetishPOV.26.08.13.XXX.1080p.MP4-GRP"))
check("compilation: an extra foreign name in the release refused", not _ix._candidate_plausible(T, "2026-08-13", P, "FeetishPOV.26.08.13.Compilation.Jane.Doe.Anna.Smith.XXX.1080p.MP4-GRP"))
print("\nALL OK" if all(res) else "\nFAILED")
sys.exit(0 if all(res) else 1)
