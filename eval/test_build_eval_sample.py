"""Tests for the stratified eval sample builder."""
from build_eval_sample import (
    band_for,
    band_record_totals,
    load_corpus,
    load_exclusions,
    split_dev_heldout,
    stratified_sample,
)


def write_corpus(tmp_path, rows):
    path = tmp_path / 'corpus.tsv'
    body = ''.join(f'{p}\t{inf}\t{g}\t{fr}\n' for p, inf, g, fr in rows)
    path.write_text('place\tinferred_location\tguid\tfrequency\n' + body,
                    encoding='utf-8')
    return str(path)


def test_load_corpus_sums_frequency_across_rows_sharing_a_guid(tmp_path):
    # snowball4 has one row per (place, inferred_location) pair, so 142,029
    # guids arrive with their record count split. Summing first is what makes
    # band assignment correct.
    path = write_corpus(tmp_path, [
        ('Brown University, Rhode Island', '', 'G1', '8'),
        ('Brown University, Rhode Island', 'Rhode Island', 'G1', '4'),
    ])
    rows = load_corpus(path, set())
    assert len(rows) == 1
    assert rows[0]['frequency'] == '12'


def test_load_corpus_keeps_guid_unique(tmp_path):
    # The scorer joins on guid; a duplicate would silently drop a row.
    path = write_corpus(tmp_path, [
        ('A, State', '', 'G1', '5'), ('A, State', 'X', 'G1', '5'),
        ('B, State', '', 'G2', '7'),
    ])
    rows = load_corpus(path, set())
    assert len({r['guid'] for r in rows}) == len(rows)


def test_load_corpus_summing_can_promote_a_row_into_a_higher_band(tmp_path):
    path = write_corpus(tmp_path, [
        ('Split, State', '', 'G1', '6'), ('Split, State', 'X', 'G1', '6'),
    ])
    rows = load_corpus(path, set())
    assert band_for(int(rows[0]['frequency'])) == 'mid'  # 12, not 6


def test_load_corpus_drops_excluded_place_strings(tmp_path):
    path = write_corpus(tmp_path, [
        ('Seen, State', '', 'G1', '5'), ('Fresh, State', '', 'G2', '5'),
    ])
    rows = load_corpus(path, {'Seen, State'})
    assert [r['guid'] for r in rows] == ['G2']


def test_load_corpus_skips_rows_missing_a_guid(tmp_path):
    path = write_corpus(tmp_path, [('A, State', '', '', '5')])
    assert load_corpus(path, set()) == []


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


def test_stratified_sample_is_insensitive_to_corpus_row_order():
    # The pool is sorted by guid before sampling, so a reordered corpus must
    # draw the same rows — otherwise the sample is not reproducible from the
    # seed alone.
    forward = stratified_sample(corpus(), {'head': 10, 'mid': 5, 'tail': 5}, 42)
    reversed_rows = list(reversed(corpus()))
    backward = stratified_sample(reversed_rows, {'head': 10, 'mid': 5, 'tail': 5}, 42)
    assert sorted(r['guid'] for r in forward['head']) == \
        sorted(r['guid'] for r in backward['head'])


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


def test_split_drops_the_odd_row_rather_than_skewing_a_half():
    sampled = stratified_sample(corpus(), {'head': 7, 'mid': 0, 'tail': 0}, 42)
    dev, heldout = split_dev_heldout(sampled, 42)
    assert len(dev) == 3
    assert len(heldout) == 3


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
