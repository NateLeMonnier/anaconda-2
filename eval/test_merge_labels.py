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
    agreed, review = merge({'G1': label('G1', 'U-1', 'U-1')},
                           {'G1': label('G1', 'U-1', 'U-1')})
    assert [r['guid'] for r in agreed] == ['G1']
    assert review == []


def test_disagreement_on_string_only_goes_to_review():
    agreed, review = merge({'G1': label('G1', 'U-1', 'U-1')},
                           {'G1': label('G1', 'U-2', 'U-1')})
    assert agreed == []
    assert review[0]['guid'] == 'G1'
    assert review[0]['disagreement'] == 'label_string_only'


def test_disagreement_on_world_column_alone_still_goes_to_review():
    agreed, review = merge({'G1': label('G1', 'U-1', 'U-1')},
                           {'G1': label('G1', 'U-1', 'U-9')})
    assert agreed == []
    assert review[0]['disagreement'] == 'label_world'


def test_both_columns_disagreeing_reports_both():
    _, review = merge({'G1': label('G1', 'U-1', 'U-1')},
                      {'G1': label('G1', 'U-2', 'U-9')})
    assert review[0]['disagreement'] == 'label_string_only,label_world'


def test_agreement_on_none_is_still_agreement():
    agreed, _ = merge({'G1': label('G1', NONE, NONE)},
                      {'G1': label('G1', NONE, NONE)})
    assert len(agreed) == 1


def test_row_missing_from_one_labeler_goes_to_review():
    agreed, review = merge({'G1': label('G1', 'U-1', 'U-1')}, {})
    assert agreed == []
    assert review[0]['disagreement'] == 'missing_from_b'


def test_row_missing_from_the_first_labeler_is_named_correctly():
    _, review = merge({}, {'G1': label('G1', 'U-1', 'U-1')})
    assert review[0]['disagreement'] == 'missing_from_a'
    assert review[0]['b_label_string_only'] == 'U-1'
    assert review[0]['a_label_string_only'] == ''


def test_review_rows_carry_both_labelers_answers():
    _, review = merge({'G1': label('G1', 'U-1', 'U-1')},
                      {'G1': label('G1', 'U-2', 'U-1')})
    assert review[0]['a_label_string_only'] == 'U-1'
    assert review[0]['b_label_string_only'] == 'U-2'


def test_labels_round_trip_through_tsv(tmp_path):
    path = tmp_path / 'labels.tsv'
    write_labels(str(path), [label('G1', 'U-1', 'U-1')], source='test')
    back = read_labels(str(path))
    assert back['G1']['label_string_only'] == 'U-1'
    assert list(back['G1'].keys()) == LABEL_FIELDS
