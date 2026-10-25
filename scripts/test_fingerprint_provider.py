"""Fingerprint check against the configured provider (v0.243.0): provider list, one-hash-at-a-time adapter, run provider kept per finding."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))
import fingerprint_provider as fp
import library_check
import library_check_repo
from db import Database

fails = []
def check(name, cond):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond: fails.append(name)


class Session:
    def __init__(self, headers): self.headers = headers

class Box:  # stash-box shaped client
    def __init__(self, key): self.session = Session({"ApiKey": key})

class Rest:  # ThePornDB shaped client
    def __init__(self, key):
        self.session = Session({"Authorization": f"Bearer {key}"})
        self.calls = []
    def find_scenes_by_hash(self, h, t):
        self.calls.append((h, t))
        return {"a1": [{"id": "S1", "title": "One"}], "a2": [{"id": "S1", "title": "One"}, {"id": "S2", "title": "Two"}]}.get(h, [])

class Stash:
    stashdb_endpoint = "https://stashdb.org/graphql"
    theporndb_endpoint = "https://theporndb.net/graphql"

def clients(stashdb_key, tpdb_key, rest):
    return lambda key: {"stashdb": Box(stashdb_key), "theporndb": rest(tpdb_key), "fansdb": Box("k")}.get(key)

extra = [{"key": "fansdb", "name": "FansDB", "url": "https://fansdb.cc/graphql"}]
rest = lambda k: Rest(k)

rows = fp.available(Stash(), clients("", "tok", rest), [])
check("only ThePornDB set up -> only ThePornDB listed", [r["key"] for r in rows] == ["theporndb"] and rows[0]["batch"] is False and rows[0]["submit"] is False)
rows = fp.available(Stash(), clients("k", "tok", rest), extra)
check("StashDB + ThePornDB + a stash-box listed", [r["key"] for r in rows] == ["stashdb", "theporndb", "fansdb"])
check("endpoint of the stash-box is its url", rows[2]["endpoint"] == "https://fansdb.cc/graphql" and rows[2]["batch"] is True)
check("nothing set up -> empty", fp.available(Stash(), clients("", "", rest), []) == [])
try:
    fp.resolve("stashdb", Stash(), clients("", "tok", rest), [])
    check("StashDB not set up -> ValueError", False)
except ValueError as e:
    check("StashDB not set up -> ValueError", "StashDB" in str(e))

lookup, row = fp.resolve("theporndb", Stash(), clients("", "tok", rest), [])
out = lookup.find_scenes_by_fingerprints([[{"hash": "a1", "algorithm": "OSHASH"}, {"hash": "a2", "algorithm": "MD5"}, {"hash": "a2", "algorithm": "PHASH"}], [{"hash": "zz", "algorithm": "OSHASH"}],
                                          [{"hash": "x", "algorithm": "DURATION"}]])
check("per-hash lookup merges and de-duplicates scene ids", [m["id"] for m in out[0]] == ["S1", "S2"])
check("PHASH (similarity-based at ThePornDB) is never asked", all(t != "PHASH" for _, t in lookup.client.calls))
check("no hit -> empty list, same length as the input", out[1] == [] and out[2] == [] and len(out) == 3)

# a TPDB run: scene tagged with the TPDB uuid, found as that scene -> match; found as another -> mismatch
scenes = [{"id": "1", "title": "A", "stash_ids": [{"endpoint": Stash.theporndb_endpoint, "stash_id": "S1"}], "files": [{"path": "/a", "fingerprints": [{"type": "oshash", "value": "a1"}]}]},
          {"id": "2", "title": "B", "stash_ids": [{"endpoint": Stash.theporndb_endpoint, "stash_id": "S9"}], "files": [{"path": "/b", "fingerprints": [{"type": "oshash", "value": "a1"}]}]}]
m, mm, amb, unv = library_check.find_library_mismatches(lookup, scenes, Stash.theporndb_endpoint, delay=0, provider_name="ThePornDB")
check("TPDB id matches", [x["id"] for x in m] == ["1"])
check("TPDB id wrong -> one correction", [x["id"] for x in mm] == ["2"] and mm[0]["found_as"][0]["id"] == "S1")
m, mm, amb, unv = library_check.find_library_mismatches(lookup, [{**scenes[0], "files": [{"path": "/a", "fingerprints": [{"type": "oshash", "value": "zz"}]}]}],
                                                       Stash.theporndb_endpoint, delay=0, provider_name="ThePornDB")
check("reason names the provider", unv and unv[0]["reason"].startswith("ThePornDB has no fingerprint match"))

with tempfile.TemporaryDirectory() as d:
    db = Database(os.path.join(d, "t.sqlite3"))
    r1 = library_check_repo.create_run(db, [])
    r2 = library_check_repo.create_run(db, ["/x"], "theporndb")
    library_check_repo.insert_finding(db, r1, 1, "A", "unverifiable", reason="r")
    library_check_repo.insert_finding(db, r2, 2, "B", "mismatch", expected_stashdb_id="e", candidates=[{"id": "S1"}])
    got = {f["scene_id"]: f["provider"] for f in library_check_repo.list_findings(db)}
    check("a run without provider counts as StashDB, findings carry the run's provider", got == {"1": "stashdb", "2": "theporndb"})
    f2 = library_check_repo.get_finding(db, [f for f in library_check_repo.list_findings(db) if f["scene_id"] == "2"][0]["id"])
    check("provider_of_finding", library_check_repo.provider_of_finding(db, f2) == "theporndb")

print("\nALL OK" if not fails else f"\n{len(fails)} FAILED")
sys.exit(1 if fails else 0)
