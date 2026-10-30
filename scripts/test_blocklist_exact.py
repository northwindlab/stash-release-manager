"""Tests (v0.189.0): a download rejected for its FILE (below the quality profile, unreadable) blocks only that exact release name; a release rejected
as such (wrong scene ...) still blocks its other encodes. No network, throw-away DB. Run:  venv/bin/python scripts/test_blocklist_exact.py"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))
os.environ["DATA_DIR"] = tempfile.mkdtemp()

import indexer_search as ix  # noqa: E402
import release_repo  # noqa: E402
from db import Database  # noqa: E402

FAILS = []
def check(name, cond):  # noqa: E302
    print(("ok   " if cond else "FAIL ") + name)
    if not cond: FAILS.append(name)

SMALL = "ExampleStudio.26.10.04.Jane.Doe.REMASTERED.XXX.MP4-GRPX"
HD = "ExampleStudio.26.10.04.Jane.Doe.REMASTERED.XXX.1080p.HEVC.x265.PRT"
P720 = "ExampleStudio.26.10.04.Jane.Doe.REMASTERED.XXX.720p.HEVC.x265.PRT"

exact = ix._split_rejected([f"exact:{SMALL}"])
check("the exact release name is blocked", ix._is_rejected(SMALL, exact))
check("... also written with other separators / case", ix._is_rejected(SMALL.lower().replace(".", " "), exact))
check("the 1080p encode of the same release is NOT blocked (the real case)", not ix._is_rejected(HD, exact))
check("the 720p encode is NOT blocked", not ix._is_rejected(P720, exact))

plain = ix._split_rejected([SMALL])
check("a plain entry still blocks the other encodes (v0.174.0 behaviour kept)", ix._is_rejected(HD, plain) and ix._is_rejected(P720, plain) and ix._is_rejected(SMALL, plain))
mixed = ix._split_rejected([f"exact:{SMALL}", "Other.Release.XXX.1080p"])
check("both kinds side by side", ix._is_rejected(SMALL, mixed) and ix._is_rejected("Other.Release.XXX.720p", mixed) and not ix._is_rejected(HD, mixed))
check("an empty list blocks nothing", not ix._is_rejected(SMALL, ix._split_rejected([])))

DB = Database(os.path.join(tempfile.mkdtemp(), "t.sqlite3"))
with DB.connect() as c:
    release_repo.insert_release(DB, c, {"id": "s1", "title": "T", "created": "2026-10-09T10:00:00Z", "date": "2026-10-04"}, "stashdb", "studio", "st1")
    release_repo.insert_release(DB, c, {"id": "s1", "title": "T", "created": "2026-10-09T10:00:00Z", "date": "2026-10-04"}, "stashdb", "performer", "p1")
release_repo.add_review_rejected_title(DB, "stashdb", "s1", SMALL, exact=True)
release_repo.add_review_rejected_title(DB, "stashdb", "s1", "Wrong.Scene.XXX.1080p")
got = release_repo.get_review_rejected_titles(DB, "stashdb", "s1")
check("stored as 'exact:<title>' next to the plain one, on every row of the scene", got == [f"exact:{SMALL}", "Wrong.Scene.XXX.1080p"])
release_repo.add_review_rejected_title(DB, "stashdb", "s1", SMALL, exact=True)
check("no duplicate when added twice", len(release_repo.get_review_rejected_titles(DB, "stashdb", "s1")) == 2)

# v0.246.13: "Not this one" on a review candidate (the 480p of a release) blocks only that file, the 1080p of the same release stays a candidate
src = open(os.path.join(os.path.dirname(__file__), "..", "api", "routers", "releases.py"), encoding="utf-8").read()
check("reject of a review candidate stores an exact entry", 'candidate["title"], exact=True)' in src)
check("the undo also removes the exact entry", "indexer_search.EXACT_PREFIX + candidate" in src)

print("\nALL OK" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
sys.exit(1 if FAILS else 0)
