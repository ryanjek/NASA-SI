# NASAI
NASA Space Apps Challenge 2026

Phase 1 is a cited catalog of four terrestrial Mars analogs: the McMurdo Dry Valleys, Askja and Dyngjufjöll, the HI-SEAS landscape on Mauna Loa, and Haughton crater. Methods, site notes, and the table schema are in [docs/phase1/methods.md](docs/phase1/methods.md). The tables are in `data/phase1/`.

```bash
uv sync
uv run python scripts/validate_phase1.py
uv run python scripts/extraction.py \
  --phase1 data/phase1 \
  --out data/phase2 \
  --ee-project nasa-mars-analog-2026
```

The files in `data/processed/` are a separate Mars landing-site screen. They are not Earth analog results. Phase 1 does not train a model or run a similarity search.
