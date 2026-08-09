# Record Accuracy Metric Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Measure what share of records `rtl_matcher` resolves to the correct Place Authority record, using a blind eval set drawn from a corpus the matcher has never seen.

**Architecture:** A seeded stratified sample of snowball4 is labeled by two independent model families that never see matcher output, resolved against a PA index built leaf-first (the inverse of RTL traversal), and scored into three buckets reweighted by band record share. Labeling freezes to a TSV once; the scorer re-runs on every matcher change.

**Tech Stack:** Python 3.12, stdlib `csv`/`json`/`unicodedata`/`random` only, `google-genai` for the Gemini labeler, `pytest` for tests.

## Global Constraints

- No imports from `code/place-normalizer`. Its LLM stack resolves through the same MNT `rtl_matcher` consumes and weights answers 70% MNT lookup, so its output is not independent.
- No pandas. `rtl_matcher.py` uses stdlib `csv`; match it.
- The MNT is never read by any file in this plan. PA only.
- PA file: `/Users/natelemonnier/storied/resources/place-authority-mnt-tsv/PA6_16_2026v77.tsv`. Columns, 1-indexed: 1 `Level`, 2 `LevelName`, 3 `Replacement_UUID`, 4 `Term`, 5 `ID`, 6 `Historical`, 7 `FullChainName`, 8 `ParentID`, 9 `Population`, 10 `Latitude`, 11 `Longitude`.
- The authority UUID is column 5 `ID`. This is what `rtl_matcher` emits as `authority_id` (see `rtl_matcher.py:336`, `_PA_FIELD_MAP`). Never use `Replacement_UUID` as the label.
- Corpus file: `/Users/natelemonnier/storied/resources/np_records_snowball4_locations.tsv`. Columns: `place`, `inferred_location`, `guid`, `frequency`.
- Exclusion file: `/Users/natelemonnier/storied/code/place-normalizer/utils/snowball2_ground_truth.tsv`, read for its `place` column only.
- Bands: head `frequency >= 1000`, mid `10 <= frequency <= 999`, tail `frequency <= 9`.
- Sample sizes: 800 head, 600 mid, 600 tail. Seed 42.
- Abstain is defined as an empty `authority_id` in matcher output. Verified against `rtl-outputs/08-08/snowball2_sample_5k_01.tsv`: empty `authority_id` partitions exactly against `no_auth_match`, `parent_amb`, `parent_rejected`, `chain_amb`, `single_amb`, `low_evidence`, and non-empty against the rest. Do not hardcode a `match_type` list.
- All emitted TSVs carry a `# seed=42 source=<path> generated_from=<script>` comment line before the header.
- Files live in `code/anaconda-2/eval/`. Tests live beside them as `eval/test_*.py`, no `__init__.py`, matching the flat sibling-import convention of `test_rtl_matcher.py`.
- Run tests with `python -m pytest eval/ -v` from `code/anaconda-2/`.

---

### Task 1: PA index and leaf-first chain resolution

**Files:**
- Create: `code/anaconda-2/eval/pa_index.py`
- Test: `code/anaconda-2/eval/test_pa_index.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `normalize_term(s: str) -> str`
  - `normalize_chain(s: str) -> str`
  - `PARow` namedtuple with fields `level level_name uuid term full_chain parent_id population replacement_uuid`
  - `Resolution` namedtuple with fields `uuid status candidates`, where `status` is one of `'unique'`, `'chain_matched'`, `'needs_disambiguation'`, `'absent'`, `'replaced'`
  - `PAIndex(rows: list[PARow])` with attribute `by_term: dict[str, list[PARow]]`
  - `PAIndex.from_tsv(path: str) -> PAIndex`
  - `PAIndex.resolve(leaf: str, proposed_chain: str) -> Resolution`

- [ ] **Step 1: Write the failing tests**

Create `code/anaconda-2/eval/test_pa_index.py`:

```python
"""Tests for the PA index used by the eval labeler."""
import pytest

from pa_index import (
    normalize_term,
    normalize_chain,
    PAIndex,
    PARow,
)


def row(term, uuid, chain, level='4', replacement=''):
    return PARow(level=level, level_name='City', uuid=uuid, term=term,
                 full_chain=chain, parent_id='', population='0',
                 replacement_uuid=replacement)


@pytest.fixture
def index():
    return PAIndex([
        row('Syracuse', 'U-NY', 'Syracuse, Onondaga, New York, USA'),
        row('Syracuse', 'U-UT', 'Syracuse, Davis, Utah, USA'),
        row('Syracuse', 'U-KS', 'Syracuse, Hamilton, Kansas, USA'),
        row('Onondaga', 'U-ONO', 'Onondaga, New York, USA', level='5'),
        row('Lowell', 'U-LOW', 'Lowell, Middlesex, Massachusetts, USA'),
        row('Montréal', 'U-MTL', 'Montréal, Québec, Canada'),
        row('Oldtown', 'U-OLD-NEW', 'Oldtown, Kent, Maryland, USA',
            replacement='U-REPLACEMENT'),
    ])


def test_normalize_term_lowercases_and_strips_accents():
    assert normalize_term('  Montréal ') == 'montreal'


def test_normalize_term_drops_punctuation():
    assert normalize_term("St. Mary's") == 'st marys'


def test_normalize_chain_keeps_comma_structure():
    assert normalize_chain('Syracuse, Onondaga, New York, USA') == \
        'syracuse, onondaga, new york, usa'


def test_unique_term_resolves_without_a_chain(index):
    result = index.resolve('Lowell', '')
    assert result.status == 'unique'
    assert result.uuid == 'U-LOW'


def test_accented_term_resolves_from_ascii_input(index):
    result = index.resolve('Montreal', 'Montreal, Quebec, Canada')
    assert result.uuid == 'U-MTL'


def test_ambiguous_term_resolves_via_proposed_chain(index):
    result = index.resolve('Syracuse', 'Syracuse, Onondaga, New York, USA')
    assert result.status == 'chain_matched'
    assert result.uuid == 'U-NY'


def test_ambiguous_term_resolves_on_partial_chain_overlap(index):
    result = index.resolve('Syracuse', 'Syracuse, New York, USA')
    assert result.status == 'chain_matched'
    assert result.uuid == 'U-NY'


def test_ambiguous_term_without_disambiguating_chain_needs_a_second_call(index):
    result = index.resolve('Syracuse', 'Syracuse, USA')
    assert result.status == 'needs_disambiguation'
    assert result.uuid is None
    assert len(result.candidates) == 3


def test_absent_term_reports_absent(index):
    result = index.resolve('Beverly Hilton Hotel', 'Beverly Hilton Hotel, USA')
    assert result.status == 'absent'
    assert result.uuid is None
    assert result.candidates == []


def test_replaced_uuid_is_flagged_not_silently_followed(index):
    result = index.resolve('Oldtown', 'Oldtown, Kent, Maryland, USA')
    assert result.status == 'replaced'
    assert result.uuid is None


def test_from_tsv_reads_the_real_column_order(tmp_path):
    path = tmp_path / 'pa.tsv'
    path.write_text(
        'Level\tLevelName\tReplacement_UUID\tTerm\tID\tHistorical\t'
        'FullChainName\tParentID\tPopulation\tLatitude\tLongitude\n'
        '4\tCity\t\tLowell\tU-LOW\t\tLowell, Middlesex, Massachusetts, USA\t'
        'U-MID\t106519\t42.6\t-71.3\n',
        encoding='utf-8')
    index = PAIndex.from_tsv(str(path))
    assert index.resolve('Lowell', '').uuid == 'U-LOW'
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/natelemonnier/storied/code/anaconda-2 && python -m pytest eval/test_pa_index.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'pa_index'`

- [ ] **Step 3: Write the implementation**

Create `code/anaconda-2/eval/pa_index.py`:

```python
"""Leaf-first PA index for the eval labeler.

Deliberately not RTL-shaped. Traversal starts at the leaf a model proposed
and moves up, where rtl_matcher starts at the rightmost term and moves left.
Selection among same-named candidates is done by comparing the model's own
proposed chain against PA's FullChainName, never by chain-connection scoring,
evidence rank, or population. The MNT is never read.
"""
import csv
import sys
import unicodedata
from collections import namedtuple

csv.field_size_limit(sys.maxsize)

PARow = namedtuple(
    'PARow',
    'level level_name uuid term full_chain parent_id population replacement_uuid')

Resolution = namedtuple('Resolution', 'uuid status candidates')


def normalize_term(s):
    """Casefold, strip accents, drop punctuation, collapse whitespace."""
    if not s:
        return ''
    decomposed = unicodedata.normalize('NFKD', s)
    stripped = ''.join(c for c in decomposed if not unicodedata.combining(c))
    kept = ''.join(c if c.isalnum() else ' ' for c in stripped.lower())
    return ' '.join(kept.split())


def normalize_chain(s):
    """Normalize each comma-separated part, rejoin with ', '."""
    if not s:
        return ''
    parts = [normalize_term(p) for p in s.split(',')]
    return ', '.join(p for p in parts if p)


def _chain_tokens(chain):
    """Set of normalized parts of a chain, leaf included."""
    return {p for p in normalize_chain(chain).split(', ') if p}


class PAIndex:
    """Exact index on normalized Term. The only surface shared with the matcher."""

    def __init__(self, rows):
        self.rows = list(rows)
        self.by_term = {}
        for r in self.rows:
            self.by_term.setdefault(normalize_term(r.term), []).append(r)

    @classmethod
    def from_tsv(cls, path):
        rows = []
        with open(path, encoding='utf-8', newline='') as f:
            for rec in csv.DictReader(f, delimiter='\t'):
                rows.append(PARow(
                    level=(rec.get('Level') or '').strip(),
                    level_name=(rec.get('LevelName') or '').strip(),
                    uuid=(rec.get('ID') or '').strip(),
                    term=(rec.get('Term') or '').strip(),
                    full_chain=(rec.get('FullChainName') or '').strip(),
                    parent_id=(rec.get('ParentID') or '').strip(),
                    population=(rec.get('Population') or '').strip(),
                    replacement_uuid=(rec.get('Replacement_UUID') or '').strip(),
                ))
        return cls(rows)

    def lookup_term(self, term):
        return self.by_term.get(normalize_term(term), [])

    def resolve(self, leaf, proposed_chain):
        """Resolve a model-proposed leaf to a PA UUID.

        Returns Resolution. A status of 'needs_disambiguation' means the
        caller must present `candidates` back to the model for a pick;
        'replaced' and 'absent' mean the caller must not guess.
        """
        candidates = self.lookup_term(leaf)
        if not candidates:
            return Resolution(None, 'absent', [])

        replaced = [c for c in candidates
                    if c.replacement_uuid and c.replacement_uuid != c.uuid]
        if replaced and len(candidates) == len(replaced):
            return Resolution(None, 'replaced', candidates)

        if len(candidates) == 1:
            return Resolution(candidates[0].uuid, 'unique', candidates)

        proposed = _chain_tokens(proposed_chain) - {normalize_term(leaf)}
        if not proposed:
            return Resolution(None, 'needs_disambiguation', candidates)

        scored = []
        for c in candidates:
            overlap = len(proposed & (_chain_tokens(c.full_chain)
                                      - {normalize_term(c.term)}))
            scored.append((overlap, c))
        best = max(s for s, _ in scored)
        winners = [c for s, c in scored if s == best]
        if best > 0 and len(winners) == 1:
            return Resolution(winners[0].uuid, 'chain_matched', candidates)
        return Resolution(None, 'needs_disambiguation', candidates)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/natelemonnier/storied/code/anaconda-2 && python -m pytest eval/test_pa_index.py -v`
Expected: PASS, 11 passed

- [ ] **Step 5: Assert FullChainName agrees with the ParentID walk**

The design assumes `FullChainName` is the flattened parent chain, so it is read rather than walked. Verify once against the real file:

```bash
cd /Users/natelemonnier/storied/code/anaconda-2
python - <<'PY'
from eval.pa_index import PAIndex, normalize_term
import csv, sys, random
csv.field_size_limit(sys.maxsize)
PA = '/Users/natelemonnier/storied/resources/place-authority-mnt-tsv/PA6_16_2026v77.tsv'
idx = PAIndex.from_tsv(PA)
by_uuid = {r.uuid: r for r in idx.rows}
random.seed(42)
sample = random.sample([r for r in idx.rows if r.parent_id], 500)
bad = 0
for r in sample:
    walk, cur, seen = [r.term], r.parent_id, set()
    while cur and cur in by_uuid and cur not in seen:
        seen.add(cur)
        walk.append(by_uuid[cur].term)
        cur = by_uuid[cur].parent_id
    if [normalize_term(t) for t in walk] != \
       [normalize_term(p) for p in r.full_chain.split(',')]:
        bad += 1
print(f'{bad}/500 disagree')
PY
```

Expected: a low count. If it exceeds 25, stop and report — the design's risk section calls for replacing the column read with the walk, which changes `PAIndex.resolve` and needs a decision before proceeding.

- [ ] **Step 6: Commit**

```bash
cd /Users/natelemonnier/storied/code/anaconda-2
git add eval/pa_index.py eval/test_pa_index.py
git commit -m "feat: leaf-first PA index for eval labeling"
```

---

### Task 2: Stratified eval sample builder

**Files:**
- Create: `code/anaconda-2/eval/build_eval_sample.py`
- Test: `code/anaconda-2/eval/test_build_eval_sample.py`

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces:
  - `SEED = 42`, `SAMPLE_SIZES = {'head': 800, 'mid': 600, 'tail': 600}`
  - `band_for(frequency: int) -> str` returning `'head' | 'mid' | 'tail'`
  - `load_exclusions(path: str) -> set[str]`
  - `stratified_sample(rows: list[dict], sizes: dict, seed: int) -> dict[str, list[dict]]`
  - `split_dev_heldout(sampled: dict, seed: int) -> tuple[list[dict], list[dict]]`
  - Output files `eval_dev.tsv`, `eval_heldout.tsv` with columns `place`, `guid`, `frequency`, `band`; and `bands.json` mapping band name to `{"strings": int, "records": int}`

- [ ] **Step 1: Write the failing tests**

Create `code/anaconda-2/eval/test_build_eval_sample.py`:

```python
"""Tests for the stratified eval sample builder."""
import json

from build_eval_sample import (
    band_for,
    band_record_totals,
    load_exclusions,
    split_dev_heldout,
    stratified_sample,
)


def corpus(n_head=50, n_mid=50, n_tail=50):
    rows = []
    for i in range(n_head):
        rows.append({'place': f'Head{i}, State, USA', 'guid': f'H{i}',
                     'frequency': '5000'})
    for i in range(n_mid):
        rows.append({'place': f'Mid{i}, State, USA', 'guid': f'M{i}',
                     'frequency': '100'})
    for i in range(n_tail):
        rows.append({'place': f'Tail{i}, State, USA', 'guid': f'T{i}',
                     'frequency': '3'})
    return rows


def test_band_boundaries():
    assert band_for(1000) == 'head'
    assert band_for(999) == 'mid'
    assert band_for(10) == 'mid'
    assert band_for(9) == 'tail'
    assert band_for(1) == 'tail'


def test_stratified_sample_honours_sizes_per_band():
    got = stratified_sample(corpus(), {'head': 10, 'mid': 5, 'tail': 5}, 42)
    assert len(got['head']) == 10
    assert len(got['mid']) == 5
    assert len(got['tail']) == 5


def test_stratified_sample_is_deterministic_under_a_seed():
    a = stratified_sample(corpus(), {'head': 10, 'mid': 5, 'tail': 5}, 42)
    b = stratified_sample(corpus(), {'head': 10, 'mid': 5, 'tail': 5}, 42)
    assert [r['guid'] for r in a['head']] == [r['guid'] for r in b['head']]


def test_stratified_sample_takes_everything_when_band_is_short():
    got = stratified_sample(corpus(n_head=3), {'head': 10, 'mid': 5, 'tail': 5}, 42)
    assert len(got['head']) == 3


def test_split_is_disjoint_and_even_per_band():
    sampled = stratified_sample(corpus(), {'head': 10, 'mid': 6, 'tail': 6}, 42)
    dev, heldout = split_dev_heldout(sampled, 42)
    dev_ids = {r['guid'] for r in dev}
    held_ids = {r['guid'] for r in heldout}
    assert dev_ids.isdisjoint(held_ids)
    assert len([r for r in dev if r['band'] == 'head']) == 5
    assert len([r for r in heldout if r['band'] == 'head']) == 5


def test_split_tags_every_row_with_its_band():
    sampled = stratified_sample(corpus(), {'head': 4, 'mid': 4, 'tail': 4}, 42)
    dev, heldout = split_dev_heldout(sampled, 42)
    assert all(r['band'] in ('head', 'mid', 'tail') for r in dev + heldout)


def test_load_exclusions_reads_the_place_column(tmp_path):
    path = tmp_path / 'gt.tsv'
    path.write_text(
        'place\tfrequency\tguid\tground_truth_name\tground_truth_id\n'
        'Mexico City, Mexico, Mexico\t72544\tG1\tCiudad de Mexico\tU1\n',
        encoding='utf-8')
    assert load_exclusions(str(path)) == {'Mexico City, Mexico, Mexico'}


def test_band_record_totals_sums_frequency_not_strings():
    totals = band_record_totals(corpus(n_head=2, n_mid=2, n_tail=2))
    assert totals['head'] == {'strings': 2, 'records': 10000}
    assert totals['tail'] == {'strings': 2, 'records': 6}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/natelemonnier/storied/code/anaconda-2 && python -m pytest eval/test_build_eval_sample.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'build_eval_sample'`

- [ ] **Step 3: Write the implementation**

Create `code/anaconda-2/eval/build_eval_sample.py`:

```python
"""Build the blind eval sample from snowball4.

Stratified by frequency band so one labeling pass yields both record accuracy
(reweighted by band record share) and per-band string accuracy. Split 50/50
into a dev half that is inspected freely and a held-out half that reports
aggregates only.
"""
import argparse
import csv
import json
import random
import sys

csv.field_size_limit(sys.maxsize)

SEED = 42
SAMPLE_SIZES = {'head': 800, 'mid': 600, 'tail': 600}
BAND_ORDER = ('head', 'mid', 'tail')

DEFAULT_CORPUS = ('/Users/natelemonnier/storied/resources/'
                  'np_records_snowball4_locations.tsv')
DEFAULT_EXCLUSIONS = ('/Users/natelemonnier/storied/code/place-normalizer/'
                      'utils/snowball2_ground_truth.tsv')


def band_for(frequency):
    if frequency >= 1000:
        return 'head'
    if frequency >= 10:
        return 'mid'
    return 'tail'


def load_exclusions(path):
    """Place strings already seen during development, read from `place`."""
    with open(path, encoding='utf-8', newline='') as f:
        return {(r.get('place') or '').strip()
                for r in csv.DictReader(f, delimiter='\t')
                if (r.get('place') or '').strip()}


def load_corpus(path, exclusions):
    rows = []
    with open(path, encoding='utf-8', newline='') as f:
        for rec in csv.DictReader(f, delimiter='\t'):
            place = (rec.get('place') or '').strip()
            if not place or place in exclusions:
                continue
            try:
                freq = int(float(rec.get('frequency') or 0))
            except ValueError:
                continue
            rows.append({'place': place, 'guid': (rec.get('guid') or '').strip(),
                         'frequency': str(freq)})
    return rows


def band_record_totals(rows):
    """Per-band string count and record count over the whole corpus."""
    totals = {b: {'strings': 0, 'records': 0} for b in BAND_ORDER}
    for r in rows:
        b = totals[band_for(int(r['frequency']))]
        b['strings'] += 1
        b['records'] += int(r['frequency'])
    return totals


def stratified_sample(rows, sizes, seed):
    buckets = {b: [] for b in BAND_ORDER}
    for r in rows:
        buckets[band_for(int(r['frequency']))].append(r)
    out = {}
    for band in BAND_ORDER:
        pool = sorted(buckets[band], key=lambda r: r['guid'])
        rng = random.Random(f'{seed}:{band}')
        n = min(sizes[band], len(pool))
        out[band] = rng.sample(pool, n)
    return out


def split_dev_heldout(sampled, seed):
    dev, heldout = [], []
    for band in BAND_ORDER:
        rows = [dict(r, band=band) for r in sampled[band]]
        rng = random.Random(f'{seed}:split:{band}')
        rng.shuffle(rows)
        half = len(rows) // 2
        dev.extend(rows[:half])
        heldout.extend(rows[half:half * 2])
    return dev, heldout


def write_tsv(path, rows, source, seed):
    with open(path, 'w', encoding='utf-8', newline='') as f:
        f.write(f'# seed={seed} source={source} '
                f'generated_from=build_eval_sample.py\n')
        w = csv.DictWriter(f, fieldnames=['place', 'guid', 'frequency', 'band'],
                           delimiter='\t', extrasaction='ignore')
        w.writeheader()
        w.writerows(rows)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--corpus', default=DEFAULT_CORPUS)
    p.add_argument('--exclusions', default=DEFAULT_EXCLUSIONS)
    p.add_argument('--out-dir', default='eval/data')
    p.add_argument('--seed', type=int, default=SEED)
    args = p.parse_args(argv)

    import os
    os.makedirs(args.out_dir, exist_ok=True)

    exclusions = load_exclusions(args.exclusions)
    rows = load_corpus(args.corpus, exclusions)
    totals = band_record_totals(rows)
    dev, heldout = split_dev_heldout(
        stratified_sample(rows, SAMPLE_SIZES, args.seed), args.seed)

    write_tsv(f'{args.out_dir}/eval_dev.tsv', dev, args.corpus, args.seed)
    write_tsv(f'{args.out_dir}/eval_heldout.tsv', heldout, args.corpus, args.seed)
    with open(f'{args.out_dir}/bands.json', 'w', encoding='utf-8') as f:
        json.dump({'seed': args.seed, 'corpus': args.corpus, 'bands': totals},
                  f, indent=2)

    print(f'excluded {len(exclusions)} seen strings, kept {len(rows)} corpus rows')
    for band in BAND_ORDER:
        t = totals[band]
        print(f'{band:5s} strings={t["strings"]:>9,} records={t["records"]:>12,}')
    print(f'dev={len(dev)} heldout={len(heldout)}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/natelemonnier/storied/code/anaconda-2 && python -m pytest eval/test_build_eval_sample.py -v`
Expected: PASS, 8 passed

- [ ] **Step 5: Run it against the real corpus**

Run: `cd /Users/natelemonnier/storied/code/anaconda-2 && python eval/build_eval_sample.py`
Expected, matching the spec's measured distribution within the 26 excluded rows:

```
head  strings=   13,144 records= 210,802,610
mid   strings=  558,493 records=  31,926,302
tail  strings=5,507,616 records=  16,358,360
dev=1000 heldout=1000
```

- [ ] **Step 6: Commit**

```bash
cd /Users/natelemonnier/storied/code/anaconda-2
git add eval/build_eval_sample.py eval/test_build_eval_sample.py
git commit -m "feat: stratified blind eval sample from snowball4"
```

---

### Task 3: Label schema and two-labeler merge

**Files:**
- Create: `code/anaconda-2/eval/labels.py`
- Create: `code/anaconda-2/eval/merge_labels.py`
- Test: `code/anaconda-2/eval/test_merge_labels.py`

**Interfaces:**
- Consumes: `pa_index.Resolution` from Task 1.
- Produces:
  - `labels.LABEL_FIELDS: list[str]` — the canonical labeler output column order
  - `labels.NONE = 'NONE'`
  - `labels.read_labels(path: str) -> dict[str, dict]` keyed by `guid`
  - `labels.write_labels(path: str, rows: list[dict], source: str) -> None`
  - `merge_labels.merge(a: dict, b: dict) -> tuple[list[dict], list[dict]]` returning `(agreed, review)`

`LABEL_FIELDS` is exactly:

```
guid, place, band,
leaf_string_only, chain_string_only, label_string_only, status_string_only,
leaf_world, chain_world, label_world, status_world
```

`label_string_only` and `label_world` hold either a PA UUID or the literal `NONE`. `status_*` holds a `Resolution.status` value or `none_after_walk`.

- [ ] **Step 1: Write the failing tests**

Create `code/anaconda-2/eval/test_merge_labels.py`:

```python
"""Tests for the two-labeler merge."""
from labels import LABEL_FIELDS, NONE, read_labels, write_labels
from merge_labels import merge


def label(guid, string_only, world, band='head'):
    return {
        'guid': guid, 'place': f'{guid} place', 'band': band,
        'leaf_string_only': 'Leaf', 'chain_string_only': 'Leaf, State, USA',
        'label_string_only': string_only, 'status_string_only': 'unique',
        'leaf_world': 'Leaf', 'chain_world': 'Leaf, State, USA',
        'label_world': world, 'status_world': 'unique',
    }


def test_matching_rows_are_agreed():
    a = {'G1': label('G1', 'U-1', 'U-1')}
    b = {'G1': label('G1', 'U-1', 'U-1')}
    agreed, review = merge(a, b)
    assert [r['guid'] for r in agreed] == ['G1']
    assert review == []


def test_disagreement_on_string_only_goes_to_review():
    a = {'G1': label('G1', 'U-1', 'U-1')}
    b = {'G1': label('G1', 'U-2', 'U-1')}
    agreed, review = merge(a, b)
    assert agreed == []
    assert review[0]['guid'] == 'G1'
    assert review[0]['disagreement'] == 'label_string_only'


def test_disagreement_on_world_column_alone_still_goes_to_review():
    a = {'G1': label('G1', 'U-1', 'U-1')}
    b = {'G1': label('G1', 'U-1', 'U-9')}
    agreed, review = merge(a, b)
    assert agreed == []
    assert review[0]['disagreement'] == 'label_world'


def test_both_columns_disagreeing_reports_both():
    a = {'G1': label('G1', 'U-1', 'U-1')}
    b = {'G1': label('G1', 'U-2', 'U-9')}
    _, review = merge(a, b)
    assert review[0]['disagreement'] == 'label_string_only,label_world'


def test_agreement_on_none_is_still_agreement():
    a = {'G1': label('G1', NONE, NONE)}
    b = {'G1': label('G1', NONE, NONE)}
    agreed, review = merge(a, b)
    assert len(agreed) == 1


def test_row_missing_from_one_labeler_goes_to_review():
    a = {'G1': label('G1', 'U-1', 'U-1')}
    agreed, review = merge(a, {})
    assert agreed == []
    assert review[0]['disagreement'] == 'missing_from_b'


def test_review_rows_carry_both_labelers_answers():
    a = {'G1': label('G1', 'U-1', 'U-1')}
    b = {'G1': label('G1', 'U-2', 'U-1')}
    _, review = merge(a, b)
    assert review[0]['a_label_string_only'] == 'U-1'
    assert review[0]['b_label_string_only'] == 'U-2'


def test_labels_round_trip_through_tsv(tmp_path):
    path = tmp_path / 'labels.tsv'
    rows = [label('G1', 'U-1', 'U-1')]
    write_labels(str(path), rows, source='test')
    back = read_labels(str(path))
    assert back['G1']['label_string_only'] == 'U-1'
    assert list(back['G1'].keys()) == LABEL_FIELDS
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/natelemonnier/storied/code/anaconda-2 && python -m pytest eval/test_merge_labels.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'labels'`

- [ ] **Step 3: Write `labels.py`**

Create `code/anaconda-2/eval/labels.py`:

```python
"""Shared label schema for the eval labelers.

Two label columns per row. `label_string_only` may climb only to places named
in the input string and drives the headline metric, because it scores the
matcher against information it actually receives. `label_world` may climb
using model world knowledge; the delta between them sizes the LLM enrichment
step that was descoped.
"""
import csv
import sys

csv.field_size_limit(sys.maxsize)

NONE = 'NONE'

LABEL_FIELDS = [
    'guid', 'place', 'band',
    'leaf_string_only', 'chain_string_only',
    'label_string_only', 'status_string_only',
    'leaf_world', 'chain_world',
    'label_world', 'status_world',
]


def read_labels(path):
    """Read a labeler TSV into {guid: row}, skipping the provenance comment."""
    out = {}
    with open(path, encoding='utf-8', newline='') as f:
        lines = [ln for ln in f if not ln.startswith('#')]
    for rec in csv.DictReader(lines, delimiter='\t'):
        out[rec['guid']] = {k: (rec.get(k) or '') for k in LABEL_FIELDS}
    return out


def write_labels(path, rows, source):
    with open(path, 'w', encoding='utf-8', newline='') as f:
        f.write(f'# source={source} generated_from=eval/labels.py\n')
        w = csv.DictWriter(f, fieldnames=LABEL_FIELDS, delimiter='\t',
                           extrasaction='ignore')
        w.writeheader()
        w.writerows(rows)
```

- [ ] **Step 4: Write `merge_labels.py`**

Create `code/anaconda-2/eval/merge_labels.py`:

```python
"""Merge two independent labelers into a frozen label set plus a review queue.

Agreement on both label columns accepts the row. Anything else is routed to a
human. Adjudication resolves a conflict between two labelers and never reads
matcher output, so it does not burn held-out blindness.
"""
import argparse
import csv

from labels import LABEL_FIELDS, read_labels, write_labels

COMPARED = ('label_string_only', 'label_world')

REVIEW_FIELDS = (['guid', 'place', 'band', 'disagreement']
                 + [f'{side}_{col}' for side in ('a', 'b')
                    for col in ('leaf_string_only', 'chain_string_only',
                                'label_string_only', 'status_string_only',
                                'leaf_world', 'chain_world',
                                'label_world', 'status_world')])


def merge(a, b):
    """Return (agreed, review). Keys are guids; rows use LABEL_FIELDS."""
    agreed, review = [], []
    for guid in sorted(set(a) | set(b)):
        ra, rb = a.get(guid), b.get(guid)
        if ra is None or rb is None:
            present = ra or rb
            review.append(_review_row(
                guid, present,
                'missing_from_b' if rb is None else 'missing_from_a',
                ra, rb))
            continue
        differing = [c for c in COMPARED if ra[c] != rb[c]]
        if differing:
            review.append(_review_row(guid, ra, ','.join(differing), ra, rb))
        else:
            agreed.append({k: ra[k] for k in LABEL_FIELDS})
    return agreed, review


def _review_row(guid, present, disagreement, ra, rb):
    row = {'guid': guid, 'place': present.get('place', ''),
           'band': present.get('band', ''), 'disagreement': disagreement}
    for side, src in (('a', ra), ('b', rb)):
        for col in ('leaf_string_only', 'chain_string_only',
                    'label_string_only', 'status_string_only',
                    'leaf_world', 'chain_world', 'label_world', 'status_world'):
            row[f'{side}_{col}'] = (src or {}).get(col, '')
    return row


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--a', required=True, help='first labeler TSV')
    p.add_argument('--b', required=True, help='second labeler TSV')
    p.add_argument('--out', default='eval/data/labels_final.tsv')
    p.add_argument('--review', default='eval/data/label_review.tsv')
    args = p.parse_args(argv)

    agreed, review = merge(read_labels(args.a), read_labels(args.b))
    write_labels(args.out, agreed, source=f'{args.a}+{args.b}')
    with open(args.review, 'w', encoding='utf-8', newline='') as f:
        f.write(f'# a={args.a} b={args.b} generated_from=merge_labels.py\n')
        w = csv.DictWriter(f, fieldnames=REVIEW_FIELDS, delimiter='\t',
                           extrasaction='ignore')
        w.writeheader()
        w.writerows(review)

    total = len(agreed) + len(review)
    pct = 100 * len(review) / total if total else 0
    print(f'agreed={len(agreed)} review={len(review)} ({pct:.1f}% disagreement)')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd /Users/natelemonnier/storied/code/anaconda-2 && python -m pytest eval/test_merge_labels.py -v`
Expected: PASS, 8 passed

- [ ] **Step 6: Commit**

```bash
cd /Users/natelemonnier/storied/code/anaconda-2
git add eval/labels.py eval/merge_labels.py eval/test_merge_labels.py
git commit -m "feat: label schema and two-labeler merge"
```

---

### Task 4: Gemini labeler

**Files:**
- Create: `code/anaconda-2/eval/label_gemini.py`
- Create: `code/anaconda-2/eval/prompt.py`
- Test: `code/anaconda-2/eval/test_prompt.py`

**Interfaces:**
- Consumes: `pa_index.PAIndex`, `pa_index.Resolution`, `labels.LABEL_FIELDS`, `labels.NONE`, `labels.write_labels`.
- Produces:
  - `prompt.redact_for_transport(place: str) -> str`
  - `prompt.build_identify_prompt(places: list[dict]) -> str`
  - `prompt.parse_identify_response(text: str, expected: int) -> list[dict]` with keys `leaf_string_only`, `chain_string_only`, `leaf_world`, `chain_world`
  - `prompt.build_disambiguate_prompt(place: str, candidates: list) -> str`
  - `prompt.parse_disambiguate_response(text: str, n: int) -> int | None`
  - `label_gemini.resolve_with_walk(index, leaf, chain) -> tuple[str, str]` returning `(uuid_or_NONE, status)`

**Data handling:** every prompt-bound place string passes through
`redact_for_transport` first, so no specific residential address reaches a third
party. 29.3% of the tail band is address-shaped (`6314 Stewart ave, Illinois`).
Redaction costs nothing in accuracy — the street is never in PA, so the label is
the containing jurisdiction with or without the house number. The local TSV keeps
the unredacted string; only the outbound prompt is redacted.

- [ ] **Step 1: Write the failing tests**

Create `code/anaconda-2/eval/test_prompt.py`:

```python
"""Tests for prompt construction and response parsing."""
import pytest

from prompt import (
    build_disambiguate_prompt,
    build_identify_prompt,
    parse_disambiguate_response,
    parse_identify_response,
    redact_for_transport,
)


def test_redaction_strips_a_house_number_from_a_street():
    assert redact_for_transport('6314 Stewart ave, Illinois') == \
        'Stewart ave, Illinois'
    assert redact_for_transport('63 Gaping Rock Road, Levittown') == \
        'Gaping Rock Road, Levittown'


def test_redaction_spares_real_numeric_place_names():
    # PA holds 12 of these; none may be damaged.
    assert redact_for_transport('100 Mile House, British Columbia') == \
        '100 Mile House, British Columbia'
    assert redact_for_transport('16th Street Baptist Church, Birmingham') == \
        '16th Street Baptist Church, Birmingham'


def test_redaction_leaves_non_street_leaves_alone():
    assert redact_for_transport('64 Club, Council Bluffs') == \
        '64 Club, Council Bluffs'


def test_redaction_only_touches_the_leaf():
    assert redact_for_transport('Chicago, 100 Mile House') == \
        'Chicago, 100 Mile House'


def test_redaction_is_a_noop_on_ordinary_places():
    assert redact_for_transport('Syracuse, New York, United States of America') \
        == 'Syracuse, New York, United States of America'


def test_identify_prompt_redacts_before_sending():
    text = build_identify_prompt([{'place': '6314 Stewart ave, Illinois'}])
    assert '6314' not in text
    assert 'Stewart ave, Illinois' in text


def test_identify_prompt_lists_every_place_numbered():
    text = build_identify_prompt([
        {'place': 'Syracuse, New York, United States of America'},
        {'place': 'Bethel Lutheran church, Chicago'},
    ])
    assert '1. Syracuse, New York, United States of America' in text
    assert '2. Bethel Lutheran church, Chicago' in text


def test_identify_prompt_states_the_two_column_rule():
    text = build_identify_prompt([{'place': 'x'}])
    assert 'string_only' in text and 'world' in text


def test_parse_identify_returns_one_entry_per_input():
    text = """```json
[{"n": 1, "leaf_string_only": "Syracuse",
  "chain_string_only": "Syracuse, Onondaga, New York, USA",
  "leaf_world": "Syracuse",
  "chain_world": "Syracuse, Onondaga, New York, USA"},
 {"n": 2, "leaf_string_only": "Chicago",
  "chain_string_only": "Chicago, Cook, Illinois, USA",
  "leaf_world": "Bethel Lutheran Church",
  "chain_world": "Chicago, Cook, Illinois, USA"}]
```"""
    got = parse_identify_response(text, expected=2)
    assert len(got) == 2
    assert got[0]['leaf_string_only'] == 'Syracuse'
    assert got[1]['leaf_world'] == 'Bethel Lutheran Church'


def test_parse_identify_pads_short_responses_with_none():
    text = '[{"n": 1, "leaf_string_only": "Syracuse", ' \
           '"chain_string_only": "Syracuse, Onondaga, New York, USA", ' \
           '"leaf_world": "Syracuse", ' \
           '"chain_world": "Syracuse, Onondaga, New York, USA"}]'
    got = parse_identify_response(text, expected=3)
    assert len(got) == 3
    assert got[2]['leaf_string_only'] == ''


def test_parse_identify_raises_on_unparseable_text():
    with pytest.raises(ValueError):
        parse_identify_response('the model apologised instead', expected=1)


def test_disambiguate_prompt_numbers_candidates_from_one():
    text = build_disambiguate_prompt(
        'Syracuse, New York, United States of America',
        ['Syracuse, Onondaga, New York, USA', 'Syracuse, Davis, Utah, USA'])
    assert '1. Syracuse, Onondaga, New York, USA' in text
    assert '2. Syracuse, Davis, Utah, USA' in text


def test_parse_disambiguate_returns_zero_based_index():
    assert parse_disambiguate_response('2', n=3) == 1


def test_parse_disambiguate_returns_none_for_out_of_range():
    assert parse_disambiguate_response('9', n=3) is None


def test_parse_disambiguate_returns_none_for_refusal():
    assert parse_disambiguate_response('NONE', n=3) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/natelemonnier/storied/code/anaconda-2 && python -m pytest eval/test_prompt.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'prompt'`

- [ ] **Step 3: Write `prompt.py`**

Create `code/anaconda-2/eval/prompt.py`:

```python
"""Prompts for the eval labeler.

The model does all the geography. It never sees rtl_matcher output, the MNT,
or a retrieved candidate list at identify time — only the raw string.
"""
import json
import re

SYSTEM = (
    "You label place strings extracted from US newspaper articles, 1800s-1900s, "
    "for a genealogical place authority. You are producing ground truth used to "
    "grade an automated matcher, so accuracy matters more than coverage: when "
    "you do not know, say so rather than guessing."
)

_RULES = """For each numbered place string, return two independent answers.

string_only: the most specific real jurisdiction you can identify using ONLY
places actually named in the input. If the leftmost item is a building, church,
hospital, street, or other feature rather than a jurisdiction, climb to the
jurisdiction named in the string. "Bethel Lutheran church, Chicago" gives
Chicago. "Beverly Hilton Hotel" alone gives empty, because no jurisdiction is
named.

world: the same question, but you may use what you know. "Beverly Hilton Hotel"
gives Beverly Hills. When world and string_only agree, repeat the answer.

For each, give the leaf (the most specific place name, spelling corrected to
its modern canonical form, so "Sanpat" becomes "San Patricio") and the full
jurisdiction chain from that leaf up to the country, comma separated. Counties
are named without the word "County". Use "USA" for the United States.

Return empty strings for both fields when you cannot identify a place.

Respond with a JSON array and nothing else. One object per input, in order:
{"n": 1, "leaf_string_only": "", "chain_string_only": "",
 "leaf_world": "", "chain_world": ""}"""

_KEYS = ('leaf_string_only', 'chain_string_only', 'leaf_world', 'chain_world')

_HOUSE_NUMBER = re.compile(r'^\s*\d+[A-Za-z]?\s+')
_STREET_SUFFIX = re.compile(
    r'\b(st|street|ave|avenue|rd|road|dr|drive|ln|lane|blvd|boulevard|'
    r'ct|court|pl|place|ter|terrace|hwy|highway|cir|circle|pkwy|parkway|way)'
    r'\.?\s*$', re.I)


def redact_for_transport(place):
    """Strip a leading house number from the leaf when the leaf is a street.

    No specific residential address reaches a third party. 29.3% of the tail
    band is address-shaped. This costs nothing in accuracy: the street is never
    in PA, so the label is the containing jurisdiction either way.

    Gated on a street suffix so real numeric place names survive — PA holds 12,
    among them "100 Mile House" and "16th Street Baptist Church". Only the leaf
    is touched; parent levels are never addresses.
    """
    if not place:
        return place
    parts = place.split(',')
    leaf = parts[0]
    if _HOUSE_NUMBER.match(leaf) and _STREET_SUFFIX.search(leaf):
        parts[0] = _HOUSE_NUMBER.sub('', leaf)
    return ','.join(parts)


def build_identify_prompt(places):
    listing = '\n'.join(f'{i}. {redact_for_transport(p["place"])}'
                        for i, p in enumerate(places, 1))
    return f'{_RULES}\n\nPlace strings:\n{listing}'


def _extract_json(text):
    fenced = re.search(r'```(?:json)?\s*(.*?)```', text, re.S)
    body = fenced.group(1) if fenced else text
    start, end = body.find('['), body.rfind(']')
    if start == -1 or end == -1:
        raise ValueError(f'no JSON array in response: {text[:200]!r}')
    return body[start:end + 1]


def parse_identify_response(text, expected):
    """Parse into exactly `expected` entries, padding short responses."""
    parsed = json.loads(_extract_json(text))
    if not isinstance(parsed, list):
        raise ValueError('response was not a JSON array')
    by_n = {}
    for i, item in enumerate(parsed, 1):
        if not isinstance(item, dict):
            continue
        n = item.get('n', i)
        by_n[int(n)] = {k: str(item.get(k) or '').strip() for k in _KEYS}
    return [by_n.get(i, {k: '' for k in _KEYS}) for i in range(1, expected + 1)]


def build_disambiguate_prompt(place, candidates):
    """Caller prepends SYSTEM, so it is deliberately absent here."""
    listing = '\n'.join(f'{i}. {c}' for i, c in enumerate(candidates, 1))
    return (f'Place string: {redact_for_transport(place)}\n\n'
            f'Place Authority holds these records under that name:\n{listing}\n\n'
            'Which one does the place string refer to? Reply with the number '
            'alone, or NONE if it is none of them or you cannot tell.')


def parse_disambiguate_response(text, n):
    """Return a zero-based index, or None for NONE / out of range / unparseable."""
    m = re.search(r'\d+', text or '')
    if not m:
        return None
    picked = int(m.group())
    return picked - 1 if 1 <= picked <= n else None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/natelemonnier/storied/code/anaconda-2 && python -m pytest eval/test_prompt.py -v`
Expected: PASS, 15 passed

- [ ] **Step 5: Write `label_gemini.py`**

Create `code/anaconda-2/eval/label_gemini.py`:

```python
"""Label the eval sample with Gemini.

Two stages. Identify runs in batches of 5 and never sees PA. Disambiguate runs
only for leaves whose Term maps to several PA records, and presents the real
FullChainName values so the model picks an index rather than emitting a UUID —
hallucinated IDs are structurally impossible.

Needs GEMINI_API_KEY in anaconda-2/.env and `pip install google-genai`.
"""
import argparse
import csv
import os
import sys
import time

from labels import NONE, write_labels
from pa_index import PAIndex
from prompt import (SYSTEM, build_disambiguate_prompt, build_identify_prompt,
                    parse_disambiguate_response, parse_identify_response)

DEFAULT_PA = ('/Users/natelemonnier/storied/resources/'
              'place-authority-mnt-tsv/PA6_16_2026v77.tsv')
BATCH = 5

csv.field_size_limit(sys.maxsize)


def load_env(path):
    if not os.path.exists(path):
        return
    with open(path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, _, v = line.partition('=')
                os.environ.setdefault(k.strip(), v.strip())


def read_sample(path):
    with open(path, encoding='utf-8', newline='') as f:
        lines = [ln for ln in f if not ln.startswith('#')]
    return list(csv.DictReader(lines, delimiter='\t'))


def resolve_with_walk(index, leaf, chain, ask=None):
    """Resolve a proposed leaf, climbing the proposed chain when it is absent.

    Returns (uuid_or_NONE, status). `ask` is called only when a leaf maps to
    several PA records and the proposed chain does not separate them.
    """
    if not leaf and not chain:
        return NONE, 'none_after_walk'

    parts = [p.strip() for p in (chain or leaf).split(',') if p.strip()]
    if leaf and (not parts or parts[0].lower() != leaf.lower()):
        parts.insert(0, leaf)

    for i, candidate_leaf in enumerate(parts):
        remaining = ', '.join(parts[i:])
        res = index.resolve(candidate_leaf, remaining)
        if res.status in ('unique', 'chain_matched'):
            return res.uuid, res.status
        if res.status == 'needs_disambiguation' and ask is not None:
            chains = [c.full_chain for c in res.candidates]
            picked = ask(remaining, chains)
            if picked is not None:
                return res.candidates[picked].uuid, 'model_disambiguated'
        if res.status == 'replaced':
            return NONE, 'replaced'
    return NONE, 'none_after_walk'


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--sample', required=True)
    p.add_argument('--out', required=True)
    p.add_argument('--pa', default=DEFAULT_PA)
    p.add_argument('--env', default='.env')
    p.add_argument('--model', default='gemini-2.5-flash',
                   help='current Flash-tier model name from AI Studio')
    p.add_argument('--sleep', type=float, default=0.5)
    args = p.parse_args(argv)

    load_env(args.env)
    key = os.environ.get('GEMINI_API_KEY')
    if not key:
        print('GEMINI_API_KEY not set; add it to anaconda-2/.env', file=sys.stderr)
        return 1

    from google import genai
    client = genai.Client(api_key=key)

    def call(text):
        resp = client.models.generate_content(
            model=args.model, contents=f'{SYSTEM}\n\n{text}')
        return resp.text or ''

    def ask(place, chains):
        try:
            return parse_disambiguate_response(
                call(build_disambiguate_prompt(place, chains)), len(chains))
        except Exception as exc:
            print(f'disambiguate failed: {exc}', file=sys.stderr)
            return None

    index = PAIndex.from_tsv(args.pa)
    sample = read_sample(args.sample)
    out = []

    for start in range(0, len(sample), BATCH):
        chunk = sample[start:start + BATCH]
        try:
            answers = parse_identify_response(
                call(build_identify_prompt(chunk)), expected=len(chunk))
        except Exception as exc:
            print(f'batch at {start} failed: {exc}', file=sys.stderr)
            answers = [{'leaf_string_only': '', 'chain_string_only': '',
                        'leaf_world': '', 'chain_world': ''}] * len(chunk)

        for src, ans in zip(chunk, answers):
            so_id, so_status = resolve_with_walk(
                index, ans['leaf_string_only'], ans['chain_string_only'], ask)
            w_id, w_status = resolve_with_walk(
                index, ans['leaf_world'], ans['chain_world'], ask)
            out.append({
                'guid': src['guid'], 'place': src['place'], 'band': src['band'],
                'leaf_string_only': ans['leaf_string_only'],
                'chain_string_only': ans['chain_string_only'],
                'label_string_only': so_id, 'status_string_only': so_status,
                'leaf_world': ans['leaf_world'],
                'chain_world': ans['chain_world'],
                'label_world': w_id, 'status_world': w_status,
            })

        print(f'{len(out)}/{len(sample)}', file=sys.stderr)
        time.sleep(args.sleep)

    write_labels(args.out, out, source=f'gemini:{args.model}')
    resolved = sum(1 for r in out if r['label_string_only'] != NONE)
    print(f'labeled {len(out)} rows, {resolved} resolved from string alone')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
```

- [ ] **Step 6: Add a resolve_with_walk test**

Append to `code/anaconda-2/eval/test_pa_index.py`:

```python
from label_gemini import resolve_with_walk
from labels import NONE


def test_walk_climbs_past_an_absent_leaf(index):
    uuid, status = resolve_with_walk(
        index, 'Bethel Lutheran Church',
        'Bethel Lutheran Church, Onondaga, New York, USA')
    assert uuid == 'U-ONO'
    assert status in ('unique', 'chain_matched')


def test_walk_returns_none_when_nothing_in_the_chain_exists(index):
    uuid, status = resolve_with_walk(index, 'Nowhere', 'Nowhere, Nohow')
    assert uuid == NONE
    assert status == 'none_after_walk'


def test_walk_asks_the_model_only_when_the_chain_cannot_separate(index):
    asked = []

    def ask(place, chains):
        asked.append((place, chains))
        return 0

    uuid, status = resolve_with_walk(index, 'Syracuse', 'Syracuse, USA', ask)
    assert asked
    assert status == 'model_disambiguated'
    assert uuid in ('U-NY', 'U-UT', 'U-KS')


def test_walk_does_not_ask_when_the_chain_already_separates(index):
    asked = []
    resolve_with_walk(index, 'Syracuse', 'Syracuse, Onondaga, New York, USA',
                      lambda p, c: asked.append(1) or 0)
    assert not asked
```

- [ ] **Step 7: Run the full eval suite**

Run: `cd /Users/natelemonnier/storied/code/anaconda-2 && python -m pytest eval/ -v`
Expected: PASS, all tests. `label_gemini` imports `google.genai` lazily inside `main`, so the suite runs without the dependency installed.

- [ ] **Step 8: Commit**

```bash
cd /Users/natelemonnier/storied/code/anaconda-2
git add eval/prompt.py eval/label_gemini.py eval/test_prompt.py eval/test_pa_index.py
git commit -m "feat: gemini labeler with leaf-first walk and model disambiguation"
```

---

### Task 5: Claude labeling batches

**Files:**
- Create: `code/anaconda-2/eval/make_label_batches.py`
- Create: `code/anaconda-2/eval/ingest_claude_labels.py`
- Test: `code/anaconda-2/eval/test_ingest_claude_labels.py`

**Interfaces:**
- Consumes: `prompt.build_identify_prompt`, `prompt.parse_identify_response`, `pa_index.PAIndex`, `label_gemini.resolve_with_walk`, `labels.write_labels`.
- Produces:
  - `make_label_batches.py` writing `eval/data/claude_batches/batch_NNN.md`, 100 rows each, each file self-contained with the identify rules and a numbered place list
  - `ingest_claude_labels.py` reading `eval/data/claude_responses/batch_NNN.json` and writing `labels_claude.tsv`
  - `ingest_claude_labels.rows_from_responses(sample, responses, index) -> list[dict]`

The Claude half is agent-driven rather than API-driven, since no Anthropic key is available. Labeling runs once per eval set and freezes to a TSV, so this does not hurt reproducibility of the scorer.

- [ ] **Step 1: Write the failing test**

Create `code/anaconda-2/eval/test_ingest_claude_labels.py`:

```python
"""Tests for ingesting subagent-produced Claude labels."""
from ingest_claude_labels import rows_from_responses
from labels import LABEL_FIELDS, NONE
from pa_index import PAIndex, PARow


def index():
    return PAIndex([
        PARow('4', 'City', 'U-NY', 'Syracuse',
              'Syracuse, Onondaga, New York, USA', '', '0', ''),
        PARow('4', 'City', 'U-CHI', 'Chicago',
              'Chicago, Cook, Illinois, USA', '', '0', ''),
    ])


SAMPLE = [
    {'guid': 'G1', 'place': 'Syracuse, New York, United States of America',
     'band': 'head'},
    {'guid': 'G2', 'place': 'Bethel Lutheran church, Chicago', 'band': 'tail'},
]


def test_rows_carry_the_full_label_schema():
    responses = {'G1': {'leaf_string_only': 'Syracuse',
                        'chain_string_only': 'Syracuse, Onondaga, New York, USA',
                        'leaf_world': 'Syracuse',
                        'chain_world': 'Syracuse, Onondaga, New York, USA'}}
    rows = rows_from_responses(SAMPLE, responses, index())
    assert list(rows[0].keys()) == LABEL_FIELDS


def test_absent_leaf_climbs_to_the_named_jurisdiction():
    responses = {'G2': {'leaf_string_only': 'Bethel Lutheran Church',
                        'chain_string_only':
                            'Bethel Lutheran Church, Chicago, Cook, Illinois, USA',
                        'leaf_world': 'Bethel Lutheran Church',
                        'chain_world':
                            'Bethel Lutheran Church, Chicago, Cook, Illinois, USA'}}
    rows = rows_from_responses(SAMPLE, responses, index())
    g2 = next(r for r in rows if r['guid'] == 'G2')
    assert g2['label_string_only'] == 'U-CHI'


def test_missing_response_becomes_none_not_a_dropped_row():
    rows = rows_from_responses(SAMPLE, {}, index())
    assert len(rows) == 2
    assert all(r['label_string_only'] == NONE for r in rows)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/natelemonnier/storied/code/anaconda-2 && python -m pytest eval/test_ingest_claude_labels.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ingest_claude_labels'`

- [ ] **Step 3: Write `make_label_batches.py`**

Create `code/anaconda-2/eval/make_label_batches.py`:

```python
"""Split the eval sample into self-contained prompt files for subagent labeling.

Each batch file carries the same identify rules the Gemini labeler uses, so the
two labelers answer the same question. Batches are 100 rows; a subagent labels
one file and writes JSON to eval/data/claude_responses/batch_NNN.json.
"""
import argparse
import csv
import json
import os
import sys

from prompt import SYSTEM, build_identify_prompt

csv.field_size_limit(sys.maxsize)
BATCH = 100


def read_sample(path):
    with open(path, encoding='utf-8', newline='') as f:
        lines = [ln for ln in f if not ln.startswith('#')]
    return list(csv.DictReader(lines, delimiter='\t'))


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--sample', required=True)
    p.add_argument('--out-dir', default='eval/data/claude_batches')
    args = p.parse_args(argv)

    os.makedirs(args.out_dir, exist_ok=True)
    sample = read_sample(args.sample)
    if not sample:
        print('sample is empty', file=sys.stderr)
        return 1

    for i, start in enumerate(range(0, len(sample), BATCH), 1):
        chunk = sample[start:start + BATCH]
        guids = [r['guid'] for r in chunk]
        path = f'{args.out_dir}/batch_{i:03d}.md'
        with open(path, 'w', encoding='utf-8') as f:
            f.write(f'{SYSTEM}\n\n{build_identify_prompt(chunk)}\n\n')
            f.write('Write your JSON array to '
                    f'`eval/data/claude_responses/batch_{i:03d}.json`. '
                    'Use these guids in order, one object per guid, adding a '
                    '"guid" field to each object:\n')
            f.write(json.dumps(guids, indent=2))
            f.write('\n')
        print(path)

    print(f'{len(sample)} rows in {i} batches', file=sys.stderr)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
```

- [ ] **Step 4: Write `ingest_claude_labels.py`**

Create `code/anaconda-2/eval/ingest_claude_labels.py`:

```python
"""Ingest subagent-produced Claude label JSON into the shared label TSV."""
import argparse
import csv
import glob
import json
import sys

from label_gemini import resolve_with_walk
from labels import NONE, write_labels
from pa_index import PAIndex

DEFAULT_PA = ('/Users/natelemonnier/storied/resources/'
              'place-authority-mnt-tsv/PA6_16_2026v77.tsv')
_KEYS = ('leaf_string_only', 'chain_string_only', 'leaf_world', 'chain_world')

csv.field_size_limit(sys.maxsize)


def read_sample(path):
    with open(path, encoding='utf-8', newline='') as f:
        lines = [ln for ln in f if not ln.startswith('#')]
    return list(csv.DictReader(lines, delimiter='\t'))


def load_responses(pattern):
    """Merge every batch JSON into {guid: {four fields}}."""
    merged = {}
    for path in sorted(glob.glob(pattern)):
        with open(path, encoding='utf-8') as f:
            for item in json.load(f):
                guid = str(item.get('guid') or '').strip()
                if guid:
                    merged[guid] = {k: str(item.get(k) or '').strip()
                                    for k in _KEYS}
    return merged


def rows_from_responses(sample, responses, index):
    """Every sample row gets a label row. Missing responses become NONE."""
    out = []
    for src in sample:
        ans = responses.get(src['guid'], {k: '' for k in _KEYS})
        so_id, so_status = resolve_with_walk(
            index, ans['leaf_string_only'], ans['chain_string_only'])
        w_id, w_status = resolve_with_walk(
            index, ans['leaf_world'], ans['chain_world'])
        out.append({
            'guid': src['guid'], 'place': src['place'], 'band': src['band'],
            'leaf_string_only': ans['leaf_string_only'],
            'chain_string_only': ans['chain_string_only'],
            'label_string_only': so_id, 'status_string_only': so_status,
            'leaf_world': ans['leaf_world'], 'chain_world': ans['chain_world'],
            'label_world': w_id, 'status_world': w_status,
        })
    return out


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--sample', required=True)
    p.add_argument('--responses', default='eval/data/claude_responses/*.json')
    p.add_argument('--out', required=True)
    p.add_argument('--pa', default=DEFAULT_PA)
    args = p.parse_args(argv)

    index = PAIndex.from_tsv(args.pa)
    rows = rows_from_responses(read_sample(args.sample),
                               load_responses(args.responses), index)
    write_labels(args.out, rows, source='claude:subagents')
    resolved = sum(1 for r in rows if r['label_string_only'] != NONE)
    print(f'labeled {len(rows)} rows, {resolved} resolved from string alone')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
```

Note: `resolve_with_walk` is called without an `ask` callback here, so a leaf whose chain cannot separate PA candidates lands as `NONE` with status `none_after_walk`. Those rows disagree with Gemini and route to review, which is the correct outcome for an ambiguity a human should settle.

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd /Users/natelemonnier/storied/code/anaconda-2 && python -m pytest eval/ -v`
Expected: PASS, all tests

- [ ] **Step 6: Commit**

```bash
cd /Users/natelemonnier/storied/code/anaconda-2
git add eval/make_label_batches.py eval/ingest_claude_labels.py eval/test_ingest_claude_labels.py
git commit -m "feat: subagent labeling batches and ingest"
```

---

### Task 6: Scorer

**Files:**
- Create: `code/anaconda-2/eval/score_records.py`
- Test: `code/anaconda-2/eval/test_score_records.py`

**Interfaces:**
- Consumes: `labels.read_labels`, `labels.NONE`, and `bands.json` from Task 2.
- Produces:
  - `bucket(authority_id: str, label: str) -> str` returning `'correct' | 'wrong' | 'abstain'`
  - `score(matcher_rows: list[dict], labels: dict, band_totals: dict) -> dict`
  - `world_delta(labels: dict) -> dict`

- [ ] **Step 1: Write the failing tests**

Create `code/anaconda-2/eval/test_score_records.py`:

```python
"""Tests for the record accuracy scorer."""
from labels import NONE
from score_records import bucket, score, world_delta


def label(guid, string_only, world=None, band='head'):
    return {'guid': guid, 'place': f'{guid} place', 'band': band,
            'label_string_only': string_only,
            'label_world': world if world is not None else string_only}


def matched(guid, authority_id):
    return {'guid': guid, 'authority_id': authority_id, 'match_type': 'x'}


TOTALS = {'head': {'strings': 10, 'records': 8000},
          'mid': {'strings': 10, 'records': 1500},
          'tail': {'strings': 10, 'records': 500}}


def test_empty_authority_id_is_abstain_not_wrong():
    assert bucket('', 'U-1') == 'abstain'


def test_exact_id_match_is_correct():
    assert bucket('U-1', 'U-1') == 'correct'


def test_any_other_id_is_wrong_including_an_ancestor():
    assert bucket('U-PARENT', 'U-1') == 'wrong'


def test_per_band_accuracy_counts_only_scored_rows():
    labels = {'G1': label('G1', 'U-1'), 'G2': label('G2', 'U-2')}
    rows = [matched('G1', 'U-1'), matched('G2', '')]
    got = score(rows, labels, TOTALS)
    assert got['bands']['head']['correct'] == 1
    assert got['bands']['head']['abstain'] == 1
    assert got['bands']['head']['accuracy'] == 0.5


def test_none_labels_are_excluded_from_the_denominator():
    labels = {'G1': label('G1', 'U-1'), 'G2': label('G2', NONE, NONE)}
    rows = [matched('G1', 'U-1'), matched('G2', '')]
    got = score(rows, labels, TOTALS)
    assert got['bands']['head']['scored'] == 1
    assert got['bands']['head']['accuracy'] == 1.0
    assert got['excluded_none'] == 1


def test_record_accuracy_weights_bands_by_record_share():
    labels = {'H': label('H', 'U-1', band='head'),
              'T': label('T', 'U-2', band='tail')}
    rows = [matched('H', 'U-1'), matched('T', 'WRONG')]
    got = score(rows, labels, TOTALS)
    # head and tail are the only live bands: 8000/(8000+500) * 1.0
    assert abs(got['record_accuracy'] - 8000 / 8500) < 0.001


def test_bands_with_no_scored_rows_do_not_break_the_weighting():
    labels = {'H': label('H', 'U-1', band='head')}
    got = score([matched('H', 'U-1')], labels, TOTALS)
    assert abs(got['record_accuracy'] - 1.0) < 0.001


def test_world_delta_counts_rows_world_knowledge_would_have_recovered():
    labels = {'G1': label('G1', NONE, 'U-9'), 'G2': label('G2', 'U-1', 'U-1')}
    got = world_delta(labels)
    assert got['recoverable'] == 1
    assert got['total'] == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/natelemonnier/storied/code/anaconda-2 && python -m pytest eval/test_score_records.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'score_records'`

- [ ] **Step 3: Write the implementation**

Create `code/anaconda-2/eval/score_records.py`:

```python
"""Score rtl_matcher output against the frozen eval labels.

Headline is record accuracy: the sum over bands of band record share times band
string accuracy. Abstain is an empty authority_id, verified as an exact
partition against match_type. Ancestors get no partial credit.
"""
import argparse
import csv
import json
import sys

from labels import NONE, read_labels

csv.field_size_limit(sys.maxsize)
BAND_ORDER = ('head', 'mid', 'tail')


def bucket(authority_id, label):
    if not (authority_id or '').strip():
        return 'abstain'
    return 'correct' if authority_id.strip() == label else 'wrong'


def read_matcher_output(path):
    with open(path, encoding='utf-8', newline='') as f:
        return {r['guid']: r for r in csv.DictReader(f, delimiter='\t')}


def score(matcher_rows, labels, band_totals):
    if isinstance(matcher_rows, dict):
        by_guid = matcher_rows
    else:
        by_guid = {r['guid']: r for r in matcher_rows}

    bands = {b: {'correct': 0, 'wrong': 0, 'abstain': 0, 'scored': 0,
                 'accuracy': 0.0} for b in BAND_ORDER}
    excluded_none = 0
    missing = 0

    for guid, lab in labels.items():
        if lab['label_string_only'] == NONE and lab.get('label_world', NONE) == NONE:
            excluded_none += 1
            continue
        row = by_guid.get(guid)
        if row is None:
            missing += 1
            continue
        band = bands[lab['band']]
        band[bucket(row.get('authority_id', ''), lab['label_string_only'])] += 1
        band['scored'] += 1

    for b in BAND_ORDER:
        if bands[b]['scored']:
            bands[b]['accuracy'] = bands[b]['correct'] / bands[b]['scored']

    live = [b for b in BAND_ORDER if bands[b]['scored']]
    weight_total = sum(band_totals[b]['records'] for b in live) or 1
    record_accuracy = sum(
        (band_totals[b]['records'] / weight_total) * bands[b]['accuracy']
        for b in live)

    return {'bands': bands, 'record_accuracy': record_accuracy,
            'excluded_none': excluded_none, 'missing_from_output': missing}


def world_delta(labels):
    """Rows the descoped LLM enrichment step would have recovered."""
    recoverable = sum(
        1 for lab in labels.values()
        if lab['label_string_only'] == NONE and lab.get('label_world') not in
        (NONE, '', None))
    return {'recoverable': recoverable, 'total': len(labels)}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output', required=True, help='rtl_matcher results TSV')
    p.add_argument('--labels', default='eval/data/labels_final.tsv')
    p.add_argument('--bands', default='eval/data/bands.json')
    p.add_argument('--detail', help='write per-row detail TSV (dev only)')
    args = p.parse_args(argv)

    labels = read_labels(args.labels)
    with open(args.bands, encoding='utf-8') as f:
        band_totals = json.load(f)['bands']
    rows = read_matcher_output(args.output)
    result = score(rows, labels, band_totals)
    delta = world_delta(labels)

    print(f'record accuracy   {result["record_accuracy"]:.1%}')
    for b in BAND_ORDER:
        s = result['bands'][b]
        print(f'{b:5s} n={s["scored"]:>4} correct={s["correct"]:>4} '
              f'wrong={s["wrong"]:>4} abstain={s["abstain"]:>4} '
              f'acc={s["accuracy"]:.1%}')
    print(f'excluded, no PA record  {result["excluded_none"]}')
    print(f'missing from output     {result["missing_from_output"]}')
    print(f'world-knowledge upside  {delta["recoverable"]} of {delta["total"]} rows')

    if args.detail:
        with open(args.detail, 'w', encoding='utf-8', newline='') as f:
            w = csv.writer(f, delimiter='\t')
            w.writerow(['guid', 'place', 'band', 'label', 'authority_id',
                        'match_type', 'bucket'])
            for guid, lab in labels.items():
                row = rows.get(guid, {})
                w.writerow([guid, lab['place'], lab['band'],
                            lab['label_string_only'],
                            row.get('authority_id', ''),
                            row.get('match_type', ''),
                            bucket(row.get('authority_id', ''),
                                   lab['label_string_only'])])
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/natelemonnier/storied/code/anaconda-2 && python -m pytest eval/test_score_records.py -v`
Expected: PASS, 8 passed

- [ ] **Step 5: Run the whole suite**

Run: `cd /Users/natelemonnier/storied/code/anaconda-2 && python -m pytest eval/ -v`
Expected: PASS, all tests across the five test files

- [ ] **Step 6: Commit**

```bash
cd /Users/natelemonnier/storied/code/anaconda-2
git add eval/score_records.py eval/test_score_records.py
git commit -m "feat: record accuracy scorer with band reweighting"
```

---

### Task 7: Runbook

**Files:**
- Create: `code/anaconda-2/eval/README.md`
- Modify: `code/anaconda-2/README.md` — add an `eval/` entry under Components

**Interfaces:**
- Consumes: every script from Tasks 2 through 6.
- Produces: no code.

- [ ] **Step 1: Write `eval/README.md`**

Create `code/anaconda-2/eval/README.md`:

```markdown
# eval

Record accuracy measurement for `rtl_matcher`. Design:
`docs/superpowers/specs/2026-08-08-record-accuracy-metric-design.md`.

The metric answers one question: of the records we process, what share resolve
to the correct Place Authority record? Labels come from two model families that
never see matcher output, over a corpus the matcher has never run against.

## Build the sample, once

    python eval/build_eval_sample.py

Writes `eval/data/eval_dev.tsv`, `eval/data/eval_heldout.tsv`, `eval/data/bands.json`.
Seed 42, so this is reproducible. Head 800, mid 600, tail 600, split 50/50.

## Label, once per eval set

Gemini half. Needs `GEMINI_API_KEY` in `../.env` and `pip install google-genai`:

    python eval/label_gemini.py --sample eval/data/eval_dev.tsv \
        --out eval/data/labels_gemini_dev.tsv

Claude half, agent-driven:

    python eval/make_label_batches.py --sample eval/data/eval_dev.tsv
    # a subagent labels each eval/data/claude_batches/batch_NNN.md
    # and writes eval/data/claude_responses/batch_NNN.json
    python eval/ingest_claude_labels.py --sample eval/data/eval_dev.tsv \
        --out eval/data/labels_claude_dev.tsv

Merge:

    python eval/merge_labels.py --a eval/data/labels_gemini_dev.tsv \
        --b eval/data/labels_claude_dev.tsv \
        --out eval/data/labels_final_dev.tsv \
        --review eval/data/label_review_dev.tsv

Hand-adjudicate every row in `label_review_dev.tsv` and append the settled rows
to `labels_final_dev.tsv`. An unreviewed disagreement is not a label. Adjudication
resolves a conflict between two labelers and never reads matcher output, so it
does not burn held-out blindness.

## Score, every time the matcher changes

    python rtl_matcher.py --input eval/data/eval_dev.tsv \
        --pa <pa.tsv> --mnt <mnt.tsv> --output-dir eval/runs
    python eval/score_records.py --output eval/runs/MM-DD/eval_dev_01.tsv \
        --labels eval/data/labels_final_dev.tsv \
        --detail eval/data/dev_detail.tsv

## Held-out discipline

Held-out runs on a cadence, not per-change, and without `--detail`. Its output is
the aggregate numbers and nothing else. Diagnose drops on dev. When held-out is
eventually burned, mint a fresh one — snowball4 has 6.08M strings, so a blind set
is a renewable resource.

## What the numbers mean

- **record accuracy** — the headline. Band accuracy weighted by band record share.
  Head carries 81.4% of records, so head errors dominate, which is correct.
- **abstain** — the matcher claimed nothing. Never counted as correct. Where the
  label names a real place, an abstain is a miss.
- **excluded, no PA record** — neither label column found a PA record. No correct
  answer exists, so these leave the denominator. Quote this share whenever you
  quote the accuracy.
- **world-knowledge upside** — rows the string alone could not resolve but model
  world knowledge could. This is the size of the LLM enrichment step that was
  descoped. Bounded above by mid plus tail, 18.6% of records.
```

- [ ] **Step 2: Add the eval entry to the project README**

In `code/anaconda-2/README.md`, after the `evaluate_normalizer.py` section and before `### test_rtl_matcher.py`, insert:

```markdown
### eval/

Record accuracy measurement against a blind labeled sample of snowball4 — the
share of records that resolve to the correct authority record, reweighted by
frequency band. Labels come from two independent model families that never see
matcher output. See `eval/README.md` for the runbook.
```

- [ ] **Step 3: Verify both files read correctly**

Run: `cd /Users/natelemonnier/storied/code/anaconda-2 && python -m pytest eval/ -v && ls eval/`
Expected: tests pass; `eval/` lists `README.md`, `build_eval_sample.py`, `ingest_claude_labels.py`, `label_gemini.py`, `labels.py`, `make_label_batches.py`, `merge_labels.py`, `pa_index.py`, `prompt.py`, and five `test_*.py` files.

- [ ] **Step 4: Commit**

```bash
cd /Users/natelemonnier/storied/code/anaconda-2
git add eval/README.md README.md
git commit -m "docs: eval runbook and README entry"
```

---

## Notes for the implementer

**Tasks 1 and 2 are independent** and can run in parallel. Task 3 needs Task 1.
Task 4 needs 1 and 3. Task 5 needs 3 and 4. Task 6 needs 3. Task 7 needs everything.

**Tasks 1, 2, 3, and 6 need no API key** and are fully testable now. Task 4's
runtime path needs `GEMINI_API_KEY`, but its unit tests do not — `google.genai`
is imported inside `main`, deliberately, so the suite runs without the dependency.

**Do not import anything from `code/place-normalizer`.** Its LLM stack resolves
through the same MNT `rtl_matcher` consumes and weights the answer 70% MNT lookup
against 30% model, so its consensus measures MNT agreement rather than geography.
That is the specific thing this design exists to avoid.
