"""Tests (v0.233.0): the userscript backend -- provider by site, per-scene status (library wins over the wishlist), send through the normal
hand-off, the served script gets this sidecar's address.
Run:  venv/bin/python scripts/test_userscript_router.py"""
import os
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))
os.environ["DATA_DIR"] = tempfile.mkdtemp()

from fastapi import HTTPException  # noqa: E402

import metadata_provider_repo  # noqa: E402
import release_repo  # noqa: E402
from routers import search as search_router  # noqa: E402
from routers import userscript as us  # noqa: E402
from db import Database  # noqa: E402

FAILS = []
def check(name, cond):  # noqa: E302
    print(("ok   " if cond else "FAIL ") + name)
    if not cond: FAILS.append(name)

db = Database(os.path.join(os.environ["DATA_DIR"], "t.sqlite3"))
metadata_provider_repo.create(db, "otherbox", "Other box", "https://box.example.org/graphql", "k")
us.init_userscript_router(db, None, None, lambda: "http://stash.example:9999/")
search_router._db = db

check("stashdb.org maps to stashdb", us.provider_for_origin("https://stashdb.org") == "stashdb")
check("an added stash-box maps by host", us.provider_for_origin("https://box.example.org") == "otherbox")
check("an unknown host maps to nothing", us.provider_for_origin("https://nope.example") is None)

# state: library beats the wishlist state; unknown scenes have no status
search_router._ids_in_library = lambda provider, ids: {"a": "77"} if "a" in ids else {}
release_repo.wishlist_states = lambda db_, provider, ids, include_open=False: {"a": "sent_to_whisparr", "b": "dismissed"}
r = us.state("https://stashdb.org", "a,b,c")
check("in library wins", r["scenes"]["a"] == {"status": "in_library", "stash_scene_id": "77", "srm": "sent_to_whisparr"})
check("wishlist state kept", r["scenes"]["b"] == {"status": "dismissed", "stash_scene_id": None, "srm": "dismissed"})
check("unknown has no status", r["scenes"]["c"]["status"] is None)
check("stash url without trailing slash", r["stash_url"] == "http://stash.example:9999")

# open rows come back as 'known' only for the userscript, and never beat a real state

# wishlist: the scene is fetched by id and goes through handoff_scene
class FakeClient:
    def get_scene_full(self, sid):
        return {"id": sid, "title": "T"} if sid == "ok" else None
search_router._client_for = lambda provider: FakeClient()
seen = {}
search_router.handoff_scene = lambda provider, scene, search: seen.update(provider=provider, scene=scene, search=search) or {"ok": 1}
check("send passes scene and flag", us.wishlist({"origin": "https://stashdb.org", "id": "ok"}, True) == {"ok": 1}
      and seen == {"provider": "stashdb", "scene": {"id": "ok", "title": "T"}, "search": True})
for body, code in (({"origin": "https://stashdb.org", "id": "gone"}, 404), ({"origin": "https://nope.example", "id": "ok"}, 404),
                   ({"origin": "https://stashdb.org", "id": ""}, 400)):
    try:
        us.wishlist(body, False)
        check(f"error {code}", False)
    except HTTPException as e:
        check(f"error {code}", e.status_code == code)

# the served script
req = SimpleNamespace(base_url="http://srm.example:6961/", url=SimpleNamespace(hostname="srm.example"))
text = us.script(req).body.decode()
check("script carries the sidecar address", "http://srm.example:6961/userscript/srm.user.js" in text and "@connect      srm.example" in text
      and "__SRM" not in text)

# performers / studios: state and watch / unwatch
import watchlist_source  # noqa: E402
from routers import watchlist as watchlist_router  # noqa: E402
class FakeStash:
    def __init__(self): self.known = {"p1": {"id": "5", "name": "N", "favorite": False, "tags": []}}
    def find_performer_by_stash_id(self, sid, endpoint): return self.known.get(sid)
    def find_studio_by_stash_id(self, sid, endpoint): return None
fs = FakeStash()
us.init_userscript_router(db, lambda: fs, None, lambda: "http://stash.example:9999/")
search_router._endpoint_for = lambda provider, sc: "https://stashdb.org/graphql"
watchlist_source.is_watched = lambda db_, sc, kind, lid: (lid == 5, {})
watchlist_source.get_mode = lambda db_: "favorites"
watchlist_source.get_tag_name = lambda db_: "Watch"
e = us.entity_state("https://stashdb.org", "performer", "p1")
check("known performer: local id and watched", e["local_id"] == "5" and e["watched"] is True and e["mode"] == "favorites")
e = us.entity_state("https://stashdb.org", "studio", "s9")
check("unknown studio: no local id, not watched", e["local_id"] is None and e["watched"] is False)
try:
    us.entity_state("https://stashdb.org", "scene", "x"); check("bad kind rejected", False)
except HTTPException as ex:
    check("bad kind rejected", ex.status_code == 400)
calls = []
search_router.favorite_performer = lambda provider, sid, body: calls.append(("fav", provider, sid, body)) or {"id": 5, "created": False}
watchlist_router._on_watch_changed = lambda kind, lid, w: calls.append(("changed", kind, lid, w))
watchlist_router.set_watch_state = lambda kind, lid, body: calls.append(("unwatch", kind, lid, body.watched))
r = us.watch({"origin": "https://stashdb.org", "kind": "performer", "id": "p1", "name": "N", "watched": True})
check("watch creates/favorites, then clears a pending decision", r["watched"] is True and calls == [
    ("fav", "stashdb", "p1", {"name": "N"}), ("changed", "performer", 5, True)])
calls.clear()
r = us.watch({"origin": "https://stashdb.org", "kind": "performer", "id": "p1", "watched": False})
check("unwatch only clears the watchlist mark", r["watched"] is False and calls == [("unwatch", "performer", 5, False)])
calls.clear()
r = us.watch({"origin": "https://stashdb.org", "kind": "studio", "id": "s9", "watched": False})
check("unwatch of a studio Stash does not have does nothing", r["local_id"] is None and calls == [])
try:
    us.watch({"origin": "https://stashdb.org", "kind": "performer", "id": "p2", "watched": True}); check("watch needs a name", False)
except HTTPException as ex:
    check("watch needs a name", ex.status_code == 400)

# regression (0.234.2): the scene painter must not run over the performer/studio bar (it has no send button and threw, which left every
# card unpainted), and a bar that fails must not stop the others
js = open(os.path.join(os.path.dirname(__file__), "..", "api", "userscript", "srm.user.js"), encoding="utf-8").read()
check("scene painter only takes bars with a scene id", "querySelectorAll('.srm-bar[data-srm-id]')" in js and "try { paintScene(wrap)" in js)

# 0.246.12: the user gets a pop-up when a hand-off worked or failed
check("send and watch show a pop-up on success and failure", js.count("toast(") >= 6 and ".srm-toast-err" in js)

print("\nALL OK" if not FAILS else "\nFAILED: %s" % FAILS)
sys.exit(1 if FAILS else 0)
