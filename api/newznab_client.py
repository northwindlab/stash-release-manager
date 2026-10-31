"""Minimal Newznab API client (2026-09-25, Indexers tab stage 2) -- the
common usenet-indexer API (NZBGeek, NZBFinder, abNZB, ...), same one
Prowlarr/Whisparr speak to under the hood. Only the two calls this
project needs: `t=caps` as a cheap authenticated connection check, and
`t=search` for a free-text query.

Responses are RSS/XML. Errors come back as HTTP 200 with an
`<error code=".." description=".."/>` root element (e.g. code 100 =
bad API key), not as an HTTP status -- checked explicitly below so a
wrong key surfaces as an error instead of a silent "no results"."""
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import xml.etree.ElementTree as ET

import requests

_NEWZNAB_NS = "{http://www.newznab.com/DTD/2010/feeds/attributes/}"
_TORZNAB_NS = "{http://torznab.com/schemas/2015/feed}"  # Torznab (Jackett/Prowlarr/torrent indexers) has its own attribute namespace

# Sent on every request (2026-09-25, found live): NZBFinder sits behind
# Cloudflare, which answers requests' default "python-requests/x.y"
# User-Agent with HTTP 403 (an HTML challenge page) even with a valid
# key; any real client name passes. Identifies this project honestly
# rather than impersonating a browser or Prowlarr.
USER_AGENT = "StashReleaseManager/1.0"


class NewznabError(Exception):
    pass


class NewznabClient:
    def __init__(self, url, api_key, api_path="/api", timeout=20):
        self.endpoint = f"{url.rstrip('/')}/{(api_path or '/api').strip('/')}"
        self.api_key = api_key
        self.timeout = timeout

    def _get(self, params):
        # Errors are re-raised WITHOUT the request URL: Newznab puts the
        # API key in the query string, and requests' own exception text
        # includes the full URL -- which would leak the key into logs,
        # the Settings "Check" status and search-failure messages
        # (caught live 2026-09-25 during this client's first real test).
        try:
            resp = requests.get(self.endpoint, params={**params, "apikey": self.api_key.strip()},
                                headers={"User-Agent": USER_AGENT}, timeout=self.timeout)
        except requests.RequestException as e:
            raise NewznabError(f"{type(e).__name__} contacting {self.endpoint}") from None
        if not resp.ok:
            hint = {401: " (wrong API key?)", 403: " (access denied -- wrong API key, or blocked by the site's protection)",
                    429: " (rate limit / API limit reached)"}.get(resp.status_code, "")
            raise NewznabError(f"HTTP {resp.status_code}{hint}")
        try:
            root = ET.fromstring(resp.content)
        except ET.ParseError as e:
            raise NewznabError(f"not a Newznab XML response: {e}")
        if root.tag == "error":
            raise NewznabError(f"code {root.get('code')}: {root.get('description')}")
        return root

    def caps(self):
        return self._get({"t": "caps"})

    def search(self, query, categories="6000", limit=100):
        """Items as plain dicts: title, size (bytes or None), age (whole
        days or None), downloadUrl (the item's NZB link, already
        carrying the apikey the indexer embeds)."""
        root = self._get({"t": "search", "q": query, "cat": categories, "limit": limit, "extended": 1})
        now = datetime.now(timezone.utc)
        items = []
        for item in root.iter("item"):
            attrs = {a.get("name"): a.get("value") for ns in (_NEWZNAB_NS, _TORZNAB_NS) for a in item.iter(f"{ns}attr")}
            # every category the item is filed under (the attribute can occur several times): the same shape as Prowlarr's results
            cats = [{"id": int(a.get("value"))} for ns in (_NEWZNAB_NS, _TORZNAB_NS) for a in item.iter(f"{ns}attr")
                    if a.get("name") == "category" and str(a.get("value") or "").isdigit()]
            size = attrs.get("size")
            if size is None:
                enclosure = item.find("enclosure")
                size = enclosure.get("length") if enclosure is not None else None
            age = None
            pub = item.findtext("pubDate")
            if pub:
                try:
                    age = max(0, (now - parsedate_to_datetime(pub)).days)
                except (TypeError, ValueError):
                    pass
            enclosure = item.find("enclosure")
            link = item.findtext("link") or (enclosure.get("url") if enclosure is not None else None)
            def _int(v):
                return int(v) if v is not None and str(v).isdigit() else None
            items.append({
                # Torznab extras (torrent indexers): seeders / peers / info hash / magnet link; None for Newznab items.
                "seeders": _int(attrs.get("seeders")), "peers": _int(attrs.get("peers")),
                "infoHash": (attrs.get("infohash") or "").lower() or None, "magnetUrl": attrs.get("magneturl"),
                "title": item.findtext("title") or "",
                "categories": cats,
                "size": int(size) if size and str(size).isdigit() else None,
                "age": age,
                "downloadUrl": link,
                # Details page on the indexer's site (2026-09-26): Newznab's
                # <comments> element, else a permalink <guid>. Filtered for
                # API keys later (indexer_search.safe_info_url()).
                "infoUrl": item.findtext("comments") or (
                    item.findtext("guid") if (item.find("guid") is not None
                                              and item.find("guid").get("isPermaLink") == "true") else None),
            })
        return items
