"""Tests for the leaf-first walk and the labeling loop.

The loop is tested with a fake transport, so no API key and no network are
needed here.
"""
import json

import pytest

from label_gemini import label_rows, resolve_with_walk
from labels import LABEL_FIELDS, NONE
from pa_index import PAIndex, PARow


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
        row('Chicago', 'U-CHI', 'Chicago, Cook, Illinois, USA'),
    ])


def test_walk_resolves_a_leaf_that_exists(index):
    uuid, status = resolve_with_walk(index, 'Chicago', 'Chicago, Cook, Illinois, USA')
    assert uuid == 'U-CHI'
    assert status == 'unique'


def test_walk_climbs_past_an_absent_leaf(index):
    uuid, status = resolve_with_walk(
        index, 'Bethel Lutheran Church',
        'Bethel Lutheran Church, Onondaga, New York, USA')
    assert uuid == 'U-ONO'
    assert status in ('unique', 'chain_matched')


def test_walk_stops_at_the_deepest_existing_node(index):
    # Syracuse is present, so the walk must not climb past it to Onondaga.
    uuid, _ = resolve_with_walk(index, 'Syracuse',
                                'Syracuse, Onondaga, New York, USA')
    assert uuid == 'U-NY'


def test_walk_returns_none_when_nothing_in_the_chain_exists(index):
    uuid, status = resolve_with_walk(index, 'Nowhere', 'Nowhere, Nohow')
    assert uuid == NONE
    assert status == 'none_after_walk'


def test_walk_returns_none_on_empty_input(index):
    assert resolve_with_walk(index, '', '') == (NONE, 'none_after_walk')


def test_walk_asks_the_model_only_when_the_chain_cannot_separate(index):
    asked = []

    def ask(place, chains):
        asked.append((place, chains))
        return 0

    uuid, status = resolve_with_walk(index, 'Syracuse', 'Syracuse, USA', ask)
    assert asked
    assert status == 'model_disambiguated'
    assert uuid in ('U-NY', 'U-UT', 'U-KS')


def test_walk_does_not_ask_when_the_chain_already_separates(index):
    asked = []
    resolve_with_walk(index, 'Syracuse', 'Syracuse, Onondaga, New York, USA',
                      lambda p, c: asked.append(1))
    assert not asked


def test_walk_without_an_ask_callback_falls_through_to_none(index):
    # ingest_claude_labels resolves with no callback; an unseparable leaf must
    # become a review row rather than a guess.
    uuid, status = resolve_with_walk(index, 'Syracuse', 'Syracuse, USA')
    assert uuid == NONE
    assert status == 'none_after_walk'


SAMPLE = [
    {'guid': 'G1', 'place': 'Syracuse, New York, United States of America',
     'band': 'head'},
    {'guid': 'G2', 'place': 'Bethel Lutheran church, Chicago', 'band': 'tail'},
]


def test_label_rows_produces_the_full_schema_for_every_row(index):
    def call(text):
        return json.dumps([
            {'n': 1, 'leaf_string_only': 'Syracuse',
             'chain_string_only': 'Syracuse, Onondaga, New York, USA',
             'leaf_world': 'Syracuse',
             'chain_world': 'Syracuse, Onondaga, New York, USA'},
            {'n': 2, 'leaf_string_only': 'Chicago',
             'chain_string_only': 'Chicago, Cook, Illinois, USA',
             'leaf_world': 'Bethel Lutheran Church',
             'chain_world': 'Chicago, Cook, Illinois, USA'},
        ])

    rows = label_rows(SAMPLE, index, call)
    assert len(rows) == 2
    assert list(rows[0].keys()) == LABEL_FIELDS
    assert rows[0]['label_string_only'] == 'U-NY'
    assert rows[1]['label_string_only'] == 'U-CHI'
    assert rows[1]['label_world'] == 'U-CHI'


def test_label_rows_survives_a_model_error_without_losing_rows(index):
    def call(text):
        raise RuntimeError('rate limited')

    rows = label_rows(SAMPLE, index, call)
    assert len(rows) == 2
    assert all(r['label_string_only'] == NONE for r in rows)
    assert all(r['status_string_only'] == 'none_after_walk' for r in rows)
