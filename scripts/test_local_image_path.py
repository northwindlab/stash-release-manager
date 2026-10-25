"""Maintenance > Performers without scenes: picture links from Stash are sent as a path, not with Stash's internal address."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))
import library_movies as lm

fails = []
def check(name, cond):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond: fails.append(name)

check("internal https address is dropped", lm._local_path("https://stash.internal:9997/performer/1593/image?t=1766698926") == "/performer/1593/image?t=1766698926")
check("internal http address is dropped", lm._local_path("http://stash.example:9999/performer/7/image") == "/performer/7/image")
check("a path stays", lm._local_path("/performer/7/image?t=1") == "/performer/7/image?t=1")
check("none stays none", lm._local_path(None) is None)
print("\nALL OK" if not fails else f"\n{len(fails)} FAILED")
sys.exit(1 if fails else 0)
