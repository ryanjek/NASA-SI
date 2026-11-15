"""Write the stage 1 prefilter survivors the front end needs.

    python3 scripts/build_survivors_json.py data/processed/prefilter_expanded_15seeds \
        ../NASA-SI/docs/handoff/mars_sites_for_frontend.csv

Inputs are read-only copies of the VM folder
/home/jishnuappana/NASA-SI/data/earth/processed/prefilter/expanded_15seeds/ (only run_summary.txt,
*/survivors.csv and */profile.json were copied) and the Mars-site handoff CSV.

Only seeds named in the CSV's `mapped_earth_seeds` column are written, so the bundle stays small.
Survivors are ~50 km HEALPix NESTED patches (nside 128) that passed the stage 1 gates. They are NOT ranked and
carry no similarity score; the front end must say so. Patches at or south of 60 S (Antarctica) are
left out on request; their count per seed is kept as `antarcticExcluded`. Patch centres are copied as-is (rounded to 0.01 deg).

Outputs:
    web/src/data/survivors_index.json   seed id -> name, seed lon/lat, survivor count (imported statically)
    web/src/data/survivors.json         seed id -> flat [lon, lat, lon, lat, ...] (loaded on demand)
"""
import csv
import json
import re
import sys
from pathlib import Path

ANTARCTIC_LAT = -60.0


def mapped_seeds(mars_csv: Path) -> set[str]:
    seeds: set[str] = set()
    with mars_csv.open(newline="") as f:
        for row in csv.DictReader(f):
            for part in (row.get("mapped_earth_seeds") or "").split(";"):
                m = re.match(r"\s*([a-z0-9_]+)", part)
                if m:
                    seeds.add(m.group(1))
    return seeds


def main(prefilter_dir: str, mars_csv: str, out_dir: str = "web/src/data") -> None:
    root = Path(prefilter_dir)
    wanted = mapped_seeds(Path(mars_csv))
    index, points = {}, {}
    for seed in sorted(wanted):
        surv = root / seed / "survivors.csv"
        prof = root / seed / "profile.json"
        if not surv.exists():
            print(f"skip {seed}: no stage 1 output")
            continue
        flat: list[float] = []
        dropped = 0
        with surv.open(newline="") as f:
            for row in csv.DictReader(f):
                lon, lat = float(row["lon"]), float(row["lat"])
                if lat <= ANTARCTIC_LAT:  # Antarctica is never suggested in the front end
                    dropped += 1
                    continue
                flat += [round(lon - 360 if lon > 180 else lon, 2), round(lat, 2)]
        info = json.loads(prof.read_text())["seed"] if prof.exists() else {}
        index[seed] = {"name": info.get("name", seed), "lon": info.get("lon"), "lat": info.get("lat"), "survivors": len(flat) // 2, "antarcticExcluded": dropped}
        points[seed] = flat
        print(f"{seed}: {len(flat) // 2} survivors")
    source = {"folder": "prefilter/expanded_15seeds", "files": "*/survivors.csv, */profile.json", "grid": "HEALPix NESTED nside 128 (~50 km)"}
    Path(out_dir, "survivors_index.json").write_text(json.dumps({"source": source, "seeds": index}, indent=1))
    Path(out_dir, "survivors.json").write_text(json.dumps(points, separators=(",", ":")))


if __name__ == "__main__":
    main(*sys.argv[1:])
