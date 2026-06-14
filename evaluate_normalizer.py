#!/usr/bin/env python3
"""Place normalization evaluator.

Measures intrinsic quality of any place normalization output and optionally
compares against ground truth. Produces a terminal summary, a JSON report,
and a flagged-rows TSV.

Direct mode (single output file):
    python evaluate_normalizer.py output.tsv --input raw.tsv [--pa PA.tsv]

Pipeline mode (Phase 1 multi-file output):
    python evaluate_normalizer.py --pipeline outputs/ prefix --input raw.tsv
"""

import argparse
import csv
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path


COLUMN_VARIANTS = {
    'guid':         ['guid', 'place_guid'],
    'authority_id': ['authority_id', 'place_id', 'MatchAuthID'],
    'original':     ['original', 'place', 'Input_Original'],
    'frequency':    ['frequency', 'Frequency'],
    'level':        ['level', 'Level'],
    'type_ahead':   ['type_ahead', 'Type_Ahead_Value', 'Typeahead'],
}

LEVEL_LABELS = {
    '1': 'Street', '2': 'Neighborhood', '3': 'Borough',
    '4': 'City', '5': 'County', '6': 'State',
    '7': 'Country Region', '8': 'Country',
    '9': 'Region', '10': 'Kingdom', '11': 'Continent',
}

UUID_PATTERN = re.compile(
    r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'
)


def read_tsv(path):
    with open(path, encoding='utf-8-sig') as f:
        reader = csv.DictReader(f, delimiter='\t')
        reader.fieldnames = [c.strip() for c in reader.fieldnames]
        return list(reader), reader.fieldnames


def detect_column(columns, canonical, override=None):
    if override and override in columns:
        return override
    if override:
        sys.exit(f"Override column '{override}' not found. Available: {columns}")
    for variant in COLUMN_VARIANTS.get(canonical, []):
        for col in columns:
            if col.lower() == variant.lower():
                return col
    return None


def require_column(columns, canonical, override=None):
    col = detect_column(columns, canonical, override)
    if not col:
        variants = COLUMN_VARIANTS.get(canonical, [])
        sys.exit(
            f"Required column '{canonical}' not found.\n"
            f"  Looked for: {variants}\n"
            f"  Available: {columns}\n"
            f"  Use --{canonical.replace('_', '-')}-col to specify."
        )
    return col


def is_valid_uuid(val):
    return bool(val) and UUID_PATTERN.match(val.strip())


def is_amb(val):
    return val and val.strip().lower() == 'amb'


def is_ill(val):
    return val and val.strip().lower() == 'ill'


def tokenize(s):
    return set(re.sub(r'[^a-z0-9 ]', ' ', s.lower()).split()) - {''}


def load_pa_lookup(pa_path):
    rows, cols = read_tsv(pa_path)
    lookup = {}
    for r in rows:
        uid = r.get('ID', '').strip()
        level = r.get('Level', '').strip()
        term = r.get('Term', '').strip()
        chain = r.get('FullChainName', '').strip()
        if uid:
            lookup[uid] = {'level': level, 'term': term, 'chain': chain}
    return lookup


def load_input_file(path, overrides):
    rows, cols = read_tsv(path)
    guid_col = require_column(cols, 'guid', overrides.get('guid'))
    orig_col = detect_column(cols, 'original', overrides.get('original'))
    freq_col = detect_column(cols, 'frequency', overrides.get('frequency'))

    lookup = {}
    for r in rows:
        g = r[guid_col].strip()
        if g:
            lookup[g] = {
                'original': r.get(orig_col, '').strip() if orig_col else '',
                'frequency': int(r.get(freq_col, '1').strip() or '1') if freq_col else 1,
            }
    return lookup


def load_direct(output_path, overrides):
    rows, cols = read_tsv(output_path)
    guid_col = require_column(cols, 'guid', overrides.get('guid'))
    id_col = require_column(cols, 'authority_id', overrides.get('authority_id'))
    orig_col = detect_column(cols, 'original', overrides.get('original'))
    freq_col = detect_column(cols, 'frequency', overrides.get('frequency'))
    level_col = detect_column(cols, 'level', overrides.get('level'))
    ta_col = detect_column(cols, 'type_ahead', overrides.get('type_ahead'))
    skipped_count_col = 'skipped_count' if 'skipped_count' in cols else None
    skipped_terms_col = 'skipped_terms' if 'skipped_terms' in cols else None

    match_type_col = 'match_type' if 'match_type' in cols else None

    unified = []
    for r in rows:
        unified.append({
            'guid': r[guid_col].strip(),
            'authority_id': r[id_col].strip() if r.get(id_col) else '',
            'original': r.get(orig_col, '').strip() if orig_col else '',
            'frequency': int(r.get(freq_col, '1').strip() or '1') if freq_col else 1,
            'level': r.get(level_col, '').strip() if level_col else '',
            'type_ahead': r.get(ta_col, '').strip() if ta_col else '',
            'match_type': r.get(match_type_col, '').strip() if match_type_col else '',
            'skipped_count': int(r.get(skipped_count_col, '0').strip() or '0') if skipped_count_col else None,
            'skipped_terms': r.get(skipped_terms_col, '').strip() if skipped_terms_col else None,
            'source': 'direct',
        })
    return unified


def load_pipeline(directory, prefix, overrides):
    matched_qa_path = os.path.join(directory, f'{prefix}_Matched_QA.tsv')
    final_path = os.path.join(directory, f'{prefix}_anaconda_food_ruled_Final.tsv')
    unmatched_path = os.path.join(directory, f'{prefix}_anaconda_food_ruled_unmatched_place_cleaned.tsv')

    for p, label in [(matched_qa_path, 'Matched_QA'), (final_path, 'Final'), (unmatched_path, 'unmatched_cleaned')]:
        if not os.path.exists(p):
            sys.exit(f"Pipeline mode: expected {label} file not found: {p}")

    unified = []

    qa_rows, qa_cols = read_tsv(matched_qa_path)
    qa_guid = require_column(qa_cols, 'guid', overrides.get('guid'))
    qa_id_col = detect_column(qa_cols, 'authority_id', overrides.get('authority_id'))
    qa_orig = detect_column(qa_cols, 'original', overrides.get('original'))
    qa_freq = detect_column(qa_cols, 'frequency', overrides.get('frequency'))

    qa_match_type_col = 'match_type' if 'match_type' in qa_cols else None

    for r in qa_rows:
        unified.append({
            'guid': r[qa_guid].strip(),
            'authority_id': r.get(qa_id_col, '').strip() if qa_id_col else '',
            'original': r.get(qa_orig, '').strip() if qa_orig else '',
            'frequency': int(r.get(qa_freq, '1').strip() or '1') if qa_freq else 1,
            'level': '',
            'type_ahead': '',
            'match_type': r.get(qa_match_type_col, '').strip() if qa_match_type_col else '',
            'skipped_count': None,
            'skipped_terms': None,
            'source': 'auto_match',
        })

    matched_guids = {row['guid'] for row in unified}

    final_rows, final_cols = read_tsv(final_path)
    f_guid = require_column(final_cols, 'guid', overrides.get('guid'))
    f_id = require_column(final_cols, 'authority_id', overrides.get('authority_id'))

    for r in final_rows:
        g = r[f_guid].strip()
        if g not in matched_guids:
            unified.append({
                'guid': g,
                'authority_id': r[f_id].strip() if r.get(f_id) else '',
                'original': '',
                'frequency': 1,
                'level': '',
                'type_ahead': '',
                'match_type': '',
                'skipped_count': None,
                'skipped_terms': None,
                'source': 'pipeline',
            })

    return unified


def enrich_from_input(unified, input_lookup):
    for row in unified:
        info = input_lookup.get(row['guid'])
        if info:
            if not row['original']:
                row['original'] = info['original']
            if row['frequency'] <= 1:
                row['frequency'] = info['frequency']


def enrich_from_pa(unified, pa_lookup):
    for row in unified:
        if not row['level'] and is_valid_uuid(row['authority_id']):
            pa_rec = pa_lookup.get(row['authority_id'].strip())
            if pa_rec:
                row['level'] = pa_rec['level']
                if not row['type_ahead']:
                    row['type_ahead'] = pa_rec['chain']


def compute_match_rate(unified):
    total_rows = len(unified)
    total_freq = sum(r['frequency'] for r in unified)

    matched = [r for r in unified if is_valid_uuid(r['authority_id'])]
    amb = [r for r in unified if is_amb(r['authority_id'])]
    ill = [r for r in unified if is_ill(r['authority_id'])]
    unmatched = [r for r in unified if not r['authority_id'].strip()]

    matched_freq = sum(r['frequency'] for r in matched)
    amb_freq = sum(r['frequency'] for r in amb)
    ill_freq = sum(r['frequency'] for r in ill)
    unmatched_freq = sum(r['frequency'] for r in unmatched)

    return {
        'total_rows': total_rows,
        'total_frequency': total_freq,
        'matched_rows': len(matched),
        'matched_frequency': matched_freq,
        'matched_row_rate': len(matched) / total_rows if total_rows else 0,
        'matched_freq_rate': matched_freq / total_freq if total_freq else 0,
        'amb_rows': len(amb),
        'amb_frequency': amb_freq,
        'ill_rows': len(ill),
        'ill_frequency': ill_freq,
        'unmatched_rows': len(unmatched),
        'unmatched_frequency': unmatched_freq,
    }


def compute_depth(unified):
    matched = [r for r in unified if is_valid_uuid(r['authority_id'])]
    has_level = [r for r in matched if r['level']]
    if not has_level:
        return None

    dist = Counter(r['level'] for r in has_level)
    total = len(has_level)
    specific = sum(1 for r in has_level if r['level'] in ('4', '5'))

    level_table = []
    for lvl in sorted(dist.keys(), key=lambda x: int(x) if x.isdigit() else 99):
        label = LEVEL_LABELS.get(lvl, lvl)
        count = dist[lvl]
        level_table.append({
            'level': lvl, 'label': label, 'count': count,
            'pct': count / total if total else 0,
        })

    return {
        'total_with_level': total,
        'distribution': level_table,
        'specificity_score': specific / total if total else 0,
    }


def detect_partial(row):
    if row['skipped_count'] is not None and row['skipped_count'] > 0:
        return 'explicit', row.get('skipped_terms') or ''

    if not row['level'] or not row['original']:
        return None, ''

    components = len([c.strip() for c in row['original'].split(',') if c.strip()])
    try:
        level = int(row['level'])
    except ValueError:
        return None, ''

    if components >= 2 and level >= 6:
        return 'inferred', f"{components} components, matched to L{level} {LEVEL_LABELS.get(str(level), '')}"
    if components >= 3 and level >= 5:
        return 'inferred', f"{components} components, matched to L{level} {LEVEL_LABELS.get(str(level), '')}"

    return None, ''


def compute_exact_matches(unified):
    matched = [r for r in unified if is_valid_uuid(r['authority_id'])]
    exact = []
    for r in matched:
        ta = r.get('type_ahead', '')
        orig = r.get('original', '')
        if not ta or not orig:
            continue
        orig_tokens = tokenize(orig)
        ta_tokens = tokenize(ta)
        if orig_tokens and orig_tokens <= ta_tokens:
            exact.append(r)

    exact.sort(key=lambda x: x['frequency'], reverse=True)
    total_matched = len(matched)
    total_matched_freq = sum(r['frequency'] for r in matched)
    exact_freq = sum(r['frequency'] for r in exact)

    return {
        'count': len(exact),
        'rate': len(exact) / total_matched if total_matched else 0,
        'frequency': exact_freq,
        'freq_rate': exact_freq / total_matched_freq if total_matched_freq else 0,
        'top_20': [
            {
                'original': r['original'], 'type_ahead': r['type_ahead'],
                'level': r['level'], 'frequency': r['frequency'],
            }
            for r in exact[:20]
        ],
        '_rows': exact,
    }


def compute_parent_resolved(unified):
    matched = [r for r in unified if is_valid_uuid(r['authority_id'])]
    parent_resolved = [r for r in matched if r.get('match_type') == 'parent_resolved']

    parent_resolved.sort(key=lambda x: x['frequency'], reverse=True)
    total_matched = len(matched)
    total_matched_freq = sum(r['frequency'] for r in matched)
    pr_freq = sum(r['frequency'] for r in parent_resolved)

    return {
        'count': len(parent_resolved),
        'rate': len(parent_resolved) / total_matched if total_matched else 0,
        'frequency': pr_freq,
        'freq_rate': pr_freq / total_matched_freq if total_matched_freq else 0,
        'top_20': [
            {
                'original': r['original'], 'type_ahead': r['type_ahead'],
                'level': r['level'], 'frequency': r['frequency'],
            }
            for r in parent_resolved[:20]
        ],
        '_rows': parent_resolved,
    }


def compute_partials(unified):
    matched = [r for r in unified if is_valid_uuid(r['authority_id'])]
    partials = []
    for r in matched:
        method, detail = detect_partial(r)
        if method:
            partials.append({**r, 'partial_method': method, 'partial_detail': detail})

    partials.sort(key=lambda x: x['frequency'], reverse=True)
    total_matched = len(matched)
    total_matched_freq = sum(r['frequency'] for r in matched)
    partial_freq = sum(p['frequency'] for p in partials)

    return {
        'count': len(partials),
        'rate': len(partials) / total_matched if total_matched else 0,
        'frequency': partial_freq,
        'freq_rate': partial_freq / total_matched_freq if total_matched_freq else 0,
        'top_20': [
            {
                'original': p['original'], 'type_ahead': p['type_ahead'],
                'level': p['level'], 'frequency': p['frequency'],
                'method': p['partial_method'], 'detail': p['partial_detail'],
            }
            for p in partials[:20]
        ],
        '_rows': partials,
    }


def compute_token_overlap(unified):
    matched = [r for r in unified if is_valid_uuid(r['authority_id'])]
    zero_overlap = []
    for r in matched:
        ta = r.get('type_ahead', '')
        orig = r.get('original', '')
        if not ta or not orig:
            continue
        orig_tokens = tokenize(orig)
        ta_tokens = tokenize(ta)
        if orig_tokens and not (orig_tokens & ta_tokens):
            zero_overlap.append(r)

    zero_overlap.sort(key=lambda x: x['frequency'], reverse=True)
    return {
        'zero_overlap_count': len(zero_overlap),
        'top_20': [
            {'original': r['original'], 'type_ahead': r['type_ahead'],
             'frequency': r['frequency']}
            for r in zero_overlap[:20]
        ],
        '_rows': zero_overlap,
    }


def compute_ground_truth(unified, gt_path, overrides):
    gt_rows, gt_cols = read_tsv(gt_path)
    gt_guid = require_column(gt_cols, 'guid', overrides.get('guid'))
    gt_id_col = None
    for variant in ['correct_authority_id', 'correct_place_id', 'authority_id', 'place_id']:
        if variant in gt_cols:
            gt_id_col = variant
            break
    if not gt_id_col:
        sys.exit(f"Ground truth file missing authority ID column. Available: {gt_cols}")

    gt_lookup = {}
    for r in gt_rows:
        g = r[gt_guid].strip()
        if g:
            gt_lookup[g] = r[gt_id_col].strip()

    correct = wrong = false_pos = miss = 0
    correct_freq = wrong_freq = false_pos_freq = miss_freq = 0
    gt_flagged = []

    for row in unified:
        gt_val = gt_lookup.get(row['guid'])
        if gt_val is None:
            continue

        pred = row['authority_id'].strip()
        freq = row['frequency']
        gt_is_resolvable = is_valid_uuid(gt_val)
        pred_is_match = is_valid_uuid(pred)

        if pred == gt_val:
            correct += 1
            correct_freq += freq
        elif pred_is_match and gt_is_resolvable:
            wrong += 1
            wrong_freq += freq
            gt_flagged.append({**row, 'gt_expected': gt_val, 'flag': 'gt_wrong_match'})
        elif pred_is_match and not gt_is_resolvable:
            false_pos += 1
            false_pos_freq += freq
            gt_flagged.append({**row, 'gt_expected': gt_val, 'flag': 'gt_false_positive'})
        elif not pred_is_match and gt_is_resolvable:
            miss += 1
            miss_freq += freq
            gt_flagged.append({**row, 'gt_expected': gt_val, 'flag': 'gt_miss'})
        else:
            correct += 1
            correct_freq += freq

    total = correct + wrong + false_pos + miss
    total_freq = correct_freq + wrong_freq + false_pos_freq + miss_freq
    tp = correct
    tp_freq = correct_freq
    pred_pos = correct + wrong + false_pos
    pred_pos_freq = correct_freq + wrong_freq + false_pos_freq
    actual_pos = correct + wrong + miss
    actual_pos_freq = correct_freq + wrong_freq + miss_freq

    precision = tp / pred_pos if pred_pos else 0
    recall = tp / actual_pos if actual_pos else 0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0
    accuracy = correct / total if total else 0

    precision_fw = tp_freq / pred_pos_freq if pred_pos_freq else 0
    recall_fw = tp_freq / actual_pos_freq if actual_pos_freq else 0
    f1_fw = 2 * precision_fw * recall_fw / (precision_fw + recall_fw) if (precision_fw + recall_fw) else 0
    accuracy_fw = correct_freq / total_freq if total_freq else 0

    gt_flagged.sort(key=lambda x: x['frequency'], reverse=True)

    return {
        'accuracy': accuracy,
        'precision': precision,
        'recall': recall,
        'f1': f1,
        'accuracy_freq_weighted': accuracy_fw,
        'precision_freq_weighted': precision_fw,
        'recall_freq_weighted': recall_fw,
        'f1_freq_weighted': f1_fw,
        'confusion': {
            'correct': correct, 'wrong_match': wrong,
            'false_positive': false_pos, 'miss': miss,
        },
        'confusion_freq': {
            'correct': correct_freq, 'wrong_match': wrong_freq,
            'false_positive': false_pos_freq, 'miss': miss_freq,
        },
        '_flagged': gt_flagged,
    }


def print_summary(name, metrics):
    mr = metrics['match_rate']
    print(f"\n=== Place Normalization Evaluation: {name} ===")
    print(f"Input: {mr['total_rows']:,} rows | Total frequency: {mr['total_frequency']:,}")

    print(f"\nMatch Rate")
    print(f"  Rows:      {mr['matched_rows']:>7,} / {mr['total_rows']:,}  ({mr['matched_row_rate']:.1%})")
    print(f"  Frequency: {mr['matched_frequency']:>7,} / {mr['total_frequency']:,}  ({mr['matched_freq_rate']:.1%})")
    print(f"  Amb: {mr['amb_rows']} ({mr['amb_frequency']:,} freq) | Ill: {mr['ill_rows']} ({mr['ill_frequency']:,} freq) | Unmatched: {mr['unmatched_rows']} ({mr['unmatched_frequency']:,} freq)")

    depth = metrics.get('resolution_depth')
    if depth:
        print(f"\nResolution Depth ({depth['total_with_level']:,} with level data)")
        for entry in depth['distribution']:
            print(f"  {entry['label']} (L{entry['level']}): {entry['count']:>7,}  ({entry['pct']:5.1%})")
        print(f"  Specificity (L4+L5): {depth['specificity_score']:.1%}")
    else:
        print(f"\nResolution Depth: unavailable (no level data; provide --pa)")

    em = metrics['exact_matches']
    print(f"\nExact Matches (all input tokens covered)")
    print(f"  Count: {em['count']:,} / {mr['matched_rows']:,}  ({em['rate']:.1%})")
    print(f"  Freq-weighted: {em['freq_rate']:.1%}")
    if em['top_20']:
        print(f"  Top by frequency:")
        for e in em['top_20'][:5]:
            arrow = f" -> {e['type_ahead']}" if e['type_ahead'] else ''
            print(f"    {e['frequency']:>8,}  {e['original']}{arrow}")

    pr = metrics['parent_resolved']
    print(f"\nParent Resolved")
    print(f"  Count: {pr['count']:,} / {mr['matched_rows']:,}  ({pr['rate']:.1%})")
    print(f"  Freq-weighted: {pr['freq_rate']:.1%}")
    if pr['top_20']:
        print(f"  Top by frequency:")
        for p in pr['top_20'][:5]:
            lvl = f" (L{p['level']})" if p['level'] else ''
            arrow = f" -> {p['type_ahead']}{lvl}" if p['type_ahead'] else ''
            print(f"    {p['frequency']:>8,}  {p['original']}{arrow}")

    pm = metrics['partial_matches']
    print(f"\nPartial Matches")
    print(f"  Count: {pm['count']:,} / {mr['matched_rows']:,}  ({pm['rate']:.1%})")
    print(f"  Freq-weighted: {pm['freq_rate']:.1%}")
    if pm['top_20']:
        print(f"  Top by frequency:")
        for p in pm['top_20'][:5]:
            arrow = f" -> {p['type_ahead']}" if p['type_ahead'] else ''
            print(f"    {p['frequency']:>8,}  {p['original']}{arrow}")

    to = metrics['token_overlap']
    print(f"\nPotential Mismatches (zero token overlap)")
    print(f"  Count: {to['zero_overlap_count']}")
    if to['top_20']:
        for t in to['top_20'][:5]:
            print(f"    {t['frequency']:>8,}  \"{t['original']}\" -> \"{t['type_ahead']}\"")

    gt = metrics.get('ground_truth')
    if gt:
        print(f"\nGround Truth")
        print(f"  Accuracy:  {gt['accuracy']:.1%}  (freq-weighted: {gt['accuracy_freq_weighted']:.1%})")
        print(f"  Precision: {gt['precision']:.1%}  (freq-weighted: {gt['precision_freq_weighted']:.1%})")
        print(f"  Recall:    {gt['recall']:.1%}  (freq-weighted: {gt['recall_freq_weighted']:.1%})")
        print(f"  F1:        {gt['f1']:.1%}  (freq-weighted: {gt['f1_freq_weighted']:.1%})")
        c = gt['confusion']
        print(f"  Confusion: correct={c['correct']} | wrong={c['wrong_match']} | false_pos={c['false_positive']} | miss={c['miss']}")

    print()


def write_json_report(metrics, output_path):
    clean = {}
    for k, v in metrics.items():
        if isinstance(v, dict):
            clean[k] = {k2: v2 for k2, v2 in v.items() if not k2.startswith('_')}
        else:
            clean[k] = v
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(clean, f, indent=2, ensure_ascii=False)
    print(f"JSON report: {output_path}")


def write_flagged_tsv(metrics, output_path):
    flagged = []

    for p in metrics['partial_matches'].get('_rows', []):
        flagged.append({
            'guid': p['guid'], 'original': p['original'],
            'frequency': p['frequency'], 'authority_id': p['authority_id'],
            'type_ahead': p.get('type_ahead', ''), 'level': p.get('level', ''),
            'flag_reason': 'partial_match',
            'detail': p.get('partial_detail', ''),
        })

    for t in metrics['token_overlap'].get('_rows', []):
        flagged.append({
            'guid': t['guid'], 'original': t['original'],
            'frequency': t['frequency'], 'authority_id': t['authority_id'],
            'type_ahead': t.get('type_ahead', ''), 'level': t.get('level', ''),
            'flag_reason': 'zero_token_overlap',
            'detail': f"\"{t['original']}\" vs \"{t.get('type_ahead', '')}\"",
        })

    gt = metrics.get('ground_truth')
    if gt:
        for g in gt.get('_flagged', []):
            flagged.append({
                'guid': g['guid'], 'original': g['original'],
                'frequency': g['frequency'], 'authority_id': g['authority_id'],
                'type_ahead': g.get('type_ahead', ''), 'level': g.get('level', ''),
                'flag_reason': g['flag'],
                'detail': f"expected={g['gt_expected']} got={g['authority_id']}",
            })

    flagged.sort(key=lambda x: x['frequency'], reverse=True)

    if not flagged:
        print(f"Flagged rows: none")
        return

    cols = ['guid', 'original', 'frequency', 'authority_id', 'type_ahead',
            'level', 'flag_reason', 'detail']
    with open(output_path, 'w', encoding='utf-8', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=cols, delimiter='\t',
                                extrasaction='ignore')
        writer.writeheader()
        writer.writerows(flagged)
    print(f"Flagged rows: {output_path} ({len(flagged)} rows)")


def main():
    parser = argparse.ArgumentParser(description='Evaluate place normalization output.')
    parser.add_argument('output', nargs='?', help='Normalizer output TSV (direct mode)')
    parser.add_argument('--pipeline', nargs=2, metavar=('DIR', 'PREFIX'),
                        help='Pipeline mode: output directory and filename prefix')
    parser.add_argument('--input', required=True, help='Original raw input TSV')
    parser.add_argument('--pa', help='Path to PA.tsv for level lookups')
    parser.add_argument('--ground-truth', help='Ground truth TSV with guid + correct_authority_id')
    parser.add_argument('--name', help='Label for this evaluation run')
    parser.add_argument('--guid-col', help='Override guid column name')
    parser.add_argument('--id-col', help='Override authority_id column name')
    parser.add_argument('--original-col', help='Override original string column name')
    parser.add_argument('--freq-col', help='Override frequency column name')
    parser.add_argument('--level-col', help='Override level column name')
    parser.add_argument('--typeahead-col', help='Override type_ahead column name')
    args = parser.parse_args()

    if not args.output and not args.pipeline:
        parser.error('Provide either a positional output file or --pipeline DIR PREFIX')
    if args.output and args.pipeline:
        parser.error('Use either direct mode (positional) or --pipeline, not both')

    overrides = {
        'guid': args.guid_col, 'authority_id': args.id_col,
        'original': args.original_col, 'frequency': args.freq_col,
        'level': args.level_col, 'type_ahead': args.typeahead_col,
    }
    overrides = {k: v for k, v in overrides.items() if v}

    script_dir = os.path.dirname(os.path.abspath(__file__))
    results_dir = os.path.join(script_dir, 'results')
    os.makedirs(results_dir, exist_ok=True)

    if args.pipeline:
        pipeline_dir, prefix = args.pipeline
        unified = load_pipeline(pipeline_dir, prefix, overrides)
        run_name = args.name or prefix
    else:
        unified = load_direct(args.output, overrides)
        run_name = args.name or os.path.basename(args.output)

    safe_name = re.sub(r'[^\w\-]', '_', run_name)
    output_base = os.path.join(results_dir, safe_name)

    input_lookup = load_input_file(args.input, overrides)
    enrich_from_input(unified, input_lookup)

    if args.pa:
        pa_lookup = load_pa_lookup(args.pa)
        enrich_from_pa(unified, pa_lookup)

    metrics = {
        'meta': {
            'name': run_name,
            'timestamp': datetime.now().isoformat(),
            'input_file': args.input,
            'output_file': args.output or f'{args.pipeline[0]}/{args.pipeline[1]}',
            'row_count': len(unified),
            'total_frequency': sum(r['frequency'] for r in unified),
        },
        'match_rate': compute_match_rate(unified),
        'resolution_depth': compute_depth(unified),
        'exact_matches': compute_exact_matches(unified),
        'parent_resolved': compute_parent_resolved(unified),
        'partial_matches': compute_partials(unified),
        'token_overlap': compute_token_overlap(unified),
    }

    if args.ground_truth:
        metrics['ground_truth'] = compute_ground_truth(unified, args.ground_truth, overrides)

    day_dir = os.path.join(results_dir, datetime.now().strftime('%m-%d'))
    os.makedirs(day_dir, exist_ok=True)
    existing = [f for f in os.listdir(day_dir) if f.startswith(safe_name)]
    if existing:
        run_num = len(set(f.split('_eval')[0].split('_flagged')[0] for f in existing)) + 1
        base = f'{safe_name}_{run_num}'
    else:
        base = safe_name
    report_base = os.path.join(day_dir, base)
    print_summary(run_name, metrics)
    write_json_report(metrics, f'{report_base}_eval.json')
    write_flagged_tsv(metrics, f'{report_base}_flagged.tsv')


if __name__ == '__main__':
    main()
