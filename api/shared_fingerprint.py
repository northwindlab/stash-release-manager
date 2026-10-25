"""A file whose fingerprint is registered at SEVERAL provider scenes (v0.246.0).

Fingerprints are crowd-sourced: when a studio re-releases the same footage (a network's VR scene re-sold by another site, a compilation that reuses a
clip, a re-encode), the same OSHASH / PHASH ends up on two or more scenes of the provider. A reverse lookup then returns all of them, and "the scene
the file is tagged with is among them" proves very little (found 2026-10-25: a Naughty America VR file was filed under WankzVR, four more files under
the wrong studio). `judge()` decides, from the length, whether the tag can still be trusted; what it cannot decide goes to the review list
(table `shared_fingerprint_review`, Maintenance > Organize library > Shared fingerprints) until the user has looked at it.
"""
import json
import logging

logger = logging.getLogger(__name__)

LENGTH_TOLERANCE = 0.06  # |file - scene| / longer one; a re-encode or an intro changes the length by a few percent, another cut by far more


def fits(file_seconds, scene_seconds):
    return bool(file_seconds and scene_seconds and abs(file_seconds - scene_seconds) / max(file_seconds, scene_seconds) <= LENGTH_TOLERANCE)


def _min(seconds):
    return f"{seconds / 60:.0f} min" if seconds else "no length"


def judge(file_seconds, tag_id, found, durations, kept=False):
    """{"ok", "how", "reason", "others"} for a file tagged with `tag_id` whose reverse lookup returned `found` ([{"id", "title"}], the tag among them).
    `durations`: {scene id: seconds or None}. `kept`: the user said the tag is right. reason: unique | kept | length_fits | also_fits | tag_off |
    other_fits | no_length."""
    others = [f for f in found if f["id"] != tag_id]
    if not others:
        return {"ok": True, "how": "fingerprint", "reason": "unique", "others": []}
    if kept:
        return {"ok": True, "how": "fingerprint (also registered at other scenes; you confirmed this one)", "reason": "kept", "others": others}
    tag_len = durations.get(tag_id)
    fitting = [o for o in others if fits(file_seconds, durations.get(o["id"]))]
    n = len(others)
    if tag_len and fits(file_seconds, tag_len):
        if not fitting:
            return {"ok": True, "how": f"fingerprint (also registered at {n} other scene{'s' if n != 1 else ''}; only this one has the file's length)",
                    "reason": "length_fits", "others": others}
        return {"ok": False, "reason": "also_fits", "others": others,
                "how": f"ambiguous: the file is registered at {n + 1} scenes and the length ({_min(file_seconds)}) fits {len(fitting) + 1} of them"}
    if tag_len:
        return {"ok": False, "reason": "other_fits" if fitting else "tag_off", "others": others,
                "how": f"ambiguous: the file ({_min(file_seconds)}) does not fit this scene ({_min(tag_len)})"
                       + (" but another scene it is registered at" if fitting else "; it is registered at other scenes too")}
    return {"ok": False, "reason": "no_length", "others": others,
            "how": f"ambiguous: the file is registered at {n + 1} scenes and the provider has no length for this one to compare"}


def split_exact(client, local_fps, found, tag_id):
    """([exact], [near]): `found` is what the provider's reverse lookup returned. Its PHASH lookup is a SIMILARITY search (a few bits of tolerance), so VR scenes
    with the same frame layout, black intros and the like come back although they are other videos (found 2026-10-25: 5 of 6 'other scenes' of one file
    were at picture-hash distance 4). Only a scene that has an exactly equal hash (OSHASH or PHASH, distance 0) registered is a real second owner of the
    file's fingerprint. `local_fps`: iterable of (algorithm, hash). The tag itself always counts as exact. A candidate whose fingerprints cannot be read
    stays in `exact` (the safe side)."""
    mine = {(a.upper(), h) for a, h in local_fps if h}
    exact, near = [], []
    for f in found:
        if f["id"] == tag_id:
            exact.append(f)
            continue
        try:
            theirs = {(x["algorithm"].upper(), x["hash"]) for x in client.get_scene_fingerprints(f["id"])}
        except Exception:  # noqa: BLE001
            logger.exception("shared fingerprint: fingerprints of %s could not be read", f["id"])
            exact.append(f)
            continue
        (exact if mine & theirs else near).append(f)
    return exact, near


def settle(db, scene_id):
    """The scene is fine now (a later check found a unique or length-confirmed match): an open row is closed."""
    with db.connect() as conn:
        conn.execute("UPDATE shared_fingerprint_review SET status = 'resolved', resolved_at = datetime('now') WHERE scene_id = ? AND status = 'open'", (str(scene_id),))


def enrich(client, found, durations, near=()):
    """The candidates with studio and date for the review list (a few detail requests, only for scenes that need a decision)."""
    out = []
    for f in found:
        entry = {"id": f["id"], "title": f.get("title"), "seconds": durations.get(f["id"]), "studio": None, "date": None}
        try:
            d = client.get_scene_detail(f["id"]) or {}
            entry.update(title=d.get("title") or entry["title"], date=d.get("date"), studio=(d.get("studio") or {}).get("name"))
        except Exception:  # noqa: BLE001
            logger.exception("shared fingerprint: detail of %s failed", f["id"])
        out.append(entry)
    for f in near:  # similar picture hash only: listed for understanding, not counted
        out.append({"id": f["id"], "title": f.get("title"), "seconds": durations.get(f["id"]), "studio": None, "date": None, "near": True})
    return out


# ---------- the review list ----------

def upsert(db, scene_id, local_title, path, file_seconds, provider, tag_id, reason, how, candidates):
    """Puts a scene on the review list. A scene the user already settled for this very tag stays settled."""
    with db.connect() as conn:
        row = conn.execute("SELECT tag_id, status FROM shared_fingerprint_review WHERE scene_id = ?", (str(scene_id),)).fetchone()
        status = "open"
        if row and row["tag_id"] == tag_id and row["status"] in ("kept", "retagged", "dismissed"):
            status = row["status"]
        conn.execute("""INSERT INTO shared_fingerprint_review (scene_id, local_title, file_path, file_seconds, provider, tag_id, reason, how, candidates, status, found_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                        ON CONFLICT (scene_id) DO UPDATE SET local_title = excluded.local_title, file_path = excluded.file_path, file_seconds = excluded.file_seconds,
                          provider = excluded.provider, tag_id = excluded.tag_id, reason = excluded.reason, how = excluded.how, candidates = excluded.candidates,
                          status = excluded.status, resolved_at = CASE WHEN excluded.status = 'open' THEN NULL ELSE shared_fingerprint_review.resolved_at END""",
                     (str(scene_id), local_title, path, file_seconds, provider, tag_id, reason, how, json.dumps(candidates, ensure_ascii=False), status))
    return status


def is_kept(db, scene_id, tag_id):
    with db.connect() as conn:
        row = conn.execute("SELECT 1 FROM shared_fingerprint_review WHERE scene_id = ? AND tag_id = ? AND status IN ('kept', 'retagged')", (str(scene_id), tag_id)).fetchone()
    return row is not None


def close_unseen(db, seen_ids, provider):
    """After a full scan: open rows of this provider the scan did not find ambiguous any more are settled (tag changed, file replaced ...)."""
    with db.connect() as conn:
        rows = conn.execute("SELECT scene_id FROM shared_fingerprint_review WHERE status = 'open' AND provider = ?", (provider,)).fetchall()
        gone = [r["scene_id"] for r in rows if r["scene_id"] not in seen_ids]
        for sid in gone:
            conn.execute("UPDATE shared_fingerprint_review SET status = 'resolved', resolved_at = datetime('now') WHERE scene_id = ?", (sid,))
    return len(gone)


def set_status(db, scene_id, status, note=None, tag_id=None):
    """`tag_id`: the provider scene the settled decision is about (the NEW tag after a "Use this scene"); the row stays settled for exactly that tag."""
    with db.connect() as conn:
        return conn.execute("UPDATE shared_fingerprint_review SET status = ?, note = ?, tag_id = COALESCE(?, tag_id), resolved_at = datetime('now') WHERE scene_id = ?",
                            (status, note, tag_id, str(scene_id))).rowcount


def get(db, scene_id):
    with db.connect() as conn:
        r = conn.execute("SELECT * FROM shared_fingerprint_review WHERE scene_id = ?", (str(scene_id),)).fetchone()
    return dict(r) if r else None


def rows(db, status="open"):
    with db.connect() as conn:
        out = []
        for r in conn.execute("SELECT * FROM shared_fingerprint_review WHERE status = ? ORDER BY lower(local_title)", (status,)):
            d = dict(r)
            d["candidates"] = json.loads(d["candidates"] or "[]")
            out.append(d)
    return out


def counts(db):
    with db.connect() as conn:
        return {r["status"]: r["n"] for r in conn.execute("SELECT status, COUNT(*) AS n FROM shared_fingerprint_review GROUP BY status")}


def open_count(db):
    return counts(db).get("open", 0)


def make_judge(db, lookup, provider_key):
    """The `judge(entry, found)` that library_check.find_library_mismatches() calls for a file registered at several scenes. `entry` carries id, title,
    paths, stashdb_id (the tag), fps ([{"hash", "algorithm"}]) and seconds. Candidates that only have a SIMILAR picture hash are set aside first
    (split_exact). A verdict that is not ok also lands on the review list."""
    client = getattr(lookup, "client", lookup)  # ThePornDB's one-hash wrapper keeps the real client here

    def judge(entry, found):
        exact, near = split_exact(client, [(f["algorithm"], f["hash"]) for f in entry.get("fps") or []], found, entry["stashdb_id"])
        ids = [f["id"] for f in exact]
        try:
            durations = client.scene_durations(ids) if hasattr(client, "scene_durations") else {}
        except Exception:  # noqa: BLE001
            logger.exception("shared fingerprint: lengths of %s failed", ids)
            durations = {}
        verdict = judge_one(db, entry, exact, durations)
        verdict["near"] = near
        if verdict["ok"]:
            settle(db, entry["id"])
        else:
            upsert(db, entry["id"], entry["title"], (entry.get("paths") or [""])[0], entry.get("seconds"), provider_key, entry["stashdb_id"],
                   verdict["reason"], verdict["how"], enrich(client, exact, durations, near))
        return verdict
    return judge


def judge_one(db, entry, found, durations):
    return judge(entry.get("seconds"), entry["stashdb_id"], found, durations, kept=is_kept(db, entry["id"], entry["stashdb_id"]))


def _norm(text):
    return "".join(ch for ch in (text or "").lower() if ch.isalnum())


def is_harmless(row):
    """A row the user can safely confirm in bulk: the tag's length fits the file, every OTHER scene whose length fits has the same studio as the tag (so
    the studio folder cannot be wrong), the file already sits in that studio's folder, and the scene's own title is the tag's title (nothing was
    changed by hand since). Only 'also_fits' rows qualify -- the others have a length problem."""
    if row.get("reason") != "also_fits":
        return False
    cands = [c for c in row.get("candidates") or [] if not c.get("near")]
    tag = next((c for c in cands if c["id"] == row["tag_id"]), None)
    if not tag or not tag.get("studio"):
        return False
    fitting = [c for c in cands if c["id"] != row["tag_id"] and fits(row.get("file_seconds"), c.get("seconds"))]
    if not fitting or any(_norm(c.get("studio")) != _norm(tag["studio"]) for c in fitting):
        return False
    parts = (row.get("file_path") or "").split("/")
    return len(parts) > 4 and parts[1] == "data" and parts[2] == "scenes" and _norm(parts[3]) == _norm(tag["studio"]) \
        and _norm(row.get("local_title")) == _norm(tag.get("title"))
