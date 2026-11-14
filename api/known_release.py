"""Known releases (v0.260.1): which release name a file's hashes belong to. Collected where the hashes are known anyway -- a verified import (the release
is the scene's) and a fingerprint mismatch whose file turned out to be another scene (the release is THAT scene's). Since v0.260.2 also how the release
was fetched (indexer, protocol, download / info URL, size). Nothing reads the table yet; it is the base for "do we already know a release of this scene?"
before an indexer is asked. The URLs can carry an indexer's API key: they stay in the database. Best-effort: collecting never fails an import."""
import json
import logging

logger = logging.getLogger(__name__)


def record(db, stash_client, provider, scene_id, path, release_title, source, grab=None):
    """Stores (provider, scene_id, release_title) with the hashes of the Stash file at `path` and, when known, how it was fetched (`grab`: the grabbed
    candidate dict). Returns True when a new row was written."""
    try:
        if not (release_title and scene_id and path):
            return False
        grab = grab if isinstance(grab, dict) else {}
        scene = stash_client.find_scene_by_path(path)
        file = next((f for f in (scene or {}).get("files", []) if f.get("path") == path), None)
        if not file:
            return False
        fps = {fp["type"].lower(): fp["value"] for fp in file.get("fingerprints", []) if fp.get("value")}
        with db.connect() as conn:
            cur = conn.execute(
                """INSERT OR IGNORE INTO known_release (provider, scene_id, release_title, oshash, phash, md5, duration, size, source,
                                                         indexer, protocol, download_url, info_url, release_size, grab_info)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (provider, str(scene_id), release_title, fps.get("oshash"), fps.get("phash"), fps.get("md5"), file.get("duration"),
                 file.get("size"), source, grab.get("indexer"), grab.get("protocol"), grab.get("downloadUrl"), grab.get("infoUrl"),
                 grab.get("size"), json.dumps(grab) if grab else None))
            return cur.rowcount > 0
    except Exception:
        logger.exception("known_release.record failed for %s scene %s", provider, scene_id)
        return False


def for_scene(db, provider, scene_id):
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM known_release WHERE provider = ? AND scene_id = ? ORDER BY id", (provider, str(scene_id)))]
