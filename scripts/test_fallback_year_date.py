"""fallback_resolutions() understands year-only and year-month scene dates (v0.260.7); a year-only date used to count as 'no date' -> no fallback."""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "api"))
from datetime import date  # noqa: E402
import indexer_search as ix  # noqa: E402
import routers.settings as rs  # noqa: E402
rs.get_setting_value = lambda db, k, d: {"fallback_resolution_days": "10", "fallback_accept_sd": "true"}.get(k, d)
T = date(2026, 11, 14)
fails = []
def check(name, ok):
    print(("ok   " if ok else "FAIL ") + name)
    if not ok:
        fails.append(name)
f = lambda d: ix.fallback_resolutions(None, d, today=T)
check("year-only, long ago: fallback allowed", f("2007") == [2160, 480])
check("year-only, this year: not yet", f("2026") == [])
check("year-month, last year: allowed", f("2025-12") == [2160, 480])
check("year-month, this month: not yet", f("2026-11") == [])
check("full date still works", f("2021-12-08") == [2160, 480] and f("2026-11-10") == [])
check("no date / garbage: none", f("") == [] and f(None) == [] and f("abcd") == [])
print("FAILED: " + ", ".join(fails) if fails else "ALL OK")
sys.exit(1 if fails else 0)
