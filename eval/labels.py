"""Shared label schema for the eval labelers.

Two label columns per row. `label_string_only` may climb only to places named
in the input string and drives the headline metric, because it scores the
matcher against information it actually receives. `label_world` may climb
using model world knowledge; the delta between them sizes the LLM enrichment
step that was descoped.
"""
import csv
import sys

csv.field_size_limit(sys.maxsize)

NONE = 'NONE'

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
