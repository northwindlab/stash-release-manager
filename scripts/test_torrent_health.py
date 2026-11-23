"""Torrent monitor (v0.231.0): the state rules (classify) and one round (run) with fake clients and hooks."""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))
import db as dbmod
import history
import release_repo
import torrent_health as th

fails = []
def check(name, cond):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond: fails.append(name)

H = 3600.0
CFG = {"stall_s": 1 * H, "real_progress": 0.05, "alt_s": 6 * H}
HASH = "a" * 40

def tor(progress=0.0, state="stalledDL", dlspeed=0, **kw):
    return {"hash": HASH, "state": state, "progress": progress, "dlspeed": dlspeed, "num_complete": 0, "num_incomplete": 3, "availability": 0.4, **kw}

# -- classify (pure), as a series of rounds like run() makes them
M = 1800.0  # one round = 30 minutes
CFG["max_gap_s"] = 2.5 * M

def roll(torrent, prev, now):
    """One round: the verdict and the row that would be stored (what the next round sees as `prev`)."""
    v = th.classify(torrent, prev, now, CFG)
    row = {**(prev or {}), "state": v["state"], "detail": v["detail"], "progress": v["progress"], "max_progress": v["max_progress"],
           "last_progress_change_at": v["last_progress_change_at"], "stalled_since": v["stalled_since"], "stalled_s": v["stalled_s"],
           "last_checked_at": now, "raw_state": (torrent or {}).get("state")}
    return v, row

def rounds(torrent_at, start, n, prev=None):
    """n rounds, 30 min apart, `torrent_at(i)` gives the torrent of round i; returns (last verdict, last row, all verdicts)."""
    verdicts = []
    for i in range(n):
        v, prev = roll(torrent_at(i), prev, start + i * M)
        verdicts.append(v)
    return verdicts[-1], prev, verdicts

v, row = roll(tor(), None, 1000)
check("first sight of a dead torrent: ok, the window starts now", v["state"] == "ok" and v["stalled_s"] == 0 and v["action"] == "none")
v, row = roll(tor(), row, 1000 + M)
check("30 min later: still ok, with a note", v["state"] == "ok" and "30 min" in v["detail"] and v["action"] == "none")
v, row = roll(tor(), row, 1000 + 2 * M)
check("1 h of looks without progress, nothing downloaded: stuck, look for another", v["state"] == "stuck" and v["action"] == "switch")
check("... the detail says how long and how far", "1.0 h" in v["detail"] and "0 %" in v["detail"])
v, row = rounds(lambda i: tor(progress=0.4), 0, 4)[:2]
check("1 h+ at 40 %: stalled, flagged only, never switched", v["state"] == "stalled" and v["action"] == "none")
v, row = rounds(lambda i: tor(progress=0.04), 0, 4)[:2]
check("4 % is not real progress yet", v["state"] == "stuck" and v["action"] == "switch")
v, row = rounds(lambda i: tor(progress=0.0), 0, 3)[:2]
v, row = roll(tor(progress=0.0), dict(row, max_progress=0.3), 3 * M)
check("it once had 30 %: real progress, flagged only", v["state"] == "stalled" and v["action"] == "none")
v, row = rounds(lambda i: tor(progress=0.01 * i), 0, 6)[:2]
check("progress between the looks: ok, window reset", v["state"] == "ok" and v["stalled_s"] == 0)
v, row = rounds(lambda i: tor(dlspeed=50_000), 0, 6)[:2]
check("download speed counts as moving", v["state"] == "ok" and v["stalled_s"] == 0)
v, row = rounds(lambda i: tor(progress=1.0, state="stalledUP"), 0, 2)[:2]
check("complete: done", v["done"] is True)

# -- paused / stopped by the user, queued / checking by the client
for raw, word in (("pausedDL", "paused"), ("stoppedDL", "stopped")):
    last, row, all_v = rounds(lambda i: tor(state=raw, progress=0.3), 0, 12)
    check(f"{raw} for 6 h: shown as stalled with a note, never switched", last["state"] == "stalled" and word in last["detail"] and all(v["action"] == "none" for v in all_v))
last, row, all_v = rounds(lambda i: tor(state="pausedDL", progress=0.0), 0, 12)
check("paused with nothing downloaded is not 'dead' either", last["state"] == "stalled" and all(v["action"] == "none" for v in all_v) and last["stalled_s"] == 0)
last, row, all_v = rounds(lambda i: tor(state="queuedDL"), 0, 12)
check("queued by the client for 6 h: ok with a note, never switched", last["state"] == "ok" and "queued" in last["detail"] and all(v["action"] == "none" for v in all_v))
last, row, all_v = rounds(lambda i: tor(state="checkingDL"), 0, 4)
check("checking: ok", last["state"] == "ok" and last["action"] == "none")
# paused for 5 h, then resumed and dead: the paused time does not count
v1, row = rounds(lambda i: tor(state="pausedDL"), 0, 10)[:2]
v2, row = roll(tor(), row, 10 * M)
check("resumed after a long pause: the clock starts at zero", v2["state"] == "ok" and v2["stalled_s"] == 0 and v2["action"] == "none")
v3, row = roll(tor(), row, 11 * M)
v4, row = roll(tor(), row, 12 * M)
check("... and only after an hour of looks it is stuck", v3["action"] == "none" and v4["state"] == "stuck" and v4["action"] == "switch")

# -- the sidecar or the client was not there: gaps do not count as stalled time
v, row = rounds(lambda i: tor(), 0, 2)[:2]            # 30 min of stall observed
v, row = roll(tor(), row, 2 * M + 10 * 3600)            # looked again 10 h later
check("a look 10 h after the last one starts a new window (nothing is switched)", v["state"] == "ok" and v["stalled_s"] == 0 and v["action"] == "none")
v, row = roll(tor(), row, 2 * M + 10 * 3600 + M)
check("... the next look counts 30 min", v["stalled_s"] == M and v["action"] == "none")
v, row = rounds(lambda i: tor(), 0, 2)[:2]
v, row = roll(tor(), row, M + 2.4 * M)                     # a slow round: still continuous
check("a gap of 2.4 rounds is still continuous", v["stalled_s"] > M and v["state"] == "stuck")
v, row = roll(tor(progress=0.6), dict(row, progress=0.3, max_progress=0.3), 10 * 24 * 3600)
check("days later with more progress than before: ok (it kept going while nobody looked)", v["state"] == "ok")
v, row = roll(tor(progress=0.3, state="pausedDL"), dict(row, progress=0.3, max_progress=0.3, last_checked_at=0), 5 * 24 * 3600)
check("days later, paused after the restart: stalled with the note, no switch", v["state"] == "stalled" and v["action"] == "none" and "paused" in v["detail"])
v, row = roll(tor(progress=1.0, state="uploading"), dict(row, last_checked_at=0), 7 * 24 * 3600)
check("days later it is complete: done", v["done"])

# -- error / missing need to be seen twice before anything is switched
v1, row = roll(tor(state="error"), {"progress": 0, "max_progress": 0, "last_checked_at": 0, "state": "ok"}, M)
check("qBittorrent error: flagged, not switched at the first look", v1["state"] == "error" and v1["action"] == "none")
v2, row = roll(tor(state="error"), row, 2 * M)
check("... switched when it is still there at the next look (nothing downloaded)", v2["state"] == "error" and v2["action"] == "switch")
v, row = roll(tor(state="missingFiles", progress=0.6), {"progress": 0.6, "max_progress": 0.6, "last_checked_at": 0, "state": "error"}, M)
check("missing files with progress: flagged, no switch, hint in the text", v["state"] == "error" and v["action"] == "none" and "recheck" in v["detail"])
v1, row = roll(None, {"progress": 0, "max_progress": 0, "last_checked_at": 0, "state": "ok"}, M)
check("gone from the client: missing, but not switched at the first look (a client that has just started)", v1["state"] == "missing" and v1["action"] == "none")
v2, row = roll(None, row, 2 * M)
check("... still gone at the next look: switch (nothing downloaded)", v2["state"] == "missing" and v2["action"] == "switch")
v2, row = roll(None, dict(row, max_progress=0.5), 2 * M)
check("gone after real progress: missing, no switch", v2["state"] == "missing" and v2["action"] == "none")
v3, row = roll(None, {"progress": 0, "max_progress": 0, "last_checked_at": 0, "state": "missing"}, 10 * 3600)
check("'missing' seen long ago does not confirm a new 'missing'", v3["action"] == "none")

# -- alternative search rate, keep waiting, recovery
last, row, _ = rounds(lambda i: tor(), 0, 3)
v, row2 = roll(tor(), dict(row, last_alt_search_at=2 * M), 3 * M)
check("alternative searched 30 min ago: not again yet", v["state"] == "stuck" and v["action"] == "none")
v, row2 = roll(tor(), dict(row, last_alt_search_at=0), 3 * M + 7 * 3600 - 3 * M)
check("... the search is allowed again after 6 h", th.classify(tor(), dict(row, last_alt_search_at=0, last_checked_at=7 * 3600 - M), 7 * 3600, CFG)["action"] == "switch")
v, row2 = roll(tor(), dict(row, keep_until=10 * 3600), 3 * M)
check("'keep waiting': flagged but no switch", v["state"] == "stuck" and v["action"] == "none")
v, row2 = roll(tor(progress=0.2, dlspeed=10), dict(row, state="stuck"), 3 * M)
check("a stuck torrent that moves again is ok again", v["state"] == "ok")

class Db:
    def __init__(self):
        self.c = sqlite3.connect(":memory:"); self.c.row_factory = sqlite3.Row
        dbmod.Database._migrate_torrent_health(None, self.c)
    def connect(self):
        c = self.c
        class Ctx:
            def __enter__(s): return c
            def __exit__(s, *a): c.commit()
        return Ctx()

class Client:
    def __init__(self, torrents): self.torrents, self.deleted = torrents, []
    def get_torrents(self, category=None):
        if self.torrents is None: raise OSError("down")
        return self.torrents
    def delete(self, h, delete_files=False): self.deleted.append((h, delete_files))

blocked, logged = [], []
release_repo.add_review_rejected_title = lambda db, p, s, title, exact=False: blocked.append((p, s, title))
history.scene = lambda *a, **k: logged.append(a)
history.event = lambda *a, **k: logged.append(a)
th.load_cfg = lambda db: CFG

def target(**kw):
    return {"kind": "scene", "provider": "stashdb", "scene_id": "s1", "film_id": None, "title": "Wet and Clean", "client_id": 1, "job_id": HASH,
            "upgrade": False, "client_row": {"id": 1, "name": "qbit"}, **kw}

def setup(torrents, tgt=None):
    d, c = Db(), Client(torrents)
    th.targets = lambda db: [tgt or target()]
    return d, c

calls = []
hooks = {"switch_scene": lambda t: (calls.append(t["job_id"]), True)[1]}

def go(d, hk, c, t_from, t_to, step=M):
    """Rounds every 30 minutes from t_from to t_to (inclusive), like the loop does."""
    t, s = t_from, None
    while t <= t_to + 1e-6:
        s = th.run(d, hk, now=t, client_factory=lambda row: c)
        t += step
    return s

# first rounds adopt, nothing is switched in the first hour
d, c = setup([tor()])
go(d, hooks, c, 10_000, 10_000)
row = th.get_row(d, 1, HASH)
check("adopted at the first look, nothing switched", row["state"] == "ok" and calls == [] and c.deleted == [])
go(d, hooks, c, 10_000 + M, 10_000 + M)
check("30 min later: still waiting", th.get_row(d, 1, HASH)["state"] == "ok" and calls == [])
s = go(d, hooks, c, 10_000 + 2 * M, 10_000 + 2 * M)
check("after an hour of looks: the alternative was grabbed, the old torrent deleted WITH its data, blocked by hash, row gone",
      s["switched"] == 1 and c.deleted == [(HASH, True)] and blocked == [("stashdb", "s1", "hash:" + HASH)] and th.get_row(d, 1, HASH) is None)

# no alternative -> stuck, nothing deleted, not searched again within 6 h
calls.clear(); blocked.clear()
hooks_none = {"switch_scene": lambda t: (calls.append(1), False)[1]}
d, c = setup([tor()])
go(d, hooks_none, c, 0, 2 * M)
row = th.get_row(d, 1, HASH)
check("no alternative: stuck, torrent stays, nothing blocked", row["state"] == "stuck" and "no other torrent" in row["detail"] and c.deleted == [] and blocked == [] and len(calls) == 1)
go(d, hooks_none, c, 3 * M, 5 * H)
check("not searched again within 6 h, the note stays", len(calls) == 1 and th.get_row(d, 1, HASH)["state"] == "stuck" and "no other torrent" in th.get_row(d, 1, HASH)["detail"])
go(d, hooks_none, c, 5 * H + M, 7 * H)
check("searched again after 6 h", len(calls) == 2)
c.torrents = [tor(progress=0.3, dlspeed=20_000)]
go(d, hooks_none, c, 7 * H + M, 7 * H + M)
check("it starts moving: ok again, still in the table", th.get_row(d, 1, HASH)["state"] == "ok")
c.torrents = [tor(progress=1.0, state="uploading")]
go(d, hooks_none, c, 7 * H + 2 * M, 7 * H + 2 * M)
check("complete: the row is removed (the import takes over)", th.get_row(d, 1, HASH) is None)

# the search itself failed (indexer not answering): stuck with that note, and NO 6 h back-off -- the next look searches again
calls.clear(); blocked.clear()
hooks_fail = {"switch_scene": lambda t: (calls.append(1), "failed")[1]}
d, c = setup([tor()])
go(d, hooks_fail, c, 0, 2 * M)
row = th.get_row(d, 1, HASH)
check("search failed: stuck with the 'could not search' note, nothing deleted or blocked",
      row["state"] == "stuck" and "could not search" in row["detail"] and c.deleted == [] and blocked == [] and len(calls) == 1)
go(d, hooks_fail, c, 3 * M, 2 * M + M)
check("search failed: searched again at the very next look (no back-off)", len(calls) == 2)

# real progress: never switched
calls.clear()
d, c = setup([tor(progress=0.4)])
go(d, hooks, c, 0, 3 * H)
check("40 % and dead: stalled, flagged, no search, nothing deleted", th.get_row(d, 1, HASH)["state"] == "stalled" and calls == [] and c.deleted == [])

# paused by the user: stalled with a note, never switched, whatever the time
calls.clear()
d, c = setup([tor(state="pausedDL", progress=0.0)])
go(d, hooks, c, 0, 24 * H)
row = th.get_row(d, 1, HASH)
check("paused for a day: stalled with the note, nothing searched or deleted", row["state"] == "stalled" and "paused" in row["detail"] and calls == [] and c.deleted == [])

# error / missing: flagged; the switch needs the same problem twice
d, c = setup([tor(state="error")])
go(d, {"switch_scene": lambda t: False}, c, 0, M)
check("error state flagged", th.get_row(d, 1, HASH)["state"] == "error")
d, c = setup([])
go(d, {"switch_scene": lambda t: False}, c, 0, M)
check("gone from the client: missing", th.get_row(d, 1, HASH)["state"] == "missing")

# the client (or the sidecar) was away: no stall time is counted, nothing switched
calls.clear()
d, c = setup([tor()])
go(d, hooks, c, 0, M)                       # 30 min of looks
c.torrents = None                            # the client is down for 12 h (no look is possible)
s = go(d, hooks, c, 2 * M, 12 * H)
check("client unreachable: skipped, row kept, not 'missing'", s["unreachable"] == 1 and th.get_row(d, 1, HASH)["state"] == "ok")
c.torrents = [tor()]                         # it is back, the torrent has not moved (it was down)
go(d, hooks, c, 12 * H + M, 12 * H + M)
check("client back after 12 h: a new window starts, nothing switched", calls == [] and c.deleted == [] and th.get_row(d, 1, HASH)["state"] == "ok")
go(d, hooks, c, 12 * H + 2 * M, 12 * H + 3 * M)
check("... an hour of looks after that the normal rule applies", len(calls) == 1)
calls.clear()
d, c = setup([tor()])
go(d, hooks, c, 0, M)
go(d, hooks, c, 3 * 24 * H, 3 * 24 * H)      # the sidecar was not running for 3 days
check("sidecar away for 3 days: the first look afterwards switches nothing", calls == [] and c.deleted == [] and th.get_row(d, 1, HASH)["state"] == "ok")
class Offline(Client):
    def transfer_info(self): return {"connection_status": "disconnected"}
calls.clear()
d = Db(); c = Offline([tor()]); th.targets = lambda db: [target()]
go(d, hooks, c, 0, M)
c.torrents = [tor()]
s = go(d, hooks, c, 2 * M, 10 * H)
check("client up but disconnected from the network: rounds skipped, nothing switched", s["offline"] == 1 and calls == [] and c.deleted == [])

# an upgrade download is flagged only
calls.clear()
d, c = setup([tor()], target(upgrade=True))
go(d, hooks, c, 0, 2 * H)
check("upgrade download: flagged, never switched", th.get_row(d, 1, HASH)["state"] == "stuck" and calls == [] and c.deleted == [])

# movie: flagged only
d, c = setup([tor()], target(kind="film", film_id=7, provider=None, scene_id=None))
go(d, hooks, c, 0, 2 * H)
row = th.get_row(d, 1, HASH)
check("movie: stuck, nothing grabbed or deleted, hint to remove it", row["state"] == "stuck" and calls == [] and c.deleted == [] and "remove it" in row["detail"])

# rows of downloads that are gone are cleaned up
d, c = setup([tor()])
go(d, hooks_none, c, 0, 0)
th.targets = lambda db: []
go(d, hooks_none, c, M, M)
check("a download no longer running loses its row", th.get_row(d, 1, HASH) is None)

# ---- the note between two looks (v0.231.6) ----
import torrent_health as _th
_cfg = _th.default_cfg()
_row = {"state": "ok", "detail": "no progress for 51 min", "stalled_s": 3060, "last_checked_at": 1000.0}
_d = _th.live_detail(_row, 1000.0 + 16 * 60, _cfg, 1800)
check("the stall time carries on after the last look and says when the next look is", _d == "no progress for 1.1 h (looked 16 min ago, next look in 14 min)")
check("a stalled note keeps its tail", _th.live_detail({**_row, "state": "stalled", "stalled_s": 7200, "detail": "no progress for 2.0 h at 12 %"}, 1060.0, _cfg, 1800).startswith("no progress for 2.0 h at 12 %"))
check("other notes untouched", _th.live_detail({**_row, "detail": "paused in the client"}, 5000.0, _cfg, 1800) == "paused in the client")
check("stuck rows untouched", _th.live_detail({**_row, "state": "stuck"}, 5000.0, _cfg, 1800) == "no progress for 51 min")
_old = _th.live_detail(_row, 1000.0 + 3 * 3600, _cfg, 1800)
check("a look long ago: nothing added, no next look", _old == "no progress for 51 min (looked 180 min ago)")


# ---- live view (display only, v0.231.6) ----
_now = 100000.0
_prev = {"state": "ok", "detail": "no progress for 51 min", "stalled_s": 3060.0, "last_checked_at": _now - 16 * 60, "progress": 0.0, "max_progress": 0.0,
         "raw_state": "metaDL", "stalled_since": _now - 67 * 60, "last_progress_change_at": _now - 67 * 60}
_t = lambda **k: {"hash": "h", "progress": 0.0, "dlspeed": 0, "state": "metaDL", "num_complete": 0, "num_incomplete": 1, "availability": 0.0, **k}
_v = _th.live_view(_prev, _t(), _now, _cfg)
check("live: still stalled, the time carries on to 67 min and the swarm is the client's", _v and _v["detail"].startswith("no progress for 1.1 h") and _v["peers"] == 1)
check("live: past the hour the row is not decided here (acted on at the next look)", _v["state"] == "ok" and "next look" in _v["detail"])
_v = _th.live_view(_prev, _t(dlspeed=50000, state="downloading", num_complete=4), _now, _cfg)
check("live: it moves again -> no stall note, seeds shown", _v["state"] == "ok" and _v["detail"] == "" and _v["seeds"] == 4)
_v = _th.live_view(_prev, _t(state="pausedDL"), _now, _cfg)
check("live: paused in the client shows at once", _v["state"] == "stalled" and "paused" in _v["detail"])
check("live: missing / error are left to the monitor", _th.live_view(_prev, None, _now, _cfg) is None and _th.live_view(_prev, _t(state="error"), _now, _cfg) is None)
_stuck = {**_prev, "state": "stuck", "detail": "no progress for 3.0 h -- no other torrent found"}
_v = _th.live_view(_stuck, _t(), _now, _cfg)
check("live: a stuck row keeps its note", _v["state"] == "stuck" and _v["detail"].endswith("-- no other torrent found"))


# "Find another" button: a failed search must not remove the torrent
import routers.torrent_health as _R
_row = {"client_id": 1, "job_id": "abc", "kind": "scene", "provider": "p", "scene_id": "s", "title": "t"}
_orig = (th.get_row, th.finish_replacement)
_fin = []
th.get_row = lambda db, c, j: _row
th.finish_replacement = lambda db, r: _fin.append(1)
_R._db, _R._target_of = object(), (lambda r: {"x": 1})
_out = {}
for _ret in ("failed", False, True):
    _R._hooks = {"switch_scene": lambda t, _ret=_ret: _ret}
    _out[_ret] = _R.switch({"client_id": 1, "job_id": "abc"})
th.get_row, th.finish_replacement = _orig
check("Find another: a failed search keeps the old torrent", _out["failed"]["switched"] is False and "did not work" in _out["failed"]["message"])
check("Find another: nothing found keeps it too; only a grabbed one replaces it", _out[False]["switched"] is False and _out[True]["switched"] is True and _fin == [1])

# A torrent removed in the client by hand (v0.261.2): shown as missing at once, not as "download complete"; one round is asked for (throttled)
import routers.releases as _Rel
_listed = {"__listed__": {"aaa"}}
check("gone: a client that answered without the torrent", _Rel._gone(_listed, "BBB") and not _Rel._gone(_listed, "AAA"))
check("gone: a client that did not answer / no torrent client says nothing", not _Rel._gone({}, "bbb") and not _Rel._gone(None, "bbb"))
check("gone: the phase is a torrent problem, not 'waiting for import'", _Rel._gone_phase()["phase"] == "torrent_problem" and _Rel._gone_phase()["health"] == "missing")
_started = []
_R._run["fn"], _R._run["running"] = (lambda: _started.append(1)), False
_R._kicked["at"] = 0.0
check("kick: the first one starts a round, a second within the gap does not", _R.kick_round() is True and _R.kick_round() is False)
import time as _t2
_t2.sleep(0.2)
check("kick: the round ran once and the running flag is free again", _started == [1] and _R._run["running"] is False)
_pub_row = {"client_id": 1, "job_id": "abc", "kind": "scene", "provider": "p", "scene_id": "s", "film_id": None, "title": "t", "state": "stalled",
            "detail": "no progress for 3.0 h", "progress": 0.99, "max_progress": 0.99, "seeds": 0, "peers": 6, "availability": 0.99, "raw_state": "forcedDL",
            "stalled_s": 5000, "last_checked_at": 100.0}
_orig_cfg, _orig_kick = _R.torrent_health.load_cfg, _R.kick_round
_kicks = []
_orig_iv = _R.torrent_health.check_interval_s
_R.torrent_health.check_interval_s = lambda db: 1800.0
_R.torrent_health.load_cfg, _R.kick_round = (lambda db: th.default_cfg()), (lambda *a, **k: _kicks.append(1))
_R._db = object()
_gone_pub = _R._public(_pub_row, 200.0, {}, {1})
_unknown_pub = _R._public(_pub_row, 200.0, {}, set())
_there_pub = _R._public(_pub_row, 200.0, {(1, "abc"): tor(progress=0.99, dlspeed=5)}, {1})
_R.torrent_health.load_cfg, _R.kick_round = _orig_cfg, _orig_kick
_R.torrent_health.check_interval_s = _orig_iv
check("stuck list: a torrent the client no longer has shows as missing at once (and asks for a round)",
      _gone_pub["state"] == "missing" and "removed from the download client" in _gone_pub["detail"] and _gone_pub["percent"] == 99.0 and _kicks == [1])
check("stuck list: a client that did not answer keeps the stored state", _unknown_pub["state"] == "stalled")
check("stuck list: a torrent that is still there is not missing", _there_pub["state"] != "missing")

# "Dismiss": the entry goes, nothing is blocked or searched again
import upgrades as _up
_rec = {"job": [], "status": [], "undo": [], "cand": []}
_o = (release_repo.set_download_job_for_scene, getattr(release_repo, "set_status_for_scene", None), _up._scene_rows, _up._undo_release_rows, _up.set_candidate, th._client_of)
release_repo.set_download_job_for_scene = lambda db, p, s, c, j: _rec["job"].append((p, s, c, j))
release_repo.set_status_for_scene = lambda db, p, s, st: _rec["status"].append((p, s, st))
_up._undo_release_rows = lambda db, cand, prev: _rec["undo"].append(prev)
_up.set_candidate = lambda db, cid, **f: _rec["cand"].append((cid, f.get("status")))
blocked.clear()
d, c = setup([tor()])
th._client_of = lambda db, cid: c
go(d, hooks_none, c, 0, 0)
_up._scene_rows = lambda db, p, s: [{"entity_type": "studio", "entity_stashdb_id": "st1", "upgrade_of": None, "imported_at": None}]
_r = th.dismiss(d, 1, HASH)
check("dismiss (wishlist scene): the torrent and its data go, the job is cleared, the scene is dismissed, nothing blocked, row gone",
      _r == {"ok": True} and c.deleted == [(HASH, True)] and _rec["job"] == [("stashdb", "s1", None, None)] and _rec["status"] == [("stashdb", "s1", "dismissed")]
      and blocked == [] and th.get_row(d, 1, HASH) is None)
check("dismiss: a second one says the entry is gone", "error" in th.dismiss(d, 1, HASH))
# an upgrade download: the library file stays, the candidate is dismissed, the rows go back
d, c = setup([tor()])
th._client_of = lambda db, cid: c
d.c.execute("CREATE TABLE upgrade_candidate (id INTEGER, provider_scene_id TEXT, file_path TEXT)")
d.c.execute("INSERT INTO upgrade_candidate VALUES (7, 's1', '/lib/old.mp4')")
go(d, hooks_none, c, 0, 0)
_rec["status"].clear()
_up._scene_rows = lambda db, p, s: [{"entity_type": "studio", "entity_stashdb_id": "upgrade:9", "upgrade_of": "/lib/old.mp4", "imported_at": None}]
th.dismiss(d, 1, HASH)
check("dismiss (upgrade): candidate dismissed, rows undone, scene status untouched",
      _rec["cand"] == [(7, "dismissed")] and _rec["undo"][-1] == [] and _rec["status"] == [])
release_repo.set_download_job_for_scene = _o[0]
if _o[1]: release_repo.set_status_for_scene = _o[1]
_up._scene_rows, _up._undo_release_rows, _up.set_candidate, th._client_of = _o[2:]

# a movie: dismissed; an upgrade of a movie goes back to plain 'imported' (no upgrade wanted)
import film_repo as _fr
_fu, _fg = [], {"upgrading": 0}
_o2 = (_fr.get, _fr.update)
_fr.get = lambda db, i: {"id": i, "title": "M", **_fg}
_fr.update = lambda db, i, **f: _fu.append(f)
for _upg in (0, 1):
    _fg["upgrading"] = _upg
    d, c = setup([tor()], tgt={**target(), "kind": "film", "scene_id": None, "provider": None, "film_id": 5})
    th._client_of = lambda db, cid: c
    go(d, hooks_none, c, 0, 0)
    th.dismiss(d, 1, HASH)
check("dismiss (movie): dismissed, job cleared; an upgrade goes back to imported",
      _fu[0]["status"] == "dismissed" and _fu[0]["download_job_id"] is None and _fu[1]["status"] == "imported" and _fu[1]["upgrade_wanted"] == 0)
_fr.get, _fr.update = _o2
th._client_of = _o[5]

print("\nALL OK" if not fails else f"\n{len(fails)} FAILED")
sys.exit(1 if fails else 0)

