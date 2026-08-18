# Old pipeline against new, on the same 2,500 rows

Run date 2026-08-12. Both pipelines read `eval/data/mnt_dev.tsv`, the same
2,500-row dev half, scored by `eval/score_records.py` against
`eval/data/mnt_labels_dev.tsv` with `eval/data/mnt_bands.json` weights. Both
were handed the same decontaminated MNT (`eval/data/mnt_holdout.tsv`) and the
same authority export (`PA6_16_2026v77.tsv`). Neither ran an LLM.

|  | term acc | record acc | coverage (records) | precision (records) |
|---|---|---|---|---|
| old pipeline | 43.3% | 21.7% | 23.0% | 81.1% |
| new matcher | 60.3% | 67.9% | 79.4% | 82.9% |

By band, with each band's share of strings and of records:

| band | string share | record share | old | new | delta |
|---|---|---|---|---|---|
| head | 0.8% | 90.8% | 19.7% | 68.9% | +49.2pt |
| mid | 12.6% | 7.0% | 41.5% | 57.3% | +15.9pt |
| low | 66.7% | 2.2% | 40.5% | 59.5% | +19.0pt |
| tail | 19.9% | 0.0% | 54.8% | 64.5% | +9.8pt |

Unweighted, on the sample as drawn: the old pipeline commits to 964 of 2,500
strings and 79.5% of those are right; the new matcher commits to 1,888 and
74.9% of those are right.

That pair of numbers is the shape of the whole result. Precision is close to
flat — the old pipeline is not careless about what it answers. What moved is
how much it answers at all, from 23.0% of records to 79.4%. The gain is
coverage of the long tail rather than accuracy on what was already being
handled, which is what the project was for.

The head band is where the record metric is decided, since 0.8% of strings
carry 90.8% of records. The old pipeline scores 19.7% there and abstains on 574
of 750 head rows. Head strings are long and heavily punctuated, and without a
memorized full-string mapping the old pipeline has no route to them.

## What this comparison is and is not

Both pipelines lost the same thing to decontamination, so the test is
symmetric. It still needs saying plainly, because the old pipeline's first
stage is a pure MNT lookup: on the decontaminated table, `01_automatch` matched
6 of 2,500 rows. In production that stage keeps the full table and resolves
whatever it has seen before.

So this measures generalization to strings the dictionary has not seen. That is
the long-tail question, and the one worth answering — the goal was for more of
the tail to be resolved automatically and less of it to reach Leafprint. It is
not a claim about total production throughput on day one, where the old
pipeline's memorized answers still count.

Two exclusions, both deliberate:

- The fuzzy stage output (`_review_required.tsv`) is not counted. It is routed
  to human review rather than committed as an automatic match, so counting it
  would credit the old pipeline for work it hands to Leafprint.
- `04a_llm_place_matcher` was not run. The new matcher has no LLM stage, so
  running one only for the old pipeline would confound the comparison.

## Runtime

Measured separately, not on identical inputs, so quote it as an order of
magnitude rather than a ratio.

The new matcher resolved 15,796 snowball2 strings in 20.8 seconds in a single
process. The old pipeline's chain on 2,494 rows took roughly nine minutes
across six stages on eight workers. On the 15,795-row snowball2 set its
`place_authority_normalizer_parallel` stage alone ran 2h38m without producing
output, with the fuzzy and merge stages still ahead of it, and was killed
rather than finished. Its cost grows faster than linearly in row count.

## Reproduce

The old pipeline hardcodes `utils/Master_Normalization_File.tsv` and
`utils/PA.tsv`. `eval/oldpipe/mirror_oldpipe.sh` builds a working directory of
symlinks to the repo with those two paths replaced, so the run touches nothing
in `code/place-normalizer` and cannot read the undecontaminated table. It also
rewrites the holdout from the MNT's `_raw`/`_value`/`_ID` shape into the
`InputString`/`MatchAuthName`/`MatchAuthID` columns stage 01 reads.

The input is `eval/data/mnt_dev.tsv` unchanged — stage 01 validates for `place`,
`guid`, and a `frequency` column, all of which it already carries.

```
bash eval/oldpipe/mirror_oldpipe.sh /tmp/oldpipe_dev \
    eval/data/mnt_holdout.tsv <pa_export.tsv>
cd /tmp/oldpipe_dev
python 01_automatch_and_split.py <repo>/eval/data/mnt_dev.tsv --output-dir outputs
cp outputs/<stem>_anaconda_food.tsv data/food.tsv
bash parallel_pipeline.sh data/food.tsv place --no-verbose
cd -
python eval/oldpipe/oldpipe_to_scorable.py \
    --matched /tmp/oldpipe_dev/outputs/<stem>_Matched.tsv \
    --final   /tmp/oldpipe_dev/outputs/restructured_food_Final.tsv \
    --labels  eval/data/mnt_labels_dev.tsv --out oldpipe_dev_scorable.tsv
python eval/score_records.py --output oldpipe_dev_scorable.tsv \
    --labels eval/data/mnt_labels_dev.tsv --bands eval/data/mnt_bands.json
```

Stage 01 derives its output names from the input stem, so `<stem>` is `mnt_dev`
for the run above. The August run used a `devfood` stem and its filenames read
differently in an earlier version of this section; the two `<stem>` paths are
the ones to check first if a file is reported missing. `mirror_oldpipe.sh` prints
the same three commands on exit, so the work directory is the source of truth if
this block and the script ever disagree again.

`oldpipe_to_scorable.py` maps `place_guid`/`place_id` to `guid`/`authority_id`
and treats the old pipeline's `Amb` and `Ill` as an empty answer, the same act
the new matcher records as an abstain, so one scoring rule covers both.

## Open

- `parallel_pipeline.sh` errored with `data/data/devfood.tsv.tsv not found`
  when given a path rather than a bare prefix. The restructure step recovered
  and the chain ran, but the invocation is fragile.
- The snowball2 old-pipeline run never completed. If the comparison is wanted
  on that corpus, it needs either a smaller sample or a profiling pass on
  `place_authority_normalizer_parallel`.
