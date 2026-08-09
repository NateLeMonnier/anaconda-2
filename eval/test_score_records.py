"""Tests for the record accuracy scorer."""
from labels import NONE
from score_records import bucket, score, world_delta


def label(guid, string_only, world=None, band='head'):
    return {'guid': guid, 'place': f'{guid} place', 'band': band,
            'label_string_only': string_only,
            'label_world': world if world is not None else string_only}


def matched(guid, authority_id, match_type='x'):
    return {'guid': guid, 'authority_id': authority_id, 'match_type': match_type}


TOTALS = {'head': {'strings': 10, 'records': 8000},
          'mid': {'strings': 10, 'records': 1500},
          'tail': {'strings': 10, 'records': 500}}


def test_empty_authority_id_is_abstain_not_wrong():
    assert bucket('', 'U-1') == 'abstain'


def test_whitespace_only_authority_id_is_also_abstain():
    assert bucket('   ', 'U-1') == 'abstain'


def test_exact_id_match_is_correct():
    assert bucket('U-1', 'U-1') == 'correct'


def test_any_other_id_is_wrong_including_an_ancestor():
    assert bucket('U-PARENT', 'U-1') == 'wrong'


def test_per_band_accuracy_counts_only_scored_rows():
    labels = {'G1': label('G1', 'U-1'), 'G2': label('G2', 'U-2')}
    got = score([matched('G1', 'U-1'), matched('G2', '')], labels, TOTALS)
    assert got['bands']['head']['correct'] == 1
    assert got['bands']['head']['abstain'] == 1
    assert got['bands']['head']['accuracy'] == 0.5


def test_none_labels_are_excluded_from_the_denominator():
    labels = {'G1': label('G1', 'U-1'), 'G2': label('G2', NONE, NONE)}
    got = score([matched('G1', 'U-1'), matched('G2', '')], labels, TOTALS)
    assert got['bands']['head']['scored'] == 1
    assert got['bands']['head']['accuracy'] == 1.0
    assert got['excluded_none'] == 1


def test_none_string_only_but_resolvable_world_still_scores():
    # Excluded only when BOTH columns are NONE. A row the string cannot
    # resolve but world knowledge can is still a real place, so the matcher
    # abstaining on it is a miss, not a free pass.
    labels = {'G1': label('G1', NONE, 'U-9')}
    got = score([matched('G1', '')], labels, TOTALS)
    assert got['excluded_none'] == 0
    assert got['bands']['head']['abstain'] == 1


def test_rows_absent_from_matcher_output_are_counted_not_silently_dropped():
    labels = {'G1': label('G1', 'U-1'), 'G2': label('G2', 'U-2')}
    got = score([matched('G1', 'U-1')], labels, TOTALS)
    assert got['missing_from_output'] == 1
    assert got['bands']['head']['scored'] == 1


def test_record_accuracy_weights_bands_by_record_share():
    labels = {'H': label('H', 'U-1', band='head'),
              'T': label('T', 'U-2', band='tail')}
    got = score([matched('H', 'U-1'), matched('T', 'WRONG')], labels, TOTALS)
    # head and tail are the only live bands: 8000/(8000+500) * 1.0
    assert abs(got['record_accuracy'] - 8000 / 8500) < 0.001


def test_record_accuracy_is_not_the_unweighted_mean():
    # Guards the whole point of the metric: a head win must outweigh a tail
    # loss. The unweighted mean of these two bands would be 0.5.
    labels = {'H': label('H', 'U-1', band='head'),
              'T': label('T', 'U-2', band='tail')}
    got = score([matched('H', 'U-1'), matched('T', 'WRONG')], labels, TOTALS)
    assert got['record_accuracy'] > 0.9


def test_bands_with_no_scored_rows_do_not_break_the_weighting():
    labels = {'H': label('H', 'U-1', band='head')}
    got = score([matched('H', 'U-1')], labels, TOTALS)
    assert abs(got['record_accuracy'] - 1.0) < 0.001


def test_score_accepts_a_dict_keyed_by_guid():
    labels = {'G1': label('G1', 'U-1')}
    got = score({'G1': matched('G1', 'U-1')}, labels, TOTALS)
    assert got['bands']['head']['correct'] == 1


def test_world_delta_counts_rows_world_knowledge_would_have_recovered():
    labels = {'G1': label('G1', NONE, 'U-9'), 'G2': label('G2', 'U-1', 'U-1')}
    got = world_delta(labels)
    assert got['recoverable'] == 1
    assert got['total'] == 2


def test_world_delta_ignores_rows_neither_column_resolved():
    labels = {'G1': label('G1', NONE, NONE)}
    assert world_delta(labels)['recoverable'] == 0
