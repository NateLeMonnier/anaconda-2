# Single-Term Matching Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the RTL matcher's single-term matching path so that (1) cities are preferred over townships/counties unless the input explicitly names that jurisdiction, (2) an optional helper term biases single-term matches toward a geographic region, and (3) single-term L3-L4 matches with >1 post-filter candidate are reclassified as `single_amb`.

**Architecture:** Three changes to `rtl_matcher.py`: a jurisdiction hint dict built during preprocessing, a jurisdiction hard filter added to `rank_candidates`, and a helper term system resolved at startup and applied as a scoring boost in the single-term path. Existing test patterns (mock `auth_cache`, mock `FileMakerClient`) are reused. No new files created.

**Tech Stack:** Python 3.12, pytest, unittest.mock

---

### Task 1: Extract jurisdiction hint detection into a standalone function

**Files:**
- Modify: `rtl_matcher.py:234-265` (extract from `transform_term`)
- Test: `test_rtl_matcher.py`

The jurisdiction hint needs to be detectable independently of `transform_term`'s full pipeline (which strips prefixes, expands abbreviations, etc.). Extract the suffix-matching loop into its own function so it can run on every term at preprocessing time without side effects.

- [ ] **Step 1: Write failing tests for `detect_jurisdiction_hint`**

```python
from rtl_matcher import detect_jurisdiction_hint

class TestDetectJurisdictionHint:
    def test_county_suffix(self):
        assert detect_jurisdiction_hint("Washington County") == "County"

    def test_township_suffix(self):
        assert detect_jurisdiction_hint("Lawrence Township") == "Township"

    def test_twp_abbreviation(self):
        assert detect_jurisdiction_hint("Lawrence Twp") == "Township"
        assert detect_jurisdiction_hint("Lawrence Twp.") == "Township"

    def test_parish_suffix(self):
        assert detect_jurisdiction_hint("Orleans Parish") == "Parish"

    def test_borough_suffix(self):
        assert detect_jurisdiction_hint("Huntingdon Borough") == "Borough"

    def test_co_abbreviation(self):
        assert detect_jurisdiction_hint("Mifflin Co") == "County"
        assert detect_jurisdiction_hint("Mifflin Co.") == "County"

    def test_no_jurisdiction(self):
        assert detect_jurisdiction_hint("Lawrence") is None

    def test_case_insensitive(self):
        assert detect_jurisdiction_hint("washington county") == "County"
        assert detect_jurisdiction_hint("LAWRENCE TOWNSHIP") == "Township"

    def test_city_name_containing_county_word(self):
        # "County" only matches as a trailing suffix, not embedded
        assert detect_jurisdiction_hint("County Line") is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/natelemonnier/storied/code/anaconda-2 && python3 -m pytest test_rtl_matcher.py::TestDetectJurisdictionHint -v`
Expected: FAIL — `ImportError: cannot import name 'detect_jurisdiction_hint'`

- [ ] **Step 3: Implement `detect_jurisdiction_hint`**

Add this function above `transform_term` in `rtl_matcher.py` (around line 234):

```python
def detect_jurisdiction_hint(term):
    """Check if a term contains a jurisdiction suffix (County, Township, etc.).
    Returns the jurisdiction type string if found, None otherwise.
    Does NOT modify the term — detection only."""
    for pattern, jurisdiction_type in JURISDICTION_SUFFIXES:
        if pattern.search(term):
            return jurisdiction_type
    return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/natelemonnier/storied/code/anaconda-2 && python3 -m pytest test_rtl_matcher.py::TestDetectJurisdictionHint -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add rtl_matcher.py test_rtl_matcher.py
git commit -m "feat: extract detect_jurisdiction_hint from transform_term"
```

---

### Task 2: Build jurisdiction hints dict during preprocessing

**Files:**
- Modify: `rtl_matcher.py:867-877` (`parse_entries`), `rtl_matcher.py:921-1057` (`main`)
- Test: `test_rtl_matcher.py`

Run `detect_jurisdiction_hint` on every term during `parse_entries` and return a `jurisdiction_hints` dict alongside `all_terms`. This dict maps lowercased term strings to their detected jurisdiction type. It needs to be passed through to Phase 3.

- [ ] **Step 1: Write failing tests for jurisdiction hints in `parse_entries`**

```python
from rtl_matcher import parse_entries

class TestParseEntriesJurisdictionHints:
    def test_returns_jurisdiction_hints(self):
        entries = [{'place': 'Washington County, Pennsylvania', 'guid': 'g1', 'frequency': '5'}]
        parsed, all_terms, jurisdiction_hints = parse_entries(entries)
        assert jurisdiction_hints['washington county'] == 'County'

    def test_no_hint_for_plain_terms(self):
        entries = [{'place': 'Lawrence, Indiana', 'guid': 'g1', 'frequency': '5'}]
        parsed, all_terms, jurisdiction_hints = parse_entries(entries)
        assert 'lawrence' not in jurisdiction_hints
        assert 'indiana' not in jurisdiction_hints

    def test_multiple_hints(self):
        entries = [
            {'place': 'Bethel Township, Clark County, Ohio', 'guid': 'g1', 'frequency': '3'},
        ]
        parsed, all_terms, jurisdiction_hints = parse_entries(entries)
        assert jurisdiction_hints['bethel township'] == 'Township'
        assert jurisdiction_hints['clark county'] == 'County'
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/natelemonnier/storied/code/anaconda-2 && python3 -m pytest test_rtl_matcher.py::TestParseEntriesJurisdictionHints -v`
Expected: FAIL — `parse_entries` returns a 2-tuple, not 3-tuple

- [ ] **Step 3: Update `parse_entries` to return jurisdiction hints**

Modify `parse_entries` in `rtl_matcher.py`:

```python
def parse_entries(entries):
    """Split each entry's place string into comma/semicolon-separated terms
    and collect the full set of unique terms across all entries for bulk lookup.
    Also detects jurisdiction hints (County, Township, etc.) for each term.
    """
    parsed = []
    all_terms = set()
    jurisdiction_hints = {}
    for entry in entries:
        terms = [t.strip() for t in re.split(r'[,;]', entry['place']) if t.strip()]
        parsed.append((entry['place'], entry['guid'], entry['frequency'], terms))
        all_terms.update(terms)
        for term in terms:
            hint = detect_jurisdiction_hint(term)
            if hint:
                jurisdiction_hints[term.lower()] = hint
    return parsed, all_terms, jurisdiction_hints
```

- [ ] **Step 4: Update `main()` to unpack the new return value**

Change line 934 in `main()` from:

```python
    parsed, all_terms = parse_entries(entries)
```

to:

```python
    parsed, all_terms, jurisdiction_hints = parse_entries(entries)
```

The `jurisdiction_hints` dict will be used by later tasks but is not consumed yet.

- [ ] **Step 5: Run all tests to verify nothing broke**

Run: `cd /Users/natelemonnier/storied/code/anaconda-2 && python3 -m pytest test_rtl_matcher.py -v`
Expected: all pass (including existing tests — `parse_entries` is not called by existing tests, and `match_entry` tests don't go through `main`)

- [ ] **Step 6: Commit**

```bash
git add rtl_matcher.py test_rtl_matcher.py
git commit -m "feat: build jurisdiction_hints dict during parse_entries"
```

---

### Task 3: Add jurisdiction hard filter to `rank_candidates`

**Files:**
- Modify: `rtl_matcher.py:727-751` (`rank_candidates`)
- Test: `test_rtl_matcher.py`

Add a `jurisdiction_hint` parameter to `rank_candidates`. When `jurisdiction_hint` is `None` (no jurisdiction word in input), filter out candidates with non-preferred jurisdiction types (Township, County, Municipio, Parish, District, Arrondissement) if any preferred-jurisdiction candidates (City, Town, Borough, Village, Comune, Kommune, Municipality) exist. When `jurisdiction_hint` is set, skip the filter entirely — the user explicitly asked for that jurisdiction type.

- [ ] **Step 1: Write failing tests for jurisdiction filtering**

```python
PREFERRED_JURISDICTIONS = {'City', 'Town', 'Borough', 'Village', 'Comune', 'Kommune', 'Municipality'}
FILTERED_JURISDICTIONS = {'Township', 'County', 'Municipio', 'Parish', 'District', 'Arrondissement'}


class TestRankCandidatesJurisdictionFilter:
    def test_city_preferred_over_township_no_hint(self):
        auth_cache = {
            'city': make_auth_record_full('city', level='4', population='80000',
                                          jurisdiction='City'),
            'twp': make_auth_record_full('twp', level='4', population='120000',
                                         jurisdiction='Township'),
        }
        result = rank_candidates(['city', 'twp'], auth_cache, parent_level=None,
                                 jurisdiction_hint=None)
        assert len(result) == 1
        assert result[0][0] == 'city'

    def test_township_kept_when_hint_is_township(self):
        auth_cache = {
            'city': make_auth_record_full('city', level='4', population='80000',
                                          jurisdiction='City'),
            'twp': make_auth_record_full('twp', level='4', population='120000',
                                         jurisdiction='Township'),
        }
        result = rank_candidates(['city', 'twp'], auth_cache, parent_level=None,
                                 jurisdiction_hint='Township')
        assert len(result) == 2
        assert result[0][0] == 'twp'

    def test_county_filtered_when_city_exists(self):
        auth_cache = {
            'city': make_auth_record_full('city', level='4', population='50000',
                                          jurisdiction='City'),
            'county': make_auth_record_full('county', level='5', population='200000',
                                            jurisdiction='County'),
        }
        result = rank_candidates(['city', 'county'], auth_cache, parent_level=None,
                                 jurisdiction_hint=None)
        assert len(result) == 1
        assert result[0][0] == 'city'

    def test_county_kept_when_hint_is_county(self):
        auth_cache = {
            'city': make_auth_record_full('city', level='4', population='50000',
                                          jurisdiction='City'),
            'county': make_auth_record_full('county', level='5', population='200000',
                                            jurisdiction='County'),
        }
        result = rank_candidates(['city', 'county'], auth_cache, parent_level=None,
                                 jurisdiction_hint='County')
        assert len(result) == 2
        assert result[0][0] == 'county'

    def test_no_preferred_candidates_keeps_all(self):
        auth_cache = {
            'twp_a': make_auth_record_full('twp_a', level='4', population='80000',
                                           jurisdiction='Township'),
            'twp_b': make_auth_record_full('twp_b', level='4', population='50000',
                                           jurisdiction='Township'),
        }
        result = rank_candidates(['twp_a', 'twp_b'], auth_cache, parent_level=None,
                                 jurisdiction_hint=None)
        assert len(result) == 2
        assert result[0][0] == 'twp_a'

    def test_borough_is_preferred(self):
        auth_cache = {
            'boro': make_auth_record_full('boro', level='4', population='30000',
                                          jurisdiction='Borough'),
            'twp': make_auth_record_full('twp', level='4', population='100000',
                                         jurisdiction='Township'),
        }
        result = rank_candidates(['boro', 'twp'], auth_cache, parent_level=None,
                                 jurisdiction_hint=None)
        assert len(result) == 1
        assert result[0][0] == 'boro'

    def test_village_is_preferred(self):
        auth_cache = {
            'village': make_auth_record_full('village', level='4', population='5000',
                                             jurisdiction='Village'),
            'county': make_auth_record_full('county', level='5', population='500000',
                                            jurisdiction='County'),
        }
        result = rank_candidates(['village', 'county'], auth_cache, parent_level=None,
                                 jurisdiction_hint=None)
        assert len(result) == 1
        assert result[0][0] == 'village'

    def test_filter_applies_with_parent_level_set(self):
        """Jurisdiction filter applies in multi-term path too."""
        auth_cache = {
            'city': make_auth_record_full('city', level='4', population='50000',
                                          jurisdiction='City'),
            'twp': make_auth_record_full('twp', level='4', population='120000',
                                         jurisdiction='Township'),
        }
        result = rank_candidates(['city', 'twp'], auth_cache, parent_level=6,
                                 jurisdiction_hint=None)
        assert len(result) == 1
        assert result[0][0] == 'city'

    def test_unknown_jurisdiction_not_filtered(self):
        """Jurisdictions not in either set (e.g. 'Suburb') survive the filter."""
        auth_cache = {
            'city': make_auth_record_full('city', level='4', population='50000',
                                          jurisdiction='City'),
            'suburb': make_auth_record_full('suburb', level='3', population='10000',
                                           jurisdiction='Suburb'),
        }
        result = rank_candidates(['city', 'suburb'], auth_cache, parent_level=None,
                                 jurisdiction_hint=None)
        assert len(result) == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/natelemonnier/storied/code/anaconda-2 && python3 -m pytest test_rtl_matcher.py::TestRankCandidatesJurisdictionFilter -v`
Expected: FAIL — `rank_candidates` does not accept `jurisdiction_hint` parameter

- [ ] **Step 3: Implement jurisdiction filtering in `rank_candidates`**

Replace the `rank_candidates` function in `rtl_matcher.py`:

```python
PREFERRED_JURISDICTIONS = frozenset({
    'City', 'Town', 'Borough', 'Village', 'Comune', 'Kommune', 'Municipality',
})

FILTERED_JURISDICTIONS = frozenset({
    'Township', 'County', 'Municipio', 'Parish', 'District', 'Arrondissement',
})


def rank_candidates(candidates, auth_cache, parent_level, jurisdiction_hint=None):
    """Rank candidates by level gap from parent anchor, then population.

    When jurisdiction_hint is None, filters out candidates with non-preferred
    jurisdiction types (Township, County, etc.) if any preferred-jurisdiction
    candidates (City, Town, Borough, Village, etc.) exist. When jurisdiction_hint
    is set (input explicitly names a jurisdiction), the filter is skipped.

    Returns list of (uuid, score) tuples sorted best-first.
    score is (level_gap, neg_population) — lower is better on both axes.
    When parent_level is None (single_term case), ranks by population only.
    """
    if not candidates:
        return []

    if jurisdiction_hint is None:
        preferred = [c for c in candidates
                     if field_str(auth_cache.get(c, {}), 'Jurisdiction') in PREFERRED_JURISDICTIONS]
        if preferred:
            candidates = preferred

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

- [ ] **Step 4: Update all existing callers of `rank_candidates`**

There are four call sites. For now, pass `jurisdiction_hint=None` to all of them (the hint will be wired in Task 5). The existing behavior is preserved because `jurisdiction_hint=None` is the default.

Call sites to verify take the default:
- Line ~807: `rank_candidates(list(parent_ids), auth_cache, None)` — single-term path
- Line ~844: `rank_candidates(list(confirmed), auth_cache, parent_level_for_ranking)` — chain path
- Line ~852: `rank_candidates(list(confirmed), auth_cache, None)` — parent_only path

All three use the default `jurisdiction_hint=None`, so no changes needed at call sites.

- [ ] **Step 5: Run all tests**

Run: `cd /Users/natelemonnier/storied/code/anaconda-2 && python3 -m pytest test_rtl_matcher.py -v`
Expected: all pass. Existing `TestRankCandidates` tests still pass because their mock records have empty `jurisdiction` fields, which means no preferred candidates are found and the filter is skipped (all candidates kept).

- [ ] **Step 6: Commit**

```bash
git add rtl_matcher.py test_rtl_matcher.py
git commit -m "feat: add jurisdiction hard filter to rank_candidates"
```

---

### Task 4: Reclassify single-term L3-L4 matches as `single_amb` when >1 post-filter candidate

**Files:**
- Modify: `rtl_matcher.py:806-812` (single-term path in `match_entry`)
- Test: `test_rtl_matcher.py`

After the jurisdiction filter runs inside `rank_candidates`, if more than one candidate survives, the match should be `single_amb` regardless of whether `detect_tie` finds a score tie. The threshold is N=1: only a single surviving candidate produces `single_term`.

- [ ] **Step 1: Write failing tests for single-term reclassification**

```python
class TestSingleTermReclassification:
    def test_single_candidate_after_filter_is_single_term(self):
        """One city, one township -> filter keeps city only -> single_term."""
        auth_cache = {
            'city': make_auth_record_full('city', level='4', population='80000',
                                          jurisdiction='City'),
            'twp': make_auth_record_full('twp', level='4', population='120000',
                                         jurisdiction='Township'),
        }
        name_cache = {'lawrence': {'city', 'twp'}}
        client = MagicMock()
        result = match_entry(['Lawrence'], name_cache, auth_cache, client, 'Lawrence')
        assert result.match_type == 'single_term'
        assert result.candidate_ids == ['city']

    def test_multiple_candidates_after_filter_is_single_amb(self):
        """Two cities survive filter -> single_amb even with different populations."""
        auth_cache = {
            'city_a': make_auth_record_full('city_a', level='4', population='80000',
                                            jurisdiction='City'),
            'city_b': make_auth_record_full('city_b', level='4', population='50000',
                                            jurisdiction='City'),
        }
        name_cache = {'lawrence': {'city_a', 'city_b'}}
        client = MagicMock()
        result = match_entry(['Lawrence'], name_cache, auth_cache, client, 'Lawrence')
        assert result.match_type == 'single_amb'
        assert set(result.tied_ids) == {'city_a', 'city_b'}

    def test_single_candidate_total_is_single_term(self):
        """Only one candidate in the pool -> single_term, no filter needed."""
        auth_cache = {
            'only': make_auth_record_full('only', level='4', population='5000',
                                          jurisdiction='City'),
        }
        name_cache = {'wapakoneta': {'only'}}
        client = MagicMock()
        result = match_entry(['Wapakoneta'], name_cache, auth_cache, client, 'Wapakoneta')
        assert result.match_type == 'single_term'
        assert result.candidate_ids == ['only']

    def test_multiple_townships_no_city_is_single_amb(self):
        """Two townships, no city -> filter keeps both -> single_amb."""
        auth_cache = {
            'twp_a': make_auth_record_full('twp_a', level='4', population='80000',
                                           jurisdiction='Township'),
            'twp_b': make_auth_record_full('twp_b', level='4', population='50000',
                                           jurisdiction='Township'),
        }
        name_cache = {'pine': {'twp_a', 'twp_b'}}
        client = MagicMock()
        result = match_entry(['Pine'], name_cache, auth_cache, client, 'Pine')
        assert result.match_type == 'single_amb'
        assert set(result.tied_ids) == {'twp_a', 'twp_b'}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/natelemonnier/storied/code/anaconda-2 && python3 -m pytest test_rtl_matcher.py::TestSingleTermReclassification -v`
Expected: FAIL — `test_multiple_candidates_after_filter_is_single_amb` fails because current code only flags `single_amb` on score ties, not on candidate count >1

- [ ] **Step 3: Update the single-term path in `match_entry`**

Replace lines 806-812 in `match_entry`:

```python
    if len(right_to_left) == 1:
        ranked = rank_candidates(list(parent_ids), auth_cache, None)
        if len(ranked) == 1:
            return MatchResult([ranked[0][0]], depth=1, match_type='single_term')
        all_ids = [uuid for uuid, _ in ranked]
        return MatchResult([], depth=1, match_type='single_amb', tied_ids=all_ids)
```

This replaces the old `detect_tie` logic for the single-term path. After `rank_candidates` applies the jurisdiction filter:
- 1 candidate remaining -> `single_term` with that candidate as winner
- >1 candidates remaining -> `single_amb` with all candidates as tied_ids (best guess is first in ranked order, but match_type signals ambiguity)

- [ ] **Step 4: Run all tests**

Run: `cd /Users/natelemonnier/storied/code/anaconda-2 && python3 -m pytest test_rtl_matcher.py -v`
Expected: new tests pass. Check that `TestMatchEntryTieDetection::test_single_term_no_tie_returns_winner` and `test_single_term_tie_produces_single_amb` still pass — they use mock records with empty jurisdiction, so the filter keeps all candidates; the first test has 2 candidates with different populations, which will now be `single_amb` instead of `single_term`.

**Important:** The existing test `test_single_term_no_tie_returns_winner` will now FAIL because it has 2 candidates (`big` and `small`) and expects `single_term`. Update it:

```python
    def test_single_term_no_tie_returns_winner(self):
        auth_cache = {
            'big': make_auth_record_full('big', level='6', population='500000'),
            'small': make_auth_record_full('small', level='6', population='100'),
        }
        name_cache = {'florida': {'big', 'small'}}
        client = MagicMock()
        terms = ['Florida']
        result = match_entry(terms, name_cache, auth_cache, client, 'Florida')
        # Two candidates with no jurisdiction -> both survive filter -> single_amb
        assert result.match_type == 'single_amb'
        assert set(result.tied_ids) == {'big', 'small'}
```

- [ ] **Step 5: Run all tests again after updating existing test**

Run: `cd /Users/natelemonnier/storied/code/anaconda-2 && python3 -m pytest test_rtl_matcher.py -v`
Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add rtl_matcher.py test_rtl_matcher.py
git commit -m "feat: reclassify single-term matches with >1 candidate as single_amb"
```

---

### Task 5: Wire jurisdiction hints from `parse_entries` through to `match_entry`

**Files:**
- Modify: `rtl_matcher.py:784-854` (`match_entry` signature and single-term path)
- Modify: `rtl_matcher.py:979-980` (call site in `main`)
- Test: `test_rtl_matcher.py`

Pass the `jurisdiction_hints` dict into `match_entry` so it can look up the hint for the current term and forward it to `rank_candidates`.

- [ ] **Step 1: Write failing tests for hint passthrough**

```python
class TestMatchEntryJurisdictionHint:
    def test_township_hint_preserves_township_candidates(self):
        """Input 'Lawrence Township' -> hint='Township' -> keep all including townships."""
        auth_cache = {
            'city': make_auth_record_full('city', level='4', population='80000',
                                          jurisdiction='City'),
            'twp': make_auth_record_full('twp', level='4', population='120000',
                                         jurisdiction='Township'),
        }
        name_cache = {'lawrence township': {'city', 'twp'}}
        jurisdiction_hints = {'lawrence township': 'Township'}
        client = MagicMock()
        result = match_entry(['Lawrence Township'], name_cache, auth_cache, client,
                             'Lawrence Township', jurisdiction_hints=jurisdiction_hints)
        # Both kept because hint suppresses filter; >1 candidate -> single_amb
        assert result.match_type == 'single_amb'
        assert 'twp' in result.tied_ids
        assert 'city' in result.tied_ids

    def test_no_hint_filters_township(self):
        """Input 'Lawrence' -> no hint -> township filtered out."""
        auth_cache = {
            'city': make_auth_record_full('city', level='4', population='80000',
                                          jurisdiction='City'),
            'twp': make_auth_record_full('twp', level='4', population='120000',
                                         jurisdiction='Township'),
        }
        name_cache = {'lawrence': {'city', 'twp'}}
        jurisdiction_hints = {}
        client = MagicMock()
        result = match_entry(['Lawrence'], name_cache, auth_cache, client,
                             'Lawrence', jurisdiction_hints=jurisdiction_hints)
        assert result.match_type == 'single_term'
        assert result.candidate_ids == ['city']

    def test_county_hint_in_multi_term_preserves_counties(self):
        """Multi-term: 'Clark County, Ohio' -> county hint -> counties kept in chain path."""
        auth_cache = {
            'ohio': make_auth_record_full('ohio', level='6', name='Ohio',
                                          population='11800000', jurisdiction='State'),
            'clark_county': make_auth_record_full('clark_county', level='5',
                                                   name='Clark', population='130000',
                                                   jurisdiction='County',
                                                   parent_uuid='ohio'),
            'clark_city': make_auth_record_full('clark_city', level='4',
                                                name='Clark', population='5000',
                                                jurisdiction='City',
                                                parent_uuid='ohio'),
        }
        name_cache = {
            'ohio': {'ohio'},
            'clark county': {'clark_county', 'clark_city'},
        }
        jurisdiction_hints = {'clark county': 'County'}
        client = MagicMock()
        client.find.return_value = []
        result = match_entry(['Clark County', 'Ohio'], name_cache, auth_cache, client,
                             'Clark County, Ohio', jurisdiction_hints=jurisdiction_hints)
        assert result.match_type == 'chain_verified'
        assert result.candidate_ids == ['clark_county']
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/natelemonnier/storied/code/anaconda-2 && python3 -m pytest test_rtl_matcher.py::TestMatchEntryJurisdictionHint -v`
Expected: FAIL — `match_entry` doesn't accept `jurisdiction_hints` parameter

- [ ] **Step 3: Update `match_entry` to accept and use jurisdiction hints**

Update the function signature:

```python
def match_entry(terms, name_cache, auth_cache, client, original, jurisdiction_hints=None):
```

In the single-term path (the block starting with `if len(right_to_left) == 1:`), look up the hint for the term:

```python
    if len(right_to_left) == 1:
        term_key = right_to_left[0].lower()
        hint = (jurisdiction_hints or {}).get(term_key)
        ranked = rank_candidates(list(parent_ids), auth_cache, None,
                                 jurisdiction_hint=hint)
        if len(ranked) == 1:
            return MatchResult([ranked[0][0]], depth=1, match_type='single_term')
        all_ids = [uuid for uuid, _ in ranked]
        return MatchResult([], depth=1, match_type='single_amb', tied_ids=all_ids)
```

For the multi-term chain path, look up the hint for the leftmost (most specific) term that contributed to the chain. This is the term at `right_to_left[depth-1]` — but since terms can be skipped, we need the last term that actually verified. Track it during the loop:

In the chain-verified ranking block (around line 843), look up the hint for the leftmost verified term:

```python
    if depth > 1:
        leftmost_key = right_to_left[len(right_to_left) - 1].lower()
        for i in range(len(right_to_left) - 1, 0, -1):
            if right_to_left[i] not in skipped:
                leftmost_key = right_to_left[i].lower()
                break
        hint = (jurisdiction_hints or {}).get(leftmost_key)
        ranked = rank_candidates(list(confirmed), auth_cache, parent_level_for_ranking,
                                 jurisdiction_hint=hint)
        winner, tied = detect_tie(ranked)
        if tied:
            return MatchResult([], depth, 'chain_amb', skip_count, skip_str, tied)
        ids = [winner] if winner else []
        return MatchResult(ids, depth, 'chain_verified', skip_count, skip_str)
```

The `parent_only` path does not need the hint — those results go through `resolve_parent_only` in `main()` which has its own disambiguation logic.

- [ ] **Step 4: Update the call site in `main()`**

Change line 980 from:

```python
        match = match_entry(terms, name_cache, auth_cache, client, place)
```

to:

```python
        match = match_entry(terms, name_cache, auth_cache, client, place,
                            jurisdiction_hints=jurisdiction_hints)
```

- [ ] **Step 5: Run all tests**

Run: `cd /Users/natelemonnier/storied/code/anaconda-2 && python3 -m pytest test_rtl_matcher.py -v`
Expected: all pass. Existing tests pass `jurisdiction_hints` as default `None`, which disables hint lookup and preserves old behavior.

- [ ] **Step 6: Commit**

```bash
git add rtl_matcher.py test_rtl_matcher.py
git commit -m "feat: wire jurisdiction hints through match_entry to rank_candidates"
```

---

### Task 6: Implement helper term resolution

**Files:**
- Modify: `rtl_matcher.py` (new function `resolve_helper_term`, new env var, update `main`)
- Test: `test_rtl_matcher.py`

Add an optional `RTL_HELPER_TERM` environment variable. When provided, resolve it to a single authority record before matching proceeds. When not provided, prompt the user and accept either a term string or empty input to decline. Store the resolved record's UUID, level, and parent chain UUIDs.

- [ ] **Step 1: Write failing tests for `resolve_helper_term`**

```python
from rtl_matcher import resolve_helper_term

class TestResolveHelperTerm:
    def test_resolves_single_match(self):
        """Single authority match returns resolved info."""
        utah_rec = make_auth_record_full('utah-uuid', level='6', name='Utah',
                                         parent_uuid='usa-uuid', jurisdiction='State')
        usa_rec = make_auth_record_full('usa-uuid', level='8', name='United States',
                                        jurisdiction='Country')
        client = MagicMock()
        client.find.side_effect = [
            # Authority_Place query for "Utah"
            make_fm_response([utah_rec]),
            # Parent chain fetch for usa-uuid
            make_fm_response([usa_rec]),
        ]
        auth_cache = {}
        result = resolve_helper_term('Utah', client, auth_cache)
        assert result is not None
        assert result['uuid'] == 'utah-uuid'
        assert result['level'] == 6
        assert 'usa-uuid' in result['ancestor_uuids']

    def test_returns_none_for_empty_string(self):
        client = MagicMock()
        result = resolve_helper_term('', client, {})
        assert result is None
        client.find.assert_not_called()

    def test_returns_none_for_none(self):
        client = MagicMock()
        result = resolve_helper_term(None, client, {})
        assert result is None
        client.find.assert_not_called()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/natelemonnier/storied/code/anaconda-2 && python3 -m pytest test_rtl_matcher.py::TestResolveHelperTerm -v`
Expected: FAIL — `ImportError: cannot import name 'resolve_helper_term'`

- [ ] **Step 3: Implement `resolve_helper_term`**

Add this function before `main()` in `rtl_matcher.py`:

```python
def resolve_helper_term(term_string, client, auth_cache):
    """Resolve a helper term string to a single authority record.

    Queries Authority_Place for the term. If multiple candidates are found,
    prints them and prompts the user to pick one. Returns a dict with uuid,
    level, and ancestor_uuids (set of all UUIDs in the parent chain), or
    None if the term is empty or resolution fails.
    """
    if not term_string:
        return None

    terms = [t.strip() for t in re.split(r'[,;]', term_string) if t.strip()]
    if not terms:
        return None

    # Build a mini name_cache for the helper term's components
    helper_cache = defaultdict(set)
    for term in terms:
        query = [{"Auth_Place_Name": f"=={term}"}]
        records = client.find("Authority_Place", query, limit=100)
        for rec in records:
            fd = rec['fieldData']
            uuid = field_str(fd, 'UUID')
            name = field_str(fd, 'Auth_Place_Name')
            if uuid and name:
                helper_cache[name.lower()].add(uuid)
                auth_cache[uuid] = fd

    # If multi-term, do a mini chain walk to narrow candidates
    if len(terms) == 1:
        candidates = list(helper_cache.get(terms[0].lower(), set()))
    else:
        reversed_terms = list(reversed(terms))
        confirmed = helper_cache.get(reversed_terms[0].lower(), set())
        for i in range(1, len(reversed_terms)):
            child_ids = helper_cache.get(reversed_terms[i].lower(), set())
            if not child_ids:
                continue
            _prefetch_missing_parents(child_ids, auth_cache, client)
            verified = {
                cid for cid in child_ids
                if walk_up_chain(cid, confirmed, auth_cache, client)
            }
            if verified:
                confirmed = verified
        candidates = list(confirmed)

    if not candidates:
        print(f"  Helper term '{term_string}' resolved to no authority records.")
        return None

    # Fetch any missing auth records
    missing = [uid for uid in candidates if uid not in auth_cache]
    if missing:
        query = [{"UUID": f"=={uid}"} for uid in missing]
        records = client.find("Authority_Place", query, limit=len(missing))
        for rec in records:
            fd = rec['fieldData']
            uid = field_str(fd, 'UUID')
            if uid:
                auth_cache[uid] = fd

    if len(candidates) == 1:
        chosen_uuid = candidates[0]
    else:
        print(f"\n  Helper term '{term_string}' matched {len(candidates)} candidates:")
        for i, uid in enumerate(candidates):
            rec = auth_cache.get(uid, {})
            ta = rec.get('Type_Ahead_Value', rec.get('Auth_Place_Name', uid))
            level = field_str(rec, 'Level')
            jur = field_str(rec, 'Jurisdiction')
            print(f"    [{i+1}] {ta}  (L{level} {jur})")

        while True:
            choice = input(f"  Pick 1-{len(candidates)} (or 'q' to skip helper term): ").strip()
            if choice.lower() == 'q':
                return None
            try:
                idx = int(choice) - 1
                if 0 <= idx < len(candidates):
                    chosen_uuid = candidates[idx]
                    break
            except ValueError:
                pass
            print(f"  Invalid choice. Enter 1-{len(candidates)} or 'q'.")

    rec = auth_cache.get(chosen_uuid, {})
    try:
        level = int(field_str(rec, 'Level'))
    except (ValueError, TypeError):
        level = 0

    # Walk up parent chain to collect ancestor UUIDs
    ancestor_uuids = set()
    current = chosen_uuid
    for _ in range(15):
        parent_uuid = field_str(auth_cache.get(current, {}), 'Parent_UUID')
        if not parent_uuid:
            break
        if parent_uuid not in auth_cache:
            query = [{"UUID": f"=={parent_uuid}"}]
            records = client.find("Authority_Place", query, limit=1)
            for r in records:
                fd = r['fieldData']
                uid = field_str(fd, 'UUID')
                if uid:
                    auth_cache[uid] = fd
        ancestor_uuids.add(parent_uuid)
        current = parent_uuid

    ta = rec.get('Type_Ahead_Value', rec.get('Auth_Place_Name', chosen_uuid))
    print(f"  Helper term resolved: {ta} (L{level})")

    return {
        'uuid': chosen_uuid,
        'level': level,
        'ancestor_uuids': ancestor_uuids,
    }
```

- [ ] **Step 4: Add the helper term prompt to `main()`**

Add this block after the `parse_entries` call and before Phase 1a in `main()`:

```python
    # Helper term — optional geographic context for single-term disambiguation
    helper_term_str = os.environ.get('RTL_HELPER_TERM', '').strip()
    if not helper_term_str:
        print("\nNo helper term provided (RTL_HELPER_TERM not set).")
        print("A helper term provides geographic context for ambiguous single-term matches.")
        print("Examples: 'Utah, USA', 'New South Wales, Australia', 'USA'")
        helper_term_str = input("Enter a helper term (or press Enter to skip): ").strip()

    helper_term = None  # resolved later, after auth_cache is built
```

Then after Phase 2b (parent chain pre-fetch), resolve the helper term:

```python
    # Resolve helper term against authority data
    if helper_term_str:
        print(f"\nResolving helper term: '{helper_term_str}' {elapsed()}")
        helper_term = resolve_helper_term(helper_term_str, client, auth_cache)
    else:
        helper_term = None
```

- [ ] **Step 5: Run all tests**

Run: `cd /Users/natelemonnier/storied/code/anaconda-2 && python3 -m pytest test_rtl_matcher.py -v`
Expected: all pass. `main()` is not called by tests, and `resolve_helper_term` tests use mocks.

- [ ] **Step 6: Commit**

```bash
git add rtl_matcher.py test_rtl_matcher.py
git commit -m "feat: add helper term resolution with interactive prompt"
```

---

### Task 7: Apply helper term geographic boost in single-term scoring

**Files:**
- Modify: `rtl_matcher.py:806-812` (single-term path in `match_entry`), `rtl_matcher.py:727-751` (`rank_candidates`)
- Modify: `rtl_matcher.py:979-980` (call site in `main`)
- Test: `test_rtl_matcher.py`

When a helper term is provided, candidates whose parent chain includes the helper term's authority record (or any of its ancestors) get a scoring boost. The boost scales inversely with the helper term's level: more specific helper = stronger boost.

- [ ] **Step 1: Write failing tests for helper term boost**

```python
class TestHelperTermBoost:
    def test_state_helper_boosts_matching_candidate(self):
        """Helper='Utah' (L6) -> candidate in Utah ranks above higher-pop candidate elsewhere."""
        auth_cache = {
            'logan_ut': make_auth_record_full('logan_ut', level='4', population='50000',
                                              jurisdiction='City', parent_uuid='cache_co'),
            'cache_co': make_auth_record_full('cache_co', level='5', name='Cache',
                                              parent_uuid='utah'),
            'utah': make_auth_record_full('utah', level='6', name='Utah',
                                          parent_uuid='usa'),
            'usa': make_auth_record_full('usa', level='8', name='USA'),
            'logan_wv': make_auth_record_full('logan_wv', level='4', population='80000',
                                              jurisdiction='City', parent_uuid='logan_co'),
            'logan_co': make_auth_record_full('logan_co', level='5', name='Logan',
                                              parent_uuid='wv'),
            'wv': make_auth_record_full('wv', level='6', name='West Virginia',
                                        parent_uuid='usa'),
        }
        helper_term = {'uuid': 'utah', 'level': 6, 'ancestor_uuids': {'usa'}}
        result = rank_candidates(
            ['logan_ut', 'logan_wv'], auth_cache, parent_level=None,
            jurisdiction_hint=None, helper_term=helper_term)
        assert result[0][0] == 'logan_ut'

    def test_country_helper_weaker_than_state(self):
        """Helper='USA' (L8) -> US candidate ranks above non-US,
        but boost is weaker than a state-level helper."""
        auth_cache = {
            'clarinda_us': make_auth_record_full('clarinda_us', level='4', population='5000',
                                                 jurisdiction='City', parent_uuid='page_co'),
            'page_co': make_auth_record_full('page_co', level='5', name='Page',
                                             parent_uuid='iowa'),
            'iowa': make_auth_record_full('iowa', level='6', name='Iowa',
                                          parent_uuid='usa'),
            'usa': make_auth_record_full('usa', level='8', name='USA'),
            'clarinda_au': make_auth_record_full('clarinda_au', level='4', population='200000',
                                                 jurisdiction='City', parent_uuid='kingston'),
            'kingston': make_auth_record_full('kingston', level='5', name='Kingston',
                                              parent_uuid='victoria'),
            'victoria': make_auth_record_full('victoria', level='6', name='Victoria',
                                              parent_uuid='australia'),
            'australia': make_auth_record_full('australia', level='8', name='Australia'),
        }
        helper_term = {'uuid': 'usa', 'level': 8, 'ancestor_uuids': set()}
        result = rank_candidates(
            ['clarinda_us', 'clarinda_au'], auth_cache, parent_level=None,
            jurisdiction_hint=None, helper_term=helper_term)
        assert result[0][0] == 'clarinda_us'

    def test_no_helper_no_boost(self):
        """Without helper term, higher population wins as before."""
        auth_cache = {
            'clarinda_us': make_auth_record_full('clarinda_us', level='4', population='5000',
                                                 jurisdiction='City'),
            'clarinda_au': make_auth_record_full('clarinda_au', level='4', population='200000',
                                                 jurisdiction='City'),
        }
        result = rank_candidates(
            ['clarinda_us', 'clarinda_au'], auth_cache, parent_level=None,
            jurisdiction_hint=None, helper_term=None)
        assert result[0][0] == 'clarinda_au'

    def test_helper_no_match_no_effect(self):
        """Helper='Utah' but all candidates are in Netherlands -> no boost, pop wins."""
        auth_cache = {
            'eindhoven_a': make_auth_record_full('eindhoven_a', level='4', population='230000',
                                                 jurisdiction='City', parent_uuid='nb'),
            'nb': make_auth_record_full('nb', level='6', name='Noord-Brabant',
                                        parent_uuid='nl'),
            'nl': make_auth_record_full('nl', level='8', name='Netherlands'),
            'eindhoven_b': make_auth_record_full('eindhoven_b', level='4', population='5000',
                                                 jurisdiction='City', parent_uuid='nb'),
        }
        helper_term = {'uuid': 'utah-uuid', 'level': 6, 'ancestor_uuids': {'usa-uuid'}}
        result = rank_candidates(
            ['eindhoven_a', 'eindhoven_b'], auth_cache, parent_level=None,
            jurisdiction_hint=None, helper_term=helper_term)
        assert result[0][0] == 'eindhoven_a'
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/natelemonnier/storied/code/anaconda-2 && python3 -m pytest test_rtl_matcher.py::TestHelperTermBoost -v`
Expected: FAIL — `rank_candidates` doesn't accept `helper_term` parameter

- [ ] **Step 3: Add helper term boost to `rank_candidates`**

Update the function signature and scoring logic:

```python
def rank_candidates(candidates, auth_cache, parent_level, jurisdiction_hint=None,
                    helper_term=None):
    """Rank candidates by level gap from parent anchor, then population.

    When jurisdiction_hint is None, filters out candidates with non-preferred
    jurisdiction types (Township, County, etc.) if any preferred-jurisdiction
    candidates (City, Town, Borough, Village, etc.) exist. When jurisdiction_hint
    is set (input explicitly names a jurisdiction), the filter is skipped.

    When helper_term is provided, candidates whose parent chain includes the
    helper term's UUID or any of its ancestors get a scoring boost. The boost
    magnitude scales inversely with the helper term's level (more specific =
    stronger boost).

    Returns list of (uuid, score) tuples sorted best-first.
    score is (helper_miss, level_gap, neg_population) — lower is better on all axes.
    When parent_level is None (single_term case), level_gap is 0 for all.
    """
    if not candidates:
        return []

    if jurisdiction_hint is None:
        preferred = [c for c in candidates
                     if field_str(auth_cache.get(c, {}), 'Jurisdiction') in PREFERRED_JURISDICTIONS]
        if preferred:
            candidates = preferred

    helper_targets = None
    helper_boost = 0
    if helper_term:
        helper_targets = {helper_term['uuid']} | helper_term['ancestor_uuids']
        helper_boost = max(1, 10 - helper_term['level'])

    def _in_helper_chain(uuid):
        if not helper_targets:
            return False
        current = uuid
        for _ in range(15):
            rec = auth_cache.get(current)
            if not rec:
                return False
            parent_uuid = field_str(rec, 'Parent_UUID')
            if not parent_uuid:
                return False
            if parent_uuid in helper_targets:
                return True
            current = parent_uuid
        return False

    def score(uuid):
        rec = auth_cache.get(uuid, {})
        pop = get_population(rec)

        helper_miss = 0
        if helper_targets:
            if not _in_helper_chain(uuid):
                helper_miss = helper_boost

        if parent_level is None:
            return (helper_miss, 0, -pop)
        try:
            level = int(field_str(rec, 'Level'))
        except (ValueError, TypeError):
            level = 0
        gap = abs(parent_level - level)
        return (helper_miss, gap, -pop)

    scored = [(uuid, score(uuid)) for uuid in candidates]
    scored.sort(key=lambda x: x[1])
    return scored
```

The scoring tuple is now 3 elements: `(helper_miss, level_gap, neg_population)`. `helper_miss` is 0 for candidates in the helper term's chain and a positive penalty for those outside it. The penalty magnitude is `max(1, 10 - helper_level)`:
- L6 state -> penalty 4 (strong preference)
- L8 country -> penalty 2 (mild preference)
- L5 county -> penalty 5 (very strong)

- [ ] **Step 4: Update `detect_tie` callers — score tuples are now 3-element**

`detect_tie` compares scores by equality, so it works with any tuple length. No change needed to `detect_tie` itself.

However, update the existing test `TestRankCandidates::test_returns_score_tuples`:

```python
    def test_returns_score_tuples(self):
        auth_cache = {
            'aaa': make_auth_record_full('aaa', level='6', population='300000'),
        }
        result = rank_candidates(['aaa'], auth_cache, parent_level=8)
        uuid, score = result[0]
        assert uuid == 'aaa'
        assert score == (0, 2, -300000)
```

And update `TestDetectTie` tests to use 3-element tuples:

```python
class TestDetectTie:
    def test_empty_list_returns_no_winner(self):
        winner, tied = detect_tie([])
        assert winner is None
        assert tied == []

    def test_single_candidate_returns_winner(self):
        winner, tied = detect_tie([('aaa', (0, 2, -300000))])
        assert winner == 'aaa'
        assert tied == []

    def test_different_scores_returns_winner(self):
        ranked = [('better', (0, 2, -300000)), ('worse', (0, 4, -900000))]
        winner, tied = detect_tie(ranked)
        assert winner == 'better'
        assert tied == []

    def test_identical_scores_returns_tie(self):
        ranked = [('a', (0, 2, -300000)), ('b', (0, 2, -300000))]
        winner, tied = detect_tie(ranked)
        assert winner is None
        assert set(tied) == {'a', 'b'}

    def test_three_candidates_two_tied_at_top(self):
        ranked = [('a', (0, 2, -100)), ('b', (0, 2, -100)), ('c', (0, 4, -500))]
        winner, tied = detect_tie(ranked)
        assert winner is None
        assert set(tied) == {'a', 'b'}

    def test_three_candidates_all_tied(self):
        ranked = [('a', (0, 2, -100)), ('b', (0, 2, -100)), ('c', (0, 2, -100))]
        winner, tied = detect_tie(ranked)
        assert winner is None
        assert set(tied) == {'a', 'b', 'c'}

    def test_same_gap_different_pop_not_tied(self):
        ranked = [('big', (0, 2, -500000)), ('small', (0, 2, -100))]
        winner, tied = detect_tie(ranked)
        assert winner == 'big'
        assert tied == []
```

- [ ] **Step 5: Pass helper_term through `match_entry` to `rank_candidates`**

Update `match_entry` signature:

```python
def match_entry(terms, name_cache, auth_cache, client, original,
                jurisdiction_hints=None, helper_term=None):
```

In the single-term path, pass `helper_term`:

```python
    if len(right_to_left) == 1:
        term_key = right_to_left[0].lower()
        hint = (jurisdiction_hints or {}).get(term_key)
        ranked = rank_candidates(list(parent_ids), auth_cache, None,
                                 jurisdiction_hint=hint, helper_term=helper_term)
        if len(ranked) == 1:
            return MatchResult([ranked[0][0]], depth=1, match_type='single_term')
        all_ids = [uuid for uuid, _ in ranked]
        return MatchResult([], depth=1, match_type='single_amb', tied_ids=all_ids)
```

Do NOT pass `helper_term` in the multi-term chain path — the chain verification already provides geographic context. Only the single-term path needs the boost.

Update the call site in `main()`:

```python
        match = match_entry(terms, name_cache, auth_cache, client, place,
                            jurisdiction_hints=jurisdiction_hints,
                            helper_term=helper_term)
```

- [ ] **Step 6: Run all tests**

Run: `cd /Users/natelemonnier/storied/code/anaconda-2 && python3 -m pytest test_rtl_matcher.py -v`
Expected: all pass

- [ ] **Step 7: Commit**

```bash
git add rtl_matcher.py test_rtl_matcher.py
git commit -m "feat: add helper term geographic boost for single-term scoring"
```

---

### Task 8: Integration verification — run matcher on sample data

**Files:**
- No code changes — this is a manual verification step
- Output: `~/storied/resources/SnowballLocationsSampled/locations_sample_5k_output.tsv`

Run the full matcher on the 5k sample with a US helper term and verify the known problem cases improved.

- [ ] **Step 1: Run with helper term "USA"**

```bash
cd /Users/natelemonnier/storied/code/anaconda-2
RTL_HELPER_TERM="USA" python3 rtl_matcher.py
```

When prompted for helper term resolution, confirm the USA record.

- [ ] **Step 2: Check previously problematic rows**

```bash
python3 -c "
import csv
with open('$HOME/storied/resources/SnowballLocationsSampled/locations_sample_5k_output.tsv') as f:
    reader = csv.DictReader(f, delimiter='\t')
    checks = ['Lawrence', 'Clarinda', 'Seaford', 'Darling Point', 'Shippensburg',
              'Pine', 'Sodus', 'LaPorte', 'Franconia', 'Charlotte']
    for r in reader:
        if r['original'] in checks:
            print(f\"{r['original']:25s} {r['match_type']:18s} {r['type_ahead']}\")
"
```

Expected changes:
- **Lawrence**: `single_amb` (previously `single_term` -> Lawrence Township, Indiana)
- **Clarinda**: `single_amb` or `single_term` with US candidate winning over Australia
- **Seaford**: `single_amb` or `single_term` with US candidate winning over Australia
- **Darling Point**: should still match Australia (no US counterpart)
- **Shippensburg**: `single_term` if city wins over township, or `single_amb`
- **Pine**: `single_amb` (multiple townships)
- **LaPorte**: `single_term` if city wins over township
- **Franconia**: `single_term` if city wins over township, or `single_amb`

- [ ] **Step 3: Check match type distribution**

```bash
python3 -c "
import csv
from collections import Counter
with open('$HOME/storied/resources/SnowballLocationsSampled/locations_sample_5k_output.tsv') as f:
    reader = csv.DictReader(f, delimiter='\t')
    types = Counter(r['match_type'] for r in reader)
for t, c in types.most_common():
    print(f'  {t:20s} {c:>5}')
"
```

Expected: significant increase in `single_amb` count compared to previous run. `single_term` count should drop. `chain_verified` and `chain_amb` should stay roughly the same (jurisdiction filter may shift a few).

- [ ] **Step 4: Check tie output file grew**

```bash
wc -l ~/storied/resources/SnowballLocationsSampled/locations_sample_5k_output_ties.tsv
```

Expected: more rows than before (previously only score-tied candidates; now includes all post-filter candidates for `single_amb` matches).
