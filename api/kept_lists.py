"""Lists that take a Stash read to make are kept in the database until something changes them (v0.241.7).

Maintenance > Movies & groups ("Videos without a group", "Groups without files") and Compare groups each read the movie folders / all groups from
Stash on every opening (seconds). The answer is kept here (table maintenance_state, key "kept:<name>"): opening reads it at once, a write that
changes what the list shows (a group is created, a video is put into a group) calls invalidate(), and "Reload" asks for a new one."""
import json
from datetime import datetime, timezone


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def get(db, name, compute, refresh=False):
    """(data, kept_at 'YYYY-MM-DD HH:MM:SS' UTC): the kept answer, or compute() -- also when `refresh` -- stored and returned."""
    key = f"kept:{name}"
    if not refresh:
        with db.connect() as conn:
            row = conn.execute("SELECT value FROM maintenance_state WHERE key = ?", (key,)).fetchone()
        if row:
            kept = json.loads(row["value"])
            return kept["data"], kept["at"]
    data = compute()
    at = _now()
    with db.connect() as conn:
        conn.execute("INSERT INTO maintenance_state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                     (key, json.dumps({"at": at, "data": data})))
    return data, at


def invalidate(db, *names):
    """The next reading makes the list new."""
    with db.connect() as conn:
        for name in names:
            conn.execute("DELETE FROM maintenance_state WHERE key = ?", (f"kept:{name}",))


def update(db, name, change):
    """Changes a kept list in place (change(data) -> new data) when there is one; nothing is made new. Keeps its time."""
    key = f"kept:{name}"
    with db.connect() as conn:
        row = conn.execute("SELECT value FROM maintenance_state WHERE key = ?", (key,)).fetchone()
        if not row:
            return
        kept = json.loads(row["value"])
        kept["data"] = change(kept["data"])
        conn.execute("UPDATE maintenance_state SET value = ? WHERE key = ?", (json.dumps(kept), key))


MOVIE_LISTS = ("movies_no_group", "movies_groups_empty", "movies_group_namesakes")
