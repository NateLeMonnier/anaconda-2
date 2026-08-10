# eval

Record accuracy measurement for `rtl_matcher`. Design:
`../docs/superpowers/specs/2026-08-08-record-accuracy-metric-design.md`.

Answers one question: of the records we process, what share resolve to the
correct Place Authority record?

Two sets answer it over two corpora, and they share the scorer, the label
schema, and the band-weighting method:

| set | corpus | labels | what it measures |
|---|---|---|---|
| snowball4 | newspaper record locations | two model families, blind | prose-shaped input, 25.1% feature-word |
| MNT | curated record place fields | curators, exact | hierarchy-shaped input, 5.6% feature-word |

The MNT set is free to build and free to relabel, and it is the one to run on
every change. The snowball4 set is the only thing that measures newspaper
prose, and its labeling pass is expensive. Neither substitutes for the other.
Sections below cover snowball4; the MNT set starts at [MNT set](#mnt-set).

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

---

# MNT set

Record accuracy over the Master Normalization Table, with the curators'
mappings as labels and the matcher's copy of those mappings withheld.

## Why the MNT

It is the only free source of exact labels: 1,443,411 rows carry
`Match_Status = UUID Verified`, each with a curator-assigned
`Match_Authority_ID` and a `Total` record count for weighting. No model
labels the set, so relabeling costs nothing and the ceiling is curation
quality rather than a labeler's accuracy.

The strings are real record input, and cleaner than the newspaper corpus.
Feature-word rate (church, cemetery, hospital, street, home, farm, hotel,
school, depot): MNT `_raw` 5.6%, snowball4 25.1%, `snowball2_sample_5k`
51.7%. Expect this set to score above what the matcher achieves on prose.

`Input_Original` is not a rawer field than `Input_formatted` — across 3,000
records pulled at spread offsets they are identical in 100% of cases, and
`Input_Original` is blank on half the table. The big MNT export's `_raw`
column is `Input_formatted`.

## Contamination is the whole problem

`rtl_matcher` loads the MNT as its dictionary. A set drawn from that table,
run against the unfiltered table:

| corpus | `_full_string_fast_path` | exact `mnt_by_raw` |
|---|---|---|
| 5k drawn from MNT | 86.7% | 100.0% |
| `snowball2_sample_5k` | 0.1% | 0.0% |
| `Ground truth 6_17 - 7_9.tsv` | 3.7% | 26.0% |

`build_mnt_holdout.py` is not optional. Skip it and the number is recall of a
lookup table.

## Build the pool, once

    python eval/build_mnt_pool.py

The only network step, about two minutes. Writes `eval/data/mnt_pool.tsv`
(9,943 rows) and `eval/data/mnt_pool_population.json`. Re-running is a no-op
unless `--force` is passed.

Sampling is per band rather than uniform. The head band is 0.69% of the
table, so a uniform draw would need roughly 218,000 records to fill a
1,500-row head quota. Pages stride across each band's found set because
FileMaker orders one by internal record id, which tracks insertion order and
therefore source project.

## Build the sample, once

    python eval/build_mnt_eval_sample.py

Writes `mnt_dev.tsv`, `mnt_heldout.tsv`, `mnt_labels_dev.tsv`,
`mnt_labels_heldout.tsv`, `mnt_bands.json`. Seed 42, 2,500 per half.

Bands split on `Total` and follow the MNT's own distribution, not the
1000/10 split snowball4 uses:

| band | `Total` | strings | quota | record share |
|---|---|---|---|---|
| head | >= 100,000 | 10,159 | 1,500 | 90.8% |
| mid | 1,000-99,999 | 158,187 | 1,500 | 7.0% |
| low | 10-999 | 838,894 | 1,200 | 2.2% |
| tail | 1-9 | 250,550 | 800 | 0.0% |

Head carries nine tenths of the record weight off 10,159 strings, so the
headline is close to head-band accuracy. Quote the unweighted per-band table
next to it.

Record shares are estimated: the string count is FileMaker's exact
foundCount, the record count is that times the sampled mean, and the Data
API cannot sum a field across a found set. The head estimate has a heavy
tail — the top 1% of sampled head strings carry 24.6% of the band's records.

Rows with an empty `Total` (15.1% of the table) are outside the frame. The
metric is defined over MNT rows with a known record count.

`Ill` and `Amb` rows stay in at their natural rate, labeled `ABSTAIN`.
Abstaining on them scores correct and committing scores wrong, which is the
only regression test the low-evidence gate has.

## Export the dictionary, once

    python eval/export_dict.py

Pulls `place_term_dictionary` (364,412 terms) and `place_term_illegible`
(60,789 terms) out of Supabase into `eval/data/dict/`, in the shape
`_load_dict_tsv` reads. Needs `SUPABASE_PASSWORD` in `../.env`. Cached;
`--force` to re-pull.

Running the eval without the dictionary is not a neutral choice. The frequency
prior is the only thing feeding `_disambiguate_by_frequency`, so without it
every ambiguous set falls through — a no-dictionary run scored 49.6% against
67.4% with it, and produced zero `freq_resolved` rows.

## Decontaminate

    python eval/build_mnt_holdout.py

Removes every MNT row reachable from an eval string, under all three keys
`_load_mnt` files a row under: the lowercase raw, the same with hyphens
spaced, and `canonicalize_place` for the full-string fast path. 5,000 eval
strings produce 5,190 keys and remove 5,086 rows; the surplus over 5,000 is
MNT rows sharing a canonical form.

The same key set filters the dictionary into `eval/data/dict_holdout/`,
because `_ingest_dict_row` unions dict terms into `mnt_by_raw`. It removes
428 of 364,412 terms — small, because dictionary entries are single names
while eval strings are full ones. The illegible list is copied verbatim: junk
terms carry no authority mapping and cannot leak an answer.

No dictionary can restore the full-string fast path. `fs_tmp`, and so
`fs_by_raw`, are built only inside `_load_mnt`.

One residual leak worth knowing about: the frequency counts are derived from
curated data that includes these 5,000 rows. Filtering removes exact term
matches, not each row's contribution to the counts for its own terms. At 5,000
rows against a corpus of hundreds of millions of records the per-term effect
is negligible, but it is not zero.

`canonicalize_place` is imported from `rtl_matcher`, not copied. A copy would
keep working until someone changed the matcher's canonicalization, then stop
decontaminating without saying so.

## Score

    python rtl_matcher.py --input eval/data/mnt_dev.tsv --pa <pa.tsv> \
        --mnt eval/data/mnt_holdout.tsv --dict eval/data/dict_holdout \
        --helper-term '' --mnt-defects /tmp/eval_defects.tsv \
        --output-dir eval/runs
    python eval/score_records.py --output eval/runs/MM-DD/mnt_dev_NN.tsv \
        --labels eval/data/mnt_labels_dev.tsv --bands eval/data/mnt_bands.json \
        --detail eval/data/mnt_dev_detail.tsv

Three flags matter. `--dict` at the **filtered** directory, never the raw one
and never omitted. `--helper-term ''` because the set is global and a
geographic prior is a confound. `--mnt-defects` at a scratch path so eval rows
do not land in the shared defect list.

Two comparison runs are worth keeping current. Drop `--dict` for the algorithm
floor. Swap `--mnt` for the full table for the deployed ceiling.

Held-out follows the same discipline as snowball4: on a cadence, no
`--detail`, aggregates only.

## Diagnose

    python eval/build_review_table.py --output eval/runs/MM-DD/mnt_dev_NN.tsv
    python eval/analyze_failures.py --output eval/runs/MM-DD/mnt_dev_NN.tsv \
        --out docs/YYYY-MM-DD-mnt-failure-analysis.md

The first writes `eval/data/mnt_dev_review.tsv`, one row per input with the
curator's chain and the matcher's chain side by side, sorted wrong first. The
second writes the taxonomy: reachability, failure classes, and cuts by answer
depth, string shape, case, band, and label status. Rerun both after every
change — the taxonomy moves more than the headline does.

## Verifying the holdout held

Two independent checks, both on the matcher's own output:

- the log line reads `Full-string MNT fast path: 0 of 2500`
- no row in the results TSV carries `match_type = mnt_full_string`

A non-zero count on either means a key variant escaped, and the run is a
lookup test rather than a matching test.

## First measurement

Dev half, 2,500 rows, 2026-08-10, `eval/runs/08-10/`.

| run | record accuracy | head | mid | low | tail |
|---|---|---|---|---|---|
| **headline** — holdout + filtered dict | **67.4%** | 68.8% | 52.7% | 57.2% | 64.0% |
| floor — holdout, no dict | 49.6% | 49.3% | 50.7% | 55.3% | 62.5% |
| ceiling — full MNT + dict | 97.4% | 97.3% | 98.8% | 97.8% | 93.8% |

The dictionary is worth 17.8 points, almost all of it in head, where the
frequency prior has counts to work with. Where the rows moved between floor
and headline: 154 `parent_amb` to `chain_verified`, 87 `no_auth_match` to
`illegible`, 62 `parent_amb` to `freq_resolved`.

The ceiling is also the label-join check — a low ceiling would mean the labels
are not lining up, not that the matcher regressed.

On the 2,336 rows carrying a real UUID: recall 58.3%, precision 75.5%,
abstains 22.8%.

Headline, split by what the curator wrote:

| label status | n | correct | wrong | abstain | accuracy |
|---|---|---|---|---|---|
| `mnt_verified` | 1,935 | 1,220 | 303 | 412 | 63.0% |
| `mnt_verified_multi` | 401 | 141 | 139 | 121 | 35.2% |
| `mnt_illegible` | 112 | 111 | 1 | 0 | 99.1% |
| `mnt_ambiguous` | 52 | 38 | 14 | 0 | 73.1% |

Rows the curators flagged `Multiple_UUID_Detected` score 28 points below the
clean ones, so quote the headline with the flag share attached. The illegible
rows go to 99.1% once the stop-list is loaded, against 91.1% without it.

## Changes measured since

| change | run | record accuracy | recall | precision |
|---|---|---|---|---|
| baseline | `mnt_dev_03` | 67.4% | 58.3% | 75.5% |
| name-fragment demotion | `mnt_dev_08` | 67.7% | 59.6% | 74.7% |
| same, held-out half | `mnt_heldout_01` | 68.5% | — | — |

Held-out ran once at the fragment change, aggregates only. It lands 0.8 points
above dev with every band within about two, so nothing measured on dev is
fitted to it.

The fragment change fixed 33 rows and broke 4, two of which are rows where
abstaining was the correct answer. Gains sit outside head — head +1, mid +18,
low +8, tail +2 — which is why recall moves 1.3 points while the record-
weighted number moves 0.3. Abstentions fall from 22.8% to 20.2%.

An earlier attempt applied the same predicate as a lookup-time filter instead
of a ranking demotion. It scored worse than baseline (66.1%), because
deleting a fragment before chain verification loses the legitimate ones —
`Valleyfield, Que.` wants Salaberry-de-Valleyfield and `Westcliff, England`
wants Westcliff-on-Sea. Sixteen head breaks against four fixes. Any future
candidate-quality signal belongs in `rank_candidates`, not in the lookup.

Where the holdout loses: 704 of 2,336 UUID-labeled rows abstain and 444
commit wrong. Abstentions are mostly `parent_amb` (349) and `no_auth_match`
(206); wrong commits are mostly `chain_verified` (368), meaning the walk
connected a chain and still landed on the wrong record.

## What the number does not cover

- **Newspaper prose.** Nothing in the MNT resembles `home of Mr. P. J.
  Meagher` or `3 Phillips street, Beverly`. The `ObitDeathPlace` project
  (109,580 rows) looks like a way in, but those rows are 4.0% feature-word —
  `Castro Valley, Calif.`, `Oneonta, N.Y.` — the cleaned death-place field
  rather than article text.
- **Unresolvable input.** The MNT holds only strings a curator already
  resolved, so it under-represents `no_auth_match`, which was 1,449 of 5,000
  rows in the 07-28 snowball run.
- **Label ceiling.** Where the MNT is wrong the metric is wrong the same way.
  `mnt_defects.tsv` and the low-evidence gate exist because MNT rows carry
  defects.

There is no free label source for the newspaper side: snowball `guid` values
are row identifiers, and 0 of 20,000 sampled appear in the PA export.
