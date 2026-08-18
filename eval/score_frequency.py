"""Score a matcher run against a frequency-carrying label set.

Reports both accuracy numbers off one file. Term accuracy is the unweighted
share of strings correct. Record accuracy weights each string by its own
`frequency`, which is an exact count here rather than the sampled mean the
band weighting in `score_records.py` has to use.

Coverage is reported separately from accuracy and on purpose. The share of
records the matcher answers on at all is a throughput number that needs no
labels, and collapsing it into accuracy is what makes "how accurate is it?"
unanswerable. Coverage times precision is record accuracy; all three are
printed so the identity is visible.

Abstain follows `eval/labels.py`: an empty `authority_id` is correct against
an ABSTAIN label, where the curator marked the string Ambiguous or Illegible,
and wrong anywhere else.

Reads any matcher output with `guid` and `authority_id` columns, so the old
pipeline scores here too once its output is mapped into that shape.
"""
import argparse
import csv
import math
import sys

csv.field_size_limit(sys.maxsize)

ABSTAIN = 'ABSTAIN'


def read_labels(path):
    with open(path, encoding='utf-8', newline='') as f:
        lines = [ln for ln in f if not ln.startswith('#')]
    return {r['guid']: r for r in csv.DictReader(lines, delimiter='\t')}


def read_output(path):
    with open(path, encoding='utf-8', newline='') as f:
        lines = [ln for ln in f if not ln.startswith('#')]
        return {r['guid']: r for r in csv.DictReader(lines, delimiter='\t')}


def bucket(authority_id, label):
    committed = (authority_id or '').strip()
    if label == ABSTAIN:
        return 'correct' if not committed else 'wrong'
    if not committed:
        return 'abstain'
    return 'correct' if committed == label else 'wrong'


def score(rows, labels):
    counts = {k: {'strings': 0, 'records': 0}
              for k in ('correct', 'wrong', 'abstain', 'missing')}
    by_status = {}
    committed = {'strings': 0, 'records': 0}
    commit_correct = {'strings': 0, 'records': 0}
    total = {'strings': 0, 'records': 0}

    for guid, lab in labels.items():
        try:
            freq = int(float(lab.get('frequency') or 0))
        except ValueError:
            freq = 0
        total['strings'] += 1
        total['records'] += freq

        row = rows.get(guid)
        if row is None:
            counts['missing']['strings'] += 1
            counts['missing']['records'] += freq
            continue

        aid = (row.get('authority_id') or '').strip()
        b = bucket(aid, lab['label'])
        counts[b]['strings'] += 1
        counts[b]['records'] += freq

        if aid:
            committed['strings'] += 1
            committed['records'] += freq
            if b == 'correct':
                commit_correct['strings'] += 1
                commit_correct['records'] += freq

        st = lab.get('status', '')
        s = by_status.setdefault(st, {'n': 0, 'correct': 0, 'records': 0,
                                      'records_correct': 0})
        s['n'] += 1
        s['records'] += freq
        if b == 'correct':
            s['correct'] += 1
            s['records_correct'] += freq

    return {'counts': counts, 'total': total, 'committed': committed,
            'commit_correct': commit_correct, 'by_status': by_status}


def wilson(correct, n):
    """95% interval on a share. Wilson rather than normal-approximation, since
    the per-status cuts get small enough for the approximation to run off the
    end of [0, 1)."""
    if not n:
        return (0.0, 0.0)
    z = 1.96
    p = correct / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, centre - half), min(1.0, centre + half))


def report(res, name):
    c, tot = res['counts'], res['total']
    ts, tr = tot['strings'] or 1, tot['records'] or 1
    term = c['correct']['strings'] / ts
    rec = c['correct']['records'] / tr
    lo, hi = wilson(c['correct']['strings'], ts)

    print(f'== {name}')
    print(f'strings {ts:,}   records {tr:,}')
    print()
    print(f'term accuracy     {term:7.1%}   (95% CI {lo:.1%}-{hi:.1%})')
    print(f'record accuracy   {rec:7.1%}')
    print()
    cov_s = res['committed']['strings'] / ts
    cov_r = res['committed']['records'] / tr
    prec_s = (res['commit_correct']['strings'] / res['committed']['strings']
              if res['committed']['strings'] else 0.0)
    prec_r = (res['commit_correct']['records'] / res['committed']['records']
              if res['committed']['records'] else 0.0)
    print(f'coverage          {cov_s:7.1%} of strings   {cov_r:7.1%} of records')
    print(f'precision         {prec_s:7.1%} of strings   {prec_r:7.1%} of records')
    print('  note: coverage x precision counts committed rows only; record')
    print('        accuracy also credits correct abstains.')
    print()
    print(f'{"bucket":10s} {"strings":>8} {"share":>8} {"records":>13} {"share":>8}')
    for k in ('correct', 'wrong', 'abstain', 'missing'):
        print(f'{k:10s} {c[k]["strings"]:>8,} {c[k]["strings"]/ts:>8.1%} '
              f'{c[k]["records"]:>13,} {c[k]["records"]/tr:>8.1%}')
    print()
    print(f'{"curator verdict":18s} {"n":>6} {"term acc":>9} {"record acc":>11}')
    for st, s in sorted(res['by_status'].items(), key=lambda kv: -kv[1]['n']):
        ra = s['records_correct'] / s['records'] if s['records'] else 0.0
        print(f'{st:18s} {s["n"]:>6} {s["correct"]/s["n"]:>9.1%} {ra:>11.1%}')


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output', required=True)
    p.add_argument('--labels', required=True)
    p.add_argument('--name', default='run')
    args = p.parse_args(argv)

    labels = read_labels(args.labels)
    rows = read_output(args.output)
    report(score(rows, labels), args.name)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
