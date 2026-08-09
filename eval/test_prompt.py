"""Tests for prompt construction and response parsing."""
import pytest

from prompt import (
    build_disambiguate_prompt,
    build_identify_prompt,
    parse_disambiguate_response,
    parse_identify_response,
    redact_for_transport,
)


def test_redaction_strips_a_house_number_from_a_street():
    assert redact_for_transport('6314 Stewart ave, Illinois') == \
        'Stewart ave, Illinois'
    assert redact_for_transport('63 Gaping Rock Road, Levittown') == \
        'Gaping Rock Road, Levittown'


def test_redaction_spares_real_numeric_place_names():
    # PA holds 12 of these; none may be damaged.
    assert redact_for_transport('100 Mile House, British Columbia') == \
        '100 Mile House, British Columbia'
    assert redact_for_transport('16th Street Baptist Church, Birmingham') == \
        '16th Street Baptist Church, Birmingham'


def test_redaction_leaves_non_street_leaves_alone():
    assert redact_for_transport('64 Club, Council Bluffs') == \
        '64 Club, Council Bluffs'


def test_redaction_only_touches_the_leaf():
    assert redact_for_transport('Chicago, 100 Mile House') == \
        'Chicago, 100 Mile House'


def test_redaction_is_a_noop_on_ordinary_places():
    assert redact_for_transport('Syracuse, New York, United States of America') \
        == 'Syracuse, New York, United States of America'


def test_identify_prompt_redacts_before_sending():
    text = build_identify_prompt([{'place': '6314 Stewart ave, Illinois'}])
    assert '6314' not in text
    assert 'Stewart ave, Illinois' in text


def test_disambiguate_prompt_redacts_before_sending():
    text = build_disambiguate_prompt('6314 Stewart ave, Illinois', ['a', 'b'])
    assert '6314' not in text


def test_identify_prompt_lists_every_place_numbered():
    text = build_identify_prompt([
        {'place': 'Syracuse, New York, United States of America'},
        {'place': 'Bethel Lutheran church, Chicago'},
    ])
    assert '1. Syracuse, New York, United States of America' in text
    assert '2. Bethel Lutheran church, Chicago' in text


def test_identify_prompt_states_the_two_column_rule():
    text = build_identify_prompt([{'place': 'x'}])
    assert 'string_only' in text and 'world' in text


def test_parse_identify_returns_one_entry_per_input():
    text = """```json
[{"n": 1, "leaf_string_only": "Syracuse",
  "chain_string_only": "Syracuse, Onondaga, New York, USA",
  "leaf_world": "Syracuse",
  "chain_world": "Syracuse, Onondaga, New York, USA"},
 {"n": 2, "leaf_string_only": "Chicago",
  "chain_string_only": "Chicago, Cook, Illinois, USA",
  "leaf_world": "Bethel Lutheran Church",
  "chain_world": "Chicago, Cook, Illinois, USA"}]
```"""
    got = parse_identify_response(text, expected=2)
    assert len(got) == 2
    assert got[0]['leaf_string_only'] == 'Syracuse'
    assert got[1]['leaf_world'] == 'Bethel Lutheran Church'


def test_parse_identify_honours_n_over_position():
    # A model that returns entries out of order must not shift every label
    # onto the wrong input row.
    text = ('[{"n": 2, "leaf_string_only": "Second", "chain_string_only": "",'
            ' "leaf_world": "", "chain_world": ""},'
            ' {"n": 1, "leaf_string_only": "First", "chain_string_only": "",'
            ' "leaf_world": "", "chain_world": ""}]')
    got = parse_identify_response(text, expected=2)
    assert got[0]['leaf_string_only'] == 'First'
    assert got[1]['leaf_string_only'] == 'Second'


def test_parse_identify_pads_short_responses_with_none():
    text = ('[{"n": 1, "leaf_string_only": "Syracuse", '
            '"chain_string_only": "Syracuse, Onondaga, New York, USA", '
            '"leaf_world": "Syracuse", '
            '"chain_world": "Syracuse, Onondaga, New York, USA"}]')
    got = parse_identify_response(text, expected=3)
    assert len(got) == 3
    assert got[2]['leaf_string_only'] == ''


def test_parse_identify_raises_on_unparseable_text():
    with pytest.raises(ValueError):
        parse_identify_response('the model apologised instead', expected=1)


def test_disambiguate_prompt_numbers_candidates_from_one():
    text = build_disambiguate_prompt(
        'Syracuse, New York, United States of America',
        ['Syracuse, Onondaga, New York, USA', 'Syracuse, Davis, Utah, USA'])
    assert '1. Syracuse, Onondaga, New York, USA' in text
    assert '2. Syracuse, Davis, Utah, USA' in text


def test_parse_disambiguate_returns_zero_based_index():
    assert parse_disambiguate_response('2', n=3) == 1


def test_parse_disambiguate_returns_none_for_out_of_range():
    assert parse_disambiguate_response('9', n=3) is None


def test_parse_disambiguate_returns_none_for_refusal():
    assert parse_disambiguate_response('NONE', n=3) is None
