# rank_candidates Level-Gap Ranking Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace token-overlap scoring in `rank_candidates` with hierarchy-aware level-gap ranking, and surface unresolvable ties as `chain_amb`/`single_amb` with a side file of tied candidates.

**Architecture:** `rank_candidates` scores each candidate by `(level_gap_from_parent, -population)` and returns scores alongside UUIDs. A `detect_tie` helper checks if the top candidates share identical scores. `match_entry` tracks the parent set's jurisdiction level through the chain walk and routes ties to new `chain_amb`/`single_amb` match types. Tied candidate details are collected in memory and written to a separate TSV at the end of the run.

**Tech Stack:** Python 3, pytest, dataclasses, csv

---

### Task 1: Rewrite `rank_candidates`

**Files:**
- Modify: `rtl_matcher.py:707-724`
- Test: `test_rtl_matcher.py`

- [ ] **Step 1: Write failing tests for the new `rank_candidates`**

Add to `test_rtl_matcher.py`:

```python
from rtl_matcher import rank_candidates

class TestRankCandidates:
    def test_single_candidate_returns_it(self):
        auth_cache = {
            'aaa': make_auth_record_full('aaa', level='6', population='300000'),
        }
        result = rank_candidates(['aaa'], auth_cache, parent_level=8)
        assert len(result) == 1
        assert result[0][0] == 'aaa'

    def test_smaller_level_gap_wins(self):
        auth_cache = {
            'state': make_auth_record_full('state', level='6', population='100000'),
            'city': make_auth_record_full('city', level='4', population='500000'),
        }
        result = rank_candidates(['state', 'city'], auth_cache, parent_level=8)
        assert result[0][0] == 'state'
        assert result[1][0] == 'city'

    def test_same_gap_higher_pop_wins(self):
        auth_cache = {
            'big': make_auth_record_full('big', level='6', population='500000'),
            'small': make_auth_record_full('small', level='6', population='10000'),
        }
        result = rank_candidates(['big', 'small'], auth_cache, parent_level=8)
        assert result[0][0] == 'big'
        assert result[1][0] == 'small'

    def test_parent_level_none_sorts_by_pop_only(self):
        auth_cache = {
            'high_pop': make_auth_record_full('high_pop', level='4', population='900000'),
            'low_pop': make_auth_record_full('low_pop', level='6', population='100'),
        }
        result = rank_candidates(['high_pop', 'low_pop'], auth_cache, parent_level=None)
        assert result[0][0] == 'high_pop'

    def test_missing_level_treated_as_zero(self):
        rec_no_level = make_auth_record_full('no_level', level='', population='50000')
        auth_cache = {
            'no_level': rec_no_level,
            'normal': make_auth_record_full('normal', level='6', population='50000'),
        }
        result = rank_candidates(['no_level', 'normal'], auth_cache, parent_level=8)
        assert result[0][0] == 'normal'

    def test_empty_candidates_returns_empty(self):
        result = rank_candidates([], {}, parent_level=8)
        assert result == []

    def test_returns_score_tuples(self):
        auth_cache = {
            'aaa': make_auth_record_full('aaa', level='6', population='300000'),
        }
        result = rank_candidates(['aaa'], auth_cache, parent_level=8)
        uuid, score = result[0]
        assert uuid == 'aaa'
        assert score == (2, -300000)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/natelemonnier/storied/code/anaconda-2 && python -m pytest test_rtl_matcher.py::TestRankCandidates -v`
Expected: FAIL — `rank_candidates` has wrong signature (missing `parent_level`, has `original`)

- [ ] **Step 3: Implement new `rank_candidates`**

Replace lines 707-724 in `rtl_matcher.py` with:

```python
def rank_candidates(candidates, auth_cache, parent_level):
    """Rank candidates by level gap from parent anchor, then population.

    Returns list of (uuid, score) tuples sorted best-first.
    score is (level_gap, neg_population) — lower is better on both axes.
    When parent_level is None (single_term case), ranks by population only.
    """
    if not candidates:
        return []

    def score(uuid):
        rec = auth_cache.get(uuid, {})
        pop = get_population(rec)
        if parent_level is None:
            return (0, -pop)
        try:
            level = int(field_str(rec, 'Level'))
        except (ValueError, TypeError):
            level = 0
        gap = abs(parent_level - level)
        return (gap, -pop)

    scored = [(uuid, score(uuid)) for uuid in candidates]
    scored.sort(key=lambda x: x[1])
    return scored
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/natelemonnier/storied/code/anaconda-2 && python -m pytest test_rtl_matcher.py::TestRankCandidates -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
cd /Users/natelemonnier/storied/code/anaconda-2
git add rtl_matcher.py test_rtl_matcher.py
git commit -m "feat: replace token-overlap scoring with level-gap ranking in rank_candidates"
```

---

### Task 2: Add `detect_tie` helper

**Files:**
- Modify: `rtl_matcher.py` (add after `rank_candidates`)
- Test: `test_rtl_matcher.py`

- [ ] **Step 1: Write failing tests for `detect_tie`**

Add to `test_rtl_matcher.py`:

```python
from rtl_matcher import detect_tie

class TestDetectTie:
    def test_empty_list_returns_no_winner(self):
        winner, tied = detect_tie([])
        assert winner is None
        assert tied == []

    def test_single_candidate_returns_winner(self):
        winner, tied = detect_tie([('aaa', (2, -300000))])
        assert winner == 'aaa'
        assert tied == []

    def test_different_scores_returns_winner(self):
        ranked = [('better', (2, -300000)), ('worse', (4, -900000))]
        winner, tied = detect_tie(ranked)
        assert winner == 'better'
        assert tied == []

    def test_identical_scores_returns_tie(self):
        ranked = [('a', (2, -300000)), ('b', (2, -300000))]
        winner, tied = detect_tie(ranked)
        assert winner is None
        assert set(tied) == {'a', 'b'}

    def test_three_candidates_two_tied_at_top(self):
        ranked = [('a', (2, -100)), ('b', (2, -100)), ('c', (4, -500))]
        winner, tied = detect_tie(ranked)
        assert winner is None
        assert set(tied) == {'a', 'b'}

    def test_three_candidates_all_tied(self):
        ranked = [('a', (2, -100)), ('b', (2, -100)), ('c', (2, -100))]
        winner, tied = detect_tie(ranked)
        assert winner is None
        assert set(tied) == {'a', 'b', 'c'}

    def test_same_gap_different_pop_not_tied(self):
        ranked = [('big', (2, -500000)), ('small', (2, -100))]
        winner, tied = detect_tie(ranked)
        assert winner == 'big'
        assert tied == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/natelemonnier/storied/code/anaconda-2 && python -m pytest test_rtl_matcher.py::TestDetectTie -v`
Expected: FAIL — `detect_tie` does not exist yet

- [ ] **Step 3: Implement `detect_tie`**

Add after `rank_candidates` in `rtl_matcher.py`:

```python
def detect_tie(ranked_with_scores):
    """Check if top candidates in a ranked list share the same score.

    Returns (winner_uuid_or_None, tied_uuids).
    If tied: winner is None, tied_uuids contains all candidates sharing the top score.
    If not tied: winner is the top candidate, tied_uuids is empty.
    """
    if not ranked_with_scores:
        return (None, [])
    if len(ranked_with_scores) == 1:
        return (ranked_with_scores[0][0], [])

    top_score = ranked_with_scores[0][1]
    tied = [uuid for uuid, s in ranked_with_scores if s == top_score]

    if len(tied) > 1:
        return (None, tied)
    return (ranked_with_scores[0][0], [])
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/natelemonnier/storied/code/anaconda-2 && python -m pytest test_rtl_matcher.py::TestDetectTie -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
cd /Users/natelemonnier/storied/code/anaconda-2
git add rtl_matcher.py test_rtl_matcher.py
git commit -m "feat: add detect_tie helper for identifying unresolvable ranking ties"
```

---

### Task 3: Update `match_entry` with parent-level tracking and tie detection

**Files:**
- Modify: `rtl_matcher.py:727-788` (`MatchResult` and `match_entry`)
- Test: `test_rtl_matcher.py`

- [ ] **Step 1: Write failing tests for `match_entry` tie detection**

These tests need a `name_cache` and `auth_cache` with a parent-child hierarchy, plus a mock client. Add to `test_rtl_matcher.py`:

```python
from rtl_matcher import match_entry


def build_hierarchy_caches():
    """Build caches for: USA (level 8) -> FL-state (level 6) and FL-city (level 4, PR).
    Original input: "Mount Dora, Florida, United States of America"
    Mount Dora is not in name_cache, so it gets skipped.
    """
    auth_cache = {
        'usa-1': make_auth_record_full(
            'usa-1', level='8', name='United States of America',
            population='330000000'),
        'fl-state': make_auth_record_full(
            'fl-state', parent_uuid='usa-1', level='6', name='Florida',
            population='22000000'),
        'fl-city': make_auth_record_full(
            'fl-city', parent_uuid='pr-1', level='4', name='Florida',
            population='9000'),
        'pr-1': make_auth_record_full(
            'pr-1', parent_uuid='usa-1', level='7', name='Puerto Rico',
            population='3200000'),
    }
    name_cache = {
        'united states of america': {'usa-1'},
        'florida': {'fl-state', 'fl-city'},
    }
    return name_cache, auth_cache


def build_tied_hierarchy_caches():
    """Two Floridas at the same level and same population under USA."""
    auth_cache = {
        'usa-1': make_auth_record_full(
            'usa-1', level='8', name='United States of America',
            population='330000000'),
        'fl-a': make_auth_record_full(
            'fl-a', parent_uuid='usa-1', level='6', name='Florida',
            population='0'),
        'fl-b': make_auth_record_full(
            'fl-b', parent_uuid='usa-1', level='6', name='Florida',
            population='0'),
    }
    name_cache = {
        'united states of america': {'usa-1'},
        'florida': {'fl-a', 'fl-b'},
    }
    return name_cache, auth_cache


class TestMatchEntryTieDetection:
    def test_chain_verified_picks_better_level_gap(self):
        name_cache, auth_cache = build_hierarchy_caches()
        client = MagicMock()
        client.find.return_value = []
        terms = ['Mount Dora', 'Florida', 'United States of America']
        result = match_entry(terms, name_cache, auth_cache, client,
                             'Mount Dora, Florida, United States of America')
        assert result.match_type == 'chain_verified'
        assert result.candidate_ids == ['fl-state']
        assert result.tied_ids == []

    def test_chain_verified_tie_produces_chain_amb(self):
        name_cache, auth_cache = build_tied_hierarchy_caches()
        client = MagicMock()
        client.find.return_value = []
        terms = ['Florida', 'United States of America']
        result = match_entry(terms, name_cache, auth_cache, client,
                             'Florida, United States of America')
        assert result.match_type == 'chain_amb'
        assert result.candidate_ids == []
        assert set(result.tied_ids) == {'fl-a', 'fl-b'}

    def test_single_term_no_tie_returns_winner(self):
        auth_cache = {
            'big': make_auth_record_full('big', level='6', population='500000'),
            'small': make_auth_record_full('small', level='6', population='100'),
        }
        name_cache = {'florida': {'big', 'small'}}
        client = MagicMock()
        terms = ['Florida']
        result = match_entry(terms, name_cache, auth_cache, client, 'Florida')
        assert result.match_type == 'single_term'
        assert result.candidate_ids == ['big']

    def test_single_term_tie_produces_single_amb(self):
        auth_cache = {
            'a': make_auth_record_full('a', level='6', population='0'),
            'b': make_auth_record_full('b', level='6', population='0'),
        }
        name_cache = {'florida': {'a', 'b'}}
        client = MagicMock()
        terms = ['Florida']
        result = match_entry(terms, name_cache, auth_cache, client, 'Florida')
        assert result.match_type == 'single_amb'
        assert result.candidate_ids == []
        assert set(result.tied_ids) == {'a', 'b'}

    def test_parent_only_unchanged(self):
        """parent_only results pass through to resolve_parent_only in main(),
        so match_entry should still return candidate_ids for it."""
        auth_cache = {
            'usa-1': make_auth_record_full(
                'usa-1', level='8', name='United States of America',
                population='330000000'),
        }
        name_cache = {
            'united states of america': {'usa-1'},
        }
        client = MagicMock()
        client.find.return_value = []
        terms = ['Springfield', 'United States of America']
        result = match_entry(terms, name_cache, auth_cache, client,
                             'Springfield, United States of America')
        assert result.match_type == 'parent_only'
        assert 'usa-1' in result.candidate_ids
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/natelemonnier/storied/code/anaconda-2 && python -m pytest test_rtl_matcher.py::TestMatchEntryTieDetection -v`
Expected: FAIL — `match_entry` still uses old `rank_candidates` signature, `MatchResult` has no `tied_ids`

- [ ] **Step 3: Add `_get_parent_level` helper**

Add before `rank_candidates` in `rtl_matcher.py`:

```python
def _get_parent_level(confirmed_set, auth_cache):
    """Extract the jurisdiction Level from the first candidate with a valid level."""
    for uid in confirmed_set:
        rec = auth_cache.get(uid, {})
        try:
            return int(field_str(rec, 'Level'))
        except (ValueError, TypeError):
            continue
    return None
```

- [ ] **Step 4: Add `tied_ids` field to `MatchResult`**

Update the dataclass at line 727:

```python
@dataclass
class MatchResult:
    candidate_ids: list = field(default_factory=list)
    depth: int = 0
    match_type: str = 'no_terms'
    skipped_count: int = 0
    skipped_terms: str = ''
    tied_ids: list = field(default_factory=list)
```

- [ ] **Step 5: Update `match_entry`**

Replace `match_entry` (lines 736-788) with:

```python
def match_entry(terms, name_cache, auth_cache, client, original):
    """Run the right-to-left matching algorithm on a single place string.

    Match types returned:
      - chain_verified: multiple terms connected through the hierarchy
      - chain_amb: chain verified but top candidates tied on level gap + population
      - single_term: only one term in the input, matched directly
      - single_amb: single term but top candidates tied on population
      - parent_only: rightmost term matched but no children verified against it
      - no_auth_match: rightmost term had no candidates in name_cache
      - no_terms: input was empty or whitespace-only
    """
    stripped = [t.strip() for t in terms if t.strip()]
    if not stripped:
        return MatchResult()

    right_to_left = list(reversed(stripped))

    parent_ids = name_cache.get(right_to_left[0].lower(), set())
    if not parent_ids:
        return MatchResult(match_type='no_auth_match')

    if len(right_to_left) == 1:
        ranked = rank_candidates(list(parent_ids), auth_cache, None)
        winner, tied = detect_tie(ranked)
        if tied:
            return MatchResult([], depth=1, match_type='single_amb', tied_ids=tied)
        ids = [winner] if winner else []
        return MatchResult(ids, depth=1, match_type='single_term')

    confirmed = parent_ids
    depth = 1
    skipped = []
    parent_level_for_ranking = None

    for i in range(1, len(right_to_left)):
        child_ids = name_cache.get(right_to_left[i].lower(), set())
        if not child_ids:
            skipped.append(right_to_left[i])
            continue

        if len(child_ids) > 50:
            print(f"    term '{right_to_left[i]}': {len(child_ids)} candidates, prefetching...", flush=True)
        _prefetch_missing_parents(child_ids, auth_cache, client)
        verified = {
            candidate_id for candidate_id in child_ids
            if walk_up_chain(candidate_id, confirmed, auth_cache, client)
        }

        if verified:
            parent_level_for_ranking = _get_parent_level(confirmed, auth_cache)
            confirmed = verified
            depth += 1
        else:
            skipped.append(right_to_left[i])

    skip_count = len(skipped)
    skip_str = '; '.join(skipped)

    if depth > 1:
        ranked = rank_candidates(list(confirmed), auth_cache, parent_level_for_ranking)
        winner, tied = detect_tie(ranked)
        if tied:
            return MatchResult([], depth, 'chain_amb', skip_count, skip_str, tied)
        ids = [winner] if winner else []
        return MatchResult(ids, depth, 'chain_verified', skip_count, skip_str)

    # parent_only: pass UUIDs through for resolve_parent_only in main()
    ranked = rank_candidates(list(confirmed), auth_cache, None)
    ids = [uuid for uuid, _ in ranked]
    return MatchResult(ids, depth, 'parent_only', skip_count, skip_str)
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd /Users/natelemonnier/storied/code/anaconda-2 && python -m pytest test_rtl_matcher.py::TestMatchEntryTieDetection -v`
Expected: All PASS

- [ ] **Step 7: Run full test suite to check for regressions**

Run: `cd /Users/natelemonnier/storied/code/anaconda-2 && python -m pytest test_rtl_matcher.py -v`
Expected: All PASS (except pre-existing `test_batches_large_sets` failure)

- [ ] **Step 8: Commit**

```bash
cd /Users/natelemonnier/storied/code/anaconda-2
git add rtl_matcher.py test_rtl_matcher.py
git commit -m "feat: wire level-gap ranking and tie detection into match_entry

Add _get_parent_level helper, tied_ids to MatchResult, chain_amb/single_amb
match types. match_entry now tracks parent level through the chain walk and
uses detect_tie to identify unresolvable ties."
```

---

### Task 4: Side file output and summary updates

**Files:**
- Modify: `rtl_matcher.py:58-62` (constants), `rtl_matcher.py:814-818` (I/O helpers), `rtl_matcher.py:821-839` (summary), `rtl_matcher.py:900-962` (main)

- [ ] **Step 1: Add tie output constants and writer**

Add `TIE_OUTPUT_FIELDS` after `OUTPUT_FIELDS` at line 62:

```python
TIE_OUTPUT_FIELDS = [
    'original', 'guid', 'frequency', 'match_type', 'match_depth',
    'authority_id', 'authority_name', 'type_ahead', 'level', 'jurisdiction',
]
```

Add `write_ties` after `write_results`:

```python
def write_ties(ties, path):
    with open(path, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=TIE_OUTPUT_FIELDS, delimiter='\t')
        writer.writeheader()
        writer.writerows(ties)
```

- [ ] **Step 2: Update `print_summary` to include new match types**

Replace the match_type list on line 829:

```python
for match_type in ['chain_verified', 'chain_amb', 'single_term', 'single_amb',
                   'parent_resolved', 'parent_only', 'parent_amb',
                   'no_auth_match', 'no_terms']:
```

- [ ] **Step 3: Update `main()` to collect ties and write side file**

Add tie output path derivation after the `OUTPUT` constant at line 51:

```python
_base, _ext = os.path.splitext(OUTPUT)
TIE_OUTPUT = os.environ.get('RTL_TIE_OUTPUT', f"{_base}_ties{_ext}")
```

In `main()`, initialize `ties = []` before the matching loop (before line 902).

After the `resolve_parent_only` block (after line 926, inside the loop), add tie collection:

```python
if match.match_type in ('chain_amb', 'single_amb') and match.tied_ids:
    for tid in match.tied_ids:
        rec = auth_cache.get(tid, {})
        ties.append({
            'original': place,
            'guid': guid,
            'frequency': frequency,
            'match_type': match.match_type,
            'match_depth': match.depth,
            'authority_id': tid,
            'authority_name': rec.get('Auth_Place_Name', ''),
            'type_ahead': rec.get('Type_Ahead_Value', ''),
            'level': rec.get('Level', ''),
            'jurisdiction': rec.get('Jurisdiction', ''),
        })
```

After `write_results(results, OUTPUT)` (line 961), add:

```python
if ties:
    write_ties(ties, TIE_OUTPUT)
    print(f"  Wrote {len(ties)} tied candidate rows to {TIE_OUTPUT}")
```

- [ ] **Step 4: Update module docstring**

Replace the ranking description in the module docstring (line 32-33) from:

```
    When multiple candidates survive, rank by token overlap between the
    original string and each candidate's Type_Ahead_Value field.
```

to:

```
    When multiple candidates survive, rank by jurisdiction level gap from
    the parent anchor (smaller gap = more direct child = better fit),
    with population as a secondary tiebreaker. Unresolvable ties are
    written to a separate side file for QA review.
```

- [ ] **Step 5: Run full test suite**

Run: `cd /Users/natelemonnier/storied/code/anaconda-2 && python -m pytest test_rtl_matcher.py -v`
Expected: All PASS (except pre-existing `test_batches_large_sets` failure)

- [ ] **Step 6: Commit**

```bash
cd /Users/natelemonnier/storied/code/anaconda-2
git add rtl_matcher.py
git commit -m "feat: add tie side file output and update summary for chain_amb/single_amb"
```

---

### Task 5: Update `rank_candidates` import in `match_entry` docstring reference

**Files:**
- Modify: `rtl_matcher.py`

- [ ] **Step 1: Verify the module docstring, `rank_candidates` docstring, and `match_entry` docstring all reference the new ranking approach consistently**

Read through each docstring and confirm no references to "token overlap" or the `original` parameter remain. Fix any inconsistencies.

- [ ] **Step 2: Run full test suite one final time**

Run: `cd /Users/natelemonnier/storied/code/anaconda-2 && python -m pytest test_rtl_matcher.py -v`
Expected: All PASS (except pre-existing `test_batches_large_sets` failure)

- [ ] **Step 3: Final commit if any docstring fixes were needed**

```bash
cd /Users/natelemonnier/storied/code/anaconda-2
git add rtl_matcher.py
git commit -m "docs: remove stale token-overlap references from docstrings"
```
