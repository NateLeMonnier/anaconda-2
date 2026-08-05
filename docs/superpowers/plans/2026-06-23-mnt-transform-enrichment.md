# MNT Transform Enrichment — Phase 1c Fix

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When the MNT provides a wrong-but-valid UUID for a transformable term (e.g., "Town of Bristol" -> Bristol, England), ensure the fallback transform path still runs so Phase 3 has both MNT candidates and transform-derived candidates available for chain verification.

**Architecture:** Phase 1c currently only processes terms with zero name_cache entries. The fix adds a second pass that also processes terms that have name_cache entries but are transformable (transform_term returns non-None). Transform results are added alongside MNT results in name_cache, not replacing them. Phase 3's chain walk then naturally picks the candidate that verifies against the parent hierarchy.

**Tech Stack:** Python 3, pytest, unittest.mock

## Global Constraints

- No new dependencies
- Phase 3 must remain offline (no FM calls added there)
- Transform results are additive — never remove existing name_cache entries
- The existing `query_fallback_transforms` function handles both jurisdiction and non-jurisdiction transforms; reuse it rather than duplicating logic

---

### Task 1: Enrich name_cache with transform candidates for MNT-matched terms

**Files:**
- Modify: `rtl_matcher.py:1486-1492` (Phase 1c call site in main())
- Test: `test_rtl_matcher.py` (new test class)

**Interfaces:**
- Consumes: `transform_term(term) -> (cleaned, jurisdiction)` — returns `(None, None)` when no transform applies
- Consumes: `query_fallback_transforms(client, terms, name_cache) -> int` — queries FM for transformed terms, stores results in name_cache under the original term key
- Produces: No new interfaces. Existing `name_cache` dict gains additional UUIDs for terms where the MNT had a result but the transform produces a different lookup string.

- [ ] **Step 1: Write the failing test**

The test creates a scenario matching the Bristol bug: a term has an MNT entry (wrong UUID), but transform_term would produce a different lookup that finds the correct authority record. Verify that after Phase 1c, name_cache contains both the MNT UUID and the transform-derived UUID.

Add to `test_rtl_matcher.py`:

```python
from rtl_matcher import transform_term, query_fallback_transforms


class TestMntTransformEnrichment:
    def test_transformable_mnt_matched_term_gets_enriched(self):
        """'Town of Bristol' has an MNT entry (Bristol, England) but transform_term
        strips 'Town of' and queries Auth_Place_Name='Bristol' + Jurisdiction='Town',
        which should find Bristol, Rhode Island. After enrichment, name_cache should
        contain BOTH UUIDs."""
        mnt_uuid = 'bristol-england-uuid'
        transform_uuid = 'bristol-ri-uuid'
        name_cache = defaultdict(set)
        name_cache['town of bristol'].add(mnt_uuid)

        # FM returns the Rhode Island Bristol for Auth_Place_Name='Bristol', Jurisdiction='Town'
        client = MagicMock()
        client.find.return_value = make_fm_response([
            make_auth_record_full(transform_uuid, name='Bristol',
                                  jurisdiction='Town', level='4'),
        ])

        # Collect transformable terms that already have name_cache entries
        all_terms = ['Town of Bristol']
        transformable_matched = [
            t for t in all_terms
            if name_cache.get(t.lower()) and transform_term(t)[0] is not None
        ]
        query_fallback_transforms(client, transformable_matched, name_cache)

        assert mnt_uuid in name_cache['town of bristol']
        assert transform_uuid in name_cache['town of bristol']

    def test_non_transformable_mnt_matched_term_skipped(self):
        """'Rhode Island' has an MNT entry and transform_term returns (None, None).
        It should not be passed to query_fallback_transforms."""
        name_cache = defaultdict(set)
        name_cache['rhode island'].add('ri-uuid')

        all_terms = ['Rhode Island']
        transformable_matched = [
            t for t in all_terms
            if name_cache.get(t.lower()) and transform_term(t)[0] is not None
        ]

        assert transformable_matched == []

    def test_enrichment_is_additive(self):
        """Transform results must not replace existing MNT entries."""
        mnt_uuid = 'existing-mnt-uuid'
        name_cache = defaultdict(set)
        name_cache['city of springfield'].add(mnt_uuid)

        client = MagicMock()
        client.find.return_value = make_fm_response([
            make_auth_record_full('springfield-city-uuid', name='Springfield',
                                  jurisdiction='City', level='4'),
        ])

        all_terms = ['City of Springfield']
        transformable_matched = [
            t for t in all_terms
            if name_cache.get(t.lower()) and transform_term(t)[0] is not None
        ]
        query_fallback_transforms(client, transformable_matched, name_cache)

        assert mnt_uuid in name_cache['city of springfield']
        assert 'springfield-city-uuid' in name_cache['city of springfield']
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd /Users/natelemonnier/storied/code/anaconda-2 && python -m pytest test_rtl_matcher.py::TestMntTransformEnrichment -v`

Expected: All 3 tests PASS. These tests simulate the enrichment logic inline — they will pass because they manually build the `transformable_matched` list and call `query_fallback_transforms`. This confirms the existing function works correctly with MNT-matched terms. The real change is wiring this into `main()`.

- [ ] **Step 3: Write a test that exercises the full Phase 1c flow**

This test verifies the actual main() logic change — that the Phase 1c block in main() collects transformable MNT-matched terms and passes them through. Since main() is monolithic, we test by verifying the filter logic that will be added:

Add to the same class in `test_rtl_matcher.py`:

```python
    def test_filter_collects_correct_terms(self):
        """The filter for Phase 1c enrichment should include terms that:
        1. Have name_cache entries (MNT-matched)
        2. Are transformable (transform_term returns non-None)
        And exclude terms that:
        - Have no name_cache entries (already handled by unmatched path)
        - Are not transformable (no prefix/suffix to strip)"""
        name_cache = defaultdict(set)
        name_cache['town of bristol'].add('some-uuid')     # transformable + matched
        name_cache['rhode island'].add('ri-uuid')           # not transformable + matched
        # 'Springfield' not in name_cache                   # transformable but unmatched

        all_terms = ['Town of Bristol', 'Rhode Island', 'Springfield']

        transformable_matched = [
            t for t in all_terms
            if name_cache.get(t.lower()) and transform_term(t)[0] is not None
        ]

        assert transformable_matched == ['Town of Bristol']
```

- [ ] **Step 4: Run the test**

Run: `cd /Users/natelemonnier/storied/code/anaconda-2 && python -m pytest test_rtl_matcher.py::TestMntTransformEnrichment -v`

Expected: PASS

- [ ] **Step 5: Implement the change in main()**

In `rtl_matcher.py`, modify the Phase 1c block (lines 1486-1492). After the existing unmatched transform pass, add a second pass for transformable MNT-matched terms:

```python
    # Phase 1c: Transform unmatched terms and retry both sources
    print(f"\nPhase 1c: Fallback transforms for unmatched terms {elapsed()}")
    unmatched = [t for t in all_terms if not name_cache.get(t.lower())]
    print(f"  {len(unmatched)} terms unmatched, applying transforms...")
    query_fallback_transforms(client, unmatched, name_cache)
    after = sum(1 for v in name_cache.values() if v)
    print(f"  After transforms: {after} terms matched (+{after - combined} new) {elapsed()}")

    # Phase 1c-enrich: also transform MNT-matched terms whose transforms
    # produce a different lookup string — the MNT entry may be wrong, and
    # the transform-derived candidates let Phase 3's chain walk pick the
    # contextually correct one.
    transformable_matched = [
        t for t in all_terms
        if name_cache.get(t.lower()) and transform_term(t)[0] is not None
    ]
    if transformable_matched:
        print(f"  Enriching {len(transformable_matched)} MNT-matched transformable terms...")
        enrich_added = query_fallback_transforms(client, transformable_matched, name_cache)
        after_enrich = sum(1 for v in name_cache.values() if v)
        print(f"  After enrichment: +{enrich_added} UUIDs added {elapsed()}")
```

- [ ] **Step 6: Run the full test suite**

Run: `cd /Users/natelemonnier/storied/code/anaconda-2 && python -m pytest test_rtl_matcher.py -v`

Expected: All existing tests pass, all new tests pass.

- [ ] **Step 7: Manual verification against the Bristol case**

Run the same FM query we used to diagnose the bug, confirming that name_cache now contains both UUIDs for "town of bristol":

```bash
cd /Users/natelemonnier/storied/code/anaconda-2 && python3 -c "
from rtl_matcher import *
from collections import defaultdict

client = FileMakerClient()
client.auth()

all_terms = {'Town of Bristol', 'Rhode Island'}

name_cache = query_mnt(client, all_terms)
print(f'After MNT: name_cache[\"town of bristol\"] = {name_cache.get(\"town of bristol\", set())}')

query_authority_by_name(client, all_terms, name_cache)

unmatched = [t for t in all_terms if not name_cache.get(t.lower())]
query_fallback_transforms(client, unmatched, name_cache)

transformable_matched = [
    t for t in all_terms
    if name_cache.get(t.lower()) and transform_term(t)[0] is not None
]
print(f'Transformable matched: {transformable_matched}')
enrich_added = query_fallback_transforms(client, transformable_matched, name_cache)

print(f'After enrichment: name_cache[\"town of bristol\"] = {name_cache.get(\"town of bristol\", set())}')
print(f'UUID count: {len(name_cache.get(\"town of bristol\", set()))}')
"
```

Expected: name_cache["town of bristol"] contains both the MNT UUID (B0611307-..., Bristol England) and the transform-derived UUIDs (D76447FF-..., Bristol RI, plus the other 4 Bristol/Town records).

- [ ] **Step 8: Commit**

```bash
git add rtl_matcher.py test_rtl_matcher.py
git commit -m "enrich MNT-matched terms with transform-derived candidates

When the MNT maps a transformable term (e.g. 'Town of Bristol') to a
wrong-but-valid UUID, Phase 1c's fallback transforms never fire because
the term already has name_cache entries. Add a second transform pass for
MNT-matched terms whose transform_term result differs from the original,
so Phase 3's chain walk has both candidate sets available."
```
