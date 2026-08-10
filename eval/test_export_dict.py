"""Tests for the place dictionary export."""
import csv
import os

from export_dict import (
    DICT_FILE,
    ILLEGIBLE_FILE,
    cached,
    export,
    write_dict_tsv,
    write_illegible_tsv,
)


class FakeCursor:
    """Stands in for psycopg2: execute() selects which rowset to iterate."""

    def __init__(self, dict_rows, illegible_rows):
        self._sets = {'place_term_dictionary': dict_rows,
                      'place_term_illegible': illegible_rows}
        self._rows = []
        self.queries = []

    def execute(self, sql):
        self.queries.append(sql)
        for table, rows in self._sets.items():
            if table in sql:
                self._rows = list(rows)
                return

    def __iter__(self):
        return iter(self._rows)


def read(path):
    with open(path, encoding='utf-8', newline='') as f:
        return list(csv.DictReader(f, delimiter='\t'))


def test_writes_the_columns_load_dict_tsv_reads(tmp_path):
    # _load_dict_tsv keys on exactly these three names; a rename here loads
    # zero mappings and silently disables the frequency prior.
    path = str(tmp_path / DICT_FILE)
    write_dict_tsv(path, [('Malone', 'u-1', 90)])
    assert read(path)[0] == {'term': 'Malone', 'authority_uuid': 'U-1',
                             'frequency': '90'}


def test_uuids_are_upper_cased_to_match_the_matcher(tmp_path):
    path = str(tmp_path / DICT_FILE)
    write_dict_tsv(path, [('Malone', 'abc-def', 1)])
    assert read(path)[0]['authority_uuid'] == 'ABC-DEF'


def test_a_missing_frequency_becomes_zero_not_an_error(tmp_path):
    path = str(tmp_path / DICT_FILE)
    write_dict_tsv(path, [('Malone', 'U1', None)])
    assert read(path)[0]['frequency'] == '0'


def test_rows_without_a_term_or_uuid_are_skipped(tmp_path):
    path = str(tmp_path / DICT_FILE)
    assert write_dict_tsv(path, [('', 'U1', 1), ('Malone', '', 1),
                                 ('Peru', 'U2', 1)]) == 1


def test_illegible_terms_write_one_column(tmp_path):
    path = str(tmp_path / ILLEGIBLE_FILE)
    write_illegible_tsv(path, [('4099',), ('menlina',)])
    assert [r['term'] for r in read(path)] == ['4099', 'menlina']


def test_illegible_accepts_bare_strings_as_well_as_rows(tmp_path):
    path = str(tmp_path / ILLEGIBLE_FILE)
    write_illegible_tsv(path, ['4099'])
    assert read(path)[0]['term'] == '4099'


def test_export_runs_both_queries_and_writes_both_files(tmp_path):
    cur = FakeCursor([('Malone', 'U1', 90)], [('4099',)])
    n_dict, n_ill = export(str(tmp_path), cur)
    assert (n_dict, n_ill) == (1, 1)
    assert len(cur.queries) == 2
    assert os.path.exists(tmp_path / DICT_FILE)
    assert os.path.exists(tmp_path / ILLEGIBLE_FILE)


def test_cached_needs_both_files_present(tmp_path):
    assert not cached(str(tmp_path))
    write_dict_tsv(str(tmp_path / DICT_FILE), [('A', 'U1', 1)])
    assert not cached(str(tmp_path))
    write_illegible_tsv(str(tmp_path / ILLEGIBLE_FILE), ['x'])
    assert cached(str(tmp_path))
