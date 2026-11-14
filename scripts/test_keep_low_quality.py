"""A download without a resolution in its name that turns out below the profile (480p) is KEPT when the scene has no file in Stash yet (v0.260.6);
only a duplicate of a scene already in the library is thrown away."""
import os, sys, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "api"))
os.environ["DATA_DIR"] = tempfile.mkdtemp()
import file_importer as fi, release_repo, history, quality_detect  # noqa: E402
from db import Database  # noqa: E402

fails = []
def check(name, ok):
    print(("ok   " if ok else "FAIL ") + name)
    if not ok:
        fails.append(name)

history._db = None
db = Database(os.path.join(os.environ["DATA_DIR"], "t.sqlite3"))
history.init(db)
with db.connect() as conn:
    conn.execute("""INSERT INTO release (provider, scene_stashdb_id, entity_type, entity_stashdb_id, title, scene_created, scene_date, studio_name, status,
                    discovered_at, grabbed_title) VALUES ('stashdb', 'S1', 'studio', 'e', 'T', '2021-12-08T00:00:00Z', '2021-12-08', 'Bang Bus',
                    'sent_to_whisparr', datetime('now'), 'BangBus.21.12.08.Stella.Elle.XXX.mp4')""")
import quality_profile_repo  # noqa: E402
with db.connect() as conn:
    conn.execute("INSERT INTO quality_profile (id, name, cutoff_resolution, resolutions_json) VALUES (7, 'T', 720, '[1080, 720]')")
    conn.execute("INSERT OR REPLACE INTO setting (key, value) VALUES ('quality_profile_id', '7')")
    conn.execute("INSERT OR REPLACE INTO setting (key, value) VALUES ('fallback_accept_sd', 'false')")
d = tempfile.mkdtemp()
f = os.path.join(d, "a.mp4"); open(f, "wb").write(b"x")
quality_detect.probe_size = lambda p: (854, 480)

class Stash:
    def __init__(self, has): self.has = has; self.theporndb_endpoint = "x"
    def find_scene_by_stashdb_id(self, sid, endpoint=None): return {"id": "1"} if self.has else None

def run(stash):
    return fi._reject_unwanted_resolution(db, "stashdb", "S1", f, d, d, os.path.join(d, "bin"), stash_client=stash)

check("480p, scene not in the library: kept", run(Stash(False)) is None and os.path.exists(f))
check("no blocklist entry was made", not release_repo.get_review_rejected_titles(db, "stashdb", "S1"))
r = run(Stash(True))
check("480p, scene already in the library: rejected", r is not None and r["status"] == "failed")
print("FAILED: " + ", ".join(fails) if fails else "ALL OK")
sys.exit(1 if fails else 0)
