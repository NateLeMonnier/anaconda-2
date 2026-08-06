# low_evidence Gate Implementation Plan

> **STATUS: complete, 2026-08-06.** All seven tasks landed; 269 tests pass and
> the acceptance target was met exactly (7 rows change, and they are the 7 the
> spec named). Read this alongside
> `.superpowers/sdd/2026-08-02-low-evidence-gate/progress.md`, which records
> what execution did differently. Three deviations matter:
>
> 1. **Line numbers and call sites below are stale.** Tasks 1-3 landed on
>    2026-08-04/05; the 2026-08-06 cleanup (`59004e9..39ba23a`) then deleted
>    FileMaker `--api` mode and split `match_entry`/`main`. The Task 4 gate
>    site is now `_match_single_term`, and `resolve_parent_match` no longer
>    takes a `client` parameter.
> 2. **The case test in `is_description` is disabled by default.** Wiring the
>    gate exposed two span-recording defects (`span_for` returned the
>    lowercased key; the spelling path recorded SymSpell's lowercased
>    suggestion instead of `Auth_Place_Name`). Both are fixed, but
>    `transform_variant` still records no span at all, and with that gap the
>    case test gates two correct rows and none of the seven targets. Spec
>    section 2 sanctions turning it off in exactly this situation.
> 3. **The gate fires only on paths that were about to commit.** Placing it
>    ahead of the structural short-circuit, as the spec's section 3 wording
>    suggests, also caught `single_amb` and `parent_rejected` rows — 94 rows
>    instead of 7, all of them one abstention relabelled as another.
>
> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop `rtl_matcher.py` from emitting a high-confidence match when the only thing supporting it is a common noun that happens to collide with an authority record.

**Architecture:** A deterministic check bolted onto the two match types that lack chain corroboration. A new `is_description` predicate gates `single_term` and `parent_resolved` into a new `low_evidence` match type that emits no match while keeping its candidates visible. Supporting it is a `NameCache` addition that records the string each uuid was actually looked up under, since for a rewritten term that string is not the anchor.

**Tech Stack:** Python 3, pytest, symspellpy. No new dependencies.

Spec: `docs/superpowers/specs/2026-08-02-low-evidence-gate-design.md`

## Global Constraints

- Base commit: `a48b26b` "fix: stop spelling corrections from deleting exact matches in ranking". All line numbers below are relative to it. That commit rewrote the `single_term` and `parent_only` branches of `match_entry`, which Tasks 4 and 5 patch — do not work from an older checkout.
- All work is in `rtl_matcher.py` and `test_rtl_matcher.py` in `code/anaconda-2/`. No new modules.
- Baseline before starting: `python3 -m pytest test_rtl_matcher.py -q` reports `234 passed`. No task may reduce that count.
- Tests pass plain `dict` and `defaultdict(set)` objects as `name_cache`. Every new code path reading span or origin data must tolerate a cache that is not a `NameCache`, following the `isinstance` pattern at `rtl_matcher.py:268`.
- No behavior change to `chain_verified`, `chain_verified_proximity`, `chain_amb`, `single_amb`, `parent_amb`, `parent_rejected`, `no_auth_match`, or `no_terms`.
- Per this repo's practice, established in `docs/superpowers/specs/2026-07-14-rtl-matcher-cleanup-design.md`, nothing is committed without user review. Run each task's commit step only after the user approves that task.
- Acceptance target: only rows whose anchor span is an appellative may change. The seven inputs are named in Task 7, step 5. Anything else that moves is a regression.

## Superseded by `a48b26b`

An earlier revision of this plan carried a Task 6 that filtered spelling corrections by the input's trailing jurisdiction descriptor. `a48b26b` solved that family first and better, by pruning jurisdictions separately within the exact and correction groups so a fuzzy City can no longer delete an exact County. Measured on the six affected inputs, all now abstain to `parent_amb` instead of resolving wrong:

```
Highway 28, Sheboygan County          Cheboygan       -> parent_amb
Havre hospital, Hill county           Hill Country    -> parent_amb
office of the county clerk, Hunt Cty  Hunt Country    -> parent_amb
Norseland township, Nicollet County   Nicolet         -> parent_amb
...Coleview, Brazos County            Brazos Country  -> parent_amb
rural crossing...Dover and Zearing    Burgau          -> parent_amb
```

The dropped task would have been a regression: filtering corrections by jurisdiction resolves `Sheboygan County` to Cheboygan County, **Michigan**, where the current code correctly abstains. Do not reintroduce it. Span recording for both spelling functions still happens, in Task 3.

---

### Task 1: `is_description` predicate

The pure function both gates call. No callers yet, so it lands independently.

**Files:**
- Modify: `rtl_matcher.py` — insert after `BARE_JURISDICTION_WORDS` at line 1401
- Test: `test_rtl_matcher.py` — new class after `TestBareJurisdictionWords` (line 1878)

**Interfaces:**
- Consumes: `BARE_JURISDICTION_WORDS` (line 1401), already derived from `JURISDICTION_SUFFIXES` and `JURISDICTION_PREFIXES`
- Produces: `is_description(span, original) -> bool`, `APPELLATIVES`, `GENERIC_FEATURE_WORDS`, `DETERMINERS`, `_case_is_informative(text) -> bool`

- [x] **Step 1: Write the failing test**

Add to `test_rtl_matcher.py`. Import `is_description` in the `from rtl_matcher import (...)` block at the top of the file, keeping the list alphabetical.

```python
class TestIsDescription:
    """The toponym-ness gate: does this span name a place or describe one?"""

    def test_determiner_plus_appellative_is_a_description(self):
        assert is_description('the village', 'Lutheran church in the village')
        assert is_description('the city', 'north east section of the city')

    def test_bare_appellative_is_a_description(self):
        assert is_description('station', 'on car floor near station')
        assert is_description('City', '626 Michigan Street, City')
        assert is_description('city', '335 State St., city')

    def test_two_appellatives_are_a_real_name(self):
        # "Grove City" and "Lake Village" are places. Requiring exactly one
        # word after the determiner is what keeps them resolvable.
        assert not is_description('Grove City', 'Grove City')
        assert not is_description('Lake Village', 'Lake Village')

    def test_appellative_qualified_by_a_proper_name_is_a_real_name(self):
        assert not is_description('Camden Place',
                                  'near the great log jam north of Camden Place')
        assert not is_description('Wakarusa township',
                                  'residence of the brides parents in Wakarusa township')

    def test_uncapitalized_span_in_a_mixed_case_original_is_a_description(self):
        assert is_description('lenoir', 'Route 2, lenoir')

    def test_capitalized_span_passes_the_case_test(self):
        assert not is_description('South Vineland', 'cottage in South Vineland')
        assert not is_description('DaCosta', 'home of his parents in DaCosta')

    def test_case_test_stands_down_on_all_lowercase_originals(self):
        # No capitalization signal exists, so only the list test may fire.
        assert not is_description('despatch', 'near despatch')

    def test_case_test_stands_down_on_all_caps_originals(self):
        assert not is_description('BOYERTOWN', 'BOYER TOWN R. D. 2')

    def test_non_alphabetic_lead_skips_the_case_test(self):
        assert not is_description('1st Ward Detroit', 'Smith home, 1st Ward Detroit')

    def test_empty_span_is_not_a_description(self):
        assert not is_description('', 'anything')
        assert not is_description('   ', 'anything')

    def test_negative_cases_from_span_reconstruction_failures(self):
        """Spans that a wrong reconstruction produced during design. Each is a
        correct match and must survive. Guards against regressing to the anchor
        or to the first left-to-right preposition."""
        for span, original in [
            ('Dispatch', 'near Despatch'),
            ('Port Deposit', 'near Port De posit'),
            ('Rhinelander', 'near Rhineland er'),
            ('Cole Camp', 'Brauerville church, south of Cole Camp'),
            ('Kansas', 'farm home, east central Kansas'),
            ('Bozeman', 'Chapel of the Presbyterian Church in Bozeman'),
            ('Bergton', 'Crab Run Church of the Brethren in Bergton'),
            ('Polson', 'home of her daughter west of Polson'),
            ('Jugenheim', 'Castle of Heiligenberg, near Jugenheim'),
        ]:
            assert not is_description(span, original), f'{span!r} in {original!r}'
```

- [x] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest test_rtl_matcher.py::TestIsDescription -v`
Expected: collection error, `ImportError: cannot import name 'is_description'`

- [x] **Step 3: Write the implementation**

Insert in `rtl_matcher.py` immediately after the `BARE_JURISDICTION_WORDS = _derive_bare_jurisdiction_words()` line (1401).

```python
# Words that name a kind of place rather than a particular one. A span built
# from nothing but these is a description the source wrote, not a toponym,
# even when the authority happens to hold a record by that name — "a lutheran
# church in the village" is not a reference to The Village, Oklahoma.
GENERIC_FEATURE_WORDS = frozenset({
    'station', 'church', 'cemetery', 'farm', 'home', 'residence', 'hospital',
    'river', 'creek', 'lake', 'island', 'beach', 'hill', 'valley', 'mountain',
    'park', 'area', 'place', 'community', 'camp', 'mission', 'school',
    'hotel', 'depot', 'mine', 'bridge', 'road', 'street', 'avenue',
    'junction', 'crossing', 'landing', 'corner', 'center', 'centre', 'grove',
})

APPELLATIVES = BARE_JURISDICTION_WORDS | GENERIC_FEATURE_WORDS

DETERMINERS = frozenset({'the', 'a', 'an'})


def _case_is_informative(text):
    """True when the source distinguishes upper from lower case.

    All-caps and all-lowercase strings carry no capitalization signal, which
    is ordinary in OCR'd newspaper text, so the case test stands down there
    rather than rejecting every span in the row.
    """
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return False
    return not (all(c.isupper() for c in letters)
                or all(c.islower() for c in letters))


def is_description(span, original):
    """True when span reads as a description rather than a place name.

    Two independent tests, either of which rejects.

    The list test rejects an optional determiner followed by exactly one
    appellative — "the village", "city", "station". Requiring exactly one word
    is what keeps real names assembled from generic words ("Grove City",
    "Lake Village") resolvable.

    The case test rejects a span with a lowercase initial when the original
    string capitalizes anything at all. A span whose first character is not a
    letter is exempt, since digits carry no case.

    span is the string that actually reached the authority. For a term the
    pipeline rewrote before lookup that is not the anchor — see
    NameCache.span_of.
    """
    if not span or not span.strip():
        return False

    words = [w for w in (t.lower().strip('.,;:') for t in span.split()) if w]
    if words and words[0] in DETERMINERS:
        words = words[1:]
    if len(words) == 1 and words[0] in APPELLATIVES:
        return True

    head = span.strip()[0]
    if _case_is_informative(original) and head.isalpha() and not head.isupper():
        return True

    return False
```

- [x] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest test_rtl_matcher.py -q`
Expected: `245 passed` (234 baseline plus 11 new)

- [x] **Step 5: Commit**

```bash
git add rtl_matcher.py test_rtl_matcher.py
git commit -m "feat: add is_description toponym-ness predicate"
```

---

### Task 2: `NameCache` span recording

The gate needs the string a uuid was looked up under. Mirrors the existing `origins` mechanism exactly.

**Files:**
- Modify: `rtl_matcher.py:208-231` (`NameCache.__init__`, and new methods after `origin_of`)
- Modify: `rtl_matcher.py:277-279` (new `span_for` helper beside `lookup_name`)
- Test: `test_rtl_matcher.py` — extend `TestNameCacheProvenance` (line 2482)

**Interfaces:**
- Consumes: `NameCache` (line 197)
- Produces: `NameCache.record_span(key, uuid, span)`, `NameCache.span_of(key, uuid) -> str`, module-level `span_for(term, uuid, name_cache) -> str`

- [x] **Step 1: Write the failing test**

Add these methods inside the existing `TestNameCacheProvenance` class. Add `span_for` to the imports.

```python
    def test_records_the_span_a_uuid_was_looked_up_under(self):
        cache = NameCache()
        cache.current_origin = 'preposition'
        cache['lutheran church in the village'].add('U-VILLAGE')
        cache.record_span('lutheran church in the village', 'U-VILLAGE',
                          'the village')
        assert cache.span_of('lutheran church in the village',
                             'U-VILLAGE') == 'the village'

    def test_span_defaults_to_the_key(self):
        # Phases that look the term up verbatim record nothing, and the key is
        # the correct answer for them.
        cache = NameCache()
        cache.current_origin = 'exact'
        cache['albany'].add('U-ALBANY')
        assert cache.span_of('albany', 'U-ALBANY') == 'albany'

    def test_span_first_writer_wins(self):
        cache = NameCache()
        cache.record_span('near despatch', 'U-D', 'Despatch')
        cache.record_span('near despatch', 'U-D', 'near Despatch')
        assert cache.span_of('near despatch', 'U-D') == 'Despatch'

    def test_span_for_tolerates_a_plain_dict_cache(self):
        assert span_for('Albany', 'U-ALBANY', {'albany': {'U-ALBANY'}}) == 'albany'

    def test_span_for_lowercases_the_term_to_key_the_lookup(self):
        cache = NameCache()
        cache.record_span('near despatch', 'U-D', 'Despatch')
        assert span_for('near Despatch', 'U-D', cache) == 'Despatch'
```

- [x] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest test_rtl_matcher.py::TestNameCacheProvenance -v`
Expected: collection error, `ImportError: cannot import name 'span_for'`

- [x] **Step 3: Write the implementation**

In `rtl_matcher.py`, add `self.spans = {}` to `NameCache.__init__`:

```python
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.origins = {}
        self.spans = {}
        self.current_origin = self.DEFAULT_ORIGIN
```

Add two methods immediately after `origin_of` (line 231):

```python
    def record_span(self, key, uuid, span):
        """Remember the string that actually reached this uuid.

        A phase that rewrites the term before looking it up — transform,
        preposition extraction, spelling correction, cardinal strip — matched
        something other than the key, and the gate in match_entry needs that
        string rather than the anchor. First writer wins, matching record().
        """
        self.spans.setdefault((key, uuid), span)

    def span_of(self, key, uuid):
        """The string that matched, defaulting to the key for the phases that
        look the term up verbatim."""
        return self.spans.get((key, uuid), key)
```

Add the module-level helper immediately after `lookup_name` (line 279):

```python
def span_for(term, uuid, name_cache):
    """span_of for a cache that may be a plain dict.

    Tests and older callers pass defaultdict(set), which has no span table;
    the key is the right answer there because those caches only ever hold
    verbatim lookups. Mirrors how lookup_name_with_origin tolerates them.
    """
    key = term.lower()
    if isinstance(name_cache, NameCache):
        return name_cache.span_of(key, uuid)
    return key
```

- [x] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest test_rtl_matcher.py -q`
Expected: `250 passed`

- [x] **Step 5: Commit**

```bash
git add rtl_matcher.py test_rtl_matcher.py
git commit -m "feat: record the span each uuid was looked up under"
```

---

### Task 3: Wire `record_span` into the rewriting lookup phases

Four local phases and their four FileMaker twins. Each already has the looked-up string in a local variable at the point it writes the cache.

**Files:**
- Modify: `rtl_matcher.py:781-813` (`query_fallback_transforms_local`)
- Modify: `rtl_matcher.py:816-850` (`query_preposition_extractions_local`)
- Modify: `rtl_matcher.py:853-872` (`query_cardinal_strip_local`)
- Modify: `rtl_matcher.py:875-925` (`query_spelling_corrections_local`)
- Modify: `rtl_matcher.py:1828-1919` (`query_fallback_transforms`)
- Modify: `rtl_matcher.py:1922-1984` (`query_preposition_extractions`)
- Modify: `rtl_matcher.py:1987-2023` (`query_cardinal_strip`)
- Modify: `rtl_matcher.py:2223-2289` (`query_spelling_corrections`)
- Test: `test_rtl_matcher.py` — new class after `TestNameCacheProvenance`

**Interfaces:**
- Consumes: `NameCache.record_span` from Task 2
- Produces: no new names. After this task `span_of` returns the rewritten string for any uuid one of these eight phases supplied.

- [x] **Step 1: Write the failing test**

```python
class TestSpanWiring:
    """Every phase that rewrites a term before lookup must record what it
    actually looked up, or the gate tests the wrong string."""

    def test_spelling_correction_records_the_corrected_term(self):
        sym = SymSpell(max_dictionary_edit_distance=1, prefix_length=7)
        sym.create_dictionary_entry('birmingham', 1)
        cache = NameCache()
        cache.current_origin = 'spelling'
        client = MagicMock()
        client.find.return_value = [
            {'fieldData': {'Auth_Place_Name': 'Birmingham', 'UUID': 'u-birm',
                           'Jurisdiction': ''}}
        ]
        query_spelling_corrections(client, ['Birminghan'], cache, sym)
        assert cache.span_of('birminghan', 'u-birm') == 'birmingham'

    def test_preposition_extraction_records_the_extracted_span(self):
        cache = NameCache()
        cache.current_origin = 'preposition'
        client = MagicMock()
        client.find.return_value = [
            {'fieldData': {'Auth_Place_Name': 'Bozeman', 'UUID': 'u-boz',
                           'Jurisdiction': 'City'}}
        ]
        query_preposition_extractions(
            client, ['Chapel of the Presbyterian Church in Bozeman'], cache)
        assert cache.span_of(
            'chapel of the presbyterian church in bozeman', 'u-boz') == 'Bozeman'

    def test_cardinal_strip_records_the_stripped_form(self):
        cache = NameCache()
        cache.current_origin = 'cardinal_strip'
        client = MagicMock()
        client.find.return_value = [
            {'fieldData': {'Auth_Place_Name': 'Kansas', 'UUID': 'u-ks',
                           'Jurisdiction': 'State'}}
        ]
        query_cardinal_strip(client, ['east central Kansas'], cache,
                             {'east central Kansas': 'east central Kansas'})
        assert cache.span_of('east central kansas', 'u-ks') == 'Kansas'
```

- [x] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest test_rtl_matcher.py::TestSpanWiring -v`
Expected: FAIL, three assertions comparing the anchor against the rewritten string, e.g. `assert 'birminghan' == 'birmingham'`

- [x] **Step 3: Write the implementation**

In `query_fallback_transforms_local`, both branches of the `for orig, (cleaned, jurisdiction)` loop:

```python
        if jurisdiction:
            records = _LOCAL.pa_by_name.get(cleaned.lower(), [])
            for rec in records:
                if rec['UUID'] and rec['Jurisdiction'].lower() == jurisdiction.lower():
                    if rec['UUID'] not in name_cache.get(key, set()):
                        name_cache[key].add(rec['UUID'])
                        _record_span(name_cache, key, rec['UUID'], cleaned)
                        added += 1
        else:
            uuids = _query_name_local(cleaned)
            new_uuids = uuids - name_cache.get(key, set())
            if new_uuids:
                name_cache[key].update(new_uuids)
                for uid in new_uuids:
                    _record_span(name_cache, key, uid, cleaned)
                added += len(new_uuids)
```

In `query_preposition_extractions_local`, both branches of the `for name in names_to_try` loop, recording `name`:

```python
                if jurisdiction:
                    records = _LOCAL.pa_by_name.get(name.lower(), [])
                    for rec in records:
                        if rec['UUID'] and rec['Jurisdiction'].lower() == jurisdiction.lower():
                            if rec['UUID'] not in name_cache.get(key, set()):
                                name_cache[key].add(rec['UUID'])
                                _record_span(name_cache, key, rec['UUID'], name)
                                added += 1
                else:
                    uuids = _query_name_local(name)
                    new_uuids = uuids - name_cache.get(key, set())
                    if new_uuids:
                        name_cache[key].update(new_uuids)
                        for uid in new_uuids:
                            _record_span(name_cache, key, uid, name)
                        added += len(new_uuids)
```

In `query_cardinal_strip_local`, recording `stripped`:

```python
        uuids = _query_name_local(stripped)
        new_uuids = uuids - name_cache.get(key, set())
        if new_uuids:
            name_cache[key].update(new_uuids)
            for uid in new_uuids:
                _record_span(name_cache, key, uid, stripped)
            added += len(new_uuids)
```

In `query_spelling_corrections_local`, recording `candidate`:

```python
            new_uuids = uuids - name_cache.get(key, set())
            if new_uuids:
                name_cache[key].update(new_uuids)
                for uid in new_uuids:
                    _record_span(name_cache, key, uid, candidate)
                added += len(new_uuids)
```

In `query_fallback_transforms`, both authority loops. The `name` local is the looked-up string:

```python
                if uuid and name and name.lower() in lookup:
                    for orig in lookup[name.lower()]:
                        name_cache[orig.lower()].add(uuid)
                        _record_span(name_cache, orig.lower(), uuid, name)
                        non_jurisdiction_added += 1
```

In `query_preposition_extractions`:

```python
            if uuid and name and name.lower() in lookup:
                for orig, jur in lookup[name.lower()]:
                    if jur and record_jurisdiction.lower() != jur.lower():
                        continue
                    name_cache[orig.lower()].add(uuid)
                    _record_span(name_cache, orig.lower(), uuid, name)
                    added += 1
```

In `query_cardinal_strip`:

```python
            if uuid and name and name.lower() in lookup:
                for orig in lookup[name.lower()]:
                    key = orig.lower()
                    if uuid not in name_cache.get(key, set()):
                        name_cache[key].add(uuid)
                        _record_span(name_cache, key, uuid, name)
                        added += 1
```

In `query_spelling_corrections`:

```python
            new_uuids = uuids - name_cache.get(key, set())
            if new_uuids:
                name_cache[key].update(new_uuids)
                for uid in new_uuids:
                    _record_span(name_cache, key, uid, candidate)
                added += len(new_uuids)
```

Add the tolerant writer beside `span_for`, so none of the eight call sites needs an `isinstance` check:

```python
def _record_span(name_cache, key, uuid, span):
    """record_span for a cache that may be a plain dict. No-op on one."""
    if isinstance(name_cache, NameCache):
        name_cache.record_span(key, uuid, span)
```

- [x] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest test_rtl_matcher.py -q`
Expected: `253 passed`

- [x] **Step 5: Commit**

```bash
git add rtl_matcher.py test_rtl_matcher.py
git commit -m "feat: wire span recording through the rewriting lookup phases"
```

---

### Task 4: `low_evidence` match type and the `single_term` gate

**Files:**
- Modify: `rtl_matcher.py:2916-2928` (`CONFIDENCE_BY_TYPE`)
- Modify: `rtl_matcher.py:2941-2946` (`NON_RESOLUTION_KIND`)
- Modify: `rtl_matcher.py:3022-3044` (the `len(right_to_left) == 1` branch of `match_entry`)
- Test: `test_rtl_matcher.py` — new class after `TestSingleTermReclassification` (line 696)

**Interfaces:**
- Consumes: `is_description` (Task 1), `span_for` (Task 2)
- Produces: match type string `'low_evidence'`, confidence `'low'`, resolution kind `'suspect'`

- [x] **Step 1: Write the failing test**

```python
class TestLowEvidenceGateSingleTerm:
    """A lone anchor has no chain to corroborate it, so a span that reads as a
    description resolves to nothing while keeping its candidate visible."""

    def _cache_with_span(self, key, uuid, span, origin='preposition'):
        cache = NameCache()
        cache.current_origin = origin
        cache[key].add(uuid)
        cache.record_span(key, uuid, span)
        return cache

    def test_appellative_span_is_gated(self):
        cache = self._cache_with_span(
            'lutheran church in the village', 'U-VILLAGE', 'the village')
        auth_cache = {'U-VILLAGE': make_auth_record_full(
            'U-VILLAGE', level='4', name='The Village', jurisdiction='City')}
        result = match_entry(['Lutheran church in the village'], cache,
                             auth_cache, MagicMock(),
                             'Lutheran church in the village')
        assert result.match_type == 'low_evidence'
        assert result.candidate_ids == []
        assert result.tied_ids == ['U-VILLAGE']
        assert result.confidence == 'low'

    def test_gated_row_reports_resolution_kind_suspect(self):
        cache = self._cache_with_span(
            'on car floor near station', 'U-STATION', 'station')
        auth_cache = {'U-STATION': make_auth_record_full(
            'U-STATION', level='4', name='Station', jurisdiction='City')}
        result = match_entry(['on car floor near station'], cache, auth_cache,
                             MagicMock(), 'on car floor near station')
        assert resolution_kind(result.match_type) == 'suspect'

    def test_real_place_span_still_resolves(self):
        cache = self._cache_with_span(
            'cottage in south vineland', 'U-SV', 'South Vineland')
        auth_cache = {'U-SV': make_auth_record_full(
            'U-SV', level='4', name='South Vineland', jurisdiction='City')}
        result = match_entry(['cottage in South Vineland'], cache, auth_cache,
                             MagicMock(), 'cottage in South Vineland')
        assert result.match_type == 'single_term'
        assert result.candidate_ids == ['U-SV']

    def test_gate_applies_to_a_curated_mnt_mapping(self):
        # The MNT maps the bare term "city" to a real Missouri community. A
        # heuristic overriding curated data is deliberate: emitting a known
        # wrong match on a bad dictionary row is the worse outcome.
        cache = NameCache()
        cache.current_origin = 'mnt'
        cache['city'].add('U-CITY')
        auth_cache = {'U-CITY': make_auth_record_full(
            'U-CITY', level='4', name='City', jurisdiction='City')}
        result = match_entry(['City'], cache, auth_cache, MagicMock(), 'City')
        assert result.match_type == 'low_evidence'

    def test_gate_caps_a_large_candidate_array(self):
        ids = [f'c-{i}' for i in range(MAX_ARRAY + 4)]
        cache = NameCache()
        cache.current_origin = 'mnt'
        for uid in ids:
            cache['city'].add(uid)
        auth_cache = {uid: make_auth_record_full(uid, level='4', name='City',
                                                 jurisdiction='City')
                      for uid in ids}
        result = match_entry(['City'], cache, auth_cache, MagicMock(), 'City')
        assert result.match_type == 'low_evidence'
        assert len(result.tied_ids) == MAX_ARRAY

    def test_plain_dict_cache_falls_back_to_the_key_as_span(self):
        # No span table, so the key is the span. "wapakoneta" is not an
        # appellative and the original is all-lowercase, so it resolves.
        auth_cache = {'only': make_auth_record_full(
            'only', level='4', name='Wapakoneta', jurisdiction='City')}
        result = match_entry(['wapakoneta'], {'wapakoneta': {'only'}},
                             auth_cache, MagicMock(), 'wapakoneta')
        assert result.match_type == 'single_term'

    def test_gated_row_emits_no_match_but_keeps_its_candidate(self):
        """The output shape the gate exists to produce, asserted end to end
        through build_result_row rather than on MatchResult alone."""
        auth_cache = {'U-VILLAGE': make_auth_record_full(
            'U-VILLAGE', level='4', name='The Village', jurisdiction='City')}
        match = MatchResult(candidate_ids=[], depth=1,
                            match_type='low_evidence', tied_ids=['U-VILLAGE'])
        row = build_result_row(match, 'Lutheran church in the village', 'g1',
                               '1', auth_cache)
        assert row['authority_id'] == ''
        assert row['authority_name'] == ''
        assert row['type_ahead'] == ''
        assert row['jurisdiction'] == ''
        assert row['level'] == ''
        assert row['candidate_ids'] == 'U-VILLAGE'
        assert row['candidate_names'] == 'The Village'
        assert row['candidates'] == 1
        assert row['confidence'] == 'low'
        assert row['resolution_kind'] == 'suspect'
        # matched_uuid still reports the best guess, as it does for every
        # non-resolution, so level-scope reporting stays populated.
        assert row['matched_uuid'] == 'U-VILLAGE'
        assert row['matched_level'] == 4


class TestLowEvidenceConfidenceTables:
    def test_low_evidence_is_low_confidence(self):
        assert CONFIDENCE_BY_TYPE['low_evidence'] == 'low'

    def test_low_evidence_is_not_a_resolution(self):
        assert resolution_kind('low_evidence') == 'suspect'
```

Add `resolution_kind`, `NameCache`, and `MAX_ARRAY` to the imports if not already present. `MAX_ARRAY` and `CONFIDENCE_BY_TYPE` are already imported; `NameCache` and `resolution_kind` are imported further down the existing import block.

- [x] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest test_rtl_matcher.py::TestLowEvidenceGateSingleTerm test_rtl_matcher.py::TestLowEvidenceConfidenceTables -v`
Expected: FAIL, `assert 'single_term' == 'low_evidence'` and `KeyError: 'low_evidence'`

- [x] **Step 3: Write the implementation**

Add to `CONFIDENCE_BY_TYPE` (line 2916), after the `'parent_rejected': 'low',` entry:

```python
    'low_evidence': 'low',
```

Add to `NON_RESOLUTION_KIND` (line 2941), after the `'parent_rejected': 'suspect',` entry:

```python
    # A lone candidate won with nothing corroborating it and a span that reads
    # as a description. Closest of the three kinds: a single candidate that
    # the walk cannot stand behind.
    'low_evidence': 'suspect',
```

Replace the `len(right_to_left) == 1` branch of `match_entry` (lines 3022-3044). `a48b26b` replaced the old `len(ranked) == 1` short-circuit with `detect_tie`, so the gate goes above that call — a descriptive span must not resolve even when the ranking separates one candidate structurally.

```python
    if len(right_to_left) == 1:
        term_key = right_to_left[0].lower()
        hint = (jurisdiction_hints or {}).get(term_key)
        ranked = rank_candidates(list(parent_ids), auth_cache, None,
                                 jurisdiction_hint=hint, helper_term=helper_term,
                                 correction_uuids=_corr.get(term_key))
        all_ids = [uuid for uuid, _ in ranked]

        # No second term can corroborate a single-term match, so a span that
        # reads as a description resolves to nothing. Ahead of detect_tie:
        # structural separation says which candidate the ranking prefers, not
        # that the string names a place at all. The candidate is still
        # surfaced; the row just stops claiming it.
        if all_ids and is_description(
                span_for(right_to_left[0], all_ids[0], name_cache),
                original):
            return MatchResult([], depth=1, match_type='low_evidence',
                               tied_ids=cap_candidates(all_ids, "low_evidence"),
                               steps=[anchor_step])

        # Structural separation resolves, same rule the chain walk uses. A
        # lone live exact match standing above a pile of spelling corrections
        # is an answer, not a tie -- "Chiago" is an MNT-curated mapping to
        # Chicago that also picks up Chisago County at edit distance 1.
        structural_winner, _ = detect_tie(ranked)
        if structural_winner:
            return MatchResult([structural_winner], depth=1,
                               match_type='single_term', steps=[anchor_step])
        winner = _disambiguate_by_frequency(right_to_left[0], all_ids,
                                            _LOCAL.dict_freq or {})
        if winner:
            return MatchResult([winner], depth=1, match_type='freq_resolved',
                               steps=[anchor_step])
        return MatchResult([], depth=1, match_type='single_amb',
                           tied_ids=cap_candidates(all_ids, "single_amb"),
                           steps=[anchor_step])
```

Note `all_ids` moved above the `len(ranked) == 1` check so the gate can read the top-ranked candidate. The two later uses are unchanged.

- [x] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest test_rtl_matcher.py -q`
Expected: `262 passed`

- [x] **Step 5: Commit**

```bash
git add rtl_matcher.py test_rtl_matcher.py
git commit -m "feat: gate descriptive single-term anchors to low_evidence"
```

---

### Task 5: `resolve_parent_match` gate

`resolve_parent_match` has no `name_cache`, so its signature grows one. There is exactly one production call site, at `rtl_matcher.py:4020` inside `_run_phase3`, which has `name_cache` in scope.

**Files:**
- Modify: `rtl_matcher.py:3654-3706` (`resolve_parent_match`)
- Modify: `rtl_matcher.py:4020` (the call site)
- Test: `test_rtl_matcher.py` — extend `TestResolveParentMatch` (line 1630)

**Interfaces:**
- Consumes: `is_description` (Task 1), `span_for` (Task 2), `'low_evidence'` (Task 4)
- Produces: `resolve_parent_match(match, terms, auth_cache, client, name_cache=None)` — the new parameter is keyword-with-default so the five existing tests in `TestResolveParentMatch` keep passing unchanged

- [x] **Step 1: Write the failing test**

Add to the existing `TestResolveParentMatch` class.

```python
    def test_appellative_anchor_is_gated_to_low_evidence(self):
        cache = NameCache()
        cache.current_origin = 'mnt'
        cache['city'].add('U-CITY')
        auth_cache = {'U-CITY': make_auth_record_full(
            'U-CITY', level='4', name='City', jurisdiction='City')}
        match = self._parent_only(['U-CITY'], had_candidates=False)
        result = resolve_parent_match(match, ['626 Michigan Street', 'City'],
                                      auth_cache, MagicMock(), cache)
        assert result.match_type == 'low_evidence'
        assert result.candidate_ids == []
        assert result.tied_ids == ['U-CITY']
        assert result.confidence == 'low'
        assert result.skipped_terms == 'Bad String'

    def test_real_parent_name_still_resolves(self):
        cache = NameCache()
        cache.current_origin = 'exact'
        cache['texas'].add('tx')
        auth_cache = {'tx': make_auth_record_full(
            'tx', level='6', name='Texas', population='29000000')}
        match = self._parent_only(['tx'], had_candidates=False)
        result = resolve_parent_match(match, ['Bad String', 'Texas'],
                                      auth_cache, MagicMock(), cache)
        assert result.match_type == 'parent_resolved'

    def test_gate_precedes_the_rejected_branch(self):
        # A descriptive anchor is not a suspect parent standing in for a
        # dropped specific; it is not a place at all. low_evidence wins.
        cache = NameCache()
        cache.current_origin = 'mnt'
        cache['city'].add('U-CITY')
        auth_cache = {'U-CITY': make_auth_record_full(
            'U-CITY', level='4', name='City', jurisdiction='City')}
        match = self._parent_only(['U-CITY'], had_candidates=True)
        result = resolve_parent_match(match, ['626 Michigan Street', 'City'],
                                      auth_cache, MagicMock(), cache)
        assert result.match_type == 'low_evidence'

    def test_omitted_name_cache_leaves_behavior_unchanged(self):
        auth_cache = {'tx': make_auth_record_full(
            'tx', level='6', name='Texas', population='29000000')}
        match = self._parent_only(['tx'], had_candidates=False)
        result = resolve_parent_match(match, ['Bad String', 'Texas'],
                                      auth_cache, MagicMock())
        assert result.match_type == 'parent_resolved'
```

- [x] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest test_rtl_matcher.py::TestResolveParentMatch -v`
Expected: FAIL, `TypeError: resolve_parent_match() takes 4 positional arguments but 5 were given`

- [x] **Step 3: Write the implementation**

Change the signature and insert the gate ahead of the existing resolution branch:

```python
def resolve_parent_match(match, terms, auth_cache, client, name_cache=None):
```

Extend the docstring with a fourth bullet after the three existing ones:

```
      - The anchor reads as a description rather than a place name: nothing
        corroborated it and the string is not a toponym. Gate -> low_evidence.
```

Insert immediately after the `winner, resolution = resolve_parent_only(...)` call and before `if resolution in ('parent_resolved', 'freq_resolved'):`

```python
    # The anchor carried this row on its own. If it reads as a description
    # rather than a name, there is nothing behind it — checked before the
    # rejected branch, since a descriptive anchor is not a suspect parent
    # standing in for a dropped specific, it is not a place at all.
    if winner and is_description(
            span_for(terms[-1], winner, name_cache or {}), terms[-1]):
        return MatchResult(
            candidate_ids=[],
            depth=match.depth,
            match_type='low_evidence',
            skipped_count=match.skipped_count,
            skipped_terms=match.skipped_terms,
            steps=match.steps,
            tied_ids=cap_candidates(list(match.candidate_ids), "low_evidence"),
        )
```

Both arguments are `terms[-1]`: the span defaults to the anchor for a verbatim
lookup, and the anchor is also the `original` whose case is the signal. This
differs from `match_entry`, which passes the whole input string, because a
single-term row has no anchor separate from the input while a parent-only row
does.

Update the call site at line 4020:

```python
        if match.match_type == 'parent_only' and match.candidate_ids:
            match = resolve_parent_match(match, terms, auth_cache, client,
                                         name_cache)
            if match.match_type == 'parent_rejected':
                recoverable_rejects += 1
```

- [x] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest test_rtl_matcher.py -q`
Expected: `266 passed`

- [x] **Step 5: Commit**

```bash
git add rtl_matcher.py test_rtl_matcher.py
git commit -m "feat: gate descriptive parent-only anchors to low_evidence"
```


### Task 6: MNT defect report

A gated anchor whose uuid came from the MNT is a dictionary defect. Collected during the run and merged into one global file so the same bad term is not rediscovered every corpus.

**Files:**
- Modify: `rtl_matcher.py` — new constant and two functions near `write_spelling_log` (line 2292)
- Modify: `rtl_matcher.py:3926` (`_run_phase3` signature and the gate branches, to collect defects)
- Modify: `rtl_matcher.py:4035` (new `--mnt-defects` argument)
- Modify: `rtl_matcher.py:4019` (main writes the file)
- Test: `test_rtl_matcher.py` — new class after `TestWriteSpellingLog` (line 1115)

**Interfaces:**
- Consumes: `NameCache.origin_of` (line 230), `'low_evidence'` (Task 4)
- Produces: `MNT_DEFECT_FIELDS`, `merge_mnt_defects(defects, path)`, `collect_mnt_defect(match, terms, name_cache, auth_cache, input_stem) -> dict | None`

- [x] **Step 1: Write the failing test**

```python
class TestMntDefectReport:
    """Gated terms whose uuid came from the curated dictionary are defects in
    that dictionary, tracked in one global file rather than per run."""

    def test_collects_a_gated_mnt_term(self):
        cache = NameCache()
        cache.current_origin = 'mnt'
        cache['city'].add('U-CITY')
        auth_cache = {'U-CITY': make_auth_record_full(
            'U-CITY', level='4', name='City', jurisdiction='City')}
        match = MatchResult(candidate_ids=[], depth=1,
                            match_type='low_evidence', tied_ids=['U-CITY'])
        defect = collect_mnt_defect(match, ['626 Michigan Street', 'City'],
                                    cache, auth_cache, 'sample_01')
        assert defect['term'] == 'city'
        assert defect['uuid'] == 'U-CITY'
        assert defect['auth_name'] == 'City'
        assert defect['jurisdiction'] == 'City'
        assert defect['last_input'] == 'sample_01'

    def test_ignores_a_gated_term_from_a_heuristic_origin(self):
        # Only curated mappings are dictionary defects. A bad preposition
        # extraction is a matcher behavior, already handled by the gate.
        cache = NameCache()
        cache.current_origin = 'preposition'
        cache['lutheran church in the village'].add('U-VILLAGE')
        auth_cache = {'U-VILLAGE': make_auth_record_full(
            'U-VILLAGE', level='4', name='The Village', jurisdiction='City')}
        match = MatchResult(candidate_ids=[], depth=1,
                            match_type='low_evidence', tied_ids=['U-VILLAGE'])
        assert collect_mnt_defect(
            match, ['Lutheran church in the village'], cache, auth_cache,
            'sample_01') is None

    def test_ignores_a_row_that_was_not_gated(self):
        cache = NameCache()
        cache.current_origin = 'mnt'
        cache['texas'].add('tx')
        auth_cache = {'tx': make_auth_record_full('tx', level='6', name='Texas')}
        match = MatchResult(candidate_ids=['tx'], depth=1,
                            match_type='parent_resolved')
        assert collect_mnt_defect(match, ['Bad String', 'Texas'], cache,
                                  auth_cache, 'sample_01') is None

    def test_merge_writes_a_new_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'mnt_defects.tsv')
            merge_mnt_defects([{'term': 'city', 'uuid': 'U-CITY',
                                'auth_name': 'City', 'jurisdiction': 'City',
                                'rows_last_run': 2, 'last_input': 'sample_01'}],
                              path)
            with open(path, encoding='utf-8') as f:
                rows = list(csv.DictReader(f, delimiter='\t'))
            assert len(rows) == 1
            assert rows[0]['term'] == 'city'
            assert rows[0]['rows_last_run'] == '2'

    def test_merge_is_idempotent_on_the_same_pair(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'mnt_defects.tsv')
            row = {'term': 'city', 'uuid': 'U-CITY', 'auth_name': 'City',
                   'jurisdiction': 'City', 'rows_last_run': 2,
                   'last_input': 'sample_01'}
            merge_mnt_defects([row], path)
            merge_mnt_defects([dict(row, rows_last_run=5,
                                    last_input='sample_02')], path)
            with open(path, encoding='utf-8') as f:
                rows = list(csv.DictReader(f, delimiter='\t'))
            assert len(rows) == 1
            # Overwritten, not accumulated. Overlapping samples would inflate
            # a running total.
            assert rows[0]['rows_last_run'] == '5'
            assert rows[0]['last_input'] == 'sample_02'

    def test_one_term_can_carry_several_mappings(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'mnt_defects.tsv')
            merge_mnt_defects([
                {'term': 'city', 'uuid': 'U-CITY', 'auth_name': 'City',
                 'jurisdiction': 'City', 'rows_last_run': 2,
                 'last_input': 'sample_01'},
                {'term': 'city', 'uuid': 'U-LONDON', 'auth_name': 'London',
                 'jurisdiction': 'City', 'rows_last_run': 2,
                 'last_input': 'sample_01'},
            ], path)
            with open(path, encoding='utf-8') as f:
                rows = list(csv.DictReader(f, delimiter='\t'))
            assert len(rows) == 2
            assert {r['uuid'] for r in rows} == {'U-CITY', 'U-LONDON'}
```

Add `import csv` to the test file's imports, and `collect_mnt_defect` and `merge_mnt_defects` to the `rtl_matcher` import block.

- [x] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest test_rtl_matcher.py::TestMntDefectReport -v`
Expected: collection error, `ImportError: cannot import name 'collect_mnt_defect'`

- [x] **Step 3: Write the implementation**

Add after `write_spelling_log` (line 2297, unmoved):

```python
MNT_DEFECT_FIELDS = ['term', 'uuid', 'auth_name', 'jurisdiction',
                     'rows_last_run', 'last_input']


def collect_mnt_defect(match, terms, name_cache, auth_cache, input_stem):
    """Report a gated row whose candidate came from the curated dictionary.

    The MNT is harvested from prior normalizations, so a mapping that was
    right for one record becomes a global rule. When the gate rejects a term
    the MNT supplied, the dictionary row is the defect and wants fixing at the
    source. Returns None for any other row.
    """
    if match.match_type != 'low_evidence' or not match.tied_ids:
        return None
    if not isinstance(name_cache, NameCache):
        return None
    key = terms[-1].lower()
    uuid = match.tied_ids[0]
    if name_cache.origin_of(key, uuid) != 'mnt':
        return None
    record = auth_cache.get(uuid, {})
    return {
        'term': key,
        'uuid': uuid,
        'auth_name': field_str(record, 'Auth_Place_Name'),
        'jurisdiction': field_str(record, 'Jurisdiction'),
        'rows_last_run': 1,
        'last_input': input_stem,
    }


def merge_mnt_defects(defects, path):
    """Merge this run's defects into the global file, keyed (term, uuid).

    Global rather than per run so a dictionary defect is not rediscovered on
    every corpus. rows_last_run is overwritten rather than accumulated: input
    samples overlap, so a running total would inflate on a rerun of the same
    data.
    """
    merged = {}
    if os.path.exists(path):
        with open(path, encoding='utf-8-sig') as f:
            for row in csv.DictReader(f, delimiter='\t'):
                merged[(row['term'], row['uuid'])] = row

    counts = defaultdict(int)
    for d in defects:
        counts[(d['term'], d['uuid'])] += d.get('rows_last_run', 1)

    for d in defects:
        pair = (d['term'], d['uuid'])
        merged[pair] = dict(d, rows_last_run=counts[pair])

    with open(path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=MNT_DEFECT_FIELDS, delimiter='\t')
        writer.writeheader()
        for pair in sorted(merged):
            writer.writerow({k: merged[pair].get(k, '') for k in MNT_DEFECT_FIELDS})
```

`_run_phase3` (line 3971) both runs the loop and writes every output file, so `mnt_defects` stays local to it and no signature or return value changes.

Add the accumulator beside the existing three at line 3985:

```python
    results = []
    ties = []
    level_provenance = []
    mnt_defects = []
    recoverable_rejects = 0
```

Both gates land in the same loop: `match_entry` can return `low_evidence` directly, and `resolve_parent_match` can convert a `parent_only` into one. Collect after the `resolve_parent_match` block at line 4020, so one call covers both:

```python
        if match.match_type == 'low_evidence':
            defect = collect_mnt_defect(
                match, terms, name_cache, auth_cache,
                os.path.splitext(os.path.basename(args.input))[0])
            if defect:
                mnt_defects.append(defect)
```

Add the CLI argument after `--mnt` (line 4082):

```python
    parser.add_argument('--mnt-defects', default='./mnt_defects.tsv',
                        help="Global MNT defect report, merged across runs "
                             "(default: ./mnt_defects.tsv)")
```

In `main`, after the `write_segment_log` block (line 4067):

```python
    if mnt_defects:
        merge_mnt_defects(mnt_defects, args.mnt_defects)
        log.info("  MNT defects: %d rows merged into %s",
                 len(mnt_defects), args.mnt_defects)
```

- [x] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest test_rtl_matcher.py -q`
Expected: `272 passed`

- [x] **Step 5: Commit**

```bash
git add rtl_matcher.py test_rtl_matcher.py
git commit -m "feat: merge gated MNT terms into a global defect report"
```

---

### Task 7: Summary line and full-run verification

`print_summary` iterates a hardcoded ordered list of match types. A type absent from that list is silently omitted from the run summary.

**Files:**
- Modify: `rtl_matcher.py:3462-3465` (the match-type list in `print_summary`)
- Test: `test_rtl_matcher.py` — new class after `TestConfidenceTier` (line 1802)

**Interfaces:**
- Consumes: everything from Tasks 1 through 6
- Produces: nothing new

- [x] **Step 1: Write the failing test**

```python
class TestSummaryCoversEveryMatchType:
    def test_every_confidence_table_type_appears_in_the_summary(self):
        """A type missing from print_summary's list is invisible in the run
        summary, which is how a new bucket goes unnoticed."""
        import inspect
        source = inspect.getsource(print_summary)
        for match_type in CONFIDENCE_BY_TYPE:
            assert f"'{match_type}'" in source, match_type
```

Add `print_summary` to the imports.

- [x] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest test_rtl_matcher.py::TestSummaryCoversEveryMatchType -v`
Expected: FAIL, `AssertionError: low_evidence`

- [x] **Step 3: Write the implementation**

Add `'low_evidence'` to the list in `print_summary`, after `'parent_rejected'`:

```python
    for match_type in ['mnt_full_string', 'chain_verified', 'chain_verified_proximity',
                       'chain_amb', 'single_term', 'single_amb', 'freq_resolved',
                       'parent_resolved', 'parent_rejected', 'low_evidence',
                       'parent_only', 'parent_amb',
                       'illegible', 'no_auth_match', 'no_terms']:
```

- [x] **Step 4: Run the full suite**

Run: `python3 -m pytest test_rtl_matcher.py -q`
Expected: `273 passed`

- [x] **Step 5: Rerun the corpus and diff against the acceptance target**

A pre-gate baseline was captured on `a48b26b` and is the comparison point. The `rtl-outputs/08-01/` files predate that commit and must NOT be used — six county rows changed between them, and diffing against 08-01 would attribute those to this work.

```
BASELINE=/private/tmp/claude-501/-Users-natelemonnier-storied/88d7a7c1-20d4-45e2-a389-17f1145b4d0d/scratchpad/baseline-a48b26b/08-04/snowball2_sample_5k_01.tsv
```

If that scratchpad file is gone, regenerate it by stashing this branch's changes, running the command below on `a48b26b`, then restoring.

```bash
echo "" | python3 rtl_matcher.py \
  --input ~/storied/resources/Snowball2-new/snowball2_sample_5k.tsv \
  --pa ~/storied/resources/place-authority-mnt-tsv/PA6_16_2026v77.tsv \
  --mnt ~/storied/resources/place-authority-mnt-tsv/Master_Normalization_File_6_16_2026v1.tsv
```

The `echo ""` answers the interactive helper-term prompt; without it the run dies with `EOFError`. The summary's last line prints the output path, of the form `rtl-outputs/MM-DD/snowball2_sample_5k_NN.tsv`. Pass it and the baseline to the diff:

```bash
python3 - "$BASELINE" "$NEW_OUTPUT_PATH" <<'EOF'
import csv, sys
old = {r['guid']: r for r in csv.DictReader(
    open(sys.argv[1], encoding='utf-8'), delimiter='\t')}
new = {r['guid']: r for r in csv.DictReader(
    open(sys.argv[2], encoding='utf-8'), delimiter='\t')}
changed = [g for g in old if g in new
           and (old[g]['match_type'] != new[g]['match_type']
                or old[g]['authority_id'] != new[g]['authority_id'])]
print(f'changed rows: {len(changed)}')
for g in changed:
    print(f"  {old[g]['original'][:46]:48} "
          f"{old[g]['match_type']} -> {new[g]['match_type']}  "
          f"{old[g]['authority_name']!r} -> {new[g]['authority_name']!r}")
EOF
```

Expected: exactly 7 changed rows, every one of them an appellative anchor going to `low_evidence` with its authority name blanked. Measured on the baseline, five arrive through Task 4's gate and two through Task 5's:

```
single_term     Lutheran church in the village                    The Village
single_term     Bant Main Street road two miles east of the city  The City
single_term     elev a miles northwest of the city                The City
single_term     north east section of the city                    The City
single_term     on car floor near station                         Station
parent_resolved 626 Michigan Street, City                         City
parent_resolved 335 State St., city                               City
```

Any row outside this list is a regression — stop and diagnose before committing. In particular the six county inputs (`Hill county`, `Sheboygan County`, `Hunt County`, `Nicollet County`, `Brazos County`, `Bureau County`) already sit at `parent_amb` in the baseline and must stay there; if any of them moves, the dropped Task 6 has crept back in.

Also confirm the defect file was written:

```bash
cat mnt_defects.tsv
```

Expected: a header plus two rows, both `term = city`, with different uuids.

- [x] **Step 6: Commit**

```bash
git add rtl_matcher.py test_rtl_matcher.py
git commit -m "feat: report low_evidence in the run summary"
```

---

## Notes for the implementer

**Why the span is recorded rather than recomputed.** Two attempts to reconstruct it from output files during design each produced a different wrong answer, and each wrong answer gated a batch of correct matches. Treating the anchor as the span gated 16 correct rows, because a transform-origin anchor keeps a lowercase leading function word (`near Despatch`) that the transform strips before lookup. Taking the first preposition left-to-right gated 12, because `extract_after_preposition` sorts candidates shortest-first. If Task 3 is done incompletely, Task 4's case test is what will misfire, and it will misfire on correct matches.

**Known residual, not a bug.** `Sheboygan County` (Wisconsin) resolves to Cheboygan County, Michigan after Task 6. Type-correct, wrong state. A parent-only row carries no state context capable of rejecting it, and this is recorded as accepted in the spec.

**`resolution_kind` imprecision.** `low_evidence` reuses `'suspect'`, documented at `rtl_matcher.py:2934` as "a single candidate won, but the walk dropped a more specific term". That is not quite what a gated row is. Reusing it avoids updating downstream consumers. If a consumer needs to distinguish the two, add a fourth kind rather than overloading further.
