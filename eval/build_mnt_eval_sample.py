"""Turn the cached MNT pool into a labeled eval set.

Labels come from the curators, not a model: `Match_Authority_ID` on a row the
Master Normalization Table marks `UUID Verified`. That is the whole reason
this set exists — the snowball labeling pass was wrong too often and too slow
to run at 5k, and the MNT already holds 1,443,411 verified mappings.

The set is only worth anything against a matcher that has not been handed the
answer. `rtl_matcher` loads the MNT as its dictionary, so a run against the
full table resolves 100% of these strings by exact lookup and 86.7% through
the full-string fast path. `build_mnt_holdout.py` is not optional.

Bands split on `Total`, the record count, and follow the MNT's own
distribution rather than the 1000/10 split `build_eval_sample.py` uses on
snowball4. `Total` >= 100,000 is 10,159 strings carrying roughly 80% of all
records, so it gets sampled at about 15% and carries the headline.

Rows with an empty `Total` (15.1% of the table) are outside the sampling
frame: the band range queries in `build_mnt_pool` match none of them. The
metric is defined over MNT rows with a known record count.

No network. Reads the pool, the PA export, and the development exclusion
lists; writes the inputs, the labels, and the band weights.
"""
import argparse
import csv
import hashlib
import json
import os
import random
import sys

from build_mnt_pool import DEFAULT_OUT as DEFAULT_POOL
from build_mnt_pool import read_pool, read_population
from labels import ABSTAIN, write_labels
from mnt_keys import canonicalize_place

csv.field_size_limit(sys.maxsize)

SEED = 42
BAND_ORDER = ('head', 'mid', 'low', 'tail')
QUOTAS = {'head': 1500, 'mid': 1500, 'low': 1200, 'tail': 800}

VERIFIED = 'UUID Verified'
# Curator verdicts that are not a UUID. Kept, because abstaining on them is
# the correct answer and that is the only test the low-evidence gate gets.
PSEUDO_IDS = {'ILL': 'mnt_illegible', 'AMB': 'mnt_ambiguous'}

DEFAULT_PA = ('/Users/natelemonnier/storied/resources/'
              'place-authority-mnt-tsv/PA6_16_2026v77.tsv')
DEFAULT_EXCLUSIONS = (
    '/Users/natelemonnier/storied/resources/ground-truth-locations/'
    'Ground truth 6_17 - 7_9.tsv',
    '/Users/natelemonnier/storied/code/place-normalizer/utils/'
    'snowball2_ground_truth.tsv',
)
DEFAULT_OUT_DIR = 'eval/data'


def load_pa_ids(path):
    """Authority UUIDs from the PA export, keyed on `ID`, not `UUID`."""
    with open(path, encoding='utf-8-sig', newline='') as f:
        return {(r.get('ID') or '').strip().upper()
                for r in csv.DictReader(f, delimiter='\t')
                if (r.get('ID') or '').strip()}


def load_exclusions(paths):
    """Place strings already seen during development, read from `place`."""
    seen = set()
    for path in paths:
        if not os.path.exists(path):
            continue
        with open(path, encoding='utf-8-sig', newline='') as f:
            for rec in csv.DictReader(f, delimiter='\t'):
                place = (rec.get('place') or '').strip()
                if place:
                    seen.add(canonicalize_place(place))
    return seen


def drop_reason(row, pa_ids, exclusions):
    """Why this pool row cannot carry a label, or None when it can."""
    place = (row.get('place') or '').strip()
    if not place:
        return 'no_place'
    if (row.get('match_status') or '').strip() != VERIFIED:
        return 'not_verified'
    authority_id = (row.get('authority_id') or '').strip()
    if not authority_id:
        return 'no_authority_id'
    if authority_id.upper() in PSEUDO_IDS:
        return None
    if authority_id.upper() not in pa_ids:
        return 'no_pa_record'
    if canonicalize_place(place) in exclusions:
        return 'seen_in_development'
    return None


def filter_pool(rows, pa_ids, exclusions):
    """Split the pool into usable rows and a count of why the rest went."""
    kept, dropped = [], {}
    for row in rows:
        reason = drop_reason(row, pa_ids, exclusions)
        if reason:
            dropped[reason] = dropped.get(reason, 0) + 1
        else:
            kept.append(row)
    return kept, dropped


def dedupe_canonical(rows):
    """One row per canonical place string.

    1.5% of MNT rows share a canonical form with another row. The guid is
    derived from that form, and the scorer joins on guid, so a duplicate
    would drop a row silently rather than loudly.
    """
    seen = set()
    out = []
    for row in rows:
        key = canonicalize_place(row['place'])
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def guid_for(place):
    digest = hashlib.sha1(canonicalize_place(place).encode('utf-8'))
    return f'mnt-{digest.hexdigest()[:12]}'


def stratified_sample(rows, quotas, seed):
    buckets = {b: [] for b in BAND_ORDER}
    for row in rows:
        if row['band'] in buckets:
            buckets[row['band']].append(row)
    out = {}
    for band in BAND_ORDER:
        # Sorted first so the draw depends on the seed and not on the order
        # FileMaker happened to return pages in.
        pool = sorted(buckets[band], key=lambda r: canonicalize_place(r['place']))
        rng = random.Random(f'{seed}:{band}')
        out[band] = rng.sample(pool, min(quotas[band], len(pool)))
    return out


def split_dev_heldout(sampled, seed):
    dev, heldout = [], []
    for band in BAND_ORDER:
        rows = list(sampled[band])
        rng = random.Random(f'{seed}:split:{band}')
        rng.shuffle(rows)
        half = len(rows) // 2
        dev.extend(rows[:half])
        heldout.extend(rows[half:half * 2])
    return dev, heldout


def label_status(row):
    pseudo = PSEUDO_IDS.get((row.get('authority_id') or '').strip().upper())
    if pseudo:
        return pseudo
    if (row.get('multiple_uuid') or '').strip().lower() == 'true':
        return 'mnt_verified_multi'
    return 'mnt_verified'


def label_for(row):
    """The curator's answer, or ABSTAIN where the curator refused to give one."""
    authority_id = (row.get('authority_id') or '').strip()
    if authority_id.upper() in PSEUDO_IDS:
        return ABSTAIN
    return authority_id.upper()


def label_row(row):
    """One row in the shared LABEL_FIELDS schema.

    `label_world` repeats `label_string_only` because a curator worked from
    the same string the matcher gets, so the world-knowledge delta the
    snowball labelers measure is zero here by construction.
    """
    label = label_for(row)
    status = label_status(row)
    return {
        'guid': guid_for(row['place']),
        'place': row['place'],
        'band': row['band'],
        'leaf_string_only': row.get('authority_name', ''),
        'chain_string_only': row.get('typeahead', ''),
        'label_string_only': label,
        'status_string_only': status,
        'leaf_world': row.get('authority_name', ''),
        'chain_world': row.get('typeahead', ''),
        'label_world': label,
        'status_world': status,
    }


def input_row(row):
    return {'place': row['place'], 'guid': guid_for(row['place']),
            'frequency': row.get('total', ''), 'band': row['band']}


def band_weights(pool_rows, populations):
    """Per-band string count and estimated record count.

    The string count is exact — FileMaker's foundCount for the band's `Total`
    range. The record count is that count times the mean `Total` over the
    pool rows in the band, which is an estimate and is marked as one. There
    is no way to sum a field across a found set through the Data API.
    """
    weights = {}
    for band in BAND_ORDER:
        totals = [int(r['total']) for r in pool_rows
                  if r['band'] == band and (r.get('total') or '').isdigit()]
        mean = sum(totals) / len(totals) if totals else 0.0
        strings = int(populations.get(band, 0))
        weights[band] = {
            'strings': strings,
            'records': int(round(strings * mean)),
            'mean_total': round(mean, 1),
            'sampled': len(totals),
            'records_estimated': True,
        }
    return weights


def write_input(path, rows, seed):
    with open(path, 'w', encoding='utf-8', newline='') as f:
        f.write(f'# seed={seed} source=MNT '
                f'generated_from=build_mnt_eval_sample.py\n')
        w = csv.DictWriter(f, fieldnames=['place', 'guid', 'frequency', 'band'],
                           delimiter='\t', extrasaction='ignore')
        w.writeheader()
        w.writerows(input_row(r) for r in rows)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--pool', default=DEFAULT_POOL)
    p.add_argument('--pa', default=DEFAULT_PA)
    p.add_argument('--exclusions', nargs='*', default=list(DEFAULT_EXCLUSIONS))
    p.add_argument('--out-dir', default=DEFAULT_OUT_DIR)
    p.add_argument('--seed', type=int, default=SEED)
    args = p.parse_args(argv)

    os.makedirs(args.out_dir, exist_ok=True)
    pool = read_pool(args.pool)
    populations = read_population(args.pool)
    pa_ids = load_pa_ids(args.pa)
    exclusions = load_exclusions(args.exclusions)

    kept, dropped = filter_pool(pool, pa_ids, exclusions)
    kept = dedupe_canonical(kept)
    dev, heldout = split_dev_heldout(
        stratified_sample(kept, QUOTAS, args.seed), args.seed)

    write_input(f'{args.out_dir}/mnt_dev.tsv', dev, args.seed)
    write_input(f'{args.out_dir}/mnt_heldout.tsv', heldout, args.seed)
    write_labels(f'{args.out_dir}/mnt_labels_dev.tsv',
                 [label_row(r) for r in dev], 'MNT')
    write_labels(f'{args.out_dir}/mnt_labels_heldout.tsv',
                 [label_row(r) for r in heldout], 'MNT')

    weights = band_weights(pool, populations)
    with open(f'{args.out_dir}/mnt_bands.json', 'w', encoding='utf-8') as f:
        json.dump({'seed': args.seed, 'source': 'MNT', 'bands': weights,
                   'pool_rows': len(pool), 'usable_rows': len(kept),
                   'dropped_from_pool': dropped}, f, indent=2)

    print(f'pool {len(pool)} rows, {len(kept)} usable')
    for reason, count in sorted(dropped.items(), key=lambda kv: -kv[1]):
        print(f'  dropped {reason:20s} {count}')
    total_records = sum(w['records'] for w in weights.values()) or 1
    for band in BAND_ORDER:
        w = weights[band]
        n_dev = sum(1 for r in dev if r['band'] == band)
        print(f'{band:5s} strings={w["strings"]:>9,} '
              f'records~{w["records"]:>13,} ({w["records"]/total_records:5.1%}) '
              f'dev={n_dev}')
    print(f'dev={len(dev)} heldout={len(heldout)}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
