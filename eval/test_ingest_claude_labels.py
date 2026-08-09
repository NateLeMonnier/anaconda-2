"""Tests for batching the sample and ingesting subagent-produced labels."""
import json

from ingest_claude_labels import load_responses, rows_from_responses
from labels import LABEL_FIELDS, NONE
from make_label_batches import write_batches
from pa_index import PAIndex, PARow


def index():
    return PAIndex([
        PARow('4', 'City', 'U-NY', 'Syracuse',
              'Syracuse, Onondaga, New York, USA', '', '0', ''),
        PARow('4', 'City', 'U-CHI', 'Chicago',
              'Chicago, Cook, Illinois, USA', '', '0', ''),
    ])


SAMPLE = [
    {'guid': 'G1', 'place': 'Syracuse, New York, United States of America',
     'band': 'head'},
    {'guid': 'G2', 'place': 'Bethel Lutheran church, Chicago', 'band': 'tail'},
]


def test_rows_carry_the_full_label_schema():
    responses = {'G1': {'leaf_string_only': 'Syracuse',
                        'chain_string_only': 'Syracuse, Onondaga, New York, USA',
                        'leaf_world': 'Syracuse',
                        'chain_world': 'Syracuse, Onondaga, New York, USA'}}
    rows = rows_from_responses(SAMPLE, responses, index())
    assert list(rows[0].keys()) == LABEL_FIELDS
    assert rows[0]['label_string_only'] == 'U-NY'


def test_absent_leaf_climbs_to_the_named_jurisdiction():
    responses = {'G2': {'leaf_string_only': 'Bethel Lutheran Church',
                        'chain_string_only':
                            'Bethel Lutheran Church, Chicago, Cook, Illinois, USA',
                        'leaf_world': 'Bethel Lutheran Church',
                        'chain_world':
                            'Bethel Lutheran Church, Chicago, Cook, Illinois, USA'}}
    rows = rows_from_responses(SAMPLE, responses, index())
    g2 = next(r for r in rows if r['guid'] == 'G2')
    assert g2['label_string_only'] == 'U-CHI'


def test_missing_response_becomes_none_not_a_dropped_row():
    rows = rows_from_responses(SAMPLE, {}, index())
    assert len(rows) == 2
    assert all(r['label_string_only'] == NONE for r in rows)


def test_load_responses_merges_batches_and_keys_on_guid(tmp_path):
    (tmp_path / 'batch_001.json').write_text(json.dumps([
        {'guid': 'G1', 'leaf_string_only': 'Syracuse',
         'chain_string_only': 'Syracuse, Onondaga, New York, USA',
         'leaf_world': 'Syracuse', 'chain_world': 'Syracuse, Onondaga, New York, USA'}
    ]), encoding='utf-8')
    (tmp_path / 'batch_002.json').write_text(json.dumps([
        {'guid': 'G2', 'leaf_string_only': 'Chicago',
         'chain_string_only': 'Chicago, Cook, Illinois, USA',
         'leaf_world': 'Chicago', 'chain_world': 'Chicago, Cook, Illinois, USA'}
    ]), encoding='utf-8')
    got = load_responses(str(tmp_path / '*.json'))
    assert set(got) == {'G1', 'G2'}
    assert got['G2']['leaf_string_only'] == 'Chicago'


def test_load_responses_skips_entries_without_a_guid(tmp_path):
    (tmp_path / 'batch_001.json').write_text(json.dumps([
        {'leaf_string_only': 'Orphan', 'chain_string_only': '',
         'leaf_world': '', 'chain_world': ''}
    ]), encoding='utf-8')
    assert load_responses(str(tmp_path / '*.json')) == {}


def test_batch_files_carry_the_guids_and_redact_addresses(tmp_path):
    sample = [{'guid': 'G9', 'place': '6314 Stewart ave, Illinois', 'band': 'tail'}]
    paths = write_batches(sample, str(tmp_path))
    text = open(paths[0], encoding='utf-8').read()
    assert 'G9' in text
    assert '6314' not in text
    assert 'Stewart ave, Illinois' in text
