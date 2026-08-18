# Outstanding work — rtl_matcher

Written 2026-08-18, at handoff. Everything here is unfinished, known to be wrong,
or measured and left alone deliberately. Each item carries the evidence behind it
so nobody has to re-derive the size of a problem before deciding whether to work
on it.

Scope is `rtl_matcher.py`. The measurement harness in `eval/` is retained for
reference rather than maintained — production went a different direction on
metrics — so its own loose ends sit in §6 and nothing above that line depends on
them.

Ordering inside each section is by measured size. §5 is the order I would work in.

---

## 1. Before anyone else can run the matcher

**The two exports are not in the repo.** `PA6_16_2026v77.tsv` (56 MB) and
`Master_Normalization_File_6_16_2026v1.tsv` (115 MB), both under
`resources/place-authority-mnt-tsv/`, both FileMaker exports. Every number in
`docs/` is tied to those two files; a newer export moves the numbers. Confirm they
sit somewhere the team can reach rather than only on one laptop.

**Credentials.** `.env` is gitignored and holds `SUPABASE_PASSWORD`, needed only
for `--dict` with no value (the live pull). An unused `GEMINI_API_KEY` is left over
from a dropped labeling path and can go. Whoever inherits this needs their own
Supabase credentials; mine stop working.

**The repository is on a personal account.** `origin` is
`github.com/NateLeMonnier/anaconda-2`. It needs to move under Storied's
organization before that account goes away.

**Uncommitted work.** At handoff the tree carries the handoff docs, the
2026-08-12 measurement scripts and docs, and roughly 340 MB of eval data that must
stay untracked — four files exceed GitHub's 100 MB limit. The `.gitignore` needs
the `sb2` and `dict_holdout_sb2` paths added alongside the existing holdout
entries.

---

## 2. Known defects in the matcher

### 2.1 `NULL` terms are handled outside the matcher

The largest single defect, and the one to fix first. Two MNT rows are keyed on the
same token:

```
_raw    _value               _ID
NULL    Ambiguous            Amb
null    Graceland Cemetery   BC1DB3CC-4AED-AB4D-997E-ABCFFB2D1816
```

`_load_mnt` drops the first because `Amb` fails `_is_valid_local_uuid`, keeps the
second, and lowercases raw keys. So every literal `NULL` in an input string
resolves to a cemetery in Decatur, Illinois, at level 2. The rightmost term
anchors the right-to-left walk, so anchoring it in Illinois makes every real term
to its left unverifiable — 12,689 of 15,796 snowball2 rows came back
`parent_rejected`. The matcher declined correctly given an anchor it had no reason
to distrust.

Snowball2 pads absent jurisdiction levels with `NULL` and 96.1% of its strings
carry at least one, 10,057 carry two. Cost: 24.9% term accuracy as shipped against
72.4% with the fix. On the MNT set the same defect is smaller but present — rows
containing a literal `NULL` score 43.2% against 63.4% for the rest.

Both halves are needed. Deleting the MNT row alone makes things worse — 94%
`no_auth_match` — because the matcher then has no `NULL` handling at all and fails
at the anchor. The stripping belongs in Phase 0 beside the other term handling;
the old pipeline shipped `04_NULL_OutputScrub.py`, so the input shape was known
and handled there. Today it is done in the eval input instead, which means
production never gets it.

The low-evidence check does not catch this: `Graceland Cemetery` is a name rather
than a bare appellative, so `is_description` passes it and the mapping never
reaches `--mnt-defects`.

Evidence: `docs/2026-08-12-snowball2-record-accuracy.md`.

### 2.2 The low-evidence capitalization test is disabled

`is_description(span, original, case_test=False)` at `rtl_matcher.py:1461`.
`transform_variant` rewrites a term before lookup and records no span, so the span
falls back to the whole term and reads as lowercase — `near Mt. Hamill` and `east
central Kansas` were both gated wrongly with the test on. Re-enable once every
rewriting phase records a span; `transform_variant` is the known gap and
`cardinal_strip` needs checking too.

Two related defects were found and fixed while wiring the check, both invisible
until something actually read a span: `span_for` defaulted to the lowercased
lookup key, so every verbatim match reported as uncapitalized, and
`query_spelling_corrections_local` recorded SymSpell's lowercased suggestion
rather than the authority's `Auth_Place_Name`. The second was gating 37 correct
rows on its own, including four of the design spec's own negative tests. Expect
more of these if a third span consumer appears.

Evidence: `.superpowers/sdd/2026-08-02-low-evidence-gate/progress.md`.

### 2.3 Two provenance systems are live at once

`name_cache.origins` (`rtl_matcher.py:216`) records which lookup supplied every
`(term, uuid)` pair across all ten phases. `correction_uuids_by_term`
(`rtl_matcher.py:3390`) covers spelling corrections only, rebuilt by re-parsing
the corrections log. Ranking reads the second; the level-provenance export reads
the first. The first subsumes the second — `rank_candidates` could ask whether the
origin is `'spelling'` and the parallel structure could go.

Left alone because the two do not always agree: `origins` is first-writer-wins, so
a UUID found exactly and later rediscovered by correction is tagged `exact` while
`correction_uuids_by_term` lists it. Collapsing them changes what feeds the
`is_weak` ranking axis, which needs a scored run rather than a byte diff.

Evidence: `docs/2026-08-06-cleanup-followups.md` §1.

### 2.4 `mnt_defects.tsv` accumulates and nothing consumes it

Twenty rows so far — `street` to Street (Village), `city` to London, `church` to
Church (Settlement), `the village` to The Village (City). They are dictionary rows
to fix at the source, and nothing downstream of this repo reads the file. It needs
a route into Leafprint's correction process or it is a log nobody opens.

---

## 3. Quality work, measured and not done

### 3.1 `parent_amb` is manufactured, not measured

The largest quality item, unchanged since 2026-07-28. At the `parent_only` exit,
`rank_candidates` is called with `parent_level=None` (`rtl_matcher.py:2661`), so
`score()` short-circuits to `(is_weak, helper_miss, 0, -pop)` at
`rtl_matcher.py:2231`, and `detect_tie` compares the structural axes only. Without
a helper term, every multi-candidate `parent_only` row scores identically and
becomes `parent_amb` mechanically — 1,220 rows in the 5k baseline, 24.4% of the
corpus.

Demoting population was deliberate: a matching authority answering to QA cannot
let population break ties. What is missing is anything replacing it on the
structural axes. Candidates, in rough order of expected discrimination —
provenance tier (§3.2), admin-level appropriateness for a bare anchor term, and a
corpus-level country prior (§3.6).

### 3.2 Provenance tier is not axis 0 of the score

Every candidate already carries its origin — `mnt_full`, `exact`, `abbrev`,
`variant`, `cardinal_strip`, `preposition`, `spelling`, `fs`. Ranking still reads
the narrower `correction_uuids` path, so a spelling-corrected candidate can tie an
exact match. Across 1,601 ambiguous rows in the 5k sample, only 42% have two or
more candidates sharing the exact leaf name of the rightmost term. The other 58%,
about 930 rows or 18.6% of the corpus, are one exact match plus fuzzy noise:

| anchor term | noise admitted |
|---|---|
| Norman | Normal (McLean, IL), Narman (Erzurum, Turkey) |
| Towanda | Tonawanda, Gowanda (both NY) |
| Germany | Germay, German (Chenango, NY) |
| Eldon | Elon (Alamance, NC), Elmdon (Solihull, England) |

The ambiguity is manufactured by candidate generation rather than present in the
data. Candidate arrays are full: 1,071 of the ambiguous rows sit at the
`MAX_ARRAY` cap of 5.

One constraint, learned the hard way. An earlier attempt applied the name-fragment
predicate as a lookup-time filter rather than a ranking demotion and scored 66.1%
against a 67.4% baseline, because deleting a candidate before chain verification
loses the legitimate ones — `Valleyfield, Que.` wants Salaberry-de-Valleyfield and
`Westcliff, England` wants Westcliff-on-Sea. Sixteen head breaks against four
fixes. Any candidate-quality signal belongs in `rank_candidates`, never in the
lookup.

### 3.3 Jurisdiction suffixes and enumeration districts

`Oregon Township, Wisconsin` wants Oregon, Dane, Wisconsin. `Kurashiki City,
Japan` wants Kurashiki. `Fancy Gap Magisterial District, Carroll, Virginia` wants
Fancy Gap. The leaf gets discarded and the row falls back to a container or
abstains. In the 2,500-row failure analysis these are the top classes —
jurisdiction suffix 60 rows, enumeration/precinct/ward 57, other wrapping tokens
50, and 34 where the term was exactly the answer. By string shape, enumeration
district rows are 55.3% wrong and jurisdiction-suffix rows 35.9% wrong, against
10.9% on plain place strings.

`Roanoke Magisterial District, Charlotte, Virginia` resolving to Charlottesville
is the worst version: the wrapper survives, the real leaf does not, and the row
commits to an unrelated place with `chain_verified` confidence.

### 3.4 County against same-named city

58 rows in the failure analysis returned a level 4 record where the truth was
level 5, or the reverse. County-depth answers are 43.6% wrong against 15.3% at
city/village depth. `NULL, Cameron, Texas` wants Cameron County and gets Cameron,
Milam. The container-slot preference in `63b5281` addresses the `X, County, State`
census shape and is flat on newspaper prose, where that shape is rare.

### 3.5 ALL CAPS input

432 rows, 34.0% correct against 66.6% for mixed case, and 42.6% abstain. Case
information is load-bearing in more places than intended — §2.2 is the other half
of this. A folding pass at input, with the original preserved for the span tests,
is the obvious first attempt.

### 3.6 Formal phrasing, prefix noise, and US bias

From a 500-row review of matched rows, 102 flagged: 50 over-reductions where a
real place name was dropped in favour of state or country level, and 22 partial
matches where the parent was right and a real sub-place was dropped. `City of Fort
Benton, County of Chouteau, State of Montana` dropped all three levels. The
wrapping vocabulary is enumerable — `City of`, `County of`, `Parish of`, `Borough
of`, `District Court of`, `outskirts of`, `vicinity of`, `hills south of`, leading
`of`.

The same review found a US bias in cross-country homonyms: `County Gray, Ontario`
resolving to Ontario, San Bernardino; every instance of `Mexico` as a country
reaching Mexico (Masiku), Pampanga, Philippines; `Ulster, Fermanagh` reaching
Fermanagh Township, Pennsylvania. A bare `Pennsylvania` competes with a town named
Pennsylvania in Mobile, Alabama, with no country or admin-level prior to separate
a state from a same-named village. The corpus-level country prior in §3.1 is the
same fix.

Findings: `resources/SnowballLocationsSampled/rtl_review_findings_500.tsv`.

### 3.7 OCR artifacts beyond edit distance 1

SymSpell at distance 1 misses `Bingham ton` (Binghamton), `Fond de Lac`,
`Ishpenning`, `Gouveneur`, `Twenty Nine Palms`, `Clay Centre`. Two families are in
play: inserted spaces inside compound names, which want a de-spacing pass rather
than a wider edit distance, and genuine two-edit misspellings, which want distance
2 with a jurisdiction guard so `Sheboygan County` does not become Michigan. That
guard was dropped once already for exactly that regression, so it needs to be
narrower than the first attempt.

### 3.8 The `no_auth_match` ceiling is lower than it looks

Of 1,449 unmatched rows in the 5k sample, 59% have no geographic token at all and
do contain a street or venue keyword — `109 W. Kemp St.`, `Bayview freight yards`,
`home of Mr. P. J. Meagher`. Roughly 850 rows are irreducible to a jurisdiction
because the source record never named one. 82% of unmatched rows have no comma,
which makes comma-less handling look like the dominant fix; it isn't. The
extractable cases are narrow — two jurisdictions fused into one term, conjoined
places, a truncated county — and realistic yield is 150 to 250 rows, 3 to 5% of
the corpus. Worth knowing before anyone promises a large win here.

### 3.9 LLM reranker, only after §3.1 and §3.2

Scoped to ambiguous rows — about 1,600 in 5,000, so cost stays bounded. Input is
the original string plus the top five candidates with their type-ahead paths, which
`build_result_row` already inlines. Output is a chosen authority id or an explicit
abstention. Gate acceptance on precision holding: recall bought with precision is
a regression for a matching authority, and the QA side file exists so uncertain
rows can stay uncertain.

Running it before the two ranking fixes means paying a model to arbitrate `Narman`
against `Norman`, and losing the ability to separate reranker error from
candidate-generation noise. `docs/2026-07-28-next-steps.md` has the literature:
GeoNorm is this design with the reranker present, RACCOON validates reranking over
retrieved candidates, and UniTopRank argues for fixing rules first at scale.

---

## 4. Code health

- `rtl_matcher.py` is 3,599 lines and `test_rtl_matcher.py` is 3,090. Past
  comfortable, and not what is holding the numbers down. Split it while adding a
  module — local data layer, candidate generation, ranking, IO — rather than as a
  standalone refactor pass.
- `format_levels.py` and `format_readable.py` both reshape results for humans and
  could share a reader.
- `PYTHONHASHSEED` is load-bearing for reproducibility and documented only in the
  README. A note in the matcher's own module docstring would be better placed.

---

## 5. What I would do first

1. **Fix `NULL`** (§2.1) — strip `NULL` terms in Phase 0, delete the
   `null -> Graceland Cemetery` MNT row at the source. Largest measured win
   available and no design work needed.
2. **Establish a baseline** before touching ranking, so every later change has a
   current number to move.
3. **Give `parent_only` real structural axes** (§3.1). A quarter of the corpus is
   abstaining for a mechanical reason.
4. **Make provenance tier axis 0 of the score** (§3.2), in `rank_candidates` and
   nowhere else.
5. **Strip jurisdiction suffixes and enumeration-district wrappers** (§3.3, §3.6)
   — the same class of fix arrived at from two independent evaluations.
6. **Then consider the reranker** (§3.9), with precision as the acceptance gate.

### How to verify a change

1. `python -m pytest -q` — necessary, not sufficient.
2. Run the same sample at `PYTHONHASHSEED=0` before and after, diff the results
   TSV, and account for every changed row.
3. If measuring against curator labels, decontaminate first —
   `eval/build_mnt_holdout.py` exists because the matcher reads the MNT as its
   dictionary, and an undecontaminated MNT-drawn sample hits the full-string fast
   path on 86.7% of rows. Two guards say the holdout held: the log line reads
   `Full-string MNT fast path: 0 of N`, and no row carries
   `match_type = mnt_full_string`.

---

## 6. The measurement harness, for completeness

Retained rather than maintained. Production went a different direction on metrics,
so none of this is the metric of record and none of it is a matcher dependency.
Its own loose ends, recorded so they are known rather than discovered:

- **Thirteen hardcoded `/Users/natelemonnier/...` defaults** across nine files in
  `eval/`, all argparse defaults or shell fallbacks, so every one is overridable
  by flag. `rtl_matcher.py` itself is clean. A `STORIED_ROOT` environment variable
  would fix all of them.
- **Four scorers exist** — `score_records.py`, `score_frequency.py`,
  `score_head.py`, `score_paired.py` — and nothing states which is authoritative.
  `score_frequency.py` is the soundest of them: it weights each string by its own
  exact frequency instead of the sampled band means `score_records.py` has to use.
- **The MNT band weighting rests on estimates.** Record shares are the exact
  string count times a sampled mean, because the FileMaker Data API cannot sum a
  field across a found set, and the head band's estimate has a heavy tail. 15.1%
  of the table has an empty `Total` and is outside the frame entirely.
- **The labels are the MNT's own**, so where the table is wrong the metric is
  wrong the same way. §2.1 is that failure at a 47-point blast radius.
- **The hand verdicts are the only unreproducible artifact in the repo** — 200
  filled verdicts each in `eval/data/sb2_tailpps_verification.tsv` and
  `sb2_tailpps_oldpipe_verification.tsv`, behind the 90.5% figure. Rerunning any
  script regenerates its output; nobody regenerates 400 human judgments. The
  adjudication was not blind, and where the old pipeline declined and the matcher
  was judged correct the decline was scored a miss on 90 of its 107 misses.
- **Unscored and unmeasured**: `Ground truth 6_17 - 7_9.tsv` (7,014 curator rows),
  frequency-weighted coverage on a production run, and any labelled set of
  newspaper prose beyond the 200 hand-adjudicated rows. The old pipeline never
  completed on snowball2 — its `place_authority_normalizer_parallel` stage ran
  2h38m on 15,795 rows without output.
- **Dead weight to delete if anyone tidies `eval/`**: `pull_input_original.py` and
  its two output files, the two blank verification sheets
  (`sb2_head200_verification.tsv`, `sb2_tail400_verification.tsv`, zero verdicts
  filled), and the derived `mnt_dev_review.tsv`.
