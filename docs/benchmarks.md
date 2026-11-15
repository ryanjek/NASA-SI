# Benchmarks: known analog sites

File: `config/benchmarks.csv` · Loader: `src/nasa_si/benchmarks.py` · Summary: `python -m nasa_si.benchmarks`

Eleven sites NASA and others already use, kept as **benchmarks**, not training labels:
positive controls for validation, calibration of what "analog-like" looks like in feature
space, and anchors for clustering Earth cells.

| role | meaning | count |
|---|---|---|
| `canonical` | named in the challenge brief (Antarctica, Atacama, Iceland, Hawaii, Haughton, Death Valley) | 6 |
| `hypothesis` | proposed by the analog report, to be tested, not assumed (Puna/Socompa, Qaidam, Dallol, Craters of the Moon, Khaybar) | 5 |

## Columns that keep the file honest
- `evidence`: `nasa_or_agency` > `peer_reviewed` > `secondary` > `report_only`.
- `coord_confidence`: `high` / `medium` / `low`. A wrong point means features are computed at the
  wrong place, so a verified row may not be low.
- `verified`: a person has checked the rationale and coordinates against the cited source.
  A verified row must have web sources and no "from memory" coordinates (a test enforces this).
  9 of 11 rows are marked verified. Yungay and Death Valley are not: their coordinates have no
  checked source yet.
- `body`: `mars`, `moon` or `both`. Use `for_body(benchmarks, "mars")` for Mars validation; the two
  lunar-tube sites (Craters of the Moon, Khaybar) stay out of it.

## Choices made by me that you should review
- **Radius** is the site's extent, not a precise boundary: Dry Valleys 40 km (ice-free area
  estimates range 2,000-15,000 km2), Haughton 12 km (covers the 23 km apparent crater), Craters
  of the Moon 22 km, small field sites 10 km. A 9 km cell cannot resolve a field station, so
  small sites cover only 3-5 cells.
- **Qaidam** is a sub-area (western yardang and Dalangtan playa terrain); the whole basin is far
  too large to be one benchmark. The report's eastern point was corrected.
- **Puna**: the report's longitude (67.2 W) was corrected to Laguna Socompa (68.21 W).
- **Khaybar**: the Moon/Mars analog claim rests only on the report, so `evidence` stays
  `report_only` and the site stays a hypothesis. Its cited paper (Stewart et al. 2024) is
  archaeology; it does not propose a lunar-habitat analog.
- The report's own scores (an "AS-Score" per site) are not used: they are not derived from data.

## Source check (done) and what is still open
Every `rationale_source` and `coords_source` was re-checked (17 distinct links and DOIs; all resolve
and the DOI titles match their claims). Fixes made:
- Dead links replaced (NASA pages for Askja, HI-SEAS and Haughton; the NPS Death Valley and
  Craters of the Moon pages; the LTER page for the Dry Valleys).
- Yungay's wrong DOI (a prebiotic-chemistry paper) replaced with McKay et al. 2003.
- Where no NASA page exists, evidence was downgraded honestly: Askja is now `secondary`,
  Haughton `peer_reviewed` (Osinski et al. 2005).
- Khaybar's coordinates now match the cited paper (25.5888 N, 39.7570 E); the paper is
  archaeology, so the lunar-analog claim stays `report_only`.

Still open:
- **Yungay** (`verified: false`): McKay et al. 2003 put the research station near 24.08 S,
  69.99 W, about 6 km west of the point used. The 10 km radius covers both, but a person should
  confirm the point.
- **Death Valley** (`verified: false`): the NPS page names Mars Hill and Mesquite Flat as test
  sites but gives no coordinates.
- Coordinates for the other rows were compared with Wikipedia and the cited papers where they
  give any, not surveyed independently.

## To verify a row
Open `rationale_source` and `coords_source`, confirm the site really is used for the stated
reason and that the point falls inside the site, fix the row if not, then set `verified` to
`true`. The loader rejects bad values, duplicate ids and out-of-range coordinates.

## Not done yet
No feature values are computed at these sites yet (that needs the Earth feature pipeline on the
equal-area grid), so there is no validation, novelty score or clustering yet.
