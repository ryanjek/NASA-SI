"""Reliable file downloads with a local provenance manifest."""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import asdict
from pathlib import Path

from .catalog import Asset

USER_AGENT = "NASA-SI/0.1 (NASA Space Apps educational project)"
CHUNK_SIZE = 1024 * 1024


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_asset(
    asset: Asset,
    destination: Path,
    *,
    overwrite: bool = False,
    progress: Callable[[str], None] = print,
) -> Path:
    """Download one asset atomically and return its local path."""
    dataset_dir = destination / asset.dataset
    dataset_dir.mkdir(parents=True, exist_ok=True)
    target = dataset_dir / asset.filename

    if target.exists() and not overwrite:
        if asset.expected_bytes is None or target.stat().st_size == asset.expected_bytes:
            progress(f"exists   {target}")
            return target
        raise RuntimeError(
            f"{target} exists but has {target.stat().st_size:,} bytes; "
            f"expected {asset.expected_bytes:,}. Use --overwrite."
        )

    partial = target.with_suffix(target.suffix + ".part")
    request = urllib.request.Request(asset.url, headers={"User-Agent": USER_AGENT})
    started = time.monotonic()
    downloaded = 0
    last_reported_pct = -10

    try:
        with urllib.request.urlopen(request, timeout=120) as response, partial.open("wb") as stream:
            total = response.headers.get("Content-Length")
            total_bytes = int(total) if total else asset.expected_bytes
            while chunk := response.read(CHUNK_SIZE):
                stream.write(chunk)
                downloaded += len(chunk)
                if total_bytes:
                    percent = int(downloaded * 100 / total_bytes)
                    if percent >= last_reported_pct + 10 or downloaded == total_bytes:
                        progress(
                            f"download {asset.filename}: "
                            f"{downloaded / 1_048_576:.1f}/"
                            f"{total_bytes / 1_048_576:.1f} MiB"
                        )
                        last_reported_pct = percent
        os.replace(partial, target)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise

    if asset.expected_bytes is not None and downloaded != asset.expected_bytes:
        target.unlink(missing_ok=True)
        raise RuntimeError(
            f"Downloaded {downloaded:,} bytes for {asset.filename}; "
            f"expected {asset.expected_bytes:,}."
        )

    progress(f"saved    {target} ({time.monotonic() - started:.1f}s)")
    return target


def download_assets(
    assets: Iterable[Asset],
    destination: Path,
    *,
    overwrite: bool = False,
    progress: Callable[[str], None] = print,
) -> list[Path]:
    """Download assets and write machine-readable provenance beside them."""
    assets = tuple(assets)
    paths = [
        download_asset(asset, destination, overwrite=overwrite, progress=progress)
        for asset in assets
    ]
    records = [
        {
            **asdict(asset),
            "local_path": str(path),
            "actual_bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        }
        for asset, path in zip(assets, paths, strict=True)
    ]
    for dataset in sorted({asset.dataset for asset in assets}):
        manifest = {
            "generated_at_unix": int(time.time()),
            "dataset": dataset,
            "assets": [record for record in records if record["dataset"] == dataset],
        }
        manifest_path = destination / dataset / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )
    return paths
