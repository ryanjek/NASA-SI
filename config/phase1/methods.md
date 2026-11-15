# Phase 1 methods

Phase 1 answers a narrower question than "which Earth places look like Mars." The question is which measurable characteristics make a known Earth site scientifically useful as a Mars analog, and which public datasets could be used later to look for those characteristics somewhere else.

This pass does not download imagery, compute embeddings, train a model, or run a similarity search. The Mars landing-site screen already stored in `data/processed/` is a different task and is not an Earth analog catalog.

## Evidence grades

Every process row in `data/phase1/processes.csv` uses one grade:

| Grade | Meaning |
| --- | --- |
| `peer_reviewed` | A journal paper opened in full or on its landing page supports the claim. |
| `conference_abstract` | The claim comes from an opened conference abstract, not a journal article. |
| `project_website` | The claim comes from a project page or encyclopedia entry. |
| `report` | The claim comes from an opened report or manuscript whose journal venue was not printed on the extracted pages. |
| `contested` | Opened sources disagree, or a later paper limits an earlier interpretation. Both sides are cited. |

A project website cannot be the only support for a geological process that is a search seed. The Devon Island canyon comparison is a website hypothesis and is not a seed. The HI-SEAS habitat site's comparison of local basalt with Curiosity's CheMin instrument is a website claim. This catalog does not use it. The mineralogy row uses the opened EMMIHS alteration abstract instead.

`references.csv` records whether the source was read as `full_text`, a `landing_page`, or was `not_opened`. A DOI is stored only when the DOI page or the paper itself was opened. Empty DOI fields are intentional.

## Coordinates

A point is stored only when an opened source states it. `sites.geojson` contains those points and no others. Sites without a published point have `coordinate_status=unlocated`, empty latitude and longitude, and no GeoJSON geometry.

`published_conflict` means a point is stored, and the coordinate note records a second published location that was not used. The stored point is the one that matches the geographic description in the same source, not a compromise between the two numbers.

- University Valley uses Marinova et al. (2011): 77°51.8′S, 160°43′E, in the Quartermain Mountains. Williams et al. (2018) print 77.868°S, 163.758°E. Those longitudes are about three degrees apart. The Quartermain value sits next to Beacon Valley. The other value is kept in the note.
- The Beacon Valley thermistor site uses Liu et al. (2018): 77°49′S, 160°39′E. Liu et al. (2015) give the valley as 77°49′S, 161°39′E. The 2018 study-site coordinate is the geometry.
- The Haughton center uses Patterson and Ernst (2020): 75°22′38″N, 89°40′48″W. The Earth Impact Database Haughton page did not load during this pass, so that database was not used as the coordinate source.
- Don Juan Pond is unlocated. Dickson et al. (2013) give 77.563°S, 161.256°E for a time-lapse camera on the Dais, not for the pond.

Elevations are published figures, not values read off a DEM. Where two sources differ by about 100 m, both are in the coordinate note.

## What is a search seed

`transferable_to_similarity_search` is `true` or `false`. `true` marks a studied patch whose described process is a candidate target for a later search. It does not mean the patch has already been scored.

These are not seeds:

- Parent regions. The Dry Valleys, Askja, the Mauna Loa flank, and the Haughton structure are context.
- Operations facilities. HI-SEAS, FMARS, and the Haughton-Mars Project Research Station record isolation, crew, and logistics. Those properties are not physical features of the ground.
- Contrasts and disturbances. Lower Taylor Valley, the pumice-rich northeast sand sheet, and the Holuhraun lava are stored so they are not quietly treated as the analog.
- Blood Falls. The opened 2009 paper describes a subglacial ferrous brine and compares it with organic-starved oceans. It is kept separate from the Phoenix dry-permafrost claim, and it is not a search seed.

## Features

`features.csv` has two roles.

`global_search` features are quantities a later phase could compute outside these four regions. Each one names the processes it can inform, the ground window to aggregate, the unit, and the way the proxy fails. None of them means "looks like Mars."

`reference_only` features are why a site is an analog and cannot be observed with the public global layers cataloged here. Ice-table depth, ice type, salt profiles, biomass, lava-flow morphology, lava-tube interiors, and hydrothermal mineral setting are in that group. They must not be filled by copying the reference site's value onto other places.

Climate layers can separate a no-thaw upper valley from a valley with a summer active layer. They do not measure the depth or the type of ground ice. EMIT mineral maps are limited to the ISS latitude band, about 52°N to 52°S, and are missing at Askja, the Dry Valleys, and Haughton. Missing EMIT stays missing. GLiM lithology is coarse, and it is included so a carbonate impact crater is not scored as if it were basalt.

## Tables

| File | One row is |
| --- | --- |
| `data/phase1/references.csv` | A source. |
| `data/phase1/sites.csv` | A region, studied patch, contrast, or facility. |
| `data/phase1/sites.geojson` | The subset of sites with a published point. |
| `data/phase1/processes.csv` | A claim tied to one site and one or more sources. |
| `data/phase1/features.csv` | A measurable quantity, either global or reference-only. |
| `data/phase1/datasets.csv` | A public dataset that was cataloged and not downloaded. |
| `data/phase1/feature_datasets.csv` | The dataset used to compute a feature. |

Identifiers join on `citation_id`, `site_id`, `process_id`, `feature_id`, and `dataset_id`. Repeated identifiers in one cell are separated by `|`.

`scripts/validate_phase1.py` checks those joins, requires a citation on every process, rejects an operations site marked as a search seed, requires every global-search feature to name a dataset that extends beyond one reference site, and rejects a GeoJSON geometry for an unlocated site.

## Column dictionary

`sites.csv`: `site_id`, `parent_site_id`, `name`, `analog_family`, `site_role`, `latitude`, `longitude`, `coordinate_status`, `coordinate_precision`, `elevation_m`, `buffer_m`, `transferable_to_similarity_search`, `citation_ids`, `coordinate_note`, `evidence_note`.

Latitude and longitude are WGS84 decimal degrees. Longitude is negative west of Greenwich. `buffer_m` is set only where a paper states a radius around a known point.

`processes.csv`: `process_id`, `site_id`, `process_name`, `category`, `mars_target`, `claim`, `evidence_strength`, `citation_ids`, `measurable_by_remote_sensing`, `caveat`.

`measurable_by_remote_sensing` is `yes`, `partial`, or `no`. `partial` means a public layer can screen a related surface condition and cannot observe the field property named in the claim.

`features.csv`: `feature_id`, `name`, `role`, `definition`, `unit`, `aggregation`, `spatial_window_m`, `why_it_matters`, `supported_process_ids`, `proxy_limitations`, `recommended_for_phase2` (`yes` or `no`).

`datasets.csv`: `dataset_id`, `name`, `provider`, `variable`, `spatial_resolution`, `temporal_coverage`, `access_url`, `license`, `auth_required`, `format`, `coverage_scope`, `covers_mcmurdo`, `covers_askja`, `covers_mauna_loa`, `covers_haughton`, `phase1_status`, `known_gaps`.

`coverage_scope` is `global`, `latitude_limited`, `multi_region`, or `reference_only`. The first three are broader than a single analog patch. `covers_*` is `yes`, `no`, or `partial` for that analog.

`feature_datasets.csv`: `feature_id`, `dataset_id`, `role_in_feature` (`primary`, `alternate`, or `context_not_measurement`).
