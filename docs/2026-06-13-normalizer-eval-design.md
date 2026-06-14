# Place Normalization Evaluator — Design Spec

## Purpose

A standalone tool that measures the quality of any place normalization run and produces comparable metrics across different normalizers (RTL matcher, main pipeline, LLM matcher, or future approaches). It answers two questions: "how well did this normalizer do?" (intrinsic quality) and optionally "how correct were its answers?" (ground truth comparison).

## Location

`/Users/natelemonnier/storied/code/normalizer-eval/evaluate_normalizer.py`

Outputs are written next to the output file being evaluated (direct mode) or into the pipeline output directory (pipeline mode).

## Two Operating Modes

### Direct Mode

For normalizers that produce a single output TSV (RTL matcher, LLM matcher, etc.):

```
python evaluate_normalizer.py output.tsv --input raw_input.tsv \
    [--pa PA.tsv] [--ground-truth gt.tsv] [--name "RTL Matcher v2"]
```

- `output.tsv` — the normalizer's output file (positional, required)
- `--input` — the original raw input TSV, joined by guid to recover original strings and frequencies when the output lacks them (required)
- `--pa` — path to PA.tsv for authority lookups (optional, enables resolution depth metrics when level is missing from output)
- `--ground-truth` — path to ground truth TSV (optional, enables precision/recall/F1)
- `--name` — a label for this run, used in report headers and JSON output (optional, defaults to output filename)

### Pipeline Mode

For the main place normalization pipeline's Phase 1 output, which splits results across multiple files:

```
python evaluate_normalizer.py --pipeline outputs/ locations_sample_5k \
    --input raw_input.tsv [--pa PA.tsv] [--ground-truth gt.tsv]
```

- `--pipeline DIR PREFIX` — the output directory and filename prefix
- The evaluator auto-discovers three files by convention:
  - `{PREFIX}_Matched_QA.tsv` — auto-matched rows from step 01 (has original string, frequency, place_id, match_type)
  - `{PREFIX}_anaconda_food_ruled_Final.tsv` — pipeline-matched rows (place_guid + place_id, many empty)
  - `{PREFIX}_anaconda_food_ruled_unmatched_place_cleaned.tsv` — unmatched rows sent to Leafprint
- These three files have zero guid overlap and together cover all input rows
- The evaluator combines them into a single unified view, joining back to `--input` by guid for original strings and frequencies

## Column Auto-Detection

The evaluator scans output column headers for known variants and maps them to a canonical internal schema. CLI flags override auto-detection.

| Canonical field | Known variants | CLI override |
|---|---|---|
| guid | `guid`, `place_guid` | `--guid-col` |
| authority_id | `authority_id`, `place_id`, `MatchAuthID` | `--id-col` |
| original | `original`, `place`, `Input_Original` | `--original-col` |
| frequency | `frequency`, `Frequency` | `--freq-col` |
| level | `level`, `Level` | `--level-col` |
| type_ahead | `type_ahead`, `Type_Ahead_Value`, `Typeahead` | `--typeahead-col` |

When a required field (guid, authority_id) cannot be found and no override is provided, the evaluator exits with an error naming the missing column and the columns it found.

## Intrinsic Quality Metrics (Always Produced)

### Match Rate

- **Row match rate**: percentage of rows with a non-empty authority_id that is a valid UUID (not `Amb` or `Ill`). Denominator is all rows. `Amb` and `Ill` are reported separately — they represent attempted-but-unresolvable, distinct from rows that got no match at all.
- **Frequency-weighted match rate**: same calculation, weighted by frequency column
- **Amb/Ill breakdown**: count and frequency of rows marked `Amb` (ambiguous) vs `Ill` (illegible) vs truly empty (no match attempted or no candidates found)

### Resolution Depth Distribution

For each matched row, the jurisdiction level of the matched authority record. Sourced from:
1. The output's `level` column if present
2. PA.tsv lookup by authority_id if `--pa` is provided and level is missing
3. Omitted if neither is available

Reported as a distribution table:

| Level | Label | Count | % of matched |
|---|---|---|---|
| 4 | City | ... | ... |
| 5 | County | ... | ... |
| 6 | State | ... | ... |
| 8 | Country | ... | ... |
| other | (grouped) | ... | ... |

Also reports a single "specificity score": the percentage of matched rows that resolved to level 4 or 5 (city/county), as a quick comparator across runs.

### Partial Match / Fallback Rate

Rows where the normalizer matched but lost specificity — the input implied a more specific location than what the match resolved to.

Detection method (in priority order):
1. **Explicit**: if the output has `skipped_count` > 0, the row is a partial match. Report `skipped_terms` if available.
2. **Inferred**: compare the number of comma-separated components in the original string to the matched level. A 3-component input matching to level 8 (country) or level 6 (state) is flagged. Specifically: if `input_components >= 2` and `matched_level >= 6` (state or broader), flag as inferred fallback. If `input_components >= 3` and `matched_level >= 5` (county or broader), flag as inferred fallback.

Reported as:
- Partial match count and percentage of matched rows
- Frequency-weighted partial match rate
- Top 10 partial matches by frequency (showing original string, matched authority, and what was lost)

### Token Overlap Check

For rows that have both an original string and a type_ahead/authority_name value, compute token overlap between the original and the matched name. Rows with zero overlapping tokens are flagged as potential mismatches. Not all are wrong (abbreviation expansions, transliterations are legitimate), but zero overlap is a useful signal.

Reported as:
- Count of zero-overlap rows
- Top 10 zero-overlap rows by frequency

## Ground Truth Metrics (When --ground-truth Provided)

The ground truth file is a TSV with at minimum:
- A guid column (joined to the output)
- A `correct_authority_id` column with the known-correct UUID (or `Amb`/`Ill`)

Metrics:
- **Accuracy**: percentage of rows where the normalizer's authority_id matches ground truth exactly
- **Precision**: of rows the normalizer matched (non-empty, non-Amb, non-Ill authority_id), what percentage were correct
- **Recall**: of rows that have a correct answer in ground truth (non-Amb, non-Ill), what percentage did the normalizer match correctly
- **F1**: harmonic mean of precision and recall
- **Confusion breakdown**: correct match, wrong match (matched but to wrong authority), false positive (matched but ground truth says Amb/Ill), miss (ground truth has answer but normalizer didn't match)

All ground truth metrics are also reported frequency-weighted.

## Output

### Terminal Summary

A formatted table printed to stdout. Example structure:

```
=== Place Normalization Evaluation: RTL Matcher v2 ===
Input: 5,000 rows | Total frequency: 1,342,614

Match Rate
  Rows:       3,133 / 5,000  (62.7%)
  Frequency:  1,323,479 / 1,342,614  (98.6%)
  Amb: 42 | Ill: 89 | Unmatched: 1,736

Resolution Depth (3,133 matched)
  City (L4):    1,999  (63.8%)
  County (L5):    393  (12.5%)
  State (L6):     535  (17.1%)
  Country (L8):    65  ( 2.1%)
  Other:          141  ( 4.5%)
  Specificity (L4+L5): 76.3%

Partial Matches
  Count:  1,137 / 3,133  (36.3%)
  Freq-weighted: ...

Potential Mismatches (zero token overlap)
  Count: 303

[Ground Truth section if provided]
```

### JSON Report

Written to `{output_basename}_eval_{timestamp}.json` next to the output file. Contains all metrics in a structured format for programmatic comparison across runs.

Top-level keys:
- `meta` — run name, timestamp, input file, output file, row count, total frequency
- `match_rate` — row and frequency-weighted rates, amb/ill/unmatched counts
- `resolution_depth` — level distribution, specificity score
- `partial_matches` — count, rate, top examples
- `token_overlap` — zero-overlap count, top examples
- `ground_truth` — accuracy, precision, recall, F1, confusion breakdown (only present when ground truth provided)

### Flagged Rows TSV

Written to `{output_basename}_eval_flagged_{timestamp}.tsv`. Contains every row the evaluator flagged, sorted by frequency descending. Columns:

- `guid`, `original`, `frequency` — row identity
- `authority_id`, `authority_name`/`type_ahead` — what was matched
- `level` — matched jurisdiction level
- `flag_reason` — one of: `partial_match`, `zero_token_overlap`, `gt_wrong_match`, `gt_false_positive`, `gt_miss`
- `detail` — human-readable explanation (e.g., "3 components, matched to L8 Country" or "ground truth: UUID-X, got: UUID-Y")

A single row can appear multiple times if it triggers multiple flags.

## Dependencies

Standard library plus pandas (already in the place-normalizer venv). No new dependencies.

## Non-Goals

- The evaluator does not run normalizers. It only reads their output.
- The evaluator does not write to FileMaker or modify any authority files.
- The evaluator does not deduplicate inputs or handle the workflow orchestrator's state machine.
- No web UI or interactive mode. CLI only.
