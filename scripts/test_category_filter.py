"""Tests (v0.247.0): result categories that are switched off are dropped; the RSS interval has a floor of 10 minutes. No network. Run:
venv/bin/python scripts/test_category_filter.py"""
import os
import sys
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))
import category_filter as cf  # noqa: E402
import search_sources  # noqa: E402
from fastapi import HTTPException  # noqa: E402

FAILS = []
def check(name, cond):  # noqa: E302
    print(("ok   " if cond else "FAIL ") + name)
    if not cond: FAILS.append(name)

ex = cf.parse(cf.DEFAULT_EXCLUDED)
check("default switches off Pack and Imageset", ex == {6050, 6060})
items = [{"title": "scene", "categories": [{"id": 6045, "name": "UHD"}]},
         {"title": "pack", "categories": [{"id": 6000}, {"id": 6050}]},
         {"title": "pics", "categories": [{"id": 6060}]},
         {"title": "jav", "categories": [{"id": 6048}]},
         {"title": "fans", "categories": [{"id": 6047}]},
         {"title": "nocat"}]
kept = [i["title"] for i in cf.drop_excluded(items, ex)]
check("pack and imageset are dropped, JAV / Creator / unknown stay", kept == ["scene", "jav", "fans", "nocat"])
check("nothing excluded: nothing dropped", cf.drop_excluded(items, set()) == items)
check("plain ids (not dicts) work too", cf.drop_excluded([{"categories": [6050]}], ex) == [])
check("valid ids", cf.valid("6050,6060") and cf.valid("") and not cf.valid("5000") and not cf.valid("abc"))

# Prowlarr source applies the setting
class FakeClient:
    def search(self, q): return items
search_sources.set_excluded_categories(lambda: ex)
res = search_sources.ProwlarrSource(FakeClient()).search("x")
check("ProwlarrSource drops them", [r["title"] for r in res] == ["scene", "jav", "fans", "nocat"])
search_sources.set_excluded_categories(lambda: {6048})
check("... and follows a changed setting", "jav" not in [r["title"] for r in search_sources.ProwlarrSource(FakeClient()).search("x")])

# Newznab items carry every category
from newznab_client import NewznabClient  # noqa: E402
xml = ('<rss xmlns:newznab="http://www.newznab.com/DTD/2010/feeds/attributes/"><channel><item><title>t</title><link>http://x/y</link>'
       '<newznab:attr name="category" value="6000"/><newznab:attr name="category" value="6050"/><newznab:attr name="size" value="5"/></item></channel></rss>')
c = NewznabClient("http://x", "k", "/api")
c._get = lambda params: ET.fromstring(xml)
got = c.search("q")
check("Newznab items carry their categories", [x["id"] for x in got[0]["categories"]] == [6000, 6050])

# settings validation
from routers import settings as st  # noqa: E402
def rejects(key, value):
    try:
        st._validate(key, value) if hasattr(st, "_validate") else None
    except HTTPException:
        return True
    return False
src = open(os.path.join(os.path.dirname(__file__), "..", "api", "routers", "settings.py"), encoding="utf-8").read()
check("rss interval floor is 10", "10 <= int(value) <= 1440" in src and "5 <= int(value) <= 1440" not in src)
check("excluded_categories is validated", "category_filter.valid(value)" in src)
print("\nALL OK" if not FAILS else "\nFAILED: %s" % FAILS)
sys.exit(1 if FAILS else 0)
