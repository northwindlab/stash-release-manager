"""A failed download blocks only its exact release name (v0.260.5), the old plain entries are repaired, and "all blocklisted" is its own search result."""
import os, sys, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "api"))
os.environ["DATA_DIR"] = tempfile.mkdtemp()
import download_failures as df, release_repo, history, indexer_search as ix  # noqa: E402
from db import Database  # noqa: E402

fails = []
def check(name, ok):
    print(("ok   " if ok else "FAIL ") + name)
    if not ok:
        fails.append(name)

history._db = None
db = Database(os.path.join(os.environ["DATA_DIR"], "t.sqlite3"))
history.init(db)
XV = "BangBus.21.12.08.Stella.Elle.XXX.XviD-iPT.Team"
HD = "BangBus.21.12.08.Stella.Elle.XXX.1080p.MP4-GRP"
def add(sid):
    with db.connect() as conn:
        conn.execute("""INSERT INTO release (provider, scene_stashdb_id, entity_type, entity_stashdb_id, title, scene_created, scene_date, studio_name, status,
                        discovered_at) VALUES ('stashdb', ?, 'studio', 'e', 'T', '2021-12-08T00:00:00Z', '2021-12-08', 'Bang Bus', 'sent_to_whisparr', datetime('now'))""", (sid,))
add("S1"); add("S2")
# new failures are exact
df.handle_failed(db, {"provider": "stashdb", "scene_stashdb_id": "S1"}, {"name": XV, "fail_message": "Aborted"})
split = ix._split_rejected(release_repo.get_review_rejected_titles(db, "stashdb", "S1"))
check("a failed download is blocked by its exact name", ix._is_rejected(XV, split) and not ix._is_rejected(HD, split))
# old plain entry (before 0.260.5) is repaired
release_repo.add_review_rejected_title(db, "stashdb", "S2", XV)
history.scene("warning", "download", f"Download failed in SABnzbd: {XV} -- Aborted, cannot be completed", "stashdb", "S2")
split = ix._split_rejected(release_repo.get_review_rejected_titles(db, "stashdb", "S2"))
check("before: the plain entry blocks the 1080p release too", ix._is_rejected(HD, split))
check("repair converts one entry", df.convert_failed_blocklists(db) == 1)
split = ix._split_rejected(release_repo.get_review_rejected_titles(db, "stashdb", "S2"))
check("after: only the exact name is blocked", ix._is_rejected(XV, split) and not ix._is_rejected(HD, split))
check("idempotent", df.convert_failed_blocklists(db) == 0)
# a title the user rejected by hand stays plain
add("S3")
release_repo.add_review_rejected_title(db, "stashdb", "S3", XV)
history.scene("warning", "download", f"Download failed in SABnzbd: {XV} -- Aborted", "stashdb", "S3")
history.scene("warning", "blocklist", f"Possible match rejected (blocklisted): {XV}", "stashdb", "S3")
check("a hand-made rejection is left alone", df.convert_failed_blocklists(db) == 0
      and ix._is_rejected(HD, ix._split_rejected(release_repo.get_review_rejected_titles(db, "stashdb", "S3"))))
print("FAILED: " + ", ".join(fails) if fails else "ALL OK")
sys.exit(1 if fails else 0)
