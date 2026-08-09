"""Build the blind eval sample from snowball4.

Stratified by frequency band so one labeling pass yields both record accuracy
(reweighted by band record share) and per-band string accuracy. Split 50/50
into a dev half that is inspected freely and a held-out half that reports
aggregates only.
"""
import argparse
import csv
import json
import os
import random
import sys

csv.field_size_limit(sys.maxsize)

SEED = 42
SAMPLE_SIZES = {'head': 800, 'mid': 600, 'tail': 600}
BAND_ORDER = ('head', 'mid', 'tail')

DEFAULT_CORPUS = ('/Users/natelemonnier/storied/resources/'
                  'np_records_snowball4_locations.tsv')
DEFAULT_EXCLUSIONS = ('/Users/natelemonnier/storied/code/place-normalizer/'
                      'utils/snowball2_ground_truth.tsv')


def band_for(frequency):
    if frequency >= 1000:
        return 'head'
    if frequency >= 10:
        return 'mid'
    return 'tail'


def load_exclusions(path):
    """Place strings already seen during development, read from `place`."""
    with open(path, encoding='utf-8', newline='') as f:
        return {(r.get('place') or '').strip()
                for r in csv.DictReader(f, delimiter='\t')
                if (r.get('place') or '').strip()}


def load_corpus(path, exclusions):
    """One row per guid, with frequency summed across the corpus.

    snowball4 carries one row per (place, inferred_location) pair, not per
    place string: 142,029 guids appear on several rows with their record
    count split between them, so `Brown University, Rhode Island` shows as 8
    and 4 rather than 12. Summing first is what makes band assignment correct
    and keeps guid the unique key the scorer joins on. Aggregating by guid
    rather than place is safe — no guid maps to more than one place string,
    though 2,498 place strings carry more than one guid.
    """
    totals = {}
    with open(path, encoding='utf-8', newline='') as f:
        for rec in csv.DictReader(f, delimiter='\t'):
            place = (rec.get('place') or '').strip()
            guid = (rec.get('guid') or '').strip()
            if not place or not guid or place in exclusions:
                continue
            try:
                freq = int(float(rec.get('frequency') or 0))
            except ValueError:
                continue
            if guid in totals:
                totals[guid]['frequency'] += freq
            else:
                totals[guid] = {'place': place, 'guid': guid, 'frequency': freq}
    return [dict(r, frequency=str(r['frequency'])) for r in totals.values()]


def band_record_totals(rows):
    """Per-band string count and record count over the whole corpus."""
    totals = {b: {'strings': 0, 'records': 0} for b in BAND_ORDER}
    for r in rows:
        b = totals[band_for(int(r['frequency']))]
        b['strings'] += 1
        b['records'] += int(r['frequency'])
    return totals


def stratified_sample(rows, sizes, seed):
    buckets = {b: [] for b in BAND_ORDER}
    for r in rows:
        buckets[band_for(int(r['frequency']))].append(r)
    out = {}
    for band in BAND_ORDER:
        pool = sorted(buckets[band], key=lambda r: r['guid'])
        rng = random.Random(f'{seed}:{band}')
        out[band] = rng.sample(pool, min(sizes[band], len(pool)))
    return out


def split_dev_heldout(sampled, seed):
    dev, heldout = [], []
    for band in BAND_ORDER:
        rows = [dict(r, band=band) for r in sampled[band]]
        rng = random.Random(f'{seed}:split:{band}')
        rng.shuffle(rows)
        half = len(rows) // 2
        dev.extend(rows[:half])
        heldout.extend(rows[half:half * 2])
    return dev, heldout


def write_tsv(path, rows, source, seed):
    with open(path, 'w', encoding='utf-8', newline='') as f:
        f.write(f'# seed={seed} source={source} '
                f'generated_from=build_eval_sample.py\n')
        w = csv.DictWriter(f, fieldnames=['place', 'guid', 'frequency', 'band'],
                           delimiter='\t', extrasaction='ignore')
        w.writeheader()
        w.writerows(rows)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--corpus', default=DEFAULT_CORPUS)
    p.add_argument('--exclusions', default=DEFAULT_EXCLUSIONS)
    p.add_argument('--out-dir', default='eval/data')
    p.add_argument('--seed', type=int, default=SEED)
    args = p.parse_args(argv)

    os.makedirs(args.out_dir, exist_ok=True)

    exclusions = load_exclusions(args.exclusions)
    rows = load_corpus(args.corpus, exclusions)
    totals = band_record_totals(rows)
    dev, heldout = split_dev_heldout(
        stratified_sample(rows, SAMPLE_SIZES, args.seed), args.seed)

    write_tsv(f'{args.out_dir}/eval_dev.tsv', dev, args.corpus, args.seed)
    write_tsv(f'{args.out_dir}/eval_heldout.tsv', heldout, args.corpus, args.seed)
    with open(f'{args.out_dir}/bands.json', 'w', encoding='utf-8') as f:
        json.dump({'seed': args.seed, 'corpus': args.corpus, 'bands': totals},
                  f, indent=2)

    print(f'excluded {len(exclusions)} seen strings, kept {len(rows)} corpus rows')
    for band in BAND_ORDER:
        t = totals[band]
        print(f'{band:5s} strings={t["strings"]:>9,} records={t["records"]:>12,}')
    print(f'dev={len(dev)} heldout={len(heldout)}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
