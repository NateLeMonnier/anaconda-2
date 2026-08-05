# Proximity Fallback for Wrong-County Matches — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When a city term fails chain verification against the confirmed county but exists in the same state, accept it if its actual county is within 50km of the confirmed county.

**Architecture:** Add a haversine distance function and a proximity fallback step inside `match_entry()`. After the main RTL loop, iterate skipped terms that had candidates, verify them against the state (parent of confirmed county), compute county-to-county distance, and accept if <50km. Requires adding Latitude/Longitude to the PA field map so auth_cache records carry coordinates.

**Tech Stack:** Python, math (for haversine), existing test framework (pytest)

## Global Constraints

- Proximity fallback only triggers when depth >= 2 (at least 2 terms already confirmed in chain)
- Only runs on skipped terms that had candidates but failed `walk_up_chain` (not terms with zero candidates)
- Distance threshold: 50km, county-to-county (confirmed county lat/long vs candidate's parent county lat/long)
- Match type: `chain_verified_proximity`
- Must not break any existing tests

---

### Task 1: Add Latitude/Longitude to PA Field Map and Auth Cache

**Files:**
- Modify: `rtl_local_data.py:18-27` (add Latitude/Longitude to `_PA_FIELD_MAP`)

**Interfaces:**
- Produces: auth_cache records now include `'Latitude'` and `'Longitude'` string fields

- [ ] **Step 1: Write failing test**

```python
# In test_rtl_matcher.py

class TestLatLongInAuthCache:
    def test_auth_record_includes_lat_long(self):
        """PA records loaded via local data should include Latitude and Longitude."""
        from rtl_local_data import _PA_FIELD_MAP
        assert 'Latitude' in _PA_FIELD_MAP.values()
        assert 'Longitude' in _PA_FIELD_MAP.values()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/natelemonnier/storied/code/anaconda-2 && python -m pytest test_rtl_matcher.py::TestLatLongInAuthCache -v`
Expected: FAIL — 'Latitude' not in _PA_FIELD_MAP.values()

- [ ] **Step 3: Add Latitude and Longitude to _PA_FIELD_MAP**

In `rtl_local_data.py`, add two entries to `_PA_FIELD_MAP`:

```python
_PA_FIELD_MAP = {
    'Term': 'Auth_Place_Name',
    'ID': 'UUID',
    'ParentID': 'Parent_UUID',
    'LevelName': 'Jurisdiction',
    'Level': 'Level',
    'Population': 'Population',
    'FullChainName': 'Type_Ahead_Value',
    'Historical': 'Historical',
    'Latitude': 'Latitude',
    'Longitude': 'Longitude',
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/natelemonnier/storied/code/anaconda-2 && python -m pytest test_rtl_matcher.py::TestLatLongInAuthCache -v`
Expected: PASS

- [ ] **Step 5: Run all existing tests to check nothing breaks**

Run: `cd /Users/natelemonnier/storied/code/anaconda-2 && python -m pytest test_rtl_matcher.py -v`
Expected: All existing tests PASS

- [ ] **Step 6: Commit**

```bash
git add rtl_local_data.py test_rtl_matcher.py
git commit -m "feat: add Latitude/Longitude to PA field map for proximity fallback"
```

---

### Task 2: Implement Haversine Distance Function

**Files:**
- Modify: `rtl_matcher.py` (add `haversine_km` function after `get_population`, around line 1261)
- Modify: `test_rtl_matcher.py` (add tests)

**Interfaces:**
- Produces: `haversine_km(lat1, lon1, lat2, lon2) -> float` — returns great-circle distance in km between two coordinate pairs. Returns `float('inf')` if any coordinate is missing/unparseable.

- [ ] **Step 1: Write failing tests**

```python
# In test_rtl_matcher.py
import math
from rtl_matcher import haversine_km


class TestHaversineKm:
    def test_same_point_returns_zero(self):
        assert haversine_km(41.0, -94.0, 41.0, -94.0) == 0.0

    def test_known_distance(self):
        # Adams County, Iowa (41.0652, -94.6864) to Union County, Iowa (41.0007, -94.2744)
        dist = haversine_km(41.0652, -94.6864, 41.0007, -94.2744)
        assert 30 < dist < 40  # ~34km apart

    def test_missing_lat_returns_inf(self):
        assert haversine_km(None, -94.0, 41.0, -94.0) == float('inf')

    def test_empty_string_returns_inf(self):
        assert haversine_km('', -94.0, 41.0, -94.0) == float('inf')

    def test_unparseable_returns_inf(self):
        assert haversine_km('abc', -94.0, 41.0, -94.0) == float('inf')

    def test_distant_points(self):
        # New York (40.7128, -74.0060) to Los Angeles (34.0522, -118.2437)
        dist = haversine_km(40.7128, -74.0060, 34.0522, -118.2437)
        assert 3900 < dist < 4000
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/natelemonnier/storied/code/anaconda-2 && python -m pytest test_rtl_matcher.py::TestHaversineKm -v`
Expected: FAIL — ImportError, haversine_km not defined

- [ ] **Step 3: Implement haversine_km**

Add to `rtl_matcher.py` after the `get_population` function (after line 1261), before `_disambiguate_by_population`:

```python
def haversine_km(lat1, lon1, lat2, lon2):
    """Great-circle distance in km between two lat/long pairs.
    Returns float('inf') if any coordinate is missing or unparseable."""
    import math
    try:
        la1, lo1, la2, lo2 = float(lat1), float(lon1), float(lat2), float(lon2)
    except (TypeError, ValueError):
        return float('inf')
    la1, lo1, la2, lo2 = map(math.radians, (la1, lo1, la2, lo2))
    dlat = la2 - la1
    dlon = lo2 - lo1
    a = math.sin(dlat / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin(dlon / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(a))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/natelemonnier/storied/code/anaconda-2 && python -m pytest test_rtl_matcher.py::TestHaversineKm -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add rtl_matcher.py test_rtl_matcher.py
git commit -m "feat: add haversine_km distance function"
```

---

### Task 3: Implement Proximity Fallback in match_entry

**Files:**
- Modify: `rtl_matcher.py:1435-1522` (modify `match_entry` function)
- Modify: `test_rtl_matcher.py` (add tests)

**Interfaces:**
- Consumes: `haversine_km(lat1, lon1, lat2, lon2)` from Task 2, `walk_up_chain(candidate_id, target_ids, auth_cache, client)`, `rank_candidates(...)`, `detect_tie(...)`, `_get_parent_level(...)`, `_prefetch_missing_parents(...)`
- Produces: `MatchResult` with `match_type='chain_verified_proximity'` when proximity fallback succeeds. `skipped_terms` includes county mismatch annotation like `"Adams County (proximity: 19km)"`

The fallback logic goes after the main RTL loop (after line 1498) and before the final ranking (line 1503). It runs when `depth > 1` and there are skipped terms that had candidates.

- [ ] **Step 1: Write test helper — extend make_auth_record_full with lat/long**

```python
# Update make_auth_record_full in test_rtl_matcher.py to accept lat/long
def make_auth_record_full(uuid, parent_uuid=None, name="Place", level="4",
                          population="", jurisdiction="", latitude="", longitude=""):
    return {
        'UUID': uuid,
        'Parent_UUID': parent_uuid or '',
        'Auth_Place_Name': name,
        'Level': level,
        'Population': population,
        'Jurisdiction': jurisdiction,
        'Type_Ahead_Value': '',
        'Latitude': latitude,
        'Longitude': longitude,
    }
```

- [ ] **Step 2: Write failing test — basic proximity match (Cromwell case)**

```python
class TestProximityFallback:
    def _build_cromwell_caches(self):
        """Cromwell, Adams County, Iowa — Cromwell is actually in Union County.
        Adams and Union counties are adjacent (~34km apart)."""
        auth_cache = {
            'usa': make_auth_record_full('usa', level='8', name='United States',
                                         population='330000000'),
            'iowa': make_auth_record_full('iowa', parent_uuid='usa', level='6',
                                          name='Iowa', population='3200000'),
            'adams-co': make_auth_record_full('adams-co', parent_uuid='iowa', level='5',
                                              name='Adams', population='3700',
                                              jurisdiction='County',
                                              latitude='41.0652', longitude='-94.6864'),
            'union-co': make_auth_record_full('union-co', parent_uuid='iowa', level='5',
                                              name='Union', population='12200',
                                              jurisdiction='County',
                                              latitude='41.0007', longitude='-94.2744'),
            'cromwell': make_auth_record_full('cromwell', parent_uuid='union-co', level='4',
                                              name='Cromwell', population='108',
                                              jurisdiction='City',
                                              latitude='41.0394', longitude='-94.4619'),
        }
        name_cache = {
            'iowa': {'iowa'},
            'adams county': {'adams-co'},
            'cromwell': {'cromwell'},
        }
        return name_cache, auth_cache

    def test_cromwell_matches_via_proximity(self):
        name_cache, auth_cache = self._build_cromwell_caches()
        client = MagicMock()
        client.find.return_value = []
        terms = ['Cromwell', 'Adams County', 'Iowa']
        result = match_entry(terms, name_cache, auth_cache, client,
                             'Cromwell, Adams County, Iowa',
                             jurisdiction_hints={'adams county': 'County'})
        assert result.match_type == 'chain_verified_proximity'
        assert result.candidate_ids == ['cromwell']
        assert result.depth == 3
        assert 'proximity' in result.skipped_terms.lower()

    def test_no_proximity_when_too_far(self):
        """City in same state but distant county — should NOT match via proximity."""
        auth_cache = {
            'usa': make_auth_record_full('usa', level='8', name='United States',
                                         population='330000000'),
            'iowa': make_auth_record_full('iowa', parent_uuid='usa', level='6',
                                          name='Iowa', population='3200000'),
            'adams-co': make_auth_record_full('adams-co', parent_uuid='iowa', level='5',
                                              name='Adams', population='3700',
                                              jurisdiction='County',
                                              latitude='41.0652', longitude='-94.6864'),
            'dubuque-co': make_auth_record_full('dubuque-co', parent_uuid='iowa', level='5',
                                                name='Dubuque', population='98000',
                                                jurisdiction='County',
                                                latitude='42.4700', longitude='-90.7100'),
            'faraway-city': make_auth_record_full('faraway-city', parent_uuid='dubuque-co',
                                                   level='4', name='Farville',
                                                   population='500', jurisdiction='City',
                                                   latitude='42.5', longitude='-90.7'),
        }
        name_cache = {
            'iowa': {'iowa'},
            'adams county': {'adams-co'},
            'farville': {'faraway-city'},
        }
        client = MagicMock()
        client.find.return_value = []
        terms = ['Farville', 'Adams County', 'Iowa']
        result = match_entry(terms, name_cache, auth_cache, client,
                             'Farville, Adams County, Iowa',
                             jurisdiction_hints={'adams county': 'County'})
        # Should NOT be proximity match — too far away
        assert result.match_type != 'chain_verified_proximity'

    def test_no_proximity_when_depth_less_than_2(self):
        """Only one term confirmed (country only) — no proximity fallback."""
        auth_cache = {
            'usa': make_auth_record_full('usa', level='8', name='United States',
                                         population='330000000'),
            'some-co': make_auth_record_full('some-co', parent_uuid='usa', level='5',
                                             name='Some', jurisdiction='County',
                                             latitude='40.0', longitude='-90.0'),
            'city-x': make_auth_record_full('city-x', parent_uuid='some-co', level='4',
                                             name='CityX', jurisdiction='City',
                                             latitude='40.1', longitude='-90.1'),
        }
        name_cache = {
            'united states': {'usa'},
            'cityx': {'city-x'},
        }
        client = MagicMock()
        client.find.return_value = []
        terms = ['CityX', 'United States']
        result = match_entry(terms, name_cache, auth_cache, client,
                             'CityX, United States')
        assert result.match_type != 'chain_verified_proximity'

    def test_proximity_picks_most_specific_skipped_term(self):
        """Two skipped terms with candidates — proximity should match the most specific one."""
        auth_cache = {
            'usa': make_auth_record_full('usa', level='8', name='United States',
                                         population='330000000'),
            'iowa': make_auth_record_full('iowa', parent_uuid='usa', level='6',
                                          name='Iowa', population='3200000'),
            'adams-co': make_auth_record_full('adams-co', parent_uuid='iowa', level='5',
                                              name='Adams', population='3700',
                                              jurisdiction='County',
                                              latitude='41.0652', longitude='-94.6864'),
            'union-co': make_auth_record_full('union-co', parent_uuid='iowa', level='5',
                                              name='Union', population='12200',
                                              jurisdiction='County',
                                              latitude='41.0007', longitude='-94.2744'),
            'cromwell': make_auth_record_full('cromwell', parent_uuid='union-co', level='4',
                                              name='Cromwell', population='108',
                                              jurisdiction='City',
                                              latitude='41.0394', longitude='-94.4619'),
            'neighborhood': make_auth_record_full('neighborhood', parent_uuid='union-co',
                                                   level='3', name='OldTown',
                                                   population='50', jurisdiction='Village',
                                                   latitude='41.04', longitude='-94.46'),
        }
        name_cache = {
            'iowa': {'iowa'},
            'adams county': {'adams-co'},
            'cromwell': {'cromwell'},
            'oldtown': {'neighborhood'},
        }
        client = MagicMock()
        client.find.return_value = []
        terms = ['OldTown', 'Cromwell', 'Adams County', 'Iowa']
        result = match_entry(terms, name_cache, auth_cache, client,
                             'OldTown, Cromwell, Adams County, Iowa',
                             jurisdiction_hints={'adams county': 'County'})
        # Cromwell should match via proximity; OldTown may chain-verify against Cromwell
        # or also match via proximity — either way, most specific wins
        assert result.match_type in ('chain_verified_proximity', 'chain_verified')
        assert result.depth >= 3

    def test_proximity_multiple_candidates_disambiguates(self):
        """Two cities with same name in different counties near the confirmed county.
        Both within 50km — should use rank_candidates + detect_tie."""
        auth_cache = {
            'usa': make_auth_record_full('usa', level='8', name='United States',
                                         population='330000000'),
            'iowa': make_auth_record_full('iowa', parent_uuid='usa', level='6',
                                          name='Iowa', population='3200000'),
            'adams-co': make_auth_record_full('adams-co', parent_uuid='iowa', level='5',
                                              name='Adams', population='3700',
                                              jurisdiction='County',
                                              latitude='41.0652', longitude='-94.6864'),
            'union-co': make_auth_record_full('union-co', parent_uuid='iowa', level='5',
                                              name='Union', population='12200',
                                              jurisdiction='County',
                                              latitude='41.0007', longitude='-94.2744'),
            'taylor-co': make_auth_record_full('taylor-co', parent_uuid='iowa', level='5',
                                               name='Taylor', population='6000',
                                               jurisdiction='County',
                                               latitude='40.7400', longitude='-94.6900'),
            'springfield-1': make_auth_record_full('springfield-1', parent_uuid='union-co',
                                                    level='4', name='Springfield',
                                                    population='200', jurisdiction='City',
                                                    latitude='41.0', longitude='-94.3'),
            'springfield-2': make_auth_record_full('springfield-2', parent_uuid='taylor-co',
                                                    level='4', name='Springfield',
                                                    population='200', jurisdiction='City',
                                                    latitude='40.75', longitude='-94.7'),
        }
        name_cache = {
            'iowa': {'iowa'},
            'adams county': {'adams-co'},
            'springfield': {'springfield-1', 'springfield-2'},
        }
        client = MagicMock()
        client.find.return_value = []
        terms = ['Springfield', 'Adams County', 'Iowa']
        result = match_entry(terms, name_cache, auth_cache, client,
                             'Springfield, Adams County, Iowa',
                             jurisdiction_hints={'adams county': 'County'})
        # Both within 50km, same pop — should be ambiguous
        assert result.match_type == 'chain_amb'
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd /Users/natelemonnier/storied/code/anaconda-2 && python -m pytest test_rtl_matcher.py::TestProximityFallback -v`
Expected: FAIL — match_type will be 'chain_verified' (skipping Cromwell) not 'chain_verified_proximity'

- [ ] **Step 4: Implement proximity fallback in match_entry**

In `rtl_matcher.py`, modify `match_entry`. The changes go in two places:

**4a.** Track which skipped terms had candidates. Replace the skip logic (lines 1480-1498) to also record candidate IDs for skipped terms:

Change the loop body. Before:
```python
        if verified:
            parent_level_for_ranking = _get_parent_level(confirmed, auth_cache)
            confirmed = verified
            depth += 1
        else:
            skipped.append(right_to_left[i])
```

After:
```python
        if verified:
            parent_level_for_ranking = _get_parent_level(confirmed, auth_cache)
            confirmed = verified
            depth += 1
        else:
            skipped.append(right_to_left[i])
            skipped_with_candidates.append((right_to_left[i], child_ids))
```

Add `skipped_with_candidates = []` alongside `skipped = []` at line 1471.

**4b.** Add the proximity fallback block after the main RTL loop, before the `if depth > 1:` ranking block (before line 1503). Add a constant `PROXIMITY_THRESHOLD_KM = 50` near the top of the file with other constants.

```python
    # --- Proximity fallback ---
    # When depth >= 2 and skipped terms had candidates that failed chain
    # verification against the confirmed county, check if any verify against
    # the state (parent of confirmed county) and are within PROXIMITY_THRESHOLD_KM.
    proximity_matched = False
    proximity_annotations = []
    if depth >= 2 and skipped_with_candidates:
        # Get the state-level ancestor of the confirmed set
        state_ids = set()
        confirmed_county_ids = set(confirmed)
        for uid in confirmed:
            rec = auth_cache.get(uid, {})
            parent_uuid = field_str(rec, 'Parent_UUID')
            if parent_uuid:
                state_ids.add(parent_uuid)

        if state_ids:
            # Try each skipped term that had candidates, collect proximity-passing ones
            # Sort by level (most specific first = lowest level number)
            proximity_candidates = []
            for skipped_term, candidate_ids in skipped_with_candidates:
                _prefetch_missing_parents(candidate_ids, auth_cache, client)
                state_verified = {
                    cid for cid in candidate_ids
                    if walk_up_chain(cid, state_ids, auth_cache, client)
                }
                if not state_verified:
                    continue

                # For each state-verified candidate, compute county-to-county distance
                for cid in state_verified:
                    cid_rec = auth_cache.get(cid, {})
                    cid_parent = field_str(cid_rec, 'Parent_UUID')
                    if not cid_parent:
                        continue
                    cid_county_rec = auth_cache.get(cid_parent, {})
                    if not cid_county_rec:
                        continue

                    # Distance from candidate's county to each confirmed county
                    min_dist = float('inf')
                    closest_confirmed = None
                    for conf_uid in confirmed_county_ids:
                        conf_rec = auth_cache.get(conf_uid, {})
                        dist = haversine_km(
                            cid_county_rec.get('Latitude', ''),
                            cid_county_rec.get('Longitude', ''),
                            conf_rec.get('Latitude', ''),
                            conf_rec.get('Longitude', ''),
                        )
                        if dist < min_dist:
                            min_dist = dist
                            closest_confirmed = conf_uid

                    if min_dist <= PROXIMITY_THRESHOLD_KM:
                        proximity_candidates.append(cid)
                        conf_name = field_str(
                            auth_cache.get(closest_confirmed, {}), 'Auth_Place_Name')
                        cid_county_name = field_str(cid_county_rec, 'Auth_Place_Name')
                        proximity_annotations.append(
                            f"{conf_name} County (proximity: {min_dist:.0f}km, "
                            f"actual: {cid_county_name} County)")

                        # Remove this term from skipped list
                        if skipped_term in skipped:
                            skipped.remove(skipped_term)

            if proximity_candidates:
                parent_level_for_ranking = _get_parent_level(confirmed, auth_cache)
                confirmed = set(proximity_candidates)
                depth += 1
                proximity_matched = True
```

**4c.** Modify the ranking/return block. Change the `if depth > 1:` block (lines 1503-1517) to handle proximity:

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

        skip_count = len(skipped)
        # Append proximity annotations to skipped_terms output
        skip_parts = [s for s in skipped]
        skip_parts.extend(proximity_annotations)
        skip_str = '; '.join(skip_parts)

        if tied:
            mt = 'chain_amb'
            return MatchResult([], depth, mt, skip_count, skip_str, tied)
        mt = 'chain_verified_proximity' if proximity_matched else 'chain_verified'
        ids = [winner] if winner else []
        return MatchResult(ids, depth, mt, skip_count, skip_str)
```

Also update `skip_count` and `skip_str` construction to happen after the proximity block rather than before. Remove the existing lines 1500-1501 (`skip_count = len(skipped)` and `skip_str = '; '.join(skipped)`) and move them into the return blocks.

For the `parent_only` return path (line 1519-1522), also compute skip_str there:

```python
    skip_count = len(skipped)
    skip_str = '; '.join(skipped)
    ranked = rank_candidates(list(confirmed), auth_cache, None)
    ids = [uuid for uuid, _ in ranked]
    return MatchResult(ids, depth, 'parent_only', skip_count, skip_str)
```

- [ ] **Step 5: Add PROXIMITY_THRESHOLD_KM constant**

Add near the top of `rtl_matcher.py`, after `FS_TYPE_CITY = "186"` (line 77):

```python
PROXIMITY_THRESHOLD_KM = 50
```

- [ ] **Step 6: Add 'chain_verified_proximity' to print_summary match types**

In `print_summary` (line 1605), add `'chain_verified_proximity'` to the list:

```python
    for match_type in ['chain_verified', 'chain_verified_proximity', 'chain_amb',
                       'single_term', 'single_amb',
                       'parent_resolved', 'parent_rejected', 'parent_only', 'parent_amb',
                       'no_auth_match', 'no_terms']:
```

- [ ] **Step 7: Run proximity tests**

Run: `cd /Users/natelemonnier/storied/code/anaconda-2 && python -m pytest test_rtl_matcher.py::TestProximityFallback -v`
Expected: All PASS

- [ ] **Step 8: Run full test suite**

Run: `cd /Users/natelemonnier/storied/code/anaconda-2 && python -m pytest test_rtl_matcher.py -v`
Expected: All PASS (existing + new)

- [ ] **Step 9: Commit**

```bash
git add rtl_matcher.py test_rtl_matcher.py
git commit -m "feat: add proximity fallback for wrong-county city matches within 50km"
```
