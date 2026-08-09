"""Label the eval sample with Gemini.

Two stages. Identify runs in batches of 5 and never sees PA. Disambiguate runs
only for leaves whose Term maps to several PA records, and presents the real
FullChainName values so the model picks an index rather than emitting a UUID —
hallucinated IDs are structurally impossible.

Every outbound place string passes through prompt.redact_for_transport first,
so no specific residential address leaves the machine.

Needs GEMINI_API_KEY in anaconda-2/.env and `pip install google-genai`.
"""
import argparse
import csv
import os
import sys
import time

from labels import NONE, write_labels
from pa_index import PAIndex
from prompt import (SYSTEM, build_disambiguate_prompt, build_identify_prompt,
                    parse_disambiguate_response, parse_identify_response)

DEFAULT_PA = ('/Users/natelemonnier/storied/resources/'
              'place-authority-mnt-tsv/PA6_16_2026v77.tsv')
BATCH = 5
EMPTY = {'leaf_string_only': '', 'chain_string_only': '',
         'leaf_world': '', 'chain_world': ''}

csv.field_size_limit(sys.maxsize)


def load_env(path):
    if not os.path.exists(path):
        return
    with open(path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, _, v = line.partition('=')
                os.environ.setdefault(k.strip(), v.strip())


def read_sample(path):
    with open(path, encoding='utf-8', newline='') as f:
        lines = [ln for ln in f if not ln.startswith('#')]
    return list(csv.DictReader(lines, delimiter='\t'))


def resolve_with_walk(index, leaf, chain, ask=None):
    """Resolve a proposed leaf, climbing the proposed chain when it is absent.

    Returns (uuid_or_NONE, status). `ask` is called only when a leaf maps to
    several PA records and the proposed chain does not separate them; without
    it, such a row lands as NONE for a human to settle.
    """
    if not leaf and not chain:
        return NONE, 'none_after_walk'

    parts = [p.strip() for p in (chain or leaf).split(',') if p.strip()]
    if leaf and (not parts or parts[0].lower() != leaf.lower()):
        parts.insert(0, leaf)

    for i, candidate_leaf in enumerate(parts):
        remaining = ', '.join(parts[i:])
        res = index.resolve(candidate_leaf, remaining)
        if res.status in ('unique', 'chain_matched'):
            return res.uuid, res.status
        if res.status == 'needs_disambiguation' and ask is not None:
            picked = ask(remaining, [c.full_chain for c in res.candidates])
            if picked is not None:
                return res.candidates[picked].uuid, 'model_disambiguated'
        if res.status == 'replaced':
            return NONE, 'replaced'
    return NONE, 'none_after_walk'


def label_rows(sample, index, call, sleep=0.0, log=None):
    """Label every sample row. `call(text) -> str` is the model transport."""
    def ask(place, chains):
        try:
            return parse_disambiguate_response(
                call(build_disambiguate_prompt(place, chains)), len(chains))
        except Exception as exc:
            if log:
                log(f'disambiguate failed: {exc}')
            return None

    out = []
    for start in range(0, len(sample), BATCH):
        chunk = sample[start:start + BATCH]
        try:
            answers = parse_identify_response(
                call(build_identify_prompt(chunk)), expected=len(chunk))
        except Exception as exc:
            if log:
                log(f'batch at {start} failed: {exc}')
            answers = [dict(EMPTY) for _ in chunk]

        for src, ans in zip(chunk, answers):
            so_id, so_status = resolve_with_walk(
                index, ans['leaf_string_only'], ans['chain_string_only'], ask)
            w_id, w_status = resolve_with_walk(
                index, ans['leaf_world'], ans['chain_world'], ask)
            out.append({
                'guid': src['guid'], 'place': src['place'], 'band': src['band'],
                'leaf_string_only': ans['leaf_string_only'],
                'chain_string_only': ans['chain_string_only'],
                'label_string_only': so_id, 'status_string_only': so_status,
                'leaf_world': ans['leaf_world'],
                'chain_world': ans['chain_world'],
                'label_world': w_id, 'status_world': w_status,
            })

        if log:
            log(f'{len(out)}/{len(sample)}')
        if sleep:
            time.sleep(sleep)
    return out


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--sample', required=True)
    p.add_argument('--out', required=True)
    p.add_argument('--pa', default=DEFAULT_PA)
    p.add_argument('--env', default='.env')
    p.add_argument('--model', default='gemini-2.5-flash',
                   help='current Flash-tier model name from AI Studio')
    p.add_argument('--sleep', type=float, default=0.5)
    args = p.parse_args(argv)

    load_env(args.env)
    key = os.environ.get('GEMINI_API_KEY')
    if not key:
        print('GEMINI_API_KEY not set; add it to anaconda-2/.env', file=sys.stderr)
        return 1

    from google import genai
    client = genai.Client(api_key=key)

    def call(text):
        resp = client.models.generate_content(
            model=args.model, contents=f'{SYSTEM}\n\n{text}')
        return resp.text or ''

    index = PAIndex.from_tsv(args.pa)
    sample = read_sample(args.sample)
    rows = label_rows(sample, index, call, sleep=args.sleep,
                      log=lambda m: print(m, file=sys.stderr))

    write_labels(args.out, rows, source=f'gemini:{args.model}')
    resolved = sum(1 for r in rows if r['label_string_only'] != NONE)
    print(f'labeled {len(rows)} rows, {resolved} resolved from string alone')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
