"""Command-line interface for the beginner-friendly Arcadia workflow."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .catalog import ASSETS, assets_for, dataset_names
from .download import download_assets
from .model import ScreeningProfile
from .raster import raster_summary


def _catalog_command(_args: argparse.Namespace) -> int:
    for asset in ASSETS:
        size = (
            f"{asset.expected_bytes / 1_048_576:.1f} MiB"
            if asset.expected_bytes
            else "unknown size"
        )
        print(f"{asset.dataset:14} {size:>12}  {asset.filename}")
        print(f"{'':28}{asset.description}")
        print(f"{'':28}{asset.url}")
    return 0


def _download_command(args: argparse.Namespace) -> int:
    try:
        selected = assets_for(args.datasets)
    except ValueError as error:
        raise SystemExit(str(error)) from error
    download_assets(
        selected,
        args.destination,
        overwrite=args.overwrite,
    )
    datasets = sorted({asset.dataset for asset in selected})
    print(f"Wrote provenance manifest(s) for: {', '.join(datasets)}")
    return 0


def _inspect_command(args: argparse.Namespace) -> int:
    summaries = []
    for path in args.paths:
        try:
            summaries.append(raster_summary(path))
        except Exception as error:
            summaries.append({"path": str(path), "error": f"{type(error).__name__}: {error}"})
    print(json.dumps(summaries, indent=2))
    return 1 if any("error" in item for item in summaries) else 0


def _screen_command(args: argparse.Namespace) -> int:
    from .screening import run_screening

    profile = ScreeningProfile(
        flat_slope_deg=args.flat_slope,
        max_abs_latitude_deg=args.max_latitude,
        min_confidence=args.min_coverage,
    )
    try:
        run_screening(
            args.raw,
            args.out,
            profile=profile,
            cell_pixels=args.cell_pixels,
            top=args.top,
            min_separation_km=args.min_separation_km,
        )
    except FileNotFoundError as error:
        raise SystemExit(str(error)) from error
    return 0


def _export_web_command(args: argparse.Namespace) -> int:
    from .webexport import export_web

    try:
        export_web(args.raw, args.out, cell_pixels=args.cell_pixels)
    except FileNotFoundError as error:
        raise SystemExit(str(error)) from error
    return 0


def _serve_command(args: argparse.Namespace) -> int:
    import functools
    import http.server

    if not (args.directory / "data" / "arcadia.json").exists():
        raise SystemExit(f"No map data in {args.directory}/data. Run `nasa-si export-web` first.")
    handler = functools.partial(
        http.server.SimpleHTTPRequestHandler, directory=str(args.directory)
    )
    with http.server.ThreadingHTTPServer((args.host, args.port), handler) as server:
        print(f"Serving the Arcadia map at http://{args.host}:{args.port}/ (Ctrl+C to stop)")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nasa-si",
        description="Download and inspect the Phase 1 Arcadia screening datasets.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    catalog = subparsers.add_parser("catalog", help="List datasets and source URLs")
    catalog.set_defaults(handler=_catalog_command)

    download = subparsers.add_parser(
        "download",
        help="Download raw rasters and write a provenance manifest",
    )
    choices = ("core", *dataset_names())
    download.add_argument(
        "datasets",
        nargs="*",
        default=["core"],
        metavar="DATASET",
        help=f"one or more of: {', '.join(choices)} (default: core)",
    )
    download.add_argument(
        "--destination",
        type=Path,
        default=Path("data/raw"),
        help="raw-data directory (default: data/raw)",
    )
    download.add_argument("--overwrite", action="store_true")
    download.set_defaults(handler=_download_command)

    inspect = subparsers.add_parser(
        "inspect",
        help="Print projection, resolution, NoData, and sampled value statistics",
    )
    inspect.add_argument("paths", nargs="+", type=Path)
    inspect.set_defaults(handler=_inspect_command)

    screen = subparsers.add_parser(
        "screen",
        help="Align MOLA, SWIM, and TES DCI and rank Arcadia candidate cells",
    )
    screen.add_argument("--raw", type=Path, default=Path("data/raw"))
    screen.add_argument("--out", type=Path, default=Path("data/processed"))
    screen.add_argument(
        "--cell-pixels",
        type=int,
        default=10,
        help="candidate cell size in 1/64 degree MOLA pixels (default: 10, ~9 km)",
    )
    screen.add_argument(
        "--flat-slope",
        type=float,
        default=ScreeningProfile.flat_slope_deg,
        help="slope in degrees, on ~1 km MOLA pixels, counted as flat (default: 5)",
    )
    screen.add_argument(
        "--max-latitude",
        type=float,
        default=ScreeningProfile.max_abs_latitude_deg,
        help="hard latitude gate in degrees (default: 50)",
    )
    screen.add_argument(
        "--min-coverage",
        type=float,
        default=ScreeningProfile.min_confidence,
        help="minimum fraction of valid pixels per cell (default: 0.8)",
    )
    screen.add_argument("--top", type=int, default=10, help="distinct candidates to report")
    screen.add_argument(
        "--min-separation-km",
        type=float,
        default=150.0,
        help="minimum distance between reported candidates (default: 150)",
    )
    screen.set_defaults(handler=_screen_command)

    export = subparsers.add_parser(
        "export-web",
        help="Write per-cell data and a hillshade for the interactive map",
    )
    export.add_argument("--raw", type=Path, default=Path("data/raw"))
    export.add_argument("--out", type=Path, default=Path("web/data"))
    export.add_argument("--cell-pixels", type=int, default=10)
    export.set_defaults(handler=_export_web_command)

    serve = subparsers.add_parser("serve", help="Serve the interactive map locally")
    serve.add_argument("--directory", type=Path, default=Path("web"))
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.set_defaults(handler=_serve_command)

    return parser


def main() -> None:
    args = build_parser().parse_args()
    raise SystemExit(args.handler(args))


if __name__ == "__main__":
    main()
