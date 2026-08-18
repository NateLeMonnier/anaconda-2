"""Draw a fixed sample from the snowball2 eval set.

The full 15,796-row set takes the old pipeline past the point where its
`place_authority_normalizer_parallel` stage finishes in useful time — it ran
2h38m on the full set without producing output. A 2,500-row draw matches the
size the old pipeline clears in about nine minutes on `mnt_dev`, so the two
can be compared on the same corpus.

Simple random draw rather than stratified. The set's own mix of verified,
ambiguous, and illegible rows is what production looks like, and record
accuracy is weighted per row by an exact frequency, so there is no band
structure to preserve.

The holdout does not need rebuilding: `mnt_holdout_sb2_v2_nullfix.tsv` was
built from all 15,796 strings in both their padded and stripped forms, so it
already removes everything reachable from any subset of them.
"""
import argparse
import csv
import os
import random
import sys

csv.field_size_limit(sys.maxsize)


def read(path):
    with open(path, encoding='utf-8', newline='') as f:
        lines = [l for l in f if not l.startswith('#')]
    return list(csv.DictReader(lines, delimiter='\t'))


def write(path, rows, fields, comment):
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    with open(path, 'w', encoding='utf-8', newline='') as f:
        f.write(f'# {comment}\n')
        w = csv.DictWriter(f, fieldnames=fields, delimiter='\t',
                           extrasaction='ignore')
        w.writeheader()
        w.writerows(rows)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--input', default='eval/data/sb2_input_nullstripped.tsv')
    p.add_argument('--labels', default='eval/data/sb2_labels.tsv')
    p.add_argument('--n', type=int, default=2500)
    p.add_argument('--seed', type=int, default=42)
    p.add_argument('--input-out', default='eval/data/sb2_sample_input.tsv')
    p.add_argument('--labels-out', default='eval/data/sb2_sample_labels.tsv')
    args = p.parse_args(argv)

    inputs = read(args.input)
    labels = {r['guid']: r for r in read(args.labels)}

    rng = random.Random(args.seed)
    picked = rng.sample(inputs, min(args.n, len(inputs)))
    picked_labels = [labels[r['guid']] for r in picked if r['guid'] in labels]

    comment = (f'source={os.path.basename(args.input)} n={args.n} '
               f'seed={args.seed} generated_from=eval/build_sb2_sample.py')
    write(args.input_out, picked, ['place', 'guid', 'frequency'], comment)
    write(args.labels_out, picked_labels,
          ['guid', 'place', 'frequency', 'label', 'status', 'truth_name'],
          comment)

    freq = sum(int(float(r['frequency'] or 0)) for r in picked)
    from collections import Counter
    mix = Counter(r['status'] for r in picked_labels)
    print(f'{len(picked)} of {len(inputs)} rows, {freq:,} records')
    for k, v in mix.most_common():
        print(f'  {k:12s} {v:>5} ({v/len(picked_labels):5.1%})')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
