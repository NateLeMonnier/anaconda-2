# RTL Level Preference Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Leafprint-style level/population disambiguation to parent_only results in rtl_matcher.py

**Architecture:** New `resolve_parent_only()` function, called from main loop after `match_entry` returns parent_only. Partitions candidates into low (L4-) vs high (L5+), applies Leafprint population rules to low group, escalates to high group when low can't resolve. All changes tagged `RTL-LEVEL-PREF`.

**Tech Stack:** Python 3.11, pytest, unittest.mock

---

### Task 1: Add `get_population` helper and `resolve_parent_only` function

**Files:**
- Modify: `rtl_matcher.py:529` (insert after `walk_up_chain`, before `rank_candidates`)
- Test: `test_rtl_matcher.py` (append new test class)

- [ ] **Step 1: Write failing tests for `resolve_parent_only`**

Append to `test_rtl_matcher.py`:

```python
from rtl_matcher import resolve_parent_only


def make_auth_record_full(uuid, parent_uuid=None, name="Place", level="4",
                          population="", jurisdiction=""):
    return {
        'UUID': uuid,
        'Parent_UUID': parent_uuid or '',
        'Auth_Place_Name': name,
        'Level': level,
        'Population': population,
        'Jurisdiction': jurisdiction,
        'Type_Ahead_Value': '',
    }


class TestResolveParentOnly:
    """Tests for Leafprint-style disambiguation of parent_only candidates."""

    def test_single_candidate_returns_it(self):
        auth_cache = {'state-uuid': make_auth_record_full('state-uuid', level='6', name='Ohio')}
        winner, status = resolve_parent_only(['state-uuid'], auth_cache, MagicMock())
        assert winner == 'state-uuid'
        assert status == 'parent_resolved'

    def test_high_level_preferred_over_low_when_low_pops_zero(self):
        """'Ohio' -> L6 state wins over L4 cities with zero population."""
        auth_cache = {
            'state': make_auth_record_full('state', level='6', name='Ohio'),
            'city-ga': make_auth_record_full('city-ga', level='4', name='Ohio'),
            'city-co': make_auth_record_full('city-co', level='4', name='Ohio'),
        }
        winner, status = resolve_parent_only(['state', 'city-ga', 'city-co'], auth_cache, MagicMock())
        assert winner == 'state'
        assert status == 'parent_resolved'

    def test_low_pop_over_50k_rest_zero_wins(self):
        """L4 candidate w/ pop >= 50k and all others zero -> use it."""
        auth_cache = {
            'big-city': make_auth_record_full('big-city', level='4', population='75000'),
            'tiny-city': make_auth_record_full('tiny-city', level='4', population='0'),
            'state': make_auth_record_full('state', level='6'),
        }
        winner, status = resolve_parent_only(['big-city', 'tiny-city', 'state'], auth_cache, MagicMock())
        assert winner == 'big-city'
        assert status == 'parent_resolved'

    def test_low_pop_under_50k_escalates_to_high(self):
        """L4 candidates all under 50k -> escalate to L5+ candidate."""
        auth_cache = {
            'small-city': make_auth_record_full('small-city', level='4', population='5000'),
            'state': make_auth_record_full('state', level='6'),
        }
        winner, status = resolve_parent_only(['small-city', 'state'], auth_cache, MagicMock())
        assert winner == 'state'
        assert status == 'parent_resolved'

    def test_low_pop_5x_rule(self):
        """Highest L4 pop > 5x next L4 -> use it even if others nonzero."""
        auth_cache = {
            'boston-ma': make_auth_record_full('boston-ma', level='4', population='675000'),
            'boston-ny': make_auth_record_full('boston-ny', level='4', population='2000'),
            'state': make_auth_record_full('state', level='6'),
        }
        winner, status = resolve_parent_only(['boston-ma', 'boston-ny', 'state'], auth_cache, MagicMock())
        assert winner == 'boston-ma'
        assert status == 'parent_resolved'

    def test_low_pop_close_escalates_to_high(self):
        """Two L4 candidates with similar pop -> escalate to L5+."""
        auth_cache = {
            'city-a': make_auth_record_full('city-a', level='4', population='60000'),
            'city-b': make_auth_record_full('city-b', level='4', population='55000'),
            'state': make_auth_record_full('state', level='6'),
        }
        winner, status = resolve_parent_only(['city-a', 'city-b', 'state'], auth_cache, MagicMock())
        assert winner == 'state'
        assert status == 'parent_resolved'

    def test_no_high_and_low_ambiguous_returns_amb(self):
        """Multiple L4 candidates, similar pop, no L5+ -> Amb."""
        auth_cache = {
            'city-a': make_auth_record_full('city-a', level='4', population='60000'),
            'city-b': make_auth_record_full('city-b', level='4', population='55000'),
        }
        winner, status = resolve_parent_only(['city-a', 'city-b'], auth_cache, MagicMock())
        assert winner is None
        assert status == 'amb'

    def test_no_low_single_high_returns_it(self):
        """Only one L5+ candidate, no L4 -> return it."""
        auth_cache = {
            'state': make_auth_record_full('state', level='6', name='Ohio'),
        }
        winner, status = resolve_parent_only(['state'], auth_cache, MagicMock())
        assert winner == 'state'
        assert status == 'parent_resolved'

    def test_multiple_high_amb_when_both_populated(self):
        """Two L6 candidates both with pop >= 50k -> Amb."""
        auth_cache = {
            'georgia-us': make_auth_record_full('georgia-us', level='6', population='10700000'),
            'georgia-country': make_auth_record_full('georgia-country', level='8', population='3700000'),
        }
        winner, status = resolve_parent_only(['georgia-us', 'georgia-country'], auth_cache, MagicMock())
        assert winner is None
        assert status == 'amb'

    def test_multiple_high_5x_rule(self):
        """Two L5+ candidates, one pop > 5x other -> use larger."""
        auth_cache = {
            'big-state': make_auth_record_full('big-state', level='6', population='10000000'),
            'tiny-county': make_auth_record_full('tiny-county', level='5', population='500'),
        }
        winner, status = resolve_parent_only(['big-state', 'tiny-county'], auth_cache, MagicMock())
        assert winner == 'big-state'
        assert status == 'parent_resolved'

    def test_multiple_high_all_zero_amb(self):
        """Multiple L5+ candidates, all pop zero -> Amb."""
        auth_cache = {
            'state-a': make_auth_record_full('state-a', level='6'),
            'state-b': make_auth_record_full('state-b', level='6'),
        }
        winner, status = resolve_parent_only(['state-a', 'state-b'], auth_cache, MagicMock())
        assert winner is None
        assert status == 'amb'

    def test_missing_population_treated_as_zero(self):
        """Records with no Population field treated as pop 0."""
        rec = make_auth_record_full('city', level='4', name='Test')
        del rec['Population']
        auth_cache = {
            'city': rec,
            'state': make_auth_record_full('state', level='6'),
        }
        winner, status = resolve_parent_only(['city', 'state'], auth_cache, MagicMock())
        assert winner == 'state'
        assert status == 'parent_resolved'

    def test_low_all_zero_no_high_returns_amb(self):
        """Multiple L4 candidates all zero pop, no L5+ -> Amb."""
        auth_cache = {
            'city-a': make_auth_record_full('city-a', level='4'),
            'city-b': make_auth_record_full('city-b', level='4'),
        }
        winner, status = resolve_parent_only(['city-a', 'city-b'], auth_cache, MagicMock())
        assert winner is None
        assert status == 'amb'
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/natelemonnier/storied/code/tmp && python -m pytest test_rtl_matcher.py::TestResolveParentOnly -v`
Expected: ImportError — `resolve_parent_only` doesn't exist yet.

- [ ] **Step 3: Write `get_population` and `resolve_parent_only` in rtl_matcher.py**

Insert after `walk_up_chain` (line 528), before `rank_candidates` (line 531):

```python
# --- BEGIN RTL-LEVEL-PREF ---

def get_population(auth_record):
    """Extract population as int from an authority record. Missing/empty/non-numeric -> 0."""
    raw = (auth_record.get('Population') or '').strip()
    try:
        return int(float(raw))
    except (ValueError, TypeError):
        return 0


def _disambiguate_by_population(candidates, auth_cache):
    """Apply Leafprint population rules to a set of same-tier candidates.

    Returns (winner_uuid, 'parent_resolved') or (None, 'amb').
    """
    if len(candidates) == 1:
        return candidates[0], 'parent_resolved'

    pops = [(uid, get_population(auth_cache.get(uid, {}))) for uid in candidates]
    pops.sort(key=lambda x: x[1], reverse=True)

    all_zero = all(p == 0 for _, p in pops)
    if all_zero:
        return None, 'amb'

    highest_uid, highest_pop = pops[0]
    second_pop = pops[1][1] if len(pops) > 1 else 0

    if highest_pop >= 50_000 and second_pop == 0:
        return highest_uid, 'parent_resolved'

    if highest_pop >= 50_000 and second_pop >= 50_000:
        return None, 'amb'

    if second_pop > 0 and highest_pop > 5 * second_pop:
        return highest_uid, 'parent_resolved'

    return None, 'amb'


def resolve_parent_only(candidate_ids, auth_cache, client):
    """Disambiguate parent_only candidates using Leafprint level/population rules.

    Partitions candidates into low (level <= 4) and high (level >= 5). Tries to
    resolve among low candidates first using population thresholds; escalates to
    the high group when low candidates can't be disambiguated.

    Returns (winner_uuid, 'parent_resolved') or (None, 'amb').
    """
    if not candidate_ids:
        return None, 'amb'

    if len(candidate_ids) == 1:
        return candidate_ids[0], 'parent_resolved'

    # Ensure all candidates are in auth_cache
    missing = [uid for uid in candidate_ids if uid not in auth_cache]
    if missing:
        for i in range(0, len(missing), BATCH):
            batch = missing[i:i + BATCH]
            query = [{"UUID": f"=={uid}"} for uid in batch]
            for rec in client.find("Authority_Place", query):
                fd = rec['fieldData']
                uid = field_str(fd, 'UUID')
                if uid:
                    auth_cache[uid] = fd

    # Partition by level
    low = []
    high = []
    for uid in candidate_ids:
        rec = auth_cache.get(uid, {})
        try:
            level = int(rec.get('Level', 0))
        except (ValueError, TypeError):
            level = 0
        if level >= 5:
            high.append(uid)
        else:
            low.append(uid)

    # If no low candidates, resolve among high group directly
    if not low:
        return _disambiguate_by_population(high, auth_cache)

    # Low candidates exist: apply Leafprint L4 rules
    low_pops = [(uid, get_population(auth_cache.get(uid, {}))) for uid in low]
    low_pops.sort(key=lambda x: x[1], reverse=True)
    all_low_zero = all(p == 0 for _, p in low_pops)
    highest_low_uid, highest_low_pop = low_pops[0]
    second_low_pop = low_pops[1][1] if len(low_pops) > 1 else 0

    # 3a: all low pops zero, high exists -> escalate
    if all_low_zero and high:
        return _disambiguate_by_population(high, auth_cache)

    # 3b: all low pops zero, no high -> amb
    if all_low_zero and not high:
        return None, 'amb'

    # 3c: highest low >= 50k, all others zero -> use it
    if highest_low_pop >= 50_000 and second_low_pop == 0:
        return highest_low_uid, 'parent_resolved'

    # 3d: highest low < 50k, high exists -> escalate
    if highest_low_pop < 50_000 and high:
        return _disambiguate_by_population(high, auth_cache)

    # 3e: highest low > 5x second -> use it
    if second_low_pop > 0 and highest_low_pop > 5 * second_low_pop:
        return highest_low_uid, 'parent_resolved'

    # 3f/3g: otherwise escalate or amb
    if high:
        return _disambiguate_by_population(high, auth_cache)
    return None, 'amb'

# --- END RTL-LEVEL-PREF ---
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/natelemonnier/storied/code/tmp && python -m pytest test_rtl_matcher.py::TestResolveParentOnly -v`
Expected: all 13 tests PASS.

- [ ] **Step 5: Run existing tests to verify no regression**

Run: `cd /Users/natelemonnier/storied/code/tmp && python -m pytest test_rtl_matcher.py -v`
Expected: all tests PASS (existing `TestPrefetchParentChains` + new `TestResolveParentOnly`).

- [ ] **Step 6: Commit**

```bash
git add rtl_matcher.py test_rtl_matcher.py
git commit -m "feat: add resolve_parent_only with Leafprint level/population rules [RTL-LEVEL-PREF]"
```

---

### Task 2: Integrate `resolve_parent_only` into main loop

**Files:**
- Modify: `rtl_matcher.py:650` (print_summary — add `parent_resolved` to type list)
- Modify: `rtl_matcher.py:724-752` (main loop — add callsite after match_entry)

- [ ] **Step 1: Write failing test for integration behavior**

Append to `test_rtl_matcher.py`:

```python
from rtl_matcher import match_entry, MatchResult


class TestMainLoopIntegration:
    """Verify resolve_parent_only is called for parent_only results."""

    def test_parent_only_with_mixed_levels_resolves_to_high(self):
        """Simulate 'route 3, Ohio' — Ohio has L6 state + L4 cities, all L4 pop zero."""
        name_cache = {
            'ohio': {'state-uuid', 'city-ga-uuid'},
            'route 3': set(),
        }
        auth_cache = {
            'state-uuid': make_auth_record_full('state-uuid', level='6', name='Ohio',
                                                 population='11800000'),
            'city-ga-uuid': make_auth_record_full('city-ga-uuid', level='4', name='Ohio',
                                                   population='0'),
        }
        client = MagicMock()
        match = match_entry(['route 3', 'Ohio'], name_cache, auth_cache, client, 'route 3, Ohio')
        assert match.match_type == 'parent_only'
        assert 'state-uuid' in match.candidate_ids
        assert 'city-ga-uuid' in match.candidate_ids
```

Note: This test confirms `match_entry` still returns `parent_only` with both candidates. The integration in `main()` calls `resolve_parent_only` on these results — we test that via the output assertions in the next step, not by mocking main.

- [ ] **Step 2: Run test to verify it passes** (this tests current behavior, should pass already)

Run: `cd /Users/natelemonnier/storied/code/tmp && python -m pytest test_rtl_matcher.py::TestMainLoopIntegration -v`
Expected: PASS.

- [ ] **Step 3: Add callsite in main loop and update summary**

In `rtl_matcher.py`, modify the main loop (around line 724). Replace the block from `match = match_entry(...)` through `row['authority_id'] = best_id`:

Find the existing block starting at line 725 and modify it. After `match = match_entry(...)`, add the resolve_parent_only callsite:

```python
        match = match_entry(terms, name_cache, auth_cache, client, place)

        # --- BEGIN RTL-LEVEL-PREF ---
        if match.match_type == 'parent_only' and match.candidate_ids:
            winner, resolution = resolve_parent_only(
                match.candidate_ids, auth_cache, client)
            if resolution == 'parent_resolved':
                match = MatchResult(
                    candidate_ids=[winner],
                    depth=match.depth,
                    match_type='parent_resolved',
                    skipped_count=match.skipped_count,
                    skipped_terms=match.skipped_terms,
                )
            elif resolution == 'amb':
                match = MatchResult(
                    candidate_ids=[],
                    depth=match.depth,
                    match_type='parent_amb',
                    skipped_count=match.skipped_count,
                    skipped_terms=match.skipped_terms,
                )
        # --- END RTL-LEVEL-PREF ---
```

Also update `print_summary` (line 650) to include the new match types. Change:

```python
    for match_type in ['chain_verified', 'single_term', 'parent_only', 'no_auth_match', 'no_terms']:
```

to:

```python
    for match_type in ['chain_verified', 'single_term', 'parent_resolved', 'parent_only', 'parent_amb', 'no_auth_match', 'no_terms']:  # RTL-LEVEL-PREF: added parent_resolved, parent_amb
```

- [ ] **Step 4: Run all tests**

Run: `cd /Users/natelemonnier/storied/code/tmp && python -m pytest test_rtl_matcher.py -v`
Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add rtl_matcher.py test_rtl_matcher.py
git commit -m "feat: integrate resolve_parent_only into main loop [RTL-LEVEL-PREF]"
```

---

### Task 3: Manual validation against known spurious cases

**Files:**
- None modified — read-only validation

- [ ] **Step 1: Run rtl_matcher against snowball4_sample and inspect parent_only -> parent_resolved conversions**

This requires FM credentials and a live connection, so only run if FM is available. Otherwise validate by reviewing the test suite covers all branches from the spec:

Spec coverage check:
- 3a: `test_high_level_preferred_over_low_when_low_pops_zero` ✓
- 3b: `test_low_all_zero_no_high_returns_amb` ✓
- 3c: `test_low_pop_over_50k_rest_zero_wins` ✓
- 3d: `test_low_pop_under_50k_escalates_to_high` ✓
- 3e: `test_low_pop_5x_rule` ✓
- 3f: `test_low_pop_close_escalates_to_high` ✓
- 3g: `test_no_high_and_low_ambiguous_returns_amb` ✓
- Step 4 single high: `test_no_low_single_high_returns_it` ✓
- Step 4 multiple high pop: `test_multiple_high_5x_rule` ✓
- Step 4 multiple high amb: `test_multiple_high_amb_when_both_populated` ✓
- Step 4 multiple high zero: `test_multiple_high_all_zero_amb` ✓
- Missing pop: `test_missing_population_treated_as_zero` ✓
- Single candidate: `test_single_candidate_returns_it` ✓

- [ ] **Step 2: Verify revert path**

Confirm: all new code is between `# --- BEGIN RTL-LEVEL-PREF ---` and `# --- END RTL-LEVEL-PREF ---` markers, plus the one-line summary type list change tagged with `# RTL-LEVEL-PREF`.
