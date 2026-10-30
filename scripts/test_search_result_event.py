"""Tests (v0.246.14): a changed search result / review candidate is announced to the open pages (release_changed), an unchanged one is not.
No network, throw-away DB. Run:  venv/bin/python scripts/test_search_result_event.py"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))
os.environ["DATA_DIR"] = tempfile.mkdtemp()

import events  # noqa: E402
import release_repo  # noqa: E402
from db import Database  # noqa: E402

FAILS = []
def check(name, cond):  # noqa: E302
    print(("ok   " if cond else "FAIL ") + name)
    if not cond: FAILS.append(name)

sent = []
events.publish = lambda kind, **kw: sent.append((kind, kw))

db = Database(os.path.join(os.environ["DATA_DIR"], "t.sqlite3"))
db.init() if hasattr(db, "init") else None
with db.connect() as conn:
    release_repo.insert_release(db, conn, {"id": "s1", "title": "Scene One", "date": "2026-01-01", "studio": {"id": "st", "name": "Studio"}, "performers": [], "created": "2026-01-01T00:00:00Z"},
                                "stashdb", "studio", "st", status="sent_to_whisparr")
sent.clear()
release_repo.set_search_result_for_scene(db, "stashdb", "s1", "no_candidates")
check("a new search result is announced", [k for k, _ in sent] == ["release_changed"] and sent[0][1]["scene_id"] == "s1")
sent.clear()
release_repo.set_search_result_for_scene(db, "stashdb", "s1", "no_candidates")
check("the same result again is not", not sent)
release_repo.set_search_result_for_scene(db, "stashdb", "s1", "needs_review")
sent.clear()
release_repo.set_review_candidate_for_scene(db, "stashdb", "s1", {"title": "X.XXX.1080p"})
check("a new review candidate is announced", len(sent) == 1)
sent.clear()
release_repo.set_review_candidate_for_scene(db, "stashdb", "s1", {"title": "X.XXX.1080p"})
check("the same candidate again is not", not sent)
sent.clear()
release_repo.set_review_candidate_for_scene(db, "stashdb", "s1", None)
check("clearing the candidate is announced", len(sent) == 1)

print("\nALL OK" if not FAILS else "\nFAILED: %s" % FAILS)
sys.exit(1 if FAILS else 0)
