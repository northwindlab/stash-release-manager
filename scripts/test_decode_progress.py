"""Tests (v0.246.15): the check of a finished encode reports its progress, and still finds a damaged file. Needs ffmpeg. Run:  venv/bin/python scripts/test_decode_progress.py"""
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))
import transcode  # noqa: E402

FAILS = []
def check(name, cond):  # noqa: E302
    print(("ok   " if cond else "FAIL ") + name)
    if not cond: FAILS.append(name)

if not shutil.which("ffmpeg"):
    print("skip (no ffmpeg)")
    sys.exit(0)
d = tempfile.mkdtemp()
good = os.path.join(d, "good.mp4")
subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25", "-t", "10", "-pix_fmt", "yuv420p", good], check=True)
seen = []
check("a good file passes with progress", transcode.decode_problem(good, progress=lambda p, s, l: seen.append(p), duration=10.0) is None)
check("progress was reported", bool(seen) and all(0 <= p <= 99 for p in seen))
data = open(good, "rb").read()
bad = os.path.join(d, "bad.mp4")
open(bad, "wb").write(data[:20000] + b"\x00" * 3000 + data[23000:])
check("a damaged file is found with progress", transcode.decode_problem(bad, progress=lambda *a: None, duration=10.0) is not None)
check("... and without it", transcode.decode_problem(bad) is not None)
print("\nALL OK" if not FAILS else "\nFAILED: %s" % FAILS)
sys.exit(1 if FAILS else 0)
