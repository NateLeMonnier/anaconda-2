"""Local TSV-backed replacements for FileMaker API queries.

Loads the Master Normalization Table and Authority Place TSV exports into
memory, then provides drop-in replacements for every FM-dependent Phase 1
and Phase 2 function in rtl_matcher.py.

Toggle with:  USE_LOCAL_DATA = True  in rtl_matcher.py
Revert with:  USE_LOCAL_DATA = False
"""

import csv
import os
import sys
import time
from collections import defaultdict

# Column mappings: PA TSV -> FM field names used by the rest of rtl_matcher
_PA_FIELD_MAP = {
    'Term': 'Auth_Place_Name',
    'ID': 'UUID',
    'ParentID': 'Parent_UUID',
    'LevelName': 'Jurisdiction',
    'Level': 'Level',
    'Population': 'Population',
    'FullChainName': 'Type_Ahead_Value',
    'Historical': 'Historical',
    'Latitude': 'Latitude',
    'Longitude': 'Longitude',
}

# Paths — override with env vars if needed
MNT_TSV = os.environ.get('RTL_MNT_TSV', os.path.expanduser(
    "~/storied/resources/place-authority-mnt-tsv/Master_Normalization_File_6_16_2026v1.tsv"))
PA_TSV = os.environ.get('RTL_PA_TSV_LOCAL', os.environ.get('RTL_PA_TSV', os.path.expanduser(
    "~/storied/resources/place-authority-mnt-tsv/PA 6_16_2026v77.tsv")))


def _is_valid_uuid(s):
    return len(s) == 36 and s[8] == '-' and s[13] == '-'


class LocalData:
    """In-memory indexes over the MNT and PA TSV files."""

    def __init__(self):
        self.mnt_by_value = None       # lowercase _value -> set of _ID (UUIDs)
        self.pa_by_name = None         # lowercase Term -> list of record dicts
        self.pa_by_uuid = None         # UUID -> record dict
        self._loaded = False

    def load(self):
        if self._loaded:
            return
        start = time.time()
        print("Loading local TSV data...")

        self._load_mnt()
        self._load_pa()

        elapsed = time.time() - start
        print(f"  Local data loaded in {elapsed:.1f}s")
        self._loaded = True

    def _load_mnt(self):
        self.mnt_by_raw = defaultdict(set)
        self.mnt_by_value = defaultdict(set)
        count = 0
        junk = 0
        with open(MNT_TSV, encoding='utf-8-sig') as f:
            reader = csv.DictReader(f, delimiter='\t')
            for row in reader:
                raw = (row.get('_raw') or '').strip()
                value = (row.get('_value') or '').strip()
                uid = (row.get('_ID') or '').strip()
                if uid:
                    if _is_valid_uuid(uid):
                        if raw:
                            self.mnt_by_raw[raw.lower()].add(uid)
                        if value:
                            self.mnt_by_value[value.lower()].add(uid)
                        count += 1
                    else:
                        junk += 1
        print(f"  MNT: {count} mappings loaded, {junk} junk IDs skipped, "
              f"{len(self.mnt_by_raw)} unique raw terms, "
              f"{len(self.mnt_by_value)} unique value terms")

    def _load_pa(self):
        self.pa_by_name = defaultdict(list)
        self.pa_by_uuid = {}
        count = 0
        with open(PA_TSV, encoding='utf-8-sig') as f:
            reader = csv.DictReader(f, delimiter='\t')
            for row in reader:
                rec = {}
                for tsv_col, fm_field in _PA_FIELD_MAP.items():
                    rec[fm_field] = (row.get(tsv_col) or '').strip()
                uid = rec['UUID']
                name = rec['Auth_Place_Name']
                if uid:
                    self.pa_by_uuid[uid] = rec
                    if name:
                        self.pa_by_name[name.lower()].append(rec)
                    count += 1
        print(f"  PA:  {count} records loaded, {len(self.pa_by_name)} unique names")


# Singleton
_DATA = LocalData()


def ensure_loaded():
    _DATA.load()


# -----------------------------------------------------------------------
# Drop-in replacements for FM query functions
# -----------------------------------------------------------------------

def query_mnt_local(terms):
    """Replace query_mnt(client, terms). Returns name_cache.

    Searches _raw first (matching FM's Input_Original behavior),
    then merges any additional hits from _value.
    """
    ensure_loaded()
    name_cache = defaultdict(set)
    matched = 0
    for term in terms:
        key = term.lower()
        uuids = set()
        raw_hits = _DATA.mnt_by_raw.get(key)
        if raw_hits:
            uuids.update(raw_hits)
        val_hits = _DATA.mnt_by_value.get(key)
        if val_hits:
            uuids.update(val_hits)
        if uuids:
            name_cache[key] = uuids
            matched += 1
    print(f"  MNT (local): {len(terms)} terms, {matched} matched")
    return name_cache


def query_authority_by_name_local(terms, name_cache):
    """Replace query_authority_by_name(client, terms, name_cache)."""
    ensure_loaded()
    added = 0
    for term in terms:
        key = term.lower()
        records = _DATA.pa_by_name.get(key, [])
        for rec in records:
            uid = rec['UUID']
            if uid and uid not in name_cache.get(key, set()):
                name_cache[key].add(uid)
                added += 1
    print(f"  Authority by name (local): {len(terms)} terms, {added} new UUIDs\n")
    return added


def query_abbreviation_expansions_local(all_terms, name_cache, jurisdiction_abbreviations):
    """Replace query_abbreviation_expansions(client, all_terms, name_cache)."""
    ensure_loaded()
    expansions = {}
    for term in all_terms:
        normalized = term.lower().rstrip('.')
        expanded = jurisdiction_abbreviations.get(normalized)
        if expanded:
            expansions[term] = expanded

    if not expansions:
        print("  No abbreviation expansions found")
        return 0

    added = 0
    for orig_term, expanded_name in expansions.items():
        records = _DATA.pa_by_name.get(expanded_name.lower(), [])
        uuids = {rec['UUID'] for rec in records if rec['UUID']}
        if uuids:
            key = orig_term.lower()
            new_uuids = uuids - name_cache.get(key, set())
            if new_uuids:
                name_cache[key].update(new_uuids)
                added += len(new_uuids)
    return added


def _query_name_local(name):
    """Look up a name in both MNT and PA, return set of UUIDs."""
    key = name.lower()
    uuids = set(_DATA.mnt_by_raw.get(key, set()))
    uuids.update(_DATA.mnt_by_value.get(key, set()))
    for rec in _DATA.pa_by_name.get(key, []):
        if rec['UUID']:
            uuids.add(rec['UUID'])
    return uuids


def query_name_variants_local(all_terms, name_cache, generate_name_variants_fn):
    """Replace query_name_variants(client, all_terms, name_cache)."""
    ensure_loaded()
    variant_map = {}
    for term in all_terms:
        variants = generate_name_variants_fn(term)
        if variants:
            variant_map[term] = variants

    if not variant_map:
        print("  No name variants to try")
        return 0

    added = 0
    for orig_term, variants in variant_map.items():
        key = orig_term.lower()
        for variant in variants:
            uuids = _query_name_local(variant)
            new_uuids = uuids - name_cache.get(key, set())
            if new_uuids:
                name_cache[key].update(new_uuids)
                added += len(new_uuids)

    total_variants = sum(len(v) for v in variant_map.values())
    print(f"  Name variants (local): {total_variants} variants checked")
    return added


def query_fallback_transforms_local(unmatched_terms, name_cache, transform_term_fn):
    """Replace query_fallback_transforms(client, unmatched_terms, name_cache)."""
    ensure_loaded()
    transforms = {}
    for term in unmatched_terms:
        cleaned, jurisdiction = transform_term_fn(term)
        if cleaned:
            transforms[term] = (cleaned, jurisdiction)

    if not transforms:
        print("  No transformable terms")
        return 0

    added = 0
    for orig, (cleaned, jurisdiction) in transforms.items():
        key = orig.lower()
        if jurisdiction:
            records = _DATA.pa_by_name.get(cleaned.lower(), [])
            for rec in records:
                if rec['UUID'] and rec['Jurisdiction'].lower() == jurisdiction.lower():
                    if rec['UUID'] not in name_cache.get(key, set()):
                        name_cache[key].add(rec['UUID'])
                        added += 1
        else:
            uuids = _query_name_local(cleaned)
            new_uuids = uuids - name_cache.get(key, set())
            if new_uuids:
                name_cache[key].update(new_uuids)
                added += len(new_uuids)

    print(f"  Fallback transforms (local): {len(transforms)} terms, {added} UUIDs")
    return added


def query_spelling_corrections_local(unmatched_terms, name_cache, sym_spell, min_len):
    """Replace query_spelling_corrections(client, unmatched_terms, name_cache, sym_spell)."""
    ensure_loaded()
    from symspellpy import Verbosity
    from rtl_matcher import ascii_fold

    eligible = [t for t in unmatched_terms
                if len(t) >= min_len and not name_cache.get(t.lower())]

    if not eligible:
        print("  No eligible terms for spelling correction")
        return 0, []

    correction_map = {}
    for term in eligible:
        folded = ascii_fold(term)
        suggestions = sym_spell.lookup(folded, Verbosity.CLOSEST, max_edit_distance=1)
        if suggestions:
            corrected_terms = [s.term for s in suggestions if s.term != folded]
            if corrected_terms:
                correction_map[term] = corrected_terms

    if not correction_map:
        print("  No spelling corrections found")
        return 0, []

    added = 0
    corrections = []
    for original, candidates in correction_map.items():
        for candidate in candidates:
            records = _DATA.pa_by_name.get(candidate, [])
            uuids = {rec['UUID'] for rec in records if rec['UUID']}
            if uuids:
                name_cache[original.lower()].update(uuids)
                added += len(uuids)
                corrections.append({
                    'original_term': original,
                    'corrected_term': candidate,
                    'edit_distance': 1,
                    'authority_uuid': ';'.join(sorted(uuids)),
                })

    return added, corrections


def query_authority_batch_local(uuids):
    """Replace query_authority_batch(client, uuids). Returns auth_cache."""
    ensure_loaded()
    auth_cache = {}
    found = 0
    for uid in uuids:
        rec = _DATA.pa_by_uuid.get(uid)
        if rec:
            auth_cache[uid] = rec
            found += 1
    print(f"  Authority batch (local): {len(uuids)} UUIDs, {found} found\n")
    return auth_cache


def prefetch_parent_chains_local(auth_cache):
    """Replace prefetch_parent_chains(client, auth_cache)."""
    ensure_loaded()
    rounds = 0
    while True:
        missing = set()
        for rec in auth_cache.values():
            parent = (rec.get('Parent_UUID') or '').strip()
            if parent and parent not in auth_cache:
                missing.add(parent)
        if not missing:
            break
        fetched = 0
        for uid in missing:
            rec = _DATA.pa_by_uuid.get(uid)
            if rec:
                auth_cache[uid] = rec
                fetched += 1
        rounds += 1
        print(f"  Parent pre-fetch (local): {len(missing)} UUIDs, {fetched} found")
        if fetched == 0:
            break
    print()


def resolve_helper_term_local(term_string, auth_cache, walk_up_chain_fn):
    """Replace resolve_helper_term(term_string, client, auth_cache)."""
    ensure_loaded()
    import re
    from rtl_matcher import field_str

    if not term_string:
        return None

    terms = [t.strip() for t in re.split(r'[,;]', term_string) if t.strip()]
    if not terms:
        return None

    term_candidates = {}
    for term in terms:
        records = _DATA.pa_by_name.get(term.lower(), [])
        uuids = set()
        for rec in records:
            uid = rec['UUID']
            if uid:
                auth_cache[uid] = rec
                uuids.add(uid)
        if uuids:
            term_candidates[term.lower()] = uuids

    if not term_candidates:
        print("  Helper term: no authority records found.")
        return None

    if len(terms) > 1:
        right_to_left = list(reversed(terms))
        confirmed = term_candidates.get(right_to_left[0].lower(), set())
        if not confirmed:
            all_found = set()
            for uuids in term_candidates.values():
                all_found.update(uuids)
            confirmed = all_found
        else:
            prefetch_parent_chains_local(auth_cache)
            for i in range(1, len(right_to_left)):
                child_ids = term_candidates.get(right_to_left[i].lower(), set())
                if not child_ids:
                    continue
                verified = {
                    cid for cid in child_ids
                    if walk_up_chain_fn(cid, confirmed, auth_cache, None)
                }
                if verified:
                    confirmed = verified
        candidates = list(confirmed)
    else:
        candidates = list(term_candidates.get(terms[0].lower(), set()))

    if not candidates:
        print("  Helper term: chain walk produced no candidates.")
        return None

    if len(candidates) == 1:
        chosen_uuid = candidates[0]
    else:
        print(f"\n  Helper term '{term_string}' matched {len(candidates)} records:")
        for idx, uuid in enumerate(candidates):
            rec = auth_cache.get(uuid, {})
            name = field_str(rec, 'Auth_Place_Name')
            level = field_str(rec, 'Level')
            jurisdiction = field_str(rec, 'Jurisdiction')
            type_ahead = field_str(rec, 'Type_Ahead_Value')
            print(f"    [{idx + 1}] {name}  level={level}  jurisdiction={jurisdiction}  ({type_ahead})")
        print(f"    [q] Skip helper term")
        choice = input("  Pick a number: ").strip().lower()
        if choice == 'q' or not choice:
            return None
        try:
            idx = int(choice) - 1
            if idx < 0 or idx >= len(candidates):
                print("  Invalid selection, skipping helper term.")
                return None
            chosen_uuid = candidates[idx]
        except ValueError:
            print("  Invalid selection, skipping helper term.")
            return None

    chosen_rec = auth_cache.get(chosen_uuid, {})
    try:
        chosen_level = int(field_str(chosen_rec, 'Level'))
    except (ValueError, TypeError):
        chosen_level = 0

    ancestor_uuids = set()
    current = chosen_uuid
    for _ in range(20):
        rec = auth_cache.get(current)
        if not rec:
            rec = _DATA.pa_by_uuid.get(current)
            if rec:
                auth_cache[current] = rec
            else:
                break
        parent = (rec.get('Parent_UUID') or '').strip()
        if not parent:
            break
        ancestor_uuids.add(parent)
        current = parent

    return {
        'uuid': chosen_uuid,
        'level': chosen_level,
        'ancestor_uuids': ancestor_uuids,
    }
