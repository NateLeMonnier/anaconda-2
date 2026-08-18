# Presentation evidence pack

Everything measured for the 2026-08-13 cross-team lead presentation, with the
file each number came from and the command that reproduces it. Written to be
read while building slides.

Two companion docs hold the detail:
`2026-08-12-old-vs-new-pipeline.md` and `2026-08-12-snowball2-record-accuracy.md`.

---

## 1. The two headline numbers

### Old pipeline against new, same 2,500 rows

|  | term acc | record acc | coverage (records) | precision (records) |
|---|---|---|---|---|
| old pipeline | 43.3% | 21.7% | 23.0% | 81.1% |
| new matcher | 60.3% | 67.9% | 79.4% | 82.9% |

Same input, same labels, same decontaminated MNT, same authority export, same
scorer, neither running an LLM.

By band:

| band | string share | record share | old | new | delta |
|---|---|---|---|---|---|
| head | 0.8% | 90.8% | 19.7% | 68.9% | +49.2pt |
| mid | 12.6% | 7.0% | 41.5% | 57.3% | +15.9pt |
| low | 66.7% | 2.2% | 40.5% | 59.5% | +19.0pt |
| tail | 19.9% | 0.0% | 54.8% | 64.5% | +9.8pt |

On the sample as drawn: old pipeline commits to 964 of 2,500 strings, 79.5% of
those right. New matcher commits to 1,888, 74.9% of those right.

That pair is the clearest way to say what changed. Precision is close to flat.
What moved is how much gets answered at all, 23.0% of records to 79.4%. The
work bought coverage of the long tail rather than accuracy on what was already
handled.

Reproduce:

```
cd /Users/natelemonnier/storied/code/anaconda-2
python eval/score_paired.py \
    --run "old pipeline=eval/runs/08-12/oldpipe/oldpipe_dev_scorable.tsv" \
    --run "new matcher=eval/runs/08-10/mnt_dev_09.tsv"
```

### Snowball2, Leafprint-verified truth, frequency-weighted

15,796 strings the Leafprint curators resolved during the Snowball2 project,
775,930 records represented.

| | value |
|---|---|
| term accuracy | 72.4% (95% CI 71.7–73.0) |
| record accuracy | 72.3% |
| coverage | 49.5% of strings, 52.4% of records |
| precision | 86.9% of strings, 89.1% of records |

Split by what the curator wrote:

| curator verdict | n | term acc | record acc |
|---|---|---|---|
| verified | 10,728 | 63.4% | 65.1% |
| ambiguous | 5,043 | 91.4% | 90.6% |
| illegible | 25 | 96.0% | 97.3% |

The 63.4% on verified rows sits next to the MNT set's 60.6% recall on
UUID-labelled rows. Two independently built sets, different corpora, different
label sources, roughly the same answer. Worth a line on a slide: the metric is
measuring the matcher rather than the sample.

Reproduce:

```
python eval/score_frequency.py \
    --output eval/runs/08-12/sb2_input_nullstripped_02.tsv \
    --labels eval/data/sb2_labels.tsv --name snowball2
```

---

## 2. Term accuracy against record accuracy

Laryn asked for both by name and flagged that the difference is what a
non-technical audience gets wrong. On the MNT set they differ by more than ten
points off identical rows.

| band | strings | string share | record share | new matcher acc |
|---|---|---|---|---|
| head | 10,159 | 0.8% | 90.8% | 69.5% |
| mid | 158,187 | 12.6% | 7.0% | 56.2% |
| low | 838,894 | 66.7% | 2.2% | 53.8% |
| tail | 250,550 | 19.9% | 0.0% | 61.5% |

Weighted by strings: **term accuracy 55.8%**. Weighted by records: **record
accuracy 68.2%**. Both ±3.1 points at 95%.

(The 08-10 run reports 67.9% record accuracy; 68.2% is the same band
accuracies recomputed from the failure-analysis rounding. Quote 67.9%.)

One caution. The 62.5% "all rows, unweighted" in
`2026-08-10-mnt-failure-analysis.md` is **not** term accuracy. It is the
accuracy of the sample, whose band quotas were 1500/1500/1200/800 and look
nothing like the string population. Do not put it on a slide as term accuracy.

Plain-language version for the deck: 0.8% of distinct place strings account for
90.8% of the records. Getting those right is what moves the record number.
Getting the other 99.2% right is what moves the term number. Both are real,
they answer different questions, and record accuracy is the one tied to how
many records get produced.

---

## 3. Coverage, precision, accuracy — three numbers, not one

Laryn's "magic number" (00:22:20 in the transcript, "we went from 80% to 99% of
the records being addressed") is coverage, not accuracy. Addressed means
auto-matched instead of shipped to Leafprint. It needs no ground truth.

- **coverage** — share of records the matcher commits on at all
- **precision** — of what it commits to, share correct
- **record accuracy** — also credits correct abstains, so it exceeds
  coverage × precision

On snowball2: 52.4% × 89.1% = 46.7% committed-and-correct, against 72.3%
record accuracy. The 25-point gap is correct abstention on the ambiguous and
illegible third of the set.

Keeping these separate is what stops "how accurate is it?" from being
unanswerable.

---

## 4. Ambiguous and illegible

Laryn raised this as an open question (00:22:20–00:23:29): how do you score a
string that no human can resolve. It is already implemented.

The scoring rule, in `eval/score_records.py:30` and `eval/score_frequency.py`:
where the curator marked a string Ambiguous or Illegible, the label is
`ABSTAIN`. Claiming nothing scores correct. Committing to any UUID scores
wrong. Everywhere else, an empty answer scores as a miss.

Evidence it works:

| set | rows | accuracy |
|---|---|---|
| MNT, curator marked illegible | 112 | 99.1% |
| MNT, curator marked ambiguous | 52 | 73.1% |
| snowball2, curator marked ambiguous | 5,043 | 91.4% |
| snowball2, curator marked illegible | 25 | 96.0% |

This is the slide that answers "what about the junk." Correctly refusing to
answer counts as a right answer, and it is measured, not asserted.

---

## 5. Classes of problems

For the slide Laryn suggested (00:15:49): show the kinds of problem rather than
the results. Source: `2026-08-10-mnt-failure-analysis.md`.

**Leaf discarded, wrapped in a jurisdiction suffix** — 60 rows, 2.6%

```
Schoenbrunn Village, Ohio      truth: Schoenbrunn, Tuscarawas, Ohio, USA
                                got:  Ohio, USA
Kurashiki City, Japan          truth: Kurashiki, Okayama, Japan
                                got:  Japan
LONGMONT CITY, BOULDER, Colorado
                               truth: Longmont, Boulder, Colorado, USA
                                got:  Boulder, Boulder, Colorado, USA
```

**Same name, wrong record** — 63 rows

```
NULL, San Francisco, California  truth: San Francisco County, California, USA
                                  got:  San Francisco, San Francisco, California, USA
Isle of Sylt, Germany            truth: Sylt, Schleswig-Holstein, Germany
                                  got:  Sylt, Nordfriesland, Schleswig-Holstein, Germany
```

**Enumeration district / precinct / ward noise** — 57 rows, 2.4%

```
Fancy Gap Magisterial District, Carroll, Virginia
    truth: Fancy Gap, Carroll, Virginia, USA
     got:  Carroll County, Virginia, USA
```

**Committed to an unrelated place** — 51 rows, 2.2%

```
Roanoke Magisterial District, Charlotte, Virginia
    truth: Charlotte County, Virginia, USA
     got:  Charlottesville, Virginia, USA
```

Accuracy by string shape, which is a good single chart:

| shape | n | correct | wrong | abstain |
|---|---|---|---|---|
| plain place string | 1,421 | 72.6% | 10.9% | 16.5% |
| all caps, no other marker | 300 | 31.3% | 22.0% | 46.7% |
| jurisdiction suffix | 234 | 53.4% | 35.9% | 10.7% |
| enumeration district / precinct | 199 | 36.7% | 55.3% | 8.0% |
| contains NULL | 115 | 44.3% | 21.7% | 33.9% |
| names a feature | 48 | 68.8% | 14.6% | 16.7% |

---

## 6. The NULL defect

The strongest single story in the pack: one bad row in a 1.4M-row dictionary,
found by building the evaluation.

Two MNT rows share a key:

```
_raw    _value               _ID
NULL    Ambiguous            Amb
null    Graceland Cemetery   BC1DB3CC-4AED-AB4D-997E-ABCFFB2D1816
```

`_load_mnt` (`rtl_matcher.py:608`) drops the first, since `Amb` fails
`_is_valid_local_uuid`, and lowercases raw keys. So every literal `NULL` token
in an input string resolves to a cemetery in Decatur, Illinois. Snowball2 pads
absent jurisdiction levels with `NULL` and 96.1% of its strings carry at least
one; 10,057 carry two.

The rightmost term anchors the right-to-left walk. Anchoring it in Decatur
makes every real term to its left unverifiable, so 12,689 rows returned
`parent_rejected` — the matcher declined, correctly, given an anchor it had no
reason to distrust.

Before and after, all high frequency:

```
Reykjavik, NULL, NULL          truth: Reykjavíkurborg (freq 5,462)
   before: (abstained) [parent_rejected]    after: Reykjavíkurborg [single_term]
Los Angeles, Cal, NULL         truth: Los Angeles (freq 1,837)
   before: (abstained) [parent_rejected]    after: Los Angeles [chain_verified]
State of New South Wales, NULL, NULL   truth: New South Wales (freq 1,191)
   before: (abstained) [parent_rejected]    after: New South Wales [single_term]
Aberdeen, S D, NULL            truth: Aberdeen (freq 1,167)
   before: (abstained) [parent_rejected]    after: Aberdeen [chain_verified]
LeRaysville, NULL, NULL        truth: Le Raysville (freq 1,081)
   before: (abstained) [parent_rejected]    after: Le Raysville [single_term]
Englewood, N J, NULL           truth: Englewood (freq 749)
   before: (abstained) [parent_rejected]    after: Englewood [chain_verified]
```

Cost on snowball2: 24.9% term accuracy as shipped, 72.4% once the mapping is
removed and `NULL` terms are stripped. Cost on the MNT set, smaller blast
radius: rows containing `NULL` score 43.2% against 63.4% for the rest, 118 of
2,500 rows.

Two things are needed, not one. Deleting the MNT row alone drives 94%
`no_auth_match`, because the matcher then has no `NULL` handling at all and
fails at the anchor. The old pipeline shipped `04_NULL_OutputScrub.py`, so this
input shape was known and handled there.

The low-evidence gate does not catch it. Its predicate is `is_description`, and
`Graceland Cemetery` is a name rather than a bare appellative, so the mapping
never reached `--mnt-defects`; that file came back empty.

**Not yet fixed in the matcher.** `NULL` stripping currently happens in the
eval input. It belongs in Phase 0. The MNT row should be deleted at source.

---

## 7. The ceiling, and where the remaining error lives

From `2026-08-10-mnt-failure-analysis.md`, on UUID-labelled rows:

| class | rows | share |
|---|---|---|
| reachable — the answer was findable and was missed | 353 | 15.1% |
| unsupported — outside the supported jurisdiction range | 320 | 13.7% |
| absent — not in the Place Authority at all | 248 | 10.6% |

**Ceiling for string-only matching logic: 75.7%.**

This is the best business slide in the pack. It separates "the code got it
wrong" from "the authority does not contain the place," and 24.3 points of the
gap to 100% is the second kind. That points at where the next investment goes,
which is the question a funding audience is actually asking.

---

## 8. Configuration matters more than any single change

| run | `--mnt` | `--dict` | record accuracy |
|---|---|---|---|
| floor | holdout | none | 49.6% |
| headline | holdout | filtered | 67.4% |
| ceiling | full MNT | filtered | 97.4% |

Quote the headline. Name the other two. Without the dictionary the frequency
prior behind `_disambiguate_by_frequency` is gone and every ambiguous set falls
through. The ceiling doubles as the label-join check — a low ceiling would mean
the labels are not lining up rather than a regression.

Changes measured since baseline, all at headline configuration:

| change | record accuracy | recall | precision |
|---|---|---|---|
| baseline (`mnt_dev_03`) | 67.4% | 58.3% | 75.5% |
| name-fragment demotion (`mnt_dev_08`) | 67.7% | 59.6% | 74.7% |
| container-slot county preference (`mnt_dev_09`) | 67.9% | 60.6% | 75.6% |

Held-out tracks dev at both steps: 68.5% then 68.7%, sitting 0.8 points above
dev and moving with it band for band. Nothing measured on dev is fitted to the
half being read.

---

## 9. Disclosures to put on the slide, not wait to be asked

- **Contamination.** The matcher loads the MNT as its dictionary. Every eval
  row is stripped from it before scoring. Proof in the run log:
  `Full-string MNT fast path: 0 of 15796`, and zero rows carry
  `match_type = mnt_full_string`. Without this the number is recall of a lookup
  table — a 5k set drawn from the MNT hits the fast path on 86.7% and exact
  `mnt_by_raw` on 100%.
- **Confidence.** MNT record accuracy ±3.1 points at 95%. The head band carries
  90.8% of record weight off 724 sampled rows, and the top 1% of sampled head
  strings carry 24.6% of that band's records. Record shares are estimates: the
  string count is FileMaker's exact foundCount, the record count is that times
  a sampled mean.
- **The multi flag.** `mnt_verified_multi` rows are 401 of 2,500 and score
  35.2% against 63.0% for clean rows, 28 points down. Attach the flag share
  whenever quoting the headline.
- **Excluded rows.** Quote "excluded, no PA record" alongside the accuracy.
  It is 0 on the dev half.
- **Frame.** 15.1% of MNT rows have an empty `Total` and sit outside the
  metric, which is defined over rows with a known record count.

---

## 10. Runtime

Measured separately on different inputs, so quote as an order of magnitude
rather than a ratio.

| | rows | time |
|---|---|---|
| new matcher, snowball2 | 15,796 | 20.8s, single process |
| old pipeline, mnt_dev chain | 2,494 | ~9 min, 6 stages, 8 workers |
| old pipeline, snowball2 | 15,795 | killed at 2h38m inside stage 4 of 6, no output |

The old pipeline's `place_authority_normalizer_parallel` grows faster than
linearly in row count. This affects the live-demo plan: running a large volume
of strings during the talk works for the new matcher and is not possible for
the comparison.

---

## 11. The LLM question

You decided against an LLM stage. Laryn warned Kendall will push on it and will
want F1 by model (00:18:08).

Your reasoning from the transcript, which holds: the cases where an LLM is
needed and useful are vastly outnumbered by cases where it is needed and the
input is unresolvable junk. Paying per call on garbage buys nothing.

There is already a measurement, from the old pipeline's own evaluation
(`code/place-normalizer/docs/LLM_EVALUATION_GUIDE.md`, dated 2026-03-10, run
against this same snowball2 ground truth):

| model | accuracy | F1 | Leafprint reduction |
|---|---|---|---|
| Claude 3 Haiku | 51.4% | 60.7% | 96.8% |
| GPT-4o-mini | 43.5% | 59.6% | 99.4% |

Both sit below the new matcher's 72.4% term accuracy on the same corpus. That
is the answer to the F1 question, it predates this work, and it did not cost
anything to produce. Caveat honestly: those are older models, the run is five
months old, and the comparison is indicative rather than controlled.

---

## 12. Files

### Ground truth and labels

| file | rows | what |
|---|---|---|
| `code/place-normalizer/utils/snowball2_ground_truth.tsv` | 15,796 | Leafprint-verified. `place, frequency, guid, ground_truth_name, ground_truth_id`. 67.9% UUIDs, 31.9% Ambiguous, 0.2% Illegible |
| `code/anaconda-2/eval/data/sb2_labels.tsv` | 15,796 | above, normalised to `guid, place, frequency, label, status, truth_name` |
| `code/anaconda-2/eval/data/mnt_labels_dev.tsv` | 2,500 | curators' `Match_Authority_ID` on `UUID Verified` MNT rows |
| `code/anaconda-2/eval/data/mnt_labels_heldout.tsv` | 2,500 | held-out half, aggregates only |
| `resources/ground-truth-locations/Ground truth 6_17 - 7_9.tsv` | 7,014 | curator export, no frequency. **Not yet scored** |

### Inputs

| file | rows | what |
|---|---|---|
| `eval/data/mnt_dev.tsv` | 2,500 | dev half, `place, guid, frequency, band` |
| `eval/data/sb2_input.tsv` | 15,796 | snowball2 as-is, NULL padding intact |
| `eval/data/sb2_input_nullstripped.tsv` | 15,796 | NULL terms removed. **This is the one the headline uses** |

### Decontaminated dictionaries

| file | what |
|---|---|
| `eval/data/mnt_holdout.tsv` | MNT minus rows reachable from mnt_dev/heldout strings |
| `eval/data/mnt_holdout_sb2_v2.tsv` | MNT minus rows reachable from snowball2 strings, both padded and stripped forms (17,554 rows removed) |
| `eval/data/mnt_holdout_sb2_v2_nullfix.tsv` | above, minus the two `null` rows. **Headline uses this** |
| `eval/data/dict_holdout/` | Supabase dictionary, filtered for mnt_dev |
| `eval/data/dict_holdout_sb2_v2/` | filtered for snowball2 (2,185 terms removed) |

Superseded, safe to delete — they were built against the padded strings only
and under-remove, which is the mistake described in §13:
`mnt_holdout_sb2.tsv`, `mnt_holdout_sb2_nullfix.tsv` (~115MB each).

### Authority

`resources/place-authority-mnt-tsv/PA6_16_2026v77.tsv` — used by both
pipelines, so the authority version is not a confound.

### Results

| file | what |
|---|---|
| `eval/runs/08-10/mnt_dev_09.tsv` | new matcher on mnt_dev, the 67.9% run |
| `eval/runs/08-12/sb2_input_01.tsv` | snowball2 with the NULL defect live — 24.9% |
| `eval/runs/08-12/sb2_input_02.tsv` | defect removed, no NULL handling — 35.3% |
| `eval/runs/08-12/sb2_input_nullstripped_01.tsv` | contaminated holdout — 87.8%, **do not quote** |
| `eval/runs/08-12/sb2_input_nullstripped_02.tsv` | **the headline, 72.4%** |
| `eval/runs/08-12/oldpipe/oldpipe_dev_scorable.tsv` | old pipeline mapped to `guid, authority_id` |
| `eval/runs/08-12/oldpipe/restructured_devfood_Final.tsv` | old pipeline raw output, `place_guid, place_id` |
| `eval/runs/08-12/oldpipe/dev_in_Matched.tsv` | old pipeline stage 01 hits, 6 rows |
| `eval/data/mnt_dev_detail.tsv` | per-row bucket for the 08-10 run |
| `eval/data/mnt_dev_review.tsv` | curator chain against matcher chain, wrong first |

### Code written for this

| file | what |
|---|---|
| `eval/build_sb2_eval.py` | ground truth to input + labels |
| `eval/score_frequency.py` | term, record, coverage, precision off one frequency-carrying label file |
| `eval/score_paired.py` | two runs side by side on the banded set |
| `eval/oldpipe/mirror_oldpipe.sh` | symlink mirror of place-normalizer with the two hardcoded authority paths swapped |
| `eval/oldpipe/oldpipe_to_scorable.py` | `place_guid`/`place_id` to `guid`/`authority_id`, `Amb`/`Ill` to empty |

---

## 13. Two mistakes made while producing this, and how they were caught

Worth knowing in case anyone asks how the numbers were validated.

**Contaminated holdout.** The first clean-looking snowball2 run scored 87.8%.
The holdout had been built from the NULL-padded strings while the matcher ran
on the stripped ones, so MNT rows keyed on the stripped forms were never
removed. Rebuilding against both forms removes 17,554 MNT rows and 2,185
dictionary terms, against 650 and 0 before. Fifteen points of that 87.8% was
memorization. Caught by the holdout's own diagnostic: `build_mnt_holdout.py`
reported far fewer removals than the string count implied.

**Stage 01 read alone.** The old pipeline's automatch matched 6 of 2,500 rows
on the decontaminated table, which looks like a 0.24% pipeline. It is a pure
MNT lookup and decontamination removes exactly what it does. The comparison
needed its downstream normalizer stages to mean anything.

---

## 14. Still open

- **Term accuracy is not in `score_records.py`.** `score_paired.py` computes
  it; the single-run scorer still prints record accuracy only.
- **Frequency-weighted coverage on a production run** has not been measured.
  This is the number closest to Laryn's "records addressed" framing and needs
  no labels.
- **`Ground truth 6_17 - 7_9.tsv`** (7,014 curator-labelled rows) is unscored.
- **Newspaper prose is not covered by any of this.** MNT strings are 5.6%
  feature-word against snowball2's 51.7%. Nothing in the MNT resembles
  `home of Mr. P. J. Meagher`. The newspaper corpus has no ground truth, so
  `evaluate_normalizer.py` against it measures resolution rate rather than
  correctness.
- **Unresolvable input is under-represented in the MNT set.** It holds only
  strings a curator already resolved. Snowball2 covers this at 32.1%
  abstain-is-correct; the MNT set is 6.6%.
- **Label ceiling.** Where the MNT is wrong the metric is wrong the same way.
  `mnt_defects.tsv` and the low-evidence gate exist because MNT rows carry
  defects, and §6 is an example the gate missed.
- **The NULL fix is not in the matcher.** Eval-side only.
