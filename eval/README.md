# eval

Record accuracy measurement for `rtl_matcher`. Design:
`../docs/superpowers/specs/2026-08-08-record-accuracy-metric-design.md`.

Answers one question: of the records we process, what share resolve to the
correct Place Authority record? Labels come from two model families that never
see matcher output, over a corpus the matcher has never run against.

## Why snowball4

`resources/np_records_snowball4_locations.tsv`. Every run in `rtl-outputs/` is
`snowball2_sample_5k`, and overlap with `snowball2_ground_truth.tsv` is 26
strings — the formats barely intersect. Prior exposure is nil by construction
rather than by discipline, and at 5.9M guids a burned held-out set can always
be replaced.

The corpus carries one row per (place, inferred_location) pair, so 142,029
guids arrive with their record count split across rows. `load_corpus` sums by
guid first; skipping that step misassigns bands and breaks the scorer's join
key. Aggregating by guid is safe — no guid maps to more than one place string.

| Band | Range | Strings | Records | % records |
|---|---|---|---|---|
| head | freq >= 1000 | 12,593 | 213,679,713 | 82.5% |
| mid | freq 10-999 | 528,892 | 29,423,056 | 11.4% |
| tail | freq 1-9 | 5,395,713 | 15,984,033 | 6.2% |

## Build the sample, once

    python eval/build_eval_sample.py

Writes `eval/data/eval_dev.tsv`, `eval/data/eval_heldout.tsv`, and
`eval/data/bands.json`. Seed 42, head 800 / mid 600 / tail 600, split 50/50.
Takes about 20 seconds.

## Label, once per eval set

Gemini half. Needs `GEMINI_API_KEY` in `../.env` and `pip install google-genai`:

    python eval/label_gemini.py --sample eval/data/eval_dev.tsv \
        --out eval/data/labels_gemini_dev.tsv

Claude half, agent-driven, since no Anthropic key is available:

    python eval/make_label_batches.py --sample eval/data/eval_dev.tsv
    # a subagent labels each eval/data/claude_batches/batch_NNN.md
    # and writes eval/data/claude_responses/batch_NNN.json
    python eval/ingest_claude_labels.py --sample eval/data/eval_dev.tsv \
        --out eval/data/labels_claude_dev.tsv

Merge:

    python eval/merge_labels.py --a eval/data/labels_gemini_dev.tsv \
        --b eval/data/labels_claude_dev.tsv \
        --out eval/data/labels_final_dev.tsv \
        --review eval/data/label_review_dev.tsv

Hand-adjudicate every row in `label_review_dev.tsv` and append the settled rows
to `labels_final_dev.tsv`. An unreviewed disagreement is not a label.
Adjudication resolves a conflict between two labelers and never reads matcher
output, so it does not burn held-out blindness.

## Score, every time the matcher changes

    python rtl_matcher.py --input eval/data/eval_dev.tsv \
        --pa <pa.tsv> --mnt <mnt.tsv> --output-dir eval/runs
    python eval/score_records.py --output eval/runs/MM-DD/eval_dev_01.tsv \
        --labels eval/data/labels_final_dev.tsv \
        --detail eval/data/dev_detail.tsv

## Held-out discipline

Held-out runs on a cadence, not per change, and without `--detail`. Its output
is the aggregate numbers and nothing else. Diagnose drops on dev. When held-out
is burned, mint a fresh one.

## How labels are decided

The label is the deepest node along the true chain that exists in PA. Leaf
exists, however obscure, and that is the label; leaf absent, climb until
something resolves; terminate at `NONE`.

That single rule covers the tail, which is mostly features rather than
jurisdictions — `Beverly Hilton Hotel`, `Bethel Lutheran church, Chicago`,
`Beveridge street, Indiana`. It also measures the low-evidence gate without a
separate metric: `Bethel Lutheran church, Chicago` labels to Chicago, so
abstaining is a miss, while `Lutheran church in the village` with no locatable
container labels `NONE`, so abstaining is correct. A wrong commit still scores
wrong.

Labeling never shares a failure mode with the matcher. Traversal runs leaf-first
and upward where RTL runs rightmost-first and leftward; selection is model world
knowledge where RTL uses chain connection, evidence rank, and population;
spelling correction is model knowledge where RTL uses SymSpell and abbreviation
tables; and the MNT is never read. The only shared surface is an exact `Term`
index on PA, unavoidable for anything emitting a PA UUID.

## Data handling

Every outbound place string passes through `prompt.redact_for_transport`, which
strips a leading house number when the leaf is a street — 29.3% of the tail band
is address-shaped. It costs nothing in accuracy, since the street is never in PA
and the label is the containing jurisdiction either way. Gated on a street
suffix so real numeric names survive; PA holds 12, including `100 Mile House`.
The unredacted string stays in the local TSV.

Only the `place` column leaves the machine. Not `guid`, not `frequency`, not
matcher output, not the MNT.

## What the numbers mean

- **record accuracy** — the headline. Band accuracy weighted by band record
  share. Head carries 82.5% of records, so head errors dominate, which is
  correct for a record metric.
- **abstain** — an empty `authority_id`. Never counted as correct. Where the
  label names a real place, an abstain is a miss.
- **excluded, no PA record** — neither label column found a PA record, so no
  correct answer exists. Quote this share whenever quoting the accuracy.
- **world-knowledge upside** — rows the string alone could not resolve but model
  world knowledge could. This is the size of the LLM enrichment step that was
  descoped, bounded above by mid plus tail, 17.6% of records.
