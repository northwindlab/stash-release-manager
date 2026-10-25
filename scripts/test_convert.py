"""Tests (v0.245.0): the Convert page's candidate list, job flow and file swap (api/convert.py) with a FAKED Stash and throwaway folders.
Run: venv/bin/python scripts/test_convert.py"""
import os, sys, tempfile, threading
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))
TMP = tempfile.mkdtemp()
os.environ["DATA_DIR"] = TMP
import convert
import history
from db import Database

FAILS = []
def check(name, cond):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond: FAILS.append(name)

history._db = None  # history.add is a no-op without a database
LIB = os.path.join(TMP, "lib"); os.makedirs(LIB + "/a")
to_local = lambda p: LIB + p[len("/data"):]

def f(id, path, fmt, v, a="mp3", w=640, h=352, size=1000, dur=100.0):
    return {"id": id, "path": path, "format": fmt, "video_codec": v, "audio_codec": a, "bit_rate": 800000, "width": w, "height": h, "size": size, "duration": dur}

class Stash:
    def __init__(self):
        self.scenes = {"1": [f("f1", "/data/a/old.avi", "avi", "mpeg4")], "2": [f("f2", "/data/a/old2.flv", "flv", "h264", "aac")],
                       "3": [f("f3", "/data/a/ok.mp4", "mp4", "h264", "aac")], "4": [f("f4", "/data/a/hevc.avi", "avi", "hevc")],
                       "5": [f("f5", "/data/a/up.wmv", "wmv", "wmv2", "wmav2")], "6": [f("f6", "/data/a/same.mp4", "mp4", "mpeg4", "aac")],
                       "7": [f("f7", "/data/a/vr.avi", "avi", "mpeg4", w=5760, h=2880)], "8": [f("f8", "/data/a/ac3.mkv", "matroska", "h264", "ac3")]}
        self.log = []; self.next_scene = 100; self.fail_scan = False
    def _graphql(self, q, v):  # like the real client: variables are required
        return {"findScenes": {"scenes": [{"id": i, "title": f"T{i}", "files": fs} for i, fs in self.scenes.items()]}}
    def scene_files(self, sid): return [dict(x) for x in self.scenes[str(sid)]] if str(sid) in self.scenes else None
    def scan_paths(self, paths):
        self.log.append("scan")
        folder = paths[0]
        for fname in os.listdir(to_local(folder)):
            p = folder + "/" + fname
            if self.fail_scan or any(x["path"] == p for fs in self.scenes.values() for x in fs):
                continue
            if fname.endswith(".mp4"):  # a new file -> a new scene of its own (a rename of a known file would keep its scene: see below)
                self.next_scene += 1
                self.scenes[str(self.next_scene)] = [{"id": f"nf{self.next_scene}", "path": p, "format": "mp4", "video_codec": "h264"}]
        return "job"
    def wait_for_job(self, j): pass
    def find_scene_by_path(self, p):
        for i, fs in self.scenes.items():
            if any(x["path"] == p for x in fs): return {"id": i}
    def merge_scene_into(self, src, dst):
        self.log.append(f"merge {src}->{dst}"); self.scenes[str(dst)] += self.scenes.pop(str(src))
    def set_primary_file(self, sid, fid): self.log.append(f"primary {fid}")
    def clean_paths(self, p): self.log.append("clean")
    def generate_metadata_for_scenes(self, ids): self.log.append("generate")

# ---- classification
check("old video codec in avi -> reencode", convert.classify(f("x", "/a.avi", "avi", "mpeg4")) == "reencode")
check("wmv -> reencode", convert.classify(f("x", "/a.wmv", "wmv", "wmv3", "wmav2")) == "reencode")
check("h264 in flv -> remux", convert.classify(f("x", "/a.flv", "flv", "h264", "aac")) == "remux")
check("h264 in mp4 / mkv -> nothing", convert.classify(f("x", "/a.mp4", "mp4", "h264", "aac")) is None and convert.classify(f("x", "/a.mkv", "matroska", "h264", "ac3")) is None)
check("hevc is not converted here", convert.classify(f("x", "/a.avi", "avi", "hevc")) is None)
check("VR is not converted here", convert.classify(f("x", "/a.avi", "avi", "mpeg4", w=5760, h=2880)) is None)
check("an unknown codec is left alone", convert.classify(f("x", "/a", "avi", None)) is None)

db = Database(os.path.join(TMP, "t.sqlite3"))
with db.connect() as conn:  # scene 5's file is searchable in Upgrade wanted (provider id); scene 1's has no id
    conn.execute("INSERT INTO upgrade_candidate (scene_id, file_path, reason, status, provider_scene_id) VALUES ('5', '/data/a/up.wmv', 'below_cutoff', 'wanted', 'P5')")
    conn.execute("INSERT INTO upgrade_candidate (scene_id, file_path, reason, status) VALUES ('1', '/data/a/old.avi', 'below_cutoff', 'wanted')")  # no provider id: cannot be searched
st = Stash()
r = convert.refresh(db, st)
rows = {x["scene_id"]: x for x in convert.rows(db)}
check("refresh lists the old-format files only", sorted(rows) == ["1", "2", "6"], )
check("refresh leaves out a file Upgrade wanted can search for (provider id), but keeps one without id", "5" not in rows)
check("refresh counts modes", r["remux"] == 1 and r["reencode"] == 2 and r["wanted"] == 3 and r["added"] == 3)
check("badge", convert.badge(db) == 3)
r2 = convert.refresh(db, st)
check("a second refresh adds nothing", r2["added"] == 0 and r2["wanted"] == 3)

# ---- queue / worker flow
check("queue: wanted -> queued", convert.queue(db, [rows["1"]["id"], rows["2"]["id"]]) == 2)
check("queue: queued again does nothing", convert.queue(db, [rows["1"]["id"]]) == 0)
job = convert.claim_next(db, "w1", to_local)
check("claim: oldest queued row, paths as the sidecar sees them", job["job_id"] == rows["1"]["id"] and job["source"] == LIB + "/a/old.avi" and job["dest"] == LIB + "/a/old.mp4" and job["mode"] == "reencode")
check("progress: the holder may report", convert.progress(db, job["job_id"], "w1", 40, 2.0, 30) is True)
check("progress: another worker is refused", convert.progress(db, job["job_id"], "w2", 40, 2.0, 30) is False)
check("done by another worker is refused", convert.worker_done(db, job["job_id"], "w2", True, None, lambda r: None))
job2 = convert.claim_next(db, "w2", to_local)
check("a second worker gets the next row", job2["job_id"] == rows["2"]["id"] and job2["mode"] == "remux")
check("nothing left to claim", convert.claim_next(db, "w3", to_local) is None)
convert.cancel(db, [job2["job_id"]])
check("cancel: a running row goes back to wanted and its worker is told to stop", convert.progress(db, job2["job_id"], "w2", 5, 1, 1) is False)
with db.connect() as conn:  # lease expiry
    conn.execute("UPDATE convert_candidate SET status='running', worker='gone', beat_at=1 WHERE id=?", (job2["job_id"],))
check("an expired lease puts the row back into the queue", convert.claim_next(db, "w9", to_local)["job_id"] == job2["job_id"])
convert.cancel(db, [job2["job_id"]])
convert.worker_done(db, job2["job_id"], "w9", True, None, lambda r: None)  # not running any more -> refused, no change
check("done after a cancel is refused", convert.rows(db) and next(x for x in convert.rows(db) if x["id"] == job2["job_id"])["status"] == "wanted")

# ---- a failure reported by the worker
err = convert.worker_done(db, job["job_id"], "w1", False, "ffmpeg broke", lambda r: None)
row1 = next(x for x in convert.rows(db) if x["id"] == job["job_id"])
check("worker failure -> failed with the reason, retry possible", err is None and row1["status"] == "failed" and "ffmpeg broke" in row1["error"])
check("queue again from failed", convert.queue(db, [job["job_id"]]) == 1)

# ---- the swap: x.avi -> x.mp4
recycled = []
def recycle(p): recycled.append(p); os.rename(p, p + ".bin")
open(LIB + "/a/old.avi", "wb").write(b"o" * 10)
open(LIB + "/a/old.mp4", "wb").write(b"n" * 5)  # what the worker wrote
class Probe:  # a stand-in for ffprobe
    def __call__(self, p): return {"vcodec": "h264", "duration": 100.0}
job = convert.claim_next(db, "w1", to_local)
done = threading.Event()
convert.worker_done(db, job["job_id"], "w1", True, None, lambda r: (convert.finish(db, st, to_local, recycle, r, probe=Probe()), done.set()))
done.wait(10)
row1 = next(x for x in convert.history_rows(db, "done") if x["id"] == job["job_id"])
check("swap: row is done, new path is the .mp4, size recorded", row1["new_path"] == "/data/a/old.mp4" and row1["size_after"] == 5)
check("swap: the new file joined the old scene and is primary", [x["path"] for x in st.scenes["1"]] == ["/data/a/old.avi", "/data/a/old.mp4"] and "primary nf101" in st.log)
check("swap order: merge, primary, then the old file leaves, then clean and generate", st.log[:5] == ["scan", "merge 101->1", "primary nf101", "clean", "generate"] and recycled == [LIB + "/a/old.avi"])
check("swap: the old file is gone from the library folder, the new one stays", not os.path.exists(LIB + "/a/old.avi") and os.path.exists(LIB + "/a/old.mp4"))
check("converted_paths knows the new file", convert.converted_paths(db) == {"/data/a/old.mp4"})

# ---- failures leave the original alone
st2 = Stash(); st2.scenes = {"9": [f("f9", "/data/a/bad.avi", "avi", "mpeg4")]}
open(LIB + "/a/bad.avi", "wb").write(b"o")
with db.connect() as conn:
    conn.execute("INSERT INTO convert_candidate (scene_id, file_path, scene_title, mode, status, duration, size) VALUES ('9', '/data/a/bad.avi', 'Bad', 'reencode', 'verifying', 100, 1)")
    bad = dict(conn.execute("SELECT * FROM convert_candidate WHERE scene_id='9'").fetchone())
convert.finish(db, st2, to_local, recycle, bad, probe=Probe())
b = next(x for x in convert.rows(db) if x["scene_id"] == "9")
check("a missing converted file fails the row, the original stays", b["status"] == "failed" and "missing" in b["error"] and os.path.exists(LIB + "/a/bad.avi"))
open(LIB + "/a/bad.mp4", "wb").write(b"n")
class WrongLen:
    def __call__(self, p): return {"vcodec": "h264", "duration": 50.0}
convert.finish(db, st2, to_local, recycle, bad, probe=WrongLen())
b = next(x for x in convert.rows(db) if x["scene_id"] == "9")
check("a wrong length fails the row and removes the bad result", b["status"] == "failed" and "long" in b["error"] and not os.path.exists(LIB + "/a/bad.mp4") and os.path.exists(LIB + "/a/bad.avi"))
open(LIB + "/a/bad.mp4", "wb").write(b"n")
st2.fail_scan = True
convert.finish(db, st2, to_local, recycle, bad, probe=Probe())
b = next(x for x in convert.rows(db) if x["scene_id"] == "9")
check("Stash not seeing the file: failed with a hint, both files kept", b["status"] == "failed" and "picked up" in b["error"] and os.path.exists(LIB + "/a/bad.avi") and os.path.exists(LIB + "/a/bad.mp4"))

# ---- an .mp4 with an old codec: written under a temporary name, renamed onto the original after the swap
st3 = Stash(); st3.scenes = {"6": [f("f6", "/data/a/same.mp4", "mp4", "mpeg4", "aac")]}
open(LIB + "/a/same.mp4", "wb").write(b"o" * 10)
open(LIB + "/a/same.srm-convert.mp4", "wb").write(b"n" * 4)
orig_scan = st3.scan_paths
def scan_with_move(paths):
    r = orig_scan(paths)
    for x in st3.scenes["6"]:  # Stash follows the renamed file by its fingerprint
        if x["path"].endswith("srm-convert.mp4") and not os.path.exists(to_local(x["path"])):
            x["path"] = "/data/a/same.mp4"
    return r
st3.scan_paths = scan_with_move
with db.connect() as conn:
    conn.execute("UPDATE convert_candidate SET status = 'verifying', duration = 100, size = 10 WHERE scene_id = '6'")
    same = dict(conn.execute("SELECT * FROM convert_candidate WHERE scene_id='6'").fetchone())
recycled.clear()
convert.finish(db, st3, to_local, recycle, same, probe=Probe())
s6 = next(x for x in convert.history_rows(db, "done") if x["scene_id"] == "6")
check("same-name case: done, final path is the original name", s6["new_path"] == "/data/a/same.mp4" and s6["size_after"] == 4)
check("same-name case: the converted bytes sit on the original name, the temporary name is gone", open(LIB + "/a/same.mp4", "rb").read() == b"n" * 4 and not os.path.exists(LIB + "/a/same.srm-convert.mp4"))
check("same-name case: the old file went to the recycle bin first", recycled == [LIB + "/a/same.mp4"])

# ---- resume after a restart
n = 0
def fake_finish(*a, **k):
    global n; n += 1
with db.connect() as conn:
    conn.execute("UPDATE convert_candidate SET status='verifying' WHERE scene_id='9'")
orig = convert.finish; convert.finish = fake_finish
convert.resume(db, st2, to_local, recycle)
import time; time.sleep(0.5); convert.finish = orig
check("resume: rows that were being checked continue", n == 1)

# ---- dismiss / restore
w = next(x for x in convert.rows(db) if x["scene_id"] == "2")
check("dismiss: wanted -> dismissed, restore brings it back", convert.dismiss(db, [w["id"]]) == 1 and convert.badge(db) == convert.badge(db) and convert.dismiss(db, [w["id"]], restore=True) == 1)
print("\nALL OK" if not FAILS else f"\n{len(FAILS)} FAILED")
sys.exit(1 if FAILS else 0)
