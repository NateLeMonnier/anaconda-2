"""Classify what an eval run got wrong, and write it up as markdown.

The scorer answers how much is wrong. This answers what kind of wrong, which
is the part that decides where to spend effort. Rerun after every matcher
change; the taxonomy shifts more than the headline does.

The split that matters most is reachability — whether the truth was findable
from the string at all:

  reachable      the truth's name appears in the input AND in PA, and its
                 parent chain connects to something else the string names.
                 Matching logic can close these.
  unsupported    the name is reachable but nothing in the string corroborates
                 it. Committing means loosening the low-evidence gate.
  absent         no form of the truth appears. Needs world knowledge.

Everything downstream of that is a breakdown of the reachable half.
"""
import argparse
import collections
import csv
import os
import re
import sys
import unicodedata

csv.field_size_limit(sys.maxsize)

_PAREN = re.compile(r'\([^)]*\)')
_APOSTROPHE = str.maketrans('', '', "'’")


def normalize_term(s):
    """Casefold, strip accents and parentheticals, drop punctuation.

    Apostrophes are deleted rather than spaced, so "St. Mary's" gives
    "st marys". Parentheticals go because PA marks superseded places inline:
    Term "Prussia", FullChainName leaf "Prussia (historical)".
    """
    if not s:
        return ''
    decomposed = unicodedata.normalize('NFKD', _PAREN.sub(' ', s))
    stripped = ''.join(c for c in decomposed if not unicodedata.combining(c))
    stripped = stripped.lower().translate(_APOSTROPHE)
    kept = ''.join(c if c.isalnum() else ' ' for c in stripped)
    return ' '.join(kept.split())

DEFAULT_PA = ('/Users/natelemonnier/storied/resources/'
              'place-authority-mnt-tsv/PA6_16_2026v77.tsv')
ABSTAIN = 'ABSTAIN'
NONE = 'NONE'

LEVEL_NAMES = {'2': 'neighborhood', '3': 'borough/town', '4': 'city/village',
               '5': 'county', '6': 'state', '7': 'country region',
               '8': 'country', '9': 'region', '10': 'kingdom',
               '11': 'continent'}

# Tokens that wrap a place name inside a census term the walk then discards.
NOISE_PATTERNS = (
    ('UK "(part of)" fragment', re.compile(r'part of|prt')),
    ('enumeration / precinct / ward', re.compile(
        r'\b(precinct|district|beat|ward|division|sec|magisterial|election'
        r'|civil|pct)\b|\d')),
    ('jurisdiction suffix', re.compile(
        r'\b(city|town|township|twp|village|borough|county|co)\b')),
)


def read_tsv(path):
    with open(path, encoding='utf-8-sig', newline='') as f:
        lines = [ln for ln in f if not ln.startswith('#')]
    return list(csv.DictReader(lines, delimiter='\t'))


def load_pa(path):
    """id -> {name, level, parent}, plus a normalized-name index."""
    records, by_name = {}, collections.defaultdict(list)
    with open(path, encoding='utf-8-sig', newline='') as f:
        for row in csv.DictReader(f, delimiter='\t'):
            uid = (row.get('ID') or '').strip().upper()
            if not uid:
                continue
            rec = {'name': (row.get('Term') or '').strip(),
                   'level': (row.get('Level') or '').strip(),
                   'parent': (row.get('ParentID') or '').strip().upper()}
            records[uid] = rec
            if rec['name']:
                by_name[normalize_term(rec['name'])].append(uid)
    return records, by_name


def ancestors(uid, pa):
    out, seen = [], set()
    while uid in pa and uid not in seen:
        seen.add(uid)
        uid = pa[uid]['parent']
        if uid:
            out.append(uid)
    return out


def name_in(name, text):
    """Whole-token containment, so CAMBRIDGESHIRE does not match Cambridge."""
    want, have = normalize_term(name).split(), normalize_term(text).split()
    if not want:
        return False
    return any(have[i:i + len(want)] == want
               for i in range(len(have) - len(want) + 1))


def string_terms(place):
    return [t.strip() for t in re.split(r'[,;]', place) if t.strip()]


def reachability(row, pa, by_name):
    """One of 'reachable', 'unsupported', 'absent'."""
    truth = row['truth_id']
    if truth not in pa:
        return 'absent'
    reached = set()
    for term in string_terms(row['place']):
        reached.update(by_name.get(normalize_term(term), ()))
    named = any(name_in(pa[truth]['name'], t)
                for t in string_terms(row['place']))
    if not named and truth not in reached:
        return 'absent'
    if set(ancestors(truth, pa)) & (reached - {truth}):
        return 'reachable'
    return 'unsupported'


def failure_class(row, pa):
    """Why a reachable row still missed."""
    truth, got = row['truth_id'], row['matcher_id']
    skipped = [t for t in row['skipped_terms'].split('; ')
               if t and 'proximity:' not in t]
    hit = next((t for t in skipped if name_in(pa[truth]['name'], t)), None)
    if hit is not None:
        residue = [w for w in normalize_term(hit).split()
                   if w not in normalize_term(pa[truth]['name']).split()]
        if not residue:
            return 'leaf discarded, term was exactly the answer'
        blob = ' '.join(residue)
        for label, pattern in NOISE_PATTERNS:
            if pattern.search(blob):
                return f'leaf discarded, wrapped in noise: {label}'
        return 'leaf discarded, wrapped in noise: other tokens'
    if row['verdict'] == 'abstain':
        return f'abstained ({row["match_type"]})'
    if got in ancestors(truth, pa):
        return 'committed to an ancestor of the leaf'
    if got in pa and normalize_term(pa[got]['name']) == \
            normalize_term(pa[truth]['name']):
        return (f'same name, wrong record '
                f'(got L{pa[got]["level"]}, truth L{pa[truth]["level"]})')
    return 'committed to an unrelated place'


def build_rows(labels, detail, output):
    by_guid_detail = {r['guid']: r for r in detail}
    by_guid_out = {r['guid']: r for r in output}
    rows = []
    for label in labels:
        guid = label['guid']
        d, o = by_guid_detail.get(guid, {}), by_guid_out.get(guid, {})
        rows.append({
            'guid': guid,
            'place': label['place'],
            'band': label['band'],
            'status': label['status_string_only'],
            'truth_id': label['label_string_only'],
            'truth_chain': label['chain_string_only'],
            'verdict': d.get('bucket', 'missing'),
            'match_type': o.get('match_type', ''),
            'matcher_id': (o.get('authority_id') or '').strip().upper(),
            'matcher_chain': o.get('type_ahead', ''),
            'skipped_terms': o.get('skipped_terms', ''),
            'candidates': o.get('candidates', ''),
            'frequency': o.get('frequency', ''),
        })
    return rows


def counts_table(title, groups, total, out, minimum=25):
    out.append(f'\n### {title}\n')
    out.append('| | n | correct | wrong | abstain |')
    out.append('|---|---|---|---|---|')
    for key, rows in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        if len(rows) < minimum:
            continue
        n = len(rows)
        c = sum(1 for r in rows if r['verdict'] == 'correct')
        w = sum(1 for r in rows if r['verdict'] == 'wrong')
        out.append(f'| {key} | {n:,} | {c / n:.1%} | {w / n:.1%} | '
                   f'{(n - c - w) / n:.1%} |')


def group_by(rows, keyfn):
    g = collections.defaultdict(list)
    for r in rows:
        g[keyfn(r)].append(r)
    return g


def shape_of(place):
    s = normalize_term(place)
    if re.search(r'\b(district|precinct|ward|beat|division|sec|magisterial'
                 r'|election|civil|ed)\b', s):
        return 'enumeration district / precinct'
    if re.search(r'\b(township|twp|town|city|village|borough|parish)\b', s):
        return 'jurisdiction suffix'
    if 'part of' in s or 'prt' in s:
        return 'UK "(part of)"'
    if re.search(r'\b(church|cemetery|hospital|home|farm|school|street|creek'
                 r'|lake|hotel)\b', s):
        return 'names a feature'
    if 'null' in s.split():
        return 'contains NULL'
    if place.isupper():
        return 'all caps, no other marker'
    return 'plain place string'


def report(rows, pa, by_name, run_name):
    out = [f'# Failure analysis — `{run_name}`\n',
           f'Generated by `eval/analyze_failures.py` over {len(rows):,} rows.\n']

    uu = [r for r in rows if r['truth_id'] not in (ABSTAIN, NONE)]
    ab = [r for r in rows if r['truth_id'] == ABSTAIN]
    correct = sum(1 for r in rows if r['verdict'] == 'correct')
    c_uu = sum(1 for r in uu if r['verdict'] == 'correct')
    w_uu = sum(1 for r in uu if r['verdict'] == 'wrong')
    a_uu = len(uu) - c_uu - w_uu

    out.append('\n## Denominators\n')
    out.append('| set | n | accuracy |')
    out.append('|---|---|---|')
    out.append(f'| all rows, unweighted | {len(rows):,} | {correct / len(rows):.1%} |')
    out.append(f'| rows with a real UUID label | {len(uu):,} | {c_uu / len(uu):.1%} |')
    if ab:
        c_ab = sum(1 for r in ab if r["verdict"] == "correct")
        out.append(f'| rows where abstaining is correct | {len(ab):,} | '
                   f'{c_ab / len(ab):.1%} |')

    out.append('\n## Precision and recall, UUID-labelled rows\n')
    committed = c_uu + w_uu
    out.append(f'- precision, correct when it answers: **{c_uu / committed:.1%}**')
    out.append(f'- recall, correct overall: **{c_uu / len(uu):.1%}**')
    out.append(f'- declines to answer: {a_uu / len(uu):.1%} '
               f'({a_uu:,} rows, of which '
               f'{sum(1 for r in uu if r["verdict"] == "abstain" and (r["candidates"] or "0") != "0"):,} '
               f'had candidates and could not choose)')

    fails = [r for r in uu if r['verdict'] != 'correct']
    reach = group_by(fails, lambda r: reachability(r, pa, by_name))
    out.append('\n## Could the answer have been found at all?\n')
    out.append('| class | rows | share of UUID-labelled |')
    out.append('|---|---|---|')
    for key in ('reachable', 'unsupported', 'absent'):
        n = len(reach.get(key, []))
        out.append(f'| {key} | {n:,} | {n / len(uu):.1%} |')
    ceiling = (c_uu + len(reach.get('reachable', []))) / len(uu)
    out.append(f'\nCeiling for string-only matching logic: **{ceiling:.1%}**')

    out.append('\n## Why the reachable ones missed\n')
    out.append('| class | rows | share of UUID-labelled |')
    out.append('|---|---|---|')
    classes = collections.Counter(failure_class(r, pa)
                                  for r in reach.get('reachable', []))
    for key, n in classes.most_common():
        out.append(f'| {key} | {n:,} | {n / len(uu):.1%} |')

    counts_table('By how deep the true answer sits',
                 group_by(uu, lambda r: LEVEL_NAMES.get(
                     pa.get(r['truth_id'], {}).get('level', ''), 'unknown')),
                 len(uu), out)
    counts_table('By string shape',
                 group_by(uu, lambda r: shape_of(r['place'])), len(uu), out)
    counts_table('By case',
                 group_by(uu, lambda r: 'ALL CAPS' if r['place'].isupper()
                          else 'mixed case'), len(uu), out)
    counts_table('By band', group_by(uu, lambda r: r['band']), len(uu), out)
    counts_table('By label status', group_by(rows, lambda r: r['status']),
                 len(rows), out)

    out.append('\n## Examples, worst class first\n')
    by_class = group_by(reach.get('reachable', []), lambda r: failure_class(r, pa))
    for key, _ in classes.most_common(4):
        out.append(f'\n**{key}**\n')
        out.append('```')
        for r in sorted(by_class[key],
                        key=lambda r: -int(r['frequency'] or 0))[:6]:
            out.append(f'{r["place"][:60]}')
            out.append(f'   truth: {r["truth_chain"][:64]}')
            out.append(f'   got:   {r["matcher_chain"][:64] or "(abstained)"}'
                       f'  [{r["match_type"]}]')
        out.append('```')
    return '\n'.join(out) + '\n'


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output', required=True, help='rtl_matcher results TSV')
    p.add_argument('--labels', default='eval/data/mnt_labels_dev.tsv')
    p.add_argument('--detail', default='eval/data/mnt_dev_detail.tsv')
    p.add_argument('--pa', default=DEFAULT_PA)
    p.add_argument('--out', default='docs/failure-analysis.md')
    args = p.parse_args(argv)

    pa, by_name = load_pa(args.pa)
    rows = build_rows(read_tsv(args.labels), read_tsv(args.detail),
                      read_tsv(args.output))
    text = report(rows, pa, by_name, os.path.basename(args.output))
    os.makedirs(os.path.dirname(args.out) or '.', exist_ok=True)
    with open(args.out, 'w', encoding='utf-8') as f:
        f.write(text)
    print(f'wrote {args.out} ({len(rows):,} rows)')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
