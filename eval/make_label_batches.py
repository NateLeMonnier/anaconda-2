"""Split the eval sample into self-contained prompt files for subagent labeling.

Each batch file carries the same identify rules the Gemini labeler uses, so the
two labelers answer the same question. Batches are 100 rows; a subagent labels
one file and writes JSON to eval/data/claude_responses/batch_NNN.json.
"""
import argparse
import csv
import json
import os
import sys

from prompt import SYSTEM, build_identify_prompt

csv.field_size_limit(sys.maxsize)
BATCH = 100


def read_sample(path):
    with open(path, encoding='utf-8', newline='') as f:
        lines = [ln for ln in f if not ln.startswith('#')]
    return list(csv.DictReader(lines, delimiter='\t'))


def write_batches(sample, out_dir):
    """Write one prompt file per batch. Returns the paths written."""
    os.makedirs(out_dir, exist_ok=True)
    paths = []
    for i, start in enumerate(range(0, len(sample), BATCH), 1):
        chunk = sample[start:start + BATCH]
        path = f'{out_dir}/batch_{i:03d}.md'
        with open(path, 'w', encoding='utf-8') as f:
            f.write(f'{SYSTEM}\n\n{build_identify_prompt(chunk)}\n\n')
            f.write('Write your JSON array to '
                    f'`eval/data/claude_responses/batch_{i:03d}.json`. '
                    'Use these guids in order, one object per guid, adding a '
                    '"guid" field to each object:\n')
            f.write(json.dumps([r['guid'] for r in chunk], indent=2))
            f.write('\n')
        paths.append(path)
    return paths


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--sample', required=True)
    p.add_argument('--out-dir', default='eval/data/claude_batches')
    args = p.parse_args(argv)

    sample = read_sample(args.sample)
    if not sample:
        print('sample is empty', file=sys.stderr)
        return 1

    paths = write_batches(sample, args.out_dir)
    for path in paths:
        print(path)
    print(f'{len(sample)} rows in {len(paths)} batches', file=sys.stderr)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
