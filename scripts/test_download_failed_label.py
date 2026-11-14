"""A failed download (v0.221.3): the scene shows the reason instead of "grabbed", and the manual search marks releases the scene was through
already. Run: venv/bin/python scripts/test_download_failed_label.py"""
import os, sys, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))
from db import Database
import release_repo, download_failures, indexer_search

res = []
def check(name, cond):
    res.append(bool(cond)); print(("ok   " if cond else "FAIL ") + name)

db = Database(os.path.join(tempfile.mkdtemp(), "t.sqlite3"))
scene = {"id": "s1", "title": "Rosie", "date": "2026-09-11", "created": "2026-09-11T00:00:00Z"}
with db.connect() as c:
    release_repo.insert_release(db, c, scene, "stashdb", "studio", "e1")
    c.execute("UPDATE release SET status='sent_to_whisparr', download_job_id='j1', grabbed_title='X.26.09.12.Rosie.XXX.2160p.MP4-GRP', last_search_result='grabbed' WHERE scene_stashdb_id='s1'")
row = dict(release_repo.list_releases(db, "sent_to_whisparr")[0])
download_failures.handle_failed(db, row, {"name": "X.26.09.12.Rosie.XXX.2160p.MP4-GRP", "fail_message": "Duplicate NZB", "client": "SABnzbd"})
r = release_repo.list_releases(db, "sent_to_whisparr")[0]
check("job removed, result says why instead of 'grabbed'", r["download_job_id"] is None and r["last_search_result"] == "download failed: Duplicate NZB")
split = indexer_search._split_rejected(release_repo.get_review_rejected_titles(db, "stashdb", "s1"))
check("the failed release is recognised again by its exact name (v0.260.5: another encode is NOT blocked any more)", indexer_search._is_rejected("X.26.09.12.Rosie.XXX.2160p.MP4-GRP", split)
      and not indexer_search._is_rejected("X.26.09.12.Rosie.XXX.1080p.MP4-GRP", split))
check("another release of the scene is not marked", not indexer_search._is_rejected("Y.26.09.12.Rosie.XXX.1080p.MP4-OTHER", split))
print("\nALL OK" if all(res) else "\nFAILED")
sys.exit(0 if all(res) else 1)
