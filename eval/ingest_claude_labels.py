"""Ingest subagent-produced Claude label JSON into the shared label TSV."""
import argparse
import csv
import glob
import json
import sys

from label_gemini import resolve_with_walk
from labels import NONE, write_labels
from pa_index import PAIndex

DEFAULT_PA = ('/Users/natelemonnier/storied/resources/'
              'place-authority-mnt-tsv/PA6_16_2026v77.tsv')
_KEYS = ('leaf_string_only', 'chain_string_only', 'leaf_world', 'chain_world')

csv.field_size_limit(sys.maxsize)


def read_sample(path):
    with open(path, encoding='utf-8', newline='') as f:
        lines = [ln for ln in f if not ln.startswith('#')]
    return list(csv.DictReader(lines, delimiter='\t'))


def load_responses(pattern):
    """Merge every batch JSON into {guid: {four fields}}."""
    merged = {}
    for path in sorted(glob.glob(pattern)):
        with open(path, encoding='utf-8') as f:
            for item in json.load(f):
                guid = str(item.get('guid') or '').strip()
                if guid:
                    merged[guid] = {k: str(item.get(k) or '').strip()
                                    for k in _KEYS}
    return merged


def rows_from_responses(sample, responses, index):
    """Every sample row gets a label row. Missing responses become NONE.

    No `ask` callback: a leaf the proposed chain cannot separate lands as NONE
    rather than a guess, which disagrees with Gemini and routes to review —
    the correct outcome for an ambiguity a human should settle.
    """
    out = []
    for src in sample:
        ans = responses.get(src['guid'], {k: '' for k in _KEYS})
        so_id, so_status = resolve_with_walk(
            index, ans['leaf_string_only'], ans['chain_string_only'])
        w_id, w_status = resolve_with_walk(
            index, ans['leaf_world'], ans['chain_world'])
        out.append({
            'guid': src['guid'], 'place': src['place'], 'band': src['band'],
            'leaf_string_only': ans['leaf_string_only'],
            'chain_string_only': ans['chain_string_only'],
            'label_string_only': so_id, 'status_string_only': so_status,
            'leaf_world': ans['leaf_world'], 'chain_world': ans['chain_world'],
            'label_world': w_id, 'status_world': w_status,
        })
    return out


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--sample', required=True)
    p.add_argument('--responses', default='eval/data/claude_responses/*.json')
    p.add_argument('--out', required=True)
    p.add_argument('--pa', default=DEFAULT_PA)
    args = p.parse_args(argv)

    index = PAIndex.from_tsv(args.pa)
    sample = read_sample(args.sample)
    responses = load_responses(args.responses)
    rows = rows_from_responses(sample, responses, index)

    write_labels(args.out, rows, source='claude:subagents')
    resolved = sum(1 for r in rows if r['label_string_only'] != NONE)
    unanswered = len(sample) - len(set(responses) & {r['guid'] for r in sample})
    print(f'labeled {len(rows)} rows, {resolved} resolved from string alone')
    if unanswered:
        print(f'{unanswered} sample rows had no response and became {NONE}',
              file=sys.stderr)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
