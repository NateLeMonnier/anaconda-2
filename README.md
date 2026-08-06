# anaconda-2

Place name normalization tools for genealogical records. Takes messy, inconsistent place strings from historical records and resolves them to authority records in a jurisdiction hierarchy.

## Components

### rtl_matcher.py

Right-to-left location matcher with fallback transforms. Reads a TSV of raw place strings and resolves each to a record in the place authority. Both sources — the Authority_Place export and the Master Normalization Table — are read into memory up front, so a run needs no network access.

```
python rtl_matcher.py --input places.tsv --pa pa_export.tsv --mnt mnt_export.tsv

# Union Storied's Supabase place dictionary on top of the MNT
python rtl_matcher.py --input places.tsv --pa pa.tsv --mnt mnt.tsv --dict --env .env  # live pull
python rtl_matcher.py --input places.tsv --pa pa.tsv --mnt mnt.tsv --dict /path/to/tsv_dir
```

Run without flags to be prompted for paths.

| Flag | Effect |
|---|---|
| `--helper-term "Utah, USA"` | Geographic context that biases single-term matches toward candidates under it. Pass `''` to run without one. |
| `--dict [DIR]` | Union the Supabase place dictionary into the MNT, adding a per-term frequency prior and an illegible-term stop-list. No value means a live pull, which needs `SUPABASE_PASSWORD` via `--env` or the environment. |
| `--no-segment-commaless` | Turn off Phase 0. Reproduces pre-segmentation output for A/B comparison. |
| `--min-level` / `--max-level` | Move the supported jurisdiction range (default 3–10). Matches outside it are reported via `below_supported` and `supported_leaf_id`, not excluded. |

The pipeline runs in four phases:

0. **Comma-less segmentation** — Split separator-less strings into hierarchy terms (`Swift Minnesota` → `['Swift', 'Minnesota']`) where the authority backs every piece. Rows whose full string is already an unambiguous MNT mapping skip straight to the answer.
1. **Name resolution** — Convert raw terms into candidate authority UUIDs via the MNT and exact authority names, then widen: abbreviation expansion, name variants, prefix/suffix transforms, extraction after a spatial preposition, SymSpell correction at edit distance 1, and a cardinal-prefix strip. Every candidate records which lookup found it and what string it matched.
2. **Authority record caching** — Load the full record for every UUID found and walk parent chains until the hierarchy above them is cached.
3. **Right-to-left matching** — Starting from the broadest (rightmost) term, walk left, keeping only candidates whose parent chain connects back to the confirmed set. Unverifiable terms are skipped rather than failing the row; a proximity fallback (within 50km) recovers likely wrong-county entries. Survivors are ranked on evidence strength, helper-term match, and level gap. Population orders the array but never breaks a tie — rows that stay ambiguous are surfaced with their candidates for QA.

Outputs land in `<output-dir>/MM-DD/` with an auto-incrementing run number:

| File | Contents |
|---|---|
| `<stem>_NN.tsv` | Results, one row per input |
| `<stem>_NN_ties.tsv` | Tied candidates for ambiguous rows |
| `<stem>_NN_levels.tsv` | Level provenance: one row per level of the winning chain, with the input token and lookup method that produced it |
| `<stem>_NN_spelling.tsv` | Corrections applied |
| `<stem>_NN_segments.tsv` | Phase 0 decisions, including rows left unsegmented and why |

### evaluate_normalizer.py

Measures intrinsic quality of normalization output and optionally compares against ground truth. Produces a terminal summary, a JSON report, and a flagged-rows TSV.

Supports two modes:
- **Direct mode** — Evaluate a single output file: `python evaluate_normalizer.py output.tsv --input raw.tsv`
- **Pipeline mode** — Evaluate Phase 1 multi-file output: `python evaluate_normalizer.py --pipeline outputs/ prefix --input raw.tsv`

### test_rtl_matcher.py

Unit tests for the RTL matcher: comma-less segmentation, parent chain pre-fetching, candidate ranking and provenance, jurisdiction filtering, tie detection, transforms, spelling correction, proximity fallback, and parent-only resolution. Run with `python -m pytest test_rtl_matcher.py`.

Tests that drive a lookup function use the `local_pa` fixture, which installs a throwaway `LocalData` as the module-global `_LOCAL` — the lookups read their indexes off it rather than taking them as arguments.

## Design Docs

- `docs/2026-07-28-next-steps.md` — Current status, measured bottlenecks, and recommended order of work
- `docs/2026-06-02-rtl-level-preference-design.md` — Level preference design for RTL matching (superseded: population no longer resolves parent-only matches)
- `docs/2026-06-02-rtl-level-preference-plan.md` — Implementation plan for level preference
- `docs/2026-06-13-normalizer-eval-design.md` — Evaluator design and metrics specification
- `docs/superpowers/specs/2026-08-02-low-evidence-gate-design.md` — Low-evidence gate, not yet landed (`is_description` is its predicate)
