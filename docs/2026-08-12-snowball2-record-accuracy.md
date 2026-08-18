# Snowball2 record accuracy, and a one-row MNT defect

Run date 2026-08-12. Labels are `place-normalizer/utils/snowball2_ground_truth.tsv`,
15,796 strings the Leafprint curators resolved during the Snowball2 project,
each carrying its own `frequency`. Term accuracy and record accuracy come off
that one file, and the record weight is an exact per-string count rather than
the sampled band mean `score_records.py` has to work from.

The set also carries the residual the MNT sample cannot. Abstain-is-correct
rows are 32.1% of the strings here against 6.6% in `mnt_dev`, which is what the
production mix looks like once the already-resolved strings are not the frame.

## The defect, first

Eight rows, all high frequency, all wrong before and right after:

```
Reykjavik, NULL, NULL
   truth:  Reykjavíkurborg   (freq 5,462)
   before: (abstained)  [parent_rejected]
   after:  Reykjavíkurborg  [single_term]
Los Angeles, Cal, NULL
   truth:  Los Angeles   (freq 1,837)
   before: (abstained)  [parent_rejected]
   after:  Los Angeles  [chain_verified]
State of New South Wales, NULL, NULL
   truth:  New South Wales   (freq 1,191)
   before: (abstained)  [parent_rejected]
   after:  New South Wales  [single_term]
Aberdeen, S D, NULL
   truth:  Aberdeen   (freq 1,167)
   before: (abstained)  [parent_rejected]
   after:  Aberdeen  [chain_verified]
LeRaysville, NULL, NULL
   truth:  Le Raysville   (freq 1,081)
   before: (abstained)  [parent_rejected]
   after:  Le Raysville  [single_term]
Angusville, NULL, NULL
   truth:  Angusville   (freq 1,052)
   before: (abstained)  [parent_rejected]
   after:  Angusville  [single_term]
Selkirk, Man, NULL
   truth:  Selkirk   (freq 761)
   before: (abstained)  [parent_rejected]
   after:  Selkirk  [chain_verified]
Englewood, N J, NULL
   truth:  Englewood   (freq 749)
   before: (abstained)  [parent_rejected]
   after:  Englewood  [chain_verified]
```

Every one of those rows resolved its rightmost term to the same record:
`Graceland Cemetery, Decatur, Macon, Illinois, USA`, UUID
`BC1DB3CC-4AED-AB4D-997E-ABCFFB2D1816`, at level 2.

The cause is two MNT rows keyed on the same token:

```
_raw    _value               _ID
NULL    Ambiguous            Amb
null    Graceland Cemetery   BC1DB3CC-4AED-AB4D-997E-ABCFFB2D1816
```

`_load_mnt` drops the first, since `Amb` fails `_is_valid_local_uuid`, and
keeps the second. It lowercases raw keys, so every literal `NULL` in an input
string reaches the cemetery. Snowball2 pads absent jurisdiction levels with
`NULL`, and 96.1% of its strings carry at least one — 10,057 carry two.

The rightmost term anchors the right-to-left walk. Anchoring it in Decatur,
Illinois makes every real term to its left unverifiable, which is why 12,689
rows came back `parent_rejected` rather than wrong: the matcher declined,
correctly, given an anchor it had no reason to distrust.

The low-evidence gate does not catch this. Its predicate is `is_description`,
and `Graceland Cemetery` is a name rather than a bare appellative, so the
mapping never reaches `--mnt-defects`. The defects file from the run is empty.

The same defect costs on the MNT set at a smaller blast radius. Rows in
`mnt_dev_detail.tsv` containing a literal `NULL` are 118 of 2,500 and score
43.2% against 63.4% for the rest.

Removing the MNT row alone makes things worse, not better — 94% `no_auth_match`
— because the matcher then has no `NULL` handling of any kind and fails the row
at the anchor. Both pieces are needed: drop the defective mapping, and drop
`NULL` terms before matching. The old pipeline shipped `04_NULL_OutputScrub.py`,
so this input shape was known and handled there.

## Numbers

Four configurations, same 15,796 strings, same PA export (`PA6_16_2026v77`),
`--helper-term ''` throughout:

| configuration | term acc | record acc | coverage (records) | precision (records) |
|---|---|---|---|---|
| as shipped today | 24.9% | 30.1% | 25.3% | 49.9% |
| MNT defect removed, no NULL handling | 35.3% | 39.9% | 12.7% | 93.6% |
| NULL stripped, holdout keyed on the padded strings | 87.8% | 89.4% | 70.7% | 92.4% |
| **NULL stripped, holdout rebuilt** | **72.4%** | **72.3%** | **52.4%** | **89.1%** |

The third row is not a result. The holdout was built from the padded strings
while the matcher ran on the stripped ones, so the MNT rows keyed on the
stripped forms were never removed and the matcher answered 15 points of it
from memory. Rebuilding the holdout against both forms removes 17,554 MNT rows
and 2,185 dictionary terms, against 650 and 0 for the padded forms alone.

The last row is the one to quote. Both holdout checks pass:
`Full-string MNT fast path: 0 of 15796`, and zero rows carry
`match_type = mnt_full_string`.

Term accuracy 72.4%, 95% CI 71.7–73.0%. Record accuracy 72.3%.

Split by what the curator wrote:

| curator verdict | n | term acc | record acc |
|---|---|---|---|
| verified | 10,728 | 63.4% | 65.1% |
| ambiguous | 5,043 | 91.4% | 90.6% |
| illegible | 25 | 96.0% | 97.3% |

The 63.4% on verified rows sits close to the MNT set's 60.6% recall on
UUID-labelled rows. Two independently built sets, different corpora, different
label sources, roughly the same number — the metric is measuring the matcher
rather than the sample.

The headline reads higher than either because a third of the set is
abstain-is-correct, where declining scores. Quote the verified-row number
alongside it.

## Coverage against accuracy

Coverage is 49.5% of strings and 52.4% of records. Precision on what it commits
to is 86.9% and 89.1%. Coverage times precision counts committed rows only;
record accuracy also credits correct abstains, which is the 20-point gap
between 52.4% × 89.1% and 72.3%.

Reporting these separately is the point. Coverage answers how much stops going
to Leafprint and needs no labels at all. Precision answers whether what came
back is right.

## Reproduce

```
python eval/build_sb2_eval.py
python eval/build_mnt_holdout.py \
    --sample eval/data/sb2_input.tsv eval/data/sb2_input_nullstripped.tsv \
    --out eval/data/mnt_holdout_sb2_v2.tsv --dict-out eval/data/dict_holdout_sb2_v2
# drop rows whose _raw lowercases to 'null'  -> mnt_holdout_sb2_v2_nullfix.tsv
python rtl_matcher.py --input eval/data/sb2_input_nullstripped.tsv \
    --pa <PA6_16_2026v77.tsv> --mnt eval/data/mnt_holdout_sb2_v2_nullfix.tsv \
    --dict eval/data/dict_holdout_sb2_v2 --helper-term '' \
    --mnt-defects /tmp/sb2_defects.tsv --output-dir eval/runs
python eval/score_frequency.py --output eval/runs/08-12/sb2_input_nullstripped_02.tsv \
    --labels eval/data/sb2_labels.tsv --name snowball2
```

## Open

- `NULL` stripping is done in the eval input rather than in `rtl_matcher`.
  It belongs in the matcher, next to the other Phase 0 term handling.
- The defective MNT row should be fixed at the source. `NULL -> Amb` is already
  there and correct; `null -> Graceland Cemetery` is the row to delete.
- The low-evidence gate missed a mapping worth 47 points on this corpus.
  Whatever replaces `is_description` should consider frequency of the raw key
  against the specificity of what it maps to.
- Old-pipeline comparison on this corpus did not complete: its
  `place_authority_normalizer_parallel` stage ran 2h38m on the 15,795 unmatched
  rows without producing output and was killed. The comparison was run on the
  2,500-row `mnt_dev` set instead — see
  `2026-08-12-old-vs-new-pipeline.md`.
