"""studio_gate: a Needs-review candidate of a studio unconnected to the scene's studio is set aside; unknown names, platforms, own performers stay (v0.260.9)."""
import os, sys, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "api"))
os.environ["DATA_DIR"] = tempfile.mkdtemp()
import other_scene_check as o, studio_gate as g  # noqa: E402
import routers.settings as rs  # noqa: E402

fails = []
def check(name, ok):
    print(("ok   " if ok else "FAIL ") + name)
    if not ok:
        fails.append(name)

STUDIOS = {"wankzvr": ("WankzVR", "POVR Premium"), "bride4k": ("Bride 4K", "VIP 4K"), "anal vids": ("Anal Vids", None),
           "sinful studio": ("Sinful Studio", "Anal Vids"), "sinful deeds": ("Sinful Deeds", None), "exploited teens": ("Exploited Teens", None),
           "teddy": ("Teddy", None)}
class Client:
    calls = 0
    def search_studios(self, term, limit=10):
        Client.calls += 1
        hit = STUDIOS.get(term.lower())
        return {"results": [{"name": hit[0], "parent": {"name": hit[1]} if hit[1] else None}] if hit else [{"name": "Unrelated Other"}]}
o.configure(lambda p: Client())
rs.get_setting_value = lambda db, k, d: {}.get(k, d)
gate = lambda studio, title, own=(), cast=("Charli Rose",): g.is_unrelated(None, "stashdb", studio, list(own), list(cast), title)

check("unrelated known studio -> set aside", gate("Exploited Teens", "WankzVR.Whos.Your.Teddy.Charli.Rose.GearVR.1600p"))
check("letter-digit joint opened: Bride4K is found as 'Bride 4K'", gate("Sinful Studio", "Bride4K.26.10.21.Amelia.Ost.XXX.1080p.MP4-NBQ", own=["Anal Vids"]))
check("sibling under the same parent stays", not gate("Sinful Deeds", "Sinful.Studio.Some.Title.1080p", own=["Anal Vids"]))
check("studio whose parent is ours stays", not gate("Anal Vids", "Sinful.Studio.Some.Title.1080p"))
check("our own studio in the name stays", not gate("Exploited Teens", "Exploited.Teens.Charli.Rose.1080p"))
check("platform name leads: stays", not gate("Elly Clutch", "Creator.2026.Lola.Rose.Elly.Clutch.1080p"))
check("platform name from the list (AnalVids): stays", not gate("Porn Force", "AnalVids.26.09.16.Mia.Piper.XXX.1080p"))
check("unknown first words: stays", not gate("Exploited Teens", "NoSuchSiteAtAll.Charli.Rose.1080p"))
check("a performer of the scene leads: stays", not gate("Exploited Teens", "Charli.Rose.Teddy.Whos.Your.1080p"))
n = Client.calls
gate("Exploited Teens", "WankzVR.Whos.Your.Teddy.Charli.Rose.GearVR.1600p")
check("lookups are cached", Client.calls == n)
rs.get_setting_value = lambda db, k, d: "false" if k == "studio_gate_enabled" else d
check("switched off: nothing is set aside", not gate("Exploited Teens", "WankzVR.Whos.Your.Teddy.Charli.Rose.GearVR.1600p"))
print("FAILED: " + ", ".join(fails) if fails else "ALL OK")
sys.exit(1 if fails else 0)
