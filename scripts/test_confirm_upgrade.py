"""'File is correct' on a failed upgrade download replaces the old file like a verified upgrade (v0.262.2)."""
import os, sys, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "api"))
os.environ["DATA_DIR"] = tempfile.mkdtemp()
import file_importer as fi, release_repo, history, upgrades  # noqa: E402
from db import Database  # noqa: E402

fails = []
def check(name, ok):
    print(("ok   " if ok else "FAIL ") + name)
    if not ok:
        fails.append(name)

db = Database(os.path.join(os.environ["DATA_DIR"], "t.sqlite3"))
history.init(db)
OLD, NEW = "/data/scenes/S/T/T 3K.mp4", "/data/scenes/S/T/T 4K.mp4"
with db.connect() as c:
    c.execute("""INSERT INTO release (provider, scene_stashdb_id, entity_type, entity_stashdb_id, title, scene_created, discovered_at, status,
                 pending_import_path, import_error_code, upgrade_of) VALUES ('stashdb','A','studio','upgrade:1','T','2020-01-01','2020-01-01',
                 'import_failed',?, 'fingerprint_mismatch', ?)""", (NEW, OLD))
    c.execute("INSERT INTO upgrade_candidate (scene_id, file_path, provider, provider_scene_id, reason, status) VALUES ('1',?,'stashdb','A','vr_low_quality','grabbed')", (OLD,))
row = lambda: release_repo.list_rows_for_scene(db, "stashdb", "A")[0]

class Stash:
    stashdb_endpoint = theporndb_endpoint = "x"
    def __init__(self, have_old=True, boom=False):
        self.have_old, self.boom, self.log = have_old, boom, []
    def find_scene_by_path(self, p):
        if p == NEW: return {"id": "2", "files": [{"path": NEW}]}
        return {"id": "1", "files": [{"path": OLD}]} if self.have_old else None
    def file_details(self, p): return {"width": 1, "height": 1, "duration": 10 if p == OLD else 5, "size": 1}
    def merge_scene_into(self, s, d):
        if self.boom: raise RuntimeError("merge failed")
        self.log.append(("merge", s, d))
    def scene_files(self, i): return [{"id": "f1", "path": OLD}, {"id": "f2", "path": NEW}]
    def set_primary_file(self, s, f): self.log.append(("primary", s, f))
    def clean_paths(self, p): self.log.append(("clean",))

recycled = []
current = {}
fi.upgrade_import_hook = lambda prov, sid, path, ok: upgrades.on_upgrade_imported(db, current["s"], lambda p: p, recycled.append, prov, sid, path, ok)
fi._trigger_generate = lambda *a, **k: None
fi._provider_client = lambda *a: None
confirm = lambda s: (current.update(s=s), fi.confirm_mismatched_file(db, s, None, "stashdb", "A"))[1]

check("compare gives both files", fi.upgrade_compare(db, Stash(), "stashdb", "A")["new"]["duration"] == 5)
res = confirm(Stash(have_old=False))
check("old file not in Stash: refused, nothing changed", "error" in res and not recycled and row()["upgrade_of"] == OLD and row()["status"] == "import_failed")
res = confirm(Stash(boom=True))
check("a failing merge: error, old file kept, row retryable", "error" in res and not recycled and row()["upgrade_of"] == OLD and row()["status"] == "import_failed")
s = Stash()
res = confirm(s)
check("confirmed: merged into the old scene, new file primary", res.get("ok") and res["scene_id"] == "1" and ("merge", "2", "1") in s.log and ("primary", "1", "f2") in s.log)
check("old file recycled once", recycled == [OLD])
r = row()
check("release imported + user_confirmed, nothing pending",
      r["status"] == "imported" and r["import_error_code"] == "user_confirmed" and r["pending_import_path"] is None and r["upgrade_of"] is None)
with db.connect() as c:
    check("candidate done", c.execute("SELECT status FROM upgrade_candidate").fetchone()["status"] == "done")
print("FAILED: " + ", ".join(fails) if fails else "ALL OK")
sys.exit(1 if fails else 0)
