"""Tests for the RTL matcher."""
import os
import tempfile
from collections import defaultdict

import pytest
from symspellpy import SymSpell, Verbosity

import rtl_matcher
from rtl_matcher import (
    _absorb_bare_jurisdiction,
    BARE_JURISDICTION_WORDS,
    build_ascii_index,
    build_cli,
    build_level_provenance,
    build_result_row,
    build_spelling_index,
    canonicalize_place,
    cap_candidates,
    CommalessSegmenter,
    COUNTRY_ABBREVIATIONS,
    deepest_supported_ancestor,
    detect_jurisdiction_hint,
    detect_tie,
    _disambiguate_by_frequency,
    has_encoding_corruption,
    haversine_km,
    _is_bare_jurisdiction,
    is_description,
    is_supported_level,
    LocalData,
    lookup_name,
    lookup_name_with_origin,
    match_entry,
    MatchResult,
    MAX_ARRAY,
    NameCache,
    parse_entries,
    prefetch_parent_chains_local,
    query_cardinal_strip_local,
    query_preposition_extractions_local,
    query_spelling_corrections_local,
    rank_candidates,
    record_level,
    resolution_kind,
    resolve_helper_term_local,
    _resolve_output_paths,
    resolve_parent_match,
    resolve_parent_only,
    SEGMENT_LOG_FIELDS,
    source_shape_tags,
    span_for,
    _strip_bare_jurisdiction,
    write_segment_log,
    write_spelling_log,
)


def make_auth_record(uuid, parent_uuid=None, name="Place"):
    return {
        'UUID': uuid,
        'Parent_UUID': parent_uuid or '',
        'Auth_Place_Name': name,
    }


@pytest.fixture
def local_pa():
    """Install a throwaway LocalData as the module-global _LOCAL.

    The lookup functions read the PA and MNT indexes off _LOCAL rather than
    taking them as arguments, so a test that drives one has to supply the
    indexes this way. Yields a loader: call it with authority records (the
    dicts make_auth_record / make_auth_record_full produce) to populate
    pa_by_uuid and pa_by_name.
    """
    original = rtl_matcher._LOCAL
    data = LocalData()
    data.mnt_by_raw = defaultdict(set)
    data.mnt_by_value = defaultdict(set)
    data.pa_by_name = defaultdict(list)
    data.pa_by_uuid = {}
    data.dict_freq = {}
    rtl_matcher._LOCAL = data

    def load(*records):
        for rec in records:
            data.pa_by_uuid[rec['UUID']] = rec
            name = rec.get('Auth_Place_Name')
            if name:
                data.pa_by_name[name.lower()].append(rec)
        return data

    yield load
    rtl_matcher._LOCAL = original


class TestPrefetchParentChains:
    def test_no_parents_to_fetch(self, local_pa):
        local_pa()
        auth_cache = {'aaa': make_auth_record('aaa')}
        prefetch_parent_chains_local(auth_cache)
        assert list(auth_cache) == ['aaa']

    def test_fetches_missing_parent(self, local_pa):
        local_pa(make_auth_record('parent', name='ParentPlace'))
        auth_cache = {
            'child': make_auth_record('child', parent_uuid='parent'),
        }

        prefetch_parent_chains_local(auth_cache)

        assert 'parent' in auth_cache
        assert auth_cache['parent']['Auth_Place_Name'] == 'ParentPlace'

    def test_walks_multiple_levels(self, local_pa):
        local_pa(
            make_auth_record('county', parent_uuid='state', name='County'),
            make_auth_record('state', parent_uuid='country', name='State'),
            make_auth_record('country', name='Country'),
        )
        auth_cache = {
            'city': make_auth_record('city', parent_uuid='county'),
        }

        prefetch_parent_chains_local(auth_cache)

        assert 'county' in auth_cache
        assert 'state' in auth_cache
        assert 'country' in auth_cache

    def test_skips_already_cached_parents(self, local_pa):
        # The PA holds a differently-named copy; an already-cached parent must
        # not be re-read over the top of what the caller put there.
        local_pa(make_auth_record('parent', name='FromPA'))
        auth_cache = {
            'child': make_auth_record('child', parent_uuid='parent'),
            'parent': make_auth_record('parent', name='AlreadyCached'),
        }

        prefetch_parent_chains_local(auth_cache)

        assert auth_cache['parent']['Auth_Place_Name'] == 'AlreadyCached'

    def test_handles_parent_absent_from_authority(self, local_pa):
        local_pa()
        auth_cache = {
            'child': make_auth_record('child', parent_uuid='missing'),
        }

        prefetch_parent_chains_local(auth_cache)

        assert 'missing' not in auth_cache

    def test_terminates_when_no_new_parents(self, local_pa):
        local_pa(make_auth_record('b'))
        auth_cache = {'a': make_auth_record('a', parent_uuid='b')}

        prefetch_parent_chains_local(auth_cache)

        assert set(auth_cache) == {'a', 'b'}

    def test_deduplicates_parent_uuids_across_records(self, local_pa):
        local_pa(make_auth_record('shared_parent'))
        auth_cache = {
            'child1': make_auth_record('child1', parent_uuid='shared_parent'),
            'child2': make_auth_record('child2', parent_uuid='shared_parent'),
        }

        prefetch_parent_chains_local(auth_cache)

        assert set(auth_cache) == {'child1', 'child2', 'shared_parent'}

    def test_resolves_large_sets(self, local_pa):
        n = 2050
        parents = [make_auth_record(f'parent_{i}') for i in range(n)]
        local_pa(*parents)
        auth_cache = {
            f'child_{i}': make_auth_record(f'child_{i}', parent_uuid=f'parent_{i}')
            for i in range(n)
        }

        prefetch_parent_chains_local(auth_cache)

        assert len(auth_cache) == 2 * n
        assert all(f'parent_{i}' in auth_cache for i in range(n))


def make_auth_record_full(uuid, parent_uuid=None, name="Place", level="4",
                          population="", jurisdiction="", latitude="", longitude="",
                          historical=""):
    return {
        'UUID': uuid,
        'Parent_UUID': parent_uuid or '',
        'Auth_Place_Name': name,
        'Level': level,
        'Population': population,
        'Jurisdiction': jurisdiction,
        'Type_Ahead_Value': '',
        'Historical': historical,
        'Latitude': latitude,
        'Longitude': longitude,
    }


class TestResolveParentOnly:
    def test_single_candidate_returns_it(self):
        uid = 'state-001'
        auth_cache = {uid: make_auth_record_full(uid, level='6', name='New York')}
        result = resolve_parent_only([uid], auth_cache)
        assert result == (uid, 'parent_resolved')

    def test_multi_candidate_population_no_longer_resolves(self):
        # Big population candidate must NOT win by population anymore.
        big = 'city-big'
        small = 'city-small'
        auth_cache = {
            big: make_auth_record_full(big, level='4', population='675000'),
            small: make_auth_record_full(small, level='4', population='2000'),
        }
        result = resolve_parent_only([big, small], auth_cache)
        assert result == (None, 'amb')

    def test_multi_candidate_cross_level_is_amb(self):
        # The "South" -> "South Africa" bug: country vs villages -> amb.
        country = 'za-1'
        village = 'v-1'
        auth_cache = {
            country: make_auth_record_full(country, level='8', population='60000000'),
            village: make_auth_record_full(village, level='4', population='0'),
        }
        result = resolve_parent_only([country, village], auth_cache)
        assert result == (None, 'amb')

    def test_multi_candidate_pop_over_50k_still_amb(self):
        # Even the "pop >= 50k, rest zero" case no longer resolves.
        big_city = 'city-big'
        small_city = 'city-small'
        auth_cache = {
            big_city: make_auth_record_full(big_city, level='4', population='75000'),
            small_city: make_auth_record_full(small_city, level='4', population='0'),
        }
        result = resolve_parent_only([big_city, small_city], auth_cache)
        assert result == (None, 'amb')


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
        # 026bc81 prepended a correction-provenance axis, so the tuple is
        # (correction_miss, helper_miss, level_gap, -population).
        assert score == (0, 0, 2, -300000)


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

    def test_correction_city_never_filters_out_exact_county(self):
        """"Sheboygan County": the exact County match must survive a
        spelling-correction City ("Cheboygan") that the jurisdiction
        pre-filter would otherwise let delete it. The correction stays in
        the array, ranked below the exact match."""
        auth_cache = {
            'exact_county': make_auth_record_full('exact_county', level='5',
                                                  population='0',
                                                  jurisdiction='County'),
            'corr_city': make_auth_record_full('corr_city', level='4',
                                               population='4686',
                                               jurisdiction='City'),
            'corr_county': make_auth_record_full('corr_county', level='5',
                                                 population='0',
                                                 jurisdiction='County'),
        }
        result = rank_candidates(['exact_county', 'corr_city', 'corr_county'],
                                 auth_cache, parent_level=None,
                                 jurisdiction_hint=None,
                                 correction_uuids={'corr_city', 'corr_county'})
        # corr_county is pruned by the City in its own tier; exact_county is not.
        assert [uuid for uuid, _ in result] == ['exact_county', 'corr_city']

    def test_historical_exact_shares_the_weak_tier(self):
        """A Historical exact match scores is_weak=1, same as a correction."""
        auth_cache = {
            'hist_exact': make_auth_record_full('hist_exact', level='4',
                                                population='0',
                                                jurisdiction='Township',
                                                historical='True'),
            'live_corr': make_auth_record_full('live_corr', level='4',
                                               population='2381',
                                               jurisdiction='Town'),
        }
        result = rank_candidates(['hist_exact', 'live_corr'], auth_cache,
                                 parent_level=None, jurisdiction_hint='Township',
                                 correction_uuids={'live_corr'})
        assert {uuid: s[0] for uuid, s in result} == {'hist_exact': 1,
                                                      'live_corr': 1}

    def test_exact_city_still_prunes_exact_county(self):
        """The prune is unchanged inside a single tier: with no corrections
        involved, a City still deletes a County."""
        auth_cache = {
            'city': make_auth_record_full('city', level='4', population='50000',
                                          jurisdiction='City'),
            'county': make_auth_record_full('county', level='5', population='200000',
                                            jurisdiction='County'),
        }
        result = rank_candidates(['city', 'county'], auth_cache, parent_level=None,
                                 jurisdiction_hint=None,
                                 correction_uuids={'unrelated'})
        assert [uuid for uuid, _ in result] == ['city']

    def test_corrections_still_rank_when_no_exact_match(self):
        auth_cache = {
            'corr_city': make_auth_record_full('corr_city', level='4',
                                               population='4686',
                                               jurisdiction='City'),
            'corr_county': make_auth_record_full('corr_county', level='5',
                                                 population='0',
                                                 jurisdiction='County'),
        }
        result = rank_candidates(['corr_city', 'corr_county'], auth_cache,
                                 parent_level=None, jurisdiction_hint='County',
                                 correction_uuids={'corr_city', 'corr_county'})
        assert len(result) == 2

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

    # Scores are the full four-axis tuple rank_candidates emits:
    # (is_correction, helper_miss, level_gap, neg_population).
    def test_single_candidate_returns_winner(self):
        winner, tied = detect_tie([('aaa', (0, 0, 2, -300000))])
        assert winner == 'aaa'
        assert tied == []

    def test_different_scores_returns_winner(self):
        ranked = [('better', (0, 0, 2, -300000)), ('worse', (0, 0, 4, -900000))]
        winner, tied = detect_tie(ranked)
        assert winner == 'better'
        assert tied == []

    def test_identical_scores_returns_tie(self):
        ranked = [('a', (0, 0, 2, -300000)), ('b', (0, 0, 2, -300000))]
        winner, tied = detect_tie(ranked)
        assert winner is None
        assert set(tied) == {'a', 'b'}

    def test_three_candidates_two_tied_at_top(self):
        ranked = [('a', (0, 0, 2, -100)), ('b', (0, 0, 2, -100)),
                  ('c', (0, 0, 4, -500))]
        winner, tied = detect_tie(ranked)
        assert winner is None
        assert set(tied) == {'a', 'b'}

    def test_three_candidates_all_tied(self):
        ranked = [('a', (0, 0, 2, -100)), ('b', (0, 0, 2, -100)),
                  ('c', (0, 0, 2, -100))]
        winner, tied = detect_tie(ranked)
        assert winner is None
        assert set(tied) == {'a', 'b', 'c'}

    def test_same_gap_different_pop_is_tie(self):
        # Population differs but the deciding axes match -> tie (population must
        # not break the tie into a single winner).
        ranked = [('big', (0, 0, 2, -500000)), ('small', (0, 0, 2, -100))]
        winner, tied = detect_tie(ranked)
        assert winner is None
        assert tied == ['big', 'small']

    def test_different_level_gap_still_resolves(self):
        # Structural separation (level_gap) still produces a single winner.
        ranked = [('better', (0, 0, 1, -100)), ('worse', (0, 0, 3, -900000))]
        winner, tied = detect_tie(ranked)
        assert winner == 'better'
        assert tied == []

    def test_historical_exact_ties_with_correction(self):
        """"Johnson, S.C.": rank_candidates puts a Historical exact match in
        the same weak tier as the correction, so detect_tie surfaces both
        instead of resolving to the defunct township."""
        ranked = [('exact_historical', (1, 0, 0, 0)),
                  ('correction_live', (1, 0, 0, -2381))]
        winner, tied = detect_tie(ranked)
        assert winner is None
        assert tied == ['exact_historical', 'correction_live']

    def test_live_exact_still_beats_correction(self):
        """"Pepin, Wis.": a live exact match is tier 0, the correction tier 1,
        so the row still resolves."""
        ranked = [('exact_live', (0, 0, 0, -781)),
                  ('correction', (1, 0, 0, -1200))]
        winner, tied = detect_tie(ranked)
        assert winner == 'exact_live'
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
        terms = ['Mount Dora', 'Florida', 'United States of America']
        result = match_entry(terms, name_cache, auth_cache,
                             'Mount Dora, Florida, United States of America')
        assert result.match_type == 'chain_verified'
        assert result.candidate_ids == ['fl-state']
        assert result.tied_ids == []

    def test_chain_verified_tie_produces_chain_amb(self):
        name_cache, auth_cache = build_tied_hierarchy_caches()
        terms = ['Florida', 'United States of America']
        result = match_entry(terms, name_cache, auth_cache,
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
        terms = ['Florida']
        result = match_entry(terms, name_cache, auth_cache, 'Florida')
        # Two candidates with no jurisdiction -> both survive filter -> single_amb
        assert result.match_type == 'single_amb'
        assert set(result.tied_ids) == {'big', 'small'}

    def test_single_term_tie_produces_single_amb(self):
        auth_cache = {
            'a': make_auth_record_full('a', level='6', population='0'),
            'b': make_auth_record_full('b', level='6', population='0'),
        }
        name_cache = {'florida': {'a', 'b'}}
        terms = ['Florida']
        result = match_entry(terms, name_cache, auth_cache, 'Florida')
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
        terms = ['Springfield', 'United States of America']
        result = match_entry(terms, name_cache, auth_cache,
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
        result = match_entry(['Lawrence'], name_cache, auth_cache, 'Lawrence')
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
        result = match_entry(['Lawrence'], name_cache, auth_cache, 'Lawrence')
        assert result.match_type == 'single_amb'
        assert set(result.tied_ids) == {'city_a', 'city_b'}

    def test_single_candidate_total_is_single_term(self):
        """Only one candidate in the pool -> single_term, no filter needed."""
        auth_cache = {
            'only': make_auth_record_full('only', level='4', population='5000',
                                          jurisdiction='City'),
        }
        name_cache = {'wapakoneta': {'only'}}
        result = match_entry(['Wapakoneta'], name_cache, auth_cache, 'Wapakoneta')
        assert result.match_type == 'single_term'
        assert result.candidate_ids == ['only']

    def test_lone_exact_above_corrections_is_single_term(self):
        """"Chiago": an MNT-curated exact mapping to Chicago, plus two
        edit-distance-1 neighbours. The exact match separates structurally,
        so the row resolves instead of going ambiguous."""
        auth_cache = {
            'chicago': make_auth_record_full('chicago', level='4',
                                             population='2665039',
                                             jurisdiction='City'),
            'chicago_twp': make_auth_record_full('chicago_twp', level='4',
                                                 population='44',
                                                 jurisdiction='Township'),
            'chisago_co': make_auth_record_full('chisago_co', level='5',
                                                population='0',
                                                jurisdiction='County'),
        }
        name_cache = {'chiago': {'chicago', 'chicago_twp', 'chisago_co'}}
        result = match_entry(['Chiago'], name_cache, auth_cache, 'Chiago',
                             correction_uuids_by_term={
                                 'chiago': {'chicago_twp', 'chisago_co'}})
        assert result.match_type == 'single_term'
        assert result.candidate_ids == ['chicago']

    def test_lone_historical_exact_ties_with_correction(self):
        """Same shape, but the exact match is Historical -> weak tier, so it
        no longer separates and the row surfaces both readings."""
        auth_cache = {
            'hist_exact': make_auth_record_full('hist_exact', level='4',
                                                population='0',
                                                jurisdiction='Township',
                                                historical='True'),
            'live_corr': make_auth_record_full('live_corr', level='4',
                                               population='2381',
                                               jurisdiction='Town'),
        }
        name_cache = {'johnson': {'hist_exact', 'live_corr'}}
        result = match_entry(['Johnson'], name_cache, auth_cache, 'Johnson',
                             correction_uuids_by_term={'johnson': {'live_corr'}})
        assert result.match_type == 'single_amb'
        assert set(result.tied_ids) == {'hist_exact', 'live_corr'}

    def test_multiple_townships_no_city_is_single_amb(self):
        """Two townships, no city -> filter keeps both -> single_amb."""
        auth_cache = {
            'twp_a': make_auth_record_full('twp_a', level='4', population='80000',
                                           jurisdiction='Township'),
            'twp_b': make_auth_record_full('twp_b', level='4', population='50000',
                                           jurisdiction='Township'),
        }
        name_cache = {'pine': {'twp_a', 'twp_b'}}
        result = match_entry(['Pine'], name_cache, auth_cache, 'Pine')
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
        result = match_entry(['Lawrence Township'], name_cache, auth_cache,
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
        result = match_entry(['Lawrence'], name_cache, auth_cache,
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
        result = match_entry(['Clark County', 'Ohio'], name_cache, auth_cache,
                             'Clark County, Ohio', jurisdiction_hints=jurisdiction_hints)
        assert result.match_type == 'chain_verified'
        assert result.candidate_ids == ['clark_county']


class TestResolveHelperTerm:
    def test_resolves_single_match(self, local_pa):
        local_pa(
            make_auth_record_full('utah-uuid', level='6', name='Utah',
                                  parent_uuid='usa-uuid', jurisdiction='State'),
            make_auth_record_full('usa-uuid', level='8', name='United States',
                                  jurisdiction='Country'),
        )
        auth_cache = {}
        result = resolve_helper_term_local('Utah', auth_cache)
        assert result is not None
        assert result['uuid'] == 'utah-uuid'
        assert result['level'] == 6
        assert 'usa-uuid' in result['ancestor_uuids']

    def test_multi_term_walks_the_chain(self, local_pa):
        """'Utah, USA' anchors on USA and keeps only the Utah that chains to
        it, not the same-named record under another country."""
        local_pa(
            make_auth_record_full('usa-uuid', level='8', name='USA',
                                  jurisdiction='Country'),
            make_auth_record_full('utah-us', level='6', name='Utah',
                                  parent_uuid='usa-uuid', jurisdiction='State'),
            make_auth_record_full('utah-elsewhere', level='6', name='Utah',
                                  parent_uuid='other-country',
                                  jurisdiction='State', population='999999'),
            make_auth_record_full('other-country', level='8', name='Elsewhere'),
        )
        auth_cache = {}
        result = resolve_helper_term_local('Utah, USA', auth_cache)
        assert result is not None
        assert result['uuid'] == 'utah-us'

    def test_returns_none_for_empty_string(self, local_pa):
        local_pa()
        assert resolve_helper_term_local('', {}) is None

    def test_returns_none_for_none(self, local_pa):
        local_pa()
        assert resolve_helper_term_local(None, {}) is None

    def test_returns_none_when_authority_has_no_such_place(self, local_pa):
        local_pa(make_auth_record_full('somewhere', name='Somewhere'))
        assert resolve_helper_term_local('Atlantis', {}) is None


class TestHelperTermBoost:
    def test_state_helper_boosts_matching_candidate(self):
        """Helper='Utah' (L6) -> candidate in Utah ranks above higher-pop candidate elsewhere."""
        auth_cache = {
            'logan_ut': make_auth_record_full('logan_ut', level='4', population='50000',
                                              jurisdiction='City', parent_uuid='cache_co'),
            'cache_co': make_auth_record_full('cache_co', level='5', name='Cache',
                                              parent_uuid='utah'),
            'utah': make_auth_record_full('utah', level='6', name='Utah',
                                          parent_uuid='usa'),
            'usa': make_auth_record_full('usa', level='8', name='USA'),
            'logan_wv': make_auth_record_full('logan_wv', level='4', population='80000',
                                              jurisdiction='City', parent_uuid='logan_co'),
            'logan_co': make_auth_record_full('logan_co', level='5', name='Logan',
                                              parent_uuid='wv'),
            'wv': make_auth_record_full('wv', level='6', name='West Virginia',
                                        parent_uuid='usa'),
        }
        helper_term = {'uuid': 'utah', 'level': 6, 'ancestor_uuids': {'usa'}}
        result = rank_candidates(
            ['logan_ut', 'logan_wv'], auth_cache, parent_level=None,
            jurisdiction_hint=None, helper_term=helper_term)
        assert result[0][0] == 'logan_ut'

    def test_country_helper_weaker_than_state(self):
        """Helper='USA' (L8) -> US candidate ranks above non-US."""
        auth_cache = {
            'clarinda_us': make_auth_record_full('clarinda_us', level='4', population='5000',
                                                 jurisdiction='City', parent_uuid='page_co'),
            'page_co': make_auth_record_full('page_co', level='5', name='Page',
                                             parent_uuid='iowa'),
            'iowa': make_auth_record_full('iowa', level='6', name='Iowa',
                                          parent_uuid='usa'),
            'usa': make_auth_record_full('usa', level='8', name='USA'),
            'clarinda_au': make_auth_record_full('clarinda_au', level='4', population='200000',
                                                 jurisdiction='City', parent_uuid='kingston'),
            'kingston': make_auth_record_full('kingston', level='5', name='Kingston',
                                              parent_uuid='victoria'),
            'victoria': make_auth_record_full('victoria', level='6', name='Victoria',
                                              parent_uuid='australia'),
            'australia': make_auth_record_full('australia', level='8', name='Australia'),
        }
        helper_term = {'uuid': 'usa', 'level': 8, 'ancestor_uuids': set()}
        result = rank_candidates(
            ['clarinda_us', 'clarinda_au'], auth_cache, parent_level=None,
            jurisdiction_hint=None, helper_term=helper_term)
        assert result[0][0] == 'clarinda_us'

    def test_no_helper_no_boost(self):
        """Without helper term, higher population wins as before."""
        auth_cache = {
            'clarinda_us': make_auth_record_full('clarinda_us', level='4', population='5000',
                                                 jurisdiction='City'),
            'clarinda_au': make_auth_record_full('clarinda_au', level='4', population='200000',
                                                 jurisdiction='City'),
        }
        result = rank_candidates(
            ['clarinda_us', 'clarinda_au'], auth_cache, parent_level=None,
            jurisdiction_hint=None, helper_term=None)
        assert result[0][0] == 'clarinda_au'

    def test_helper_no_match_no_effect(self):
        """Helper='Utah' but all candidates are in Netherlands -> no boost, pop wins."""
        auth_cache = {
            'eindhoven_a': make_auth_record_full('eindhoven_a', level='4', population='230000',
                                                 jurisdiction='City', parent_uuid='nb'),
            'nb': make_auth_record_full('nb', level='6', name='Noord-Brabant',
                                        parent_uuid='nl'),
            'nl': make_auth_record_full('nl', level='8', name='Netherlands'),
            'eindhoven_b': make_auth_record_full('eindhoven_b', level='4', population='5000',
                                                 jurisdiction='City', parent_uuid='nb'),
        }
        helper_term = {'uuid': 'utah-uuid', 'level': 6, 'ancestor_uuids': {'usa-uuid'}}
        result = rank_candidates(
            ['eindhoven_a', 'eindhoven_b'], auth_cache, parent_level=None,
            jurisdiction_hint=None, helper_term=helper_term)
        assert result[0][0] == 'eindhoven_a'


# ---------------------------------------------------------------------------
# Phase 1d: Spelling correction — build_spelling_index
# ---------------------------------------------------------------------------


class TestBuildSpellingIndex:
    def _write_tsv(self, tmp_dir, rows):
        """Write a minimal PA-format TSV and return its path."""
        path = os.path.join(tmp_dir, "pa_test.tsv")
        with open(path, 'w') as f:
            f.write("Level\tLevelName\tReplacement_UUID\tTerm\tID\tHistorical\tFullChainName\tParentID\tPopulation\tLatitude\tLongitude\n")
            for row in rows:
                f.write(row + "\n")
        return path

    def test_loads_terms_from_tsv(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_tsv(tmp, [
                "1\tCountry\t\tBirmingham\tUUID1\t\tBirmingham\tP1\t1000000\t0\t0",
                "1\tCountry\t\tCalifornia\tUUID2\t\tCalifornia\tP2\t39000000\t0\t0",
            ])
            sym = build_spelling_index(path)
            # Callers are expected to ascii_fold input before querying
            result = sym.lookup("birminghan", Verbosity.CLOSEST, max_edit_distance=1)
            assert len(result) >= 1
            assert result[0].term == "birmingham"

    def test_ascii_folds_terms(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_tsv(tmp, [
                "1\tCountry\t\tMéxico\tUUID1\t\tMéxico\tP1\t0\t0\t0",
            ])
            sym = build_spelling_index(path)
            result = sym.lookup("mexco", Verbosity.CLOSEST, max_edit_distance=1)
            assert len(result) >= 1
            assert result[0].term == "mexico"

    def test_deduplicates_folded_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_tsv(tmp, [
                "1\tCountry\t\tMéxico\tUUID1\t\tMéxico\tP1\t0\t0\t0",
                "1\tCountry\t\tMexico\tUUID2\t\tMexico\tP2\t0\t0\t0",
            ])
            sym = build_spelling_index(path)
            assert sym.words.get("mexico") is not None

    def test_handles_multi_word_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_tsv(tmp, [
                "1\tCountry\t\tNew York\tUUID1\t\tNew York\tP1\t8000000\t0\t0",
            ])
            sym = build_spelling_index(path)
            # Callers are expected to ascii_fold input before querying
            result = sym.lookup("new yrok", Verbosity.CLOSEST, max_edit_distance=1)
            assert len(result) >= 1
            assert result[0].term == "new york"


# ---------------------------------------------------------------------------
# Phase 1d: query_spelling_corrections and write_spelling_log tests
# ---------------------------------------------------------------------------


class TestQuerySpellingCorrections:
    def _make_sym(self, terms):
        """Build a SymSpell index from a list of canonical terms."""
        sym = SymSpell(max_dictionary_edit_distance=1, prefix_length=7)
        for t in terms:
            sym.create_dictionary_entry(t.lower(), 1)
        return sym

    def test_corrects_misspelling_and_adds_to_name_cache(self, local_pa):
        local_pa(make_auth_record('uuid-birm', name='Birmingham'))
        sym = self._make_sym(["birmingham"])
        name_cache = defaultdict(set)

        added, corrections = query_spelling_corrections_local(
            ["Birminghan"], name_cache, sym
        )

        assert added >= 1
        assert 'uuid-birm' in name_cache['birminghan']
        assert len(corrections) == 1
        # Corrections are keyed by the lowercased term used throughout the
        # pipeline, so the log records the lowercase form.
        assert corrections[0]['original_term'] == 'birminghan'
        assert corrections[0]['corrected_term'] == 'birmingham'

    def test_skips_short_terms(self, local_pa):
        local_pa(make_auth_record('uuid-lima', name='Lima'))
        sym = self._make_sym(["lima", "lira"])
        name_cache = defaultdict(set)

        added, corrections = query_spelling_corrections_local(
            ["Lira"], name_cache, sym
        )

        assert added == 0
        assert len(corrections) == 0
        assert 'lira' not in name_cache

    def test_skips_terms_already_in_name_cache(self, local_pa):
        local_pa(make_auth_record('uuid-birm', name='Birmingham'))
        sym = self._make_sym(["birmingham"])
        name_cache = defaultdict(set)
        name_cache['birmingham'].add('existing-uuid')

        added, corrections = query_spelling_corrections_local(
            ["Birmingham"], name_cache, sym
        )

        # The term spells correctly, so SymSpell offers nothing but itself and
        # the existing mapping stands untouched.
        assert added == 0
        assert name_cache['birmingham'] == {'existing-uuid'}

    def test_discards_correction_that_does_not_resolve(self, local_pa):
        # SymSpell knows 'birmingham' but the authority has no record for it,
        # so the correction must not reach name_cache or the log.
        local_pa()
        sym = self._make_sym(["birmingham"])
        name_cache = defaultdict(set)

        added, corrections = query_spelling_corrections_local(
            ["Birminghan"], name_cache, sym
        )

        assert added == 0
        assert 'birminghan' not in name_cache
        assert len(corrections) == 0

    def test_accepts_multiple_suggestions(self, local_pa):
        local_pa(
            make_auth_record('uuid-1', name='Springfield'),
            make_auth_record('uuid-2', name='Springfild'),
        )
        sym = self._make_sym(["springfield", "springfild"])
        name_cache = defaultdict(set)

        query_spelling_corrections_local(["Springfeld"], name_cache, sym)

        assert 'uuid-1' in name_cache['springfeld'] or 'uuid-2' in name_cache['springfeld']


class TestWriteSpellingLog:
    def test_writes_tsv(self):
        corrections = [
            {'original_term': 'Birminghan', 'corrected_term': 'birmingham',
             'edit_distance': 1, 'authority_uuid': 'uuid-1'},
        ]
        with tempfile.NamedTemporaryFile(mode='w', suffix='.tsv', delete=False) as f:
            path = f.name
        write_spelling_log(corrections, path)

        with open(path) as f:
            lines = f.readlines()
        assert len(lines) == 2  # header + 1 row
        assert 'Birminghan' in lines[1]
        assert 'birmingham' in lines[1]
        os.unlink(path)


class TestMntTransformEnrichment:
    def test_transformable_mnt_matched_term_gets_enriched(self, local_pa):
        """'Town of Bristol' has an MNT entry (Bristol, England) but transform_term
        strips 'Town of' and looks up Auth_Place_Name='Bristol' + Jurisdiction='Town',
        which should find Bristol, Rhode Island. After enrichment, name_cache should
        contain BOTH UUIDs."""
        from rtl_matcher import transform_term, query_fallback_transforms_local

        mnt_uuid = 'bristol-england-uuid'
        transform_uuid = 'bristol-ri-uuid'
        local_pa(make_auth_record_full(transform_uuid, name='Bristol',
                                       jurisdiction='Town', level='4'))
        name_cache = defaultdict(set)
        name_cache['town of bristol'].add(mnt_uuid)

        all_terms = ['Town of Bristol']
        transformable_matched = [
            t for t in all_terms
            if name_cache.get(t.lower()) and transform_term(t)[0] is not None
        ]
        query_fallback_transforms_local(transformable_matched, name_cache,
                                        transform_term_fn=transform_term)

        assert mnt_uuid in name_cache['town of bristol']
        assert transform_uuid in name_cache['town of bristol']

    def test_non_transformable_mnt_matched_term_skipped(self):
        """'Rhode Island' has an MNT entry and transform_term returns (None, None).
        It should not be passed to query_fallback_transforms_local."""
        from rtl_matcher import transform_term

        name_cache = defaultdict(set)
        name_cache['rhode island'].add('ri-uuid')

        all_terms = ['Rhode Island']
        transformable_matched = [
            t for t in all_terms
            if name_cache.get(t.lower()) and transform_term(t)[0] is not None
        ]

        assert transformable_matched == []

    def test_enrichment_is_additive(self, local_pa):
        """Transform results must not replace existing MNT entries."""
        from rtl_matcher import transform_term, query_fallback_transforms_local

        mnt_uuid = 'existing-mnt-uuid'
        local_pa(make_auth_record_full('springfield-city-uuid', name='Springfield',
                                       jurisdiction='City', level='4'))
        name_cache = defaultdict(set)
        name_cache['city of springfield'].add(mnt_uuid)

        all_terms = ['City of Springfield']
        transformable_matched = [
            t for t in all_terms
            if name_cache.get(t.lower()) and transform_term(t)[0] is not None
        ]
        query_fallback_transforms_local(transformable_matched, name_cache,
                                        transform_term_fn=transform_term)

        assert mnt_uuid in name_cache['city of springfield']
        assert 'springfield-city-uuid' in name_cache['city of springfield']

    def test_filter_collects_correct_terms(self):
        """The filter for Phase 1c enrichment should include terms that:
        1. Have name_cache entries (MNT-matched)
        2. Are transformable (transform_term returns non-None)
        And exclude terms that:
        - Have no name_cache entries (already handled by unmatched path)
        - Are not transformable (no prefix/suffix to strip)"""
        from rtl_matcher import transform_term

        name_cache = defaultdict(set)
        name_cache['town of bristol'].add('some-uuid')     # transformable + matched
        name_cache['rhode island'].add('ri-uuid')           # not transformable + matched
        # 'Springfield' not in name_cache                   # transformable but unmatched

        all_terms = ['Town of Bristol', 'Rhode Island', 'Springfield']

        transformable_matched = [
            t for t in all_terms
            if name_cache.get(t.lower()) and transform_term(t)[0] is not None
        ]

        assert transformable_matched == ['Town of Bristol']


class TestLatLongInAuthCache:
    def test_auth_record_includes_lat_long(self):
        """PA records loaded via local data should include Latitude and Longitude."""
        from rtl_matcher import _PA_FIELD_MAP
        assert 'Latitude' in _PA_FIELD_MAP.values()
        assert 'Longitude' in _PA_FIELD_MAP.values()


class TestHaversineKm:
    def test_same_point_returns_zero(self):
        assert haversine_km(41.0, -94.0, 41.0, -94.0) == 0.0

    def test_known_distance(self):
        # Adams County, Iowa (41.0652, -94.6864) to Union County, Iowa (41.0007, -94.2744)
        dist = haversine_km(41.0652, -94.6864, 41.0007, -94.2744)
        assert 30 < dist < 40  # ~34km apart

    def test_missing_lat_returns_inf(self):
        assert haversine_km(None, -94.0, 41.0, -94.0) == float('inf')

    def test_empty_string_returns_inf(self):
        assert haversine_km('', -94.0, 41.0, -94.0) == float('inf')

    def test_unparseable_returns_inf(self):
        assert haversine_km('abc', -94.0, 41.0, -94.0) == float('inf')

    def test_distant_points(self):
        # New York (40.7128, -74.0060) to Los Angeles (34.0522, -118.2437)
        dist = haversine_km(40.7128, -74.0060, 34.0522, -118.2437)
        assert 3900 < dist < 4000


class TestProximityFallback:
    def _build_cromwell_caches(self):
        """Cromwell, Adams County, Iowa — Cromwell is actually in Union County.
        Adams and Union counties are adjacent (~34km apart)."""
        auth_cache = {
            'usa': make_auth_record_full('usa', level='8', name='United States',
                                         population='330000000'),
            'iowa': make_auth_record_full('iowa', parent_uuid='usa', level='6',
                                          name='Iowa', population='3200000'),
            'adams-co': make_auth_record_full('adams-co', parent_uuid='iowa', level='5',
                                              name='Adams', population='3700',
                                              jurisdiction='County',
                                              latitude='41.0652', longitude='-94.6864'),
            'union-co': make_auth_record_full('union-co', parent_uuid='iowa', level='5',
                                              name='Union', population='12200',
                                              jurisdiction='County',
                                              latitude='41.0007', longitude='-94.2744'),
            'cromwell': make_auth_record_full('cromwell', parent_uuid='union-co', level='4',
                                              name='Cromwell', population='108',
                                              jurisdiction='City',
                                              latitude='41.0394', longitude='-94.4619'),
        }
        name_cache = {
            'iowa': {'iowa'},
            'adams county': {'adams-co'},
            'cromwell': {'cromwell'},
        }
        return name_cache, auth_cache

    def test_cromwell_matches_via_proximity(self):
        name_cache, auth_cache = self._build_cromwell_caches()
        terms = ['Cromwell', 'Adams County', 'Iowa']
        result = match_entry(terms, name_cache, auth_cache,
                             'Cromwell, Adams County, Iowa',
                             jurisdiction_hints={'adams county': 'County'})
        assert result.match_type == 'chain_verified_proximity'
        assert result.candidate_ids == ['cromwell']
        assert result.depth == 3
        assert 'proximity' in result.skipped_terms.lower()

    def test_no_proximity_when_too_far(self):
        """City in same state but distant county — should NOT match via proximity."""
        auth_cache = {
            'usa': make_auth_record_full('usa', level='8', name='United States',
                                         population='330000000'),
            'iowa': make_auth_record_full('iowa', parent_uuid='usa', level='6',
                                          name='Iowa', population='3200000'),
            'adams-co': make_auth_record_full('adams-co', parent_uuid='iowa', level='5',
                                              name='Adams', population='3700',
                                              jurisdiction='County',
                                              latitude='41.0652', longitude='-94.6864'),
            'dubuque-co': make_auth_record_full('dubuque-co', parent_uuid='iowa', level='5',
                                                name='Dubuque', population='98000',
                                                jurisdiction='County',
                                                latitude='42.4700', longitude='-90.7100'),
            'faraway-city': make_auth_record_full('faraway-city', parent_uuid='dubuque-co',
                                                   level='4', name='Farville',
                                                   population='500', jurisdiction='City',
                                                   latitude='42.5', longitude='-90.7'),
        }
        name_cache = {
            'iowa': {'iowa'},
            'adams county': {'adams-co'},
            'farville': {'faraway-city'},
        }
        terms = ['Farville', 'Adams County', 'Iowa']
        result = match_entry(terms, name_cache, auth_cache,
                             'Farville, Adams County, Iowa',
                             jurisdiction_hints={'adams county': 'County'})
        # Should NOT be proximity match — too far away
        assert result.match_type != 'chain_verified_proximity'

    def test_no_proximity_when_depth_less_than_2(self):
        """Only one term confirmed (country only) — no proximity fallback."""
        auth_cache = {
            'usa': make_auth_record_full('usa', level='8', name='United States',
                                         population='330000000'),
            'some-co': make_auth_record_full('some-co', parent_uuid='usa', level='5',
                                             name='Some', jurisdiction='County',
                                             latitude='40.0', longitude='-90.0'),
            'city-x': make_auth_record_full('city-x', parent_uuid='some-co', level='4',
                                             name='CityX', jurisdiction='City',
                                             latitude='40.1', longitude='-90.1'),
        }
        name_cache = {
            'united states': {'usa'},
            'cityx': {'city-x'},
        }
        terms = ['CityX', 'United States']
        result = match_entry(terms, name_cache, auth_cache,
                             'CityX, United States')
        assert result.match_type != 'chain_verified_proximity'

    def test_proximity_picks_most_specific_skipped_term(self):
        """Two skipped terms with candidates — proximity should match the most specific one."""
        auth_cache = {
            'usa': make_auth_record_full('usa', level='8', name='United States',
                                         population='330000000'),
            'iowa': make_auth_record_full('iowa', parent_uuid='usa', level='6',
                                          name='Iowa', population='3200000'),
            'adams-co': make_auth_record_full('adams-co', parent_uuid='iowa', level='5',
                                              name='Adams', population='3700',
                                              jurisdiction='County',
                                              latitude='41.0652', longitude='-94.6864'),
            'union-co': make_auth_record_full('union-co', parent_uuid='iowa', level='5',
                                              name='Union', population='12200',
                                              jurisdiction='County',
                                              latitude='41.0007', longitude='-94.2744'),
            'cromwell': make_auth_record_full('cromwell', parent_uuid='union-co', level='4',
                                              name='Cromwell', population='108',
                                              jurisdiction='City',
                                              latitude='41.0394', longitude='-94.4619'),
            'neighborhood': make_auth_record_full('neighborhood', parent_uuid='union-co',
                                                   level='3', name='OldTown',
                                                   population='50', jurisdiction='Village',
                                                   latitude='41.04', longitude='-94.46'),
        }
        name_cache = {
            'iowa': {'iowa'},
            'adams county': {'adams-co'},
            'cromwell': {'cromwell'},
            'oldtown': {'neighborhood'},
        }
        terms = ['OldTown', 'Cromwell', 'Adams County', 'Iowa']
        result = match_entry(terms, name_cache, auth_cache,
                             'OldTown, Cromwell, Adams County, Iowa',
                             jurisdiction_hints={'adams county': 'County'})
        # Cromwell should match via proximity; OldTown may chain-verify against Cromwell
        # or also match via proximity — either way, most specific wins
        assert result.match_type in ('chain_verified_proximity', 'chain_verified')
        assert result.depth >= 3

    def test_proximity_multiple_candidates_disambiguates(self):
        """Two cities with same name in different counties near the confirmed county.
        Both within 50km — should use rank_candidates + detect_tie."""
        auth_cache = {
            'usa': make_auth_record_full('usa', level='8', name='United States',
                                         population='330000000'),
            'iowa': make_auth_record_full('iowa', parent_uuid='usa', level='6',
                                          name='Iowa', population='3200000'),
            'adams-co': make_auth_record_full('adams-co', parent_uuid='iowa', level='5',
                                              name='Adams', population='3700',
                                              jurisdiction='County',
                                              latitude='41.0652', longitude='-94.6864'),
            'union-co': make_auth_record_full('union-co', parent_uuid='iowa', level='5',
                                              name='Union', population='12200',
                                              jurisdiction='County',
                                              latitude='41.0007', longitude='-94.2744'),
            'taylor-co': make_auth_record_full('taylor-co', parent_uuid='iowa', level='5',
                                               name='Taylor', population='6000',
                                               jurisdiction='County',
                                               latitude='40.7400', longitude='-94.6900'),
            'springfield-1': make_auth_record_full('springfield-1', parent_uuid='union-co',
                                                    level='4', name='Springfield',
                                                    population='200', jurisdiction='City',
                                                    latitude='41.0', longitude='-94.3'),
            'springfield-2': make_auth_record_full('springfield-2', parent_uuid='taylor-co',
                                                    level='4', name='Springfield',
                                                    population='200', jurisdiction='City',
                                                    latitude='40.75', longitude='-94.7'),
        }
        name_cache = {
            'iowa': {'iowa'},
            'adams county': {'adams-co'},
            'springfield': {'springfield-1', 'springfield-2'},
        }
        terms = ['Springfield', 'Adams County', 'Iowa']
        result = match_entry(terms, name_cache, auth_cache,
                             'Springfield, Adams County, Iowa',
                             jurisdiction_hints={'adams county': 'County'})
        # Both within 50km, same pop — should be ambiguous
        assert result.match_type == 'chain_amb'


# ---------------------------------------------------------------------------
# Dict-union reintegration tests
# ---------------------------------------------------------------------------


def _write_tsv(path, header, rows):
    with open(path, 'w', encoding='utf-8') as f:
        f.write('\t'.join(header) + '\n')
        for r in rows:
            f.write('\t'.join(r) + '\n')


U1 = '11111111-1111-1111-1111-111111111111'
U2 = '22222222-2222-2222-2222-222222222222'


class TestCanonicalizePlace:
    def test_lowercases_and_normalizes_separators(self):
        assert canonicalize_place("Danville,VA ,  United States") == "danville, va, united states"

    def test_semicolons_treated_like_commas(self):
        assert canonicalize_place("Boston; Mass") == "boston, mass"

    def test_single_segment_passthrough(self):
        assert canonicalize_place("  Hesse ") == "hesse"


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

    def test_dict_tsv_loads_illegible_stoplist(self, tmp_path):
        mnt = str(tmp_path / 'mnt.tsv')
        _write_tsv(mnt, ['_value', '_ID', '_raw', '_geoclass'],
                   [['Hesse', U1, 'hessen', 'Global']])
        d = tmp_path / 'dictdir'
        d.mkdir()
        _write_tsv(str(d / 'place_term_dictionary.tsv'),
                   ['term', 'authority_uuid', 'level', 'jurisdiction', 'frequency'],
                   [['hesse', U2, '2', 'Germany', '40']])
        _write_tsv(str(d / 'place_term_illegible.tsv'),
                   ['term', 'frequency'],
                   [['Uk Known', '3'], ['a?', '1']])
        ld = LocalData()
        ld._load_mnt(mnt)
        ld._load_dict_tsv(str(d))
        assert ld.illegible == {'uk known', 'a?'}


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


class TestSkippedHadCandidates:
    """match_entry must flag whether any skipped (dropped) term had authority
    candidates that failed chain verification -- the 'recoverable data' signal."""

    def test_flag_false_when_skipped_term_has_no_candidates(self):
        """'Bad String, USA': Bad String is absent from name_cache, so it is
        skipped with no candidates -> skipped_had_candidates is False."""
        auth_cache = {
            'usa-1': make_auth_record_full(
                'usa-1', level='8', name='United States of America',
                population='330000000'),
        }
        name_cache = {'united states of america': {'usa-1'}}
        result = match_entry(['Bad String', 'United States of America'],
                             name_cache, auth_cache,
                             'Bad String, United States of America')
        assert result.match_type == 'parent_only'
        assert result.skipped_had_candidates is False

    def test_flag_true_when_skipped_term_has_unchained_candidate(self):
        """Springfield is in name_cache but its only candidate does not chain
        up to USA -> skipped with a candidate -> skipped_had_candidates True."""
        auth_cache = {
            'usa-1': make_auth_record_full(
                'usa-1', level='8', name='United States of America',
                population='330000000'),
            'spr-ca': make_auth_record_full(
                'spr-ca', parent_uuid='canada-1', level='4',
                name='Springfield', population='5000'),
        }
        name_cache = {
            'united states of america': {'usa-1'},
            'springfield': {'spr-ca'},
        }
        result = match_entry(['Springfield', 'United States of America'],
                             name_cache, auth_cache,
                             'Springfield, United States of America')
        assert result.match_type == 'parent_only'
        assert result.skipped_had_candidates is True

    def test_parent_only_honors_jurisdiction_hint(self):
        """'Court House, Sheboygan County': the anchor carries a County hint,
        so the City/County jurisdiction pre-filter must not fire and delete
        the county the hint describes."""
        auth_cache = {
            'sheb-county': make_auth_record_full(
                'sheb-county', parent_uuid='wi', level='5',
                name='Sheboygan', population='0', jurisdiction='County'),
            'cheb-city': make_auth_record_full(
                'cheb-city', parent_uuid='mi', level='4',
                name='Cheboygan', population='4686', jurisdiction='City'),
            'courthouse-sc': make_auth_record_full(
                'courthouse-sc', parent_uuid='sc', level='4',
                name='Court House', population='0', jurisdiction='Township'),
        }
        name_cache = {
            'sheboygan county': {'sheb-county', 'cheb-city'},
            'court house': {'courthouse-sc'},
        }
        result = match_entry(['Court House', 'Sheboygan County'],
                             name_cache, auth_cache,
                             'Court House, Sheboygan County',
                             jurisdiction_hints={'sheboygan county': 'County'})
        assert result.match_type == 'parent_only'
        assert 'sheb-county' in result.candidate_ids


class TestResolveParentMatch:
    """The parent_only post-processing decision: resolve vs reject vs amb."""

    def _parent_only(self, candidate_ids, had_candidates):
        return MatchResult(
            candidate_ids=list(candidate_ids),
            depth=1,
            match_type='parent_only',
            skipped_count=1,
            skipped_terms='Bad String',
            skipped_had_candidates=had_candidates,
        )

    def test_resolves_when_no_recoverable_data(self):
        """No recoverable specific -> parent stands as parent_resolved,
        skipped terms preserved."""
        auth_cache = {'tx': make_auth_record_full(
            'tx', level='6', name='Texas', population='29000000')}
        match = self._parent_only(['tx'], had_candidates=False)
        result = resolve_parent_match(match, ['Bad String', 'Texas'],
                                      auth_cache)
        assert result.match_type == 'parent_resolved'
        assert result.candidate_ids == ['tx']
        assert result.skipped_terms == 'Bad String'

    def test_rejects_when_recoverable_data_present(self):
        """A recoverable specific that failed to chain -> parent_rejected, low
        confidence, but the best-guess parent is carried, not discarded."""
        auth_cache = {'tx': make_auth_record_full(
            'tx', level='6', name='Texas', population='29000000')}
        match = self._parent_only(['tx'], had_candidates=True)
        result = resolve_parent_match(match, ['Bad String', 'Texas'],
                                      auth_cache)
        assert result.match_type == 'parent_rejected'
        assert result.candidate_ids == ['tx']
        assert result.confidence == 'low'
        assert result.skipped_terms == 'Bad String'

    def test_ambiguous_parent_produces_parent_amb_with_tied_ids(self):
        """Two zero-population parent candidates cannot be disambiguated ->
        parent_amb, empty candidates, tie set exposed in tied_ids."""
        auth_cache = {
            'fl-a': make_auth_record_full('fl-a', level='6', population='0'),
            'fl-b': make_auth_record_full('fl-b', level='6', population='0'),
        }
        match = self._parent_only(['fl-a', 'fl-b'], had_candidates=False)
        result = resolve_parent_match(match, ['Bad String', 'Florida'],
                                      auth_cache)
        assert result.match_type == 'parent_amb'
        assert result.candidate_ids == []
        assert result.confidence == 'low'
        assert set(result.tied_ids) == {'fl-a', 'fl-b'}

    def test_parent_amb_array_capped(self):
        """A large ambiguous parent set is capped at MAX_ARRAY in tied_ids."""
        ids = [f'c-{i}' for i in range(MAX_ARRAY + 4)]
        auth_cache = {i: make_auth_record_full(i, level='4', population='0')
                      for i in ids}
        match = self._parent_only(ids, had_candidates=False)
        result = resolve_parent_match(match, ['South'], auth_cache)
        assert result.match_type == 'parent_amb'
        assert result.confidence == 'low'
        assert len(result.tied_ids) == MAX_ARRAY


class TestBuildResultRow:
    def test_single_answer_row(self):
        auth_cache = {'a': make_auth_record_full('a', level='4', name='Alexandria',
                                                 jurisdiction='City')}
        match = MatchResult(candidate_ids=['a'], depth=2, match_type='chain_verified')
        row = build_result_row(match, 'x, Alexandria', 'g1', '3', auth_cache)
        assert row['authority_id'] == 'a'
        assert row['authority_name'] == 'Alexandria'
        assert row['candidate_ids'] == 'a'
        assert row['candidate_names'] == 'Alexandria'
        assert row['candidates'] == 1
        assert row['confidence'] == 'high'

    def test_amb_array_inlined(self):
        # candidate_names uses the full type-ahead path so same-name candidates
        # are distinguishable; missing type-ahead falls back to the place name.
        rec_a = make_auth_record_full('a', level='4', name='Beverly')
        rec_a['Type_Ahead_Value'] = 'Beverly, Essex, Massachusetts, United States'
        rec_b = make_auth_record_full('b', level='4', name='Beverley')
        rec_b['Type_Ahead_Value'] = 'Beverley, East Riding of Yorkshire, England'
        rec_c = make_auth_record_full('c', level='4', name='Beverly')  # no type-ahead
        auth_cache = {'a': rec_a, 'b': rec_b, 'c': rec_c}
        match = MatchResult(candidate_ids=[], depth=1, match_type='parent_amb',
                            tied_ids=['a', 'b', 'c'])
        row = build_result_row(match, '3 Phillips street, Beverly', 'g2', '1', auth_cache)
        # ffddab7 blanks authority_* on non-resolutions so an unresolved row
        # never reads as an answer; the ranked array is what survives.
        assert row['authority_id'] == ''
        assert row['authority_name'] == ''
        assert row['matched_uuid'] == 'a'          # best guess, kept separately
        assert row['candidate_ids'] == 'a|b|c'
        assert row['candidate_names'] == (
            'Beverly, Essex, Massachusetts, United States|'
            'Beverley, East Riding of Yorkshire, England|'
            'Beverly')                             # c falls back to place name
        assert row['candidates'] == 3
        assert row['confidence'] == 'low'

    def test_parent_rejected_hides_authority_keeps_candidates(self):
        # parent_rejected must not look like a match: authority_* blank, but the
        # candidate columns still carry the context.
        rec = make_auth_record_full('tx', level='6', name='Texas',
                                    jurisdiction='State')
        rec['Type_Ahead_Value'] = 'Texas, United States'
        auth_cache = {'tx': rec}
        match = MatchResult(candidate_ids=['tx'], depth=1,
                            match_type='parent_rejected',
                            skipped_count=1, skipped_terms='Bad String')
        row = build_result_row(match, 'Bad String, Texas', 'g4', '1', auth_cache)
        assert row['authority_id'] == ''
        assert row['authority_name'] == ''
        assert row['type_ahead'] == ''
        assert row['jurisdiction'] == ''
        assert row['level'] == ''
        assert row['candidate_ids'] == 'tx'
        assert row['candidate_names'] == 'Texas, United States'
        assert row['candidates'] == 1
        assert row['confidence'] == 'low'

    def test_no_candidates_row(self):
        match = MatchResult(candidate_ids=[], depth=0, match_type='no_auth_match')
        row = build_result_row(match, 'nowhere', 'g3', '', {})
        assert row['authority_id'] == ''
        assert row['candidate_ids'] == ''
        assert row['candidate_names'] == ''
        assert row['candidates'] == 0


class TestSouthRegression:
    def test_generic_parent_with_garbage_siblings_is_parent_amb(self):
        # "Garbage, garbage, South": only "south" matches authority; it pulls a
        # country and a village. Must NOT resolve to the country by population.
        country = 'za-1'
        village = 'v-1'
        auth_cache = {
            country: make_auth_record_full(country, level='8', name='South Africa',
                                           population='60000000'),
            village: make_auth_record_full(village, level='4', name='South',
                                           population='500'),
        }
        name_cache = {'south': {country, village}}
        match = match_entry(['Garbage', 'garbage', 'South'], name_cache,
                            auth_cache, 'Garbage, garbage, South')
        # rightmost "south" anchors parent_only with two candidates
        assert match.match_type == 'parent_only'
        resolved = resolve_parent_match(match, ['Garbage', 'garbage', 'South'],
                                        auth_cache)
        assert resolved.match_type == 'parent_amb'
        assert resolved.confidence == 'low'
        assert set(resolved.tied_ids) == {country, village}


class TestMatchEntryArrayCap:
    def test_single_amb_capped(self):
        # MAX_ARRAY + 3 same-level candidates for one term -> single_amb, capped.
        n = MAX_ARRAY + 3
        ids = [f'city-{i}' for i in range(n)]
        auth_cache = {i: make_auth_record_full(i, level='4', population=str(1000 * (n - k)))
                      for k, i in enumerate(ids)}
        name_cache = {'springfield': set(ids)}
        result = match_entry(['Springfield'], name_cache, auth_cache,
                             'Springfield')
        assert result.match_type == 'single_amb'
        assert result.confidence == 'low'
        assert len(result.tied_ids) == MAX_ARRAY


class TestConfidenceTier:
    def test_high_types(self):
        for mt in ('mnt_full_string', 'chain_verified', 'single_term', 'parent_resolved'):
            assert MatchResult(match_type=mt).confidence == 'high'

    def test_medium_types(self):
        for mt in ('freq_resolved', 'chain_verified_proximity'):
            assert MatchResult(match_type=mt).confidence == 'medium'

    def test_low_types(self):
        for mt in ('single_amb', 'chain_amb', 'parent_amb', 'parent_rejected'):
            assert MatchResult(match_type=mt).confidence == 'low'

    def test_unknown_type_is_none(self):
        assert MatchResult(match_type='no_auth_match').confidence == 'none'
        assert MatchResult().confidence == 'none'  # default match_type is 'no_terms'


class TestCapCandidates:
    def test_under_cap_unchanged(self):
        ids = ['a', 'b', 'c']
        assert cap_candidates(ids) == ['a', 'b', 'c']

    def test_at_cap_unchanged(self):
        ids = [str(i) for i in range(MAX_ARRAY)]
        assert cap_candidates(ids) == ids

    def test_over_cap_truncates_preserving_order(self):
        ids = [str(i) for i in range(MAX_ARRAY + 3)]
        result = cap_candidates(ids, "test")
        assert result == ids[:MAX_ARRAY]
        assert len(result) == MAX_ARRAY

    def test_empty(self):
        assert cap_candidates([]) == []


U3 = '33333333-3333-3333-3333-333333333333'
U4 = '44444444-4444-4444-4444-444444444444'
U5 = '55555555-5555-5555-5555-555555555555'


def _segmenter(tier1=None, tier2=None, levels=None, aliases=None):
    """Build a CommalessSegmenter over literal indexes.

    tier1  {name: uuid} canonical names, i.e. PA Term / MNT _value
    tier2  {name: uuid} raw MNT input strings
    levels {uuid: level string}
    """
    levels = levels or {}
    pa_by_name = defaultdict(list)
    for name, uuid in (tier1 or {}).items():
        pa_by_name[name.lower()].append({'UUID': uuid})
    pa_by_uuid = {uuid: {'Level': str(level)} for uuid, level in levels.items()}
    mnt_by_raw = defaultdict(set)
    for name, uuid in (tier2 or {}).items():
        mnt_by_raw[name.lower()].add(uuid)
    return CommalessSegmenter(
        pa_by_name=pa_by_name, pa_by_uuid=pa_by_uuid,
        mnt_by_raw=mnt_by_raw, mnt_by_value=defaultdict(set),
        country_aliases=aliases if aliases is not None else COUNTRY_ABBREVIATIONS)


class TestBareJurisdictionWords:
    def test_derived_from_jurisdiction_tables(self):
        for word in ('county', 'township', 'parish', 'borough', 'town',
                     'city', 'district', 'village', 'state', 'province'):
            assert word in BARE_JURISDICTION_WORDS

    def test_does_not_contain_real_place_names(self):
        for word in ('york', 'hampton', 'bend', 'macon', 'georgia', 'madison'):
            assert word not in BARE_JURISDICTION_WORDS

    def test_is_bare_jurisdiction(self):
        assert _is_bare_jurisdiction('City')
        assert _is_bare_jurisdiction('Twp.')
        assert not _is_bare_jurisdiction('Hampton City')
        assert not _is_bare_jurisdiction('')

    def test_strip_trailing_only(self):
        assert _strip_bare_jurisdiction('Hampton City') == 'Hampton'
        assert _strip_bare_jurisdiction('WEST BEND TOWN') == 'WEST BEND'
        assert _strip_bare_jurisdiction('Hampton') is None

    def test_does_not_strip_leading(self):
        # Stripping the leading word would make every "<descriptor> <place>"
        # span resolve, so the walk would prefer "County Iowa" over "Iowa".
        assert _strip_bare_jurisdiction('County Iowa') is None

    def test_absorb_merges_left(self):
        assert _absorb_bare_jurisdiction([('Hampton', 1), ('City', 0)]) == \
            [('Hampton City', 1)]

    def test_absorb_leading_segment_merges_right(self):
        assert _absorb_bare_jurisdiction([('Township', 0), ('Macon', 1)]) == \
            [('Township Macon', 1)]


class TestIsDescription:
    """The toponym-ness gate: does this span name a place or describe one?"""

    def test_determiner_plus_appellative_is_a_description(self):
        assert is_description('the village', 'Lutheran church in the village')
        assert is_description('the city', 'north east section of the city')

    def test_bare_appellative_is_a_description(self):
        assert is_description('station', 'on car floor near station')
        assert is_description('City', '626 Michigan Street, City')
        assert is_description('city', '335 State St., city')

    def test_two_appellatives_are_a_real_name(self):
        # "Grove City" and "Lake Village" are places. Requiring exactly one
        # word after the determiner is what keeps them resolvable.
        assert not is_description('Grove City', 'Grove City')
        assert not is_description('Lake Village', 'Lake Village')

    def test_appellative_qualified_by_a_proper_name_is_a_real_name(self):
        assert not is_description('Camden Place',
                                  'near the great log jam north of Camden Place')
        assert not is_description('Wakarusa township',
                                  'residence of the brides parents in Wakarusa township')

    def test_uncapitalized_span_in_a_mixed_case_original_is_a_description(self):
        assert is_description('lenoir', 'Route 2, lenoir')

    def test_capitalized_span_passes_the_case_test(self):
        assert not is_description('South Vineland', 'cottage in South Vineland')
        assert not is_description('DaCosta', 'home of his parents in DaCosta')

    def test_case_test_stands_down_on_all_lowercase_originals(self):
        # No capitalization signal exists, so only the list test may fire.
        assert not is_description('despatch', 'near despatch')

    def test_case_test_stands_down_on_all_caps_originals(self):
        assert not is_description('BOYERTOWN', 'BOYER TOWN R. D. 2')

    def test_non_alphabetic_lead_skips_the_case_test(self):
        assert not is_description('1st Ward Detroit', 'Smith home, 1st Ward Detroit')

    def test_empty_span_is_not_a_description(self):
        assert not is_description('', 'anything')
        assert not is_description('   ', 'anything')

    def test_negative_cases_from_span_reconstruction_failures(self):
        """Spans that a wrong reconstruction produced during design. Each is a
        correct match and must survive. Guards against regressing to the anchor
        or to the first left-to-right preposition."""
        for span, original in [
            ('Dispatch', 'near Despatch'),
            ('Port Deposit', 'near Port De posit'),
            ('Rhinelander', 'near Rhineland er'),
            ('Cole Camp', 'Brauerville church, south of Cole Camp'),
            ('Kansas', 'farm home, east central Kansas'),
            ('Bozeman', 'Chapel of the Presbyterian Church in Bozeman'),
            ('Bergton', 'Crab Run Church of the Brethren in Bergton'),
            ('Polson', 'home of her daughter west of Polson'),
            ('Jugenheim', 'Castle of Heiligenberg, near Jugenheim'),
        ]:
            assert not is_description(span, original), f'{span!r} in {original!r}'


class TestCommalessOracle:
    def test_tier1_canonical_name(self):
        seg = _segmenter(tier1={'Utah': U1}, levels={U1: 6})
        assert seg.tier('Utah') == 1
        assert seg.max_level('Utah') == 6

    def test_tier2_raw_input_string(self):
        seg = _segmenter(tier2={'fort madison': U1}, levels={U1: 4})
        assert seg.tier('Fort Madison') == 2

    def test_unknown_span(self):
        seg = _segmenter(tier1={'Utah': U1})
        assert seg.tier('Nowheresville') is None
        assert seg.max_level('Nowheresville') == 0

    def test_comma_bearing_key_never_matches_a_span(self):
        # A comma-bearing MNT raw is a whole multi-term input string. Admitting
        # it would merge "Illinois US" into one span.
        seg = _segmenter(tier2={'illinois, us': U1})
        assert seg.tier('Illinois US') is None
        assert seg.tier('Illinois, US') is None

    def test_transform_term_fallback_strips_county(self):
        seg = _segmenter(tier1={'Clark': U1}, levels={U1: 5})
        assert seg.tier('Clark County') == 1

    def test_jurisdiction_strip_fallback(self):
        seg = _segmenter(tier1={'Hampton': U1}, levels={U1: 4})
        assert seg.tier('Hampton City') == 1

    def test_country_alias_resolves_via_pa_name(self):
        seg = _segmenter(tier1={'USA': U1}, levels={U1: 8})
        assert seg.tier('US') == 1
        assert seg.max_level('US') == 8

    def test_ascii_fold_variant(self):
        seg = _segmenter(tier1={'Mexico': U1}, levels={U1: 8})
        assert seg.tier('México') == 1

    def test_max_level_takes_the_broadest_reading(self):
        # "England" is both a level-4 city and a level-8 country.
        seg = _segmenter(levels={U1: 4, U2: 8})
        seg.pa_by_name['england'] = [{'UUID': U1}, {'UUID': U2}]
        assert seg.max_level('England') == 8

    def test_indexes_are_not_copied(self):
        pa_by_name = defaultdict(list)
        pa_by_uuid, mnt_by_raw, mnt_by_value = {}, defaultdict(set), defaultdict(set)
        seg = CommalessSegmenter(pa_by_name=pa_by_name, pa_by_uuid=pa_by_uuid,
                                 mnt_by_raw=mnt_by_raw, mnt_by_value=mnt_by_value)
        assert seg.pa_by_name is pa_by_name
        assert seg.pa_by_uuid is pa_by_uuid
        assert seg.mnt_by_raw is mnt_by_raw
        assert seg.mnt_by_value is mnt_by_value


class TestCommalessSegmentation:
    def test_city_state(self):
        seg = _segmenter(tier1={'Swift': U1, 'Minnesota': U2},
                         levels={U1: 4, U2: 6})
        assert seg('Swift Minnesota') == ['Swift', 'Minnesota']

    def test_city_state_country(self):
        seg = _segmenter(tier1={'Sydney': U1, 'Nova Scotia': U2, 'Canada': U3},
                         levels={U1: 4, U2: 6, U3: 8})
        assert seg('Sydney Nova Scotia Canada') == ['Sydney', 'Nova Scotia', 'Canada']

    def test_longest_tier1_span_wins(self):
        seg = _segmenter(tier1={'Salt Lake City': U1, 'Salt Lake': U2,
                                'Utah': U3, 'USA': U4},
                         levels={U1: 4, U2: 5, U3: 6, U4: 8})
        assert seg('Salt Lake City Utah USA') == ['Salt Lake City', 'Utah', 'USA']

    def test_tier1_tail_beats_longer_tier2_span(self):
        # "Illinois US" exists as a raw input string but must still split.
        seg = _segmenter(tier1={'Illinois': U1, 'USA': U2},
                         tier2={'illinois us': U3},
                         levels={U1: 6, U2: 8, U3: 6})
        assert seg('Illinois US') == ['Illinois', 'US']

    def test_place_name_prefix_keeps_tier2_name_intact(self):
        # "Fort Madison" is only a raw string while "Madison" is a canonical
        # county, so tier-1 preference alone would shatter the city name.
        seg = _segmenter(tier1={'Madison': U1, 'Iowa': U2},
                         tier2={'fort madison': U3},
                         levels={U1: 5, U2: 6, U3: 4})
        assert seg('Fort Madison Iowa') == ['Fort Madison', 'Iowa']

    def test_cardinal_prefix_keeps_tier2_name_intact(self):
        seg = _segmenter(tier1={'Prussia': U1, 'Germany': U2},
                         tier2={'west prussia': U3},
                         levels={U1: 10, U2: 8, U3: 6})
        assert seg('West Prussia Germany') == ['West Prussia', 'Germany']

    def test_prefix_exception_never_applies_at_the_rightmost_boundary(self):
        # terms[-1] is the jurisdiction anchor, so the canonical reading must
        # win there or the whole string collapses into one segment.
        seg = _segmenter(tier1={'Madison': U1, 'Iowa': U2},
                         tier2={'fort madison': U3, 'fort madison iowa': U4},
                         levels={U1: 5, U2: 6, U3: 4, U4: 4})
        assert seg('Fort Madison Iowa') == ['Fort Madison', 'Iowa']

    def test_bare_jurisdiction_word_absorbed_left(self):
        seg = _segmenter(tier1={'Hampton': U1, 'Virginia': U2, 'USA': U3},
                         levels={U1: 4, U2: 6, U3: 8})
        assert seg('Hampton Hampton City Virginia USA') == \
            ['Hampton', 'Hampton City', 'Virginia', 'USA']

    def test_abbreviated_jurisdiction_suffix(self):
        seg = _segmenter(tier1={'Synnes': U1, 'Minnesota': U2},
                         levels={U1: 4, U2: 6})
        assert seg('Synnes Twp Minnesota') == ['Synnes Twp', 'Minnesota']

    def test_unmatched_words_glue_into_one_segment(self):
        seg = _segmenter(tier1={'Utah': U1, 'USA': U2}, levels={U1: 6, U2: 8})
        assert seg.walk('Foo Bar Baz Utah USA'.split()) == [
            ('Foo Bar Baz', 0), ('Utah', 1), ('USA', 1)]

    def test_repeated_segment_is_preserved_not_deduped(self):
        # A city and its county sharing a name is normal; match_entry already
        # skips adjacent duplicates.
        seg = _segmenter(tier1={'Monroe': U1, 'Wisconsin': U2},
                         levels={U1: 4, U2: 6})
        assert seg('Monroe Monroe Wisconsin') == ['Monroe', 'Monroe', 'Wisconsin']

    def test_rightmost_segment_is_the_broadest(self):
        seg = _segmenter(tier1={'Smiths Bridge': U1, 'Macon': U2,
                                'North Carolina': U3},
                         levels={U1: 4, U2: 5, U3: 6})
        result = seg('Smiths Bridge Township Macon North Carolina')
        assert result[-1] == 'North Carolina'

    def test_max_span_words_respected(self):
        seg = _segmenter(tier1={'a b c d e f g': U1, 'g': U2}, levels={U1: 6, U2: 6})
        assert seg.tier('a b c d e f g') == 1
        # 7 words exceeds SEGMENT_MAX_SPAN_WORDS, so the walk cannot take it
        assert seg._best_span_start('a b c d e f g'.split(), 7) != (0, 1)


class TestCommalessGate:
    def test_single_word_input_makes_no_record(self):
        seg = _segmenter(tier1={'Utah': U1}, levels={U1: 6})
        assert seg('Utah') is None
        assert seg.decisions == []

    def test_rejects_single_segment(self):
        # Whole string is a tier-2 raw only, so whole_known does not fire but
        # the walk still yields one segment.
        seg = _segmenter(tier2={'eagles club rooms': U1}, levels={U1: 4})
        assert seg('Eagles club rooms') is None
        assert seg.decisions[-1]['reason'] == 'single_segment'

    def test_jurisdiction_suffixed_name_is_whole_known(self):
        # "Rost township" strips to the known place "Rost", so it is a single
        # term in its own right and must not be split.
        seg = _segmenter(tier1={'Rost': U1}, levels={U1: 4})
        assert seg('Rost township') is None
        assert seg.decisions[-1]['reason'] == 'whole_known'

    def test_rejects_whole_string_already_known(self):
        seg = _segmenter(tier1={'East Georgia': U1, 'East': U2, 'Georgia': U3},
                         levels={U1: 4, U2: 4, U3: 6})
        assert seg('East Georgia') is None
        assert seg.decisions[-1]['reason'] == 'whole_known'

    def test_whole_known_ignores_tier2(self):
        # These inputs commonly exist as comma-less MNT raws; rejecting on that
        # basis discarded 3% of City/State rows.
        seg = _segmenter(tier1={'Swift': U1, 'Minnesota': U2},
                         tier2={'swift minnesota': U3}, levels={U1: 4, U2: 6})
        assert seg('Swift Minnesota') == ['Swift', 'Minnesota']

    def test_rejects_leading_digit(self):
        seg = _segmenter(tier1={'Rosalia': U1, 'Lane': U2}, levels={U1: 6, U2: 6})
        assert seg('1617 Rosalia Lane') is None
        assert seg.decisions[-1]['reason'] == 'digits'

    def test_rejects_long_digit_run(self):
        seg = _segmenter(tier1={'Highway': U1, 'Utah': U2}, levels={U1: 6, U2: 6})
        assert seg('Highway 101 Utah') is None
        assert seg.decisions[-1]['reason'] == 'digits'

    def test_rejects_prose_stopword(self):
        seg = _segmenter(tier1={'hospital': U1, 'Germany': U2}, levels={U1: 4, U2: 8})
        assert seg('a hospital in Germany') is None
        assert seg.decisions[-1]['reason'] == 'stopword'

    def test_stopword_inside_a_canonical_name_is_allowed(self):
        seg = _segmenter(tier1={'Isle of Wight': U1, 'England': U2},
                         levels={U1: 5, U2: 8})
        assert seg('Isle of Wight England') == ['Isle of Wight', 'England']

    def test_rejects_unresolved_segment(self):
        seg = _segmenter(tier1={'Utah': U1, 'USA': U2}, levels={U1: 6, U2: 8})
        assert seg('Foo Bar Utah USA') is None
        assert seg.decisions[-1]['reason'].startswith('unresolved:')

    def test_rejects_short_segment(self):
        seg = _segmenter(tier1={'B': U1, 'C': U2, 'Canada': U3},
                         levels={U1: 6, U2: 6, U3: 8})
        assert seg('B C Canada') is None
        assert seg.decisions[-1]['reason'] == 'short_segment'

    def test_rejects_rightmost_level_below_six(self):
        seg = _segmenter(tier1={'Sandy': U1, 'Salt Lake': U2}, levels={U1: 4, U2: 5})
        assert seg('Sandy Salt Lake') is None
        assert seg.decisions[-1]['reason'] == 'rightmost_level:5'

    def test_rejects_over_word_cap(self):
        seg = _segmenter(tier1={'Utah': U1}, levels={U1: 6})
        assert seg(' '.join(['word'] * 13)) is None
        assert seg.decisions[-1]['reason'] == 'word_count'

    def test_counters_and_reasons(self):
        seg = _segmenter(tier1={'Swift': U1, 'Minnesota': U2}, levels={U1: 4, U2: 6})
        seg('Swift Minnesota')
        seg('1617 Rosalia Lane')
        assert seg.examined == 2
        assert seg.accepted == 1
        assert seg.reasons == {'digits': 1}

    def test_accepted_decision_record(self):
        seg = _segmenter(tier1={'Swift': U1, 'Minnesota': U2}, levels={U1: 4, U2: 6})
        seg('Swift Minnesota', guid='g1', frequency='7')
        record = seg.decisions[-1]
        assert record['decision'] == 'accepted'
        assert record['reason'] == ''
        assert record['guid'] == 'g1'
        assert record['frequency'] == '7'
        assert record['segment_count'] == 2
        assert record['segments'] == 'Swift|Minnesota'
        assert record['tiers'] == '1|1'
        assert record['rightmost_level'] == 6

    def test_log_is_capped(self):
        seg = _segmenter(tier1={'Swift': U1, 'Minnesota': U2}, levels={U1: 4, U2: 6})
        seg.log_max = 1
        seg('Swift Minnesota')
        seg('Swift Minnesota')
        assert len(seg.decisions) == 1
        assert seg.examined == 2


class TestParseEntriesSegmentation:
    def test_absent_segment_fn_is_unchanged_behavior(self):
        entries = [{'place': 'Salt Lake City Utah USA', 'guid': 'g', 'frequency': '1'}]
        parsed, all_terms, _hints = parse_entries(entries)
        assert parsed[0][3] == ['Salt Lake City Utah USA']
        assert all_terms == {'Salt Lake City Utah USA'}

    def test_segments_replace_the_single_term(self):
        entries = [{'place': 'Salt Lake City Utah USA', 'guid': 'g', 'frequency': '1'}]
        parsed, all_terms, _hints = parse_entries(
            entries, segment_fn=lambda t, **kw: ['Salt Lake City', 'Utah', 'USA'])
        assert parsed[0][3] == ['Salt Lake City', 'Utah', 'USA']
        assert all_terms == {'Salt Lake City', 'Utah', 'USA'}

    def test_none_return_keeps_single_term(self):
        entries = [{'place': 'Eagles club rooms', 'guid': 'g', 'frequency': '1'}]
        parsed, _all_terms, _hints = parse_entries(entries,
                                                   segment_fn=lambda t, **kw: None)
        assert parsed[0][3] == ['Eagles club rooms']

    def test_not_called_for_comma_row(self):
        calls = []

        def stub(term, **kw):
            calls.append(term)
            return ['x', 'y']

        entries = [{'place': 'Provo, Utah', 'guid': 'g', 'frequency': '1'}]
        parsed, _all_terms, _hints = parse_entries(entries, segment_fn=stub)
        assert calls == []
        assert parsed[0][3] == ['Provo', 'Utah']

    def test_not_called_for_semicolon_row(self):
        calls = []
        entries = [{'place': 'Provo; Utah', 'guid': 'g', 'frequency': '1'}]
        parse_entries(entries, segment_fn=lambda t, **kw: calls.append(t))
        assert calls == []

    def test_original_guid_frequency_preserved(self):
        entries = [{'place': 'Swift Minnesota', 'guid': 'g9', 'frequency': '42'}]
        parsed, _all_terms, _hints = parse_entries(
            entries, segment_fn=lambda t, **kw: ['Swift', 'Minnesota'])
        assert parsed[0][0] == 'Swift Minnesota'
        assert parsed[0][1] == 'g9'
        assert parsed[0][2] == '42'

    def test_guid_and_frequency_passed_to_segmenter(self):
        seen = {}

        def stub(term, guid='', frequency=''):
            seen.update(term=term, guid=guid, frequency=frequency)
            return None

        parse_entries([{'place': 'Swift Minnesota', 'guid': 'g1',
                        'frequency': '3'}], segment_fn=stub)
        assert seen == {'term': 'Swift Minnesota', 'guid': 'g1', 'frequency': '3'}

    def test_noise_filter_runs_after_segmentation(self):
        entries = [{'place': 'Route 66 Utah USA', 'guid': 'g', 'frequency': '1'}]
        parsed, _all_terms, _hints = parse_entries(
            entries, segment_fn=lambda t, **kw: ['Route 66', 'Utah', 'USA'])
        assert parsed[0][3] == ['Utah', 'USA']

    def test_all_noise_segments_fall_back_unfiltered(self):
        entries = [{'place': 'Route 66 RR 2', 'guid': 'g', 'frequency': '1'}]
        parsed, _all_terms, _hints = parse_entries(
            entries, segment_fn=lambda t, **kw: ['Route 66', 'RR 2'])
        assert parsed[0][3] == ['Route 66', 'RR 2']

    def test_jurisdiction_hints_detected_on_segments(self):
        entries = [{'place': 'Mount Pleasant Henry County Iowa',
                    'guid': 'g', 'frequency': '1'}]
        _parsed, _all_terms, hints = parse_entries(
            entries,
            segment_fn=lambda t, **kw: ['Mount Pleasant', 'Henry County', 'Iowa'])
        assert hints['henry county'] == 'County'

    def test_compound_place_substitution_applies_first(self):
        seen = []
        entries = [{'place': 'Washington, D.C.', 'guid': 'g', 'frequency': '1'}]
        parse_entries(entries, segment_fn=lambda t, **kw: seen.append(t))
        assert seen == ['Washington D.C.']


class TestWriteSegmentLog:
    def test_writes_tsv_with_header(self):
        decisions = [{
            'original': 'Swift Minnesota', 'guid': 'g1', 'frequency': '1',
            'decision': 'accepted', 'reason': '', 'segment_count': 2,
            'segments': 'Swift|Minnesota', 'tiers': '1|1', 'rightmost_level': 6,
        }]
        with tempfile.NamedTemporaryFile(mode='w', suffix='.tsv',
                                        delete=False) as f:
            path = f.name
        try:
            write_segment_log(decisions, path)
            with open(path) as f:
                lines = f.read().strip().split('\n')
            assert lines[0] == '\t'.join(SEGMENT_LOG_FIELDS)
            assert 'Swift|Minnesota' in lines[1]
        finally:
            os.unlink(path)

    def test_writes_rejection_reason(self):
        decisions = [{
            'original': '1617 Rosalia Lane', 'guid': '', 'frequency': '',
            'decision': 'rejected', 'reason': 'digits', 'segment_count': 0,
            'segments': '', 'tiers': '', 'rightmost_level': '',
        }]
        with tempfile.NamedTemporaryFile(mode='w', suffix='.tsv',
                                        delete=False) as f:
            path = f.name
        try:
            write_segment_log(decisions, path)
            with open(path) as f:
                body = f.read()
            assert 'rejected\tdigits' in body
        finally:
            os.unlink(path)


class TestSegmentCommalessFlag:
    def _args(self, extra):
        return build_cli().parse_args(['--input', 'i.tsv'] + extra)

    def test_default_on(self):
        assert self._args([]).segment_commaless is True

    def test_negated_flag_disables(self):
        assert self._args(['--no-segment-commaless']).segment_commaless is False

    def test_explicit_flag_enables(self):
        assert self._args(['--segment-commaless']).segment_commaless is True

    def test_parses_alongside_other_flags(self):
        args = self._args(['--dict', '--segment-commaless'])
        assert args.dict == 'live' and args.segment_commaless is True


class TestResolveOutputPaths:
    def test_returns_five_paths(self, tmp_path):
        output, ties, spelling, segments, levels = _resolve_output_paths(
            'places.tsv', str(tmp_path))
        assert output.endswith('places_01.tsv')
        assert ties.endswith('places_01_ties.tsv')
        assert spelling.endswith('places_01_spelling.tsv')
        assert segments.endswith('places_01_segments.tsv')
        assert levels.endswith('places_01_levels.tsv')

    def test_side_files_do_not_disturb_numbering(self, tmp_path):
        output, _t, _s, segments, levels = _resolve_output_paths(
            'places.tsv', str(tmp_path))
        open(output, 'w').close()
        open(segments, 'w').close()
        open(levels, 'w').close()
        next_output, _t2, _s2, _g2, _l2 = _resolve_output_paths('places.tsv',
                                                                str(tmp_path))
        assert next_output.endswith('places_02.tsv')


# ---------------------------------------------------------------------------
# Export-defect fixes: source-defect reporting, resolution kinds, level scope,
# candidate provenance, and term-to-level attribution.
# ---------------------------------------------------------------------------


class TestSourceDefectFlags:
    def test_detects_cp437_signatures(self):
        # UTF-8 bytes decoded as code page 437, with and without the
        # transliteration of the Greek gamma to a Latin G.
        assert has_encoding_corruption('near Vend+¦me, France')
        assert has_encoding_corruption('train crossing in ErieGÇÖs West Side')
        assert has_encoding_corruption('ΓÇÖ raw form')

    def test_leaves_legitimate_characters_alone(self):
        # C-cedilla is a real letter, and '+' and 'G' are real characters in
        # place text, so none of them is a signature on its own.
        assert not has_encoding_corruption('Besançon, Doubs, France')
        assert not has_encoding_corruption('Çankaya, Ankara, Turkey')
        assert not has_encoding_corruption('Ciudad de México')
        assert not has_encoding_corruption('Hartwell, Gwinnett? or nearest GA county')
        assert not has_encoding_corruption('@24 I, street northeast')

    def test_tags_redundancy_shapes(self):
        assert source_shape_tags('five miles north of Danville, Danville') == ['embedded']
        assert source_shape_tags('Madison, route 6, Madison') == ['restated']
        assert source_shape_tags('Koolik, Koolik') == ['adjacent', 'doubled']
        assert source_shape_tags('hospital, Nashville; Tenn.') == ['joined']
        assert source_shape_tags('Last Chance restaurant, Newark, Ohio') == []

    def test_pipe_counts_as_a_join(self):
        tags = source_shape_tags('Washington, Pa., hospital, Washington|Pennsylvania')
        assert 'joined' in tags and 'restated' in tags

    def test_adjacent_repeat_is_reported_not_judged(self):
        # Indistinguishable from the ordinary city/county homonym convention
        # without an authority lookup, so the shape is reported either way.
        assert source_shape_tags('Albany, Albany, New York') == ['adjacent']

    def test_row_carries_the_flags(self):
        match = MatchResult(candidate_ids=[], depth=0, match_type='no_auth_match')
        row = build_result_row(match, 'near Vend+¦me, France', 'g1', '1', {})
        assert row['source_encoding_suspect'] == 'true'
        row = build_result_row(match, 'Koolik, Koolik', 'g2', '1', {})
        assert row['source_encoding_suspect'] == ''
        assert row['source_shape'] == 'adjacent;doubled'


class TestResolutionKind:
    def test_ties_are_ties(self):
        for match_type in ('single_amb', 'chain_amb', 'parent_amb'):
            assert resolution_kind(match_type) == 'tie'

    def test_parent_rejected_is_suspect_not_a_tie(self):
        assert resolution_kind('parent_rejected') == 'suspect'

    def test_everything_else_resolves(self):
        for match_type in ('chain_verified', 'single_term', 'parent_resolved',
                           'freq_resolved', 'no_auth_match'):
            assert resolution_kind(match_type) == 'resolved'

    def test_suspect_row_has_one_candidate_and_blank_authority(self):
        # The shape that made parent_rejected read as ambiguous: a single
        # candidate, carried at low confidence, with authority_* left blank.
        auth = {'a': make_auth_record_full('a', name='Trego')}
        match = MatchResult(candidate_ids=['a'], depth=1,
                            match_type='parent_rejected')
        row = build_result_row(match, 'District Court, County of Trego',
                               'g1', '1', auth)
        assert row['resolution_kind'] == 'suspect'
        assert row['candidates'] == 1
        assert row['authority_id'] == ''

    def test_tie_row_keeps_its_array(self):
        auth = {'a': make_auth_record_full('a'), 'b': make_auth_record_full('b')}
        match = MatchResult(candidate_ids=[], depth=1, match_type='parent_amb',
                            tied_ids=['a', 'b'])
        row = build_result_row(match, 'Bartholdi Hotel, New York', 'g1', '1', auth)
        assert row['resolution_kind'] == 'tie'
        assert row['candidates'] == 2


class TestSupportedLevelScope:
    def _nyc_chain(self):
        return {
            'hk': make_auth_record_full('hk', 'man', "Hell's Kitchen", level='2',
                                        jurisdiction='Neighborhood'),
            'man': make_auth_record_full('man', 'nyc', 'Manhattan', level='4'),
            'nyc': make_auth_record_full('nyc', 'ny', 'New York City', level='5'),
            'ny': make_auth_record_full('ny', 'usa', 'New York', level='6'),
            'usa': make_auth_record_full('usa', None, 'USA', level='8'),
        }

    def test_level_two_is_outside_the_supported_range(self):
        assert not is_supported_level(2)
        assert is_supported_level(3)
        assert is_supported_level(10)
        assert not is_supported_level(11)
        assert not is_supported_level(None)

    def test_record_level_survives_junk(self):
        auth = {'a': make_auth_record_full('a', level='4'),
                'b': make_auth_record_full('b', level='')}
        assert record_level('a', auth) == 4
        assert record_level('b', auth) is None
        assert record_level('missing', auth) is None

    def test_supported_match_is_its_own_ancestor(self):
        auth = self._nyc_chain()
        assert deepest_supported_ancestor('man', auth) == ('man', 4)

    def test_unsupported_match_climbs_to_the_first_supported_level(self):
        auth = self._nyc_chain()
        assert deepest_supported_ancestor('hk', auth) == ('man', 4)

    def test_orphan_below_the_range_has_no_supported_answer(self):
        auth = {'orph': make_auth_record_full('orph', None, 'Cemetery', level='2')}
        assert deepest_supported_ancestor('orph', auth) == (None, None)

    def test_non_resolution_still_reports_its_level(self):
        # The defect: a parent_amb row landing on a level-2 record showed no
        # level at all, because authority_* is blanked for non-resolutions.
        auth = self._nyc_chain()
        match = MatchResult(candidate_ids=[], depth=1, match_type='parent_amb',
                            tied_ids=['hk'])
        row = build_result_row(match, 'Woodlawn Convalescent Home, Clinton',
                               'g1', '1', auth)
        assert row['authority_id'] == ''
        assert row['matched_uuid'] == 'hk'
        assert row['matched_level'] == 2
        assert row['below_supported'] == 'true'
        assert row['supported_leaf_id'] == 'man'

    def test_supported_match_is_not_flagged(self):
        auth = self._nyc_chain()
        match = MatchResult(candidate_ids=['man'], depth=2,
                            match_type='chain_verified')
        row = build_result_row(match, 'Manhattan, New York', 'g1', '1', auth)
        assert row['below_supported'] == ''
        assert row['supported_leaf_id'] == 'man'

    def test_losing_unsupported_candidate_is_counted(self):
        # Influence that otherwise leaves no trace: the level-2 record shaped
        # the tie without winning it.
        auth = self._nyc_chain()
        match = MatchResult(candidate_ids=[], depth=1, match_type='chain_amb',
                            tied_ids=['man', 'hk'])
        row = build_result_row(match, 'somewhere', 'g1', '1', auth)
        assert row['below_supported'] == ''
        assert row['unsupported_in_candidates'] == 'true'


class TestNameCacheProvenance:
    def test_records_the_phase_that_supplied_each_uuid(self):
        cache = NameCache()
        cache.current_origin = 'exact'
        cache['roanoke'].add('U-ROANOKE')
        cache.current_origin = 'spelling'
        cache['roannke'].add('U-ROANOKE')
        assert cache.origin_of('roanoke', 'U-ROANOKE') == 'exact'
        assert cache.origin_of('roannke', 'U-ROANOKE') == 'spelling'

    def test_first_writer_wins(self):
        # A later, more permissive phase that rediscovers a term must not
        # relabel what an exact lookup already found.
        cache = NameCache()
        cache.current_origin = 'exact'
        cache['dumas'].add('U-DUMAS')
        cache.current_origin = 'variant'
        cache['dumas'].update({'U-DUMAS'})
        assert cache.origin_of('dumas', 'U-DUMAS') == 'exact'

    def test_update_and_assignment_are_both_recorded(self):
        cache = NameCache()
        cache.current_origin = 'mnt'
        cache['a'].update({'U-1', 'U-2'})
        cache['b'] = {'U-3'}
        assert cache.origin_of('a', 'U-1') == 'mnt'
        assert cache.origin_of('a', 'U-2') == 'mnt'
        assert cache.origin_of('b', 'U-3') == 'mnt'

    def test_behaves_like_the_defaultdict_it_replaced(self):
        cache = NameCache()
        assert cache.get('absent') is None
        cache['fresh'].add('U-1')
        assert cache['fresh'] == {'U-1'}
        assert sum(1 for v in cache.values() if v) == 1

    def test_lookup_reports_origin_per_term(self):
        cache = NameCache()
        cache.current_origin = 'exact'
        cache['roanoke'].add('U-ROANOKE')
        cache.current_origin = 'spelling'
        cache['roannke'].add('U-ROANOKE')
        ascii_cache = build_ascii_index(cache)
        assert lookup_name_with_origin('Roanoke', cache, ascii_cache) == {
            'U-ROANOKE': 'exact'}
        assert lookup_name_with_origin('Roannke', cache, ascii_cache) == {
            'U-ROANOKE': 'spelling'}

    def test_ascii_fold_is_its_own_origin(self):
        cache = NameCache()
        cache.current_origin = 'exact'
        cache['méxico'].add('U-MEXICO')
        ascii_cache = build_ascii_index(cache)
        # Reached only by folding the input, so folding is what matched.
        assert lookup_name_with_origin('Mexico', cache, ascii_cache) == {
            'U-MEXICO': 'ascii_fold'}
        # Written with the accent, it is a direct hit.
        assert lookup_name_with_origin('México', cache, ascii_cache) == {
            'U-MEXICO': 'exact'}

    def test_lookup_name_still_returns_a_plain_set(self):
        cache = NameCache()
        cache['albany'].add('U-ALBANY')
        assert lookup_name('Albany', cache, {}) == {'U-ALBANY'}

    def test_plain_dict_caches_still_work(self):
        assert lookup_name_with_origin('x', {'x': {'U-X'}}, {}) == {'U-X': 'exact'}

    def test_records_the_span_a_uuid_was_looked_up_under(self):
        cache = NameCache()
        cache.current_origin = 'preposition'
        cache['lutheran church in the village'].add('U-VILLAGE')
        cache.record_span('lutheran church in the village', 'U-VILLAGE',
                          'the village')
        assert cache.span_of('lutheran church in the village',
                             'U-VILLAGE') == 'the village'

    def test_span_defaults_to_the_key(self):
        # Phases that look the term up verbatim record nothing, and the key is
        # the correct answer for them.
        cache = NameCache()
        cache.current_origin = 'exact'
        cache['albany'].add('U-ALBANY')
        assert cache.span_of('albany', 'U-ALBANY') == 'albany'

    def test_span_first_writer_wins(self):
        cache = NameCache()
        cache.record_span('near despatch', 'U-D', 'Despatch')
        cache.record_span('near despatch', 'U-D', 'near Despatch')
        assert cache.span_of('near despatch', 'U-D') == 'Despatch'

    def test_span_for_tolerates_a_plain_dict_cache(self):
        assert span_for('Albany', 'U-ALBANY', {'albany': {'U-ALBANY'}}) == 'albany'

    def test_span_for_lowercases_the_term_to_key_the_lookup(self):
        cache = NameCache()
        cache.record_span('near despatch', 'U-D', 'Despatch')
        assert span_for('near Despatch', 'U-D', cache) == 'Despatch'


class TestSpanWiring:
    """Every phase that rewrites a term before lookup must record what it
    actually looked up, or the gate tests the wrong string."""

    def test_spelling_correction_records_the_corrected_term(self, local_pa):
        local_pa(make_auth_record('u-birm', name='Birmingham'))
        sym = SymSpell(max_dictionary_edit_distance=1, prefix_length=7)
        sym.create_dictionary_entry('birmingham', 1)
        cache = NameCache()
        cache.current_origin = 'spelling'
        query_spelling_corrections_local(['Birminghan'], cache, sym)
        assert cache.span_of('birminghan', 'u-birm') == 'birmingham'

    def test_preposition_extraction_records_the_extracted_span(self, local_pa):
        local_pa(make_auth_record_full('u-boz', name='Bozeman',
                                       jurisdiction='City'))
        cache = NameCache()
        cache.current_origin = 'preposition'
        query_preposition_extractions_local(
            ['Chapel of the Presbyterian Church in Bozeman'], cache)
        assert cache.span_of(
            'chapel of the presbyterian church in bozeman', 'u-boz') == 'Bozeman'

    def test_cardinal_strip_records_the_stripped_form(self, local_pa):
        local_pa(make_auth_record_full('u-ks', name='Kansas',
                                       jurisdiction='State'))
        cache = NameCache()
        cache.current_origin = 'cardinal_strip'
        query_cardinal_strip_local(['eastern Kansas'], cache,
                                   {'eastern Kansas': 'east Kansas'})
        assert cache.span_of('eastern kansas', 'u-ks') == 'Kansas'


class TestTermAttribution:
    def _virginia(self):
        cache = NameCache()
        cache.current_origin = 'abbrev'
        cache['va'].add('U-VA')
        cache.current_origin = 'spelling'
        cache['roannke'].add('U-ROANOKE')
        auth = {
            'U-ROANOKE': make_auth_record_full('U-ROANOKE', 'U-VA', 'Roanoke',
                                               level='4', jurisdiction='City'),
            'U-VA': make_auth_record_full('U-VA', 'U-USA', 'Virginia',
                                          level='6', jurisdiction='State'),
            'U-USA': make_auth_record_full('U-USA', None, 'USA',
                                           level='8', jurisdiction='Country'),
        }
        return cache, auth

    def test_walk_records_the_term_behind_each_step(self):
        cache, auth = self._virginia()
        match = match_entry(['Roannke', 'Va'], cache, auth,
                            'Roannke, Va', ascii_cache=build_ascii_index(cache))
        assert match.match_type == 'chain_verified'
        assert [s.term for s in match.steps] == ['Va', 'Roannke']

    def test_fuzzy_match_reports_the_token_that_drove_it(self):
        # The report's central complaint: a fuzzy match recorded nothing, so
        # an auditor could not tell what produced the answer.
        cache, auth = self._virginia()
        match = match_entry(['Roannke', 'Va'], cache, auth,
                            'Roannke, Va', ascii_cache=build_ascii_index(cache))
        by_level = {r['level']: r for r in build_level_provenance(match, 'g1', auth)}
        assert by_level['4']['raw_term'] == 'Roannke'
        assert by_level['4']['name'] == 'Roanoke'
        assert by_level['4']['match_method'] == 'fuzzy'
        assert by_level['4']['origin'] == 'spelling'

    def test_abbreviation_reads_as_normalized(self):
        cache, auth = self._virginia()
        match = match_entry(['Roannke', 'Va'], cache, auth,
                            'Roannke, Va', ascii_cache=build_ascii_index(cache))
        by_level = {r['level']: r for r in build_level_provenance(match, 'g1', auth)}
        assert by_level['6']['raw_term'] == 'Va'
        assert by_level['6']['match_method'] == 'normalized'
        assert by_level['6']['origin'] == 'abbrev'

    def test_levels_no_term_reached_are_inferred(self):
        # A null raw term must mean "supplied by the hierarchy", never
        # "the term was not in the input".
        cache, auth = self._virginia()
        match = match_entry(['Roannke', 'Va'], cache, auth,
                            'Roannke, Va', ascii_cache=build_ascii_index(cache))
        by_level = {r['level']: r for r in build_level_provenance(match, 'g1', auth)}
        assert by_level['8']['raw_term'] == ''
        assert by_level['8']['match_method'] == 'inferred'

    def test_chain_is_emitted_leaf_first(self):
        cache, auth = self._virginia()
        match = match_entry(['Roannke', 'Va'], cache, auth,
                            'Roannke, Va', ascii_cache=build_ascii_index(cache))
        rows = build_level_provenance(match, 'g1', auth)
        assert [r['name'] for r in rows] == ['Roanoke', 'Virginia', 'USA']
        assert [r['depth_from_leaf'] for r in rows] == [0, 1, 2]
        assert all(r['guid'] == 'g1' for r in rows)

    def test_single_term_carries_its_anchor(self):
        cache = NameCache()
        cache.current_origin = 'exact'
        cache['ohio'].add('U-OH')
        auth = {'U-OH': make_auth_record_full('U-OH', None, 'Ohio', level='6')}
        match = match_entry(['Ohio'], cache, auth, 'Ohio')
        assert [s.term for s in match.steps] == ['Ohio']
        rows = build_level_provenance(match, 'g1', auth)
        assert rows[0]['raw_term'] == 'Ohio'
        assert rows[0]['match_method'] == 'verbatim'

    def test_no_candidates_yields_no_rows(self):
        match = MatchResult(candidate_ids=[], depth=0, match_type='no_auth_match')
        assert build_level_provenance(match, 'g1', {}) == []

    def test_unsupported_leaf_still_appears_in_provenance(self):
        # The level-2 match is kept, so the export can show what actually
        # matched rather than only reporting that something did.
        cache = NameCache()
        cache.current_origin = 'exact'
        cache['hollis'].add('hk')
        auth = {
            'hk': make_auth_record_full('hk', 'man', 'Hollis', level='2',
                                        jurisdiction='Neighborhood'),
            'man': make_auth_record_full('man', None, 'Queens', level='4'),
        }
        match = match_entry(['Hollis'], cache, auth, 'Flint Pond Road, Hollis')
        rows = build_level_provenance(match, 'g1', auth)
        assert rows[0]['level'] == '2'
        assert rows[0]['raw_term'] == 'Hollis'
        assert rows[1]['name'] == 'Queens'
