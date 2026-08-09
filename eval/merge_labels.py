"""Merge two independent labelers into a frozen label set plus a review queue.

Agreement on both label columns accepts the row. Anything else is routed to a
human. Adjudication resolves a conflict between two labelers and never reads
matcher output, so it does not burn held-out blindness.
"""
import argparse
import csv

from labels import LABEL_FIELDS, read_labels, write_labels

COMPARED = ('label_string_only', 'label_world')

_SIDE_COLS = ('leaf_string_only', 'chain_string_only',
              'label_string_only', 'status_string_only',
              'leaf_world', 'chain_world',
              'label_world', 'status_world')

REVIEW_FIELDS = (['guid', 'place', 'band', 'disagreement']
                 + [f'{side}_{col}' for side in ('a', 'b') for col in _SIDE_COLS])


def merge(a, b):
    """Return (agreed, review). Keys are guids; agreed rows use LABEL_FIELDS."""
    agreed, review = [], []
    for guid in sorted(set(a) | set(b)):
        ra, rb = a.get(guid), b.get(guid)
        if ra is None or rb is None:
            review.append(_review_row(
                guid, ra or rb,
                'missing_from_b' if rb is None else 'missing_from_a', ra, rb))
            continue
        differing = [c for c in COMPARED if ra[c] != rb[c]]
        if differing:
            review.append(_review_row(guid, ra, ','.join(differing), ra, rb))
        else:
            agreed.append({k: ra[k] for k in LABEL_FIELDS})
    return agreed, review


def _review_row(guid, present, disagreement, ra, rb):
    row = {'guid': guid, 'place': present.get('place', ''),
           'band': present.get('band', ''), 'disagreement': disagreement}
    for side, src in (('a', ra), ('b', rb)):
        for col in _SIDE_COLS:
            row[f'{side}_{col}'] = (src or {}).get(col, '')
    return row


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--a', required=True, help='first labeler TSV')
    p.add_argument('--b', required=True, help='second labeler TSV')
    p.add_argument('--out', default='eval/data/labels_final.tsv')
    p.add_argument('--review', default='eval/data/label_review.tsv')
    args = p.parse_args(argv)

    agreed, review = merge(read_labels(args.a), read_labels(args.b))
    write_labels(args.out, agreed, source=f'{args.a}+{args.b}')
    with open(args.review, 'w', encoding='utf-8', newline='') as f:
        f.write(f'# a={args.a} b={args.b} generated_from=merge_labels.py\n')
        w = csv.DictWriter(f, fieldnames=REVIEW_FIELDS, delimiter='\t',
                           extrasaction='ignore')
        w.writeheader()
        w.writerows(review)

    total = len(agreed) + len(review)
    pct = 100 * len(review) / total if total else 0
    print(f'agreed={len(agreed)} review={len(review)} ({pct:.1f}% disagreement)')
    print(f'an unreviewed disagreement is not a label: adjudicate {args.review} '
          f'and append the settled rows to {args.out}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
