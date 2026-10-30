"""Tests (v0.246.16): a scene dated today or yesterday is not "stuck" yet. main.py is large, so the two functions are cut out of its source.
Run:  venv/bin/python scripts/test_stuck_grace.py"""
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone

src = open(os.path.join(os.path.dirname(__file__), "..", "api", "main.py"), encoding="utf-8").read()
block = re.search(r"STUCK_GRACE_DAYS = 1.*?\n\n\nIMMEDIATE_TARGETED_PER_TICK", src, re.S).group(0).rsplit("\n\n\n", 1)[0]
ns = {"datetime": datetime, "timezone": timezone}
exec(block, ns)

FAILS = []
def check(name, cond):  # noqa: E302
    print(("ok   " if cond else "FAIL ") + name)
    if not cond: FAILS.append(name)

t = date(2026, 10, 30)
f = ns["too_new_to_be_stuck"]
check("today is too new", f("2026-10-30", t))
check("yesterday is too new", f("2026-10-29", t))
check("two days ago is stuck", not f("2026-10-28", t))
check("a date with a time part works", f("2026-10-30T00:00:00Z", t))
check("no date: not too new", not f(None, t) and not f("", t) and not f("garbage", t))
check("a future date is too new", f("2026-11-02", t))
rows = [{"last_search_result": "no_candidates", "scene_date": "2020-01-01"},
        {"last_search_result": "no_candidates", "scene_date": date.today().isoformat()},
        {"last_search_result": None, "scene_date": "2020-01-01"},
        {"last_search_result": "duplicate_download", "scene_date": "2020-01-01"}]
check("stuck_releases keeps only the old searched one", len(ns["stuck_releases"](rows)) == 1)
print("\nALL OK" if not FAILS else "\nFAILED: %s" % FAILS)
sys.exit(1 if FAILS else 0)
