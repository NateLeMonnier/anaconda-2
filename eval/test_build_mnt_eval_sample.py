"""Tests for the MNT eval sample builder."""
from build_mnt_eval_sample import (
    band_weights,
    dedupe_canonical,
    drop_reason,
    filter_pool,
    guid_for,
    input_row,
    label_row,
    load_exclusions,
    load_pa_ids,
    split_dev_heldout,
    stratified_sample,
)
from labels import ABSTAIN


def pool_row(place, band='head', total='5000', authority_id='U-1',
             match_status='UUID Verified', multiple_uuid='',
             authority_name='Name', typeahead='Name, State, USA'):
    return {'place': place, 'band': band, 'total': total,
            'authority_id': authority_id, 'authority_name': authority_name,
            'typeahead': typeahead, 'match_status': match_status,
            'multiple_uuid': multiple_uuid, 'geoclass': 'USA',
            'project': '1940 Township|Place'}


PA = {'U-1', 'U-2'}


# ---------------------------------------------------------------------------
# Which pool rows can carry a label
# ---------------------------------------------------------------------------

def test_a_verified_row_with_a_pa_backed_id_is_usable():
    assert drop_reason(pool_row('Malone, NY'), PA, set()) is None


def test_an_unverified_row_is_dropped():
    # Match_Status is the curator's sign-off. Without it the label is a guess.
    row = pool_row('Malone, NY', match_status='Invalid UUID')
    assert drop_reason(row, PA, set()) == 'not_verified'


def test_a_label_absent_from_pa_is_dropped():
    # No PA record means no correct answer exists, so the row would only
    # dilute the denominator.
    row = pool_row('Malone, NY', authority_id='U-GONE')
    assert drop_reason(row, PA, set()) == 'no_pa_record'


def test_an_illegible_row_is_kept():
    # Abstaining is the right answer here, and these rows are the only test
    # the low-evidence gate gets.
    assert drop_reason(pool_row('4099', authority_id='Ill'), PA, set()) is None


def test_an_ambiguous_row_is_kept():
    assert drop_reason(pool_row('York Co', authority_id='Amb'), PA, set()) is None


def test_a_pseudo_id_is_not_checked_against_pa():
    # Ill and Amb are curator verdicts, not UUIDs; looking them up in PA and
    # dropping the miss would delete every abstain case.
    assert drop_reason(pool_row('x', authority_id='ILL'), set(), set()) is None


def test_a_string_seen_in_development_is_dropped():
    row = pool_row('Malone, NY')
    seen = load_exclusions([])  # empty, then add by hand
    seen.add('malone, ny')
    assert drop_reason(row, PA, seen) == 'seen_in_development'


def test_exclusions_match_on_the_canonical_form_not_the_literal():
    row = pool_row('Malone ,  NY')
    assert drop_reason(row, PA, {'malone, ny'}) == 'seen_in_development'


def test_rows_with_no_place_or_no_id_are_dropped():
    assert drop_reason(pool_row(''), PA, set()) == 'no_place'
    assert drop_reason(pool_row('x', authority_id=''), PA, set()) \
        == 'no_authority_id'


def test_filter_pool_counts_every_reason_it_dropped_for():
    rows = [pool_row('Keep, NY'),
            pool_row('Bad, NY', match_status=''),
            pool_row('Gone, NY', authority_id='U-GONE')]
    kept, dropped = filter_pool(rows, PA, set())
    assert [r['place'] for r in kept] == ['Keep, NY']
    assert dropped == {'not_verified': 1, 'no_pa_record': 1}


# ---------------------------------------------------------------------------
# Dedupe and guid
# ---------------------------------------------------------------------------

def test_canonical_duplicates_collapse_to_one_row():
    # The guid is derived from the canonical form and the scorer joins on it,
    # so a duplicate would drop a row silently.
    rows = [pool_row('Malone, NY'), pool_row('malone ,  ny'),
            pool_row('Peru, VT')]
    assert [r['place'] for r in dedupe_canonical(rows)] == ['Malone, NY',
                                                            'Peru, VT']


def test_guid_is_stable_across_calls():
    assert guid_for('Malone, NY') == guid_for('Malone, NY')


def test_guid_ignores_spacing_and_case():
    assert guid_for('Malone, NY') == guid_for('MALONE ,  ny')


def test_guid_separates_different_places():
    assert guid_for('Malone, NY') != guid_for('Peru, VT')


# ---------------------------------------------------------------------------
# Sampling
# ---------------------------------------------------------------------------

def pool(n=40):
    rows = []
    for band, total in (('head', '200000'), ('mid', '5000'),
                        ('low', '100'), ('tail', '3')):
        rows += [pool_row(f'{band}{i}, State, USA', band=band, total=total)
                 for i in range(n)]
    return rows


def test_stratified_sample_honours_the_quota_per_band():
    got = stratified_sample(pool(), {'head': 10, 'mid': 8, 'low': 6, 'tail': 4},
                            42)
    assert [len(got[b]) for b in ('head', 'mid', 'low', 'tail')] == [10, 8, 6, 4]


def test_stratified_sample_takes_everything_when_a_band_is_short():
    got = stratified_sample(pool(n=3), {'head': 10, 'mid': 10, 'low': 10,
                                        'tail': 10}, 42)
    assert len(got['head']) == 3


def test_stratified_sample_is_deterministic_under_a_seed():
    quotas = {'head': 10, 'mid': 10, 'low': 10, 'tail': 10}
    a = stratified_sample(pool(), quotas, 42)
    b = stratified_sample(pool(), quotas, 42)
    assert [r['place'] for r in a['head']] == [r['place'] for r in b['head']]


def test_stratified_sample_ignores_the_order_filemaker_returned_pages_in():
    # The pool is paged out of a found set; the draw has to depend on the
    # seed alone or it is not reproducible.
    quotas = {'head': 10, 'mid': 10, 'low': 10, 'tail': 10}
    forward = stratified_sample(pool(), quotas, 42)
    backward = stratified_sample(list(reversed(pool())), quotas, 42)
    assert sorted(r['place'] for r in forward['head']) == \
        sorted(r['place'] for r in backward['head'])


def test_the_two_halves_are_disjoint_and_even():
    quotas = {'head': 10, 'mid': 10, 'low': 10, 'tail': 10}
    dev, heldout = split_dev_heldout(stratified_sample(pool(), quotas, 42), 42)
    assert {r['place'] for r in dev}.isdisjoint({r['place'] for r in heldout})
    assert len(dev) == len(heldout) == 20


# ---------------------------------------------------------------------------
# Labels
# ---------------------------------------------------------------------------

def test_a_verified_row_labels_to_its_authority_id():
    row = label_row(pool_row('Malone, NY', authority_id='u-1'))
    assert row['label_string_only'] == 'U-1'
    assert row['status_string_only'] == 'mnt_verified'


def test_an_illegible_row_labels_to_abstain():
    row = label_row(pool_row('4099', authority_id='Ill'))
    assert row['label_string_only'] == ABSTAIN
    assert row['status_string_only'] == 'mnt_illegible'


def test_an_ambiguous_row_labels_to_abstain():
    row = label_row(pool_row('York Co', authority_id='Amb'))
    assert row['label_string_only'] == ABSTAIN
    assert row['status_string_only'] == 'mnt_ambiguous'


def test_the_multiple_uuid_flag_rides_along_in_the_status():
    # 20.4% of the MNT is flagged. Carrying it lets the score be quoted with
    # and without those rows instead of relabelling.
    row = label_row(pool_row('Malone, NY', multiple_uuid='True'))
    assert row['status_string_only'] == 'mnt_verified_multi'


def test_the_world_column_repeats_the_string_only_column():
    # A curator worked from the same string the matcher gets, so the
    # world-knowledge delta is zero here by construction.
    row = label_row(pool_row('Malone, NY'))
    assert row['label_world'] == row['label_string_only']


def test_label_and_input_rows_share_a_guid():
    row = pool_row('Malone, NY')
    assert label_row(row)['guid'] == input_row(row)['guid']


def test_input_frequency_is_the_record_count():
    assert input_row(pool_row('Malone, NY', total='7'))['frequency'] == '7'


# ---------------------------------------------------------------------------
# Band weights
# ---------------------------------------------------------------------------

def test_string_counts_come_from_the_population_not_the_pool():
    # The pool is a sample; the string count is FileMaker's exact foundCount.
    weights = band_weights(pool(n=10), {'head': 10159, 'mid': 158187,
                                        'low': 838894, 'tail': 250550})
    assert weights['head']['strings'] == 10159


def test_records_are_the_population_times_the_sampled_mean():
    weights = band_weights([pool_row('a', band='head', total='100'),
                            pool_row('b', band='head', total='300')],
                           {'head': 10})
    assert weights['head']['mean_total'] == 200.0
    assert weights['head']['records'] == 2000


def test_records_are_marked_estimated():
    # There is no way to sum a field across a found set through the Data API,
    # so this number must never be quoted as exact.
    weights = band_weights(pool(n=2), {'head': 10})
    assert weights['head']['records_estimated'] is True


def test_a_band_with_no_sampled_rows_weighs_nothing_rather_than_crashing():
    weights = band_weights([], {'head': 10159})
    assert weights['head']['records'] == 0


def test_non_numeric_totals_are_ignored_rather_than_raising():
    weights = band_weights([pool_row('a', band='head', total=''),
                            pool_row('b', band='head', total='100')],
                           {'head': 10})
    assert weights['head']['mean_total'] == 100.0


def test_load_pa_ids_reads_the_id_column_not_uuid(tmp_path):
    # The PA export keys on ID; reading UUID gives an empty set and every row
    # then drops as no_pa_record.
    path = tmp_path / 'pa.tsv'
    path.write_text('Level\tTerm\tID\tFullChainName\n4\tMalone\tu-1\tMalone, NY\n',
                    encoding='utf-8')
    assert load_pa_ids(str(path)) == {'U-1'}
