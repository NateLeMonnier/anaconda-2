# anaconda-2

Place name normalization tools for genealogical records. Takes messy, inconsistent place strings from historical records and resolves them to authority records in a jurisdiction hierarchy.

## Components

### rtl_matcher.py

Right-to-left location matcher with fallback transforms. Reads a TSV of raw place strings and resolves each to an authority record in the Authority_Place table, using either local TSV exports (default) or the FileMaker Data API.

```
python rtl_matcher.py --input places.tsv --pa pa_export.tsv --mnt mnt_export.tsv
python rtl_matcher.py --api --env .env --input places.tsv   # FileMaker mode

# Atomized dictionary mode
python rtl_matcher.py --input places.tsv --pa pa.tsv --mnt mnt.tsv --dict --env .env # Default live Supabase pull
python rtl_matcher.py --input places.tsv --pa pa.tsv --mnt mnt.tsv --dict /path/to/tsv_dir
```

Run without flags to be prompted for paths. `--helper-term "Utah, USA"` supplies geographic context for disambiguating single-term matches.

The pipeline runs in three phases:

1. **Name Resolution** — Convert raw terms into candidate authority UUIDs via the Master Normalization Table and Authority_Place table. Fallback layers: abbreviation expansion, name variant generation, prefix/suffix transforms, SymSpell spelling correction, and (API mode) FamilySearch lookups for unmatched cities.
2. **Authority Record Caching** — Bulk-fetch authority records and walk parent chains to pre-cache the full jurisdiction hierarchy.
3. **Right-to-Left Matching** — Starting from the broadest (rightmost) term, walk left through the place string, pruning candidates at each level by verifying parent-child relationships in the hierarchy. Unverifiable candidates fall back to proximity matching (within 50km) and population-based disambiguation.

Outputs land in `<output-dir>/MM-DD/` with an auto-incrementing run number: the results TSV, a `_ties.tsv` side file listing tied candidates for ambiguous rows, and a `_spelling.tsv` log of applied corrections.

### evaluate_normalizer.py

Measures intrinsic quality of normalization output and optionally compares against ground truth. Produces a terminal summary, a JSON report, and a flagged-rows TSV.

Supports two modes:
- **Direct mode** — Evaluate a single output file: `python evaluate_normalizer.py output.tsv --input raw.tsv`
- **Pipeline mode** — Evaluate Phase 1 multi-file output: `python evaluate_normalizer.py --pipeline outputs/ prefix --input raw.tsv`

### test_rtl_matcher.py

Unit tests for the RTL matcher: parent chain pre-fetching, population-based disambiguation, candidate ranking, jurisdiction filtering, tie detection, transforms, and spelling correction. Run with `python -m pytest test_rtl_matcher.py`.

## Design Docs

- `docs/2026-06-02-rtl-level-preference-design.md` — Level preference design for RTL matching (superseded: parent-only resolution now uses population alone)
- `docs/2026-06-02-rtl-level-preference-plan.md` — Implementation plan for level preference
- `docs/2026-06-13-normalizer-eval-design.md` — Evaluator design and metrics specification
