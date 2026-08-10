"""Tests for MNT decontamination.

Every test here guards the same failure: a key form left behind means the
matcher answers an eval row from the dictionary and the score reads as
matching when it is recall of a lookup table. A missed variant is silent —
nothing downstream can tell the difference.
"""
import csv

from build_mnt_holdout import (
    exclusion_keys,
    filter_dict,
    filter_mnt,
    raw_of,
    read_places,
)
from mnt_keys import canonicalize_place, index_keys


def write_mnt(tmp_path, rows, name='mnt.tsv'):
    path = tmp_path / name
    body = ''.join(f'{v}\t{i}\t{r}\t{g}\n' for v, i, r, g in rows)
    path.write_text('_value\t_ID\t_raw\t_geoclass\n' + body, encoding='utf-8')
    return str(path)


def write_sample(tmp_path, places, name='sample.tsv'):
    path = tmp_path / name
    body = ''.join(f'{p}\tG{n}\t5\thead\n' for n, p in enumerate(places))
    path.write_text('# seed=42\nplace\tguid\tfrequency\tband\n' + body,
                    encoding='utf-8')
    return str(path)


def kept_raws(path):
    with open(path, encoding='utf-8', newline='') as f:
        return [r['_raw'] for r in csv.DictReader(f, delimiter='\t')]


# ---------------------------------------------------------------------------
# Key forms
# ---------------------------------------------------------------------------

def test_lowercase_form_is_a_key():
    assert 'malone, ny' in index_keys('Malone, NY')


def test_dehyphenated_form_is_a_key():
    # _load_mnt files every raw twice when it carries a hyphen.
    assert 'st jean, quebec' in index_keys('St-Jean, Quebec')


def test_canonical_form_is_a_key_for_comma_bearing_strings():
    assert canonicalize_place('A ,  B') in index_keys('A ,  B')


def test_a_comma_less_string_gets_no_full_string_key():
    # fs_by_raw is only built for strings carrying a comma or semicolon, so
    # claiming a canonical key here would overstate what has to be removed.
    assert index_keys('Illinois') == {'illinois'}


def test_semicolons_count_as_separators_too():
    assert canonicalize_place('A; B') in index_keys('A; B')


def test_an_empty_string_produces_no_keys():
    assert index_keys('') == set()
    assert index_keys(None) == set()


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------

def test_the_exact_eval_string_is_removed(tmp_path):
    mnt = write_mnt(tmp_path, [('Malone', 'U1', 'Malone, NY', 'USA'),
                               ('Peru', 'U2', 'Peru, VT', 'USA')])
    out = str(tmp_path / 'holdout.tsv')
    filter_mnt(mnt, out, exclusion_keys(['Malone, NY']))
    assert kept_raws(out) == ['Peru, VT']


def test_a_case_variant_of_the_eval_string_is_removed(tmp_path):
    # mnt_by_raw is keyed lowercase, so a differently-cased MNT row is the
    # same live path back to the answer.
    mnt = write_mnt(tmp_path, [('Malone', 'U1', 'MALONE, NY', 'USA')])
    out = str(tmp_path / 'holdout.tsv')
    filter_mnt(mnt, out, exclusion_keys(['Malone, NY']))
    assert kept_raws(out) == []


def test_a_spacing_variant_is_removed_through_the_canonical_key(tmp_path):
    # canonicalize_place collapses segment spacing, and fs_by_raw is keyed on
    # it, so 'A ,B' and 'A, B' are one entry in the fast path.
    mnt = write_mnt(tmp_path, [('A', 'U1', 'A ,B', 'USA')])
    out = str(tmp_path / 'holdout.tsv')
    filter_mnt(mnt, out, exclusion_keys(['A, B']))
    assert kept_raws(out) == []


def test_a_hyphen_variant_is_removed(tmp_path):
    mnt = write_mnt(tmp_path, [('St Jean', 'U1', 'St Jean, Quebec', 'Global')])
    out = str(tmp_path / 'holdout.tsv')
    filter_mnt(mnt, out, exclusion_keys(['St-Jean, Quebec']))
    assert kept_raws(out) == []


def test_every_duplicate_of_an_eval_string_goes_not_just_the_first(tmp_path):
    # 1.5% of MNT rows share a canonical form. Leaving the second copy would
    # leave the fast path intact.
    mnt = write_mnt(tmp_path, [('Malone', 'U1', 'Malone, NY', 'USA'),
                               ('Malone', 'U2', 'malone,  ny', 'Global'),
                               ('Peru', 'U3', 'Peru, VT', 'USA')])
    out = str(tmp_path / 'holdout.tsv')
    _, removed = filter_mnt(mnt, out, exclusion_keys(['Malone, NY']))
    assert removed == 2
    assert kept_raws(out) == ['Peru, VT']


def test_unrelated_rows_survive_untouched(tmp_path):
    mnt = write_mnt(tmp_path, [('Peru', 'U2', 'Peru, VT', 'USA')])
    out = str(tmp_path / 'holdout.tsv')
    kept, removed = filter_mnt(mnt, out, exclusion_keys(['Malone, NY']))
    assert (kept, removed) == (1, 0)


def test_the_header_and_all_columns_are_preserved(tmp_path):
    mnt = write_mnt(tmp_path, [('Peru', 'U2', 'Peru, VT', 'USA')])
    out = str(tmp_path / 'holdout.tsv')
    filter_mnt(mnt, out, set())
    with open(out, encoding='utf-8', newline='') as f:
        rows = list(csv.DictReader(f, delimiter='\t'))
    assert rows[0] == {'_value': 'Peru', '_ID': 'U2', '_raw': 'Peru, VT',
                       '_geoclass': 'USA'}


def test_an_authority_name_shared_with_other_rows_is_not_collateral(tmp_path):
    # Only the memorized full string goes. Other rows resolving to the same
    # place keep working, which is what makes the holdout a fair test rather
    # than a lobotomy.
    mnt = write_mnt(tmp_path, [('Malone', 'U1', 'Malone, NY', 'USA'),
                               ('Malone', 'U1', 'Malone Franklin NY', 'USA')])
    out = str(tmp_path / 'holdout.tsv')
    filter_mnt(mnt, out, exclusion_keys(['Malone, NY']))
    assert kept_raws(out) == ['Malone Franklin NY']


def test_reads_the_alternate_raw_column_name(tmp_path):
    # _load_mnt accepts InputString as a fallback for _raw; a holdout that
    # only knew _raw would pass an unfiltered table straight through.
    path = tmp_path / 'alt.tsv'
    path.write_text('MatchAuthName\tMatchAuthID\tInputString\n'
                    'Malone\tU1\tMalone, NY\n', encoding='utf-8')
    with open(path, encoding='utf-8', newline='') as f:
        row = next(csv.DictReader(f, delimiter='\t'))
    assert raw_of(row) == 'Malone, NY'


# ---------------------------------------------------------------------------
# Sample reading
# ---------------------------------------------------------------------------

def test_read_places_skips_the_provenance_comment(tmp_path):
    path = write_sample(tmp_path, ['Malone, NY', 'Peru, VT'])
    assert read_places(path) == ['Malone, NY', 'Peru, VT']


def test_keys_accumulate_across_both_halves(tmp_path):
    # Held-out strings must leave the dictionary too, or the first held-out
    # run is scored against a matcher that memorized them.
    keys = exclusion_keys(['Malone, NY', 'Peru, VT'])
    assert 'malone, ny' in keys and 'peru, vt' in keys


# ---------------------------------------------------------------------------
# Dictionary filtering
# ---------------------------------------------------------------------------

def write_dict(tmp_path, terms, illegible=('xxxx', 'zzz')):
    d = tmp_path / 'dict'
    d.mkdir()
    (d / 'place_term_dictionary.tsv').write_text(
        'term\tauthority_uuid\tfrequency\n'
        + ''.join(f'{t}\t{u}\t{f}\n' for t, u, f in terms), encoding='utf-8')
    (d / 'place_term_illegible.tsv').write_text(
        'term\n' + ''.join(f'{t}\n' for t in illegible), encoding='utf-8')
    return str(d)


def read_dict(path):
    with open(path, encoding='utf-8', newline='') as f:
        return list(csv.DictReader(f, delimiter='\t'))


def test_a_dictionary_term_matching_an_eval_string_is_dropped(tmp_path):
    # _ingest_dict_row files dict terms into mnt_by_raw, so an unfiltered
    # dictionary hands back the single-term lookup the MNT filter removed.
    src = write_dict(tmp_path, [('Malone', 'U1', '90'), ('Peru', 'U2', '40')])
    out = str(tmp_path / 'dict_holdout')
    kept, removed = filter_dict(src, out, exclusion_keys(['Malone']))
    assert (kept, removed) == (1, 1)
    rows = read_dict(f'{out}/place_term_dictionary.tsv')
    assert [r['term'] for r in rows] == ['Peru']


def test_an_unrelated_dictionary_term_survives(tmp_path):
    src = write_dict(tmp_path, [('Peru', 'U2', '40')])
    out = str(tmp_path / 'dict_holdout')
    assert filter_dict(src, out, exclusion_keys(['Malone, NY'])) == (1, 0)


def test_the_frequency_column_survives_the_filter(tmp_path):
    # dict_freq is the whole point of loading the dictionary; a dropped or
    # mangled frequency silently disables _disambiguate_by_frequency.
    src = write_dict(tmp_path, [('Peru', 'U2', '40')])
    out = str(tmp_path / 'dict_holdout')
    filter_dict(src, out, set())
    assert read_dict(f'{out}/place_term_dictionary.tsv')[0] == {
        'term': 'Peru', 'authority_uuid': 'U2', 'frequency': '40'}


def test_the_illegible_list_is_copied_untouched(tmp_path):
    # Junk terms carry no authority mapping, so they cannot leak an answer.
    # Thinning the list would re-enable spelling correction on junk.
    src = write_dict(tmp_path, [('Peru', 'U2', '40')], illegible=('peru', 'qqq'))
    out = str(tmp_path / 'dict_holdout')
    filter_dict(src, out, exclusion_keys(['Peru']))
    assert (open(f'{out}/place_term_illegible.tsv', encoding='utf-8').read()
            == 'term\nperu\nqqq\n')


def test_dictionary_filtering_is_case_insensitive(tmp_path):
    src = write_dict(tmp_path, [('MALONE', 'U1', '90')])
    out = str(tmp_path / 'dict_holdout')
    assert filter_dict(src, out, exclusion_keys(['Malone']))[1] == 1
