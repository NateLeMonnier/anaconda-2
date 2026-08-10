"""Tests for the failure taxonomy.

Classification bugs here are silent: a row lands in the wrong bucket and the
report reads as a confident diagnosis. The token-boundary test is the one that
has already been wrong once, matching CAMBRIDGESHIRE to Cambridge.
"""
from analyze_failures import (
    failure_class,
    name_in,
    reachability,
    shape_of,
    string_terms,
)

PA = {
    'USA': {'name': 'USA', 'level': '8', 'parent': ''},
    'OH': {'name': 'Ohio', 'level': '6', 'parent': 'USA'},
    'TUS': {'name': 'Tuscarawas', 'level': '5', 'parent': 'OH'},
    'SCH': {'name': 'Schoenbrunn', 'level': '4', 'parent': 'TUS'},
    'CAMBS': {'name': 'Cambridgeshire', 'level': '5', 'parent': 'UK'},
    'CAM': {'name': 'Cambridge', 'level': '4', 'parent': 'CAMBS'},
    'UK': {'name': 'United Kingdom', 'level': '8', 'parent': ''},
}
BY_NAME = {'usa': ['USA'], 'ohio': ['OH'], 'tuscarawas': ['TUS'],
           'schoenbrunn': ['SCH'], 'cambridgeshire': ['CAMBS'],
           'cambridge': ['CAM'], 'united kingdom': ['UK']}


def row(place, truth_id, verdict='wrong', matcher_id='', skipped='',
        match_type='chain_verified'):
    return {'place': place, 'truth_id': truth_id, 'verdict': verdict,
            'matcher_id': matcher_id, 'skipped_terms': skipped,
            'match_type': match_type, 'candidates': '1'}


# ---------------------------------------------------------------------------
# Token matching
# ---------------------------------------------------------------------------

def test_a_name_matches_on_whole_tokens():
    assert name_in('Schoenbrunn', 'Schoenbrunn Village')


def test_a_name_does_not_match_inside_a_longer_word():
    # The bug this test exists for: Cambridge is not present in
    # CAMBRIDGESHIRE, and treating it as present invents a failure class.
    assert not name_in('Cambridge', 'CAMBRIDGESHIRE')


def test_multi_word_names_match_as_a_run():
    assert name_in('New York', 'Ward 3 New York city')
    assert not name_in('New York', 'York, New Jersey')


def test_matching_ignores_case_and_punctuation():
    assert name_in('St. Joseph', 'st joseph')


def test_string_terms_splits_on_both_separators():
    assert string_terms('A, B; C') == ['A', 'B', 'C']
    assert string_terms('A,, B') == ['A', 'B']


# ---------------------------------------------------------------------------
# Reachability
# ---------------------------------------------------------------------------

def test_named_and_chain_connected_is_reachable():
    r = row('Schoenbrunn Village, Ohio', 'SCH')
    assert reachability(r, PA, BY_NAME) == 'reachable'


def test_named_without_chain_support_is_unsupported():
    # Nothing else in the string corroborates it, so committing means
    # loosening the low-evidence gate rather than fixing the walk.
    r = row('Schoenbrunn', 'SCH')
    assert reachability(r, PA, BY_NAME) == 'unsupported'


def test_a_truth_that_never_appears_is_absent():
    r = row('Somewhere Else, Ohio', 'SCH')
    assert reachability(r, PA, BY_NAME) == 'absent'


def test_a_truth_missing_from_pa_is_absent():
    assert reachability(row('X, Ohio', 'GONE'), PA, BY_NAME) == 'absent'


# ---------------------------------------------------------------------------
# Failure classes
# ---------------------------------------------------------------------------

def test_a_discarded_term_holding_only_the_answer_is_its_own_class():
    r = row('Schoenbrunn, Ohio', 'SCH', matcher_id='OH',
            skipped='Schoenbrunn')
    assert failure_class(r, PA) == 'leaf discarded, term was exactly the answer'


def test_a_discarded_term_wrapped_in_a_jurisdiction_suffix():
    r = row('Schoenbrunn Village, Ohio', 'SCH', matcher_id='OH',
            skipped='Schoenbrunn Village')
    assert failure_class(r, PA) == \
        'leaf discarded, wrapped in noise: jurisdiction suffix'


def test_a_discarded_term_wrapped_in_an_enumeration_number():
    r = row('Precinct 1 Schoenbrunn, Ohio', 'SCH', matcher_id='OH',
            skipped='Precinct 1 Schoenbrunn')
    assert failure_class(r, PA) == \
        'leaf discarded, wrapped in noise: enumeration / precinct / ward'


def test_proximity_annotations_are_not_mistaken_for_skipped_terms():
    # skipped_terms mixes real terms with proximity log text; reading the log
    # line as an input term invents a modifier-strip opportunity.
    r = row('Schoenbrunn, Ohio', 'SCH', matcher_id='OH',
            skipped='Schoenbrunn County (proximity: 10km, actual: Tuscarawas)')
    assert failure_class(r, PA) != 'leaf discarded, term was exactly the answer'


def test_committing_to_an_ancestor_is_its_own_class():
    r = row('Schoenbrunn, Ohio', 'SCH', matcher_id='TUS')
    assert failure_class(r, PA) == 'committed to an ancestor of the leaf'


def test_same_name_different_record_reports_both_levels():
    pa = dict(PA, LEECITY={'name': 'Lee', 'level': '4', 'parent': 'OH'},
              LEECO={'name': 'Lee', 'level': '5', 'parent': 'OH'})
    r = row('Lee, Ohio', 'LEECO', matcher_id='LEECITY')
    assert failure_class(r, pa) == 'same name, wrong record (got L4, truth L5)'


def test_an_abstention_carries_its_match_type():
    r = row('Lee Co., South Carolina', 'TUS', verdict='abstain',
            match_type='chain_amb')
    assert failure_class(r, PA) == 'abstained (chain_amb)'


# ---------------------------------------------------------------------------
# String shape
# ---------------------------------------------------------------------------

def test_shape_recognises_an_enumeration_district():
    assert shape_of('Precinct 1 Omaha city Ward 8') == \
        'enumeration district / precinct'


def test_shape_prefers_enumeration_over_jurisdiction_suffix():
    # "Omaha city" carries both markers; the enumeration prefix is the one
    # that predicts the failure, so it has to win.
    assert 'enumeration' in shape_of('District 4 Omaha city, Douglas')


def test_shape_falls_through_to_plain():
    assert shape_of('Malone, New York') == 'plain place string'
