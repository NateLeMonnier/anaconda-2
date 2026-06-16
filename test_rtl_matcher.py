"""Tests for batch parent pre-fetch and resolve_parent_only in rtl_matcher."""
import pytest
from unittest.mock import MagicMock
from rtl_matcher import prefetch_parent_chains, resolve_parent_only, BATCH, detect_tie, match_entry, detect_jurisdiction_hint, parse_entries, resolve_helper_term


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


class TestRankCandidatesJurisdictionFilter:
    def test_city_preferred_over_township_no_hint(self):
        auth_cache = {
            'city': make_auth_record_full('city', level='4', population='80000',
                                          jurisdiction='City'),
            'twp': make_auth_record_full('twp', level='4', population='120000',
                                         jurisdiction='Township'),
        }
        result = rank_candidates(['city', 'twp'], auth_cache, parent_level=None,
                                 jurisdiction_hint=None)
        assert len(result) == 1
        assert result[0][0] == 'city'

    def test_township_kept_when_hint_is_township(self):
        auth_cache = {
            'city': make_auth_record_full('city', level='4', population='80000',
                                          jurisdiction='City'),
            'twp': make_auth_record_full('twp', level='4', population='120000',
                                         jurisdiction='Township'),
        }
        result = rank_candidates(['city', 'twp'], auth_cache, parent_level=None,
                                 jurisdiction_hint='Township')
        assert len(result) == 2
        assert result[0][0] == 'twp'

    def test_county_filtered_when_city_exists(self):
        auth_cache = {
            'city': make_auth_record_full('city', level='4', population='50000',
                                          jurisdiction='City'),
            'county': make_auth_record_full('county', level='5', population='200000',
                                            jurisdiction='County'),
        }
        result = rank_candidates(['city', 'county'], auth_cache, parent_level=None,
                                 jurisdiction_hint=None)
        assert len(result) == 1
        assert result[0][0] == 'city'

    def test_county_kept_when_hint_is_county(self):
        auth_cache = {
            'city': make_auth_record_full('city', level='4', population='50000',
                                          jurisdiction='City'),
            'county': make_auth_record_full('county', level='5', population='200000',
                                            jurisdiction='County'),
        }
        result = rank_candidates(['city', 'county'], auth_cache, parent_level=None,
                                 jurisdiction_hint='County')
        assert len(result) == 2
        assert result[0][0] == 'county'

    def test_no_preferred_candidates_keeps_all(self):
        auth_cache = {
            'twp_a': make_auth_record_full('twp_a', level='4', population='80000',
                                           jurisdiction='Township'),
            'twp_b': make_auth_record_full('twp_b', level='4', population='50000',
                                           jurisdiction='Township'),
        }
        result = rank_candidates(['twp_a', 'twp_b'], auth_cache, parent_level=None,
                                 jurisdiction_hint=None)
        assert len(result) == 2
        assert result[0][0] == 'twp_a'

    def test_borough_is_preferred(self):
        auth_cache = {
            'boro': make_auth_record_full('boro', level='4', population='30000',
                                          jurisdiction='Borough'),
            'twp': make_auth_record_full('twp', level='4', population='100000',
                                         jurisdiction='Township'),
        }
        result = rank_candidates(['boro', 'twp'], auth_cache, parent_level=None,
                                 jurisdiction_hint=None)
        assert len(result) == 1
        assert result[0][0] == 'boro'

    def test_village_is_preferred(self):
        auth_cache = {
            'village': make_auth_record_full('village', level='4', population='5000',
                                             jurisdiction='Village'),
            'county': make_auth_record_full('county', level='5', population='500000',
                                            jurisdiction='County'),
        }
        result = rank_candidates(['village', 'county'], auth_cache, parent_level=None,
                                 jurisdiction_hint=None)
        assert len(result) == 1
        assert result[0][0] == 'village'

    def test_filter_applies_with_parent_level_set(self):
        auth_cache = {
            'city': make_auth_record_full('city', level='4', population='50000',
                                          jurisdiction='City'),
            'twp': make_auth_record_full('twp', level='4', population='120000',
                                         jurisdiction='Township'),
        }
        result = rank_candidates(['city', 'twp'], auth_cache, parent_level=6,
                                 jurisdiction_hint=None)
        assert len(result) == 1
        assert result[0][0] == 'city'

    def test_unknown_jurisdiction_not_filtered(self):
        auth_cache = {
            'city': make_auth_record_full('city', level='4', population='50000',
                                          jurisdiction='City'),
            'suburb': make_auth_record_full('suburb', level='3', population='10000',
                                           jurisdiction='Suburb'),
        }
        result = rank_candidates(['city', 'suburb'], auth_cache, parent_level=None,
                                 jurisdiction_hint=None)
        assert len(result) == 2


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


def build_hierarchy_caches():
    """Build caches for: USA (level 8) -> FL-state (level 6) and FL-city (level 4, PR).
    Original input: "Mount Dora, Florida, United States of America"
    Mount Dora is not in name_cache, so it gets skipped.
    """
    auth_cache = {
        'usa-1': make_auth_record_full(
            'usa-1', level='8', name='United States of America',
            population='330000000'),
        'fl-state': make_auth_record_full(
            'fl-state', parent_uuid='usa-1', level='6', name='Florida',
            population='22000000'),
        'fl-city': make_auth_record_full(
            'fl-city', parent_uuid='pr-1', level='4', name='Florida',
            population='9000'),
        'pr-1': make_auth_record_full(
            'pr-1', parent_uuid='usa-1', level='7', name='Puerto Rico',
            population='3200000'),
    }
    name_cache = {
        'united states of america': {'usa-1'},
        'florida': {'fl-state', 'fl-city'},
    }
    return name_cache, auth_cache


def build_tied_hierarchy_caches():
    """Two Floridas at the same level and same population under USA."""
    auth_cache = {
        'usa-1': make_auth_record_full(
            'usa-1', level='8', name='United States of America',
            population='330000000'),
        'fl-a': make_auth_record_full(
            'fl-a', parent_uuid='usa-1', level='6', name='Florida',
            population='0'),
        'fl-b': make_auth_record_full(
            'fl-b', parent_uuid='usa-1', level='6', name='Florida',
            population='0'),
    }
    name_cache = {
        'united states of america': {'usa-1'},
        'florida': {'fl-a', 'fl-b'},
    }
    return name_cache, auth_cache


class TestMatchEntryTieDetection:
    def test_chain_verified_picks_better_level_gap(self):
        name_cache, auth_cache = build_hierarchy_caches()
        client = MagicMock()
        client.find.return_value = []
        terms = ['Mount Dora', 'Florida', 'United States of America']
        result = match_entry(terms, name_cache, auth_cache, client,
                             'Mount Dora, Florida, United States of America')
        assert result.match_type == 'chain_verified'
        assert result.candidate_ids == ['fl-state']
        assert result.tied_ids == []

    def test_chain_verified_tie_produces_chain_amb(self):
        name_cache, auth_cache = build_tied_hierarchy_caches()
        client = MagicMock()
        client.find.return_value = []
        terms = ['Florida', 'United States of America']
        result = match_entry(terms, name_cache, auth_cache, client,
                             'Florida, United States of America')
        assert result.match_type == 'chain_amb'
        assert result.candidate_ids == []
        assert set(result.tied_ids) == {'fl-a', 'fl-b'}

    def test_single_term_no_tie_returns_winner(self):
        auth_cache = {
            'big': make_auth_record_full('big', level='6', population='500000'),
            'small': make_auth_record_full('small', level='6', population='100'),
        }
        name_cache = {'florida': {'big', 'small'}}
        client = MagicMock()
        terms = ['Florida']
        result = match_entry(terms, name_cache, auth_cache, client, 'Florida')
        # Two candidates with no jurisdiction -> both survive filter -> single_amb
        assert result.match_type == 'single_amb'
        assert set(result.tied_ids) == {'big', 'small'}

    def test_single_term_tie_produces_single_amb(self):
        auth_cache = {
            'a': make_auth_record_full('a', level='6', population='0'),
            'b': make_auth_record_full('b', level='6', population='0'),
        }
        name_cache = {'florida': {'a', 'b'}}
        client = MagicMock()
        terms = ['Florida']
        result = match_entry(terms, name_cache, auth_cache, client, 'Florida')
        assert result.match_type == 'single_amb'
        assert result.candidate_ids == []
        assert set(result.tied_ids) == {'a', 'b'}

    def test_parent_only_unchanged(self):
        """parent_only results pass through to resolve_parent_only in main(),
        so match_entry should still return candidate_ids for it."""
        auth_cache = {
            'usa-1': make_auth_record_full(
                'usa-1', level='8', name='United States of America',
                population='330000000'),
        }
        name_cache = {
            'united states of america': {'usa-1'},
        }
        client = MagicMock()
        client.find.return_value = []
        terms = ['Springfield', 'United States of America']
        result = match_entry(terms, name_cache, auth_cache, client,
                             'Springfield, United States of America')
        assert result.match_type == 'parent_only'
        assert 'usa-1' in result.candidate_ids


class TestParseEntriesJurisdictionHints:
    def test_returns_jurisdiction_hints(self):
        entries = [{'place': 'Washington County, Pennsylvania', 'guid': 'g1', 'frequency': '5'}]
        parsed, all_terms, jurisdiction_hints = parse_entries(entries)
        assert jurisdiction_hints['washington county'] == 'County'

    def test_no_hint_for_plain_terms(self):
        entries = [{'place': 'Lawrence, Indiana', 'guid': 'g1', 'frequency': '5'}]
        parsed, all_terms, jurisdiction_hints = parse_entries(entries)
        assert 'lawrence' not in jurisdiction_hints
        assert 'indiana' not in jurisdiction_hints

    def test_multiple_hints(self):
        entries = [
            {'place': 'Bethel Township, Clark County, Ohio', 'guid': 'g1', 'frequency': '3'},
        ]
        parsed, all_terms, jurisdiction_hints = parse_entries(entries)
        assert jurisdiction_hints['bethel township'] == 'Township'
        assert jurisdiction_hints['clark county'] == 'County'


class TestDetectJurisdictionHint:
    def test_county_suffix(self):
        assert detect_jurisdiction_hint("Washington County") == "County"

    def test_township_suffix(self):
        assert detect_jurisdiction_hint("Lawrence Township") == "Township"

    def test_twp_abbreviation(self):
        assert detect_jurisdiction_hint("Lawrence Twp") == "Township"
        assert detect_jurisdiction_hint("Lawrence Twp.") == "Township"

    def test_parish_suffix(self):
        assert detect_jurisdiction_hint("Orleans Parish") == "Parish"

    def test_borough_suffix(self):
        assert detect_jurisdiction_hint("Huntingdon Borough") == "Borough"

    def test_co_abbreviation(self):
        assert detect_jurisdiction_hint("Mifflin Co") == "County"
        assert detect_jurisdiction_hint("Mifflin Co.") == "County"

    def test_no_jurisdiction(self):
        assert detect_jurisdiction_hint("Lawrence") is None

    def test_case_insensitive(self):
        assert detect_jurisdiction_hint("washington county") == "County"
        assert detect_jurisdiction_hint("LAWRENCE TOWNSHIP") == "Township"

    def test_city_name_containing_county_word(self):
        assert detect_jurisdiction_hint("County Line") is None


class TestSingleTermReclassification:
    def test_single_candidate_after_filter_is_single_term(self):
        """One city, one township -> filter keeps city only -> single_term."""
        auth_cache = {
            'city': make_auth_record_full('city', level='4', population='80000',
                                          jurisdiction='City'),
            'twp': make_auth_record_full('twp', level='4', population='120000',
                                         jurisdiction='Township'),
        }
        name_cache = {'lawrence': {'city', 'twp'}}
        client = MagicMock()
        result = match_entry(['Lawrence'], name_cache, auth_cache, client, 'Lawrence')
        assert result.match_type == 'single_term'
        assert result.candidate_ids == ['city']

    def test_multiple_candidates_after_filter_is_single_amb(self):
        """Two cities survive filter -> single_amb even with different populations."""
        auth_cache = {
            'city_a': make_auth_record_full('city_a', level='4', population='80000',
                                            jurisdiction='City'),
            'city_b': make_auth_record_full('city_b', level='4', population='50000',
                                            jurisdiction='City'),
        }
        name_cache = {'lawrence': {'city_a', 'city_b'}}
        client = MagicMock()
        result = match_entry(['Lawrence'], name_cache, auth_cache, client, 'Lawrence')
        assert result.match_type == 'single_amb'
        assert set(result.tied_ids) == {'city_a', 'city_b'}

    def test_single_candidate_total_is_single_term(self):
        """Only one candidate in the pool -> single_term, no filter needed."""
        auth_cache = {
            'only': make_auth_record_full('only', level='4', population='5000',
                                          jurisdiction='City'),
        }
        name_cache = {'wapakoneta': {'only'}}
        client = MagicMock()
        result = match_entry(['Wapakoneta'], name_cache, auth_cache, client, 'Wapakoneta')
        assert result.match_type == 'single_term'
        assert result.candidate_ids == ['only']

    def test_multiple_townships_no_city_is_single_amb(self):
        """Two townships, no city -> filter keeps both -> single_amb."""
        auth_cache = {
            'twp_a': make_auth_record_full('twp_a', level='4', population='80000',
                                           jurisdiction='Township'),
            'twp_b': make_auth_record_full('twp_b', level='4', population='50000',
                                           jurisdiction='Township'),
        }
        name_cache = {'pine': {'twp_a', 'twp_b'}}
        client = MagicMock()
        result = match_entry(['Pine'], name_cache, auth_cache, client, 'Pine')
        assert result.match_type == 'single_amb'
        assert set(result.tied_ids) == {'twp_a', 'twp_b'}


class TestMatchEntryJurisdictionHint:
    def test_township_hint_preserves_township_candidates(self):
        """Input 'Lawrence Township' -> hint='Township' -> keep all including townships."""
        auth_cache = {
            'city': make_auth_record_full('city', level='4', population='80000',
                                          jurisdiction='City'),
            'twp': make_auth_record_full('twp', level='4', population='120000',
                                         jurisdiction='Township'),
        }
        name_cache = {'lawrence township': {'city', 'twp'}}
        jurisdiction_hints = {'lawrence township': 'Township'}
        client = MagicMock()
        result = match_entry(['Lawrence Township'], name_cache, auth_cache, client,
                             'Lawrence Township', jurisdiction_hints=jurisdiction_hints)
        # Both kept because hint suppresses filter; >1 candidate -> single_amb
        assert result.match_type == 'single_amb'
        assert 'twp' in result.tied_ids
        assert 'city' in result.tied_ids

    def test_no_hint_filters_township(self):
        """Input 'Lawrence' -> no hint -> township filtered out."""
        auth_cache = {
            'city': make_auth_record_full('city', level='4', population='80000',
                                          jurisdiction='City'),
            'twp': make_auth_record_full('twp', level='4', population='120000',
                                         jurisdiction='Township'),
        }
        name_cache = {'lawrence': {'city', 'twp'}}
        jurisdiction_hints = {}
        client = MagicMock()
        result = match_entry(['Lawrence'], name_cache, auth_cache, client,
                             'Lawrence', jurisdiction_hints=jurisdiction_hints)
        assert result.match_type == 'single_term'
        assert result.candidate_ids == ['city']

    def test_county_hint_in_multi_term_preserves_counties(self):
        """Multi-term: 'Clark County, Ohio' -> county hint on leftmost term -> counties kept."""
        auth_cache = {
            'ohio': make_auth_record_full('ohio', level='6', name='Ohio',
                                          population='11800000', jurisdiction='State'),
            'clark_county': make_auth_record_full('clark_county', level='5',
                                                   name='Clark', population='130000',
                                                   jurisdiction='County',
                                                   parent_uuid='ohio'),
            'clark_city': make_auth_record_full('clark_city', level='4',
                                                name='Clark', population='5000',
                                                jurisdiction='City',
                                                parent_uuid='ohio'),
        }
        name_cache = {
            'ohio': {'ohio'},
            'clark county': {'clark_county', 'clark_city'},
        }
        jurisdiction_hints = {'clark county': 'County'}
        client = MagicMock()
        client.find.return_value = []
        result = match_entry(['Clark County', 'Ohio'], name_cache, auth_cache, client,
                             'Clark County, Ohio', jurisdiction_hints=jurisdiction_hints)
        assert result.match_type == 'chain_verified'
        assert result.candidate_ids == ['clark_county']


class TestResolveHelperTerm:
    def test_resolves_single_match(self):
        utah_rec = make_auth_record_full('utah-uuid', level='6', name='Utah',
                                         parent_uuid='usa-uuid', jurisdiction='State')
        usa_rec = make_auth_record_full('usa-uuid', level='8', name='United States',
                                        jurisdiction='Country')
        client = MagicMock()
        # First call: Authority_Place query for "Utah"
        # Second call: parent chain fetch for usa-uuid
        client.find.side_effect = [
            make_fm_response([utah_rec]),
            make_fm_response([usa_rec]),
        ]
        auth_cache = {}
        result = resolve_helper_term('Utah', client, auth_cache)
        assert result is not None
        assert result['uuid'] == 'utah-uuid'
        assert result['level'] == 6
        assert 'usa-uuid' in result['ancestor_uuids']

    def test_returns_none_for_empty_string(self):
        client = MagicMock()
        result = resolve_helper_term('', client, {})
        assert result is None
        client.find.assert_not_called()

    def test_returns_none_for_none(self):
        client = MagicMock()
        result = resolve_helper_term(None, client, {})
        assert result is None
        client.find.assert_not_called()
