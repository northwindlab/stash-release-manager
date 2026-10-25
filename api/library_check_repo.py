"""CRUD for the Maintenance page's Library Check runs and findings (see
db.py's library_check_run/library_check_finding tables, added 2026-09-16)."""
import json


def create_run(db, scope_paths, provider="stashdb"):
    with db.connect() as conn:
        cur = conn.execute(
            "INSERT INTO library_check_run (scope_paths, provider) VALUES (?, ?)",
            (json.dumps(scope_paths or []), provider or "stashdb"),
        )
        return cur.lastrowid


def update_run_progress(db, run_id, current, total):
    with db.connect() as conn:
        conn.execute(
            "UPDATE library_check_run SET current = ?, total = ? WHERE id = ?",
            (current, total, run_id),
        )


def finish_run(db, run_id, status, match_count=0, mismatch_count=0, unverifiable_count=0, error=None):
    with db.connect() as conn:
        conn.execute(
            """UPDATE library_check_run
               SET status = ?, finished_at = datetime('now'),
                   match_count = ?, mismatch_count = ?, unverifiable_count = ?, error = ?
               WHERE id = ?""",
            (status, match_count, mismatch_count, unverifiable_count, error, run_id),
        )


def get_run(db, run_id):
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM library_check_run WHERE id = ?", (run_id,)).fetchone()
        return dict(row) if row else None


def get_latest_run(db):
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM library_check_run ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None


def insert_finding(db, run_id, scene_id, scene_title, kind, expected_stashdb_id=None,
                    candidates=None, file_paths=None, reason=None):
    """Deletes any existing `pending` finding for the same scene first
    (from an earlier run) so a scene never has more than one live
    pending finding at once -- past applied/dismissed/failed rows are
    left alone, they're history, not live state."""
    with db.connect() as conn:
        conn.execute(
            "DELETE FROM library_check_finding WHERE scene_id = ? AND status = 'pending'",
            (str(scene_id),),
        )
        conn.execute(
            """INSERT INTO library_check_finding
               (run_id, scene_id, scene_title, kind, expected_stashdb_id, candidates_json, file_paths, reason)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (run_id, str(scene_id), scene_title, kind, expected_stashdb_id,
             json.dumps(candidates) if candidates is not None else None,
             json.dumps(file_paths) if file_paths is not None else None, reason),
        )


def list_findings(db, status="pending", kind=None):
    query = ("SELECT f.*, COALESCE(r.provider, 'stashdb') AS provider FROM library_check_finding f "
             "LEFT JOIN library_check_run r ON r.id = f.run_id WHERE 1=1")
    params = []
    if status:
        query += " AND f.status = ?"
        params.append(status)
    if kind:
        query += " AND f.kind = ?"
        params.append(kind)
    query += " ORDER BY f.id DESC"
    with db.connect() as conn:
        rows = conn.execute(query, params).fetchall()
        findings = []
        for r in rows:
            d = dict(r)
            d["candidates"] = json.loads(d["candidates_json"]) if d["candidates_json"] else []
            d["file_paths"] = json.loads(d["file_paths"]) if d["file_paths"] else []
            findings.append(d)
        return findings


def get_finding(db, finding_id):
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM library_check_finding WHERE id = ?", (finding_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["candidates"] = json.loads(d["candidates_json"]) if d["candidates_json"] else []
        d["file_paths"] = json.loads(d["file_paths"]) if d["file_paths"] else []
        return d


def set_finding_status(db, finding_id, status, applied_title=None, error=None):
    with db.connect() as conn:
        conn.execute(
            """UPDATE library_check_finding
               SET status = ?, applied_title = ?, error = ?, resolved_at = datetime('now')
               WHERE id = ?""",
            (status, applied_title, error, finding_id),
        )


def delete_finding(db, finding_id):
    """A finding that is no longer true (the re-check found the scene matches after all): gone, not 'resolved' -- the status column only knows
    pending / applied / failed / dismissed."""
    with db.connect() as conn:
        conn.execute("DELETE FROM library_check_finding WHERE id = ?", (finding_id,))


def provider_of_finding(db, finding):
    """The provider the run that made this finding asked ('stashdb' when unknown)."""
    run = get_run(db, finding.get("run_id")) if finding.get("run_id") else None
    return (run or {}).get("provider") or "stashdb"
