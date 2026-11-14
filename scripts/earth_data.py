"""Discover and download Earth inputs. Run with Python 3.12 or newer."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

CMR = "https://cmr.earthdata.nasa.gov/search/granules.json"
DEM = "https://copernicus-dem-90m.s3.eu-central-1.amazonaws.com"
NASA_PRODUCTS = {
    "temperature-terra": ("MOD11C3", "061", "monthly"),
    "temperature-aqua": ("MYD11C3", "061", "monthly"),
    "vegetation": ("MOD13C2", "061", "monthly"),
    "land-cover": ("MCD12C1", "061", "annual"),
    # NSIDC indexes this collection as 61, although its DOI ends in .061.
    "snow": ("MOD10CM", "61", "monthly"),
    "albedo": ("MCD43C3", "061", "seasonal"),
    "dust": ("M2TMNXAER", "5.12.4", "monthly"),
    "precipitation": ("M2TMNXLND", "5.12.4", "monthly"),
}
NAMES = ("terrain", "permafrost", *NASA_PRODUCTS)
DEFAULT_BBOX = (-170.0, 14.0, -52.0, 72.0)
ALBEDO_MONTH_DAYS = ("01-15", "04-15", "07-15", "10-15")


def seasonal_albedo_assets(assets: list[dict], year: int) -> tuple[list[dict], list[str]]:
    """Keep one MCD43C3 granule per target date, preferring newer production stamps."""
    dates = {f"{year}-{month_day}" for month_day in ALBEDO_MONTH_DAYS}
    selected = {}
    for asset in sorted(assets, key=lambda asset: asset["filename"]):
        date = (asset.get("start") or "")[:10]
        if date in dates:
            selected[date] = asset
    return [selected[date] for date in sorted(selected)], sorted(dates - selected.keys())


def request_bytes(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "NASA-SI-Earth/0.1"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                     delete=False, suffix=".tmp") as stream:
        temporary = Path(stream.name)
        json.dump(value, stream, indent=2)
        stream.write("\n")
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def safe_name(value: str) -> str:
    if not value or value in (".", "..") or any(c in value for c in '/\\:'):
        raise ValueError(f"Unsafe catalog filename: {value!r}")
    return value


def cmr_assets(name: str, year: int, bbox: tuple) -> tuple[list, dict]:
    short_name, version, cadence = NASA_PRODUCTS[name]
    assets = {}
    starts = set()
    page = 1
    while True:
        query = urllib.parse.urlencode({
            "short_name": short_name, "version": version,
            "temporal": f"{year}-01-01T00:00:00Z,{year}-12-31T23:59:59Z",
            "bounding_box": ",".join(map(str, bbox)),
            "page_size": 2000, "page_num": page,
        })
        entries = json.loads(request_bytes(f"{CMR}?{query}"))["feed"]["entry"]
        if not entries:
            break
        for entry in entries:
            start = entry.get("time_start", "")
            if not start.startswith(str(year)):
                continue
            links = [link["href"] for link in entry.get("links", [])
                     if link.get("rel", "").endswith("/data#")
                     and not link.get("inherited")
                     and link.get("href", "").startswith("https://")
                     and urllib.parse.urlparse(link["href"]).path.lower().endswith(
                         (".hdf", ".h5", ".nc", ".nc4", ".tif"))]
            if not links:
                continue
            # Prefer cloud HTTPS files when both archive and cloud copies are indexed.
            url = sorted(links, key=lambda u: ("earthdatacloud" not in u, u))[0]
            filename = safe_name(urllib.parse.unquote(Path(urllib.parse.urlparse(url).path).name))
            assets[filename] = {
                "dataset": name, "filename": filename, "url": url,
                "year": year, "start": start, "end": entry.get("time_end"),
                "concept_id": entry["id"], "provider": entry.get("data_center"),
                "authentication": "earthdata", "product": short_name, "version": version,
            }
            starts.add(start[:10])
        page += 1
    expected = {f"{year}-{month:02d}" for month in range(1, 13)}
    observed = {start[:7] for start in starts}
    missing = sorted(expected - observed)
    if cadence == "seasonal":
        selected, missing_dates = seasonal_albedo_assets(list(assets.values()), year)
        return selected, {
            "cadence": cadence,
            "available_dates": [asset["start"][:10] for asset in selected],
            "missing_dates": missing_dates,
            "complete": not missing_dates,
            "files": len(selected),
        }
    complete = bool(assets) and (cadence == "annual" or not missing)
    return list(assets.values()), {
        "cadence": cadence, "available_dates": sorted(starts),
        "missing_months": [] if cadence == "annual" else missing,
        "complete": complete, "files": len(assets),
    }


def terrain_assets(bbox: tuple) -> tuple[list, dict]:
    """List existing COG tiles and select those intersecting the download envelope."""
    west, south, east, north = bbox
    token = None
    assets = []
    ns = {"s": "http://s3.amazonaws.com/doc/2006-03-01/"}
    while True:
        params = {"list-type": "2"}
        if token:
            params["continuation-token"] = token
        root = ET.fromstring(request_bytes(f"{DEM}/?{urllib.parse.urlencode(params)}"))
        for item in root.findall("s:Contents", ns):
            key = item.findtext("s:Key", namespaces=ns)
            match = re.search(r"_([NS])(\d{2})_00_([EW])(\d{3})_00_DEM\.tif$", key)
            if not match:
                continue
            lat = int(match[2]) * (1 if match[1] == "N" else -1)
            lon = int(match[4]) * (1 if match[3] == "E" else -1)
            if lon < east and lon + 1 > west and lat < north and lat + 1 > south:
                assets.append({"dataset": "terrain", "filename": key.split("/")[-1],
                               "url": f"{DEM}/{key}", "authentication": "none",
                               "expected_bytes": int(item.findtext("s:Size", namespaces=ns)),
                               "year": None, "product": "Copernicus GLO-90 2021 release"})
        token = root.findtext("s:NextContinuationToken", namespaces=ns)
        if not token:
            break
    return assets, {"files": len(assets), "complete": bool(assets)}


def permafrost_assets(year: int) -> tuple[list, dict]:
    base = ("https://dap.ceda.ac.uk/neodc/esacci/permafrost/data/"
            "permafrost_extent/L4/area4/pp/v05.0/northern_hemisphere")
    filename = f"ESACCI-PERMAFROST-L4-PFR-MODISLST_CRYOGRID-AREA4_PP-{year}-fv05.0.nc"
    url = f"{base}/{year}/{filename}"
    request = urllib.request.Request(url, method="HEAD")
    with urllib.request.urlopen(request, timeout=30) as response:
        size = response.headers.get("Content-Length")
    return [{"dataset": "permafrost", "filename": filename, "url": url,
             "year": year, "product": "ESA Permafrost CCI PFR v5.0",
             "authentication": "none", "expected_bytes": int(size) if size else None}], {
                 "files": 1, "complete": True,
                 "coverage_note": "Northern hemisphere product; outside coverage is unknown, not zero.",
             }


def discover(args: argparse.Namespace) -> int:
    inventory = {
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "year": args.year, "permafrost_year": args.permafrost_year,
        "grid_cell_size_m": 9000, "grid_crs": "EPSG:6933",
        "download_bbox": args.bbox,
        "study_area": "Mainland Canada, Alaska, contiguous USA, Mexico",
        "exclude": ["Greenland", "Canadian Arctic islands"],
        "spatial_status": "Raw envelope/global files; mainland clipping and grid aggregation pending.",
        "datasets": {}, "assets": [],
    }
    if args.manifest.exists() and set(args.datasets) != set(NAMES):
        previous = json.loads(args.manifest.read_text(encoding="utf-8"))
        if (previous.get("year") != args.year
                or previous.get("permafrost_year") != args.permafrost_year
                or list(previous.get("download_bbox", [])) != list(args.bbox)):
            raise ValueError("Partial discovery settings differ from the existing inventory. "
                             "Use a separate --manifest or rediscover all datasets.")
        inventory["datasets"] = {k: v for k, v in previous["datasets"].items()
                                 if k not in args.datasets}
        inventory["assets"] = [a for a in previous["assets"]
                               if a["dataset"] not in args.datasets]
    for name in args.datasets:
        print(f"Discovering {name} ...", flush=True)
        try:
            if name == "terrain":
                assets, coverage = terrain_assets(args.bbox)
            elif name == "permafrost":
                assets, coverage = permafrost_assets(args.permafrost_year)
            else:
                assets, coverage = cmr_assets(name, args.year, args.bbox)
            inventory["assets"].extend(assets)
            inventory["datasets"][name] = coverage
            print(f"  {len(assets)} files; complete={coverage['complete']}")
        except Exception as error:
            inventory["datasets"][name] = {"complete": False, "error": str(error)}
            print(f"  unavailable: {error}", file=sys.stderr)
        write_json(args.manifest, inventory)
    return 0 if all(d["complete"] for d in inventory["datasets"].values()) else 1


def validate_payload(path: Path) -> None:
    with path.open("rb") as stream:
        magic = stream.read(8)
    valid = (magic.startswith(b"CDF") or magic.startswith(b"\x89HDF\r\n\x1a\n")
             or magic.startswith(b"\x0e\x03\x13\x01")
             or magic[:4] in (b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+"))
    if not valid:
        raise ValueError(f"Expected NetCDF/HDF/GeoTIFF, received invalid payload for {path.name}")


def download_one(asset: dict, destination: Path, nasa=None) -> Path:
    """Stage, verify and atomically publish a file; verified cache entries are reused."""
    directory = destination / safe_name(asset["dataset"])
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / safe_name(asset["filename"])
    receipt = target.with_suffix(target.suffix + ".json")
    if target.exists() and receipt.exists():
        old = json.loads(receipt.read_text(encoding="utf-8"))
        if old["url"] == asset["url"] and old["sha256"] == digest(target):
            return target
    with tempfile.TemporaryDirectory(dir=directory, prefix="staging-") as stage:
        partial = Path(stage) / target.name
        if asset["authentication"] == "earthdata":
            paths = nasa.download([asset["url"]], local_path=stage,
                                  provider=asset.get("provider"), threads=1)
            if len(paths) != 1:
                raise RuntimeError("Earthdata did not return exactly one downloaded file")
            partial = Path(paths[0])
        else:
            request = urllib.request.Request(asset["url"], headers={"User-Agent": "NASA-SI-Earth/0.1"})
            with urllib.request.urlopen(request, timeout=60) as response, partial.open("wb") as stream:
                expected = response.headers.get("Content-Length")
                while chunk := response.read(1024 * 1024):
                    stream.write(chunk)
            if expected and partial.stat().st_size != int(expected):
                raise RuntimeError("Incomplete HTTP download")
        validate_payload(partial)
        expected = asset.get("expected_bytes")
        if expected is not None and partial.stat().st_size != expected:
            raise RuntimeError("Downloaded size differs from catalog")
        record = {**asset, "sha256": digest(partial), "actual_bytes": partial.stat().st_size,
                  "downloaded_utc": datetime.now(timezone.utc).isoformat()}
        os.replace(partial, target)
        write_json(receipt, record)
    return target


def download(args: argparse.Namespace) -> int:
    inventory = json.loads(args.manifest.read_text(encoding="utf-8"))
    assets = [a for a in inventory["assets"] if a["dataset"] in args.datasets]
    missing_dates = []
    if "albedo" in args.datasets:
        albedo, missing_dates = seasonal_albedo_assets(
            [a for a in assets if a["dataset"] == "albedo"], inventory["year"]
        )
        assets = [a for a in assets if a["dataset"] != "albedo"] + albedo
        if missing_dates:
            print(f"Missing albedo dates: {', '.join(missing_dates)}; "
                  "rerun discover --datasets albedo.", file=sys.stderr)
    if args.limit:
        assets = [a for name in args.datasets
                  for a in [a for a in assets if a["dataset"] == name][:args.limit]]
    missing = set(args.datasets) - {a["dataset"] for a in assets}
    for name in sorted(missing):
        print(f"No discovered assets for {name}; rerun discover.", file=sys.stderr)
    nasa = None
    auth_error = None
    if any(a["authentication"] == "earthdata" for a in assets):
        try:
            import earthaccess
            nasa = earthaccess
            auth = nasa.login()
            if not auth.authenticated:
                raise RuntimeError("Earthdata authentication was unsuccessful")
        except Exception as error:
            auth_error = str(error)
            print(f"NASA download unavailable: {error}", file=sys.stderr)
    failures = len(missing) + len(missing_dates)
    for asset in assets:
        if asset["authentication"] == "earthdata" and auth_error:
            failures += 1
            continue
        for attempt in range(3):
            try:
                path = download_one(asset, args.destination, nasa)
                print(f"Ready {path}", flush=True)
                break
            except Exception as error:
                if "eula" in str(error).lower():
                    print(f"Blocked {asset['filename']}: NASA requires account authorization. "
                          "Sign in at https://urs.earthdata.nasa.gov/ and open Applications > "
                          "Authorized Apps > APPROVE MORE APPLICATIONS. For MERRA-2, "
                          "search NASA GESDISC DATA ARCHIVE, review the agreement and authorize. "
                          "Then rerun this download command. "
                          f"Original error: {error}", file=sys.stderr)
                    failures += 1
                    break
                if attempt == 2:
                    print(f"Failed {asset['filename']}: {error}", file=sys.stderr)
                    failures += 1
                else:
                    time.sleep(2 ** attempt)
    return int(failures > 0)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("catalog", help="Print supported source products")
    for command in ("discover", "download"):
        sub = commands.add_parser(command)
        sub.add_argument("--datasets", nargs="+", choices=NAMES, default=list(NAMES))
        sub.add_argument("--manifest", type=Path, default=Path("data/earth/inventory.json"))
        if command == "discover":
            sub.add_argument("--year", type=int, default=2025)
            sub.add_argument("--permafrost-year", type=int, choices=range(2003, 2024), default=2023)
            sub.add_argument("--bbox", type=float, nargs=4, default=DEFAULT_BBOX,
                             metavar=("WEST", "SOUTH", "EAST", "NORTH"))
        else:
            sub.add_argument("--destination", type=Path, default=Path("data/earth/raw"))
            sub.add_argument("--limit", type=int, help="Download this many files per dataset for a smoke test")
    args = parser.parse_args()
    if args.command == "catalog":
        print("terrain: Copernicus GLO-90 (static 2021 release)")
        print("permafrost: ESA CCI permafrost fraction v5 (2023)")
        for name, spec in NASA_PRODUCTS.items():
            print(f"{name}: {spec[0]} v{spec[1]} ({spec[2]})")
        return 0
    if args.command == "discover":
        west, south, east, north = args.bbox
        if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
            parser.error("bbox must be WEST SOUTH EAST NORTH without antimeridian crossing")
        return discover(args)
    if args.limit is not None and args.limit < 1:
        parser.error("limit must be positive")
    return download(args)


if __name__ == "__main__":
    raise SystemExit(main())
