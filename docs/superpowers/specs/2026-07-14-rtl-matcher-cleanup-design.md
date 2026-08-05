# rtl_matcher cleanup — design

Date: 2026-07-14
Status: approved (in conversation)

## Goal

No-regret cleanup of `rtl_matcher.py` on main so the code reads like an
experienced developer wrote and honed it. Future product shape (library,
internal CLI, service) is undecided, so structural bets are deferred:
no package split, no CI, no pyproject.toml, no committed lint config.

## Constraints

- Behavior-preserving except where a diagnosed genuine bug is found.
- Committed behavior is spec: when a test disagrees with code, the test is
  presumed stale unless diagnosis shows a real defect.
- Nothing commits without user review, except the dict-mode parking commit.
- Dict-mode work (uncommitted `--dict` Supabase support) is out of scope,
  parked on branch `wip/dict-mode`.

## Steps

1. Park dict-mode diff on `wip/dict-mode`. (done)
2. Fix test suite. Four `TestResolveParentOnly` tests assert the pre-db103d8
   level-preference behavior that was deliberately removed; rewrite them to
   assert current population-only disambiguation. Diagnose the failing
   spelling-correction test (mock call_count mismatch, likely stale after
   the quoted-name-search change in c7c76e3) and fix test or code
   accordingly, flagging any code fix before commit.
3. Dead code sweep: ruff + vulture + manual read. Unused functions, stale
   scaffolding comments (e.g. RTL-LEVEL-PREF markers), leftover debug paths.
   Ambiguous items listed for user decision, not silently deleted.
4. Consistency pass: one docstring style, consistent naming, coherent
   section headers. No behavior change.
5. README refresh to match current CLI flags, phases, and outputs.
6. Ruff lint fix with default rules; no config file committed.

## Verification

- Full test suite green after every commit.
- One `--local` smoke run on a small input before and after the cleanup,
  outputs diffed to confirm identical behavior.

## Commit discipline

One concern per commit, small diffs, existing `feat:/fix:/docs:` style.
