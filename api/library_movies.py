"""Maintenance lists for movies / groups (2026-09-29, user decisions in the roadmap memory).

Stash model: a film is a Group; a single-file film = one scene + a Group of the same title;
a film in parts (CD1/CD2, real scenes) = one Group with one scene per part (scene_index).
Only the movie folders (/data/Movies, /data/movies) are looked at -- normal scenes have no group
by design and never show up here.

  videos_without_group()   film folders whose videos belong to no Group, with a suggested title
                           from the folder name "Title (Year)"; nothing is created without a click
  performers_without_scenes()  performers with 0 scenes (the Watchlist tag marks intended ones)
  groups_without_files()   groups with no scene at all, or only scenes without a file
"""
import film_names
import base64
import html
import logging
import os
import re
import time
from collections import defaultdict
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

MOVIE_ROOTS = ("/data/Movies/", "/data/movies/")
SHORT_MINUTES = 20
_FOLDER_RE = re.compile(r"^(?P<title>.*?)\s*\((?P<year>\d{4})?\)\s*$")
_PART_RE = re.compile(r"(?i)(cd|disc|disk|part|pt)[\s._\-]?(\d+|[a-d])\b")

_NO_GROUP_QUERY = """
query { findScenes(filter: {per_page: -1}, scene_filter: {groups: {modifier: IS_NULL}}) {
  scenes { id title date files { id path duration width height } } } }
"""
_PERFORMERS_QUERY = """
query { findPerformers(filter: {per_page: -1}, performer_filter: {scene_count: {value: 0, modifier: EQUALS}}) {
  performers { id name disambiguation favorite image_path scene_count tags { id name } } } }
"""
_ALL_GROUPS_QUERY = """
query { findGroups(filter: {per_page: -1}) { groups { id name date scene_count } } }
"""
_GROUPS_QUERY = """
query { findGroups(filter: {per_page: -1}) { groups { id name date scene_count scenes { id files { id } } } } }
"""


def _local_path(url):
    """Stash hands out picture links with the address the sidecar reaches it by (an internal host). The browser must load them from the address
    it opened Stash with, so only the path and query are kept: /performer/12/image?t=...."""
    return urlsplit(url)._replace(scheme="", netloc="").geturl() if isinstance(url, str) and url.startswith(("http://", "https://")) else url


class Movies:
    def __init__(self, stash_client, setting_getter):
        self.stash = stash_client
        self.setting = setting_getter

    def movie_roots(self):
        """The film folders: /Movies/ and /movies/ plus the first folder of the movie folder template (v0.194.0), e.g. /data/Films/."""
        return film_names.movie_roots(self.setting("whisparr_root_folder_path"), self.setting("movie_folder_format"))

    # ---------- videos without a group (movie folders only) ----------

    @staticmethod
    def _film_key(path, root):
        """(folder path, is_film_folder): a folder named 'Title (Year)' is one film; a genre folder
        (e.g. /data/movies/Anal/) is not -- each video in it is its own candidate."""
        rel_dir = os.path.dirname(path)[len(root):].strip("/")
        leaf = rel_dir.split("/")[-1] if rel_dir else ""
        if leaf and _FOLDER_RE.match(leaf):
            return os.path.dirname(path), True
        return path, False

    def videos_without_group(self):
        scenes = self.stash._graphql(_NO_GROUP_QUERY, {})["findScenes"]["scenes"]
        by_title = defaultdict(list)  # normalized title -> existing groups (an empty one is a ready-made target)
        for g in self.stash._graphql(_ALL_GROUPS_QUERY, {})["findGroups"]["groups"]:
            by_title[self._norm(g["name"])].append(g)
        folders = defaultdict(list)
        meta = {}
        roots = self.movie_roots()
        for s in scenes:
            for f in s["files"]:
                root = next((r for r in roots if f["path"].startswith(r)), None)
                if not root:
                    continue
                key, is_film_dir = self._film_key(f["path"], root)
                folders[key].append({"scene_id": s["id"], "title": s["title"], "file": os.path.basename(f["path"]),
                                     "minutes": round((f["duration"] or 0) / 60, 1)})
                meta[key] = (root, is_film_dir)
        rows = []
        for key, vids in folders.items():
            root, is_film_dir = meta[key]
            leaf = os.path.basename(key) if is_film_dir else os.path.splitext(os.path.basename(key))[0]
            m = _FOLDER_RE.match(leaf)
            title = (m.group("title") if m else leaf).strip()
            year = (m.group("year") if m else None) or None
            vids.sort(key=lambda v: v["file"].lower())
            total = round(sum(v["minutes"] for v in vids), 1)
            flags = []
            if total < SHORT_MINUTES:
                flags.append("under 20 min -- probably not a film")
            if len(vids) > 1:
                flags.append("cd/part split" if sum(1 for v in vids if _PART_RE.search(v["file"])) >= 2
                             else f"{len(vids)} videos (scenes of one film?)")
            cands = by_title.get(self._norm(title), [])
            if year:  # prefer a group of the same year when there are several
                cands = sorted(cands, key=lambda g: (g.get("date") or "")[:4] != year)
            match = ({"id": cands[0]["id"], "name": cands[0]["name"], "date": cands[0].get("date"),
                      "scenes": cands[0]["scene_count"]} if cands else None)
            rows.append({"folder": key[len(root):] if key.startswith(root) else key, "root": root.rstrip("/"),
                         "title": title, "year": year, "minutes": total, "videos": vids, "flags": flags,
                         "existing_group": match})
        rows.sort(key=lambda r: (r["root"], r["folder"].lower()))
        return rows

    @staticmethod
    def _norm(text):
        return re.sub(r"[^a-z0-9]", "", (text or "").lower())

    def create_group(self, title, scene_ids, group_id=None, scrape_url=None):
        """Puts the scenes in a Group in the given order (scene_index counting on from the group's
        current scenes). Without `group_id` a new Group named `title` is created; with one the scenes
        join that existing group (e.g. an empty one made from the provider's metadata). The scenes'
        other groups are kept. Refuses an empty title (new group) / no scenes. With `scrape_url` (v0.212.0) a NEW group is
        created with the data of that scraped page (what Stash's own group scraper reads: name, date, length, director,
        synopsis, studio and tags that exist locally, covers)."""
        title = (title or "").strip()
        scraped = None
        if scrape_url and not group_id:
            scraped = self.scrape_group_input(scrape_url)
            title = title or scraped["name"]
        # One scene can hold several files of the same film folder: every scene goes in once, in the given order.
        scene_ids = list(dict.fromkeys(str(i) for i in scene_ids or []))
        if not scene_ids or (not group_id and not title):
            raise ValueError("a title and at least one scene are required")
        start = 1
        if group_id:
            g = self.stash._graphql("query($id: ID!) { findGroup(id: $id) { id name scene_count } }",
                                    {"id": str(group_id)})["findGroup"]
            if not g:
                raise ValueError("group not found")
            title, start = g["name"], (g["scene_count"] or 0) + 1
        else:
            group_id = self.stash._graphql("mutation($i: GroupCreateInput!) { groupCreate(input: $i) { id } }",
                                           {"i": scraped or {"name": title}})["groupCreate"]["id"]
            if scraped:
                title = scraped["name"]
        for index, sid in enumerate(scene_ids, start):
            cur = self.stash._graphql("query($id: ID!) { findScene(id: $id) { groups { group { id } scene_index } } }",
                                      {"id": sid})["findScene"]["groups"]
            groups = [{"group_id": g["group"]["id"], "scene_index": g["scene_index"]} for g in cur]
            groups.append({"group_id": group_id, "scene_index": index})
            self.stash._graphql("mutation($i: SceneUpdateInput!) { sceneUpdate(input: $i) { id } }",
                                {"i": {"id": sid, "groups": groups}})
        return {"group_id": group_id, "title": title, "scenes": len(scene_ids)}

    # ---------- Groups from a scraped page (v0.212.0) ----------
    # Like the Edit Group page of Stash: the page is read by whichever of Stash's installed GROUP scrapers handles its address
    # (`scrapeGroupURL`; studio / tags come back already matched to local ones). Stash's group scrapers read a PAGE only, none
    # searches by title, so a title search needs a SOURCE of our own: SEARCH_SOURCES below (key -> label, search function), one entry per
    # shop that can be searched; everything after the search is source-independent. A page address can also be pasted directly.
    _ADE_CARD = re.compile(r'<div class="product-card" id="card(\d+)">(.*?)<div class="item-info', re.S)
    _search_cache = {}

    @classmethod
    def _search_adultempire(cls, query):
        """[{id, title, url, image, type}] from AdultDVDEmpire's search page (movies only, page order)."""
        import requests
        r = requests.get("https://www.adultdvdempire.com/allsearch/search", params={"q": query}, timeout=30,
                         headers={"User-Agent": "Mozilla/5.0"}, cookies={"ageConfirmed": "true"})
        r.raise_for_status()
        out, seen = [], set()
        for cid, block in cls._ADE_CARD.findall(r.text):
            link = re.search(r'product-details__item-title">\s*<a href="\s*(/\d+/[^"]+)"[^>]*?title="([^"]*)"', block, re.S)
            if not link or cid in seen:
                continue
            seen.add(cid)
            kind = re.search(r'product-details__product-type">\s*([^<]*?)\s*<', block)
            img = re.search(r'<img[^>]*?src="(https://[^"]+/movie/[^"]+)"', block, re.S)
            out.append({"id": cid, "title": html.unescape(link.group(2)).strip(), "url": "https://www.adultdvdempire.com" + link.group(1),
                        "image": html.unescape(img.group(1)) if img else None, "type": (kind.group(1) if kind else "").strip()})
        return out

    SEARCH_SOURCES = {"adultempire": ("AdultDVDEmpire (title search)", "_search_adultempire")}

    def group_sources(self):
        """What the picker offers: the title-search sources, and the installed Stash group scrapers (which page addresses can be pasted)."""
        return {"sources": [{"key": k, "label": v[0]} for k, v in self.SEARCH_SOURCES.items()],
                "scrapers": [{"id": sc["id"], "name": sc["name"], "urls": (sc.get("group") or {}).get("urls") or []}
                             for sc in self._group_scrapers()]}

    def group_search(self, source, query):
        query = (query or "").strip()
        if source not in self.SEARCH_SOURCES:
            raise ValueError("unknown search source")
        if not query:
            raise ValueError("a search term is required")
        key = (source, query.lower())
        hit = self._search_cache.get(key)
        if hit and time.time() - hit[0] < 600:
            return hit[1]
        out = getattr(self, self.SEARCH_SOURCES[source][1])(query)
        self._search_cache[key] = (time.time(), out)
        return out

    def _group_scrapers(self):
        hit = getattr(self, "_scrapers_cache", None)
        if hit and time.time() - hit[0] < 300:
            return hit[1]
        rows = self.stash._graphql("{ listScrapers(types: [GROUP]) { id name group { urls supported_scrapes } } }", {})["listScrapers"]
        rows = [r for r in rows if "URL" in ((r.get("group") or {}).get("supported_scrapes") or [])]
        self._scrapers_cache = (time.time(), rows)
        return rows

    def scraper_for_url(self, url):
        """Name of the installed group scraper that handles this page address, else None (Stash matches by the scraper's url fragments)."""
        from urllib.parse import urlparse
        if urlparse(url or "").scheme not in ("http", "https"):
            return None
        low = url.lower()
        for sc in self._group_scrapers():
            if any(frag.lower() in low for frag in (sc.get("group") or {}).get("urls") or []):
                return sc["name"]
        return None

    @staticmethod
    def _seconds(text):
        """'2:49:00' / '49:00' -> seconds, None when it is not a length."""
        parts = [p for p in re.split(r":", str(text or "").strip()) if p != ""]
        if not parts or not all(p.isdigit() for p in parts) or len(parts) > 3:
            return None
        total = 0
        for p in parts:
            total = total * 60 + int(p)
        return total or None

    def scrape_group_input(self, url):
        """GroupCreateInput from the page: Stash's group scraper does the reading (studio / tags only when they exist locally)."""
        scraper = self.scraper_for_url(url)
        if not scraper:
            raise ValueError("No installed Stash group scraper handles that address (Stash > Settings > Metadata Providers > Scrapers)")
        # A scraper that fetches a page can be refused by the site now and then (seen with themoviedb.org: "http error 401" on every
        # second or third try of the SAME address): tried up to three times before it counts as failed.
        data = None
        for attempt in range(3):
            try:
                data = self.stash._graphql(
                    "query($u: String!) { scrapeGroupURL(url: $u) { name aliases duration date director urls synopsis studio { stored_id name } "
                    "tags { stored_id name } front_image back_image } }", {"u": url})["scrapeGroupURL"]
                break
            except RuntimeError as e:
                if "http error" not in str(e).lower() or attempt == 2:
                    raise ValueError(f"The scraper {scraper} failed: {str(e).replace('Stash GraphQL error: ', '')[:200]} (the site refused the request; try again in a moment)")
                time.sleep(1.5)
        if not data or not (data.get("name") or "").strip():
            raise ValueError(f"The scraper {scraper} found nothing on that page")
        inp = {"name": data["name"].strip(), "urls": data.get("urls") or [url]}
        for key in ("aliases", "date", "director", "synopsis", "front_image", "back_image"):
            if data.get(key):
                inp[key] = data[key]
        seconds = self._seconds(data.get("duration"))
        if seconds:
            inp["duration"] = seconds
        studio = data.get("studio") or {}
        if studio.get("stored_id"):
            inp["studio_id"] = studio["stored_id"]
        tags = data.get("tags") or []
        known = [t["stored_id"] for t in tags if t.get("stored_id")]
        if known:
            inp["tag_ids"] = known
        return inp

    # ---------- Groups from ThePornDB movies (v0.94.0) ----------

    def local_studio_index(self):
        """{normalised name or alias: [local studio ids]} -- a ThePornDB movie's studio is only linked when a local studio of that name
        (or alias) EXISTS and is unambiguous; no studio is created."""
        rows = self.stash._graphql("{ findStudios(filter: {per_page: -1}) { studios { id name aliases } } }", {})["findStudios"]["studios"]
        index = defaultdict(list)
        for st in rows:
            for name in [st["name"]] + list(st.get("aliases") or []):
                key = self._norm(name)
                if key and st["id"] not in index[key]:
                    index[key].append(st["id"])
        return index

    @staticmethod
    def _data_url(url):
        """Image as a data: URL (what Stash's groupCreate wants for front_image/back_image); None when it can't be fetched."""
        import requests
        try:
            r = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code != 200 or not r.content:
                return None
            kind = r.headers.get("content-type", "image/jpeg").split(";")[0]
            return f"data:{kind};base64,{base64.b64encode(r.content).decode()}"
        except Exception:
            logger.exception("movies: image download failed: %s", url)
            return None

    def tpdb_group_input(self, movie, studio_index, images=True):
        """GroupCreateInput fields from a ThePornDB /movies/{id} record -- GROUP data only (name, date, length, director, synopsis, urls,
        studio when a local one exists, front/back cover). Returns (input, notes)."""
        notes = []
        directors = []
        for d in movie.get("directors") or []:
            name = d.get("name") if isinstance(d, dict) else d
            if name:
                directors.append(str(name))
        urls = [u for u in (movie.get("url"), f"https://theporndb.net/movies/{movie['slug']}" if movie.get("slug") else None) if u]
        name = movie["title"].strip()
        art = re.match(r"^(.*?),\s*(the|a|an)$", name, re.I)  # ThePornDB lists "Driver, the": the Group is called "The Driver"
        if art:
            name = f"{art.group(2).capitalize()} {art.group(1)}"
        inp = {"name": name, "urls": urls}
        if movie.get("date"):
            inp["date"] = movie["date"]
        if movie.get("duration"):
            inp["duration"] = int(movie["duration"])
        if directors:
            inp["director"] = ", ".join(directors)
        if movie.get("description"):
            inp["synopsis"] = movie["description"]
        site = (movie.get("site") or {}).get("name")
        ids = studio_index.get(self._norm(site), []) if site else []
        if len(ids) == 1:
            inp["studio_id"] = ids[0]
        else:
            notes.append(f"studio {site!r}: " + ("no local studio of that name" if not ids else f"ambiguous ({len(ids)} local studios)"))
        if not images:
            return inp, notes
        poster = movie.get("poster")
        front = self._data_url(poster) if isinstance(poster, str) and poster.startswith("http") else None
        if front:
            inp["front_image"] = front
        else:
            notes.append("no front cover")
        back_url = movie.get("back_image")
        if isinstance(back_url, str) and back_url.startswith("http"):
            back = self._data_url(back_url)
            if back:
                inp["back_image"] = back
        return inp, notes

    def create_from_tpdb(self, movie, scene_ids, studio_index):
        """Creates the Group from the ThePornDB movie and puts the scene(s) in it (scene data stays untouched). Returns
        {"group_id", "name", "notes", "studio"}."""
        inp, notes = self.tpdb_group_input(movie, studio_index)
        group_id = self.stash._graphql("mutation($i: GroupCreateInput!) { groupCreate(input: $i) { id } }", {"i": inp})["groupCreate"]["id"]
        self.create_group(inp["name"], scene_ids, group_id=group_id)
        return {"group_id": group_id, "name": inp["name"], "notes": notes, "studio": bool(inp.get("studio_id"))}

    # ---------- complete an existing Group with its ThePornDB movie (v0.227.0) ----------

    @staticmethod
    def tpdb_search(client, query, minutes=None):
        """ThePornDB movie search (one request): [{id, title, year, minutes, site, performers, length_ok, url}]. `minutes` = the length
        of the film's file(s); a hit within 10 % of it is marked length_ok."""
        resp = client._get("/movies", params={"q": query, "per_page": 10})
        resp.raise_for_status()
        out = []
        for d in resp.json().get("data") or []:
            dm = (d.get("duration") or 0) / 60
            out.append({"id": d["id"], "title": d.get("title"), "year": int((d.get("date") or "0")[:4] or 0), "minutes": round(dm, 1),
                        "site": (d.get("site") or {}).get("name"), "performers": len(d.get("performers") or []),
                        "length_ok": bool(dm and minutes and abs(dm / minutes - 1) <= 0.10),
                        "url": f"https://theporndb.net/movies/{d['slug']}" if d.get("slug") else d.get("url"), "source_url": d.get("url")})
        return out

    def link_group_to_tpdb(self, movie, group_id):
        """Adds the ThePornDB movie page to the Group's links (the import takes a Group with that link for a confirmed film) and
        fills what the Group lacks: date, length, director, synopsis, studio (only a local one). Nothing that is set is changed, the name
        stays, no cover is touched. Returns {"group_id", "name", "link", "filled", "notes"}."""
        g = self.stash._graphql("query($id: ID!) { findGroup(id: $id) { id name date duration director synopsis urls studio { id } } }",
                                {"id": str(group_id)})["findGroup"]
        if not g:
            raise ValueError("group not found")
        inp, notes = self.tpdb_group_input(movie, self.local_studio_index(), images=False)
        link = next((u for u in inp["urls"] if "theporndb.net/movies/" in u), None)
        if not link:
            raise ValueError("ThePornDB gave no page for this movie")
        upd = {"id": str(group_id)}
        have = {u.rstrip("/").lower() for u in g.get("urls") or []}
        upd["urls"] = list(g.get("urls") or []) + ([] if link.rstrip("/").lower() in have else [link])
        filled = []
        for field in ("date", "duration", "director", "synopsis"):
            if inp.get(field) and not g.get(field):
                upd[field] = inp[field]
                filled.append(field)
        if inp.get("studio_id") and not g.get("studio"):
            upd["studio_id"] = inp["studio_id"]
            filled.append("studio")
        self.stash._graphql("mutation($i: GroupUpdateInput!) { groupUpdate(input: $i) { id } }", {"i": upd})
        return {"group_id": str(group_id), "name": g["name"], "link": link, "filled": filled, "notes": notes}

    # ---------- performers without scenes ----------

    def performers_without_scenes(self):
        """Performers with 0 scenes. `on_watchlist` follows the watchlist source (v0.238.3): the Favorite flag in 'favorites' mode, the watchlist tag in 'tag'
        mode (there Favorite is just a favorite again)."""
        wl_name = (self.setting("watchlist_tag_name") or "Watchlist") if self.setting else "Watchlist"
        tag_mode = bool(self.setting) and self.setting("watchlist_source") == "tag"
        rows = []
        for p in self.stash._graphql(_PERFORMERS_QUERY, {})["findPerformers"]["performers"]:
            tags = [t["name"] for t in p["tags"]]
            rows.append({"id": p["id"], "name": p["name"], "disambiguation": p.get("disambiguation") or "",
                         "favorite": p["favorite"], "image_path": _local_path(p.get("image_path")),
                         "on_watchlist": (wl_name in tags) if tag_mode else bool(p["favorite"])})
        rows.sort(key=lambda r: r["name"].lower())
        return rows

    def delete_empty_performer(self, performer_id):
        """Deletes a performer from Stash, but only while it still has no scene (the list may be old). Returns {"ok", "name"} or raises ValueError."""
        data = self.stash._graphql("query($id: ID!) { findPerformer(id: $id) { id name scene_count } }", {"id": str(performer_id)})["findPerformer"]
        if not data:
            raise ValueError("Stash has no performer with this id any more")
        if data["scene_count"]:
            raise ValueError(f"{data['name']} has {data['scene_count']} scene(s) now -- not deleted")
        self.stash._graphql("mutation($i: PerformerDestroyInput!) { performerDestroy(input: $i) }", {"i": {"id": str(performer_id)}})
        return {"ok": True, "name": data["name"]}

    # ---------- empty groups that have a namesake WITH files (v0.241.9) ----------

    def group_namesakes(self):
        """Empty groups (no scene with a file) next to groups that have files and a name that is the same film by group_compare's rules
        (accents, "Vol.", ", The", brackets, the numbers must agree). [{group: {id, name, date, scenes}, namesakes: [{id, name, date, scenes, score, exact}]}].
        An empty group with such a namesake is usually a duplicate made by a scrape or an import."""
        import group_compare
        empties, filled = [], []
        for g in self.stash._graphql(_GROUPS_QUERY, {})["findGroups"]["groups"]:
            files = sum(1 for s in g["scenes"] if s["files"])
            row = {"id": g["id"], "name": g["name"], "date": g.get("date"), "scenes": len(g["scenes"]), "with_files": files}
            (filled if files else empties).append(row)
        out = []
        for e in empties:
            found = []
            for f in filled:
                hit = group_compare.compare(e["name"], f["name"])
                if hit:
                    exact = group_compare.normalise(e["name"]) == group_compare.normalise(f["name"])
                    found.append({**f, "score": hit[0], "note": hit[1], "exact": exact})
            if found:
                found.sort(key=lambda x: (-x["exact"], -x["score"], x["name"].lower()))
                out.append({"group": e, "namesakes": found[:5]})
        out.sort(key=lambda r: r["group"]["name"].lower())
        return out

    def delete_empty_group(self, group_id):
        """Deletes a group that still has no scene with a file (re-checked here); its scenes, if any, stay in Stash. Raises ValueError otherwise."""
        g = self.stash._graphql("query($id: ID!) { findGroup(id: $id) { id name scenes { id files { id } } } }", {"id": str(group_id)})["findGroup"]
        if not g:
            raise ValueError("Stash has no group with this id any more")
        if any(s["files"] for s in g["scenes"]):
            raise ValueError(f"\"{g['name']}\" has scenes with files now -- not deleted")
        self.stash._graphql("mutation($i: GroupDestroyInput!) { groupDestroy(input: $i) }", {"i": {"id": str(group_id)}})
        return {"ok": True, "name": g["name"]}

    # ---------- groups without files ----------

    def groups_without_files(self):
        rows = []
        for g in self.stash._graphql(_GROUPS_QUERY, {})["findGroups"]["groups"]:
            with_files = [s for s in g["scenes"] if s["files"]]
            if with_files:
                continue
            rows.append({"id": g["id"], "name": g["name"], "date": g.get("date"),
                         "scenes": len(g["scenes"]),
                         "reason": "no scene at all" if not g["scenes"] else "its scenes have no file"})
        rows.sort(key=lambda r: r["name"].lower())
        return rows
