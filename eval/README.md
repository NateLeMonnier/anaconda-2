# eval

Record accuracy measurement for `rtl_matcher`. Answers one question: of the
records we process, what share resolve to the correct Place Authority record?

Labels come from the curators, not a model — `Match_Authority_ID` on rows the
Master Normalization Table marks `UUID Verified`, 1,443,411 of them. Nothing
here calls an LLM, so relabeling costs nothing and the ceiling is curation
quality rather than a labeler's accuracy.

An earlier version labeled a snowball4 sample with two model families. It was
dropped, because the labels were wrong too often and too slow to produce at
5k. The band weighting and the scorer are what survived from it.

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

## Build the pool, once

    python eval/build_mnt_pool.py

The only FileMaker step, about two minutes. Writes `eval/data/mnt_pool.tsv`
(9,943 rows) and `eval/data/mnt_pool_population.json`. Re-running is a no-op
unless `--force` is passed.

Sampling is per band rather than uniform. The head band is 0.69% of the table,
so a uniform draw would need roughly 218,000 records to fill a 1,500-row head
quota. Pages stride across each band's found set because FileMaker orders one
by internal record id, which tracks insertion order and therefore source
project.

## Build the sample, once

    python eval/build_mnt_eval_sample.py

Writes `mnt_dev.tsv`, `mnt_heldout.tsv`, `mnt_labels_dev.tsv`,
`mnt_labels_heldout.tsv`, `mnt_bands.json`. Seed 42, 2,500 per half.

Bands split on `Total`, the record count:

| band | `Total` | strings | quota | record share |
|---|---|---|---|---|
| head | >= 100,000 | 10,159 | 1,500 | 90.8% |
| mid | 1,000-99,999 | 158,187 | 1,500 | 7.0% |
| low | 10-999 | 838,894 | 1,200 | 2.2% |
| tail | 1-9 | 250,550 | 800 | 0.0% |

Head carries nine tenths of the record weight off 10,159 strings, so the
headline is close to head-band accuracy. Quote the unweighted per-band table
next to it.

Record shares are estimated: the string count is FileMaker's exact foundCount,
the record count is that times the sampled mean, and the Data API cannot sum a
field across a found set. The head estimate has a heavy tail — the top 1% of
sampled head strings carry 24.6% of the band's records.

Rows with an empty `Total` (15.1% of the table) are outside the frame. The
metric is defined over MNT rows with a known record count.

`Ill` and `Amb` rows stay in at their natural rate, labeled `ABSTAIN`.
Abstaining on them scores correct and committing scores wrong, which is the
only regression test the low-evidence gate has.

## Decontaminate

    python eval/build_mnt_holdout.py

Removes every MNT row reachable from an eval string, under all three keys
`_load_mnt` files a row under: the lowercase raw, the same with hyphens
spaced, and `canonicalize_place` for the full-string fast path. 5,000 eval
strings produce 5,190 keys and remove 5,086 rows; the surplus over 5,000 is
MNT rows sharing a canonical form.

The same key set filters the dictionary into `eval/data/dict_holdout/`,
because `_ingest_dict_row` unions dict terms into `mnt_by_raw`. It removes 428
of 364,412 terms — small, because dictionary entries are single names while
eval strings are full ones. The illegible list is copied verbatim: junk terms
carry no authority mapping and cannot leak an answer.

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

## Diagnose

    python eval/build_review_table.py --output eval/runs/MM-DD/mnt_dev_NN.tsv
    python eval/analyze_failures.py --output eval/runs/MM-DD/mnt_dev_NN.tsv \
        --out docs/YYYY-MM-DD-mnt-failure-analysis.md

The first writes `eval/data/mnt_dev_review.tsv`, one row per input with the
curator's chain and the matcher's chain side by side, sorted wrong first. The
second writes the taxonomy: reachability, failure classes, and cuts by answer
depth, string shape, case, band, and label status. Rerun both after every
change — the taxonomy moves more than the headline does.

## Held-out discipline

Held-out runs on a cadence, not per change, and without `--detail`. Its output
is the aggregate numbers and nothing else. Diagnose drops on dev. When held-out
is burned, mint a fresh one.

Two independent checks that the holdout held, both on the matcher's own output:

- the log line reads `Full-string MNT fast path: 0 of 2500`
- no row in the results TSV carries `match_type = mnt_full_string`

A non-zero count on either means a key variant escaped, and the run is a
lookup test rather than a matching test.

## What the numbers mean

- **record accuracy** — the headline. Band accuracy weighted by band record
  share. Head carries 90.8% of records, so head errors dominate, which is
  correct for a record metric.
- **abstain** — an empty `authority_id`. Correct only against an `ABSTAIN`
  label, where the curator marked the string Illegible or Ambiguous. Anywhere
  else it is a miss.
- **excluded, no PA record** — the label names no PA record, so no correct
  answer exists. Quote this share whenever quoting the accuracy.

## Measurements

Dev half, 2,500 rows, 2026-08-10, `eval/runs/08-10/`.

Configuration matters more than any single change measured so far:

| run | `--mnt` | `--dict` | record accuracy |
|---|---|---|---|
| floor | holdout | none | 49.6% |
| headline | holdout | filtered | 67.4% |
| ceiling | full MNT | filtered | 97.4% |

The ceiling doubles as the label-join check — a low ceiling means the labels
are not lining up, not that the matcher regressed.

Changes since, all at the headline configuration:

| change | run | record accuracy | recall | precision |
|---|---|---|---|---|
| baseline | `mnt_dev_03` | 67.4% | 58.3% | 75.5% |
| name-fragment demotion | `mnt_dev_08` | 67.7% | 59.6% | 74.7% |
| container-slot county preference | `mnt_dev_09` | 67.9% | 60.6% | 75.6% |

Held-out tracks dev at both steps, aggregates only:

| run | record accuracy | recall | precision |
|---|---|---|---|
| `mnt_heldout_01`, at the fragment change | 68.5% | 59.2% | 75.2% |
| `mnt_heldout_02`, at the container slot | 68.7% | 60.1% | 76.0% |

Held-out sits 0.8 points above dev and moves with it, band for band, so
nothing measured on dev is fitted to the half being read.

The container-slot rule is the only change so far to raise precision and recall
together, because it rewrites an existing commit rather than creating one. Its
gains are mid and low band; head is untouched. On snowball2 it is flat — 2,390
matched rows against 2,395 — since `X, County, State` is a census shape that
newspaper prose rarely takes.

Headline split by what the curator wrote:

| label status | n | correct | wrong | abstain | accuracy |
|---|---|---|---|---|---|
| `mnt_verified` | 1,935 | 1,220 | 303 | 412 | 63.0% |
| `mnt_verified_multi` | 401 | 141 | 139 | 121 | 35.2% |
| `mnt_illegible` | 112 | 111 | 1 | 0 | 99.1% |
| `mnt_ambiguous` | 52 | 38 | 14 | 0 | 73.1% |

Rows the curators flagged `Multiple_UUID_Detected` score 28 points below the
clean ones, so quote the headline with the flag share attached.

An earlier attempt applied the fragment predicate as a lookup-time filter
instead of a ranking demotion. It scored below baseline at 66.1%, because
deleting a candidate before chain verification loses the legitimate ones —
`Valleyfield, Que.` wants Salaberry-de-Valleyfield and `Westcliff, England`
wants Westcliff-on-Sea. Sixteen head breaks against four fixes. Any future
candidate-quality signal belongs in `rank_candidates`, not in the lookup.

## What the number does not cover

- **Newspaper prose.** MNT strings are 5.6% feature-word against
  `snowball2_sample_5k`'s 51.7%. Nothing in the MNT resembles `home of Mr.
  P. J. Meagher` or `3 Phillips street, Beverly`. The `ObitDeathPlace` project
  looked like a way in, but those rows profile at 4.0% feature-word — it is the
  cleaned death-place field, not article text. The newspaper corpus has no
  ground truth, so `evaluate_normalizer.py` against it measures resolution
  rate rather than correctness.
- **Unresolvable input.** The MNT holds only strings a curator already
  resolved, so it under-represents `no_auth_match`.
- **Label ceiling.** Where the MNT is wrong the metric is wrong the same way.
  `mnt_defects.tsv` and the low-evidence gate exist because MNT rows carry
  defects.
