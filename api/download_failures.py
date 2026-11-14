"""A download SABnzbd reports as 'Failed' (v0.94.2, 2026-10-03).

Until then only 'Completed' jobs were looked at: a failed one (e.g. a par2 repair that could not finish) left its release on
'sent_to_whisparr' with a download job for ever -- Activity showed "Download complete -- waiting for import" and nothing happened
(found with the 2700p upgrade of "A Pussy Work Out", 8.3 GB, failed 2026-10-02). Now:
  * the failed release title goes on the scene's own blocklist (release.review_rejected_titles) as an EXACT entry (v0.260.5: this upload failed, its
    other encodes -- "1080p.MP4" for a failed "XviD" -- may be fine; a plain entry blocked them all), so no later search grabs it again;
  * the release loses its download job and stays on the wishlist ('sent_to_whisparr'), the periodic search tries another release;
  * an UPGRADE grab (release.upgrade_of): the upgrade candidate goes back to 'wanted' (title added to its rejected titles), the
    release rows are put back as they were (the pseudo 'upgrade:' row is removed), the next upgrade search tries another release;
  * a History entry says what failed and why.
"""
import json
import logging
import re

import history
import release_repo
import upgrades

logger = logging.getLogger(__name__)


def handle_failed(db, release, job):
    """`release` = the release row that holds the download job, `job` = SABnzbd's history entry (status 'Failed'). Returns a short summary."""
    provider, pid = release["provider"], release["scene_stashdb_id"]
    title = (job or {}).get("name")
    reason = ((job or {}).get("fail_message") or "no reason given").strip()
    if title:
        release_repo.add_review_rejected_title(db, provider, pid, title, exact=True)
    rows = upgrades._scene_rows(db, provider, pid)
    upgrade_of = next((r.get("upgrade_of") for r in rows if r.get("upgrade_of")), None)
    release_repo.set_download_job_for_scene(db, provider, pid, None, None)
    if not upgrade_of:  # the row would keep saying "grabbed" (Stuck); an upgrade candidate has its own last_search_result below
        release_repo.set_search_result_for_scene(db, provider, pid, f"download failed: {reason}"[:200])
    summary = {"title": title, "reason": reason, "upgrade": bool(upgrade_of)}
    if upgrade_of:
        with db.connect() as conn:
            cand = conn.execute("SELECT * FROM upgrade_candidate WHERE provider_scene_id = ? AND file_path = ?",
                                (pid, upgrade_of)).fetchone()
        if cand is not None:
            cand = dict(cand)
            rejected = upgrades._rejected_titles(cand)
            if title and title not in rejected:
                rejected.append(title)
            # rows that existed before the grab (a watched entity) go back to 'imported'/'new'; only the pseudo row is removed
            previous = [(r["entity_type"], r["entity_stashdb_id"], "imported" if r.get("imported_at") else "new")
                        for r in rows if not str(r["entity_stashdb_id"]).startswith("upgrade:")]
            upgrades._undo_release_rows(db, cand, previous)
            upgrades.set_candidate(db, cand["id"], status="wanted", rejected_titles=json.dumps(rejected),
                                   last_search_result=f"download failed: {reason}"[:200])
            summary["candidate"] = cand["id"]
    history.scene("warning", "download", f"Download failed in {(job or {}).get('client') or 'SABnzbd'}: {title or '?'} -- {reason}"
                  + (" (upgrade: the next upgrade search tries another release)" if upgrade_of
                     else " (back on the wishlist, the next search tries another release)"),
                  provider, pid)
    logger.warning("download failed for scene %s: %s (%s)", pid, title, reason)
    return summary


def handle_lost(db, release):
    """The download client no longer knows the job of a grabbed release (see download_jobs.is_lost). Not the release's fault, so no blocklist:
    the job is dropped and the scene shows on Stuck (with the manual search) instead of waiting for ever."""
    provider, pid = release["provider"], release["scene_stashdb_id"]
    release_repo.set_download_job_for_scene(db, provider, pid, None, None)
    release_repo.set_search_result_for_scene(db, provider, pid, "download lost: the download client no longer knows this job")
    history.scene("warning", "download", "The download client no longer knows this download (removed from its history?) -- back on Stuck, "
                  f"search again or pick a release by hand: {release.get('grabbed_title') or '?'}", provider, pid)


_FAILED_RE = re.compile(r"^Download failed in [^:]+: (.+?) -- ")


def convert_failed_blocklists(db):
    """One-off repair (v0.260.5, idempotent): before this version a failed download put its title on the scene's blocklist as a PLAIN entry, which blocks
    every other encode of the release too (case: a failed "...XviD-iPT.Team" blocked the "...1080p.MP4-GRP" releases of the same scene). A plain
    entry whose History says "Download failed in ...: <title>" and that was never rejected by hand ("Blocklisted" / "Possible match rejected") becomes
    an exact entry. Returns how many entries were converted."""
    converted = 0
    with db.connect() as conn:
        failed = conn.execute("SELECT provider, scene_id, message FROM history WHERE category = 'download' AND message LIKE 'Download failed in %' "
                              "AND scene_id IS NOT NULL").fetchall()
        rejected = {(r["provider"], r["scene_id"], r["message"]) for r in conn.execute(
            "SELECT provider, scene_id, message FROM history WHERE category IN ('blocklist', 'review') AND scene_id IS NOT NULL")}
    seen = set()
    for row in failed:
        m = _FAILED_RE.match(row["message"] or "")
        if not m or (row["provider"], row["scene_id"], m.group(1)) in seen:
            continue
        title = m.group(1).strip()
        seen.add((row["provider"], row["scene_id"], title))
        by_hand = any(t == title or title in t for p, s, t in rejected if p == row["provider"] and s == row["scene_id"])
        if by_hand:
            continue
        current = release_repo.get_review_rejected_titles(db, row["provider"], row["scene_id"])
        if title in current:
            release_repo.remove_review_rejected_title(db, row["provider"], row["scene_id"], title)
            release_repo.add_review_rejected_title(db, row["provider"], row["scene_id"], title, exact=True)
            converted += 1
            history.scene("info", "blocklist", f"The failed download {title} blocks only that exact release now (other encodes may be fine)",
                          row["provider"], row["scene_id"])
    return converted
