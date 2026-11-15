"""Look up human-readable place names for stage 2 cells and cache them.

    python3 scripts/geocode_candidates.py web/src/data/candidates.json data/processed/geocode_cache.json [seed ...]

Sources (OpenStreetMap data, ODbL; credited in the front end):
  * Overpass `is_in`: the island / archipelago / peninsula / desert / plateau / valley / basin area that contains the point.
  * Nominatim reverse (zoom 10): the most specific administrative names (town, county, region, state, country).
Only the cell coordinates are sent. Requests are paced (Nominatim 1/s policy) and retried on 429/5xx.
The cache is keyed by coordinates rounded to 0.05 deg (~5 km), so neighbouring cells share a lookup.
Antarctic cells (<= 60 S) are skipped: the front end never shows them.
"""
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

UA = "NASA-SI/0.1 (educational analog-site prototype)"
OVERPASS = ("https://overpass.kumi.systems/api/interpreter", "https://overpass-api.de/api/interpreter")
MAX_RANK = int(__import__("os").environ.get("MAX_RANK", "1000"))  # only look up cells up to this rank per seed
TOP_FOR_NATURAL = int(__import__("os").environ.get("TOP_FOR_NATURAL", "10"))  # island/natural names only for each seed's top cells (Overpass is slow)
NATURAL = r"island|islet|archipelago|peninsula|desert|plateau|valley|basin|mountain_range|volcano"


def get(url: str, data: bytes | None = None, tries: int = 3):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, data=data, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode())
        except Exception as e:  # noqa: BLE001 - retry any network/HTTP error, then give up
            wait = 2 * (i + 1)
            print(f"  retry in {wait}s: {e}", flush=True)
            time.sleep(wait)
    return None


def natural_area(lat: float, lon: float):
    q = f'[out:json][timeout:25];is_in({lat},{lon})->.a;area.a[~"^(place|natural)$"~"{NATURAL}"];out tags;'
    body = urllib.parse.urlencode({"data": q}).encode()
    d = None
    for mirror in OVERPASS:  # the main server often answers 502 under load; fall back to a mirror
        d = get(mirror, body, tries=1)
        if d:
            break
    if not d:
        return None
    best = None
    for e in d.get("elements", []):
        t = e.get("tags", {})
        name = t.get("name:en") or t.get("name")
        kind = t.get("place") or t.get("natural")
        if not name:
            continue
        # Prefer the most specific feature: an island over its archipelago, etc.
        rank = {"islet": 0, "island": 1, "volcano": 1, "valley": 2, "peninsula": 3, "basin": 3, "plateau": 4, "desert": 4, "mountain_range": 5, "archipelago": 6}.get(kind, 7)
        if best is None or rank < best[0]:
            best = (rank, name, kind)
    return {"name": best[1], "kind": best[2]} if best else None


def admin(lat: float, lon: float):
    q = urllib.parse.urlencode({"format": "jsonv2", "lat": lat, "lon": lon, "zoom": 10, "accept-language": "en"})
    d = get(f"https://nominatim.openstreetmap.org/reverse?{q}")
    return (d or {}).get("address")


def main(candidates: str, cache_path: str, *seeds: str) -> None:
    cells = json.loads(Path(candidates).read_text())["candidates"]
    cache_file = Path(cache_path)
    cache = json.loads(cache_file.read_text()) if cache_file.exists() else {}
    todo, want_natural = [], set()
    for c in cells:
        if c["lat"] <= -60 or (seeds and c["seed"] not in seeds) or c["rank"] > MAX_RANK:
            continue
        key = f"{round(c['lat'] / 0.05) * 0.05:.2f},{round(c['lon'] / 0.05) * 0.05:.2f}"
        if c["rank"] <= TOP_FOR_NATURAL:
            want_natural.add(key)
        if key not in cache and key not in todo:
            todo.append(key)
    print(f"{len(todo)} new lookups ({len(cache)} cached)", flush=True)
    for i, key in enumerate(todo, 1):
        lat, lon = map(float, key.split(","))
        nat = natural_area(lat, lon) if key in want_natural else None
        adm = admin(lat, lon)
        time.sleep(1.1)
        cache[key] = {"natural": nat, "address": adm}
        print(f"[{i}/{len(todo)}] {key}: {(nat or {}).get('name')} | {(adm or {}).get('state')}, {(adm or {}).get('country')}", flush=True)
        if i % 10 == 0:
            cache_file.write_text(json.dumps(cache, indent=1))
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(cache, indent=1))
    print("done", flush=True)


if __name__ == "__main__":
    main(*sys.argv[1:])
