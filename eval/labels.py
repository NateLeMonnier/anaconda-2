"""Shared label schema for the eval set.

Two label columns per row, a holdover from a labeling pass that used two model
families and has since been dropped in favour of the curators' own mappings.
`label_string_only` drives the metric. `label_world` was for what outside
knowledge could reach; the MNT builder writes the same value into both, so the
distinction currently carries no signal and is kept only so existing label
files parse.

Two sentinels stand in for a UUID, and they are not the same thing. `NONE`
means no correct answer exists, so the row leaves the denominator. `ABSTAIN`
means the correct answer is to claim nothing — a curator marked the string
Illegible or Ambiguous — so the row stays in the denominator and an empty
`authority_id` scores correct. Only the MNT set emits `ABSTAIN`; it is what
measures the low-evidence gate without a second metric.
"""
import csv
import sys

csv.field_size_limit(sys.maxsize)

NONE = 'NONE'
ABSTAIN = 'ABSTAIN'

LABEL_FIELDS = [
    'guid', 'place', 'band',
    'leaf_string_only', 'chain_string_only',
    'label_string_only', 'status_string_only',
    'leaf_world', 'chain_world',
    'label_world', 'status_world',
]


def read_labels(path):
    """Read a labeler TSV into {guid: row}, skipping the provenance comment."""
    out = {}
    with open(path, encoding='utf-8', newline='') as f:
        lines = [ln for ln in f if not ln.startswith('#')]
    for rec in csv.DictReader(lines, delimiter='\t'):
        out[rec['guid']] = {k: (rec.get(k) or '') for k in LABEL_FIELDS}
    return out


def write_labels(path, rows, source):
    with open(path, 'w', encoding='utf-8', newline='') as f:
        f.write(f'# source={source} generated_from=eval/labels.py\n')
        w = csv.DictWriter(f, fieldnames=LABEL_FIELDS, delimiter='\t',
                           extrasaction='ignore')
        w.writeheader()
        w.writerows(rows)
