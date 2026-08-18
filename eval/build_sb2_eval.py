"""Turn the Leafprint-verified snowball2 ground truth into an eval set.

`snowball2_ground_truth.tsv` is 15,796 rows the Leafprint curators resolved
during the Snowball2 project, carrying a per-string `frequency`. That makes it
the one labelled set that answers both accuracy questions off the same file:
term accuracy is the unweighted share correct, record accuracy is the same
share weighted by frequency. No band estimation, because the record count is
exact per string rather than a sampled mean.

It also carries the residual the MNT set cannot. The MNT holds only strings a
curator resolved, so abstain-is-correct rows are 6.6% of it; here they are
32.1%, which is what the production mix actually looks like.

Writes an input TSV in the shape `rtl_matcher` reads and a labels TSV in the
shape `score_frequency.py` reads. `Amb` and `Ill` become ABSTAIN, matching
`eval/labels.py`, so the low-evidence gate is scored the same way in both sets.
"""
import argparse
import csv
import os
import sys

csv.field_size_limit(sys.maxsize)

DEFAULT_GT = ('/Users/natelemonnier/storied/code/place-normalizer/utils/'
              'snowball2_ground_truth.tsv')
DEFAULT_INPUT = 'eval/data/sb2_input.tsv'
DEFAULT_LABELS = 'eval/data/sb2_labels.tsv'

ABSTAIN = 'ABSTAIN'
ABSTAIN_TOKENS = {'amb', 'ambiguous', 'ill', 'illegible'}


def classify(ground_truth_id):
    """The label for one ground-truth cell, plus the curator's own verdict.

    A UUID is the answer. `Amb` and `Ill` mean the curator looked at the string
    and declined, so abstaining is correct and committing is wrong. An empty
    cell is neither — it was never labelled, and scoring it either way would be
    inventing a label.
    """
    v = (ground_truth_id or '').strip()
    if not v:
        return None, 'unlabelled'
    low = v.lower()
    if low in ABSTAIN_TOKENS:
        return ABSTAIN, ('illegible' if low.startswith('ill') else 'ambiguous')
    return v, 'verified'


def read_ground_truth(path):
    with open(path, encoding='utf-8-sig', newline='') as f:
        return list(csv.DictReader(f, delimiter='\t'))


def build(rows):
    inputs, labels, skipped = [], [], 0
    seen = set()
    for r in rows:
        place = (r.get('place') or '').strip()
        guid = (r.get('guid') or '').strip()
        label, status = classify(r.get('ground_truth_id'))
        if not place or not guid or label is None or guid in seen:
            skipped += 1
            continue
        seen.add(guid)
        try:
            freq = int(float(r.get('frequency') or 0))
        except ValueError:
            freq = 0
        inputs.append({'place': place, 'guid': guid, 'frequency': freq})
        labels.append({'guid': guid, 'place': place, 'frequency': freq,
                       'label': label, 'status': status,
                       'truth_name': (r.get('ground_truth_name') or '').strip()})
    return inputs, labels, skipped


def write_tsv(path, rows, fields, header_comment=None):
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    with open(path, 'w', encoding='utf-8', newline='') as f:
        if header_comment:
            f.write(f'# {header_comment}\n')
        w = csv.DictWriter(f, fieldnames=fields, delimiter='\t',
                           extrasaction='ignore')
        w.writeheader()
        w.writerows(rows)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--ground-truth', default=DEFAULT_GT)
    p.add_argument('--input-out', default=DEFAULT_INPUT)
    p.add_argument('--labels-out', default=DEFAULT_LABELS)
    args = p.parse_args(argv)

    rows = read_ground_truth(args.ground_truth)
    inputs, labels, skipped = build(rows)

    src = os.path.basename(args.ground_truth)
    write_tsv(args.input_out, inputs, ['place', 'guid', 'frequency'],
              f'source={src} generated_from=eval/build_sb2_eval.py')
    write_tsv(args.labels_out, labels,
              ['guid', 'place', 'frequency', 'label', 'status', 'truth_name'],
              f'source={src} generated_from=eval/build_sb2_eval.py')

    total_f = sum(r['frequency'] for r in inputs)
    abstain = [r for r in labels if r['label'] == ABSTAIN]
    abstain_f = sum(r['frequency'] for r in abstain)
    print(f'{len(rows)} ground-truth rows -> {len(inputs)} eval rows '
          f'({skipped} skipped: unlabelled, blank, or duplicate guid)')
    print(f'records represented  {total_f:,}')
    print(f'abstain-is-correct   {len(abstain)} strings ({len(abstain)/len(inputs):.1%}), '
          f'{abstain_f:,} records ({abstain_f/total_f:.1%})')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
