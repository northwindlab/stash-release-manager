"""A discarded wrong upgrade download leaves the candidate 'wanted', not 'grabbed', and keeps what was blocked (v0.262.3)."""
import json, os, sys, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "api"))
os.environ["DATA_DIR"] = tempfile.mkdtemp()
import release_repo, history, upgrades  # noqa: E402
from db import Database  # noqa: E402

fails = []
def check(name, ok):
    print(("ok   " if ok else "FAIL ") + name)
    if not ok:
        fails.append(name)

db = Database(os.path.join(os.environ["DATA_DIR"], "t.sqlite3"))
history.init(db)
def cand(pid, path, status="grabbed", rejected=None):
    with db.connect() as c:
        c.execute("INSERT INTO upgrade_candidate (scene_id, file_path, provider, provider_scene_id, reason, status, rejected_titles) "
                  "VALUES (?,?,'stashdb',?,'vr_low_quality',?,?)", (pid, path, pid, status, json.dumps(rejected) if rejected else None))
def row(pid, entity, path, status="sent_to_whisparr", job=None, code=None, imported=None, blocked=None):
    with db.connect() as c:
        c.execute("INSERT INTO release (provider, scene_stashdb_id, entity_type, entity_stashdb_id, title, scene_created, discovered_at, status, "
                  "upgrade_of, download_job_id, import_error_code, imported_at, review_rejected_titles) "
                  "VALUES ('stashdb',?, 'studio', ?, 'T', '2020-01-01', '2020-01-01', ?, ?, ?, ?, ?, ?)",
                  (pid, entity, status, path, job, code, imported, json.dumps(blocked) if blocked else None))
rows = lambda pid: release_repo.list_rows_for_scene(db, "stashdb", pid)
def candidate(pid):
    with db.connect() as c:
        return dict(c.execute("SELECT * FROM upgrade_candidate WHERE provider_scene_id = ?", (pid,)).fetchone())

# a library scene outside the watchlist: only the row made for the upgrade
cand("A", "/a", rejected=["Old.Rejected"]); row("A", "upgrade:A", "/a", blocked=["Bad.Release", "hash:123"])
check("goes back", upgrades.return_to_upgrade_list(db, "stashdb", "A") is True)
c = candidate("A")
check("candidate wanted", c["status"] == "wanted" and c["review_candidate"] is None)
check("blocked entries moved to the candidate", set(json.loads(c["rejected_titles"])) == {"Old.Rejected", "Bad.Release", "hash:123"})
check("the row made for the upgrade is gone", rows("A") == [])
upgrades._prepare_release_rows(db, c, {"title": "T", "date": "2020-01-01", "studio": {}}, True)
check("the next search's row knows what is blocked", set(release_repo.get_review_rejected_titles(db, "stashdb", "A")) == {"Old.Rejected", "Bad.Release", "hash:123"})

# a watched scene: its own row goes back as it was
cand("B", "/b"); row("B", "studio-1", "/b", imported="2020-02-02", blocked=["X"])
upgrades.return_to_upgrade_list(db, "stashdb", "B")
r = rows("B")[0]
check("own row imported again, no upgrade mark", r["status"] == "imported" and r["upgrade_of"] is None)

# moved to the wishlist by the user: left alone
cand("C", "/c", status="wishlist"); row("C", "upgrade:C", "/c", blocked=["X"])
check("a 'wishlist' candidate is left alone", upgrades.return_to_upgrade_list(db, "stashdb", "C") is False and rows("C")[0]["upgrade_of"] == "/c")
check("an ordinary scene is no upgrade", upgrades.return_to_upgrade_list(db, "stashdb", "nope") is False)

# start-up repair: only the stranded one
cand("D", "/d"); row("D", "upgrade:D", "/d", blocked=["X"])
cand("E", "/e"); row("E", "upgrade:E", "/e", job="j1", blocked=["X"])
cand("F", "/f"); row("F", "upgrade:F", "/f", status="import_failed", code="fingerprint_mismatch", blocked=["X"])
cand("G", "/g"); row("G", "upgrade:G", "/g", code="source_missing", blocked=["X"])
cand("H", "/h"); row("H", "upgrade:H", "/h")
check("repairs exactly the stranded one", upgrades.repair_stranded_grabs(db) == 1 and candidate("D")["status"] == "wanted")
check("downloading, failed, waiting for its file, nothing blocked: untouched", all(candidate(p)["status"] == "grabbed" for p in "EFGH"))
check("a second run changes nothing", upgrades.repair_stranded_grabs(db) == 0)
print("FAILED: " + ", ".join(fails) if fails else "ALL OK")
sys.exit(1 if fails else 0)
