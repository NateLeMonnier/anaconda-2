"""Tests for the FileMaker pool draw.

The offset planner is the part worth testing hardest: a bug there quietly
biases the pool toward one region of the found set, which is one era of
curation, and nothing downstream would notice.
"""
import io
import json
import urllib.error

import pytest

from build_mnt_pool import (
    FileMaker,
    dedupe_rows,
    draw_band,
    page_offsets,
    read_pool,
    record_to_row,
    write_pool,
)


# ---------------------------------------------------------------------------
# page_offsets
# ---------------------------------------------------------------------------

def test_walks_the_whole_band_when_it_is_smaller_than_the_request():
    assert page_offsets(found_count=250, want=1000, page=100) == [1, 101, 201]


def test_covers_at_least_the_requested_count():
    offsets = page_offsets(found_count=100000, want=3000, page=100)
    assert len(offsets) * 100 >= 3000


def test_pages_never_overlap():
    offsets = page_offsets(found_count=100000, want=3000, page=100)
    assert all(b - a >= 100 for a, b in zip(offsets, offsets[1:]))


def test_pages_stride_across_the_whole_found_set_not_just_the_front():
    # FileMaker orders a found set by internal record id, which tracks
    # insertion order and therefore source project. Reading from the front
    # would sample one era of curation.
    offsets = page_offsets(found_count=100000, want=3000, page=100)
    assert offsets[-1] > 90000


def test_last_page_stays_inside_the_found_set():
    offsets = page_offsets(found_count=1000, want=500, page=100)
    assert offsets[-1] + 100 - 1 <= 1000


def test_offsets_are_one_based():
    assert page_offsets(found_count=1000, want=100, page=100)[0] >= 1


def test_is_deterministic_under_a_seed():
    a = page_offsets(found_count=100000, want=3000, page=100, seed=42)
    b = page_offsets(found_count=100000, want=3000, page=100, seed=42)
    assert a == b


def test_a_different_seed_moves_the_pages():
    a = page_offsets(found_count=100000, want=3000, page=100, seed=42)
    b = page_offsets(found_count=100000, want=3000, page=100, seed=7)
    assert a != b


def test_dense_request_falls_back_to_sequential_pages():
    # Requested share is high enough that strided pages would overlap.
    assert page_offsets(found_count=101, want=90, page=40) == [1, 41, 81]


def test_empty_inputs_ask_for_nothing():
    assert page_offsets(found_count=0, want=100, page=10) == []
    assert page_offsets(found_count=100, want=0, page=10) == []


# ---------------------------------------------------------------------------
# Row shaping
# ---------------------------------------------------------------------------

def test_record_to_row_maps_filemaker_names_onto_pool_columns():
    row = record_to_row({'Input_formatted': 'Malone, NY',
                         'Match_Authority_ID': 'U1',
                         'Total': 7}, 'low')
    assert row['place'] == 'Malone, NY'
    assert row['authority_id'] == 'U1'
    assert row['total'] == '7'      # Total arrives as a JSON number
    assert row['band'] == 'low'


def test_record_to_row_fills_absent_fields_rather_than_raising():
    row = record_to_row({'Input_formatted': 'Malone, NY'}, 'low')
    assert row['typeahead'] == ''
    assert row['project'] == ''


def test_dedupe_keeps_first_seen_and_ignores_case():
    rows = [{'place': 'Malone, NY'}, {'place': 'malone, ny'},
            {'place': 'Peru, VT'}]
    assert [r['place'] for r in dedupe_rows(rows)] == ['Malone, NY', 'Peru, VT']


def test_pool_survives_a_write_read_round_trip(tmp_path):
    path = str(tmp_path / 'pool.tsv')
    rows = [record_to_row({'Input_formatted': 'Malone, NY',
                           'Match_Authority_ID': 'U1', 'Total': 7}, 'low')]
    write_pool(path, rows, seed=42)
    assert read_pool(path) == rows


def test_pool_header_comment_is_skipped_on_read(tmp_path):
    path = str(tmp_path / 'pool.tsv')
    write_pool(path, [record_to_row({'Input_formatted': 'A'}, 'low')], seed=42)
    assert read_pool(path)[0]['place'] == 'A'


# ---------------------------------------------------------------------------
# Data API client
# ---------------------------------------------------------------------------

def fake_opener(payloads):
    """Return an opener yielding each payload in turn; raises get raised."""
    queue = list(payloads)

    def opener(_request):
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return io.StringIO(json.dumps(item))
    return opener


def response(records, found):
    return {'response': {'data': [{'fieldData': r} for r in records],
                         'dataInfo': {'foundCount': found}}}


def test_find_returns_records_and_the_found_count():
    client = FileMaker('http://h', 'db', 'tok',
                       opener=fake_opener([response([{'a': 1}], 42)]))
    records, found = client.find([{'Total': '>=1000'}])
    assert found == 42
    assert records[0]['fieldData'] == {'a': 1}


def test_a_find_matching_nothing_is_an_empty_result_not_an_error():
    # FileMaker answers a no-match find with HTTP 404 and its own code 401.
    not_found = urllib.error.HTTPError('u', 404, 'Not Found', {}, None)
    client = FileMaker('http://h', 'db', 'tok',
                       opener=fake_opener([not_found]))
    assert client.find([{'Total': '>=1000'}]) == ([], 0)


def test_a_transient_failure_is_retried():
    flaky = urllib.error.URLError('connection reset')
    client = FileMaker('http://h', 'db', 'tok',
                       opener=fake_opener([flaky, response([{'a': 1}], 1)]),
                       sleeper=lambda s: None)
    _, found = client.find([{'Total': '>=1000'}])
    assert found == 1


def test_retries_give_up_rather_than_looping_forever():
    errs = [urllib.error.URLError('down')] * 8
    client = FileMaker('http://h', 'db', 'tok', opener=fake_opener(errs),
                       sleeper=lambda s: None)
    with pytest.raises(urllib.error.URLError):
        client.find([{'Total': '>=1000'}])


def test_draw_band_stops_at_the_requested_count():
    pages = [response([{'Input_formatted': f'P{i}'} for i in range(100)], 5000)]
    pages += [response([{'Input_formatted': f'P{i}-{p}'} for i in range(100)],
                       5000) for p in range(50)]
    client = FileMaker('http://h', 'db', 'tok', opener=fake_opener(pages))
    rows, found = draw_band(client, 'head', '>=100000', want=250, page=100,
                            seed=42, log=lambda m: None)
    assert found == 5000
    assert len(rows) == 250
    assert all(r['band'] == 'head' for r in rows)
