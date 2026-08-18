"""Map old-pipeline output into the shape score_frequency.py reads.

The old pipeline ends in `place_guid` / `place_id` across two files: the
automatch hits from stage 01 and the normalizer's `_Final.tsv`. `place_id`
carries `Amb` and `Ill` alongside real UUIDs, which are the pipeline's own way
of declining. Those map to an empty `authority_id`, the same act the new
matcher records as an abstain, so both are scored by one rule rather than
each being credited for its own vocabulary.

Rows the pipeline dropped entirely are emitted with an empty authority_id too.
Silently omitting them would let attrition read as abstention.
"""
import argparse
import csv
import os
import sys

csv.field_size_limit(sys.maxsize)

DECLINE = {'amb', 'ambiguous', 'ill', 'illegible', 'nan', 'none', 'null', ''}


def read_pairs(path, guid_col='place_guid', id_col='place_id'):
    if not path or not os.path.exists(path):
        return {}
    out = {}
    with open(path, encoding='utf-8-sig', newline='') as f:
        rd = csv.DictReader(f, delimiter='\t')
        cols = rd.fieldnames or []
        g = guid_col if guid_col in cols else next(
            (c for c in cols if c.strip().lower() in ('place_guid', 'guid')), None)
        i = id_col if id_col in cols else next(
            (c for c in cols if c.strip().lower() in
             ('place_id', 'matchauthid', 'match_authority_id', 'authority_id')), None)
        if not g or not i:
            print(f'  {os.path.basename(path)}: no guid/id columns in {cols[:8]}',
                  file=sys.stderr)
            return {}
        for r in rd:
            guid = (r.get(g) or '').strip()
            if not guid:
                continue
            val = (r.get(i) or '').strip()
            out[guid] = '' if val.lower() in DECLINE else val
    return out


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--matched', help='stage 01 _Matched.tsv')
    p.add_argument('--final', help='pipeline _Final.tsv')
    p.add_argument('--labels', required=True, help='defines the full row set')
    p.add_argument('--out', required=True)
    args = p.parse_args(argv)

    resolved = {}
    for path in (args.matched, args.final):
        got = read_pairs(path)
        # A real UUID never loses to a later blank: the two files partition the
        # input, but 05_combine tolerates overlap and so must this.
        for k, v in got.items():
            if v or k not in resolved:
                resolved[k] = v
        print(f'{os.path.basename(path or "-"):40s} {len(got):>7,} rows, '
              f'{sum(1 for v in got.values() if v):>7,} with a UUID')

    with open(args.labels, encoding='utf-8', newline='') as f:
        lines = [l for l in f if not l.startswith('#')]
    guids = [r['guid'] for r in csv.DictReader(lines, delimiter='\t')]

    missing = 0
    with open(args.out, 'w', encoding='utf-8', newline='') as f:
        w = csv.writer(f, delimiter='\t')
        w.writerow(['guid', 'authority_id'])
        for g in guids:
            if g not in resolved:
                missing += 1
            w.writerow([g, resolved.get(g, '')])
    committed = sum(1 for g in guids if resolved.get(g))
    print(f'\n{len(guids):,} label rows -> {args.out}')
    print(f'  committed to a UUID   {committed:,} ({committed/len(guids):.1%})')
    print(f'  never reached output  {missing:,} ({missing/len(guids):.1%})')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
