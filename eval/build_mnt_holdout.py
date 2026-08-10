"""Strip the eval strings out of the MNT the matcher loads.

Without this the MNT eval measures nothing. `rtl_matcher` reads the Master
Normalization Table as its dictionary, so a 5k set drawn from that table hits
`mnt_by_raw` on 100% of rows and the full-string fast path on 86.7%. For
contrast, `snowball2_sample_5k` hits the fast path on 0.1% — that gap is
memorization, not matching.

Removing a row takes its `_value` contribution with it, which is correct. The
authority name survives in the PA export and in every other MNT row that
resolves to it, so name resolution still works; what leaves is the memorized
full-string answer.

`--dict` needs the same treatment, which `--dict-in`/`--dict-out` do here.
The place dictionary unions into `mnt_by_raw` through `_ingest_dict_row`
(`rtl_matcher.py:480`), so an unfiltered dictionary hands back single-term
lookups the MNT filter just removed. The exposure stops there: `fs_tmp`, and
so `fs_by_raw` and `_full_string_fast_path`, are built only inside
`_load_mnt`, which means no dictionary can restore the full-string path.

Running the eval without `--dict` is the wrong way to close that. It also
switches off the frequency prior behind `_disambiguate_by_frequency` and the
illegible stop-list, which costs far more than the leak it prevents.

One path left open: `--mnt-defects` accumulates across runs. Point it at a
scratch file so eval rows do not land in the shared defect list.
"""
import argparse
import csv
import os
import shutil
import sys

from mnt_keys import index_keys

csv.field_size_limit(sys.maxsize)

DEFAULT_MNT = ('/Users/natelemonnier/storied/resources/'
               'place-authority-mnt-tsv/Master_Normalization_File_6_16_2026v1.tsv')
DEFAULT_SAMPLES = ('eval/data/mnt_dev.tsv', 'eval/data/mnt_heldout.tsv')
DEFAULT_OUT = 'eval/data/mnt_holdout.tsv'
DEFAULT_DICT_IN = 'eval/data/dict'
DEFAULT_DICT_OUT = 'eval/data/dict_holdout'

DICT_FILE = 'place_term_dictionary.tsv'
ILLEGIBLE_FILE = 'place_term_illegible.tsv'


def read_places(path):
    """Place strings from an eval input TSV, skipping the provenance comment."""
    with open(path, encoding='utf-8', newline='') as f:
        lines = [ln for ln in f if not ln.startswith('#')]
    return [(r.get('place') or '').strip()
            for r in csv.DictReader(lines, delimiter='\t')
            if (r.get('place') or '').strip()]


def exclusion_keys(places):
    """Every index key the eval strings would be reachable through."""
    keys = set()
    for place in places:
        keys |= index_keys(place)
    return keys


def raw_of(row):
    """The MNT input string, under either column name `_load_mnt` accepts."""
    return (row.get('_raw') or row.get('InputString') or '').strip()


def filter_mnt(mnt_path, out_path, keys):
    """Copy the MNT minus every row reachable from an eval string.

    Streams: the table is 1.4M rows and there is no reason to hold it.
    """
    os.makedirs(os.path.dirname(out_path) or '.', exist_ok=True)
    kept = removed = 0
    with open(mnt_path, encoding='utf-8-sig', newline='') as fin, \
            open(out_path, 'w', encoding='utf-8', newline='') as fout:
        reader = csv.DictReader(fin, delimiter='\t')
        writer = csv.DictWriter(fout, fieldnames=reader.fieldnames,
                                delimiter='\t', extrasaction='ignore')
        writer.writeheader()
        for row in reader:
            if index_keys(raw_of(row)) & keys:
                removed += 1
                continue
            writer.writerow(row)
            kept += 1
    return kept, removed


def filter_dict(dict_in, dict_out, keys):
    """Copy the `--dict` directory minus terms an eval string would reach.

    Only `place_term_dictionary.tsv` is filtered. The illegible list is copied
    verbatim: it holds junk terms with no authority mapping, so it cannot leak
    an answer, and thinning it would only re-enable the spelling corrections
    on junk that having it loaded exists to suppress.
    """
    os.makedirs(dict_out, exist_ok=True)
    kept = removed = 0
    src = os.path.join(dict_in, DICT_FILE)
    with open(src, encoding='utf-8-sig', newline='') as fin, \
            open(os.path.join(dict_out, DICT_FILE), 'w',
                 encoding='utf-8', newline='') as fout:
        reader = csv.DictReader(fin, delimiter='\t')
        writer = csv.DictWriter(fout, fieldnames=reader.fieldnames,
                                delimiter='\t', extrasaction='ignore')
        writer.writeheader()
        for row in reader:
            if index_keys(row.get('term') or '') & keys:
                removed += 1
                continue
            writer.writerow(row)
            kept += 1

    ill_src = os.path.join(dict_in, ILLEGIBLE_FILE)
    if os.path.exists(ill_src):
        shutil.copyfile(ill_src, os.path.join(dict_out, ILLEGIBLE_FILE))
    return kept, removed


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--sample', nargs='+', default=list(DEFAULT_SAMPLES),
                   help='eval input TSVs whose strings must not be in the MNT')
    p.add_argument('--mnt', default=DEFAULT_MNT)
    p.add_argument('--out', default=DEFAULT_OUT)
    p.add_argument('--dict-in', default=DEFAULT_DICT_IN,
                   help='directory written by eval/export_dict.py')
    p.add_argument('--dict-out', default=DEFAULT_DICT_OUT)
    p.add_argument('--no-dict', action='store_true',
                   help='skip the dictionary; the MNT filter still runs')
    args = p.parse_args(argv)

    places = []
    for path in args.sample:
        places.extend(read_places(path))
    keys = exclusion_keys(places)

    kept, removed = filter_mnt(args.mnt, args.out, keys)
    print(f'{len(places)} eval strings -> {len(keys)} index keys')
    print(f'MNT  {kept + removed} rows -> {kept} kept, {removed} removed')
    if removed < len(places):
        print(f'note: {len(places) - removed} eval strings had no MNT row to '
              f'remove', file=sys.stderr)

    if not args.no_dict:
        if not os.path.exists(os.path.join(args.dict_in, DICT_FILE)):
            print(f'no dictionary at {args.dict_in}; run eval/export_dict.py',
                  file=sys.stderr)
            return 1
        d_kept, d_removed = filter_dict(args.dict_in, args.dict_out, keys)
        print(f'dict {d_kept + d_removed} terms -> {d_kept} kept, '
              f'{d_removed} removed')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
