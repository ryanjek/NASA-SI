"""Small, explicit catalog for the Phase 1 Arcadia datasets."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Asset:
    dataset: str
    filename: str
    url: str
    description: str
    expected_bytes: int | None = None


MOLA_BASE = (
    "https://pds-geosciences.wustl.edu/mgs/"
    "urn-nasa-pds-mgs_mola_topography_derived/meg064"
)
SWIM_BASE = "https://swim.psi.edu/output/PilotStudy/Arcadia"
DCI_BASE = "http://www.mars.asu.edu/~ruff/DCI"


ASSETS: tuple[Asset, ...] = (
    Asset(
        "mola",
        "megt90n000gb.img",
        f"{MOLA_BASE}/megt90n000gb.img",
        "MOLA 64 ppd northern topography, 0-180 degrees east",
        132_710_400,
    ),
    Asset(
        "mola",
        "megt90n000gb.xml",
        f"{MOLA_BASE}/megt90n000gb.xml",
        "PDS4 label for the 0-180 degree MOLA raster",
        15_519,
    ),
    Asset(
        "mola",
        "megt90n180gb.img",
        f"{MOLA_BASE}/megt90n180gb.img",
        "MOLA 64 ppd northern topography, 180-360 degrees east",
        132_710_400,
    ),
    Asset(
        "mola",
        "megt90n180gb.xml",
        f"{MOLA_BASE}/megt90n180gb.xml",
        "PDS4 label for the 180-360 degree MOLA raster",
        15_509,
    ),
    Asset(
        "swim",
        "swim_arcadia_ice_consistency.tif",
        f"{SWIM_BASE}/Composite/Ci.tif",
        "SWIM Arcadia combined subsurface-ice consistency",
        2_625_059,
    ),
    Asset(
        "swim-evidence",
        "swim_arcadia_geomorphology.tif",
        f"{SWIM_BASE}/Datasets/Geomorph.tif",
        "SWIM geomorphology ice evidence",
        2_625_027,
    ),
    Asset(
        "swim-evidence",
        "swim_arcadia_radar_power.tif",
        f"{SWIM_BASE}/Datasets/Rad_Power.tif",
        "SWIM SHARAD surface-power ice evidence",
        2_625_061,
    ),
    Asset(
        "swim-evidence",
        "swim_arcadia_thermal.tif",
        f"{SWIM_BASE}/Datasets/Thermal.tif",
        "SWIM thermal ice evidence",
        2_625_029,
    ),
    Asset(
        "swim-evidence",
        "swim_arcadia_neutron.tif",
        f"{SWIM_BASE}/Datasets/Wdn.tif",
        "SWIM neutron-derived lower-layer water-equivalent hydrogen",
        2_625_043,
    ),
    Asset(
        "swim-evidence",
        "swim_arcadia_dielectric.tif",
        f"{SWIM_BASE}/Datasets/Dielectric.tif",
        "SWIM radar dielectric ice evidence",
        2_625_043,
    ),
    Asset(
        "swim-evidence",
        "swim_arcadia_reflectors.tif",
        f"{SWIM_BASE}/Datasets/Reflectors.tif",
        "SWIM mapped shallow radar reflectors",
        396_363,
    ),
    Asset(
        "dust",
        "tes_dust_cover_index.pds",
        f"{DCI_BASE}/dci_lo_ice_dust_16ppd_shifted.pds",
        "TES numeric global Dust Cover Index, 16 pixels per degree",
        66_356_224,
    ),
)


def dataset_names() -> tuple[str, ...]:
    return tuple(sorted({asset.dataset for asset in ASSETS}))


def assets_for(names: list[str] | tuple[str, ...]) -> tuple[Asset, ...]:
    """Return assets for dataset names, expanding the convenient ``core`` group."""
    expanded = set(names)
    if "core" in expanded:
        expanded.remove("core")
        expanded.update(("mola", "swim", "dust"))

    unknown = expanded.difference(dataset_names())
    if unknown:
        choices = ", ".join(("core", *dataset_names()))
        raise ValueError(f"Unknown dataset(s): {', '.join(sorted(unknown))}. Choose: {choices}")

    return tuple(asset for asset in ASSETS if asset.dataset in expanded)
