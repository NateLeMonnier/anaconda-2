# Record Accuracy Metric — Design

Date: 2026-08-08
Status: Approved, not yet implemented

## Problem

`rtl_matcher` has no measured accuracy. Intrinsic quality is reported by
`evaluate_normalizer.py`, but nothing answers the question leads actually ask:
of the records we process, what share resolve to the correct place?

The obvious source of labels, `place-normalizer/utils/snowball2_ground_truth.tsv`,
is unsuitable. Its 15,797 rows are frequency-sorted head-of-distribution strings in
a three-slot `NULL`-padded format (`South D., NULL, NULL`), many of them special
cases resolved by hand. It does not represent ordinary record input, and the
strings have been read repeatedly during development.

Building a fresh set by inspecting matcher output would produce a number that only
describes the cases already fixed. The design below avoids that at both the input
and the label level.

## Corpus

`resources/np_records_snowball4_locations.tsv` — 6,079,253 rows covering
259,087,272 records. Columns: `place`, `inferred_location`, `guid`, `frequency`.
Format is ordinary comma-delimited input
(`Syracuse, New York, United States of America`).

A row is one (place, inferred_location) pair, not one place string. 142,029
guids appear on several rows with their record count split between them —
`Brown University, Rhode Island` shows as 8 and 4 rather than 12. The sampler
sums by guid first, which yields 5,937,224 unique rows and keeps guid the
unique key the scorer joins on. Aggregating by guid is safe: no guid maps to
more than one place string, though 2,498 place strings carry more than one guid.

Every run in `rtl-outputs/` is `snowball2_sample_5k`. Snowball4 has never been fed
to the matcher. Overlap with `snowball2_ground_truth.tsv` is 26 strings out of
6.08M — the two formats barely intersect. Prior exposure is nil by construction
rather than by discipline.

Frequency distribution:

Measured after guid aggregation and after dropping the 26 seen strings:

| Band | Range | Strings | % strings | Records | % records |
|---|---|---|---|---|---|
| head | freq >= 1000 | 12,593 | 0.21% | 213,679,713 | 82.47% |
| mid | freq 10-999 | 528,892 | 8.91% | 29,423,056 | 11.36% |
| tail | freq 1-9 | 5,395,713 | 90.88% | 15,984,033 | 6.17% |

Because snowball4 is effectively inexhaustible, a burned held-out set can always be
replaced with a fresh blind one. That is the durable answer to test-set decay.

## Sample

Seeded stratified draw, seed 42, recorded in the output header of every emitted file.

- Drop the 26 strings present in `snowball2_ground_truth.tsv`.
- 800 head / 600 mid / 600 tail = 2,000 strings. Head is a 6% census of the 13,144
  strings carrying 81% of records.
- Within each band, 50/50 seeded split into dev and held-out, so both halves carry
  identical band composition.

Dev is inspected freely, fixed against, and re-run at will. Held-out reports
aggregate numbers only.

## Labels

The label is **the deepest node along the true chain that exists in Place
Authority**. Leaf exists, however obscure, and that is the label. Leaf absent, walk
up until something resolves. Terminates at `NONE` when nothing does.

This single rule covers the tail, which is dominated by buildings and features
rather than jurisdictions — `Beverly Hilton Hotel`, `Bethel Lutheran church, Chicago`,
`Bicounty Community Hospital`, `Beveridge street, Indiana`. Raw leftmost token
exact-matching a PA `Term` runs 89.1% head, 48.8% mid, 15.6% tail, and the tail
shortfall is these features, which are correctly absent from PA.

The rule also measures the low-evidence gate without a separate metric.
`Bethel Lutheran church, Chicago` labels to Chicago, so abstaining is a miss because
Chicago was the available answer. `Lutheran church in the village` with no locatable
container labels `NONE`, so abstaining is correct. A wrong commit — The Village,
Oklahoma — still scores wrong.

### Independence from the matcher

Labeling must not share failure modes with `rtl_matcher`. The procedure:

1. Model reads the raw string and proposes the **corrected canonical leaf** plus the
   full chain it believes that leaf sits in. Correction happens in model world
   knowledge (`Sanpat` -> `San Patricio`), not via SymSpell or abbreviation tables.
2. Look up the leaf in a PA index on normalized `Term`. Of 224,287 distinct Terms,
   192,245 map to exactly one PA row; only 16 exceed 100 rows, max `washington` at 320.
3. Single PA row, take it. Multiple rows, compare each row's `FullChainName` against
   the model's proposed chain; exactly one agreeing candidate resolves without a
   second call.
4. Zero or multiple agreeing candidates, second model call presenting the real PA
   chains numbered, model returns an index. Returning an index rather than a UUID
   makes hallucinated IDs structurally impossible.
5. Leaf absent from PA, model proposes the next level up and the loop repeats.

What this does not share with `rtl_matcher`: traversal runs leaf-first and upward
where RTL runs rightmost-first and leftward; selection is model world knowledge where
RTL uses chain-connection, evidence rank, and population; spelling correction is model
knowledge where RTL uses SymSpell and abbreviation expansion; and the MNT is never
read. The only shared surface is an exact `Term` index on PA, unavoidable for anything
that emits a PA UUID.

`FullChainName` (PA column 7) is already the flattened parent chain
(`Syracuse, Onondaga, New York, USA`), so it is read rather than walked. The build
asserts once that it agrees with the `ParentID` walk. Normalized `FullChainName`
collides for only 141 of 333,713 chains (0.04%); collisions flag to review rather
than guess.

### Two label columns

Every row gets two labels from one pass:

- `label_string_only` — the model may climb only to places named in the input.
  `Bethel Lutheran church, Chicago` -> Chicago. `Beverly Hilton Hotel` alone -> `NONE`.
- `label_world_knowledge` — the model climbs using what it knows.
  `Beverly Hilton Hotel` -> Beverly Hills.

`label_string_only` drives the headline, because it scores the matcher against the
information it actually receives. The delta between the two columns sizes the LLM
enrichment step that was deliberately descoped: how many rows it would recover, and
against a per-token price, what it would cost. The recoverable ceiling is bounded by
mid plus tail at 17.6% of records, before subtracting the rows that already resolve.

### Consensus

Two independent model families:

- Gemini via the Google AI Studio free tier, called from a script.
  `GEMINI_API_KEY` added to `anaconda-2/.env`, which currently holds only
  `SUPABASE_PASSWORD`.
- Claude via in-session subagents, since no Anthropic key is available. The sampler
  emits prompt-ready batches; subagents label them; results merge to a TSV.

2,000 rows at batches of 5 is roughly 400 calls per labeler, small enough that free
tier limits do not bind.

Agreement on both label columns accepts the row. Disagreement on either routes to
`label_review.tsv` for hand adjudication — expect 10-20%, so 200-400 rows. Adjudication
resolves a conflict between two labelers and never reads matcher output, so it does
not burn held-out blindness.

Two-way agreement is a weaker signal than three-way. It is accepted because the job
it has to do is route uncertain rows to a human, which two labelers do adequately.

No code from `code/place-normalizer` is used. Its LLM stack resolves through
`Master_Normalization_File.tsv` — the same MNT `rtl_matcher` consumes — and
`llm_config.yaml` weights the answer 70% MNT lookup against 30% model, so its
consensus measures MNT agreement rather than geography.

## Scoring

Join `rtl_matcher` output to labels on `guid`, compare `authority_id` against
`label_string_only`. Three buckets:

- **correct** — `authority_id` equals the label exactly.
- **wrong** — any other ID, including a true ancestor of the label. No partial credit.
- **abstain** — `ambiguous` or `low_evidence`. Reported separately, never counted
  as correct.

Per-band string accuracy, then record accuracy as the sum over bands of
(band record share x band accuracy), with band record totals carried in `bands.json`.
Head errors dominate the headline, which is correct for a record metric.

Rows labeled `NONE` in both columns are excluded from the denominator and their share
reported separately as PA coverage.

## Components

Written to `code/anaconda-2/eval/`. No dependency on `place-normalizer`.

**`build_eval_sample.py`** (~120 lines) — reads snowball4, drops snowball2-GT
overlaps, seeded stratified draw, dev/held-out split. Emits `eval_dev.tsv` and
`eval_heldout.tsv` with `place / guid / frequency / band`, plus `bands.json` carrying
each band's total record count for reweighting.

**`label_gemini.py`** (~250 lines) — PA `Term` index, the two-stage resolution loop
above, both label columns. Writes `labels_gemini.tsv`.

**`make_label_batches.py`** (~60 lines) — splits the sample into subagent-sized
prompt files for the Claude pass. Its output merges to `labels_claude.tsv`.

**`merge_labels.py`** (~100 lines) — joins the two labelers, marks agreement, emits
frozen `labels_final.tsv` and `label_review.tsv`.

**`score_records.py`** (~150 lines) — the scorer. Six numbers for held-out, full
per-row detail for dev, plus the world-knowledge delta.

Labeling runs once per eval set and freezes to a TSV, so the Claude half being
agent-driven does not hurt reproducibility. The sampler and scorer are scripts because
the scorer re-runs on every matcher change.

## Operating discipline

Held-out runs on a cadence, not per-change. Its output is the aggregate numbers and
nothing else — no row dump, no readable file. A drop is diagnosed on dev. When the
held-out set is eventually burned, a fresh one is minted from snowball4.

## Risks

The `FullChainName` versus `ParentID` agreement assertion may fail on some rows; if
so, the walk replaces the column read and step 3 costs one extra join.

Tail labeling generates the most disagreement and the most hand adjudication, and the
tail is only 6.3% of records. If review volume exceeds appetite, cut tail sample size
rather than skipping adjudication — an unreviewed disagreement is not a label.
