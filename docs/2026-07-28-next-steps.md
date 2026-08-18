# RTL Matcher — Status and Next Steps

Date: 2026-07-28
HEAD: `c466620` (feat: cardinal prefix strip, compound place handling, proximity and non-resolution fixes)
Measured on: `rtl-outputs/07-28/snowball2_sample_5k_01.tsv` (5000 rows, 25316 total frequency)

## Current numbers

| match_type | rows | frequency |
|---|---|---|
| `no_auth_match` | 1449 | 6420 |
| `chain_verified` | 1275 | 6867 |
| `parent_amb` | 1166 | 5614 |
| `chain_amb` | 255 | 1385 |
| `chain_verified_proximity` | 206 | 1467 |
| `single_amb` | 180 | 869 |
| `parent_resolved` | 141 | 933 |
| `freq_resolved` | 129 | 568 |
| `parent_rejected` | 108 | 528 |
| `single_term` | 88 | 650 |
| `mnt_full_string` | 3 | 15 |

Resolved to a single authority: 36.8% of rows, 41.5% of frequency. Ambiguous: 32.0% of rows. Resolved plus ambiguous: 68.9% of rows, 72.6% of frequency.

The pipeline finds candidates for roughly seven rows in ten and commits on fewer than four. The bottleneck is resolution, not retrieval.

## Accuracy is stale

The last ground-truth measurement is `results/07-16/dict-union_eval.json`:

- Recall (the evaluator's `accuracy`, correct / all GT rows): **32.8%**
- Precision (correct / committed): **92.1%** — 2301 correct, 198 wrong, 0 false positive, 4515 miss

Twelve commits have landed since that run: confidence tiers, population demotion, cardinal prefix strip, preposition extraction, compound place handling, proximity fixes. None of them has been measured against ground truth.

`2ec63b0` (population no longer resolves `parent_only`) should have moved recall down and precision up by construction, since it converts resolutions into abstentions. Neither direction is confirmed.

The reason the 07-28 run has no accuracy section is coverage, not configuration: the 5k sample shares **zero** guids with `code/place-normalizer/utils/snowball2_ground_truth.tsv` (15796 labeled rows) and exactly one place string with `resources/ground-truth-locations/Ground truth 6_17 - 7_9.tsv` (7014 labeled rows). The evaluator's `--ground-truth` path was never exercised on this sample.

## Root cause of `parent_amb`

`rtl_matcher.py:2450`, the `parent_only` exit from `match_entry`:

```python
ranked = rank_candidates(list(confirmed), auth_cache, None)
```

`parent_level` arrives as `None`, so `score()` at `rtl_matcher.py:2196` short-circuits to `(helper_miss, 0, -pop)`. `detect_tie` at `rtl_matcher.py:2226` compares `s[:2]`, the structural axes only. Without a helper term every candidate scores `(0, 0)`, so **every `parent_only` row carrying two or more candidates becomes `parent_amb` mechanically**.

That accounts for 1166 rows, 23.3% of the corpus. `resolve_parent_only`'s own docstring states the consequence: "Population never resolves a multi-candidate set. Fall through to amb."

Demoting population was the right call for a matching authority that has to answer to QA. The gap is that nothing replaced it on the structural axes, so the tie test has nothing left to discriminate on.

## Candidate pools carry no provenance

SymSpell corrections land in `name_cache` alongside exact name matches, and downstream ranking cannot tell them apart. Drawn from `parent_amb` rows in the 07-28 output:

| anchor term | noise candidates admitted |
|---|---|
| Norman | Normal (McLean, IL), Narman (Erzurum, Turkey) |
| Napier | Naper (Boyd, NE) |
| Towanda | Tonawanda, Gowanda (both NY) |
| Texas | Tejas |
| Germany | Germay, German (Chenango, NY) |
| Eldon | Elon (Alamance, NC), Elmdon (Solihull, England) |
| Walnut | Marianna (Lee, AR) |

Measured across all 1601 ambiguous rows: only **42%** have two or more candidates sharing the exact leaf name of the rightmost term. The remaining 58%, about 930 rows or 18.6% of the corpus, are one exact match plus fuzzy noise. The ambiguity is manufactured by candidate generation, not present in the data.

A second, smaller version of the same problem is cross-country exact homonyms — bare `Pennsylvania` competes with a town named Pennsylvania in Mobile, Alabama, and `Illinois` competes with Illinois Township, Sedgwick County, Kansas — where there is no country prior or admin-level prior to separate a state from a same-named village.

Candidate count distribution across ambiguous rows: 2 candidates on 276 rows, 3 on 121, 4 on 133, and 5 (the `MAX_ARRAY` cap) on 1071. Recall is healthy; the arrays are full.

## The `no_auth_match` ceiling is lower than it looks

Of the 1449 rows:

- **8%** contain a US state, state abbreviation, or major country token anywhere in the string
- **59%** have no geographic token at all and do contain a street or venue keyword — `109 W. Kemp St.`, `Bayview freight yards`, `home of Mr. P. J. Meagher`, `Dykeman's Baptist Church`

Roughly 850 rows are irreducible to a jurisdiction because the source record never named one.

82% of `no_auth_match` rows have no comma, which makes commaless handling look like the dominant fix. It isn't. The extractable commaless cases are narrow: `Highway 175 South of Meeker Hill La, Germantown Washington County` (two jurisdictions fused into one term), `Allants Ga.` (misspelled city plus state abbreviation), `Sayre and Wyalusing` (conjoined places), `Ireland, County Ferma` (truncated county). Realistic yield is 150 to 250 rows, 3 to 5% of the corpus.

## What the literature says

**GeoNorm** (Zhang & Bethard, \*SEM 2023) is this architecture with one component missing. BM25 generates candidates from the gazetteer, then a transformer reranker scores them using ontology features including population, and the generate-and-rerank cycle runs twice — countries, states and counties first, then the remaining mentions using the coarse results as context. The right-to-left walk already implements the two-stage idea. What's absent is the reranker; its place is currently held by a hand-tuned lexicographic tuple.

**RACCOON** (WWW 2025) retrieves gazetteer candidates and feeds them into an LLM prompt as context, the first RAG-based geocoder. It validates the LLM-as-reranker-over-retrieved-candidates shape rather than LLM-as-extractor.

**Hu et al. 2024** (lightweight open LLMs plus geo-knowledge, IJGIS) report a fine-tuned Llama2-7B beating prior SOTA by 13% with mean error down 83%, and geo-knowledge-guided GPT beating both NER tools and fine-tuned BERT on the recognition half of the problem.

**UniTopRank** (IJGIS 2026) argues the opposite direction, using rule-driven ranking specifically to avoid LLM cost and inference latency at scale. That constraint applies here too, given 5k-row batches against the FileMaker Data API, and it supports fixing the rules before spending tokens.

## Recommended order

### 0. Re-measure before changing anything

Run HEAD against `resources/ground-truth-locations/Ground truth 6_17 - 7_9.tsv` with `evaluate_normalizer.py --ground-truth`, and separately against a sample drawn from `snowball2_ground_truth.tsv` so future intrinsic runs have accuracy coverage. No new code. Every decision below needs the current precision and recall as its baseline.

### 1. Give `parent_only` real structural axes

Fix the `(helper_miss, 0)` collapse in `rank_candidates`. Candidate axes, in rough order of expected discrimination: provenance tier (below), admin-level appropriateness for a bare anchor term, corpus-level country prior. Most of the 1166 `parent_amb` rows should resolve without reinstating population as a tiebreaker, which keeps the `2ec63b0` policy intact.

### 2. Tier candidate generation by provenance

**Half of this has landed.** Corrected 2026-08-18; the original text below described both halves as pending.

The tagging shipped in `3d54306` and `30dbf25`. Every candidate carries its origin — `mnt_full`, `exact`, `abbrev`, `variant`, `cardinal_strip`, `preposition`, `spelling`, `fs` — via `NameCache.origins` (`rtl_matcher.py:216`), `MatchStep.origins`, and the `ORIGIN_TO_METHOD` table (`rtl_matcher.py:127`). The level-provenance export reads them, which is where `_levels.tsv` gets its `match_method` column.

What is still open is the ranking half: make the tier axis 0 of the score, so a spelling-corrected candidate can never tie an exact match. `rank_candidates` still reads the narrower `correction_uuids_by_term` path (`rtl_matcher.py:3390`) rather than the origin, so the tag is recorded and never scored on. Roughly 930 rows, and it produces the feature a reranker would need as input.

Two constraints on doing it, both learned since: the signal belongs in `rank_candidates` and not in the lookup — an earlier attempt filtering candidates at lookup time scored 66.1% against a 67.4% baseline — and `origins` is first-writer-wins, so it disagrees with `correction_uuids_by_term` on any UUID found exactly and later rediscovered by correction. See `docs/2026-08-06-cleanup-followups.md` §1 and §3, and `OUTSTANDING.md` §3.2.

### 3. LLM reranker, scoped to ambiguous rows only

About 1600 rows in 5000, so cost stays bounded. Input is the original place string plus the top five candidates with their full type-ahead paths; `build_result_row` already inlines `candidate_ids` and `candidate_names`, so the prompt is most of the way assembled. Output is a chosen authority id or an explicit abstention.

Gate acceptance on precision holding at or above the current 92%. Recall bought with precision is a regression for a matching authority, and the QA side file exists precisely so uncertain rows can stay uncertain.

Running this step before 1 and 2 means paying an LLM to arbitrate `Narman` against `Norman`, and losing the ability to separate reranker error from candidate-generation noise.

### 4. Extraction and commaless handling

LLM-assisted, after the reranker is measured, with expectations capped at 3 to 5% of rows.

### 5. Cleanup as a byproduct

3178 lines in `rtl_matcher.py` plus 1668 in the test file is past comfortable, and it isn't what's holding the numbers down. Split it while adding the LLM module — local data layer, candidate generation, ranking, IO — rather than as a standalone refactor pass.

## Sources

- [Improving Toponym Resolution with Better Candidate Generation, Transformer-based Reranking, and Two-Stage Resolution](https://aclanthology.org/2023.starsem-1.6/) (GeoNorm)
- [RACCOON: A Retrieval-Augmented Generation Approach for Location Coordinate Capture from News Articles](https://arxiv.org/abs/2501.11440)
- [Toponym resolution leveraging lightweight and open-source large language models and geo-knowledge](https://www.tandfonline.com/doi/full/10.1080/13658816.2024.2405182)
- [UniTopRank: a scalable and language-independent method for toponym resolution](https://www.tandfonline.com/doi/full/10.1080/13658816.2026.2645831)
- [A survey on geocoding: algorithms and datasets for toponym resolution](https://link.springer.com/article/10.1007/s10579-024-09730-2)
- [Scalable Toponym Resolution with LLMs: Accuracy and Speed Optimizations](https://ceur-ws.org/Vol-3969/paper6.pdf)
- [Are We There Yet? Evaluating SOTA Neural Geoparsers Using EUPEG](https://arxiv.org/pdf/2007.07455)
