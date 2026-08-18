# anaconda-2 — rtl_matcher

`rtl_matcher.py` resolves the messy place strings that come off historical
records — `home of her daughter, Newport Rd., Uhrichsville`, `near Muskego`,
`LONGMONT CITY, BOULDER, Colorado` — to records in Storied's Place Authority
hierarchy. It walks each string right to left, from the broadest jurisdiction
inward, keeping only candidates whose parent chain connects back to what it has
already confirmed. No LLM anywhere in the path.

**This is the work that matters in this repo.** Everything else here exists
because it was needed to measure the matcher, and is inventoried at the bottom
rather than maintained.

**Status: handed off 2026-08-18.** `main` is green at 429 tests. Read
[`OUTSTANDING.md`](OUTSTANDING.md) before changing ranking or disambiguation —
the largest known defect is a one-row dictionary problem worth 47 points, and the
largest quality item is a quarter of the corpus abstaining for a mechanical
reason.

## Setup

Python 3.12, two third-party packages:

```
pip install -r requirements.txt
```

The matcher takes every path as a flag and reads no network at match time. The
one exception is `--dict` with no value, a live Supabase pull that needs
`SUPABASE_PASSWORD` from `--env` or the environment. See `.env.example`.

Runs are deterministic within a fixed `PYTHONHASHSEED`, because several paths
iterate sets of UUID strings. Pin it whenever diffing two runs:
`PYTHONHASHSEED=0 python rtl_matcher.py ...`.

## Running it

Input is a TSV with a required `place` column. `guid` and `frequency` are
optional and carried through to the output.

```
python rtl_matcher.py --input places.tsv --pa pa_export.tsv --mnt mnt_export.tsv

# with Storied's Supabase place dictionary unioned on top of the MNT
python rtl_matcher.py --input places.tsv --pa pa.tsv --mnt mnt.tsv --dict --env .env
python rtl_matcher.py --input places.tsv --pa pa.tsv --mnt mnt.tsv --dict /path/to/tsv_dir
```

Run without flags to be prompted for paths.

| Flag | Effect |
|---|---|
| `--helper-term "Utah, USA"` | Geographic context that biases single-term matches toward candidates under it. Pass `''` to run without one; omitting it prompts. |
| `--dict [DIR]` | Union the Supabase place dictionary into the MNT, adding a per-term frequency prior and an illegible-term stop-list. No value means a live pull. |
| `--no-segment-commaless` | Turn off Phase 0. Reproduces pre-segmentation output for A/B comparison. |
| `--min-level` / `--max-level` | Move the supported jurisdiction range (default 3–10). Matches outside it are reported via `below_supported` and `supported_leaf_id`, not excluded. |
| `--mnt-defects PATH` | Where to accumulate MNT mappings the low-evidence check rejected (default `mnt_defects.tsv`). Global rather than per-run, since these are dictionary rows to fix at the source. |
| `--output-dir` | Root for the dated output folders (default `rtl-outputs/`). |

### The two exports it reads

Both are loaded fully into memory at startup, and neither is in the repo — each
is over 50 MB. Every measurement in `docs/` used these two files:

| Input | Flag | File |
|---|---|---|
| Place Authority | `--pa` | `resources/place-authority-mnt-tsv/PA6_16_2026v77.tsv` |
| Master Normalization Table | `--mnt` | `resources/place-authority-mnt-tsv/Master_Normalization_File_6_16_2026v1.tsv` |

The Supabase dictionary is not optional in practice. Its per-term frequency prior
is the only input to `_disambiguate_by_frequency`, and dropping it cost 18 points
of record accuracy in the one place that was measured.

## How a row is resolved

Four phases.

**Phase 0 — comma-less segmentation.** Split separator-less strings into
hierarchy terms (`Swift Minnesota` → `['Swift', 'Minnesota']`) where the
authority backs every piece. A row whose full string is already an unambiguous
MNT mapping skips straight to the answer.

**Phase 1 — name resolution.** Turn raw terms into candidate authority UUIDs via
the MNT and exact authority names, then widen: abbreviation expansion, name
variants, prefix and suffix transforms, extraction after a spatial preposition,
SymSpell correction at edit distance 1, and a cardinal-prefix strip. Every
candidate records which lookup found it and what string it matched —
`NameCache.origins` at `rtl_matcher.py:216`.

**Phase 2 — authority record caching.** Load the full record for every UUID
found, then walk parent chains until the hierarchy above them is cached.

**Phase 3 — right-to-left matching.** Start at the broadest (rightmost) term and
walk left, keeping only candidates whose parent chain connects back to the
confirmed set. A term that cannot be verified is skipped rather than failing the
row, and a proximity fallback within 50 km recovers likely wrong-county entries.
Survivors are ranked in `rank_candidates` on evidence strength, helper-term
match, and level gap. Population orders the array but never breaks a tie — rows
that stay ambiguous are surfaced with their candidates for QA instead of being
guessed at.

A row that would commit with no chain corroboration — a one-term input, or an
anchor with nothing confirmed to its left — passes one last check. If the string
that reached the authority is a bare appellative rather than a name (`the
village`, `station`, `city`), the row comes back `low_evidence`, claiming nothing
and keeping its candidates visible, which is what stops `Lutheran church in the
village` resolving to The Village, Oklahoma. Rejected mappings that came from the
MNT are logged to `--mnt-defects`. One caveat carries into any work here: the
check's capitalization test is off by default, per `OUTSTANDING.md` §2.2.

## Output

Files land in `<output-dir>/MM-DD/` with an auto-incrementing run number.

| File | Contents |
|---|---|
| `<stem>_NN.tsv` | Results, one row per input |
| `<stem>_NN_ties.tsv` | Tied candidates for ambiguous rows |
| `<stem>_NN_levels.tsv` | One row per level of each winning chain, with the input token and lookup method behind it |
| `<stem>_NN_spelling.tsv` | Corrections applied |
| `<stem>_NN_segments.tsv` | Phase 0 decisions, including rows left unsegmented and why |

`match_type` says which path produced the answer. The ones that commit:
`chain_verified`, `chain_verified_proximity`, `parent_resolved`, `freq_resolved`,
`single_term`, `mnt_full_string`. The ones that decline: `chain_amb`,
`parent_amb`, `single_amb`, `parent_rejected`, `low_evidence`, `no_auth_match`.
Declining is a designed outcome — a matching authority that answers to QA is
better off surfacing candidates than guessing.

`format_readable.py results.tsv` renders one readable block per row, which is
what to hand a reviewer. `format_levels.py` joins results with `_levels.tsv` into
per-level JSON.

## Tests

```
python -m pytest -q                        # 429 tests, about 0.3s
python -m pytest test_rtl_matcher.py -q    # 290 of them, the matcher's own
```

Coverage: comma-less segmentation, parent chain pre-fetching, candidate ranking
and provenance, jurisdiction filtering, tie detection, transforms, spelling
correction, the proximity fallback, parent-only resolution. Tests that drive a
lookup use the `local_pa` fixture, which installs a throwaway `LocalData` as the
module-global `_LOCAL`, since the lookups read their indexes off it rather than
taking them as arguments.

**A green suite is not enough for a behavior change.** The pattern used
throughout: run the same sample at a fixed `PYTHONHASHSEED` before and after,
diff the results TSV, and account for every changed row. A change that moves rows
you cannot explain is not understood yet. The low-evidence work is the worked
example — its acceptance criterion was "exactly 7 rows change, all appellative
anchors," and meeting it surfaced two span-recording defects that no test caught.

## How it performs

Measured without an LLM, against three different label sources. Full numbers and
the caveats attached to each are in `docs/2026-08-12-results-table.md`.

| corpus | what it tests | result |
|---|---|---|
| 200 hand-adjudicated newspaper strings, drawn proportional to frequency | production-shaped input, the corpus it was built for | 90.5% accuracy, 93.7% precision, 51.3% yield |
| 15,796 Leafprint-verified strings, frequency-weighted | generalization against curator labels | 72.3% record accuracy, 89.1% precision |
| 2,500 curated MNT strings, dictionary decontaminated | unseen curated strings | 67.9% record accuracy |

Against the old pipeline in `code/place-normalizer` on identical rows, it commits
to 1,888 of 2,500 strings where the old pipeline commits to 964, at flat
precision — the gain is coverage of the long tail rather than accuracy on what
was already handled. It resolved 15,796 strings in 20.8 seconds single-process,
where the old pipeline's `place_authority_normalizer_parallel` stage ran 2h38m on
the same corpus without producing output.

Quote coverage and precision as a pair. Coverage alone is gamed by guessing,
precision alone by answering less.

## Files

```
rtl_matcher.py            the matcher, 3.6k lines
test_rtl_matcher.py       its tests
format_readable.py        results -> readable text, for reviewers
format_levels.py          results + levels -> per-level JSON
mnt_defects.tsv           MNT mappings the low-evidence check rejected, accumulating
requirements.txt          symspellpy, pytest
OUTSTANDING.md            unfinished work and known defects
docs/                     design docs, measurements, failure analyses
rtl-outputs/              dated run output, gitignored
```

### Also in the repo, not maintained

Kept for reference because it produced the numbers above. Production went a
different direction on measurement, so none of this is the metric of record and
none of it is a dependency of the matcher.

| Path | What it was |
|---|---|
| `eval/` | Record accuracy against curator labels: MNT band sampling, dictionary decontamination, two scorers, a failure taxonomy, and the hand-adjudication sheets behind the 90.5% figure. `eval/README.md` is its runbook. |
| `eval/oldpipe/` | Mirror and output adapter for scoring `code/place-normalizer` on identical inputs. |
| `evaluate_normalizer.py` | Label-free output quality, with optional ground-truth comparison. Useful on corpora with no labels, where the question is resolution rate rather than correctness. |
| `results/` | Older evaluator output, gitignored. |

One piece of `eval/` is worth knowing about even if the rest is ignored:
`eval/build_mnt_holdout.py`. The matcher reads the MNT as its dictionary, so any
evaluation drawn from that table must strip those rows out first or the score is
recall of a lookup table — 86.7% of an MNT-drawn sample hits the full-string fast
path undecontaminated. Anyone measuring this matcher against curator labels needs
that step regardless of which scorer they use.

## Documents

Matcher first:

1. `OUTSTANDING.md` — unfinished work, known defects, what to do first
2. `docs/2026-08-10-mnt-failure-analysis.md` — where the errors are, by class. The most useful document for deciding what to fix
3. `docs/2026-07-28-next-steps.md` — architecture options and the toponym-resolution literature behind them
4. `docs/2026-08-06-cleanup-followups.md` — what the cleanup pass found and deliberately left
5. `docs/superpowers/specs/2026-08-02-low-evidence-gate-design.md` and its plan — the gate that shipped; `is_description` is its predicate
6. `docs/2026-06-02-rtl-level-preference-design.md` / `-plan.md` — superseded, population no longer resolves parent-only matches

Measurement, for when a number needs sourcing:

- `docs/2026-08-12-results-table.md` — every figure in one place with its caveats
- `docs/2026-08-12-presentation-evidence.md` — the evidence pack behind it
- `docs/2026-08-12-snowball2-record-accuracy.md` — the `NULL` defect and the four configurations around it
- `docs/2026-08-12-old-vs-new-pipeline.md` — old against new on identical rows
- `docs/2026-08-08-parent-amb-to-freq-resolved.md` — frequency disambiguation design and results
- `docs/2026-06-13-normalizer-eval-design.md` — `evaluate_normalizer.py` design
