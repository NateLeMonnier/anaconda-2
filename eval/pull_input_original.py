"""Pull `Input_Original` for the eval rows and diff it against `Input_formatted`.

The eval set feeds the matcher `Input_formatted`, the MNT's cleaned input
string (`build_mnt_pool.py:57`). The table also holds `Input_Original`, the
raw string from source data. Nothing documents that choice, and if the two
differ materially then the MNT numbers were measured on strings production
never sees.

This answers the question rather than assuming it either way: fetch both
fields for every eval row, report how often they differ and how, and write an
input TSV carrying the original string under the same guid so the labels still
join.

Reads only. Batches finds because the Data API takes an array of query objects
as an OR, so 2,500 strings cost tens of requests rather than thousands.
"""
import argparse
import csv
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
csv.field_size_limit(sys.maxsize)

from build_mnt_pool import FileMaker, LAYOUT, load_env  # noqa: E402

# FileMaker treats these as find operators; a literal one has to be escaped.
FM_OPERATORS = '@*#?!=<>"~'


def escape(value):
    out = []
    for ch in value:
        if ch in FM_OPERATORS or ch == '\\':
            out.append('\\')
        out.append(ch)
    return ''.join(out)


def read_input(path):
    with open(path, encoding='utf-8', newline='') as f:
        lines = [l for l in f if not l.startswith('#')]
    return list(csv.DictReader(lines, delimiter='\t'))


def fetch(client, places, batch=40, log=print):
    """{Input_formatted: Input_Original} for the strings that resolve."""
    found = {}
    for i in range(0, len(places), batch):
        chunk = places[i:i + batch]
        query = [{'Input_formatted': f'=={escape(p)}'} for p in chunk]
        records, _ = client.find(query, limit=len(chunk) * 20)
        for rec in records:
            fd = rec.get('fieldData', {})
            fmt = (fd.get('Input_formatted') or '').strip()
            orig = (fd.get('Input_Original') or '').strip()
            if fmt and fmt not in found:
                found[fmt] = orig
        log(f'  {min(i + batch, len(places))}/{len(places)} queried, '
            f'{len(found)} resolved')
    return found


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--input', default='eval/data/mnt_dev.tsv')
    p.add_argument('--env', default='/Users/natelemonnier/storied/code/'
                                    'place-normalizer/.env')
    p.add_argument('--out', default='eval/data/mnt_dev_original.tsv')
    p.add_argument('--report', default='eval/data/mnt_dev_original_diff.tsv')
    p.add_argument('--batch', type=int, default=40)
    args = p.parse_args(argv)

    rows = read_input(args.input)
    load_env(args.env)
    missing = [k for k in ('FILEMAKER_HOST', 'FILEMAKER_DATABASE',
                           'FILEMAKER_USERNAME', 'FILEMAKER_PASSWORD')
               if not os.environ.get(k)]
    if missing:
        print(f'missing credentials: {", ".join(missing)}', file=sys.stderr)
        return 1

    client = FileMaker.connect(os.environ['FILEMAKER_HOST'],
                               os.environ['FILEMAKER_DATABASE'],
                               os.environ['FILEMAKER_USERNAME'],
                               os.environ['FILEMAKER_PASSWORD'])
    print(f'layout {LAYOUT}, {len(rows)} rows')

    places = [r['place'] for r in rows]
    found = fetch(client, places, batch=args.batch)

    unresolved = [p for p in places if p not in found]
    differ = [(p, found[p]) for p in places
              if p in found and found[p] and found[p] != p]
    same = sum(1 for p in places if p in found and found[p] == p)
    blank = sum(1 for p in places if p in found and not found[p])

    print()
    print(f'resolved in FileMaker   {len(found):,} of {len(places):,}')
    print(f'  Input_Original == Input_formatted   {same:,}')
    print(f'  Input_Original differs              {len(differ):,}')
    print(f'  Input_Original blank                {blank:,}')
    print(f'unresolved              {len(unresolved):,}')

    if differ:
        print('\nfirst differences:')
        for fmt, orig in differ[:15]:
            print(f'  formatted: {fmt!r}')
            print(f'  original : {orig!r}')

    with open(args.report, 'w', encoding='utf-8', newline='') as f:
        w = csv.writer(f, delimiter='\t')
        w.writerow(['guid', 'input_formatted', 'input_original', 'state'])
        for r in rows:
            pl = r['place']
            if pl not in found:
                state = 'unresolved'
                orig = ''
            else:
                orig = found[pl]
                state = ('blank' if not orig
                         else 'same' if orig == pl else 'differs')
            w.writerow([r['guid'], pl, orig, state])

    # Input carrying the original string under the same guid, so the existing
    # label file still joins. Rows with no original fall back to the formatted
    # string rather than dropping out, which keeps the denominator identical
    # between the two runs.
    with open(args.out, 'w', encoding='utf-8', newline='') as f:
        f.write(f'# source={args.input} field=Input_Original '
                f'generated_from=eval/pull_input_original.py\n')
        w = csv.DictWriter(f, fieldnames=['place', 'guid', 'frequency', 'band'],
                           delimiter='\t', extrasaction='ignore')
        w.writeheader()
        for r in rows:
            orig = found.get(r['place']) or r['place']
            w.writerow({**r, 'place': orig})
    print(f'\nwrote {args.out} and {args.report}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
