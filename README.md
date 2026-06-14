# anaconda-2

Place name normalization tools for genealogical records. Takes messy, inconsistent place strings from historical records and resolves them to authority records in a jurisdiction hierarchy.

## Components

### rtl_matcher.py

Right-to-left location matcher with fallback transforms. Reads a TSV of raw place strings and resolves each to an authority record in FileMaker's Authority_Place table.

The pipeline runs in three phases:

1. **Name Resolution** — Convert raw terms into candidate authority UUIDs via the Master Normalization Table and Authority_Place table, with fallback transforms (abbreviation expansion, directional prefix stripping, jurisdiction suffix separation).
2. **Authority Record Caching** — Bulk-fetch authority records and walk parent chains to pre-cache the full jurisdiction hierarchy.
3. **Right-to-Left Matching** — Starting from the broadest (rightmost) term, walk left through the place string, pruning candidates at each level by verifying parent-child relationships in the hierarchy.

### evaluate_normalizer.py

Measures intrinsic quality of normalization output and optionally compares against ground truth. Produces a terminal summary, a JSON report, and a flagged-rows TSV.

Supports two modes:
- **Direct mode** — Evaluate a single output file: `python evaluate_normalizer.py output.tsv --input raw.tsv`
- **Pipeline mode** — Evaluate Phase 1 multi-file output: `python evaluate_normalizer.py --pipeline outputs/ prefix --input raw.tsv`

### test_rtl_matcher.py

Unit tests for the RTL matcher's parent chain pre-fetching and parent-only resolution logic.

## Design Docs

- `docs/2026-06-02-rtl-level-preference-design.md` — Level preference design for RTL matching
- `docs/2026-06-02-rtl-level-preference-plan.md` — Implementation plan for level preference
- `docs/2026-06-13-normalizer-eval-design.md` — Evaluator design and metrics specification
