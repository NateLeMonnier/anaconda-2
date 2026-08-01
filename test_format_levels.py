"""Tests for the levels export.

The export is now a join between the matcher's results TSV and the level
provenance side file, so these cover the reshaping and the four defects the
export was holding up: a dangling leaf_uuid, ambiguity conflated with
suspicion, a null raw term that could not be told apart from an absent one,
and upstream source defects travelling unreported.
"""

from format_levels import (
    LEVELS,
    build_entry,
    load_level_provenance,
    summarize,
)


def make_row(**overrides):
    row = {
        'original': 'Roannke, Va',
        'guid': 'g1',
        'frequency': '3',
        'match_type': 'chain_verified',
        'confidence': 'high',
        'candidates': '1',
        'resolution_kind': 'resolved',
        'matched_uuid': 'U-ROANOKE',
        'matched_level': '4',
        'below_supported': '',
        'supported_leaf_id': 'U-ROANOKE',
        'unsupported_in_candidates': '',
        'source_encoding_suspect': '',
        'source_shape': '',
    }
    row.update(overrides)
    return row


def make_node(depth, level, uuid, name, raw='', method='inferred',
              origin='', jurisdiction=''):
    return {
        'guid': 'g1',
        'depth_from_leaf': str(depth),
        'level': str(level),
        'uuid': uuid,
        'name': name,
        'jurisdiction': jurisdiction,
        'raw_term': raw,
        'match_method': method,
        'origin': origin,
    }


ROANOKE_CHAIN = [
    make_node(0, 4, 'U-ROANOKE', 'Roanoke', 'Roannke', 'fuzzy', 'spelling', 'City'),
    make_node(1, 6, 'U-VA', 'Virginia', 'Va', 'normalized', 'abbrev', 'State'),
    make_node(2, 8, 'U-USA', 'USA', '', 'inferred', '', 'Country'),
]


class TestLeafUuid:
    def test_leaf_comes_from_the_supported_id(self):
        entry = build_entry(make_row(), {'g1': ROANOKE_CHAIN})
        assert entry['leaf_uuid'] == 'U-ROANOKE'
        assert entry['leaf_uuid'] in {n['uuid'] for n in entry['levels'].values()}

    def test_unsupported_match_does_not_dangle(self):
        # The defect: leaf_uuid pointed at a level-2 record that appears at no
        # level in the entry, so the identifier resolved to nothing on show.
        chain = [
            make_node(0, 2, 'U-HK', "Hell's Kitchen", 'Clinton', 'verbatim',
                      'exact', 'Neighborhood'),
            make_node(1, 4, 'U-MAN', 'Manhattan', '', 'inferred', '', 'Borough'),
            make_node(2, 6, 'U-NY', 'New York', '', 'inferred', '', 'State'),
        ]
        entry = build_entry(make_row(
            original='Woodlawn Convalescent Home, Clinton',
            matched_uuid='U-HK', matched_level='2', below_supported='true',
            supported_leaf_id='U-MAN'), {'g1': chain})
        assert entry['leaf_uuid'] == 'U-MAN'
        assert entry['leaf_uuid'] in {n['uuid'] for n in entry['levels'].values()}
        assert entry['matched_below_supported'] is True
        assert entry['matched_level'] == 2

    def test_unsupported_match_is_kept_not_discarded(self):
        chain = [
            make_node(0, 2, 'U-HK', "Hell's Kitchen", 'Clinton', 'verbatim',
                      'exact', 'Neighborhood'),
            make_node(1, 4, 'U-MAN', 'Manhattan', '', 'inferred', '', 'Borough'),
        ]
        entry = build_entry(make_row(
            matched_uuid='U-HK', matched_level='2', below_supported='true',
            supported_leaf_id='U-MAN'), {'g1': chain})
        assert entry['matched_below']['name'] == "Hell's Kitchen"
        assert entry['matched_below']['level'] == 2
        assert entry['matched_below']['raw'] == 'Clinton'
        # and it stays out of the supported levels block
        assert '2' not in entry['levels']

    def test_no_candidate_leaves_the_leaf_null(self):
        entry = build_entry(make_row(match_type='no_auth_match',
                                     supported_leaf_id='', matched_uuid='',
                                     matched_level='', candidates='0'), {})
        assert entry['leaf_uuid'] is None
        assert entry['levels'] == {}


class TestResolutionKind:
    def test_tie_is_ambiguous(self):
        entry = build_entry(make_row(match_type='parent_amb',
                                     resolution_kind='tie', candidates='5'),
                            {'g1': ROANOKE_CHAIN})
        assert entry['ambiguous'] is True
        assert entry['parent_suspect'] is False

    def test_suspect_is_not_ambiguous(self):
        # parent_rejected carried ambiguous=true with a single candidate,
        # unlike every other ambiguous type. It is suspicion, not a tie.
        entry = build_entry(make_row(match_type='parent_rejected',
                                     resolution_kind='suspect', candidates='1'),
                            {'g1': ROANOKE_CHAIN})
        assert entry['ambiguous'] is False
        assert entry['parent_suspect'] is True
        assert entry['candidate_count'] == 1

    def test_resolved_is_neither(self):
        entry = build_entry(make_row(), {'g1': ROANOKE_CHAIN})
        assert entry['ambiguous'] is False
        assert entry['parent_suspect'] is False


class TestRawAndMethod:
    def test_fuzzy_token_is_recorded(self):
        entry = build_entry(make_row(), {'g1': ROANOKE_CHAIN})
        assert entry['levels']['4']['raw'] == 'Roannke'
        assert entry['levels']['4']['name'] == 'Roanoke'
        assert entry['levels']['4']['match_method'] == 'fuzzy'
        assert entry['levels']['4']['origin'] == 'spelling'

    def test_null_raw_is_explained_by_the_method(self):
        # A null must read as "the hierarchy supplied this", never as
        # "the term was not in the input".
        node = build_entry(make_row(), {'g1': ROANOKE_CHAIN})['levels']['8']
        assert node['raw'] is None
        assert node['match_method'] == 'inferred'

    def test_every_level_states_a_method(self):
        entry = build_entry(make_row(), {'g1': ROANOKE_CHAIN})
        assert all(n['match_method'] for n in entry['levels'].values())


class TestLevelsBlock:
    def test_only_supported_levels_are_emitted(self):
        chain = ROANOKE_CHAIN + [
            make_node(3, 11, 'U-NA', 'North America', '', 'inferred', '', 'Continent')]
        entry = build_entry(make_row(), {'g1': chain})
        assert set(entry['levels']) == {'4', '6', '8'}
        assert all(int(k) in LEVELS for k in entry['levels'])

    def test_levels_are_sorted_numerically(self):
        entry = build_entry(make_row(), {'g1': ROANOKE_CHAIN})
        assert list(entry['levels']) == ['4', '6', '8']

    def test_same_level_collision_keeps_the_more_specific_node(self):
        chain = [
            make_node(0, 4, 'U-CITY', 'Springfield', 'Springfield', 'verbatim', 'exact'),
            make_node(1, 4, 'U-OTHER', 'Springfield Township', '', 'inferred', ''),
            make_node(2, 6, 'U-IL', 'Illinois', 'Illinois', 'verbatim', 'exact'),
        ]
        entry = build_entry(make_row(), {'g1': chain})
        assert entry['levels']['4']['uuid'] == 'U-CITY'


class TestSourceDefects:
    def test_encoding_flag_travels(self):
        entry = build_entry(make_row(original='near Vend+¦me, France',
                                     source_encoding_suspect='true'),
                            {'g1': ROANOKE_CHAIN})
        assert entry['source_encoding_suspect'] is True
        # the string itself is untouched — repairing it would be lossy
        assert entry['original'] == 'near Vend+¦me, France'

    def test_shape_tags_split(self):
        entry = build_entry(make_row(source_shape='adjacent;doubled'),
                            {'g1': ROANOKE_CHAIN})
        assert entry['source_shape'] == ['adjacent', 'doubled']

    def test_clean_row_has_no_tags(self):
        entry = build_entry(make_row(), {'g1': ROANOKE_CHAIN})
        assert entry['source_shape'] == []
        assert entry['source_encoding_suspect'] is False


class TestLoadProvenance:
    def test_groups_by_guid_and_orders_leaf_first(self, tmp_path):
        path = tmp_path / 'levels.tsv'
        header = ('guid\tdepth_from_leaf\tlevel\tuuid\tname\tjurisdiction\t'
                  'raw_term\tmatch_method\torigin\n')
        path.write_text(
            header
            + 'g1\t2\t8\tU-USA\tUSA\tCountry\t\tinferred\t\n'
            + 'g1\t0\t4\tU-ROANOKE\tRoanoke\tCity\tRoannke\tfuzzy\tspelling\n'
            + 'g1\t1\t6\tU-VA\tVirginia\tState\tVa\tnormalized\tabbrev\n'
            + 'g2\t0\t6\tU-OH\tOhio\tState\tOhio\tverbatim\texact\n',
            encoding='utf-8')
        by_guid = load_level_provenance(str(path))
        assert set(by_guid) == {'g1', 'g2'}
        assert [n['name'] for n in by_guid['g1']] == ['Roanoke', 'Virginia', 'USA']


class TestSummary:
    def test_counts_the_scope_and_source_findings(self):
        entries = [
            build_entry(make_row(), {'g1': ROANOKE_CHAIN}),
            build_entry(make_row(below_supported='true', matched_level='2',
                                 supported_leaf_id='U-VA'), {'g1': ROANOKE_CHAIN}),
            build_entry(make_row(unsupported_in_candidates='true'),
                        {'g1': ROANOKE_CHAIN}),
            build_entry(make_row(source_encoding_suspect='true',
                                 source_shape='joined'), {'g1': ROANOKE_CHAIN}),
        ]
        summary = summarize(entries)
        assert summary['rows'] == 4
        assert summary['matched_below_supported'] == 1
        assert summary['unsupported_in_candidates'] == 1
        assert summary['encoding_suspect'] == 1
        assert summary['source_shapes'] == {'joined': 1}
