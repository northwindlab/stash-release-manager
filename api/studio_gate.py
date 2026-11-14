"""Needs review: a release of a studio that is not connected to the wanted scene's studio is not offered (v0.260.9).

Case: the wishlist scene "Charli" (studio Exploited Teens) was offered "WankzVR.Whos.Your.Teddy.Charli.Rose.GearVR.1600p". StashDB knows both studios and
they have nothing to do with each other (WankzVR sits under another parent) -- the release cannot be this scene.

`is_unrelated()` reads the studio the release name starts with (the longest run of 1-4 leading words that is exactly the name of a stash-box studio) and
asks whether it is connected to ours: the same studio, the parent / a site of the wanted scene's studio (`own_names`), or a studio with the same parent.
Decided ONLY when the name starts with a known studio that is not connected; never decided (the candidate stays) when
  * the first words are a platform name (setting `studio_platform_names`: Creator, OF, ... -- creators are often filed under their own name),
  * the first words are no studio the stash-box knows (an unknown site, no studio part),
  * the first words are one of the wanted scene's performers,
  * a lookup fails or the per-call budget of lookups is used up.
Used for the Needs-review choice only, not for the automatic choice (which wants title and date to match anyway)."""
import logging
import re
import threading
import time

import other_scene_check as osc

logger = logging.getLogger("studio_gate")

DEFAULT_PLATFORMS = "Creator, OF, Fansly, ManyVids, AnalVids, Anal Vids, Pornbox"
MAX_RUN = 4          # leading words that can make up a studio name
MAX_LOOKUPS = 10     # distinct stash-box requests per call
CACHE_SECONDS = 6 * 3600
_cache = {}          # compact run -> (time, studio {"name", "parent"} or None)
_lock = threading.Lock()


def enabled(db):
    from routers.settings import get_setting_value
    return str(get_setting_value(db, "studio_gate_enabled", "true")).lower() != "false"


def platform_names(db):
    from routers.settings import get_setting_value
    raw = get_setting_value(db, "studio_platform_names", DEFAULT_PLATFORMS)
    return {osc.compact(p) for p in re.split(r"[,;\n]", raw or "") if osc.compact(p)}


def leading_words(release_title):
    text = re.sub(r"^\s*\[[^\]]*\]\s*", "", release_title or "")  # "[site] ..." prefix
    return [w for w in re.split(r"[^A-Za-z0-9]+", text) if w]


def _terms(words):
    """Search terms for a run of words: as written, and with the CamelCase / letter-digit joints opened ("Bride4K" -> "Bride 4K")."""
    plain = " ".join(words)
    opened = re.sub(r"(?<=[a-z])(?=[A-Z0-9])", " ", plain)
    return [plain] if opened == plain else [plain, opened]


def _lookup(client, words, budget):
    """The stash-box studio whose name is exactly this run of words ({"name", "parent"}), None when there is none; False when the budget is used up."""
    key = osc.compact("".join(words))
    with _lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < CACHE_SECONDS:
            return hit[1]
    found = None
    for term in _terms(words):
        if budget[0] <= 0:
            return False
        budget[0] -= 1
        for s in (client.search_studios(term, limit=10).get("results") or []):
            if osc.compact(s.get("name")) == key:
                found = {"name": s.get("name"), "parent": (s.get("parent") or {}).get("name")}
                break
        if found:
            break
    with _lock:
        _cache[key] = (time.time(), found)
    return found


def is_unrelated(db, provider, studio_name, own_names, performer_names, release_title):
    """True when the release name starts with a known stash-box studio that has nothing to do with `studio_name` (see the module docstring)."""
    if provider != "stashdb" or not enabled(db):
        return False
    client = osc._get_client(provider) if osc._get_client else None
    if client is None or not hasattr(client, "search_studios"):
        return False
    words = leading_words(release_title)
    runs = [words[:n] for n in range(MAX_RUN, 0, -1) if len(words) >= n]
    if not runs:
        return False
    own = set(osc._name_variants(studio_name))
    for n in own_names or ():
        own |= osc._name_variants(n)
    platforms = platform_names(db)
    cast = {osc.compact(str(n)) for n in (performer_names or [])}
    compacts = [osc.compact("".join(r)) for r in runs]
    if any(c in platforms or c in own or c in cast for c in compacts):
        return False  # a platform, our own studio / parent / site, or one of our performers leads the name
    budget = [MAX_LOOKUPS]
    for run, c in zip(runs, compacts):  # longest first
        if len(c) < 3:
            continue
        try:
            studio = _lookup(client, run, budget)
        except Exception as e:
            logger.warning("studio_gate: lookup of %r failed: %s", " ".join(run), e)
            return False
        if studio is False:
            return False
        if studio is None:
            continue
        related = osc.compact(studio["name"]) in own or (studio.get("parent") and osc.compact(studio["parent"]) in own)
        return not related
    return False
