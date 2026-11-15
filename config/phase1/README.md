# Phase 1 catalog (Ryan Ng, read-only copies)

Exact copies of the Phase 1 tables and methods file, taken from git commit `19e190c3cc8b8bf972fab2c79750da2228bb2bfa`
(branch `origin/earthmap`, path `Phase 1/`). The same files are in the shared Drive folder
`1xrrr9LAgGS6O2ZXYGgaeVPzpQCVjgsBy`. Do not edit here; fix upstream and re-copy.

**Local additions (the only differences from upstream, appended rows, nothing upstream was changed):**
`datasets.csv` gains `modis_mod11c3` and `feature_datasets.csv` gains two `alternate` rows linking it to
`lst_summer_max_c` and `lst_winter_min_c`. It is the coarse monthly stand-in for the Drive's `modis_lst`
(MOD11A1) used by the 50 km pre-filter. The Drive originals are untouched (read-only). Upstream hashes
are below; the two edited files list their upstream hash first, then the local one.

Copies live under `config/` because `data/` is git-ignored in this repo.
Read `methods.md` first: it defines evidence grades, what a search seed is, and which
features are `global_search` and which are `reference_only`.

SHA-256:
```
67aeed495fa74601f55f7dad410f68455e3296cb6d5bfa9a486c318925b0fa50  datasets.csv  (upstream)
68c1ab6cb3543879d373c0bc9c0b428ef460e7e54a286e3a9215b5d9f95ae263  datasets.csv  (local, with additions)
bbbeeec9c0e51091e1f89de58f01eff80d2346e3544a27bf6da47f0231cb0225  feature_datasets.csv  (upstream)
b57edce26f6334d56f49b362b1bcec8df1ac59c1271fbad3644e82d362a32315  feature_datasets.csv  (local, with additions)
b2866d040bd3114a19eba44434c22c0ae374b1a99391054d8f3a51161ec4bb07  features.csv
7d8e06515ab5a8b8c86f47c73cdf35d656bed793aa6c8f025ef3db058c1202f6  processes.csv
8ff9a89aefe398c1e29b07f8b950fde89d69cc951223dfd6a7a0462393cdd1e8  references.csv
c7e560fcab59f911de98cd2e2ce067c687395a85523758b5a129821680dcf96c  sites.csv
38c85d226d9140e30ee7bf67b342031c147601a1024d7c11ec882812d8656ba6  sites.geojson
68b116c06feee8f8689cf851cfb2e555fa42161d2e401eb46d1a609749fb4343  methods.md
```
