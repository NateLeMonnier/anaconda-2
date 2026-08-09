"""Tests for the stratified eval sample builder."""
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
