# Dict-Mode Reintegration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reintegrate the parked `wip/dict-mode` work into `rtl_matcher.py` as a union data source (MNT TSV + Supabase dictionary), plus a full-string MNT fast path, an illegible stop-list, and frequency-based disambiguation.

**Architecture:** The Supabase `place_term_dictionary` supplements (never replaces) the MNT TSV: both feed the same `mnt_by_raw` segment index, with per-(term, uuid) frequency kept for dict-origin pairs. A new full-string index unlocks the 1.13M comma-containing MNT rows that segment lookup can never reach. `place_term_illegible` (60k curated junk terms) becomes a stop-list that blocks spelling correction and produces a distinct `illegible` status. Frequency disambiguates ambiguous candidates before population rules, guarded by a skew threshold, and only when every candidate has frequency data.

**Tech Stack:** Python 3, psycopg2 (only when `--dict live`), symspellpy, pytest.

## Design Decisions (settled by data analysis + grilling, 2026-07-16)

- Pure swap to dictionary loses 63k clean single-element MNT terms plus ~350k full-string resolutions not reproducible from segments. Union chosen.
- Dictionary and MNT agree on 99.7% of shared terms; disagreements just merge into the candidate set and disambiguate normally.
- Full-string MNT index: 1,134,373 unique canonical strings, only 2 ambiguous (excluded).
- Frequency stage: fires after chain verification, before population rules; requires every candidate to have a freq entry (mixed dict/MNT candidate sets skip straight to population); winner needs freq >= 10 and >= 5x runner-up.
- `level`/`jurisdiction` columns from the dictionary are NOT used in this change (follow-up work).
- `dictionary_coverage_audit.py` from `wip/dict-mode` is not ported.
- Backward compatibility: with no `--dict` flag, behavior is identical to main except the full-string fast path.

## Global Constraints

- No behavior change to FM/API mode (`--api`).
- `psycopg2` import stays inside the live loader function — TSV-only users must not need it installed.
- Deterministic outputs: any iteration over sets that affects output must sort first (main commit 2292dba established this).
- Supabase connection: host `aws-1-us-west-1.pooler.supabase.com`, port 5432, dbname `postgres`, user `parser_readonly.ncahtzbmazzqrorjkjwm`, password from `SUPABASE_PASSWORD` env var (loadable via `--env` file at `code/anaconda-2/.env`).
- New match_type values must be added to the summary printer list (`rtl_matcher.py:2137`).
- All work on a fresh branch off `main`. `wip/dict-mode` stays untouched until final cleanup.

---

### Task 1: Full-string canonicalizer and index

**Files:**
- Modify: `rtl_matcher.py` (LocalData class, `__init__` ~line 262, `_load_mnt` ~line 306)
- Test: `test_rtl_matcher.py`

**Interfaces:**
- Produces: `canonicalize_place(s: str) -> str` (module level); `LocalData.fs_by_raw: dict[str, str]` mapping canonical full string -> single UUID, built during `_load_mnt`, ambiguous strings excluded.

- [ ] **Step 1: Create branch**

```bash
cd /Users/natelemonnier/storied/code/anaconda-2
git checkout main && git pull && git checkout -b 20260716-dict-union
```

- [ ] **Step 2: Write failing tests**

Add to `test_rtl_matcher.py`:

```python
import rtl_matcher
from rtl_matcher import canonicalize_place, LocalData


class TestCanonicalizePlace:
    def test_lowercases_and_normalizes_separators(self):
        assert canonicalize_place("Danville,VA ,  United States") == "danville, va, united states"

    def test_semicolons_treated_like_commas(self):
        assert canonicalize_place("Boston; Mass") == "boston, mass"

    def test_single_segment_passthrough(self):
        assert canonicalize_place("  Hesse ") == "hesse"


def _write_tsv(path, header, rows):
    with open(path, 'w', encoding='utf-8') as f:
        f.write('\t'.join(header) + '\n')
        for r in rows:
            f.write('\t'.join(r) + '\n')


U1 = '11111111-1111-1111-1111-111111111111'
U2 = '22222222-2222-2222-2222-222222222222'


class TestFullStringIndex:
    def test_comma_rows_build_fs_index(self, tmp_path):
        mnt = str(tmp_path / 'mnt.tsv')
        _write_tsv(mnt, ['_value', '_ID', '_raw', '_geoclass'], [
            ['Danville', U1, 'Danville,VA, United States', 'US'],
            ['Hesse', U2, 'Hesse', 'Global'],
        ])
        ld = LocalData()
        ld._load_mnt(mnt)
        assert ld.fs_by_raw == {'danville, va, united states': U1}

    def test_ambiguous_full_strings_excluded(self, tmp_path):
        mnt = str(tmp_path / 'mnt.tsv')
        _write_tsv(mnt, ['_value', '_ID', '_raw', '_geoclass'], [
            ['A', U1, 'Weston, Ontario, Canada', 'CA'],
            ['B', U2, 'Weston, Ontario, Canada', 'CA'],
        ])
        ld = LocalData()
        ld._load_mnt(mnt)
        assert ld.fs_by_raw == {}
```

- [ ] **Step 3: Run tests, verify failure**

Run: `python3 -m pytest test_rtl_matcher.py -k "Canonicalize or FullString" -v`
Expected: FAIL / ImportError (`canonicalize_place` not defined).

- [ ] **Step 4: Implement**

Module level, near `_is_valid_local_uuid` (~line 250):

```python
def canonicalize_place(s):
    """Lowercase a place string and normalize separators: split on [,;],
    strip each segment, rejoin with ', '. Both the full-string index keys
    and lookups run through this so spacing variants collapse."""
    parts = [p.strip() for p in re.split(r'[,;]', s.lower()) if p.strip()]
    return ', '.join(parts)
```

In `LocalData.__init__` add:

```python
        self.fs_by_raw = None       # canonical full string -> single UUID
        self.dict_freq = None       # (term_lower, uuid_upper) -> frequency
        self.illegible = set()      # curated junk terms (lowercase)
```

In `_load_mnt`, before the read loop add `fs_tmp = defaultdict(set)`; inside the valid-UUID branch (where `raw` is truthy) add:

```python
                        if ',' in raw or ';' in raw:
                            fs_tmp[canonicalize_place(raw)].add(uid)
```

After the read loop:

```python
        self.fs_by_raw = {k: next(iter(v)) for k, v in fs_tmp.items()
                          if len(v) == 1}
        log.info("  MNT full-string index: %d entries (%d ambiguous dropped)",
                 len(self.fs_by_raw), sum(1 for v in fs_tmp.values() if len(v) > 1))
```

- [ ] **Step 5: Run tests, verify pass**

Run: `python3 -m pytest test_rtl_matcher.py -v`
Expected: new tests PASS, all pre-existing tests still PASS.

- [ ] **Step 6: Commit**

```bash
git add rtl_matcher.py test_rtl_matcher.py
git commit -m "feat: build full-string MNT index during local load"
```

---

### Task 2: Full-string fast path in the pipeline

**Files:**
- Modify: `rtl_matcher.py` — `main()` (~line 2373 after `parse_entries`, ~line 2478 before Phase 2), `_run_phase3` (~line 2513), summary list (~line 2137)

**Interfaces:**
- Consumes: `canonicalize_place`, `_LOCAL.fs_by_raw` (Task 1).
- Produces: `_run_phase3(args, parsed, name_cache, auth_cache, client, jurisdiction_hints, ascii_cache, helper_term, spelling_corrections, start, elapsed, fs_hits=None)`; new match_type `mnt_full_string`.

- [ ] **Step 1: Wire the fast path**

In `main()` after `parsed, all_terms, jurisdiction_hints = parse_entries(entries)`:

```python
    fs_hits = {}
    if args.local and _LOCAL.fs_by_raw:
        for place, _guid, _freq, _terms in parsed:
            uid = _LOCAL.fs_by_raw.get(canonicalize_place(place))
            if uid:
                fs_hits[place] = uid
        log.info("  Full-string MNT fast path: %d of %d entries pre-resolved",
                 len(fs_hits), len(parsed))
```

Before Phase 2 (`auth_cache = fn_auth_batch(all_auth_ids)`), make the fast-path UUIDs resolvable:

```python
    all_auth_ids.update(fs_hits.values())
```

Pass `fs_hits` into `_run_phase3(..., fs_hits=fs_hits)` and add the parameter (`fs_hits=None`). In the entry loop, replace the direct `match_entry` call:

```python
        fs_uid = (fs_hits or {}).get(place)
        if fs_uid and fs_uid in auth_cache:
            match = MatchResult([fs_uid], depth=len(terms),
                                match_type='mnt_full_string')
        else:
            match = match_entry(terms, name_cache, auth_cache, client, place,
                                jurisdiction_hints=jurisdiction_hints,
                                ascii_cache=ascii_cache,
                                helper_term=helper_term)
```

(The `fs_uid in auth_cache` guard is the staleness check: an MNT UUID absent from the PA is dead and falls through to the normal pipeline.)

Add `'mnt_full_string'` to the match_type list in the summary printer at ~line 2137.

- [ ] **Step 2: Smoke run**

```bash
printf 'place\tguid\tfrequency\nDanville, Va, United States\tg1\t3\n' > /tmp/fs_smoke.tsv
python3 rtl_matcher.py --local \
  --mnt "/Users/natelemonnier/storied/resources/place-authority-mnt-tsv/Master_Normalization_File_6_16_2026v1.tsv" \
  --pa "/Users/natelemonnier/storied/resources/place-authority-mnt-tsv/PA 6_16_2026v77.tsv" \
  --input /tmp/fs_smoke.tsv --output-dir /tmp/fs_smoke_out
```

Expected log line: `Full-string MNT fast path: 1 of 1 entries pre-resolved`; output row has `match_type=mnt_full_string` and a non-empty `authority_id`. 

- [ ] **Step 3: Run full test suite**

Run: `python3 -m pytest test_rtl_matcher.py -v`
Expected: PASS (no existing test touches `_run_phase3`'s signature).

- [ ] **Step 4: Commit**

```bash
git add rtl_matcher.py
git commit -m "feat: full-string MNT fast path before segment matching"
```

---

### Task 3: --dict flag and Supabase/TSV dictionary loaders (union)

**Files:**
- Modify: `rtl_matcher.py` — `LocalData.load` (~line 272), new loader methods after `_load_mnt`, `build_cli` (~line 2632), `main()` load call (~line 2340)
- Test: `test_rtl_matcher.py`
- Reference: `git show wip/dict-mode:rtl_matcher.py` (`_load_dict_live`, `_load_dict_tsv`) — port with the union changes below; do not port `_supplement_from_pa` (PA TSV stays the primary authority source).

**Interfaces:**
- Consumes: `LocalData` indexes from Task 1.
- Produces: `LocalData.load(mnt_path, pa_path, dict_source=None, env_path=None)`; `_LOCAL.dict_freq` populated; dict terms unioned into `mnt_by_raw`; Supabase-only authority places added to `pa_by_uuid`/`pa_by_name`; CLI flag `--dict [live|DIR]`.

- [ ] **Step 1: Write failing test (TSV mode)**

```python
class TestDictUnion:
    def test_dict_tsv_unions_into_mnt_index(self, tmp_path):
        mnt = str(tmp_path / 'mnt.tsv')
        _write_tsv(mnt, ['_value', '_ID', '_raw', '_geoclass'],
                   [['Hesse', U1, 'hessen', 'Global']])
        d = tmp_path / 'dictdir'
        d.mkdir()
        _write_tsv(str(d / 'place_term_dictionary.tsv'),
                   ['term', 'authority_uuid', 'level', 'jurisdiction', 'frequency'],
                   [['hesse', U2, '2', 'Germany', '40'],
                    ['hessen', U2, '2', 'Germany', '7']])
        ld = LocalData()
        ld._load_mnt(mnt)
        ld._load_dict_tsv(str(d))
        assert ld.mnt_by_raw['hessen'] == {U1, U2}   # union, both sources
        assert ld.mnt_by_raw['hesse'] == {U2}
        assert ld.dict_freq[('hesse', U2)] == 40
```

- [ ] **Step 2: Run test, verify failure**

Run: `python3 -m pytest test_rtl_matcher.py -k DictUnion -v`
Expected: FAIL (`_load_dict_tsv` not defined).

- [ ] **Step 3: Implement loaders**

`LocalData.load` becomes:

```python
    def load(self, mnt_path, pa_path, dict_source=None, env_path=None):
        """Read the TSVs into dictionaries keyed for the lookups we do.
        dict_source: None, 'live' (Supabase), or a directory of TSV exports
        (place_term_dictionary.tsv, place_term_illegible.tsv). Dict terms
        union into mnt_by_raw; they never replace MNT data."""
        if self._loaded:
            return
        start = time.time()
        log.info("Loading local data...")
        self._load_mnt(mnt_path)
        self._load_pa(pa_path)
        if dict_source == 'live':
            self._load_dict_live(env_path)
        elif dict_source:
            self._load_dict_tsv(dict_source)
        log.info("  Local data loaded in %.1fs", time.time() - start)
        self._loaded = True
```

Init `self.dict_freq = {}` inside `_load_mnt` (alongside the other index initializations) so it is always a dict in local mode.

New methods (adapted from `wip/dict-mode`; union semantics — `mnt_by_raw` is already populated, we only `.add`):

```python
    def _ingest_dict_row(self, term, uid, freq):
        key = term.lower()
        self.mnt_by_raw[key].add(uid)
        dh = key.replace('-', ' ')
        if dh != key:
            self.mnt_by_raw[dh].add(uid)
        self.dict_freq[(key, uid)] = freq

    def _load_dict_live(self, env_path):
        import psycopg2
        if env_path:
            _load_env_file(env_path)
        password = os.environ.get('SUPABASE_PASSWORD')
        if not password:
            raise ValueError("SUPABASE_PASSWORD not set. Use --env or export it.")
        conn = psycopg2.connect(
            host="aws-1-us-west-1.pooler.supabase.com", port=5432,
            dbname="postgres", user="parser_readonly.ncahtzbmazzqrorjkjwm",
            password=password)
        cur = conn.cursor()
        log.info("  Pulling place_term_dictionary...")
        cur.execute("SELECT term, authority_uuid, frequency "
                    "FROM place_term_dictionary")
        n = 0
        for term, uuid, freq in cur:
            self._ingest_dict_row(term.strip(), str(uuid).upper(), freq or 0)
            n += 1
        log.info("  Dictionary: %d mappings unioned (%d unique terms total)",
                 n, len(self.mnt_by_raw))
        log.info("  Pulling authority_place...")
        cur.execute("SELECT uuid, parent_uuid, canonical_name, level, "
                    "jurisdiction, full_chain_name FROM authority_place")
        added = 0
        for uuid, parent_uuid, name, level, jurisdiction, chain in cur:
            uid = str(uuid).upper()
            if uid in self.pa_by_uuid:
                continue          # PA TSV is primary (has pop/lat/lon)
            rec = {
                'Auth_Place_Name': name or '',
                'UUID': uid,
                'Parent_UUID': str(parent_uuid).upper() if parent_uuid else '',
                'Jurisdiction': jurisdiction or '',
                'Level': str(level) if level is not None else '',
                'Population': '', 'Type_Ahead_Value': chain or '',
                'Historical': '', 'Latitude': '', 'Longitude': '',
            }
            self.pa_by_uuid[uid] = rec
            if name:
                nkey = name.lower()
                self.pa_by_name[nkey].append(rec)
                dh = nkey.replace('-', ' ')
                if dh != nkey:
                    self.pa_by_name[dh].append(rec)
            added += 1
        log.info("  Authority: %d Supabase-only records added", added)
        log.info("  Pulling place_term_illegible...")
        cur.execute("SELECT term FROM place_term_illegible")
        self.illegible = {t[0].strip().lower() for t in cur if t[0]}
        log.info("  Illegible stop-list: %d terms", len(self.illegible))
        cur.close()
        conn.close()

    def _load_dict_tsv(self, dirpath):
        path = os.path.join(dirpath, 'place_term_dictionary.tsv')
        n = 0
        with open(path, encoding='utf-8-sig') as f:
            reader = csv.DictReader(f, delimiter='\t')
            for row in reader:
                term = (row.get('term') or '').strip()
                uid = (row.get('authority_uuid') or '').strip().upper()
                if term and uid and _is_valid_local_uuid(uid):
                    self._ingest_dict_row(term, uid, int(row.get('frequency') or 0))
                    n += 1
        log.info("  Dictionary TSV: %d mappings unioned", n)
        ill = os.path.join(dirpath, 'place_term_illegible.tsv')
        if os.path.exists(ill):
            with open(ill, encoding='utf-8-sig') as f:
                reader = csv.DictReader(f, delimiter='\t')
                self.illegible = {(r.get('term') or '').strip().lower()
                                  for r in reader if (r.get('term') or '').strip()}
            log.info("  Illegible stop-list: %d terms", len(self.illegible))
```

Module-level helper near the top of the file:

```python
def _load_env_file(path):
    """Parse KEY=VALUE lines from an env file into os.environ."""
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, _, val = line.partition('=')
                os.environ[key.strip()] = val.strip().strip('"').strip("'")
```

CLI in `build_cli()` (after `--env`):

```python
    parser.add_argument('--dict', nargs='?', const='live',
                        help="Supplement MNT with the Supabase place dictionary "
                             "(union). Pass a directory of TSV exports, or no "
                             "value for a live connection (needs SUPABASE_PASSWORD "
                             "via --env or the environment). Local mode only.")
```

Update `--env` help text: "(required with --api) or Supabase password (used with --dict live)".

In `main()` change the load call:

```python
        _LOCAL.load(args.mnt, args.pa, dict_source=args.dict, env_path=args.env)
```

- [ ] **Step 4: Run tests, verify pass**

Run: `python3 -m pytest test_rtl_matcher.py -v`
Expected: PASS.

- [ ] **Step 5: Live smoke run**

```bash
python3 rtl_matcher.py --local --dict --env .env \
  --mnt "/Users/natelemonnier/storied/resources/place-authority-mnt-tsv/Master_Normalization_File_6_16_2026v1.tsv" \
  --pa "/Users/natelemonnier/storied/resources/place-authority-mnt-tsv/PA 6_16_2026v77.tsv" \
  --input /tmp/fs_smoke.tsv --output-dir /tmp/dict_smoke_out
```

Expected log lines: `Dictionary: 364412 mappings unioned`, `Illegible stop-list: 60789 terms`, run completes.

- [ ] **Step 6: Commit**

```bash
git add rtl_matcher.py test_rtl_matcher.py
git commit -m "feat: --dict unions Supabase dictionary into local MNT index"
```

---

### Task 4: Illegible stop-list wiring

**Files:**
- Modify: `rtl_matcher.py` — spelling phase in `main()` (~line 2463), `_run_phase3` entry loop, summary list (~line 2137)
- Test: `test_rtl_matcher.py`

**Interfaces:**
- Consumes: `_LOCAL.illegible` (Task 3).
- Produces: illegible terms excluded from spelling correction; entries whose every term is illegible get match_type `illegible`.

- [ ] **Step 1: Wire spelling exclusion**

In `main()`, the spelling call currently passes `all_terms`. Change to:

```python
    spell_terms = [t for t in all_terms if t.lower() not in _LOCAL.illegible]
    if len(spell_terms) < len(all_terms):
        log.info("  %d illegible terms excluded from spelling correction",
                 len(all_terms) - len(spell_terms))
    spelling_added, spelling_corrections = fn_spelling(
        spell_terms, name_cache, sym_spell, transform_map=transform_map)
```

(`_LOCAL.illegible` is an empty set unless `--dict` loaded it, so non-dict runs are unchanged.)

- [ ] **Step 2: Wire illegible status**

In `_run_phase3`, after the fast-path/`match_entry` block and before the `parent_only` retry block:

```python
        if (match.match_type == 'no_auth_match' and _LOCAL.illegible
                and all(t.lower() in _LOCAL.illegible for t in terms)):
            match = MatchResult(depth=0, match_type='illegible')
```

Add `'illegible'` to the summary printer list at ~line 2137.

- [ ] **Step 3: Test**

```python
class TestIllegible:
    def test_all_terms_illegible_gets_status(self):
        saved = rtl_matcher._LOCAL.illegible
        rtl_matcher._LOCAL.illegible = {'uk known', 'a?'}
        try:
            assert all(t.lower() in rtl_matcher._LOCAL.illegible
                       for t in ['uk known', 'a?'])
        finally:
            rtl_matcher._LOCAL.illegible = saved
```

Plus a smoke run: add a row `uk known\tg2\t1` to `/tmp/fs_smoke.tsv`, rerun the Task 3 live smoke command, expect that row's `match_type=illegible` in the output TSV.

Run: `python3 -m pytest test_rtl_matcher.py -v`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add rtl_matcher.py test_rtl_matcher.py
git commit -m "feat: illegible stop-list blocks spelling correction, gets own status"
```

---

### Task 5: Frequency disambiguation

**Files:**
- Modify: `rtl_matcher.py` — new function above `_disambiguate_by_population` (~line 1699), `resolve_parent_only` (~line 1723), single-term branch in `match_entry` (~line 1898), call site in `_run_phase3` (~line 2539), summary list (~line 2137)
- Test: `test_rtl_matcher.py`

**Interfaces:**
- Consumes: `_LOCAL.dict_freq` (Task 3).
- Produces: `_disambiguate_by_frequency(term, candidates, dict_freq) -> str | None`; `resolve_parent_only(candidate_ids, auth_cache, client, term=None)` may return `(uuid, 'freq_resolved')`; new match_type `freq_resolved`.

- [ ] **Step 1: Write failing tests**

```python
from rtl_matcher import _disambiguate_by_frequency


class TestFrequencyDisambiguation:
    def test_skewed_frequency_picks_winner(self):
        freq = {('springfield', U1): 100, ('springfield', U2): 4}
        assert _disambiguate_by_frequency('Springfield', [U1, U2], freq) == U1

    def test_below_ratio_returns_none(self):
        freq = {('springfield', U1): 40, ('springfield', U2): 20}
        assert _disambiguate_by_frequency('springfield', [U1, U2], freq) is None

    def test_below_floor_returns_none(self):
        freq = {('x', U1): 9, ('x', U2): 1}
        assert _disambiguate_by_frequency('x', [U1, U2], freq) is None

    def test_mixed_origin_missing_freq_returns_none(self):
        freq = {('y', U1): 500}          # U2 is MNT-only, no freq entry
        assert _disambiguate_by_frequency('y', [U1, U2], freq) is None

    def test_empty_freq_returns_none(self):
        assert _disambiguate_by_frequency('z', [U1, U2], {}) is None
```

- [ ] **Step 2: Run tests, verify failure**

Run: `python3 -m pytest test_rtl_matcher.py -k Frequency -v`
Expected: FAIL (ImportError).

- [ ] **Step 3: Implement**

Above `_disambiguate_by_population`:

```python
FREQ_MIN = 10      # winner needs at least this many observations
FREQ_RATIO = 5     # and at least this multiple of the runner-up


def _disambiguate_by_frequency(term, candidates, dict_freq):
    """Pick a winner by dictionary frequency. Fires only when EVERY candidate
    has a frequency entry for this term (mixed dict/MNT-origin sets fall
    through to population rules — absence of freq is not evidence against an
    MNT mapping). Winner needs FREQ_MIN observations and FREQ_RATIO times the
    runner-up."""
    if not term or not dict_freq or len(candidates) < 2:
        return None
    key = term.lower()
    freqs = []
    for uid in candidates:
        f = dict_freq.get((key, uid))
        if f is None:
            return None
        freqs.append((f, uid))
    freqs.sort(key=lambda x: (-x[0], x[1]))
    top_f, top_uid = freqs[0]
    if top_f >= FREQ_MIN and top_f >= FREQ_RATIO * freqs[1][0]:
        return top_uid
    return None
```

`resolve_parent_only` gains `term=None` parameter; before the final `_disambiguate_by_population` call:

```python
    winner = _disambiguate_by_frequency(term, candidate_ids,
                                        _LOCAL.dict_freq or {})
    if winner:
        return (winner, 'freq_resolved')
```

Call site in `_run_phase3` (~line 2539) — pass the parent term (rightmost term produced the parent candidates) and accept the new resolution:

```python
            winner, resolution = resolve_parent_only(
                match.candidate_ids, auth_cache, client, term=terms[-1])
            if resolution in ('parent_resolved', 'freq_resolved'):
```

and in the winning `MatchResult` use `match_type=resolution` instead of the literal `'parent_resolved'`.

Single-term branch in `match_entry` (~line 1903), before returning `single_amb`:

```python
        winner = _disambiguate_by_frequency(right_to_left[0], all_ids,
                                            _LOCAL.dict_freq or {})
        if winner:
            return MatchResult([winner], depth=1, match_type='freq_resolved')
        return MatchResult([], depth=1, match_type='single_amb', tied_ids=all_ids)
```

Add `'freq_resolved'` to the summary printer list.

- [ ] **Step 4: Run tests, verify pass**

Run: `python3 -m pytest test_rtl_matcher.py -v`
Expected: PASS (existing `resolve_parent_only` tests unaffected — `term` defaults to None and `_LOCAL.dict_freq` is None/empty in those fixtures).

- [ ] **Step 5: Commit**

```bash
git add rtl_matcher.py test_rtl_matcher.py
git commit -m "feat: frequency disambiguation before population rules"
```

---

### Task 6: Spelling index from memory in dict mode

**Files:**
- Modify: `rtl_matcher.py` — new function after `build_spelling_index` (~line 1264), spelling phase in `main()` (~line 2461)

**Interfaces:**
- Consumes: `_LOCAL.pa_by_name`, `_LOCAL.mnt_by_raw`.
- Produces: `build_spelling_index_from_memory() -> SymSpell`.

- [ ] **Step 1: Implement (port from wip/dict-mode unchanged)**

```python
def build_spelling_index_from_memory():
    """Build the SymSpell index from _LOCAL data already in memory: PA
    canonical names plus every dictionary/MNT term. Used in dict mode so
    the correction vocabulary covers the union."""
    sym = SymSpell(max_dictionary_edit_distance=1, prefix_length=7)
    seen = set()
    for key in list(_LOCAL.pa_by_name) + list(_LOCAL.mnt_by_raw):
        folded = ascii_fold(key)
        if folded and folded not in seen:
            sym.create_dictionary_entry(folded, 1)
            seen.add(folded)
    return sym
```

In `main()` replace `sym_spell = build_spelling_index(args.pa)`:

```python
    if args.dict:
        sym_spell = build_spelling_index_from_memory()
    else:
        sym_spell = build_spelling_index(args.pa)
```

- [ ] **Step 2: Verify**

Run: `python3 -m pytest test_rtl_matcher.py -v` — PASS.
Rerun the Task 3 live smoke command; expected log `Index built: <n> entries` with n > 400000 (union vocabulary), run completes.

- [ ] **Step 3: Commit**

```bash
git add rtl_matcher.py
git commit -m "feat: dict mode builds spelling index from in-memory union"
```

---

### Task 7: A/B evaluation gate (merge blocker)

**Files:**
- Create: `results/dict-union-ab/` (eval outputs)
- Reference: `evaluate_normalizer.py`, ground truth at `/Users/natelemonnier/storied/resources/ground-truth-locations/Ground truth 6_17 - 7_9.tsv`

**Interfaces:**
- Consumes: the finished branch.
- Produces: go/no-go numbers for merging to main.

Paths used below:
- MNT = `/Users/natelemonnier/storied/resources/place-authority-mnt-tsv/Master_Normalization_File_6_16_2026v1.tsv`
- PA = `/Users/natelemonnier/storied/resources/place-authority-mnt-tsv/PA 6_16_2026v77.tsv`
- GT source = `/Users/natelemonnier/storied/resources/ground-truth-locations/Ground truth 6_17 - 7_9.tsv` (columns: place, MatchAuthorityID, ... — no guid, ID column name not recognized by the evaluator, hence the prep step)

- [ ] **Step 1: Prep eval input and normalized ground truth**

The evaluator joins output to ground truth by guid (`evaluate_normalizer.py:423`) and only accepts ID columns named `correct_authority_id`/`correct_place_id`/`authority_id`/`place_id`. Generate a matcher input (synthetic guids) and a conforming GT file:

```bash
mkdir -p results/dict-union-ab
python3 - <<'EOF'
import csv
src = '/Users/natelemonnier/storied/resources/ground-truth-locations/Ground truth 6_17 - 7_9.tsv'
with open(src, encoding='utf-8-sig') as f:
    rows = list(csv.DictReader(f, delimiter='\t'))
with open('results/dict-union-ab/eval_input.tsv', 'w', encoding='utf-8') as f:
    f.write('place\tguid\tfrequency\n')
    for i, r in enumerate(rows):
        f.write(f"{r['place']}\tgt{i}\t1\n")
with open('results/dict-union-ab/ground_truth.tsv', 'w', encoding='utf-8') as f:
    f.write('guid\tcorrect_authority_id\n')
    for i, r in enumerate(rows):
        f.write(f"gt{i}\t{r['MatchAuthorityID']}\n")
print(f"{len(rows)} rows prepared")
EOF
```

- [ ] **Step 2: Baseline run on main**

```bash
git stash --include-untracked && git checkout main
python3 rtl_matcher.py --local \
  --mnt "/Users/natelemonnier/storied/resources/place-authority-mnt-tsv/Master_Normalization_File_6_16_2026v1.tsv" \
  --pa "/Users/natelemonnier/storied/resources/place-authority-mnt-tsv/PA 6_16_2026v77.tsv" \
  --input results/dict-union-ab/eval_input.tsv \
  --output-dir results/dict-union-ab/baseline
git checkout 20260716-dict-union && git stash pop
```

- [ ] **Step 3: Branch run, dict enabled**

```bash
python3 rtl_matcher.py --local --dict --env .env \
  --mnt "/Users/natelemonnier/storied/resources/place-authority-mnt-tsv/Master_Normalization_File_6_16_2026v1.tsv" \
  --pa "/Users/natelemonnier/storied/resources/place-authority-mnt-tsv/PA 6_16_2026v77.tsv" \
  --input results/dict-union-ab/eval_input.tsv \
  --output-dir results/dict-union-ab/dict-union
```

- [ ] **Step 4: Evaluate both against ground truth**

Output lands in a dated subfolder (`<output-dir>/MM-DD/eval_input_NN.tsv`) — substitute the actual file each run printed:

```bash
python3 evaluate_normalizer.py results/dict-union-ab/baseline/07-16/eval_input_01.tsv \
  --input results/dict-union-ab/eval_input.tsv \
  --ground-truth results/dict-union-ab/ground_truth.tsv \
  --pa "/Users/natelemonnier/storied/resources/place-authority-mnt-tsv/PA 6_16_2026v77.tsv" \
  --name baseline
python3 evaluate_normalizer.py results/dict-union-ab/dict-union/07-16/eval_input_01.tsv \
  --input results/dict-union-ab/eval_input.tsv \
  --ground-truth results/dict-union-ab/ground_truth.tsv \
  --pa "/Users/natelemonnier/storied/resources/place-authority-mnt-tsv/PA 6_16_2026v77.tsv" \
  --name dict-union
```

- [ ] **Step 5: Gate check**

Merge requires ALL of:
- Ground-truth accuracy: dict-union >= baseline.
- Match rate: dict-union > baseline.
- Ambiguous count (`*_amb` types): dict-union <= baseline.
- Hand spot-check 20 rows that changed from unmatched/amb to matched (`freq_resolved`, `mnt_full_string`, dict-sourced matches): at least 18/20 correct.

If any gate fails, diagnose before merging — the likeliest suspects are the FREQ_MIN/FREQ_RATIO thresholds (tighten to 20/10x) and full-string staleness.

- [ ] **Step 6: Merge and clean up**

```bash
git checkout main && git merge --no-ff 20260716-dict-union
git branch -D 20260716-dict-union
git branch -D wip/dict-mode          # only after merge confirmed good
```

Also update `CLAUDE.md` in anaconda-2 (if it documents CLI flags) with `--dict`.
