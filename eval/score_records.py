"""Score rtl_matcher output against the frozen eval labels.

Headline is record accuracy: the sum over bands of band record share times band
string accuracy. Abstain is an empty authority_id — verified as an exact
partition against match_type on a 5k run, so no match_type list is hardcoded
here. Ancestors get no partial credit.

Bands come from the bands file rather than a constant, because the two eval
sets split differently: snowball4 on head/mid/tail at 1000/10, the MNT set on
head/mid/low/tail at 100000/1000/10, which is where its own record mass sits.
"""
import argparse
import csv
import json
import sys

from labels import ABSTAIN, NONE, read_labels

csv.field_size_limit(sys.maxsize)
BAND_ORDER = ('head', 'mid', 'low', 'tail')


def band_order(band_totals):
    """Bands present in the weights file, in the canonical broad-to-narrow
    order, with anything unrecognised appended so it is never dropped."""
    known = [b for b in BAND_ORDER if b in band_totals]
    return tuple(known + sorted(b for b in band_totals if b not in BAND_ORDER))


def bucket(authority_id, label):
    """Correct, wrong, or abstain for one row.

    An ABSTAIN label inverts the usual reading: the curator marked the string
    Illegible or Ambiguous, so claiming nothing is the right answer and
    committing to any UUID is wrong.
    """
    committed = (authority_id or '').strip()
    if label == ABSTAIN:
        return 'correct' if not committed else 'wrong'
    if not committed:
        return 'abstain'
    return 'correct' if committed == label else 'wrong'


def read_matcher_output(path):
    with open(path, encoding='utf-8', newline='') as f:
        return {r['guid']: r for r in csv.DictReader(f, delimiter='\t')}


def score(matcher_rows, labels, band_totals):
    by_guid = (matcher_rows if isinstance(matcher_rows, dict)
               else {r['guid']: r for r in matcher_rows})
    order = band_order(band_totals)

    bands = {b: {'correct': 0, 'wrong': 0, 'abstain': 0, 'scored': 0,
                 'accuracy': 0.0} for b in order}
    excluded_none = 0
    missing = 0

    for guid, lab in labels.items():
        if lab['label_string_only'] == NONE and lab.get('label_world', NONE) == NONE:
            excluded_none += 1
            continue
        row = by_guid.get(guid)
        if row is None:
            missing += 1
            continue
        band = bands[lab['band']]
        band[bucket(row.get('authority_id', ''), lab['label_string_only'])] += 1
        band['scored'] += 1

    for b in order:
        if bands[b]['scored']:
            bands[b]['accuracy'] = bands[b]['correct'] / bands[b]['scored']

    live = [b for b in order if bands[b]['scored']]
    weight_total = sum(band_totals[b]['records'] for b in live) or 1
    record_accuracy = sum(
        (band_totals[b]['records'] / weight_total) * bands[b]['accuracy']
        for b in live)

    return {'bands': bands, 'record_accuracy': record_accuracy,
            'excluded_none': excluded_none, 'missing_from_output': missing}


def world_delta(labels):
    """Rows the descoped LLM enrichment step would have recovered."""
    recoverable = sum(
        1 for lab in labels.values()
        if lab['label_string_only'] == NONE
        and lab.get('label_world') not in (NONE, '', None))
    return {'recoverable': recoverable, 'total': len(labels)}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output', required=True, help='rtl_matcher results TSV')
    p.add_argument('--labels', default='eval/data/labels_final.tsv')
    p.add_argument('--bands', default='eval/data/bands.json')
    p.add_argument('--detail', help='per-row detail TSV; dev only, never held-out')
    args = p.parse_args(argv)

    labels = read_labels(args.labels)
    with open(args.bands, encoding='utf-8') as f:
        band_totals = json.load(f)['bands']
    rows = read_matcher_output(args.output)
    result = score(rows, labels, band_totals)
    delta = world_delta(labels)

    print(f'record accuracy   {result["record_accuracy"]:.1%}')
    for b in band_order(band_totals):
        s = result['bands'][b]
        print(f'{b:5s} n={s["scored"]:>4} correct={s["correct"]:>4} '
              f'wrong={s["wrong"]:>4} abstain={s["abstain"]:>4} '
              f'acc={s["accuracy"]:.1%}')
    print(f'excluded, no PA record  {result["excluded_none"]}')
    print(f'missing from output     {result["missing_from_output"]}')
    print(f'world-knowledge upside  {delta["recoverable"]} of {delta["total"]} rows')

    if args.detail:
        with open(args.detail, 'w', encoding='utf-8', newline='') as f:
            w = csv.writer(f, delimiter='\t')
            w.writerow(['guid', 'place', 'band', 'label', 'authority_id',
                        'match_type', 'bucket'])
            for guid, lab in labels.items():
                row = rows.get(guid, {})
                w.writerow([guid, lab['place'], lab['band'],
                            lab['label_string_only'],
                            row.get('authority_id', ''),
                            row.get('match_type', ''),
                            bucket(row.get('authority_id', ''),
                                   lab['label_string_only'])])
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
