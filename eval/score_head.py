"""Score a filled head-verification sheet, and combine it with a tail estimate.

The head stratum is a census, so its accuracy carries no sampling error — the
only uncertainty is reviewer judgment. The tail is sampled, so the combined
figure inherits the tail's interval scaled by the tail's weight. Reporting
them apart and then together is what makes the combined number defensible:

    record accuracy = w_head x acc_head + w_tail x acc_tail

Partial sheets are fine and expected. Rows left blank or marked `?` drop out
of the numerator and denominator, and the coverage actually verified is
reported, so a sheet filled down to row 25 yields an exact number on whatever
share of the corpus those 25 rows carry rather than a wrong number on all of
it.
"""
import argparse
import csv
import sys

csv.field_size_limit(sys.maxsize)

CORRECT = {'y', 'a'}
WRONG = {'n'}
SKIP = {'', '?'}


def read_sheet(path):
    with open(path, encoding='utf-8', newline='') as f:
        lines = [l for l in f if not l.startswith('#')]
    return list(csv.DictReader(lines, delimiter='\t'))


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--sheet', default='eval/data/sb2_head200_verification.tsv')
    p.add_argument('--corpus-records', type=int, default=4570403)
    p.add_argument('--tail-accuracy', type=float,
                   help='tail record accuracy as a fraction, e.g. 0.723')
    p.add_argument('--tail-label', default='Leafprint sample')
    p.add_argument('--pps', action='store_true',
                   help='sheet came from a probability-proportional-to-size '
                        'draw, so every row already represents one record and '
                        'the estimator is an unweighted mean over draws. '
                        'Weighting these by frequency counts size twice.')
    args = p.parse_args(argv)

    rows = read_sheet(args.sheet)
    scored = wrong_f = correct_f = scored_f = 0
    skipped = skipped_f = 0
    declined_right = declined_wrong = 0

    for r in rows:
        v = (r.get('verdict') or '').strip().lower()
        if args.pps:
            # One draw, one record, one unit of weight.
            f = 1
        else:
            try:
                f = int(float(r.get('frequency') or 0))
            except ValueError:
                f = 0
        if v in SKIP:
            skipped += 1
            skipped_f += f
            continue
        scored += 1
        scored_f += f
        if v in CORRECT:
            correct_f += f
            if v == 'a':
                declined_right += 1
        elif v in WRONG:
            wrong_f += f
            if not (r.get('authority_id') or '').strip():
                declined_wrong += 1
        else:
            print(f'unrecognised verdict {v!r} at rank {r.get("rank")}',
                  file=sys.stderr)
            scored -= 1
            scored_f -= f

    if not scored_f:
        print('nothing scored yet — fill the verdict column')
        return 1

    acc_head = correct_f / scored_f
    verified_share = 1.0 if args.pps else scored_f / args.corpus_records

    if args.pps:
        # Binomial on the number of draws, which is what carries the error here.
        import math
        z, n, p_hat = 1.96, scored, acc_head
        d = 1 + z * z / n
        centre = (p_hat + z * z / (2 * n)) / d
        half = z * math.sqrt(p_hat * (1 - p_hat) / n + z * z / (4 * n * n)) / d
        print(f'PPS estimate over {n} draws')
        print(f'  record accuracy    {acc_head:.1%}  '
              f'(95% CI {max(0, centre - half):.1%}-{min(1, centre + half):.1%})')
        print(f'  correct draws      {correct_f} of {scored_f}')
        if declined_right or declined_wrong:
            print(f'  of declined rows:  {declined_right} rightly, '
                  f'{declined_wrong} wrongly')
        if skipped:
            print(f'  not yet adjudicated {skipped}')
        return 0

    print(f'head stratum, verified rows only')
    print(f'  rows scored        {scored} of {len(rows)} '
          f'({skipped} left blank or marked ?)')
    print(f'  records scored     {scored_f:,} '
          f'({verified_share:.1%} of the corpus)')
    print(f'  correct            {correct_f:,}')
    print(f'  wrong              {wrong_f:,}')
    print(f'  head accuracy      {acc_head:.1%}   (census, no sampling error)')
    if declined_right or declined_wrong:
        print(f'  of declined rows:  {declined_right} rightly, '
              f'{declined_wrong} wrongly')

    if args.tail_accuracy is not None:
        w_head = verified_share
        w_tail = 1 - w_head
        combined = w_head * acc_head + w_tail * args.tail_accuracy
        print()
        print(f'combined estimate')
        print(f'  head  {w_head:6.1%} x {acc_head:6.1%}  (census)')
        print(f'  tail  {w_tail:6.1%} x {args.tail_accuracy:6.1%}  '
              f'({args.tail_label}, sampled)')
        print(f'  record accuracy   {combined:.1%}')
        print()
        print('  The tail term carries the sampling error and the tail sample')
        print('  is not a random draw of the tail, so quote the head number as')
        print('  exact and the combined number as an estimate.')
    else:
        print()
        print('pass --tail-accuracy to combine with a tail estimate')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
