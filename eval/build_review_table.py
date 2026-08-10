"""Join an eval run against its labels into one row-per-input review table.

`score_records --detail` writes the verdict but only the bare UUIDs, which is
unreadable by eye. This puts the curator's chain and the matcher's chain side
by side, plus what the walk skipped, so a failure can be diagnosed without
looking anything up.

Sorted wrong first, then abstain, then correct, and by record frequency
inside each, so the rows that cost the most sit at the top.
"""
import argparse
import csv
import sys

csv.field_size_limit(sys.maxsize)

VERDICT_ORDER = {'wrong': 0, 'abstain': 1, 'correct': 2}
FIELDS = ['verdict', 'band', 'frequency', 'input', 'truth_chain',
          'matcher_chain', 'match_type', 'candidates', 'skipped_terms',
          'label_status', 'truth_id', 'matcher_id', 'guid']


def read_tsv(path):
    with open(path, encoding='utf-8', newline='') as f:
        lines = [ln for ln in f if not ln.startswith('#')]
    return list(csv.DictReader(lines, delimiter='\t'))


def by_guid(rows):
    return {r['guid']: r for r in rows}


def truth_chain(label):
    """What the curator settled on, in a form a human can read."""
    if label['label_string_only'] == 'ABSTAIN':
        kind = label['status_string_only'].replace('mnt_', '')
        return f'({kind}) claim nothing'
    return (label['chain_string_only'] or label['leaf_string_only']
            or label['label_string_only'])


def review_rows(labels, detail, output):
    rows = []
    for guid, label in labels.items():
        d = detail.get(guid, {})
        o = output.get(guid, {})
        rows.append({
            'verdict': d.get('bucket', 'missing'),
            'band': label['band'],
            'frequency': o.get('frequency', ''),
            'input': label['place'],
            'truth_chain': truth_chain(label),
            'matcher_chain': (o.get('type_ahead') or o.get('authority_name')
                              or ''),
            'match_type': o.get('match_type', ''),
            'candidates': o.get('candidates', ''),
            'skipped_terms': o.get('skipped_terms', ''),
            'label_status': label['status_string_only'],
            'truth_id': label['label_string_only'],
            'matcher_id': o.get('authority_id', ''),
            'guid': guid,
        })

    def freq(r):
        try:
            return int(r['frequency'])
        except ValueError:
            return 0
    rows.sort(key=lambda r: (VERDICT_ORDER.get(r['verdict'], 9), -freq(r)))
    return rows


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--labels', default='eval/data/mnt_labels_dev.tsv')
    p.add_argument('--detail', default='eval/data/mnt_dev_detail.tsv')
    p.add_argument('--output', required=True, help='rtl_matcher results TSV')
    p.add_argument('--out', default='eval/data/mnt_dev_review.tsv')
    args = p.parse_args(argv)

    rows = review_rows(by_guid(read_tsv(args.labels)),
                       by_guid(read_tsv(args.detail)),
                       by_guid(read_tsv(args.output)))
    with open(args.out, 'w', encoding='utf-8', newline='') as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, delimiter='\t',
                           extrasaction='ignore')
        w.writeheader()
        w.writerows(rows)

    counts = {}
    for r in rows:
        counts[r['verdict']] = counts.get(r['verdict'], 0) + 1
    print(f'{args.out}: {len(rows)} rows  ' +
          '  '.join(f'{k}={v}' for k, v in sorted(counts.items())))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
