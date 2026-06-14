"""Tests for batch parent pre-fetch and resolve_parent_only in rtl_matcher."""
import pytest
from unittest.mock import MagicMock
from rtl_matcher import prefetch_parent_chains, resolve_parent_only, BATCH, detect_tie


def make_auth_record(uuid, parent_uuid=None, name="Place"):
    return {
        'UUID': uuid,
        'Parent_UUID': parent_uuid or '',
        'Auth_Place_Name': name,
    }


def make_fm_response(records):
    return [{'fieldData': r} for r in records]


class TestPrefetchParentChains:
    def test_no_parents_to_fetch(self):
        auth_cache = {
            'aaa': make_auth_record('aaa'),
        }
        client = MagicMock()
        prefetch_parent_chains(client, auth_cache)
        client.find.assert_not_called()

    def test_fetches_missing_parent(self):
        auth_cache = {
            'child': make_auth_record('child', parent_uuid='parent'),
        }
        parent_rec = make_auth_record('parent', name='ParentPlace')
        client = MagicMock()
        client.find.return_value = make_fm_response([parent_rec])

        prefetch_parent_chains(client, auth_cache)

        assert 'parent' in auth_cache
        assert auth_cache['parent']['Auth_Place_Name'] == 'ParentPlace'

    def test_walks_multiple_levels(self):
        auth_cache = {
            'city': make_auth_record('city', parent_uuid='county'),
        }
        county_rec = make_auth_record('county', parent_uuid='state')
        state_rec = make_auth_record('state', parent_uuid='country')
        country_rec = make_auth_record('country')

        client = MagicMock()
        client.find.side_effect = [
            make_fm_response([county_rec]),
            make_fm_response([state_rec]),
            make_fm_response([country_rec]),
        ]

        prefetch_parent_chains(client, auth_cache)

        assert 'county' in auth_cache
        assert 'state' in auth_cache
        assert 'country' in auth_cache

    def test_skips_already_cached_parents(self):
        auth_cache = {
            'child': make_auth_record('child', parent_uuid='parent'),
            'parent': make_auth_record('parent'),
        }
        client = MagicMock()
        prefetch_parent_chains(client, auth_cache)
        client.find.assert_not_called()

    def test_handles_empty_find_results(self):
        auth_cache = {
            'child': make_auth_record('child', parent_uuid='missing'),
        }
        client = MagicMock()
        client.find.return_value = []

        prefetch_parent_chains(client, auth_cache)

        assert 'missing' not in auth_cache

    def test_terminates_when_no_new_parents(self):
        auth_cache = {
            'a': make_auth_record('a', parent_uuid='b'),
        }
        b_rec = make_auth_record('b')
        client = MagicMock()
        client.find.return_value = make_fm_response([b_rec])

        prefetch_parent_chains(client, auth_cache)

        assert client.find.call_count == 1

    def test_deduplicates_parent_uuids_across_records(self):
        auth_cache = {
            'child1': make_auth_record('child1', parent_uuid='shared_parent'),
            'child2': make_auth_record('child2', parent_uuid='shared_parent'),
        }
        parent_rec = make_auth_record('shared_parent')
        client = MagicMock()
        client.find.return_value = make_fm_response([parent_rec])

        prefetch_parent_chains(client, auth_cache)

        assert client.find.call_count == 1
        query_arg = client.find.call_args[0][1]
        uuid_queries = [q['UUID'] for q in query_arg]
        assert len(uuid_queries) == 1

    def test_batches_large_sets(self):
        auth_cache = {}
        parent_recs = []
        for i in range(250):
            child_id = f'child_{i}'
            parent_id = f'parent_{i}'
            auth_cache[child_id] = make_auth_record(child_id, parent_uuid=parent_id)
            parent_recs.append(make_auth_record(parent_id))

        batched_responses = []
        for i in range(0, 250, BATCH):
            batch_recs = parent_recs[i:i + BATCH]
            batched_responses.append(make_fm_response(batch_recs))

        client = MagicMock()
        client.find.side_effect = batched_responses

        prefetch_parent_chains(client, auth_cache)

        assert client.find.call_count == 3


def make_auth_record_full(uuid, parent_uuid=None, name="Place", level="4",
                          population="", jurisdiction=""):
    return {
        'UUID': uuid,
        'Parent_UUID': parent_uuid or '',
        'Auth_Place_Name': name,
        'Level': level,
        'Population': population,
        'Jurisdiction': jurisdiction,
        'Type_Ahead_Value': '',
    }


class TestResolveParentOnly:
    def test_single_candidate_returns_it(self):
        uid = 'state-001'
        auth_cache = {uid: make_auth_record_full(uid, level='6', name='New York')}
        result = resolve_parent_only([uid], auth_cache, MagicMock())
        assert result == (uid, 'parent_resolved')

    def test_high_level_preferred_over_low_when_low_pops_zero(self):
        state = 'state-001'
        city_a = 'city-001'
        city_b = 'city-002'
        auth_cache = {
            state: make_auth_record_full(state, level='6', name='New York', population='0'),
            city_a: make_auth_record_full(city_a, level='4', name='Syracuse', population='0'),
            city_b: make_auth_record_full(city_b, level='4', name='Syracuse', population='0'),
        }
        result = resolve_parent_only([state, city_a, city_b], auth_cache, MagicMock())
        assert result == (state, 'parent_resolved')

    def test_low_pop_over_50k_rest_zero_wins(self):
        big_city = 'city-big'
        small_city = 'city-small'
        state = 'state-001'
        auth_cache = {
            big_city: make_auth_record_full(big_city, level='4', population='75000'),
            small_city: make_auth_record_full(small_city, level='4', population='0'),
            state: make_auth_record_full(state, level='6', population='5000000'),
        }
        result = resolve_parent_only([big_city, small_city, state], auth_cache, MagicMock())
        assert result == (big_city, 'parent_resolved')

    def test_low_pop_under_50k_escalates_to_high(self):
        city = 'city-001'
        state = 'state-001'
        auth_cache = {
            city: make_auth_record_full(city, level='4', population='5000'),
            state: make_auth_record_full(state, level='6', population='3000000'),
        }
        result = resolve_parent_only([city, state], auth_cache, MagicMock())
        assert result == (state, 'parent_resolved')

    def test_low_pop_5x_rule(self):
        big_city = 'city-big'
        small_city = 'city-small'
        state = 'state-001'
        auth_cache = {
            big_city: make_auth_record_full(big_city, level='4', population='675000'),
            small_city: make_auth_record_full(small_city, level='4', population='2000'),
            state: make_auth_record_full(state, level='6', population='10000000'),
        }
        result = resolve_parent_only([big_city, small_city, state], auth_cache, MagicMock())
        assert result == (big_city, 'parent_resolved')

    def test_low_pop_close_escalates_to_high(self):
        city_a = 'city-a'
        city_b = 'city-b'
        state = 'state-001'
        auth_cache = {
            city_a: make_auth_record_full(city_a, level='4', population='60000'),
            city_b: make_auth_record_full(city_b, level='4', population='55000'),
            state: make_auth_record_full(state, level='6', population='8000000'),
        }
        result = resolve_parent_only([city_a, city_b, state], auth_cache, MagicMock())
        assert result == (state, 'parent_resolved')

    def test_no_high_and_low_ambiguous_returns_amb(self):
        city_a = 'city-a'
        city_b = 'city-b'
        auth_cache = {
            city_a: make_auth_record_full(city_a, level='4', population='60000'),
            city_b: make_auth_record_full(city_b, level='4', population='55000'),
        }
        result = resolve_parent_only([city_a, city_b], auth_cache, MagicMock())
        assert result == (None, 'amb')

    def test_no_low_single_high_returns_it(self):
        state = 'state-001'
        auth_cache = {
            state: make_auth_record_full(state, level='6', population='5000000'),
        }
        result = resolve_parent_only([state], auth_cache, MagicMock())
        assert result == (state, 'parent_resolved')

    def test_multiple_high_amb_when_both_populated(self):
        state = 'state-001'
        country = 'country-001'
        auth_cache = {
            state: make_auth_record_full(state, level='6', population='10700000'),
            country: make_auth_record_full(country, level='8', population='3700000'),
        }
        result = resolve_parent_only([state, country], auth_cache, MagicMock())
        assert result == (None, 'amb')

    def test_multiple_high_5x_rule(self):
        big_state = 'state-big'
        small_region = 'region-small'
        auth_cache = {
            big_state: make_auth_record_full(big_state, level='6', population='10000000'),
            small_region: make_auth_record_full(small_region, level='5', population='500'),
        }
        result = resolve_parent_only([big_state, small_region], auth_cache, MagicMock())
        assert result == (big_state, 'parent_resolved')

    def test_multiple_high_all_zero_amb(self):
        state_a = 'state-a'
        state_b = 'state-b'
        auth_cache = {
            state_a: make_auth_record_full(state_a, level='6', population='0'),
            state_b: make_auth_record_full(state_b, level='6', population='0'),
        }
        result = resolve_parent_only([state_a, state_b], auth_cache, MagicMock())
        assert result == (None, 'amb')

    def test_missing_population_treated_as_zero(self):
        city_no_pop = 'city-nopop'
        state = 'state-001'
        rec = make_auth_record_full(city_no_pop, level='4')
        del rec['Population']
        auth_cache = {
            city_no_pop: rec,
            state: make_auth_record_full(state, level='6', population='5000000'),
        }
        result = resolve_parent_only([city_no_pop, state], auth_cache, MagicMock())
        assert result == (state, 'parent_resolved')

    def test_low_all_zero_no_high_returns_amb(self):
        city_a = 'city-a'
        city_b = 'city-b'
        auth_cache = {
            city_a: make_auth_record_full(city_a, level='4', population='0'),
            city_b: make_auth_record_full(city_b, level='4', population='0'),
        }
        result = resolve_parent_only([city_a, city_b], auth_cache, MagicMock())
        assert result == (None, 'amb')


from rtl_matcher import rank_candidates


class TestRankCandidates:
    def test_single_candidate_returns_it(self):
        auth_cache = {
            'aaa': make_auth_record_full('aaa', level='6', population='300000'),
        }
        result = rank_candidates(['aaa'], auth_cache, parent_level=8)
        assert len(result) == 1
        assert result[0][0] == 'aaa'

    def test_smaller_level_gap_wins(self):
        auth_cache = {
            'state': make_auth_record_full('state', level='6', population='100000'),
            'city': make_auth_record_full('city', level='4', population='500000'),
        }
        result = rank_candidates(['state', 'city'], auth_cache, parent_level=8)
        assert result[0][0] == 'state'
        assert result[1][0] == 'city'

    def test_same_gap_higher_pop_wins(self):
        auth_cache = {
            'big': make_auth_record_full('big', level='6', population='500000'),
            'small': make_auth_record_full('small', level='6', population='10000'),
        }
        result = rank_candidates(['big', 'small'], auth_cache, parent_level=8)
        assert result[0][0] == 'big'
        assert result[1][0] == 'small'

    def test_parent_level_none_sorts_by_pop_only(self):
        auth_cache = {
            'high_pop': make_auth_record_full('high_pop', level='4', population='900000'),
            'low_pop': make_auth_record_full('low_pop', level='6', population='100'),
        }
        result = rank_candidates(['high_pop', 'low_pop'], auth_cache, parent_level=None)
        assert result[0][0] == 'high_pop'

    def test_missing_level_treated_as_zero(self):
        rec_no_level = make_auth_record_full('no_level', level='', population='50000')
        auth_cache = {
            'no_level': rec_no_level,
            'normal': make_auth_record_full('normal', level='6', population='50000'),
        }
        result = rank_candidates(['no_level', 'normal'], auth_cache, parent_level=8)
        assert result[0][0] == 'normal'

    def test_empty_candidates_returns_empty(self):
        result = rank_candidates([], {}, parent_level=8)
        assert result == []

    def test_returns_score_tuples(self):
        auth_cache = {
            'aaa': make_auth_record_full('aaa', level='6', population='300000'),
        }
        result = rank_candidates(['aaa'], auth_cache, parent_level=8)
        uuid, score = result[0]
        assert uuid == 'aaa'
        assert score == (2, -300000)


class TestDetectTie:
    def test_empty_list_returns_no_winner(self):
        winner, tied = detect_tie([])
        assert winner is None
        assert tied == []

    def test_single_candidate_returns_winner(self):
        winner, tied = detect_tie([('aaa', (2, -300000))])
        assert winner == 'aaa'
        assert tied == []

    def test_different_scores_returns_winner(self):
        ranked = [('better', (2, -300000)), ('worse', (4, -900000))]
        winner, tied = detect_tie(ranked)
        assert winner == 'better'
        assert tied == []

    def test_identical_scores_returns_tie(self):
        ranked = [('a', (2, -300000)), ('b', (2, -300000))]
        winner, tied = detect_tie(ranked)
        assert winner is None
        assert set(tied) == {'a', 'b'}

    def test_three_candidates_two_tied_at_top(self):
        ranked = [('a', (2, -100)), ('b', (2, -100)), ('c', (4, -500))]
        winner, tied = detect_tie(ranked)
        assert winner is None
        assert set(tied) == {'a', 'b'}

    def test_three_candidates_all_tied(self):
        ranked = [('a', (2, -100)), ('b', (2, -100)), ('c', (2, -100))]
        winner, tied = detect_tie(ranked)
        assert winner is None
        assert set(tied) == {'a', 'b', 'c'}

    def test_same_gap_different_pop_not_tied(self):
        ranked = [('big', (2, -500000)), ('small', (2, -100))]
        winner, tied = detect_tie(ranked)
        assert winner == 'big'
        assert tied == []
