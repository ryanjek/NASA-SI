# Earth dataset acquisition

Run commands from the repository root with Python 3.12+. This follows the
mars-map branch's catalog, local cache and provenance approach. The downloader
is independent of the Mars CLI so both branches can be integrated later.

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements-earth.txt
.\.venv\Scripts\python scripts/earth_data.py catalog
.\.venv\Scripts\python scripts/earth_data.py discover
.\.venv\Scripts\python scripts/earth_data.py download --limit 1
.\.venv\Scripts\python scripts/earth_data.py download
```

Discovery uses public catalogs and needs no login. NASA downloads use
earthaccess.login(), including its normal interactive login or existing
environment/netrc credentials. Keep credentials outside the repository.
Public Copernicus and CEDA files can download without the earthaccess package.

Use `--datasets terrain permafrost` (or any names printed by catalog) to select
sources in either command. Use `--bbox WEST SOUTH EAST NORTH` during discovery
to test a smaller area. A discovery failure is recorded per dataset and the
other sources are still attempted. Rerun discovery when connectivity returns.
Partial discovery replaces only selected datasets and preserves other entries
when the year and bounding box match. Full discovery rebuilds the inventory. Downloads write
one receipt per file, check hashes before reuse, stage transfers atomically,
reject HTML login responses, and retry failed transfers. An incomplete inventory
can be downloaded, but missing requested datasets produce a nonzero exit code.

## Scope and scientific choices

### Recovering the initial download

Seven successful sample files can be reused. Snow uses CMR version `61` (the DOI
uses `.061`). Refresh just snow without repeating the terrain catalog query:

```powershell
python scripts/earth_data.py discover --datasets snow
python scripts/earth_data.py download --datasets snow dust precipitation --limit 1
```

For `Eula Acceptance Failure`, sign in at https://urs.earthdata.nasa.gov/.
Go to Applications > Authorized Apps > APPROVE MORE APPLICATIONS, search for
`NASA GESDISC DATA ARCHIVE`, review the agreement and authorize access.
Then rerun the download. Credentials should only be entered in NASA's website
or the interactive earthaccess prompt. Successful files are reused on reruns.

`--limit 1` is a sample check, not a full acquisition. After it succeeds, use
`python scripts/earth_data.py download` for the full acquisition. Spatial
masking and 9 km aggregation are still pending.

Albedo uses four MCD43C3 snapshots per year, on January 15, April 15, July 15,
and October 15. These sample the middle months of the Northern Hemisphere
meteorological seasons. Each product uses a 16-day weighted retrieval window,
so treat these as seasonal snapshots. Check retrieval quality and snow cover
before comparing surface reflectivity across sites. The download command also
applies this selection to older inventories that list every day.

Authorization instructions: https://urs.earthdata.nasa.gov/documentation/for_users/how_to_preauth_app

- Analysis target is 9,000 m cells in EPSG:6933. The current script acquires raw
  inputs. It does not create a grid or calculate features.
- Study region is mainland Canada, Alaska, contiguous USA and Mexico, excluding
  Greenland and Canadian Arctic islands. The default bbox is an acquisition
  envelope. Global products and edge tiles include areas outside this region.
  A mainland polygon mask must be applied before feature extraction or scoring.
- Temporal products request 2025. Permafrost fraction uses verified CCI v5 2023.
  Elevation uses the static Copernicus GLO-90 2021 release. These dates remain
  explicit in the inventory and receipts.
- CMR discovery reports missing months or missing scheduled dates. An unavailable 2025
  annual product is reported as incomplete. There is no automatic year fallback.
- MOD11C3/MYD11C3 monthly means support seasonal comparisons. Daily extremes
  require daily products in a later processing stage. Clear-sky sampling and
  QA flags must be handled during processing.
- Precipitation uses NASA MERRA-2 M2TMNXLND for this first downloader, replacing
  the proposed TerraClimate input. Verify precipitation variable semantics and
  convert rates to monthly totals using seconds per month during extraction.
  MERRA-2 precipitation and dust are 0.5 by 0.625 degrees. Their effective
  resolution remains coarse when assigned to 9 km cells.
- Permafrost fraction is modeled frozen-ground coverage. Ground ice abundance
  needs separate evidence. Its coverage gaps must remain missing. Ground
  temperature and active-layer thickness are future additions, not downloaded
  by the permafrost source in this script.
- HLS refinement and geology/access/protected-area annotations are downstream
  tasks. The current source set includes NDVI, land-cover fractions and albedo
  instead of downloading all high-resolution reflectance scenes.

## Sources

- Challenge resources: https://www.spaceappschallenge.org/2026/challenges/permanent-moon-base-locations-and-mars/?tab=resources
- Terrain: https://registry.opendata.aws/copernicus-dem/
- Permafrost: https://catalogue.ceda.ac.uk/uuid/d235665772ec4b558e9a89ac85595e71/
- NASA catalog API: https://cmr.earthdata.nasa.gov/search/site/docs/search/api.html
- Temperature: https://doi.org/10.5067/MODIS/MOD11C3.061 and https://doi.org/10.5067/MODIS/MYD11C3.061
- Vegetation: https://doi.org/10.5067/MODIS/MOD13C2.061
- Land cover: https://doi.org/10.5067/MODIS/MCD12C1.061
- Snow: https://doi.org/10.5067/MODIS/MOD10CM.061
- Albedo: https://doi.org/10.5067/MODIS/MCD43C3.061
- MERRA-2: https://gmao.gsfc.nasa.gov/reanalysis/MERRA-2/
- Authentication/download API: https://earthaccess.readthedocs.io/en/stable/api/
