#!/usr/bin/env python3
"""Expand RTL matcher output into per-jurisdiction-level JSON.

Reads two files the matcher writes together: the main results TSV and the
`_levels.tsv` provenance side file, which carries one row per level of each
winning chain along with the input token that produced it and the method
that reached it. This script joins them on guid and reshapes.

An earlier version rebuilt the chain itself, pulling authority_place from
Supabase and recovering the input token by string-matching level names back
against the original string. That could not see a fuzzy match at all — a row
resolving `Roannke` to Roanoke showed a null token — and it disagreed with
the matcher about where terms begin and end, because it split on commas
while the matcher splits on commas and semicolons. Both problems are fixed
at the source now, so this file only joins and reshapes.

Usage:
  python3 format_levels.py <results.tsv> [--levels PATH] [--sample N]
                           [--seed S] [--out PATH]

--levels defaults to the sibling `<stem>_levels.tsv` the matcher wrote.
"""

import argparse
import csv
import json
import sys
from collections import defaultdict
from pathlib import Path

csv.field_size_limit(10 ** 9)

# Levels carried in the `levels` block. authority_place also holds level 2
# (neighborhoods and institutions) and level 11 (continent). Level 2 is being
# retired, so a match landing there is reported separately rather than shown
# as the answer — see `matched_below_supported`. There is no level 1.
LEVELS = list(range(3, 11))


def _flag(value):
    return bool(value and value.strip())


def _int_or_none(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def load_level_provenance(path):
    """guid -> [level rows], leaf first, as the matcher emitted them."""
    by_guid = defaultdict(list)
    with open(path, newline='', encoding='utf-8') as f:
        for row in csv.DictReader(f, delimiter='\t'):
            by_guid[row['guid']].append(row)
    for rows in by_guid.values():
        rows.sort(key=lambda r: _int_or_none(r['depth_from_leaf']) or 0)
    return by_guid


def build_entry(row, provenance):
    """One JSON entry: the matcher's answer plus how it got there."""
    kind = row.get('resolution_kind', '')
    chain = provenance.get(row['guid'], [])

    entry = {
        'original': row['original'],
        'guid': row['guid'],
        'frequency': _int_or_none(row['frequency']),
        'match_type': row['match_type'],
        'confidence': row['confidence'],
        'resolution_kind': kind,
        # A tie means the ranking could not choose. parent_rejected used to
        # land here too, which is why it read as ambiguous while carrying a
        # single candidate; it is now its own kind.
        'ambiguous': kind == 'tie',
        'parent_suspect': kind == 'suspect',
        'candidate_count': _int_or_none(row['candidates']) or 0,
        'leaf_uuid': row.get('supported_leaf_id') or None,
        'matched_below_supported': _flag(row.get('below_supported')),
        'matched_level': _int_or_none(row.get('matched_level')),
        'unsupported_in_candidates': _flag(row.get('unsupported_in_candidates')),
        'source_encoding_suspect': _flag(row.get('source_encoding_suspect')),
        'source_shape': (row.get('source_shape') or '').split(';')
                        if row.get('source_shape') else [],
        'matched_below': None,
        'levels': {},
    }

    for node in chain:
        level = _int_or_none(node['level'])
        detail = {
            'raw': node['raw_term'] or None,
            'uuid': node['uuid'],
            'name': node['name'],
            'jurisdiction': node['jurisdiction'],
            'match_method': node['match_method'],
            'origin': node['origin'] or None,
        }
        if level in LEVELS:
            key = str(level)
            # A chain holds one node per level; if two collide the more
            # specific (leaf-ward, seen first) node wins.
            entry['levels'].setdefault(key, detail)
        elif entry['matched_below'] is None and node['depth_from_leaf'] == '0':
            # The match itself sat outside the supported range. Kept whole so
            # the auditor can see what actually matched, rather than only
            # being told that something did.
            entry['matched_below'] = dict(detail, level=level)

    entry['levels'] = {k: entry['levels'][k]
                       for k in sorted(entry['levels'], key=int)}
    return entry


def summarize(entries):
    shapes = defaultdict(int)
    for entry in entries:
        for tag in entry['source_shape']:
            shapes[tag] += 1
    matched = [e for e in entries if e['leaf_uuid'] or e['matched_below']]
    return {
        'rows': len(entries),
        'with_a_leaf': len(matched),
        'matched_below_supported': sum(1 for e in entries
                                       if e['matched_below_supported']),
        'unsupported_in_candidates': sum(1 for e in entries
                                         if e['unsupported_in_candidates']),
        'encoding_suspect': sum(1 for e in entries
                                if e['source_encoding_suspect']),
        'source_shapes': dict(sorted(shapes.items())),
    }


def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('input', help="Matcher results TSV")
    parser.add_argument('--levels',
                        help="Level provenance TSV "
                             "(default: <stem>_levels.tsv beside the input)")
    parser.add_argument('--sample', type=int, default=0,
                        help="Random sample size (0 = all rows)")
    parser.add_argument('--seed', type=int, default=42,
                        help="Random seed for the sample (default: 42)")
    parser.add_argument('--out', help="Output JSON path")
    args = parser.parse_args()

    in_path = Path(args.input)
    levels_path = Path(args.levels) if args.levels else in_path.with_name(
        in_path.stem + '_levels.tsv')
    if not levels_path.exists():
        raise SystemExit(
            f"level provenance file not found: {levels_path}\n"
            "Re-run rtl_matcher.py; it writes this alongside the results TSV.")

    suffix = f'_{args.sample}' if args.sample else ''
    out_path = Path(args.out) if args.out else in_path.with_name(
        in_path.stem + f'_levels{suffix}.json')

    with in_path.open(newline='', encoding='utf-8') as f:
        rows = list(csv.DictReader(f, delimiter='\t'))
    print(f"read {len(rows)} rows from {in_path}", file=sys.stderr)

    missing = [c for c in ('supported_leaf_id', 'resolution_kind')
               if c not in (rows[0] if rows else {})]
    if missing:
        raise SystemExit(
            f"input TSV is missing {missing}; it predates the provenance "
            "changes. Re-run rtl_matcher.py against the source data.")

    if args.sample and args.sample < len(rows):
        import random
        rows = random.Random(args.seed).sample(rows, args.sample)
        print(f"sampled {len(rows)} rows (seed {args.seed})", file=sys.stderr)

    provenance = load_level_provenance(levels_path)
    print(f"level provenance for {len(provenance)} rows from {levels_path}",
          file=sys.stderr)

    entries = [build_entry(r, provenance) for r in rows]
    summary = summarize(entries)

    payload = {
        'source': str(in_path),
        'row_count': len(entries),
        'sample': {'size': args.sample, 'seed': args.seed} if args.sample else None,
        'levels_emitted': LEVELS,
        'run': summary,
        'entries': entries,
    }
    with out_path.open('w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    total = len(entries)
    print(f"wrote {total} entries to {out_path}", file=sys.stderr)
    print(f"  with a leaf candidate: {summary['with_a_leaf']} "
          f"({summary['with_a_leaf'] / total:.1%})", file=sys.stderr)
    print(f"  ambiguous (tie):       "
          f"{sum(1 for e in entries if e['ambiguous'])}", file=sys.stderr)
    print(f"  parent suspect:        "
          f"{sum(1 for e in entries if e['parent_suspect'])}", file=sys.stderr)
    print(f"  matched below level {LEVELS[0]}: "
          f"{summary['matched_below_supported']}", file=sys.stderr)

    methods = defaultdict(int)
    for entry in entries:
        for node in entry['levels'].values():
            methods[node['match_method']] += 1
    nodes = sum(methods.values())
    if nodes:
        detail = ', '.join(f"{m} {c} ({c / nodes:.0%})"
                           for m, c in sorted(methods.items(),
                                              key=lambda kv: -kv[1]))
        print(f"  level nodes: {nodes} — {detail}", file=sys.stderr)
    for level in LEVELS:
        key = str(level)
        found = [e['levels'][key] for e in entries if key in e['levels']]
        raw = sum(1 for n in found if n['raw'])
        if found:
            print(f"  L{level}: {len(found)} nodes, {raw} with a raw term "
                  f"({raw / len(found):.0%})", file=sys.stderr)


if __name__ == '__main__':
    main()
