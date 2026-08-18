"""Build a hand-verification sheet for the highest-frequency strings.

Record accuracy is frequency-weighted, so the head of a corpus decides it. In
snowball2, 200 strings out of 499,994 carry 45.5% of the records. Censusing
those by hand costs an hour and yields an exact number on that stratum, with
no sampling error and no contamination question, against a sampled estimate
for the tail.

The sheet is sorted by frequency and carries a running share of the corpus, so
the reviewer can stop at whatever coverage they are willing to pay for and
still know exactly what they bought.

Columns to fill: `verdict`, and `true_uuid` where the verdict is `n`.

    y   the matcher's answer is right
    n   the matcher's answer is wrong, or it declined when an answer exists
    a   the matcher declined and declining was right — no resolvable place
    ?   unsure, revisit

`a` and `n` are the same keystroke cost and mean opposite things on a declined
row, which is the distinction the whole abstain policy rests on.
"""
import argparse
import csv
import os
import sys

csv.field_size_limit(sys.maxsize)


def read(path):
    with open(path, encoding='utf-8', newline='') as f:
        lines = [l for l in f if not l.startswith('#')]
    return list(csv.DictReader(lines, delimiter='\t'))


def freq(row):
    try:
        return int(float(row.get('frequency') or 0))
    except ValueError:
        return 0


SHEET_FIELDS = [
    'rank', 'frequency', 'pct_corpus', 'cum_pct_corpus', 'cum_pct_stratum',
    'place', 'matcher_answer', 'matcher_chain', 'match_type',
    'authority_id', 'candidates', 'verdict', 'true_uuid', 'notes',
]


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output', default='eval/runs/08-12/sb2_head200_input_01.tsv',
                   help='matcher output over the head strings')
    p.add_argument('--corpus-records', type=int, default=4570403,
                   help='total records in the corpus the head was drawn from')
    p.add_argument('--out', default='eval/data/sb2_head200_verification.tsv')
    args = p.parse_args(argv)

    rows = sorted(read(args.output), key=lambda r: -freq(r))
    stratum = sum(freq(r) for r in rows) or 1

    running = 0
    out = []
    for i, r in enumerate(rows, 1):
        f = freq(r)
        running += f
        committed = (r.get('authority_id') or '').strip()
        cands = (r.get('candidate_names') or '').strip()
        out.append({
            'rank': i,
            'frequency': f,
            'pct_corpus': f'{f / args.corpus_records:.3%}',
            'cum_pct_corpus': f'{running / args.corpus_records:.1%}',
            'cum_pct_stratum': f'{running / stratum:.1%}',
            'place': r.get('original', ''),
            'matcher_answer': r.get('authority_name', '') or '(declined)',
            'matcher_chain': r.get('type_ahead', '') or r.get('jurisdiction', ''),
            'match_type': r.get('match_type', ''),
            'authority_id': committed,
            # Only useful where it declined; on a committed row the winner is
            # already in matcher_answer and the list is noise.
            'candidates': '' if committed else cands[:300],
            'verdict': '',
            'true_uuid': '',
            'notes': '',
        })

    os.makedirs(os.path.dirname(args.out) or '.', exist_ok=True)
    with open(args.out, 'w', encoding='utf-8', newline='') as f:
        f.write(f'# head verification sheet from {args.output}\n')
        f.write(f'# {len(out)} strings, {stratum:,} records, '
                f'{stratum / args.corpus_records:.1%} of the corpus\n')
        f.write('# fill verdict: y=right  n=wrong  a=declined and that was '
                'right  ?=unsure\n')
        w = csv.DictWriter(f, fieldnames=SHEET_FIELDS, delimiter='\t',
                           extrasaction='ignore')
        w.writeheader()
        w.writerows(out)

    declined = sum(1 for r in out if not r['authority_id'])
    declined_f = sum(r['frequency'] for r in out if not r['authority_id'])
    print(f'{args.out}')
    print(f'  {len(out)} strings, {stratum:,} records, '
          f'{stratum / args.corpus_records:.1%} of the corpus')
    print(f'  committed {len(out) - declined} strings, '
          f'{stratum - declined_f:,} records '
          f'({(stratum - declined_f) / stratum:.1%} of the stratum)')
    print(f'  declined  {declined} strings, {declined_f:,} records '
          f'({declined_f / stratum:.1%}) — these need the closest attention')
    print()
    print('  effort guide, by cumulative share of the whole corpus:')
    for n in (10, 25, 50, 100, 200):
        if n <= len(out):
            print(f'    first {n:>3} rows -> {out[n-1]["cum_pct_corpus"]:>6} '
                  f'of corpus records')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
