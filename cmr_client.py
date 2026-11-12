# Granule search against NASA's Common Metadata Repository (CMR).
# This plays the part stac_client.py plays in SatStream: it turns a platform,
# a bounding box, a date range and a cloud limit into a list of scene dicts
# with a footprint and a key -> URL map of the scene's files.
#
# CMR's granules.json is used rather than CMR-STAC because it filters on cloud
# cover for every collection that records it and pages cleanly with the
# CMR-Search-After header.

import re

from . import credentials
from .providers import CMR_GRANULES, PROVIDERS

DATA_REL = "http://esipfed.org/ns/fedsearch/1.1/data#"
BROWSE_REL = "http://esipfed.org/ns/fedsearch/1.1/browse#"
_SKIP_EXT = (".xml", ".json", ".md5", ".sha256", ".txt", ".png", ".jpg", ".jpeg", ".html")


def search(provider_name, bbox, start, end, max_cloud=None, limit=500, is_canceled=None, status=None):
    """bbox is (west, south, east, north) in degrees; start/end are 'YYYY-MM-DD'."""
    cfg = PROVIDERS[provider_name]
    w, s, e, n = bbox
    params = {
        "short_name": cfg["short_name"],
        "provider": cfg["provider"],
        "bounding_box": "%.6f,%.6f,%.6f,%.6f" % (w, s, e, n),
        "page_size": min(limit, 500),
        "sort_key[]": "-start_date",
    }
    if cfg.get("version"):
        params["version"] = cfg["version"]
    # DEMs are static; a date filter would only hide tiles.
    if cfg["kind"] != "dem":
        params["temporal"] = "%sT00:00:00Z,%sT23:59:59Z" % (start, end)
    if max_cloud is not None and cfg.get("supports_cloud") and max_cloud < 100:
        params["cloud_cover"] = "0,%d" % int(max_cloud)

    scenes, headers, page = [], {}, 0
    while True:
        if is_canceled and is_canceled():
            break
        page += 1
        if status:
            status("Searching CMR (page %d)..." % page)
        data, resp_headers = credentials.get_json(CMR_GRANULES, params, headers=headers)
        entries = data.get("feed", {}).get("entry", [])
        for entry in entries:
            scene = parse_entry(entry, provider_name)
            if scene:
                scenes.append(scene)
        hits = int(resp_headers.get("CMR-Hits") or resp_headers.get("cmr-hits") or 0)
        after = resp_headers.get("CMR-Search-After") or resp_headers.get("cmr-search-after")
        if not entries or not after or len(scenes) >= limit or len(scenes) >= hits:
            break
        headers = {"CMR-Search-After": after}
    return scenes[:limit], hits


def parse_entry(entry, provider_name):
    cfg = PROVIDERS[provider_name]
    files, browse = {}, None
    all_links = []
    for link in entry.get("links", []):
        href = link.get("href", "")
        rel = link.get("rel", "")
        if link.get("inherited"):
            continue
        if rel == BROWSE_REL and href.startswith("https") and not browse:
            browse = href
            continue
        if rel != DATA_REL or not href.startswith("https"):
            continue
        if href.lower().endswith(_SKIP_EXT):
            continue
        all_links.append(href)
        name = href.rsplit("/", 1)[-1]
        for key, pattern in cfg["bands"].items():
            if key not in files and re.search(pattern, name):
                files[key] = href
    if not all_links:
        return None

    gid = entry.get("producer_granule_id") or entry.get("title") or entry.get("id")
    tile = ""
    m = re.search(cfg.get("tile_regex") or r"$^", gid) or re.search(cfg.get("tile_regex") or r"$^",
                                                                      all_links[0].rsplit("/", 1)[-1])
    if m:
        tile = m.group(1)

    cloud = entry.get("cloud_cover")
    try:
        cloud = float(cloud) if cloud not in (None, "") else None
    except ValueError:
        cloud = None

    return dict(
        id=gid,
        concept_id=entry.get("id"),
        provider=provider_name,
        datetime=entry.get("time_start") or "",
        cloud=cloud,
        tile=tile,
        sensor=_sensor(gid, provider_name),
        rings=footprint(entry),
        files=files,
        links=all_links,
        browse=browse,
    )


def _sensor(gid, provider_name):
    if gid.startswith("HLS.L30"):
        return "HLS L30"
    if gid.startswith("HLS.S30"):
        return "HLS S30"
    if provider_name.startswith("OPERA"):
        return "OPERA"
    if gid.startswith("ECOv002") or gid.startswith("ECO"):
        return "ECOSTRESS"
    if gid.startswith("ASTGTM"):
        return "ASTER"
    if gid.startswith("NASADEM"):
        return "SRTM"
    if gid.startswith("MOD"):
        return "Terra MODIS"
    if gid.startswith("MYD"):
        return "Aqua MODIS"
    return provider_name.split(" (")[0]


def footprint(entry):
    """Return a list of rings, each a list of (lon, lat), from CMR polygons or boxes."""
    rings = []
    for poly in entry.get("polygons") or []:
        # Each polygon is a list of rings; the first is the outer ring.
        text = poly[0] if isinstance(poly, list) and poly else poly
        nums = [float(v) for v in str(text).split()]
        ring = [(nums[i + 1], nums[i]) for i in range(0, len(nums) - 1, 2)]  # CMR writes lat lon
        if len(ring) >= 3:
            rings.append(ring)
    if rings:
        return rings
    for box in entry.get("boxes") or []:
        s, w, n, e = [float(v) for v in str(box).split()]
        rings.append([(w, s), (e, s), (e, n), (w, n), (w, s)])
    return rings


def bbox_of(scenes):
    xs, ys = [], []
    for sc in scenes:
        for ring in sc["rings"]:
            for x, y in ring:
                xs.append(x)
                ys.append(y)
    if not xs:
        return None
    return min(xs), min(ys), max(xs), max(ys)
