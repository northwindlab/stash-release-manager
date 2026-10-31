"""Indexer result categories that can be switched off (v0.247.0).

The searches ask for the parent category 6000 (XXX) and filter on the categories each result carries; NZBFinder (2026-10) files its results under
sub categories: DVD 6010, SD 6030, HD 6040, UHD 6045, VR 6046, Creator 6047, JAV 6048, Pack 6050, Imageset 6060, Other 6999. Settings >
Indexer search lists them; the ones that are switched off (setting `excluded_categories`, comma separated ids) are dropped from every
result list. Default: Pack and Imageset (a pack of many scenes or a set of pictures is never the one scene we want).
"""

CATEGORIES = [
    (6010, "DVD"), (6030, "SD"), (6040, "HD"), (6045, "UHD"), (6046, "VR"), (6047, "Creator"),
    (6048, "JAV"), (6050, "Pack"), (6060, "Imageset"), (6999, "Other"),
]
DEFAULT_EXCLUDED = "6050,6060"


def parse(value):
    """'6050, 6060' -> {6050, 6060}; anything that is not a number is ignored."""
    return {int(p) for p in str(value or "").replace(";", ",").split(",") if p.strip().isdigit()}


def valid(value):
    parts = [p.strip() for p in str(value or "").split(",") if p.strip()]
    return all(p.isdigit() and 6000 <= int(p) < 7000 for p in parts)


def drop_excluded(items, excluded):
    """Items (Prowlarr results or Newznab items) without a category that is switched off. An item without categories stays."""
    if not excluded:
        return items
    out = []
    for item in items:
        ids = {c.get("id") if isinstance(c, dict) else c for c in (item.get("categories") or [])}
        if not (ids & excluded):
            out.append(item)
    return out
