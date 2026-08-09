"""Score rtl_matcher output against the frozen eval labels.

Headline is record accuracy: the sum over bands of band record share times band
string accuracy. Abstain is an empty authority_id — verified as an exact
partition against match_type on a 5k run, so no match_type list is hardcoded
here. Ancestors get no partial credit.
"""
import argparse
import csv
import json
import sys

from labels import NONE, read_labels

csv.field_size_limit(sys.maxsize)
BAND_ORDER = ('head', 'mid', 'tail')


def bucket(authority_id, label):
    if not (authority_id or '').strip():
        return 'abstain'
    return 'correct' if authority_id.strip() == label else 'wrong'


def read_matcher_output(path):
    with open(path, encoding='utf-8', newline='') as f:
        return {r['guid']: r for r in csv.DictReader(f, delimiter='\t')}


def score(matcher_rows, labels, band_totals):
    by_guid = (matcher_rows if isinstance(matcher_rows, dict)
               else {r['guid']: r for r in matcher_rows})

    bands = {b: {'correct': 0, 'wrong': 0, 'abstain': 0, 'scored': 0,
                 'accuracy': 0.0} for b in BAND_ORDER}
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

    for b in BAND_ORDER:
        if bands[b]['scored']:
            bands[b]['accuracy'] = bands[b]['correct'] / bands[b]['scored']

    live = [b for b in BAND_ORDER if bands[b]['scored']]
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
    for b in BAND_ORDER:
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
