"""Candidate sources for the sidecar's own release search (2026-09-25,
"explicit search backend" -- stage 1 of the Indexers-tab work, see the
project roadmap memory).

Replaces the old implicit "Prowlarr is used whenever a Prowlarr URL is
configured" convention with an explicit `search_backend` setting
(routers/settings.py's SEARCH_BACKENDS), since direct indexers add a
third possible source and "whichever has a URL saved" stops being a
meaningful rule once there are three.

indexer_search.search_and_grab() takes one of these as `search_source`:
- `None` -- the 'whisparr' backend: Whisparr's own
  `/release?movieId=X`, handled inline there since it needs the movie
  id rather than a free-text query.
- a source object -- `search(query)` returns candidates already in the
  normalized shape `_select_best()`/`grab_candidate()` consume, and
  `indexer_priorities()` returns `{indexerId: priority}` in that
  source's own indexer-id space (lower = preferred).
- an `UnavailableSource` -- the chosen backend isn't usable (e.g.
  'prowlarr' picked but no Prowlarr URL saved). Deliberately NOT a
  silent fallback to another backend: the user explicitly chose this
  one, so the search records RESULT_SEARCH_BACKEND_UNAVAILABLE instead
  and the reason is visible, rather than quietly searching somewhere
  they didn't pick.
"""
import logging
from concurrent.futures import ThreadPoolExecutor

import category_filter
from indexer_search import normalize_prowlarr_release
from newznab_client import NewznabClient

logger = logging.getLogger(__name__)


_excluded_categories = lambda: set()  # noqa: E731 -- main.py sets it: the categories switched off in Settings (category_filter.py)


def set_excluded_categories(fn):
    global _excluded_categories
    _excluded_categories = fn


class ProwlarrSource:
    name = "prowlarr"
    available = True

    def __init__(self, client):
        self.client = client

    def search(self, query):
        raw = category_filter.drop_excluded(self.client.search(query), _excluded_categories())
        return usable_protocols_only([normalize_prowlarr_release(r) for r in raw])

    def indexer_priorities(self):
        """Best-effort, same as indexer_search._indexer_priorities() -- a
        hiccup here just means every candidate counts as equal priority
        rather than failing the whole search."""
        try:
            return {i["id"]: i.get("priority", 25) for i in self.client.get_indexers()}
        except Exception:
            logger.exception("search_sources: failed to fetch Prowlarr indexer priorities, treating all as equal")
            return {}


# Results whose protocol has no enabled download client could never be grabbed (*arr: "no client for the protocol" rejection) -- dropped here so
# every consumer (automatic search, movie search, manual search) only sees what can be sent. main.py sets the checker.
_protocol_checker = None


def set_protocol_checker(fn):
    global _protocol_checker
    _protocol_checker = fn


def usable_protocols_only(results):
    if _protocol_checker is None:
        return results
    cache = {}
    return [r for r in results if cache.setdefault(r.get("protocol") or "usenet", _protocol_checker(r.get("protocol") or "usenet"))]


class IndexersSource:
    """This sidecar's own direct Newznab indexers (2026-09-25, stage 2):
    every enabled indexer is searched in parallel and the results
    merged, then go through the exact same plausibility/quality pipeline
    as any other source (via normalize_prowlarr_release(), which only
    needs the generic candidate fields, nothing Prowlarr-specific).
    One indexer failing just drops its results (logged); only ALL of
    them failing raises, so the caller records RESULT_SEARCH_FAILED
    rather than a misleading "no results"."""
    name = "indexers"
    available = True

    def __init__(self, rows):
        self.rows = rows

    def _search_one(self, row, query):
        client = NewznabClient(row["url"], row["api_key"], row["api_path"])
        torrent = row.get("type") == "torznab"
        items = category_filter.drop_excluded(client.search(query, row["categories"] or "6000"), _excluded_categories())
        if torrent:  # *arr "Minimum Seeders": fewer are dropped; the indexer's seed goals travel with the result
            items = [i for i in items if (i.get("seeders") or 0) >= (row.get("minimum_seeders") if row.get("minimum_seeders") is not None else 1)]
        return [normalize_prowlarr_release({
            **item, "indexerId": row["id"], "indexer": row["name"], "protocol": "torrent" if torrent else "usenet",
            **({"seedRatio": row.get("seed_ratio"), "seedTime": row.get("seed_time")} if torrent else {}),
        }) for item in items]

    def search(self, query):
        results, errors = [], []
        with ThreadPoolExecutor(max_workers=min(8, len(self.rows)) or 1) as pool:
            futures = [(row, pool.submit(self._search_one, row, query)) for row in self.rows]
            for row, future in futures:
                try:
                    results.extend(future.result())
                except Exception as e:
                    logger.warning("search_sources: indexer %r search failed: %s", row["name"], e)
                    errors.append(f"{row['name']}: {e}")
        if errors and len(errors) == len(self.rows):
            raise RuntimeError("all indexers failed: " + "; ".join(errors))
        return usable_protocols_only(results)

    def indexer_priorities(self):
        return {row["id"]: row["priority"] for row in self.rows}


class FeedSource:
    """RSS mode (2026-09-30): the newest releases of the indexers, fetched ONCE per tick (a blank query,
    like an RSS sync), searched locally afterwards. `search(query)` returns the feed items that contain
    every word of the query, so indexer_search.search_and_grab() runs unchanged -- its title/date/performer
    plausibility and quality rules decide, the feed only replaces the network round trips. Letters and
    digits are compared without separators ("Pegas Productions" matches "PegasProductions")."""
    name = "feed"
    available = True

    def __init__(self, items, priorities):
        import re
        self._re = re
        self.items = items
        self._priorities = priorities
        self._flat = [(it, "".join(re.findall(r"[a-z0-9]+", (it.get("title") or "").lower()))) for it in items]

    def search(self, query):
        words = self._re.findall(r"[a-z0-9]+", (query or "").lower())
        if not words:
            return []
        return [it for it, flat in self._flat if all(w in flat for w in words)]

    def indexer_priorities(self):
        return self._priorities

    def window_days(self):
        """How many days back the feed reaches: the age of its oldest item (at least 1). Unknown ages -> 1."""
        ages = [it.get("age") for it in self.items if isinstance(it.get("age"), (int, float))]
        return max([1] + [int(a) for a in ages])

    @classmethod
    def fetch(cls, source, categories_hint=None):
        """Blank-query pull through the configured backend (Prowlarr or the direct indexers)."""
        items = source.search("")
        return cls(items, source.indexer_priorities())


class UnavailableSource:
    available = False

    def __init__(self, name, reason):
        self.name = name
        self.reason = reason


def older_than_feed(scene_date, window_days, today=None):
    """True when a scene's date lies further back than the RSS feed reaches (+1 day of slack): its release cannot be in the feed any more, only a
    targeted indexer search can find it (v0.213.4). Unknown / unparsable date -> False (rely on the feed and the back-off)."""
    from datetime import date, datetime
    try:
        d = datetime.strptime(str(scene_date)[:10], "%Y-%m-%d").date()
    except ValueError:
        return False
    return ((today or date.today()) - d).days > int(window_days) + 1
