"""film_import.discard ("Delete + search" / "Delete + dismiss" on Activity > Import failed): files to the recycle bin, scenes removed, the movie goes back to
'wanted' or 'dismissed' (v0.243.15: an undefined variable made it fail with HTTP 500 AFTER the files were gone). Throwaway database, fake Stash. Run:
venv/bin/python scripts/test_film_discard.py"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))
os.environ["DATA_DIR"] = tempfile.mkdtemp()

import film_import  # noqa: E402
import film_repo  # noqa: E402
from db import Database  # noqa: E402

fails = []
def check(name, cond):  # noqa: E302
    print(("ok   " if cond else "FAIL ") + name)
    if not cond: fails.append(name)


class Stash:
    def __init__(self): self.destroyed = []
    def find_scene_by_path(self, p): return {"id": 7}
    def scene_files(self, sid): return [1]
    def destroy_scene(self, sid, delete_file=False): self.destroyed.append(sid)
    def clean_paths(self, folders): pass


root = tempfile.mkdtemp()
folder = os.path.join(root, "Movies", "Film (2011)")
os.makedirs(folder)
open(os.path.join(folder, "film.mp4"), "w").write("x")
recycled = []
def recycle(p): recycled.append(p); os.remove(p)  # noqa: E302

db = Database(os.path.join(tempfile.mkdtemp(), "t.sqlite3"))
for dismiss, expect in ((False, "wanted"), (True, "dismissed")):
    fid, _ = film_repo.add(db, title=f"Film {expect}", year=2011, status="import_failed")
    os.makedirs(folder, exist_ok=True)  # the first discard removed the empty folder
    path = os.path.join(folder, "film.mp4")
    open(path, "w").write("x")
    film_repo.update(db, fid, pending_import_path=json.dumps([path]), grabbed_title="Some.Release")
    st = Stash()
    res = film_import.discard(db, st, film_repo.get(db, fid), recycle, lambda p: p, dismiss=dismiss)
    after = film_repo.get(db, fid)
    check(f"dismiss={dismiss}: status {expect}", res["status"] == expect and after["status"] == expect)
    check(f"dismiss={dismiss}: file recycled, scene removed", res["files"] == ["film.mp4"] and st.destroyed == ["7"])
    check(f"dismiss={dismiss}: the pending path and error are cleared, the release is blocklisted",
          not after.get("pending_import_path") and "Some.Release" in (after.get("rejected_titles") or []))

print("\nALL OK" if not fails else f"\n{len(fails)} FAILED")
sys.exit(1 if fails else 0)
