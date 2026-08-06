# Cleanup follow-ups

Date: 2026-08-06
Cleanup range: `59004e9..39ba23a`

The cleanup pass was behavior-preserving by construction: every commit was
verified byte-identical against a pre-cleanup run of the 5k sample, in both the
no-helper and multi-term-helper configurations. Four things surfaced during it
that cannot be fixed under that constraint. They are written down here so they
are known rather than discovered.

## 1. Two provenance systems are live at once

`name_cache.origins` (`rtl_matcher.py:216`) records, for every `(term, uuid)`
pair, which lookup phase supplied it — `mnt`, `exact`, `abbrev`, `variant`,
`transform`, `preposition`, `spelling`, `cardinal_strip`, `ascii_fold`, `fs`.
It is complete and it is populated by every phase.

`correction_uuids_by_term` (`rtl_matcher.py:3158`) covers spelling corrections
only, and is rebuilt in `_run_phase3` by re-parsing the corrections log that
`query_spelling_corrections_local` returned.

The split is that **ranking reads the second and the export reads the first**.
`rank_candidates` takes `correction_uuids` and uses it for the `is_weak` axis;
`build_level_provenance` reads `step.origins` and maps it through
`ORIGIN_TO_METHOD`. The first subsumes the second — `rank_candidates` could ask
whether the origin is `'spelling'` and drop the parallel structure entirely.

Not done here because it changes what feeds `is_weak`, and the two sources are
not guaranteed to agree: `origins` records first-writer-wins, so a UUID found
by an exact match and later rediscovered by correction is tagged `exact`, while
`correction_uuids_by_term` would list it. That difference is probably the
correct behavior in both cases, but "probably" needs an `evaluate_normalizer`
run confirming precision holds, not a byte diff.

## 2. `parent_amb` is manufactured, not measured

Unchanged from `2026-07-28-next-steps.md` §1 and still the largest single
quality item. At the `parent_only` exit, `parent_level` arrives as `None`, so
`score()` short-circuits to `(is_weak, helper_miss, 0, -pop)` and `detect_tie`
compares only the first three axes. Without a helper term, every
multi-candidate `parent_only` row scores identically and becomes `parent_amb`
mechanically — 1220 rows in the 5k baseline, 24.4% of the corpus.

This is a feature change wearing a cleanup label, which is why it stayed out.

## 3. `next-steps.md` §2 is stale

That section calls for tagging candidates by provenance as unstarted work. It
landed in `3d54306` and `30dbf25`: `NameCache.origins`, `NameCache.spans`,
`MatchStep.origins`, and the `ORIGIN_TO_METHOD` table at `rtl_matcher.py:127`
all exist, and the level-provenance export consumes them.

What has *not* landed is the second half of that recommendation — making the
tier axis 0 of the ranking score. Ranking still uses the narrower
`correction_uuids` path described in item 1. The doc should be corrected before
the team reads it, or it describes finished work as pending and unfinished work
as covered.

## 4. Accuracy is unmeasured

Per `next-steps.md` §0, the last ground-truth run is
`results/07-16/dict-union_eval.json` — 32.8% recall, 92.1% precision. Sixteen
feature commits have landed since. Nothing in this cleanup changed behavior, so
the number is no more stale than it was, but it is the first question the team
will ask and it predates half the features being presented.

The blocker named in that doc still holds: the 5k sample shares no guids with
`snowball2_ground_truth.tsv` and one place string with the 6/17–7/9 ground
truth, so `--ground-truth` has never been exercised on it. Measuring means
drawing a sample that overlaps a labeled set first.

## Also worth knowing

- ~~**`is_description` is not wired in.**~~ Resolved later the same day: Tasks
  4-7 of `docs/superpowers/plans/2026-08-02-low-evidence-gate.md` landed, so
  `is_description` now backs the `low_evidence` match type from both
  uncorroborated paths. Its case test is off by default — see the ledger at
  `.superpowers/sdd/2026-08-02-low-evidence-gate/progress.md` for why, and for
  the two span-recording defects that wiring it exposed. The live gap it left
  behind: **`transform_variant` rewrites a term before lookup and records no
  span**, which is what keeps the case test disabled.
- **`--helper-term` prompts when omitted.** Passing `''` is what skips it. The
  old help text claimed the opposite; corrected in `e38ffff`.
- **`PYTHONHASHSEED` matters for reproducibility.** Several paths iterate sets
  of UUID strings. Runs are deterministic within a fixed seed and were verified
  so; comparing two runs without pinning it can show spurious diffs.
