"""A release name that belongs to ANOTHER scene on the stash-box (v0.259.0).

Case: the wanted scene "Tight and Fresh Anal Newbie" (studio A, 2013-01-25, performer Anna Morna) was offered the release
"MyDadsHotGirlfriend.13.02.08.Anna.Morna.XXX.1080p" in Needs review: it names the performer and its date is only 14 days away. But StashDB has a
scene of the studio MyDadsHotGirlfriend dated exactly 2013-02-08 with Anna Morna in the cast -- the release is THAT scene, never ours.

`titles_of_other_scenes()` finds such releases: the studio prefix and the date of the name are looked up on the stash-box (studio search, then the
scenes of that studio on that day); the release counts as another scene's when one of those scenes is not ours and one of its performers is named in
the release. The titles go into the search's `taken_titles` (the same list as the names other scenes already hold), so the automatic choice, the
Needs-review choice and the search log (verdict `other_scene`) all leave them out. A lookup that fails never removes a candidate (it stays)."""
import logging
import re
import threading
import time

logger = logging.getLogger("other_scene_check")

_get_client = None
_cache = {}  # (studio spelling, date) -> (time, [scenes of that studio on that day] or None when the studio is unknown)
_lock = threading.Lock()
CACHE_SECONDS = 6 * 3600
MAX_LOOKUPS = 8  # distinct (studio, date) pairs asked per search: a search with dozens of foreign names must not hammer the stash-box
_DATE_RE = re.compile(r"(?:^|[^0-9])((?:19|20)?\d{2})[ ._-](\d{2})[ ._-](\d{2})(?=[^0-9]|$)")


def configure(get_provider_client):
    global _get_client
    _get_client = get_provider_client


def compact(text):
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def split_release(title):
    """(studio prefix as written, "YYYY-MM-DD") of a name like "Studio.Name.13.02.08.Performer...", or None when it has no studio + date part."""
    m = _DATE_RE.search(title or "")
    if not m:
        return None
    year, month, day = m.groups()
    year = int(year) + (2000 if len(year) == 2 else 0)
    if not (1 <= int(month) <= 12 and 1 <= int(day) <= 31):
        return None
    prefix = title[:m.start(1)].strip(" ._-[]")
    return (prefix, f"{year:04d}-{month}-{day}") if len(compact(prefix)) >= 4 else None


def _search_term(prefix):
    """"MyDadsHotGirlfriend" / "My.Dads.Hot.Girlfriend" -> "My Dads Hot Girlfriend" (the stash-box's studio search wants words)."""
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", prefix)
    return re.sub(r"[^A-Za-z0-9]+", " ", spaced).strip()


def _name_variants(studio_name):
    base = re.split(r"\s*(?::|\s-\s)\s*", studio_name or "", maxsplit=1)[0]
    return {v for v in (compact(studio_name), compact(base)) if v}


def _scenes_of(client, prefix, date):
    """The scenes of the studio spelled `prefix` that carry `date` -- None when no studio of that name exists. Cached."""
    key = (compact(prefix), date)
    with _lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < CACHE_SECONDS:
            return hit[1]
    studios = client.search_studios(_search_term(prefix), limit=10).get("results") or []
    studio = next((s for s in studios if compact(prefix) in _name_variants(s.get("name"))), None)
    scenes = client.find_scenes_on_date(studio["id"], date) if studio else None
    with _lock:
        _cache[key] = (time.time(), scenes)
    return scenes


def titles_of_other_scenes(provider, scene_id, scene_date, performer_names, studio_name, releases):
    """The release names among `releases` that belong to another scene of the stash-box (see the module docstring)."""
    client = _get_client(provider) if _get_client and provider == "stashdb" else None
    if client is None or not hasattr(client, "find_scenes_on_date"):
        return []
    names = [compact(str(n)) for n in (performer_names or []) if len(compact(str(n))) >= 5]
    own_studio = _name_variants(studio_name)
    out, asked = [], set()
    for r in releases or []:
        title = r.get("title") or ""
        split = split_release(title)
        if not split or not names or not any(n in compact(title) for n in names):
            continue  # a release that does not even name one of our performers is no candidate anyway
        prefix, date = split
        if date == scene_date and compact(prefix) in own_studio:
            continue  # our own studio and day: that is what the normal rules decide
        if (compact(prefix), date) not in asked and len(asked) >= MAX_LOOKUPS:
            continue
        asked.add((compact(prefix), date))
        try:
            scenes = _scenes_of(client, prefix, date)
        except Exception as e:
            logger.warning("other_scene_check: lookup of %r on %s failed: %s", prefix, date, e)
            continue
        for s in scenes or []:
            if s.get("id") == scene_id:
                continue
            cast = [compact((p.get("performer") or p).get("name")) for p in (s.get("performers") or [])]
            if any(c and c in compact(title) for c in cast):
                out.append(title)
                break
    return out


def sweep_review_candidates(db):
    """One pass over the stored "Needs review" candidates of the wishlist (at start-up): a candidate that belongs to another stash-box scene is
    removed (the same check the search applies now), the scene's search result becomes 'title_mismatch_only'. Returns how many were removed."""
    import history
    import indexer_search
    import release_repo
    removed = 0
    with db.connect() as conn:
        rows = [dict(r) for r in conn.execute(
            """SELECT provider, scene_stashdb_id, title, scene_date, studio_name, cast_names, review_candidate FROM release
               WHERE status = 'sent_to_whisparr' AND download_job_id IS NULL AND last_search_result = 'needs_review'
               AND review_candidate IS NOT NULL AND provider = 'stashdb'""")]
    import json
    for r in rows:
        try:
            title = (json.loads(r["review_candidate"]) or {}).get("title")
        except (TypeError, ValueError):
            continue
        names = [n for n in (r["cast_names"] or "").split("|") if n]
        if not title or not names:
            continue
        try:
            hit = titles_of_other_scenes(r["provider"], r["scene_stashdb_id"], r["scene_date"], names, r["studio_name"], [{"title": title}])
        except Exception as e:
            logger.warning("other_scene_check: sweep of %s failed: %s", r["scene_stashdb_id"], e)
            continue
        if hit:
            release_repo.set_review_candidate_for_scene(db, r["provider"], r["scene_stashdb_id"], None)
            release_repo.set_search_result_for_scene(db, r["provider"], r["scene_stashdb_id"], indexer_search.RESULT_TITLE_MISMATCH_ONLY)
            history.scene("info", "review", f"Possible match removed: {title} belongs to another scene on the stash-box (studio and date of the name)",
                          r["provider"], r["scene_stashdb_id"])
            removed += 1
    return removed
