"""Write adjudicated verdicts into a verification sheet by rank.

Adjudication happens in batches, so this has to be safe to run repeatedly. It
refuses to silently overwrite a verdict that is already set unless --force is
passed, because a second pass quietly clobbering the first would be invisible
in the final number.

    python eval/apply_verdicts.py --sheet S --set "1=y,2=y,3=a,13=a"
    python eval/apply_verdicts.py --sheet S --set "17=n" --true-uuid "17=ABC-..."
"""
import argparse
import csv
import sys

csv.field_size_limit(sys.maxsize)

VALID = {'y', 'n', 'a', '?'}


def parse_pairs(spec):
    out = {}
    for part in (spec or '').split(','):
        part = part.strip()
        if not part:
            continue
        k, _, v = part.partition('=')
        out[k.strip()] = v.strip()
    return out


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--sheet', required=True)
    p.add_argument('--set', dest='verdicts', required=True,
                   help='comma-separated rank=verdict')
    p.add_argument('--true-uuid', default='', help='comma-separated rank=uuid')
    # Repeatable rather than comma-separated: notes routinely contain commas,
    # and splitting on them silently truncated the note and then crashed on
    # the fragment.
    p.add_argument('--note', action='append', default=[],
                   metavar='RANK=TEXT', help='repeatable, one rank=note each')
    p.add_argument('--force', action='store_true',
                   help='overwrite verdicts that are already filled')
    args = p.parse_args(argv)

    verdicts = parse_pairs(args.verdicts)
    uuids = parse_pairs(args.true_uuid)
    notes = {}
    for item in args.note:
        k, _, v = item.partition('=')
        notes[k.strip()] = v.strip()

    bad = {k: v for k, v in verdicts.items() if v not in VALID}
    if bad:
        print(f'invalid verdicts: {bad} (allowed: {sorted(VALID)})',
              file=sys.stderr)
        return 1

    with open(args.sheet, encoding='utf-8', newline='') as f:
        lines = f.readlines()
    comments = [l for l in lines if l.startswith('#')]
    body = [l for l in lines if not l.startswith('#')]
    reader = csv.DictReader(body, delimiter='\t')
    fields = reader.fieldnames
    rows = list(reader)

    by_rank = {r['rank']: r for r in rows}
    missing = [k for k in verdicts if k not in by_rank]
    if missing:
        print(f'no such rank: {missing}', file=sys.stderr)
        return 1

    clobber = [k for k, v in verdicts.items()
               if (by_rank[k].get('verdict') or '').strip()
               and by_rank[k]['verdict'] != v]
    if clobber and not args.force:
        print(f'already adjudicated, pass --force to change: {clobber}',
              file=sys.stderr)
        return 1

    for k, v in verdicts.items():
        by_rank[k]['verdict'] = v
    for k, v in uuids.items():
        by_rank[k]['true_uuid'] = v
    for k, v in notes.items():
        by_rank[k]['notes'] = v

    with open(args.sheet, 'w', encoding='utf-8', newline='') as f:
        f.writelines(comments)
        w = csv.DictWriter(f, fieldnames=fields, delimiter='\t',
                           extrasaction='ignore')
        w.writeheader()
        w.writerows(rows)

    filled = sum(1 for r in rows if (r.get('verdict') or '').strip())
    print(f'wrote {len(verdicts)} verdicts; {filled} of {len(rows)} filled')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
