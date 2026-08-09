"""Tests for the PA index used by the eval labeler."""
import pytest

from pa_index import (
    normalize_term,
    normalize_chain,
    PAIndex,
    PARow,
)


def row(term, uuid, chain, level='4', replacement=''):
    return PARow(level=level, level_name='City', uuid=uuid, term=term,
                 full_chain=chain, parent_id='', population='0',
                 replacement_uuid=replacement)


@pytest.fixture
def index():
    return PAIndex([
        row('Syracuse', 'U-NY', 'Syracuse, Onondaga, New York, USA'),
        row('Syracuse', 'U-UT', 'Syracuse, Davis, Utah, USA'),
        row('Syracuse', 'U-KS', 'Syracuse, Hamilton, Kansas, USA'),
        row('Onondaga', 'U-ONO', 'Onondaga, New York, USA', level='5'),
        row('Lowell', 'U-LOW', 'Lowell, Middlesex, Massachusetts, USA'),
        row('Montréal', 'U-MTL', 'Montréal, Québec, Canada'),
        row('Oldtown', 'U-OLD-NEW', 'Oldtown, Kent, Maryland, USA',
            replacement='U-REPLACEMENT'),
    ])


def test_normalize_term_lowercases_and_strips_accents():
    assert normalize_term('  Montréal ') == 'montreal'


def test_normalize_term_drops_punctuation():
    assert normalize_term("St. Mary's") == 'st marys'


def test_normalize_term_drops_parentheticals():
    # PA marks superseded places inline: Term "Prussia", chain leaf
    # "Prussia (historical)".
    assert normalize_term('Prussia (historical)') == 'prussia'


def test_normalize_term_keeps_jurisdiction_qualifiers():
    # Qualifier stripping belongs to chain comparison, not the index.
    assert normalize_term('Pike County') == 'pike county'


def test_normalize_chain_keeps_comma_structure():
    assert normalize_chain('Syracuse, Onondaga, New York, USA') == \
        'syracuse, onondaga, new york, usa'


def test_unique_term_resolves_without_a_chain(index):
    result = index.resolve('Lowell', '')
    assert result.status == 'unique'
    assert result.uuid == 'U-LOW'


def test_accented_term_resolves_from_ascii_input(index):
    result = index.resolve('Montreal', 'Montreal, Quebec, Canada')
    assert result.uuid == 'U-MTL'


def test_ambiguous_term_resolves_via_proposed_chain(index):
    result = index.resolve('Syracuse', 'Syracuse, Onondaga, New York, USA')
    assert result.status == 'chain_matched'
    assert result.uuid == 'U-NY'


def test_ambiguous_term_resolves_on_partial_chain_overlap(index):
    result = index.resolve('Syracuse', 'Syracuse, New York, USA')
    assert result.status == 'chain_matched'
    assert result.uuid == 'U-NY'


def test_ambiguous_term_without_disambiguating_chain_needs_a_second_call(index):
    result = index.resolve('Syracuse', 'Syracuse, USA')
    assert result.status == 'needs_disambiguation'
    assert result.uuid is None
    assert len(result.candidates) == 3


def test_qualifier_mismatch_between_term_and_chain_still_matches():
    # PA disagrees with itself on qualifiers for 12.7% of rows.
    idx = PAIndex([
        row('Pike', 'U-PIKE-OH', 'Pike County, Ohio, USA', level='5'),
        row('Pike', 'U-PIKE-KY', 'Pike County, Kentucky, USA', level='5'),
    ])
    result = idx.resolve('Pike', 'Pike, Ohio, USA')
    assert result.status == 'chain_matched'
    assert result.uuid == 'U-PIKE-OH'


def test_absent_term_reports_absent(index):
    result = index.resolve('Beverly Hilton Hotel', 'Beverly Hilton Hotel, USA')
    assert result.status == 'absent'
    assert result.uuid is None
    assert result.candidates == []


def test_replaced_uuid_is_flagged_not_silently_followed(index):
    result = index.resolve('Oldtown', 'Oldtown, Kent, Maryland, USA')
    assert result.status == 'replaced'
    assert result.uuid is None


def test_mixed_replaced_and_live_candidates_drops_the_superseded_row():
    # A term can have some rows superseded and others still current. The
    # superseded row must never be returned as the answer, nor offered to
    # the model as a disambiguation candidate.
    idx = PAIndex([
        row('Taylor', 'U-TAYLOR-OLD', 'Taylor, Texas, USA',
            replacement='U-TAYLOR-NEW'),
        row('Taylor', 'U-TAYLOR-NEW', 'Taylor, Texas, USA'),
    ])
    result = idx.resolve('Taylor', 'Taylor, Texas, USA')
    assert result.uuid == 'U-TAYLOR-NEW'
    assert all(c.uuid != 'U-TAYLOR-OLD' for c in result.candidates)


def test_from_tsv_reads_the_real_column_order(tmp_path):
    path = tmp_path / 'pa.tsv'
    path.write_text(
        'Level\tLevelName\tReplacement_UUID\tTerm\tID\tHistorical\t'
        'FullChainName\tParentID\tPopulation\tLatitude\tLongitude\n'
        '4\tCity\t\tLowell\tU-LOW\t\tLowell, Middlesex, Massachusetts, USA\t'
        'U-MID\t106519\t42.6\t-71.3\n',
        encoding='utf-8')
    index = PAIndex.from_tsv(str(path))
    assert index.resolve('Lowell', '').uuid == 'U-LOW'
