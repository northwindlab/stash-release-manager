"""other_scene_check: an undated release name whose title part is ANOTHER stash-box scene's title is not this scene's (v0.260.8)."""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "api"))
import other_scene_check as o  # noqa: E402

fails = []
def check(name, ok):
    print(("ok   " if ok else "FAIL ") + name)
    if not ok:
        fails.append(name)

check("title words: performer = studio stripped", o.title_words("Jane.Roe.Sis.Lets.You.Creampie.1080p", {"janeroe"}) == "Sis Lets You Creampie")
check("title words: quality tail cut at xxx", o.title_words("Studio.One.Some.Title.XXX.1080p.MP4-GRP", {"studioone"}) == "Some Title")
check("title words: name at the end", o.title_words("Some.Title.Jane.Roe.720p", {"janeroe"}) == "Some Title")

class Client:
    def __init__(self):
        self.calls = 0; self.fail = False
    def find_scenes_on_date(self, studio_id, date):
        return []
    def search_scenes(self, term, limit=10):
        self.calls += 1
        if self.fail:
            raise RuntimeError("down")
        return {"results": [
            {"id": "other", "title": "Sis Lets You Creampie", "studio": {"name": "Jane Roe"}, "performers": [{"name": "Jane Roe"}]},
            {"id": "mine", "title": "Hot POV Anal Fucking", "studio": {"name": "Jane Roe"}, "performers": [{"name": "Jane Roe"}]},
            {"id": "x", "title": "Sis Lets You Creampie", "studio": {"name": "Else"}, "performers": [{"name": "Nobody Known"}]}]}

c = Client()
o.configure(lambda provider: c)
run = lambda title, scene_title="Hot POV Anal Fucking": o.titles_of_other_scenes(
    "stashdb", "mine", "2016-09-04", ["Jane Roe"], "Jane Roe", [{"title": title}], scene_title=scene_title)
check("title of another scene of the same studio / performer -> other scene", run("Jane.Roe.Sis.Lets.You.Creampie.1080p") == ["Jane.Roe.Sis.Lets.You.Creampie.1080p"])
n = c.calls
check("our own title in the name -> ours, no lookup", run("Jane.Roe.Hot.POV.Anal.Fucking.1080p") == [] and c.calls == n)
check("a title no scene carries -> stays", run("Jane.Roe.Brand.New.Title.Here.1080p") == [])
check("the name has neither performer nor studio -> no lookup", run("Somebody.Sis.Lets.You.Creampie.1080p") == [])
c.fail = True
o._title_cache.clear()
check("a failing lookup keeps the candidate", run("Jane.Roe.Sis.Lets.You.Creampie.1080p") == [])
print("FAILED: " + ", ".join(fails) if fails else "ALL OK")
sys.exit(1 if fails else 0)
