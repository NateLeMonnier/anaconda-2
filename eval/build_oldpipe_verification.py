"""Build an adjudication sheet for the old pipeline on the same PPS draws.

Symmetric adjudication: each system's own output is reviewed on its own merits
under the same policies, rather than one system being scored against labels
derived from the other. That removes the definitional advantage the first
system would otherwise hold over every row where it was judged correct.

Ranks are carried over from the rtl_matcher sheet by joining on guid, so row N
here is the same input string as row N there and the two are comparable
line by line. Sorting this file independently would silently misalign them
wherever two draws share a frequency.

The old pipeline writes `place_guid` / `place_id`, and spells declining as
`Amb` or `Ill`. Both become an empty answer, the same way `oldpipe_to_scorable`
treats them, so one adjudication rule covers both systems.
"""
import argparse
import csv
import os
import sys

csv.field_size_limit(sys.maxsize)

DECLINE = {'amb', 'ambiguous', 'ill', 'illegible', 'nan', 'none', 'null', ''}

SHEET_FIELDS = [
    'rank', 'frequency', 'place', 'matcher_answer', 'matcher_chain',
    'match_type', 'authority_id', 'verdict', 'true_uuid', 'notes',
]


def read(path, skip_comments=True):
    with open(path, encoding='utf-8-sig', newline='') as f:
        lines = [l for l in f if not (skip_comments and l.startswith('#'))]
    return list(csv.DictReader(lines, delimiter='\t'))


def read_pairs(path):
    if not path or not os.path.exists(path):
        return {}
    out = {}
    for r in read(path):
        g = (r.get('place_guid') or r.get('guid') or '').strip()
        if not g:
            continue
        v = (r.get('place_id') or '').strip()
        out[g] = '' if v.lower() in DECLINE else v
    return out


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--ranks', default='eval/data/sb2_tailpps_verification.tsv',
                   help='rtl sheet supplying rank order and the input strings')
    p.add_argument('--input', default='eval/data/sb2_tailpps_input.tsv',
                   help='the PPS input, for guid<->place')
    p.add_argument('--matched', help='stage 01 _Matched.tsv')
    p.add_argument('--final', help='pipeline _Final.tsv')
    p.add_argument('--pa', default='/Users/natelemonnier/storied/resources/'
                                   'place-authority-mnt-tsv/PA6_16_2026v77.tsv')
    p.add_argument('--out', default='eval/data/sb2_tailpps_oldpipe_verification.tsv')
    args = p.parse_args(argv)

    inputs = {r['guid']: r for r in read(args.input)}
    ranks = read(args.ranks)
    # place -> guid, since the rtl sheet carries the string rather than the guid
    place_to_guid = {r['place']: g for g, r in inputs.items()}

    resolved = {}
    for path in (args.matched, args.final):
        for k, v in read_pairs(path).items():
            if v or k not in resolved:
                resolved[k] = v

    pa = {}
    for r in read(args.pa, skip_comments=False):
        uid = (r.get('ID') or '').strip()
        if uid:
            pa[uid] = (r.get('FullChainName') or '', r.get('Term') or '')

    rows, missing = [], 0
    for r in ranks:
        guid = place_to_guid.get(r['place'], '')
        aid = resolved.get(guid, '')
        if guid not in resolved:
            missing += 1
        chain, term = pa.get(aid, ('', ''))
        rows.append({
            'rank': r['rank'],
            'frequency': r['frequency'],
            'place': r['place'],
            'matcher_answer': term or '(declined)',
            'matcher_chain': chain,
            'match_type': 'committed' if aid else 'declined',
            'authority_id': aid,
            'verdict': '', 'true_uuid': '', 'notes': '',
        })

    os.makedirs(os.path.dirname(args.out) or '.', exist_ok=True)
    with open(args.out, 'w', encoding='utf-8', newline='') as f:
        f.write('# old pipeline on the same PPS draws; ranks joined from '
                f'{os.path.basename(args.ranks)}\n')
        f.write('# fill verdict: y=right  n=wrong  a=declined and that was '
                'right  ?=unsure\n')
        w = csv.DictWriter(f, fieldnames=SHEET_FIELDS, delimiter='\t',
                           extrasaction='ignore')
        w.writeheader()
        w.writerows(rows)

    committed = sum(1 for r in rows if r['authority_id'])
    unresolved_uuid = sum(1 for r in rows
                          if r['authority_id'] and not r['matcher_chain'])
    print(f'{args.out}')
    print(f'  {len(rows)} rows, {committed} committed '
          f'({committed / len(rows):.1%}), {len(rows) - committed} declined')
    print(f'  never reached the pipeline output: {missing}')
    if unresolved_uuid:
        print(f'  committed to a UUID absent from the PA export: '
              f'{unresolved_uuid}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
