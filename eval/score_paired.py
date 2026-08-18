"""Score two runs side by side on the banded MNT eval set.

`score_records.py` reports the headline for one run. This reports the pair,
plus the three numbers a non-technical audience actually asks for and which
the headline alone conflates:

- term accuracy — band accuracies weighted by each band's share of strings
- record accuracy — the same weighted by each band's share of records
- coverage — the share the run commits to at all, which needs no labels
- precision — of what it commits to, the share correct

Term and record differ here by more than ten points off identical rows, since
the head band is 0.8% of strings and 90.8% of records. That gap is the reason
to name which one is being quoted.

Usage:
    python eval/score_paired.py --run "old pipeline"=path.tsv \
        --run "new matcher"=path.tsv --labels ... --bands ...
"""
import argparse
import csv
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
csv.field_size_limit(sys.maxsize)

from labels import ABSTAIN, NONE, read_labels  # noqa: E402


def load(path):
    with open(path, encoding='utf-8', newline='') as f:
        lines = [l for l in f if not l.startswith('#')]
    return {r['guid']: r for r in csv.DictReader(lines, delimiter='\t')}


def bucket(authority_id, label):
    committed = (authority_id or '').strip()
    if label == ABSTAIN:
        return 'correct' if not committed else 'wrong'
    if not committed:
        return 'abstain'
    return 'correct' if committed == label else 'wrong'


def measure(rows, labels, bands):
    per = {b: {'correct': 0, 'n': 0, 'commit': 0, 'commit_ok': 0} for b in bands}
    for guid, lab in labels.items():
        if (lab['label_string_only'] == NONE
                and lab.get('label_world', NONE) == NONE):
            continue
        r = rows.get(guid)
        if r is None:
            continue
        aid = (r.get('authority_id') or '').strip()
        b = per[lab['band']]
        bk = bucket(aid, lab['label_string_only'])
        b['n'] += 1
        b['correct'] += bk == 'correct'
        if aid:
            b['commit'] += 1
            b['commit_ok'] += bk == 'correct'

    def rate(num, den):
        return {b: (per[b][num] / per[b][den] if per[b][den] else 0.0)
                for b in per}

    acc = rate('correct', 'n')
    cov = rate('commit', 'n')
    prec = rate('commit_ok', 'commit')

    str_total = sum(bands[b]['strings'] for b in bands)
    rec_total = sum(bands[b]['records'] for b in bands)

    def weighted(d, key, total):
        return sum(bands[b][key] / total * d[b] for b in d)

    return {
        'term': weighted(acc, 'strings', str_total),
        'record': weighted(acc, 'records', rec_total),
        'cov_r': weighted(cov, 'records', rec_total),
        'prec_r': weighted(prec, 'records', rec_total),
        'acc': acc, 'per': per,
    }


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run', action='append', required=True,
                   metavar='NAME=PATH', help='repeatable')
    p.add_argument('--labels', default='eval/data/mnt_labels_dev.tsv')
    p.add_argument('--bands', default='eval/data/mnt_bands.json')
    args = p.parse_args(argv)

    labels = read_labels(args.labels)
    bands = json.load(open(args.bands, encoding='utf-8'))['bands']
    str_total = sum(bands[b]['strings'] for b in bands)
    rec_total = sum(bands[b]['records'] for b in bands)

    results = {}
    for spec in args.run:
        name, _, path = spec.partition('=')
        results[name] = measure(load(path), labels, bands)

    print(f'{"":16s} {"term acc":>9} {"record acc":>11} '
          f'{"coverage(rec)":>14} {"precision(rec)":>15}')
    for name, r in results.items():
        print(f'{name:16s} {r["term"]:>9.1%} {r["record"]:>11.1%} '
              f'{r["cov_r"]:>14.1%} {r["prec_r"]:>15.1%}')

    names = list(results)
    print()
    header = f'{"band":6s} {"str share":>10} {"rec share":>10}'
    for n in names:
        header += f' {n[:9]:>10}'
    if len(names) == 2:
        header += f' {"delta":>9}'
    print(header)
    for b in ('head', 'mid', 'low', 'tail'):
        if b not in bands:
            continue
        line = (f'{b:6s} {bands[b]["strings"]/str_total:>10.1%} '
                f'{bands[b]["records"]/rec_total:>10.1%}')
        for n in names:
            line += f' {results[n]["acc"][b]:>10.1%}'
        if len(names) == 2:
            d = (results[names[1]]['acc'][b] - results[names[0]]['acc'][b]) * 100
            line += f' {d:>+7.1f}pt'
        print(line)

    print()
    for name, r in results.items():
        per = r['per']
        n = sum(per[b]['n'] for b in per)
        c = sum(per[b]['commit'] for b in per)
        ok = sum(per[b]['commit_ok'] for b in per)
        print(f'{name:16s} sample as drawn: {n} scored, {c} committed '
              f'({c/n:.1%}), {ok} correct ({ok/max(c,1):.1%} precision)')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
