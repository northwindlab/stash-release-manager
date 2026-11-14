"""'Grab anyway' (v0.260.3): the duplicate guard's force mode keeps only the queue check; the earlier SABnzbd entries are cleared before the add."""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "api"))
import indexer_search as ix  # noqa: E402

fails = []
def check(name, ok):
    print(("ok   " if ok else "FAIL ") + name)
    if not ok:
        fails.append(name)

T = "Studio.26.10.20.Jane.Roe.Blonde.Disturbance.In.The.Bedroom.XXX.1080p.MP4-GRP"
class Sab:
    def __init__(self, queue=(), history=(), archive=()):
        self.queue, self.history, self.archive, self.deleted = list(queue), list(history), list(archive), []
    def get_queue(self): return self.queue
    def get_history(self, search=None, limit=50): return self.history
    def get_archive(self, limit=5000): return self.archive
    def delete_history(self, nzo, del_files=False): self.deleted.append(("h", nzo))
    def delete_archived(self, nzo): self.deleted.append(("a", nzo))

hist = Sab(history=[{"name": T, "status": "Completed", "nzo_id": "H1"}])
check("history entry refuses a normal grab", ix._duplicate_guard(hist, T) is not None)
check("... but not a forced one", ix._duplicate_guard(hist, T, force=True) is None)
failed = Sab(history=[{"name": T, "status": "Failed", "nzo_id": "H1"}, {"name": T, "status": "Failed", "nzo_id": "H2"}])
check("failed twice refuses, forced does not", ix._duplicate_guard(failed, T) and ix._duplicate_guard(failed, T, force=True) is None)
q = Sab(queue=[{"filename": T}])
check("a queued release is refused even when forced", ix._duplicate_guard(q, T, force=True) is not None)
s = Sab(history=[{"name": T, "nzo_id": "H1"}, {"name": "Other.Release", "nzo_id": "H9"}], archive=[{"name": T, "nzo_id": "A1"}])
ix._forget_in_sabnzbd(s, T)
check("only this release's history + archive entries are removed", s.deleted == [("h", "H1"), ("a", "A1")])

# ---- the daily cap: refused normally, sent (and still counted) when forced
import tempfile
os.environ["DATA_DIR"] = tempfile.mkdtemp()
from db import Database
db = Database(os.path.join(os.environ["DATA_DIR"], "t.sqlite3"))
with db.connect() as conn:
    conn.execute("INSERT OR REPLACE INTO setting (key, value) VALUES ('max_grabs_per_day', '1')")
ix._grab_counted(db)
class FakeSab(Sab):
    def __init__(self, *a, **k): super().__init__()
    def add_url(self, url, category=None, nzbname=None): return ["NZO1"]
ix.SabnzbdClient = FakeSab
ix.download_client_repo.pick_for_protocol = lambda db, protocol: {"id": 1, "type": "sabnzbd", "url": "u", "api_key": "k", "category": None, "name": "sab"}
cand = {"title": T, "downloadUrl": "http://i/x", "protocol": "usenet"}
check("cap reached: refused", ix.send_to_download_client(db, cand, lambda m: None)["result"] == ix.RESULT_DAILY_CAP)
r = ix.send_to_download_client(db, cand, lambda m: None, force=True)
check("cap reached + forced: sent", r["result"] == ix.RESULT_GRABBED and r["nzo_id"] == "NZO1")
check("... and counted", ix._grab_budget(db)[1] == 2)
print("FAILED: " + ", ".join(fails) if fails else "ALL OK")
sys.exit(1 if fails else 0)
