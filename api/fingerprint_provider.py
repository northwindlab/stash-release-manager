"""Which metadata provider the fingerprint tools ask (v0.243.0).

The Fingerprint check used to ask StashDB only. A user without a StashDB account (ThePornDB only, or another stash-box) needs the same tools
against the provider they have: `available()` lists the configured providers, `resolve()` gives the client and the Stash endpoint of one.
StashDB and the user-added stash-boxes speak the stash-box GraphQL protocol and look up many files per request; ThePornDB is asked one
hash at a time (`HashLookup`)."""
import time

NAMES = {"stashdb": "StashDB", "theporndb": "ThePornDB"}


class HashLookup:
    """Gives ThePornDB's one-hash REST lookup the shape of a stash-box's `find_scenes_by_fingerprints`: a list per file, each a list of
    {"id", "title"} (the scenes any of the file's hashes is registered against, de-duplicated). Only the EXACT hashes (OSHASH, MD5) are asked:
    ThePornDB's PHASH lookup is similarity-based, which must not decide that a scene's id is wrong or right."""
    batch = False

    def __init__(self, client, delay=0.3):
        self.client = client
        self.delay = delay

    def find_scenes_by_fingerprints(self, fingerprint_sets):
        out = []
        for fps in fingerprint_sets:
            found = {}
            for fp in fps:
                if fp.get("algorithm") not in ("OSHASH", "MD5"):
                    continue
                for m in self.client.find_scenes_by_hash(fp["hash"], fp["algorithm"]):
                    found.setdefault(m["id"], m)
                time.sleep(self.delay)
            out.append(list(found.values()))
        return out


def _configured(client):
    headers = getattr(getattr(client, "session", None), "headers", {})
    return bool((headers.get("ApiKey") or "").strip() or (headers.get("Authorization") or "").replace("Bearer", "").strip())


def available(stash_client, get_provider_client, extra_providers):
    """[{"key", "name", "endpoint", "batch", "submit"}] of every provider that can be asked: StashDB and ThePornDB when their key is set, plus the
    enabled user-added stash-boxes (`extra_providers` = metadata_provider rows). `submit`: fingerprints can be sent back to it (stash-box only)."""
    rows = []
    for key, endpoint in (("stashdb", stash_client.stashdb_endpoint), ("theporndb", stash_client.theporndb_endpoint)):
        client = get_provider_client(key)
        if client is not None and _configured(client):
            rows.append({"key": key, "name": NAMES[key], "endpoint": endpoint, "batch": key == "stashdb", "submit": key == "stashdb"})
    for p in extra_providers:
        rows.append({"key": p["key"], "name": p["name"], "endpoint": p["url"], "batch": True, "submit": True})
    return rows


def resolve(key, stash_client, get_provider_client, extra_providers):
    """(client with find_scenes_by_fingerprints, provider row) for `key` (None = StashDB); ValueError when it is not configured."""
    key = key or "stashdb"
    row = next((r for r in available(stash_client, get_provider_client, extra_providers) if r["key"] == key), None)
    if row is None:
        raise ValueError(f"{NAMES.get(key, key)} is not set up (Settings > Connections)")
    client = get_provider_client(key)
    return (client if row["batch"] else HashLookup(client)), row
