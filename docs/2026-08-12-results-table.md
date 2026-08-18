# All measurements, one place

Every figure recomputed from the sheets on 2026-08-12. Three separate
evaluations, three different label sources. They are not interchangeable and
the differences between them are stated under each.

---

## 1. Realistic input — the snowball2 tail

The corpus the pipeline was built for: `home of her daughter, Newport Rd.,
Uhrichsville`, `near Muskego`, `152 North 1st West`. Sampled with probability
proportional to frequency, so each draw stands for one record and plain
averages estimate record-weighted figures.

Labels: both systems' outputs adjudicated by hand under identical written
policies. 200 draws adjudicated, 300 drawn.

| | rtl_matcher | old pipeline |
|---|---|---|
| **accuracy** | **90.5%** (85.6–93.8) | **46.5%** (39.7–53.4) |
| accuracy, strict — ancestors count as misses | 86.5% | 45.0% |
| **yield** — records it answers at all, 300 draws | **51.3%** (45.7–56.9) | **7.7%** (5.2–11.2) |
| **precision** — of what it answers, share right | **93.7%** (87.6–96.9) | **71.4%** (50.0–86.2) |
| **records placed correctly** — yield × precision | **52.0%** (45.1–58.8) | **7.5%** (4.6–12.0) |
| verdicts | 104 y / 77 a / 19 n | 15 y / 78 a / 107 n |

Intervals are Wilson at 95%.

The one-line version: the new pipeline answers about seven times as often and
is more trustworthy when it does.

Read the old pipeline's 46.5% carefully — 78 of its 93 correct rows are
correct refusals. It placed 15 records out of 200. The new pipeline placed 104.

Its precision interval spans 50–86% because it rests on 21 committed rows. Say
"roughly 70%", not "71.4%".

---

## 2. Curated input — the MNT dev half

2,500 strings drawn from the Master Normalization Table, labelled by the
curators before either system existed, band-reweighted by record share.
Ancestors get no credit in this scorer.

| | rtl_matcher | old pipeline |
|---|---|---|
| **record accuracy** | **67.9%** | **21.7%** |
| term accuracy | 60.3% | 43.3% |
| coverage, record-weighted | 79.4% | 23.0% |
| precision, record-weighted | 82.9% | 81.1% |
| commits to an answer | 1,888 / 2,500 | 964 / 2,500 |
| of those, correct | 74.9% | 79.5% |

By band, showing where the record weight sits:

| band | share of strings | share of records | rtl_matcher | old pipeline |
|---|---|---|---|---|
| head | 0.8% | 90.8% | 68.9% | 19.7% |
| mid | 12.6% | 7.0% | 57.3% | 41.5% |
| low | 66.7% | 2.2% | 59.5% | 40.5% |
| tail | 19.9% | 0.0% | 64.5% | 54.8% |

Precision barely moved between the two systems. What moved is how often either
answers at all — 23.0% of records to 79.4%.

---

## 3. Leafprint-verified input — snowball2 full set

15,796 strings the Leafprint curators resolved, each carrying an exact
frequency. rtl_matcher only; the old pipeline run on this corpus was killed
after 2h38m without producing output.

| | rtl_matcher |
|---|---|
| term accuracy | 72.4% (71.7–73.0) |
| record accuracy | 72.3% |
| coverage | 49.5% of strings, 52.4% of records |
| precision | 86.9% of strings, 89.1% of records |

By what the curator wrote:

| verdict | n | term acc | record acc |
|---|---|---|---|
| verified | 10,728 | 63.4% | 65.1% |
| ambiguous | 5,043 | 91.4% | 90.6% |
| illegible | 25 | 96.0% | 97.3% |

---

## What each metric means

| metric | denominator | question it answers | needs labels |
|---|---|---|---|
| yield | every row | does it answer at all? | no |
| precision | rows it answered | when it speaks, is it right? | yes |
| accuracy | every row | right, including when it correctly refuses? | yes |
| yield × precision | every row | what share of records get placed correctly? | yes |

Yield alone is gamed by guessing. Precision alone is gamed by answering less.
Quote them as a pair.

---

## How the three evaluations differ

| | corpus | labels | who made them |
|---|---|---|---|
| 1. PPS tail | raw newspaper strings, 55.2% feature-word | hand-adjudicated | me, reviewed with Nate |
| 2. MNT dev | curated table strings, 7.1% feature-word | `Match_Authority_ID` | Leafprint curators, pre-existing |
| 3. snowball2 full | Leafprint-processed, 18.5% feature-word | `ground_truth_id` | Leafprint curators, 2026-03 |

Only evaluation 1 uses input resembling production. Only evaluations 2 and 3
use labels neither system authored.

---

## Caveats that travel with these numbers

**Evaluation 1 adjudication is not blind.** I scored both sheets knowing which
was which, having seen rtl_matcher's answers first. Where the old pipeline
declined and rtl_matcher had been judged correct, I scored the decline as a
miss because a resolvable answer demonstrably exists — 90 of its 107 misses.
For strings like `home in Cushing, Oklahoma` that is self-evident; I did not
re-derive all 90 independently. That assumption is load-bearing for the 46.5%.
The yield comparison does not depend on it.

**Evaluation 2 ran on a decontaminated MNT.** Both systems lost their
dictionary lookup, so it measures generalization to unseen strings rather than
day-one throughput. The old pipeline's stage 01 matched 6 of 2,500 under
decontamination; in production it keeps the full table.

**Evaluation 1 ran on the full MNT, deployed configuration.** No decontamination
was needed: the old pipeline's automatch matched 0 of 300, and rtl_matcher's
full-string fast path did not fire. These tail strings are absent from the
dictionary.

**Ancestor credit differs between evaluations.** Evaluation 1 counts a correct
broader place as correct, per Nate's ruling; evaluations 2 and 3 do not. The
strict row in table 1 is the comparable one.

**Neither system ran an LLM in any evaluation.**

---

## Runtime

| | rows | time |
|---|---|---|
| rtl_matcher, snowball2 full | 15,796 | 20.8s, single process |
| rtl_matcher, PPS sample | 300 | 13.7s |
| old pipeline, PPS sample | 300 | ~7 min, 6 stages, 8 workers |
| old pipeline, mnt_dev | 2,494 | ~9 min |
| old pipeline, snowball2 full | 15,795 | killed at 2h38m, no output |
