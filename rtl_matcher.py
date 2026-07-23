#!/usr/bin/env python3
"""Right-to-left location matching with fallback transforms.

Takes a TSV of raw place strings from genealogical records and attempts to
resolve each one to an authority record in FileMaker's Authority_Place table.

The core idea: place strings are written broadest-to-narrowest by convention
("Syracuse, New York, United States of America"), so reading right-to-left
lets us anchor on the broadest geography first, then narrow down by verifying
that each successive term is a child of the previous match in the jurisdiction
hierarchy.

The pipeline runs in three phases:

  Phase 1 — Name Resolution
    Convert raw place-string terms into candidate authority UUIDs by querying
    two sources: the Master Normalization Table (MNT, which maps known input
    strings to authority IDs) and the Authority_Place table (direct name match).
    Terms that fail both lookups get a second pass with fallback transforms
    (stripping directional prefixes, expanding abbreviations like "St." to
    "Saint", separating jurisdiction suffixes like "County" for filtered search).
    Terms still unresolved after transforms are run through symspellpy spelling
    correction (edit distance 1, ASCII-folded, 5+ character terms only), and
    finally through FamilySearch city resolution as a last resort.

  Phase 2 — Authority Record Caching
    Fetch the full authority records for every UUID discovered in Phase 1, then
    walk up the Parent_UUID chain in bulk to pre-cache the entire jurisdiction
    hierarchy. This avoids per-entry API calls during matching.

  Phase 3 — Right-to-Left Matching
    For each input place string, start from the rightmost (broadest) term and
    look up its candidate UUIDs. Move left one term at a time, keeping only
    candidates whose Parent_UUID chain connects back to the current confirmed
    set. When multiple candidates survive, rank by jurisdiction level gap
    from the parent anchor (smaller gap = more direct child = better fit),
    with population as a secondary tiebreaker. Unresolvable ties are
    written to a separate side file for QA review.
"""

import argparse
import base64
import csv
import json
import logging
import os
import re
import socket
import ssl
import sys
import time
import unicodedata
import urllib.request
import urllib.error
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
import threading
from dataclasses import dataclass, field
from functools import partial
from symspellpy import SymSpell, Verbosity

log = logging.getLogger(__name__)

MIN_SPELLING_LEN = 5

# FileMaker Data API accepts multiple query objects per request with no
# documented cap, so we batch aggressively to minimize round trips.
BATCH = 1000

FS_BASE = "https://api-integ.familysearch.org/platform/places/search"
FS_TYPE_CITY = "186"

# Max distance (km) between a skipped term's actual county and the confirmed
# county for the proximity fallback to accept a chain-verification failure
# as a likely wrong-county data-entry issue rather than a genuine mismatch.
PROXIMITY_THRESHOLD_KM = 50

OUTPUT_FIELDS = [
    'original', 'guid', 'frequency', 'match_type', 'confidence', 'match_depth',
    'candidates', 'authority_name', 'type_ahead', 'jurisdiction',
    'level', 'authority_id', 'candidate_ids', 'candidate_names',
    'skipped_count', 'skipped_terms',
]

TIE_OUTPUT_FIELDS = [
    'original', 'guid', 'frequency', 'match_type', 'confidence', 'match_depth',
    'authority_id', 'authority_name', 'type_ahead', 'level', 'jurisdiction',
]


def field_str(field_data, key):
    """Safely extract a string field from a FileMaker record, returning '' if null."""
    val = field_data.get(key)
    if val is None:
        return ''
    return str(val).strip()


def tokenize(s):
    return set(re.sub(r'[^a-z0-9 ]', ' ', s.lower()).split()) - {''}


def ascii_fold(s):
    """Normalize a Unicode string to its ASCII equivalent (e.g. México -> mexico)."""
    return unicodedata.normalize('NFKD', s).encode('ascii', 'ignore').decode('ascii').lower()


def build_ascii_index(name_cache):
    """Build a secondary index keyed by ASCII-folded names.

    Only entries whose folded form differs from the original key are added,
    since identical entries are already reachable through name_cache directly.
    """
    ascii_cache = defaultdict(set)
    for key, uuids in name_cache.items():
        folded = ascii_fold(key)
        if folded != key:
            ascii_cache[folded].update(uuids)
    return ascii_cache


def _normalize_hyphens(s):
    """Replace hyphens with spaces for consistent lookup keying."""
    return s.replace('-', ' ')


def lookup_name(term, name_cache, ascii_cache):
    """Look up a term in name_cache and merge with ascii_cache matches."""
    key = term.lower()
    result = set(name_cache.get(key, set()))
    folded = ascii_fold(key)
    result.update(ascii_cache.get(folded, set()))
    dehyphenated = _normalize_hyphens(key)
    if dehyphenated != key:
        result.update(name_cache.get(dehyphenated, set()))
        folded_dh = ascii_fold(dehyphenated)
        result.update(ascii_cache.get(folded_dh, set()))
    return result


# ---------------------------------------------------------------------------
# FileMaker client
#
# Wraps the FileMaker Data API (v2) with session management and automatic
# re-authentication on token expiry. The find() method accepts a list of
# query objects, where each object is an OR condition and fields within an
# object are AND conditions — matching FileMaker's native find semantics.
# ---------------------------------------------------------------------------


class FileMakerClient:
    """Thin FileMaker Data API client: token auth, _find queries with
    re-auth on expiry, and a call counter for run summaries."""

    def __init__(self, env_path):
        self._load_env(env_path)
        self.host = os.environ['FILEMAKER_HOST']
        self.database = os.environ['FILEMAKER_DATABASE']
        self._ssl_ctx = ssl.create_default_context()
        self.token = None
        self.call_count = 0

    def _load_env(self, env_path):
        """Parse a .env file and inject its values into os.environ."""
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, _, val = line.partition('=')
                    os.environ[key.strip()] = val.strip().strip('"').strip("'")

    def auth(self):
        """Open a new FileMaker Data API session and store the bearer token."""
        url = f"{self.host}/fmi/data/v1/databases/{self.database}/sessions"
        req = urllib.request.Request(url, data=b'{}', method='POST')
        req.add_header('Content-Type', 'application/json')
        creds = base64.b64encode(
            f"{os.environ['FILEMAKER_USERNAME']}:{os.environ['FILEMAKER_PASSWORD']}".encode()
        ).decode()
        req.add_header('Authorization', f'Basic {creds}')
        resp = urllib.request.urlopen(req, context=self._ssl_ctx, timeout=30)
        data = json.loads(resp.read())
        self.token = data['response']['token']

    def find(self, layout, query, limit=2000, _retry=False):
        """Execute a _find request against the given layout.

        Returns a list of record dicts on success, or an empty list if
        no records match (FM error 401) or on HTTP errors. Retries once
        on connection-level failures (timeout, reset, SSL EOF) with a
        fresh auth token in case the session expired.
        """
        if not self.token:
            self.auth()
        url = f"{self.host}/fmi/data/v2/databases/{self.database}/layouts/{layout}/_find"
        payload = json.dumps({"query": query, "limit": str(limit)})
        req = urllib.request.Request(url, data=payload.encode(), method='POST')
        req.add_header('Content-Type', 'application/json')
        req.add_header('Authorization', f'Bearer {self.token}')
        self.call_count += 1
        try:
            resp = urllib.request.urlopen(req, context=self._ssl_ctx, timeout=30)
            data = json.loads(resp.read())
            if data['messages'][0]['code'] == '0':
                return data['response']['data']
            return []
        except (socket.timeout, ConnectionError, OSError) as e:
            if not _retry:
                log.warning("  Connection error (%s), re-authing and retrying...", e)
                self.auth()
                return self.find(layout, query, limit, _retry=True)
            log.warning("  Connection error on retry (%s), skipping batch", e)
            return []
        except urllib.error.HTTPError as e:
            body = e.read()
            try:
                data = json.loads(body)
                code = data['messages'][0]['code']
                if code == '401':
                    return []
            except (json.JSONDecodeError, KeyError, IndexError):
                pass
            if e.code in (401, 403) and not _retry:
                self.auth()
                return self.find(layout, query, limit, _retry=True)
            log.warning("  FM error %d: %s", e.code, body[:200])
            return []


# ---------------------------------------------------------------------------
# Local TSV data — in-memory indexes over MNT and PA exports
# ---------------------------------------------------------------------------

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


def _is_valid_local_uuid(s):
    """Cheap shape check used on the TSV load hot path (see is_valid_uuid
    for the strict regex used elsewhere)."""
    return len(s) == 36 and s[8] == '-' and s[13] == '-'


def _load_env_file(path):
    """Parse KEY=VALUE lines from an env file into os.environ."""
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, _, val = line.partition('=')
                os.environ[key.strip()] = val.strip().strip('"').strip("'")


def canonicalize_place(s):
    """Lowercase a place string and normalize separators: split on [,;],
    strip each segment, rejoin with ', '. Both the full-string index keys
    and lookups run through this so spacing variants collapse."""
    parts = [p.strip() for p in re.split(r'[,;]', s.lower()) if p.strip()]
    return ', '.join(parts)


class LocalData:
    """In-memory indexes over the MNT and PA TSV files."""

    def __init__(self):
        self.mnt_by_raw = None
        self.mnt_by_value = None
        self.pa_by_name = None
        self.pa_by_uuid = None
        self.fs_by_raw = None       # canonical full string -> single UUID
        self.dict_freq = None       # (term_lower, uuid_upper) -> frequency
        self.illegible = set()      # curated junk terms (lowercase)
        self._loaded = False

    def load(self, mnt_path, pa_path, dict_source=None, env_path=None):
        """Read the TSVs into dictionaries keyed for the lookups we do.
        dict_source: None, 'live' (Supabase), or a directory of TSV exports
        (place_term_dictionary.tsv, place_term_illegible.tsv). Dict terms
        union into mnt_by_raw; they never replace MNT data."""
        if self._loaded:
            return
        start = time.time()
        log.info("Loading local TSV data...")
        self._load_mnt(mnt_path)
        self._load_pa(pa_path)
        if dict_source == 'live':
            self._load_dict_live(env_path)
        elif dict_source:
            self._load_dict_tsv(dict_source)
        log.info("  Local data loaded in %.1fs", time.time() - start)
        self._loaded = True

    def _ingest_dict_row(self, term, uid, freq):
        key = term.lower()
        self.mnt_by_raw[key].add(uid)
        dh = key.replace('-', ' ')
        if dh != key:
            self.mnt_by_raw[dh].add(uid)
        self.dict_freq[(key, uid)] = freq

    def _load_dict_live(self, env_path):
        import psycopg2
        if env_path:
            _load_env_file(env_path)
        password = os.environ.get('SUPABASE_PASSWORD')
        if not password:
            raise ValueError("SUPABASE_PASSWORD not set. Use --env or export it.")
        conn = psycopg2.connect(
            host="aws-1-us-west-1.pooler.supabase.com", port=5432,
            dbname="postgres", user="parser_readonly.ncahtzbmazzqrorjkjwm",
            password=password)
        cur = conn.cursor()
        log.info("  Pulling place_term_dictionary...")
        cur.execute("SELECT term, authority_uuid, frequency "
                    "FROM place_term_dictionary")
        n = 0
        for term, uuid, freq in cur:
            self._ingest_dict_row(term.strip(), str(uuid).upper(), freq or 0)
            n += 1
        log.info("  Dictionary: %d mappings unioned (%d unique terms total)",
                 n, len(self.mnt_by_raw))
        log.info("  Pulling authority_place...")
        cur.execute("SELECT uuid, parent_uuid, canonical_name, level, "
                    "jurisdiction, full_chain_name FROM authority_place")
        added = 0
        for uuid, parent_uuid, name, level, jurisdiction, chain in cur:
            uid = str(uuid).upper()
            if uid in self.pa_by_uuid:
                continue          # PA TSV is primary (has pop/lat/lon)
            rec = {
                'Auth_Place_Name': name or '',
                'UUID': uid,
                'Parent_UUID': str(parent_uuid).upper() if parent_uuid else '',
                'Jurisdiction': jurisdiction or '',
                'Level': str(level) if level is not None else '',
                'Population': '', 'Type_Ahead_Value': chain or '',
                'Historical': '', 'Latitude': '', 'Longitude': '',
            }
            self.pa_by_uuid[uid] = rec
            if name:
                nkey = name.lower()
                self.pa_by_name[nkey].append(rec)
                dh = nkey.replace('-', ' ')
                if dh != nkey:
                    self.pa_by_name[dh].append(rec)
            added += 1
        log.info("  Authority: %d Supabase-only records added", added)
        log.info("  Pulling place_term_illegible...")
        cur.execute("SELECT term FROM place_term_illegible")
        self.illegible = {t[0].strip().lower() for t in cur if t[0]}
        log.info("  Illegible stop-list: %d terms", len(self.illegible))
        cur.close()
        conn.close()

    def _load_dict_tsv(self, dirpath):
        path = os.path.join(dirpath, 'place_term_dictionary.tsv')
        n = 0
        with open(path, encoding='utf-8-sig') as f:
            reader = csv.DictReader(f, delimiter='\t')
            for row in reader:
                term = (row.get('term') or '').strip()
                uid = (row.get('authority_uuid') or '').strip().upper()
                if term and uid and _is_valid_local_uuid(uid):
                    self._ingest_dict_row(term, uid, int(row.get('frequency') or 0))
                    n += 1
        log.info("  Dictionary TSV: %d mappings unioned", n)
        ill = os.path.join(dirpath, 'place_term_illegible.tsv')
        if os.path.exists(ill):
            with open(ill, encoding='utf-8-sig') as f:
                reader = csv.DictReader(f, delimiter='\t')
                self.illegible = {(r.get('term') or '').strip().lower()
                                  for r in reader if (r.get('term') or '').strip()}
            log.info("  Illegible stop-list: %d terms", len(self.illegible))

    def _load_mnt(self, path):
        self.mnt_by_raw = defaultdict(set)
        self.mnt_by_value = defaultdict(set)
        self.dict_freq = {}
        fs_tmp = defaultdict(set)
        count = 0
        junk = 0
        with open(path, encoding='utf-8-sig') as f:
            reader = csv.DictReader(f, delimiter='\t')
            for row in reader:
                raw = (row.get('_raw') or row.get('InputString') or '').strip()
                value = (row.get('_value') or row.get('MatchAuthName') or '').strip()
                uid = (row.get('_ID') or row.get('MatchAuthID') or '').strip()
                if uid:
                    if _is_valid_local_uuid(uid):
                        if raw:
                            raw_key = raw.lower()
                            self.mnt_by_raw[raw_key].add(uid)
                            dh = raw_key.replace('-', ' ')
                            if dh != raw_key:
                                self.mnt_by_raw[dh].add(uid)
                            if ',' in raw or ';' in raw:
                                fs_tmp[canonicalize_place(raw)].add(uid)
                        if value:
                            val_key = value.lower()
                            self.mnt_by_value[val_key].add(uid)
                            dh = val_key.replace('-', ' ')
                            if dh != val_key:
                                self.mnt_by_value[dh].add(uid)
                        count += 1
                    else:
                        junk += 1
        self.fs_by_raw = {k: next(iter(v)) for k, v in fs_tmp.items()
                          if len(v) == 1}
        log.info("  MNT: %d mappings loaded, %d junk IDs skipped, "
                 "%d unique raw terms, %d unique value terms",
                 count, junk, len(self.mnt_by_raw), len(self.mnt_by_value))
        log.info("  MNT full-string index: %d entries (%d ambiguous dropped)",
                 len(self.fs_by_raw), sum(1 for v in fs_tmp.values() if len(v) > 1))

    def _load_pa(self, path):
        self.pa_by_name = defaultdict(list)
        self.pa_by_uuid = {}
        count = 0
        with open(path, encoding='utf-8-sig') as f:
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
                        key = name.lower()
                        self.pa_by_name[key].append(rec)
                        dehyphenated = key.replace('-', ' ')
                        if dehyphenated != key:
                            self.pa_by_name[dehyphenated].append(rec)
                    count += 1
        log.info("  PA:  %d records loaded, %d unique names", count, len(self.pa_by_name))


_LOCAL = LocalData()


def _query_name_local(name):
    """Return the union of MNT and PA UUIDs for a name (case-insensitive)."""
    key = name.lower()
    uuids = set(_LOCAL.mnt_by_raw.get(key, set()))
    uuids.update(_LOCAL.mnt_by_value.get(key, set()))
    for rec in _LOCAL.pa_by_name.get(key, []):
        if rec['UUID']:
            uuids.add(rec['UUID'])
    return uuids


# ---------------------------------------------------------------------------
# Local query functions — drop-in replacements for FM query functions
# ---------------------------------------------------------------------------

def query_mnt_local(terms):
    """Build a name_cache of term -> UUIDs from the in-memory MNT indexes."""
    name_cache = defaultdict(set)
    matched = 0
    for term in terms:
        key = term.lower()
        uuids = set()
        raw_hits = _LOCAL.mnt_by_raw.get(key)
        if raw_hits:
            uuids.update(raw_hits)
        val_hits = _LOCAL.mnt_by_value.get(key)
        if val_hits:
            uuids.update(val_hits)
        if uuids:
            name_cache[key] = uuids
            matched += 1
    log.info("  MNT (local): %d terms, %d matched", len(terms), matched)
    return name_cache


def query_authority_by_name_local(terms, name_cache):
    """Add UUIDs of PA records whose canonical name matches each term."""
    added = 0
    for term in terms:
        key = term.lower()
        records = _LOCAL.pa_by_name.get(key, [])
        for rec in records:
            uid = rec['UUID']
            if uid and uid not in name_cache.get(key, set()):
                name_cache[key].add(uid)
                added += 1
    log.info("  Authority by name (local): %d terms, %d new UUIDs", len(terms), added)
    return added


def query_abbreviation_expansions_local(all_terms, name_cache, jurisdiction_abbreviations):
    """Expand jurisdiction abbreviations (e.g. 'tex' -> 'Texas') and add the
    expanded names' UUIDs under the original term's key."""
    expansions = {}
    for term in all_terms:
        normalized = term.lower().rstrip('.')
        expanded = jurisdiction_abbreviations.get(normalized)
        if expanded:
            expansions[term] = [expanded]
        else:
            multi = JURISDICTION_ABBREVIATION_MULTI.get(normalized)
            if multi:
                expansions[term] = multi

    if not expansions:
        log.info("  No abbreviation expansions found")
        return 0

    added = 0
    for orig_term, expanded_names in expansions.items():
        key = orig_term.lower()
        for expanded_name in expanded_names:
            records = _LOCAL.pa_by_name.get(expanded_name.lower(), [])
            uuids = {rec['UUID'] for rec in records if rec['UUID']}
            if uuids:
                new_uuids = uuids - name_cache.get(key, set())
                if new_uuids:
                    name_cache[key].update(new_uuids)
                    added += len(new_uuids)
    return added


def query_name_variants_local(all_terms, name_cache, generate_name_variants_fn):
    """Look up generated spelling/punctuation variants of each term and add
    any hits under the original term's key."""
    variant_map = {}
    for term in all_terms:
        variants = generate_name_variants_fn(term)
        if variants:
            variant_map[term] = variants

    if not variant_map:
        log.info("  No name variants to try")
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
    log.info("  Name variants (local): %d variants checked", total_variants)
    return added


def query_fallback_transforms_local(unmatched_terms, name_cache, transform_term_fn):
    """Apply fallback transforms (prefix stripping, suffix separation) to
    unmatched terms and look up the cleaned forms, honoring any jurisdiction
    constraint the transform detected."""
    transforms = {}
    for term in unmatched_terms:
        cleaned, jurisdiction = transform_term_fn(term)
        if cleaned:
            transforms[term] = (cleaned, jurisdiction)

    if not transforms:
        log.info("  No transformable terms")
        return 0

    added = 0
    for orig, (cleaned, jurisdiction) in transforms.items():
        key = orig.lower()
        if jurisdiction:
            records = _LOCAL.pa_by_name.get(cleaned.lower(), [])
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

    log.info("  Fallback transforms (local): %d terms, %d UUIDs", len(transforms), added)
    return added


def query_preposition_extractions_local(unmatched_terms, name_cache):
    """For terms still unmatched after prefix-anchored transforms, scan for
    spatial prepositions anywhere in the string and try matching the substring
    after them.  Results stored under the ORIGINAL term key in name_cache."""
    added = 0
    tried = 0
    for term in unmatched_terms:
        key = term.lower()
        candidates = extract_after_preposition(term)
        if not candidates:
            continue
        tried += 1
        for candidate in candidates:
            cleaned, jurisdiction = transform_term(candidate)
            names_to_try = [candidate]
            if cleaned:
                names_to_try.append(cleaned)
            for name in names_to_try:
                if jurisdiction:
                    records = _LOCAL.pa_by_name.get(name.lower(), [])
                    for rec in records:
                        if rec['UUID'] and rec['Jurisdiction'].lower() == jurisdiction.lower():
                            if rec['UUID'] not in name_cache.get(key, set()):
                                name_cache[key].add(rec['UUID'])
                                added += 1
                else:
                    uuids = _query_name_local(name)
                    new_uuids = uuids - name_cache.get(key, set())
                    if new_uuids:
                        name_cache[key].update(new_uuids)
                        added += len(new_uuids)
            if name_cache.get(key):
                break
    log.info("  Preposition extractions (local): %d terms tried, %d UUIDs added", tried, added)
    return added


def query_spelling_corrections_local(terms, name_cache, sym_spell,
                                     transform_map=None):
    """Find edit-distance-1 corrections via SymSpell and add their PA UUIDs.
    Returns (added_count, correction_log_rows)."""
    candidates_by_key = {}
    for term in terms:
        if len(term) < MIN_SPELLING_LEN:
            continue
        key = term.lower()
        folded = ascii_fold(term)
        suggestions = sym_spell.lookup(folded, Verbosity.ALL, max_edit_distance=1)
        if suggestions:
            corrected = [s.term for s in suggestions if s.term != folded]
            if corrected:
                candidates_by_key[key] = corrected

    if transform_map:
        for orig_term, cleaned in transform_map.items():
            if len(cleaned) < MIN_SPELLING_LEN:
                continue
            key = orig_term.lower()
            folded = ascii_fold(cleaned)
            suggestions = sym_spell.lookup(folded, Verbosity.ALL, max_edit_distance=1)
            if suggestions:
                corrected = [s.term for s in suggestions if s.term != folded]
                if corrected:
                    existing = candidates_by_key.get(key, [])
                    candidates_by_key[key] = existing + corrected

    if not candidates_by_key:
        log.info("  No spelling corrections found")
        return 0, []

    added = 0
    corrections = []
    for key, candidates in candidates_by_key.items():
        for candidate in candidates:
            records = _LOCAL.pa_by_name.get(candidate, [])
            uuids = {rec['UUID'] for rec in records if rec['UUID']}
            new_uuids = uuids - name_cache.get(key, set())
            if new_uuids:
                name_cache[key].update(new_uuids)
                added += len(new_uuids)
                corrections.append({
                    'original_term': key,
                    'corrected_term': candidate,
                    'edit_distance': 1,
                    'authority_uuid': ';'.join(sorted(new_uuids)),
                })

    return added, corrections


def query_authority_batch_local(uuids):
    """Fetch full authority records for a set of UUIDs from the PA index."""
    auth_cache = {}
    found = 0
    for uid in uuids:
        rec = _LOCAL.pa_by_uuid.get(uid)
        if rec:
            auth_cache[uid] = rec
            found += 1
    log.info("  Authority batch (local): %d UUIDs, %d found", len(uuids), found)
    return auth_cache


def prefetch_parent_chains_local(auth_cache):
    """Walk up Parent_UUID references layer by layer until every ancestor
    is cached (local equivalent of prefetch_parent_chains)."""
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
            rec = _LOCAL.pa_by_uuid.get(uid)
            if rec:
                auth_cache[uid] = rec
                fetched += 1
        log.info("  Parent pre-fetch (local): %d UUIDs, %d found", len(missing), fetched)
        if fetched == 0:
            break


def resolve_helper_term_local(term_string, auth_cache):
    """Resolve a helper-term string (e.g. 'Utah, USA') to a single authority
    UUID used as a geographic boost during ranking. Returns None if no
    unambiguous record is found."""
    if not term_string:
        return None

    terms = [t.strip() for t in re.split(r'[,;]', term_string) if t.strip()]
    if not terms:
        return None

    term_candidates = {}
    for term in terms:
        records = _LOCAL.pa_by_name.get(term.lower(), [])
        uuids = set()
        for rec in records:
            uid = rec['UUID']
            if uid:
                auth_cache[uid] = rec
                uuids.add(uid)
        if uuids:
            term_candidates[term.lower()] = uuids

    if not term_candidates:
        log.info("  Helper term: no authority records found.")
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
                    if walk_up_chain(cid, confirmed, auth_cache, None)
                }
                if verified:
                    confirmed = verified
        candidates = list(confirmed)
    else:
        candidates = list(term_candidates.get(terms[0].lower(), set()))

    if not candidates:
        log.info("  Helper term: chain walk produced no candidates.")
        return None

    if len(candidates) == 1:
        chosen_uuid = candidates[0]
    else:
        def _pop(uid):
            try:
                return int(auth_cache.get(uid, {}).get('Population') or 0)
            except (ValueError, TypeError):
                return 0
        chosen_uuid = max(candidates, key=lambda uid: (_pop(uid), uid))
        rec = auth_cache.get(chosen_uuid, {})
        log.info("  Helper term '%s' had %d candidates, auto-picked: %s (%s)",
                 term_string, len(candidates),
                 field_str(rec, 'Auth_Place_Name'),
                 field_str(rec, 'Type_Ahead_Value'))

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
            rec = _LOCAL.pa_by_uuid.get(current)
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


# ---------------------------------------------------------------------------
# UUID validation
# ---------------------------------------------------------------------------

UUID_RE = re.compile(
    r'^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-'
    r'[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$')


def is_valid_uuid(value):
    """Strict regex validation of the canonical 8-4-4-4-12 UUID form."""
    return bool(UUID_RE.match(value))


# ---------------------------------------------------------------------------
# Fallback transforms
#
# When a raw term like "Washington County" or "near St. Louis" fails to match
# directly, these transforms produce alternate lookup strings. The transforms
# are composed in order: strip directional prefixes ("north of"), strip
# trailing descriptors ("area", "district"), separate jurisdiction suffixes
# ("County" -> search for "Washington" with Jurisdiction="County"), and expand
# abbreviations ("St." -> "Saint").
#
# The original term is preserved as the cache key so results map back to the
# input string that produced them.
# ---------------------------------------------------------------------------

JURISDICTION_SUFFIXES = [
    (re.compile(r'\s+County$', re.I), 'County'),
    (re.compile(r'\s+Township$', re.I), 'Township'),
    (re.compile(r'\s+Twp\.?$', re.I), 'Township'),
    (re.compile(r'\s+Parish$', re.I), 'Parish'),
    (re.compile(r'\s+Borough$', re.I), 'Borough'),
    (re.compile(r'\s+Co\.?$', re.I), 'County'),
]

PREFIX_PATTERNS = [
    re.compile(r'^(?:north|south|east|west|northeast|northwest|southeast|southwest)\s+of\s+', re.I),
    re.compile(r'^near\s+', re.I),
    re.compile(r'^(?:rural|suburban)\s+', re.I),
]

JURISDICTION_PREFIXES = [
    (re.compile(r'^county\s+of\s+', re.I), 'County'),
    (re.compile(r'^parish\s+of\s+', re.I), 'Parish'),
    (re.compile(r'^borough\s+of\s+', re.I), 'Borough'),
    (re.compile(r'^township\s+of\s+', re.I), 'Township'),
    (re.compile(r'^town\s+of\s+', re.I), 'Town'),
    (re.compile(r'^city\s+of\s+', re.I), 'City'),
    (re.compile(r'^district\s+of\s+', re.I), 'District'),
    (re.compile(r'^village\s+of\s+', re.I), 'Village'),
    (re.compile(r'^state\s+of\s+', re.I), 'State'),
    (re.compile(r'^province\s+of\s+', re.I), 'Province'),
]

# Prefixes that should be stripped but NOT used as jurisdiction filters,
# because the authority may store the place under a different jurisdiction
# (e.g. "Territory of Alaska" -> Alaska is stored as State, not Territory).
NON_FILTERING_PREFIXES = [
    re.compile(r'^territory\s+of\s+', re.I),
]

TRAILING_DESCRIPTORS = [
    re.compile(r'\s+(?:area|district|community|region|vicinity|neighborhood)$', re.I),
    re.compile(r'\s+R\.?\s?D\.?\s*\d*$', re.I),
    re.compile(r'\s+Route\s+\d+$', re.I),
    re.compile(r'\s+R\.?\s?R\.?\s*\d*$', re.I),
]

NOISE_TERM_RE = re.compile(
    r'^(?:Route|Rt\.?|R\.?\s?D\.?|R\.?\s?R\.?)\s*\d*$', re.I
)

ABBREVIATION_EXPANSIONS = [
    (re.compile(r'^St\.\s*', re.I), 'Saint '),
    (re.compile(r'^Ft\.\s*', re.I), 'Fort '),
    (re.compile(r'^Mt\.\s*', re.I), 'Mount '),
]

# Bidirectional prefix mappings: (long_form, short_form). Used to generate
# alternate lookups when the authority stores the opposite form from the input.
PREFIX_SWAPS = [
    ('Saint', 'St'),
    ('Fort', 'Ft'),
    ('Mount', 'Mt'),
]

# Prefixes that appear both joined (DeKalb) and spaced (De Kalb) in authority
# data. Only common genealogical/geographic prefixes to avoid false positives.
SPACING_PREFIXES = ['De', 'La', 'Le', 'Du', 'Van', 'Del', 'Los', 'Las', 'San']
_SPACING_SPLIT_CAMEL_RE = re.compile(
    r'^(' + '|'.join(SPACING_PREFIXES) + r')([A-Z]\w+)$'
)
_SPACING_SPLIT_FLAT_RE = re.compile(
    r'^(' + '|'.join(SPACING_PREFIXES) + r')(\w{3,})$', re.I
)
_SPACING_JOIN_RE = re.compile(
    r'^(' + '|'.join(SPACING_PREFIXES) + r')\s+(\w+)$', re.I
)

# Jurisdiction abbreviation table: maps abbreviated forms to their canonical
# authority names. Looked up case-insensitively with trailing periods stripped.
# These are ADDITIVE — matching an abbreviation injects the expanded form's
# authority UUIDs into name_cache alongside whatever the raw term already found.
JURISDICTION_ABBREVIATIONS = {
    # US states — USPS 2-letter codes
    'al': 'Alabama', 'ak': 'Alaska', 'az': 'Arizona', 'ar': 'Arkansas',
    'ca': 'California', 'co': 'Colorado', 'ct': 'Connecticut', 'de': 'Delaware',
    'fl': 'Florida', 'ga': 'Georgia', 'hi': 'Hawaii', 'id': 'Idaho',
    'il': 'Illinois', 'in': 'Indiana', 'ia': 'Iowa', 'ks': 'Kansas',
    'ky': 'Kentucky', 'la': 'Louisiana', 'me': 'Maine', 'md': 'Maryland',
    'ma': 'Massachusetts', 'mi': 'Michigan', 'mn': 'Minnesota', 'ms': 'Mississippi',
    'mo': 'Missouri', 'mt': 'Montana', 'ne': 'Nebraska', 'nv': 'Nevada',
    'nh': 'New Hampshire', 'nj': 'New Jersey', 'nm': 'New Mexico', 'ny': 'New York',
    'nc': 'North Carolina', 'nd': 'North Dakota', 'oh': 'Ohio', 'ok': 'Oklahoma',
    'or': 'Oregon', 'pa': 'Pennsylvania', 'ri': 'Rhode Island', 'sc': 'South Carolina',
    'sd': 'South Dakota', 'tn': 'Tennessee', 'tx': 'Texas', 'ut': 'Utah',
    'vt': 'Vermont', 'va': 'Virginia', 'wa': 'Washington', 'wv': 'West Virginia',
    'wi': 'Wisconsin', 'wy': 'Wyoming', 'dc': 'Washington D.C.',
    # US territories
    'pr': 'Puerto Rico', 'gu': 'Guam', 'vi': 'Virgin Islands',
    'as': 'American Samoa',
    # US states — common historical/informal abbreviations
    'ala': 'Alabama',
    'cal': 'California', 'calif': 'California',
    'colo': 'Colorado', 'conn': 'Connecticut',
    'del': 'Delaware', 'fla': 'Florida',
    'ill': 'Illinois', 'ind': 'Indiana',
    'kan': 'Kansas', 'kas': 'Kansas', 'kans': 'Kansas',
    'ken': 'Kentucky',
    'mass': 'Massachusetts',
    'minn': 'Minnesota', 'miss': 'Mississippi',
    'neb': 'Nebraska', 'nebr': 'Nebraska',
    'nev': 'Nevada', 'okla': 'Oklahoma',
    'ore': 'Oregon', 'oreg': 'Oregon',
    'penn': 'Pennsylvania', 'penna': 'Pennsylvania',
    'tenn': 'Tennessee', 'tex': 'Texas',
    'vir': 'Virginia', 'wash': 'Washington',
    'wis': 'Wisconsin', 'wisc': 'Wisconsin',
    'wyo': 'Wyoming',
    # Canadian provinces
    'ab': 'Alberta', 'mb': 'Manitoba',
    'nb': 'New Brunswick',
    'ns': 'Nova Scotia', 'on': 'Ontario', 'pe': 'Prince Edward Island',
    'qc': 'Quebec', 'sk': 'Saskatchewan',
    'nt': 'Northwest Territories', 'nu': 'Nunavut', 'yt': 'Yukon',
    # Australian states
    'nsw': 'New South Wales', 'qld': 'Queensland', 'vic': 'Victoria',
    'tas': 'Tasmania', 'act': 'Australian Capital Territory',
    # Mexican states
    'ags': 'Aguascalientes', 'bcs': 'Baja California Sur',
    'camp': 'Campeche', 'chis': 'Chiapas', 'chih': 'Chihuahua',
    'coah': 'Coahuila de Zaragoza', 'col': 'Colima',
    'dgo': 'Durango', 'gto': 'Guanajuato', 'gro': 'Guerrero',
    'hgo': 'Hidalgo', 'jal': 'Jalisco', 'mex': 'Mexico',
    'mor': 'Morelos', 'nay': 'Nayarit',
    'oax': 'Oaxaca', 'pue': 'Puebla',
    'qro': 'Queretaro de Arteaga', 'qroo': 'Quintana Roo',
    'slp': 'San Luis Potosi', 'sin': 'Sinaloa', 'son': 'Sonora',
    'tab': 'Tabasco', 'tamps': 'Tamaulipas', 'tlax': 'Tlaxcala',
    'ver': 'Veracruz-Llave', 'yuc': 'Yucatan', 'zac': 'Zacatecas',
}

# Keys that map to multiple jurisdictions across countries. Both expansions
# are queried and their UUIDs merged — Phase 3 chain walk disambiguates.
JURISDICTION_ABBREVIATION_MULTI = {
    'bc': ['British Columbia', 'Baja California'],
    'mich': ['Michigan', 'Michoacan de Ocampo'],
    'nl': ['Newfoundland and Labrador', 'Nuevo Leon'],
}


def detect_jurisdiction_hint(term):
    """Check if a term contains a jurisdiction suffix (County, Township, etc.).
    Returns the jurisdiction type string if found, None otherwise.
    Does NOT modify the term — detection only."""
    for pattern, jurisdiction_type in JURISDICTION_SUFFIXES:
        if pattern.search(term):
            return jurisdiction_type
    return None


def transform_term(term):
    """Apply all fallback transforms in sequence and return the cleaned term
    plus any jurisdiction filter extracted from it. Returns (None, None) if
    the transforms produced no change from the original."""
    cleaned = term
    jurisdiction = None

    for pattern in PREFIX_PATTERNS:
        cleaned = pattern.sub('', cleaned).strip()

    # Non-filtering prefixes: strip but don't set jurisdiction hint
    for pattern in NON_FILTERING_PREFIXES:
        stripped = pattern.sub('', cleaned)
        if stripped != cleaned:
            cleaned = stripped.strip()
            break

    # Jurisdiction prefixes: "County of X" -> "X" with jurisdiction hint
    for pattern, jurisdiction_type in JURISDICTION_PREFIXES:
        stripped = pattern.sub('', cleaned)
        if stripped != cleaned:
            cleaned = stripped.strip()
            jurisdiction = jurisdiction_type
            break

    for pattern in TRAILING_DESCRIPTORS:
        cleaned = pattern.sub('', cleaned).strip()

    # Only one jurisdiction suffix should match; "Washington County Township"
    # is not a real pattern, so we break on first hit.
    if not jurisdiction:
        for pattern, jurisdiction_type in JURISDICTION_SUFFIXES:
            stripped = pattern.sub('', cleaned)
            if stripped != cleaned:
                cleaned = stripped.strip()
                jurisdiction = jurisdiction_type
                break

    for pattern, replacement in ABBREVIATION_EXPANSIONS:
        expanded = pattern.sub(replacement, cleaned)
        if expanded != cleaned:
            cleaned = expanded.strip()
            break

    # Inline parenthesis merge: "Trevo(se)" -> "Trevose" when no space before "("
    if '(' in cleaned:
        merged = re.sub(r'(?<!\s)\(([^)]*)\)', r'\1', cleaned)
        if merged != cleaned:
            cleaned = merged

    if not cleaned or cleaned.lower() == term.lower():
        return None, None

    return cleaned, jurisdiction


SPATIAL_PREPOSITIONS = frozenset({
    'near', 'from', 'outside', 'at', 'in', 'to',
})

DIRECTIONAL_PREPOSITIONS = frozenset({
    'north', 'south', 'east', 'west',
    'northeast', 'northwest', 'southeast', 'southwest',
})


def extract_after_preposition(term):
    """Extract candidate place names found after spatial prepositions anywhere
    in the term.  Returns a list of candidates, shortest (most specific) first.
    Only called when transform_term already failed — handles mid-string noise
    like 'home of her daughter near Luana' or 'one mile north of Buffalo'."""
    candidates = []
    words = term.split()
    for i, word in enumerate(words):
        w = word.lower().rstrip('.,;:')
        if w in SPATIAL_PREPOSITIONS:
            rest = ' '.join(words[i + 1:]).strip()
            if rest:
                candidates.append(rest)
        elif w in DIRECTIONAL_PREPOSITIONS:
            if i + 1 < len(words) and words[i + 1].lower().rstrip('.,;:') == 'of':
                rest = ' '.join(words[i + 2:]).strip()
                if rest:
                    candidates.append(rest)
        elif w == 'of' and i > 0:
            prev = words[i - 1].lower().rstrip('.,;:')
            if prev not in DIRECTIONAL_PREPOSITIONS:
                rest = ' '.join(words[i + 1:]).strip()
                if rest:
                    candidates.append(rest)

    candidates.sort(key=len)
    seen = set()
    return [c for c in candidates if not (c.lower() in seen or seen.add(c.lower()))]


# ---------------------------------------------------------------------------
# Phase 1: Build name_cache
#
# name_cache maps lowercased term strings to sets of authority UUIDs. It
# answers the question "given this place name, what authority records could
# it refer to?" Phase 1 populates this cache through three sub-phases:
#   1a) MNT lookup — uses previously-curated input-to-authority mappings
#   1b) Authority name lookup — direct match on Auth_Place_Name
#   1c) Fallback transforms — for terms that failed 1a and 1b, try
#       cleaned/expanded variants
# ---------------------------------------------------------------------------

def query_mnt(client, terms):
    """Query the Master Normalization Table for exact matches on Input_Original.

    The MNT contains some non-UUID values in Match_Authority_ID (legacy data
    artifacts), so we validate each ID before adding it to the cache.
    """
    name_cache = defaultdict(set)
    junk_count = 0

    def extract(field_data):
        nonlocal junk_count
        original = field_str(field_data, 'Input_Original')
        authority_id = field_str(field_data, 'Match_Authority_ID')
        if original and authority_id:
            if is_valid_uuid(authority_id):
                name_cache[original.lower()].add(authority_id)
            else:
                junk_count += 1

    term_list = list(terms)
    total = len(term_list)
    for i in range(0, total, BATCH):
        batch = term_list[i:i + BATCH]
        query = [{"Input_Original": f"=={t}"} for t in batch]
        records = client.find("Master%20Normalization%20Table", query, limit=10000)
        for rec in records:
            extract(rec['fieldData'])
        done = min(i + BATCH, total)
        log.info("  MNT: %d/%d terms, %d matched, %d junk filtered",
                 done, total, len(name_cache), junk_count)

    unmatched = [t for t in term_list if not name_cache.get(t.lower())]
    if unmatched:
        rescan_matched = 0
        for i in range(0, len(unmatched), BATCH):
            batch = unmatched[i:i + BATCH]
            query = [{"Input_Original": f"=={t} "} for t in batch]
            records = client.find("Master%20Normalization%20Table", query, limit=10000)
            for rec in records:
                extract(rec['fieldData'])
            rescan_matched = sum(1 for t in unmatched if name_cache.get(t.lower()))
        if rescan_matched:
            log.info("  MNT whitespace rescan: %d additional terms matched", rescan_matched)

    return name_cache


def query_authority_by_name(client, terms, name_cache):
    """Query Authority_Place for exact matches on Auth_Place_Name.

    This catches authority records that exist in the place hierarchy but were
    never entered into the MNT, which is common for less frequently referenced
    places.
    """
    added = 0

    def extract(field_data):
        nonlocal added
        name = field_str(field_data, 'Auth_Place_Name')
        uuid = field_str(field_data, 'UUID')
        if uuid and name:
            key = name.lower()
            if uuid not in name_cache.get(key, set()):
                name_cache[key].add(uuid)
                added += 1

    term_list = list(terms)
    total = len(term_list)
    for i in range(0, total, BATCH):
        batch = term_list[i:i + BATCH]
        query = [{"Auth_Place_Name": f"=={t}"} for t in batch]
        records = client.find("Authority_Place", query, limit=10000)
        for rec in records:
            extract(rec['fieldData'])
        done = min(i + BATCH, total)
        log.info("  Authority by name: %d/%d terms, %d new UUIDs", done, total, added)
    return added


def query_fallback_transforms(client, unmatched_terms, name_cache):
    """For terms that failed both MNT and direct authority lookup, apply
    transforms (strip prefixes, expand abbreviations, separate jurisdiction
    suffixes) and re-query. Results are stored under the ORIGINAL term key
    in name_cache so they map back to the input that produced them.

    Terms with a jurisdiction suffix (e.g., "Washington County") get a
    compound query filtering on both Auth_Place_Name and Jurisdiction.
    All other transforms query both the MNT and Authority_Place without
    jurisdiction filtering.
    """
    transforms = {}
    for term in unmatched_terms:
        cleaned, jurisdiction = transform_term(term)
        if cleaned:
            transforms[term] = (cleaned, jurisdiction)

    if not transforms:
        log.info("  No transformable terms")
        return 0

    items = list(transforms.items())
    jurisdiction_terms = [(orig, cleaned, jur) for orig, (cleaned, jur) in items if jur]
    non_jurisdiction_terms = [(orig, cleaned) for orig, (cleaned, jur) in items if not jur]
    added = 0

    # Jurisdiction-filtered lookups: "Washington County" becomes a query for
    # Auth_Place_Name="Washington" AND Jurisdiction="County"
    if jurisdiction_terms:
        for i in range(0, len(jurisdiction_terms), BATCH):
            batch = jurisdiction_terms[i:i + BATCH]
            query = [{"Auth_Place_Name": f"=={cleaned}", "Jurisdiction": f"=={jur}"}
                     for _, cleaned, jur in batch]
            # Map cleaned names back to original terms for cache storage
            lookup = defaultdict(list)
            for orig, cleaned, jurisdiction in batch:
                lookup[cleaned.lower()].append((orig, jurisdiction))

            records = client.find("Authority_Place", query, limit=10000)
            for rec in records:
                field_data = rec['fieldData']
                name = field_str(field_data, 'Auth_Place_Name')
                uuid = field_str(field_data, 'UUID')
                record_jurisdiction = field_str(field_data, 'Jurisdiction')
                if uuid and name and name.lower() in lookup:
                    for orig, expected_jurisdiction in lookup[name.lower()]:
                        if record_jurisdiction.lower() == expected_jurisdiction.lower():
                            name_cache[orig.lower()].add(uuid)
                            added += 1

            done = min(i + BATCH, len(jurisdiction_terms))
            log.info("  Fallback (jurisdiction): %d/%d terms, %d UUIDs",
                     done, len(jurisdiction_terms), added)

    # Non-jurisdiction transforms: "near St. Louis" -> "Saint Louis", queried
    # against both MNT and Authority_Place without jurisdiction filtering
    non_jurisdiction_added = 0
    if non_jurisdiction_terms:
        for i in range(0, len(non_jurisdiction_terms), BATCH):
            batch = non_jurisdiction_terms[i:i + BATCH]
            cleaned_list = [cleaned for _, cleaned in batch]
            lookup = defaultdict(list)
            for orig, cleaned in batch:
                lookup[cleaned.lower()].append(orig)

            mnt_query = [{"Input_Original": f"=={cleaned}"} for cleaned in cleaned_list]
            mnt_records = client.find("Master%20Normalization%20Table", mnt_query, limit=10000)
            for rec in mnt_records:
                field_data = rec['fieldData']
                input_original = field_str(field_data, 'Input_Original')
                authority_id = field_str(field_data, 'Match_Authority_ID')
                if input_original and authority_id and is_valid_uuid(authority_id) and input_original.lower() in lookup:
                    for orig in lookup[input_original.lower()]:
                        name_cache[orig.lower()].add(authority_id)
                        non_jurisdiction_added += 1

            authority_query = [{"Auth_Place_Name": f"=={cleaned}"} for cleaned in cleaned_list]
            authority_records = client.find("Authority_Place", authority_query, limit=10000)
            for rec in authority_records:
                field_data = rec['fieldData']
                name = field_str(field_data, 'Auth_Place_Name')
                uuid = field_str(field_data, 'UUID')
                if uuid and name and name.lower() in lookup:
                    for orig in lookup[name.lower()]:
                        name_cache[orig.lower()].add(uuid)
                        non_jurisdiction_added += 1

            done = min(i + BATCH, len(non_jurisdiction_terms))
            log.info("  Fallback (other): %d/%d terms, %d UUIDs",
                     done, len(non_jurisdiction_terms), non_jurisdiction_added)

    return added + non_jurisdiction_added


def query_preposition_extractions(client, unmatched_terms, name_cache):
    """FM version: for terms still unmatched after prefix-anchored transforms,
    scan for spatial prepositions anywhere and try matching the substring after
    them.  Results stored under the ORIGINAL term key in name_cache."""
    extractions = {}
    for term in unmatched_terms:
        candidates = extract_after_preposition(term)
        if not candidates:
            continue
        names_for_term = []
        for candidate in candidates:
            cleaned, jurisdiction = transform_term(candidate)
            names_for_term.append((candidate, jurisdiction))
            if cleaned:
                names_for_term.append((cleaned, jurisdiction))
        if names_for_term:
            extractions[term] = names_for_term

    if not extractions:
        log.info("  No preposition-extractable terms")
        return 0

    added = 0
    all_names = []
    for orig, names_for_term in extractions.items():
        for name, _jur in names_for_term:
            all_names.append((orig, name, _jur))

    for i in range(0, len(all_names), BATCH):
        batch = all_names[i:i + BATCH]
        lookup = defaultdict(list)
        for orig, name, jur in batch:
            lookup[name.lower()].append((orig, jur))

        mnt_query = [{"Input_Original": f"=={name}"} for _, name, _ in batch]
        mnt_records = client.find("Master%20Normalization%20Table", mnt_query, limit=10000)
        for rec in mnt_records:
            field_data = rec['fieldData']
            input_original = field_str(field_data, 'Input_Original')
            authority_id = field_str(field_data, 'Match_Authority_ID')
            if input_original and authority_id and is_valid_uuid(authority_id) and input_original.lower() in lookup:
                for orig, _jur in lookup[input_original.lower()]:
                    name_cache[orig.lower()].add(authority_id)
                    added += 1

        authority_query = [{"Auth_Place_Name": f"=={name}"} for _, name, _ in batch]
        authority_records = client.find("Authority_Place", authority_query, limit=10000)
        for rec in authority_records:
            field_data = rec['fieldData']
            name = field_str(field_data, 'Auth_Place_Name')
            uuid = field_str(field_data, 'UUID')
            record_jurisdiction = field_str(field_data, 'Jurisdiction')
            if uuid and name and name.lower() in lookup:
                for orig, jur in lookup[name.lower()]:
                    if jur and record_jurisdiction.lower() != jur.lower():
                        continue
                    name_cache[orig.lower()].add(uuid)
                    added += 1

        done = min(i + BATCH, len(all_names))
        log.info("  Preposition extractions: %d/%d lookups, %d UUIDs", done, len(all_names), added)

    return added


def query_abbreviation_expansions(client, all_terms, name_cache):
    """Expand jurisdiction abbreviations and add their authority UUIDs to
    name_cache under the ORIGINAL term key. This is additive: existing
    candidates for the term are preserved, and expanded-form candidates are
    merged in. The chain walk in Phase 3 then decides among all candidates.

    Strips trailing periods before matching (so "Ky." matches "ky").
    Handles multi-mapped abbreviations (e.g. "bc" -> both British Columbia
    and Baja California) by querying all expansions.
    """
    expansions = {}
    for term in all_terms:
        normalized = term.lower().rstrip('.')
        expanded = JURISDICTION_ABBREVIATIONS.get(normalized)
        if expanded:
            expansions[term] = [expanded]
        else:
            multi = JURISDICTION_ABBREVIATION_MULTI.get(normalized)
            if multi:
                expansions[term] = multi

    if not expansions:
        log.info("  No abbreviation expansions found")
        return 0

    unique_expanded = set()
    for names in expansions.values():
        unique_expanded.update(names)
    expanded_to_uuids = defaultdict(set)
    expanded_list = list(unique_expanded)
    for i in range(0, len(expanded_list), BATCH):
        batch = expanded_list[i:i + BATCH]
        query = [{"Auth_Place_Name": f"=={name}"} for name in batch]
        records = client.find("Authority_Place", query, limit=10000)
        for rec in records:
            fd = rec['fieldData']
            name = field_str(fd, 'Auth_Place_Name')
            uuid = field_str(fd, 'UUID')
            if uuid and name:
                expanded_to_uuids[name.lower()].add(uuid)

    added = 0
    for orig_term, expanded_names in expansions.items():
        key = orig_term.lower()
        for expanded_name in expanded_names:
            uuids = expanded_to_uuids.get(expanded_name.lower(), set())
            if uuids:
                new_uuids = uuids - name_cache.get(key, set())
                if new_uuids:
                    name_cache[key].update(new_uuids)
                    added += len(new_uuids)

    return added


def _generate_name_variants(term):
    """Return a list of alternate forms for a place name term.

    Handles two categories:
    - Prefix swaps: Saint<->St, Fort<->Ft, Mount<->Mt (bidirectional)
    - Spacing variants: DeKalb<->De Kalb, LaFontaine<->La Fontaine
    """
    variants = []

    for long_form, short_form in PREFIX_SWAPS:
        lower = term.lower()
        long_prefix = long_form.lower() + ' '
        short_prefix = short_form.lower() + ' '
        short_prefix_dot = short_form.lower() + '. '
        if lower.startswith(long_prefix):
            rest = term[len(long_form) + 1:]
            variants.append(f"{short_form} {rest}")
        elif lower.startswith(short_prefix) or lower.startswith(short_prefix_dot):
            dot_offset = len(short_form) + 1
            if lower[dot_offset:dot_offset + 1] == '.':
                dot_offset += 1
            rest = term[dot_offset:].lstrip()
            variants.append(f"{long_form} {rest}")

    m = _SPACING_SPLIT_CAMEL_RE.match(term)
    if m:
        variants.append(f"{m.group(1)} {m.group(2)}")
    elif _SPACING_SPLIT_FLAT_RE.match(term):
        m = _SPACING_SPLIT_FLAT_RE.match(term)
        prefix_part = m.group(1)
        rest_part = m.group(2)
        variants.append(f"{prefix_part} {rest_part[0].upper()}{rest_part[1:]}")

    m = _SPACING_JOIN_RE.match(term)
    if m:
        prefix = m.group(1)
        rest = m.group(2)
        variants.append(f"{prefix}{rest[0].upper()}{rest[1:]}" if rest else f"{prefix}")

    return variants


def query_name_variants(client, all_terms, name_cache):
    """Generate alternate forms of place names (prefix swaps and spacing
    variants) and query both MNT and Authority_Place for each. Results are
    merged into name_cache under the ORIGINAL term key. Additive — existing
    candidates are preserved."""
    variant_map = {}
    for term in all_terms:
        variants = _generate_name_variants(term)
        if variants:
            variant_map[term] = variants

    if not variant_map:
        log.info("  No name variants to try")
        return 0

    unique_variants = set()
    for variants in variant_map.values():
        unique_variants.update(variants)

    variant_to_uuids = defaultdict(set)
    variant_list = list(unique_variants)

    for i in range(0, len(variant_list), BATCH):
        batch = variant_list[i:i + BATCH]

        mnt_query = [{"Input_Original": f"=={v}"} for v in batch]
        mnt_records = client.find("Master%20Normalization%20Table", mnt_query, limit=10000)
        for rec in mnt_records:
            fd = rec['fieldData']
            original = field_str(fd, 'Input_Original')
            authority_id = field_str(fd, 'Match_Authority_ID')
            if original and authority_id and is_valid_uuid(authority_id):
                variant_to_uuids[original.lower()].add(authority_id)

        auth_query = [{"Auth_Place_Name": f"=={v}"} for v in batch]
        auth_records = client.find("Authority_Place", auth_query, limit=10000)
        for rec in auth_records:
            fd = rec['fieldData']
            name = field_str(fd, 'Auth_Place_Name')
            uuid = field_str(fd, 'UUID')
            if uuid and name:
                variant_to_uuids[name.lower()].add(uuid)

        done = min(i + BATCH, len(variant_list))
        log.info("  Name variants: %d/%d variants queried", done, len(variant_list))

    added = 0
    for orig_term, variants in variant_map.items():
        key = orig_term.lower()
        for variant in variants:
            uuids = variant_to_uuids.get(variant.lower(), set())
            new_uuids = uuids - name_cache.get(key, set())
            if new_uuids:
                name_cache[key].update(new_uuids)
                added += len(new_uuids)

    return added


# ---------------------------------------------------------------------------
# Phase 1d: Spelling correction via symspellpy
# ---------------------------------------------------------------------------


def build_spelling_index(tsv_path):
    """Build a SymSpell edit-distance-1 index from PA canonical names."""
    sym = SymSpell(max_dictionary_edit_distance=1, prefix_length=7)
    seen = set()
    with open(tsv_path, encoding='utf-8') as f:
        reader = csv.DictReader(f, delimiter='\t')
        for row in reader:
            term = row.get('Term', '').strip()
            if not term:
                continue
            folded = ascii_fold(term)
            if folded and folded not in seen:
                sym.create_dictionary_entry(folded, 1)
                seen.add(folded)
    return sym


def build_spelling_index_from_memory():
    """Build the SymSpell index from _LOCAL data already in memory: PA
    canonical names plus every dictionary/MNT term. Used in dict mode so
    the correction vocabulary covers the union."""
    sym = SymSpell(max_dictionary_edit_distance=1, prefix_length=7)
    seen = set()
    for key in list(_LOCAL.pa_by_name) + list(_LOCAL.mnt_by_raw):
        if ',' in key:
            continue          # full-string MNT keys are not spelling vocabulary
        folded = ascii_fold(key)
        if folded and folded not in seen:
            sym.create_dictionary_entry(folded, 1)
            seen.add(folded)
    return sym


SPELLING_LOG_FIELDS = ['original_term', 'corrected_term', 'edit_distance', 'authority_uuid']


def query_spelling_corrections(client, terms, name_cache, sym_spell,
                               transform_map=None):
    """FM-backed version of query_spelling_corrections_local: find
    edit-distance-1 corrections and add their authority UUIDs.
    Returns (added_count, correction_log_rows)."""
    candidates_by_key = {}
    for term in terms:
        if len(term) < MIN_SPELLING_LEN:
            continue
        key = term.lower()
        folded = ascii_fold(term)
        suggestions = sym_spell.lookup(folded, Verbosity.ALL, max_edit_distance=1)
        if suggestions:
            corrected = [s.term for s in suggestions if s.term != folded]
            if corrected:
                candidates_by_key[key] = corrected

    if transform_map:
        for orig_term, cleaned in transform_map.items():
            if len(cleaned) < MIN_SPELLING_LEN:
                continue
            key = orig_term.lower()
            folded = ascii_fold(cleaned)
            suggestions = sym_spell.lookup(folded, Verbosity.ALL, max_edit_distance=1)
            if suggestions:
                corrected = [s.term for s in suggestions if s.term != folded]
                if corrected:
                    existing = candidates_by_key.get(key, [])
                    candidates_by_key[key] = existing + corrected

    if not candidates_by_key:
        log.info("  No spelling corrections found")
        return 0, []

    all_corrected = set()
    for cands in candidates_by_key.values():
        all_corrected.update(cands)

    corrected_to_uuids = defaultdict(set)
    corrected_list = list(all_corrected)
    for i in range(0, len(corrected_list), BATCH):
        batch = corrected_list[i:i + BATCH]
        query = [{"Auth_Place_Name": f"=={t}"} for t in batch]
        records = client.find("Authority_Place", query, limit=10000)
        for rec in records:
            name = field_str(rec['fieldData'], 'Auth_Place_Name')
            uuid = field_str(rec['fieldData'], 'UUID')
            if name and uuid:
                corrected_to_uuids[name.lower()].add(uuid)

    added = 0
    corrections = []
    for key, candidates in candidates_by_key.items():
        for candidate in candidates:
            uuids = corrected_to_uuids.get(candidate, set())
            new_uuids = uuids - name_cache.get(key, set())
            if new_uuids:
                name_cache[key].update(new_uuids)
                added += len(new_uuids)
                corrections.append({
                    'original_term': key,
                    'corrected_term': candidate,
                    'edit_distance': 1,
                    'authority_uuid': ';'.join(sorted(new_uuids)),
                })

    return added, corrections


def write_spelling_log(corrections, path):
    """Write the spelling-correction side file (TSV)."""
    with open(path, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=SPELLING_LOG_FIELDS, delimiter='\t')
        writer.writeheader()
        writer.writerows(corrections)


# ---------------------------------------------------------------------------
# Phase 1e: FamilySearch city resolution
#
# For entries where a city-level term went unresolved through 1a-1c but a
# right-side jurisdiction term (state/country) IS resolved, query the
# FamilySearch Places API to find the canonical city name, then retry
# Authority_Place with that name. This bridges spelling variants and
# historical names that FM knows under a different string.
# ---------------------------------------------------------------------------


def _fs_request(url, _max_retries=3):
    """GET a FamilySearch API URL with 429 backoff. Returns parsed JSON or
    None on any failure."""
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    for attempt in range(_max_retries):
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status == 204:
                    return None
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < _max_retries - 1:
                time.sleep(2 ** attempt)
                continue
            return None
        except Exception:
            return None
    return None


def _resolve_fs_id(term, fs_id_cache):
    """Resolve a jurisdiction term to its FamilySearch place ID, preferring
    an exact quoted-name search over the fuzzy fallback."""
    key = term.lower()
    if key in fs_id_cache:
        return fs_id_cache[key]

    q_quoted = urllib.parse.quote(f'name:"{term}"', safe=':+~"')
    data = _fs_request(f"{FS_BASE}?q={q_quoted}&count=5")

    if not data:
        q_unquoted = urllib.parse.quote(f"name:{term}", safe=':+~')
        data = _fs_request(f"{FS_BASE}?q={q_unquoted}&count=5")

    if not data:
        fs_id_cache[key] = None
        return None

    for entry in data.get("entries", []):
        places = entry.get("content", {}).get("gedcomx", {}).get("places", [])
        if places:
            fs_id_cache[key] = places[0].get("id")
            return fs_id_cache[key]

    fs_id_cache[key] = None
    return None


def _fs_city_lookup(city_term, parent_fs_id):
    """Search FamilySearch for a city under a parent jurisdiction and return
    its canonical display name, or None."""
    encoded_city_quoted = urllib.parse.quote(f'"{city_term}"')
    q_quoted = f"name:{encoded_city_quoted}+parentId:{parent_fs_id}~"
    data = _fs_request(f"{FS_BASE}?q={q_quoted}&count=10")

    if not data:
        encoded_city = urllib.parse.quote(city_term)
        q_unquoted = f"name:{encoded_city}+parentId:{parent_fs_id}~"
        data = _fs_request(f"{FS_BASE}?q={q_unquoted}&count=10")

    if not data:
        return None

    for entry in data.get("entries", []):
        places = entry.get("content", {}).get("gedcomx", {}).get("places", [])
        if not places:
            continue
        place_type = places[0].get("type", "")
        if place_type.split("/")[-1] == FS_TYPE_CITY:
            full_name = places[0].get("names", [{}])[0].get("value", "")
            return full_name.split(",")[0].strip()
    # TODO: consider type 378 (township) for rural records
    return None


def query_fs_places(client, parsed, name_cache, max_workers=8):
    """Phase 1e: use FamilySearch to resolve city terms that failed 1a-1d.

    Walks parsed entries to find (unresolved_city, jurisdiction) pairs,
    deduplicates them, queries FS for the canonical city name, and retries
    Authority_Place with the result.

    FS lookups are parallelized across threads; FM queries run sequentially
    afterward since there are far fewer of them.
    """
    pairs = {}
    for place, guid, frequency, terms in parsed:
        for i, term in enumerate(terms):
            if name_cache.get(term.lower()):
                continue
            if re.match(r'^\d', term):
                continue
            right = [t for t in terms[i + 1:] if name_cache.get(t.lower())]
            if not right:
                continue
            jurisdiction = right[0]
            key = (term.lower(), jurisdiction.lower())
            if key not in pairs:
                pairs[key] = (term, jurisdiction)

    if not pairs:
        log.info("  No eligible (city, jurisdiction) pairs")
        return 0

    unique_pairs = list(pairs.values())
    log.info("  %d unique (city, jurisdiction) pairs to resolve...", len(unique_pairs))

    unique_jurisdictions = list({j.lower(): j for j in
                                 [jp for _, jp in unique_pairs]}.values())
    log.info("  Resolving %d unique jurisdiction FS IDs (%d threads)...",
             len(unique_jurisdictions), max_workers)
    fs_id_cache = {}
    fs_id_lock = threading.Lock()

    def _resolve_fs_id_threaded(term):
        result = _resolve_fs_id(term, {})
        with fs_id_lock:
            fs_id_cache[term.lower()] = result

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        list(pool.map(_resolve_fs_id_threaded, unique_jurisdictions))

    resolved_jurisdictions = sum(1 for v in fs_id_cache.values() if v)
    log.info("  %d/%d jurisdictions resolved",
             resolved_jurisdictions, len(unique_jurisdictions))

    def _lookup_one_pair(city_term, jurisdiction_term):
        parent_id = fs_id_cache.get(jurisdiction_term.lower())
        if not parent_id:
            return (city_term, None)
        canonical = _fs_city_lookup(city_term, parent_id)
        return (city_term, canonical)

    log.info("  Looking up %d city terms via FS (%d threads)...",
             len(unique_pairs), max_workers)
    fs_results = []
    done_count = 0
    done_lock = threading.Lock()

    def _lookup_and_track(pair):
        nonlocal done_count
        result = _lookup_one_pair(*pair)
        with done_lock:
            done_count += 1
            if done_count % 100 == 0:
                log.info("    [%d/%d] FS lookups complete",
                         done_count, len(unique_pairs))
        return result

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        fs_results = list(pool.map(_lookup_and_track, unique_pairs))

    fs_hits = 0
    fm_added = 0
    fm_queries = 0
    canonicals = []
    for city_term, canonical in fs_results:
        if canonical:
            canonicals.append((city_term, canonical))

    log.info("  %d FS hits, querying FM for authority records...", len(canonicals))
    for city_term, canonical in canonicals:
        fs_hits += 1
        query = [{"Auth_Place_Name": f"=={canonical}"}]
        fm_queries += 1
        records = client.find("Authority_Place", query)
        for rec in records:
            fd = rec['fieldData']
            uuid = field_str(fd, 'UUID')
            if uuid:
                name_cache[city_term.lower()].add(uuid)
                fm_added += 1

    fs_skipped = len(unique_pairs) - len(canonicals)
    log.info("  %d FS hits -> %d new authority records added "
             "(%d skipped, %d FM queries)",
             fs_hits, fm_added, fs_skipped, fm_queries)
    return fm_added


# ---------------------------------------------------------------------------
# Phase 2: Resolve authority records
#
# Phase 1 gives us UUIDs, but matching requires the full authority records
# (Parent_UUID for chain walking, Level for ranking). Phase 2
# fetches all of these in batch, then walks up the Parent_UUID hierarchy
# level by level to pre-cache ancestor records. Without this pre-fetch,
# Phase 3 would make individual API calls for each parent encountered
# during chain walking, which dominated runtime in earlier versions.
# ---------------------------------------------------------------------------

def query_authority_batch(client, uuids):
    """Fetch full authority records for a set of UUIDs from Authority_Place."""
    auth_cache = {}
    uuid_list = list(uuids)
    total = len(uuid_list)
    for i in range(0, total, BATCH):
        batch = uuid_list[i:i + BATCH]
        query = [{"UUID": f"=={uuid}"} for uuid in batch]
        records = client.find("Authority_Place", query)
        for rec in records:
            field_data = rec['fieldData']
            uuid = field_str(field_data, 'UUID')
            if uuid:
                auth_cache[uuid] = field_data
        done = min(i + BATCH, total)
        log.info("  Authority: %d/%d UUIDs resolved, %d found",
                 done, total, len(auth_cache))
    return auth_cache


def prefetch_parent_chains(client, auth_cache):
    """Walk up the jurisdiction hierarchy in bulk, layer by layer.

    Each round collects every Parent_UUID referenced by cached records that
    is not yet in the cache, fetches those parents in batch, and repeats.
    Jurisdiction hierarchies are typically 5-6 levels deep (city -> county ->
    state -> country), so this converges in 2-3 rounds.
    """
    while True:
        missing = set()
        for rec in auth_cache.values():
            parent_uuid = field_str(rec, 'Parent_UUID')
            if parent_uuid and parent_uuid not in auth_cache:
                missing.add(parent_uuid)
        if not missing:
            break
        missing_list = list(missing)
        total = len(missing_list)
        fetched = 0
        for i in range(0, total, BATCH):
            batch = missing_list[i:i + BATCH]
            query = [{"UUID": f"=={uuid}"} for uuid in batch]
            records = client.find("Authority_Place", query)
            for rec in records:
                field_data = rec['fieldData']
                uuid = field_str(field_data, 'UUID')
                if uuid:
                    auth_cache[uuid] = field_data
                    fetched += 1
            done = min(i + BATCH, total)
            log.info("  Parent pre-fetch: %d/%d UUIDs, %d found",
                     done, total, fetched)
        if fetched == 0:
            break


# ---------------------------------------------------------------------------
# Phase 3: Matching
#
# For each input place string, we reverse the comma-separated terms and work
# right-to-left. The rightmost term (broadest geography, e.g., "United States
# of America") seeds the confirmed set. Each successive term to the left is
# checked: does any candidate for this term have a Parent_UUID chain that
# connects to the current confirmed set? If so, those verified candidates
# replace the confirmed set (narrowing from country to state to county to
# city). Terms that cannot be verified are skipped rather than failing the
# whole match, since input data often contains extra qualifiers like
# "near" or informal region names.
# ---------------------------------------------------------------------------

def _prefetch_missing_parents(candidate_ids, auth_cache, client, max_hops=10):
    """Collect all parent UUIDs reachable from candidate_ids that are missing
    from auth_cache, then fetch them in batch. This avoids one-at-a-time API
    calls during walk_up_chain.
    """
    missing = set()
    for cid in candidate_ids:
        current = cid
        for _ in range(max_hops):
            rec = auth_cache.get(current)
            if not rec:
                missing.add(current)
                break
            parent_uuid = field_str(rec, 'Parent_UUID')
            if not parent_uuid or parent_uuid in auth_cache:
                break
            current = parent_uuid
    if not missing or client is None:
        return
    missing_list = list(missing)
    for i in range(0, len(missing_list), BATCH):
        batch = missing_list[i:i + BATCH]
        query = [{"UUID": f"=={uuid}"} for uuid in batch]
        records = client.find("Authority_Place", query)
        for rec in records:
            fd = rec['fieldData']
            uuid = field_str(fd, 'UUID')
            if uuid:
                auth_cache[uuid] = fd
    if missing:
        _prefetch_missing_parents(
            [m for m in missing if m in auth_cache], auth_cache, client, max_hops)


def walk_up_chain(candidate_id, target_ids, auth_cache, client, max_hops=10):
    """Check whether candidate_id is a descendant of any UUID in target_ids
    by following the Parent_UUID chain upward. Returns True if a connection
    is found within max_hops, False otherwise.
    """
    current = candidate_id
    for _ in range(max_hops):
        rec = auth_cache.get(current)
        if not rec:
            return False
        parent_uuid = field_str(rec, 'Parent_UUID')
        if not parent_uuid:
            return False
        if parent_uuid in target_ids:
            return True
        current = parent_uuid
    return False


def get_population(auth_record):
    """Extract population as an integer from a FM authority record.
    Missing, empty, or non-numeric values return 0."""
    val = auth_record.get('Population')
    if val is None:
        return 0
    raw = str(val).strip()
    try:
        return int(float(raw))
    except (ValueError, TypeError):
        return 0


def haversine_km(lat1, lon1, lat2, lon2):
    """Great-circle distance in km between two lat/long pairs.
    Returns float('inf') if any coordinate is missing or unparseable."""
    import math
    try:
        la1, lo1, la2, lo2 = float(lat1), float(lon1), float(lat2), float(lon2)
    except (TypeError, ValueError):
        return float('inf')
    la1, lo1, la2, lo2 = map(math.radians, (la1, lo1, la2, lo2))
    dlat = la2 - la1
    dlon = lo2 - lo1
    a = math.sin(dlat / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin(dlon / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(a))


FREQ_MIN = 10      # winner needs at least this many observations
FREQ_RATIO = 5     # and at least this multiple of the runner-up
MAX_ARRAY = 5      # low-confidence arrays keep at most this many ranked candidates


def cap_candidates(ranked_ids, context=""):
    """Trim a ranked candidate-id list to MAX_ARRAY, logging any drop so that
    truncation of a possibly-correct low-population outlier is never silent."""
    if len(ranked_ids) > MAX_ARRAY:
        log.debug("    array truncated%s: %d candidates -> %d",
                  f" ({context})" if context else "", len(ranked_ids), MAX_ARRAY)
        return ranked_ids[:MAX_ARRAY]
    return list(ranked_ids)


def _disambiguate_by_frequency(term, candidates, dict_freq):
    """Pick a winner by dictionary frequency. Fires only when EVERY candidate
    has a frequency entry for this term (mixed dict/MNT-origin sets fall
    through to population rules — absence of freq is not evidence against an
    MNT mapping). Winner needs FREQ_MIN observations and FREQ_RATIO times the
    runner-up."""
    if not term or not dict_freq or len(candidates) < 2:
        return None
    key = term.lower()
    freqs = []
    for uid in candidates:
        f = dict_freq.get((key, uid))
        if f is None:
            return None
        freqs.append((f, uid))
    freqs.sort(key=lambda x: (-x[0], x[1]))
    top_f, top_uid = freqs[0]
    if top_f >= FREQ_MIN and top_f >= FREQ_RATIO * freqs[1][0]:
        return top_uid
    return None


def resolve_parent_only(candidate_ids, auth_cache, client, term=None):
    """Resolve parent_only candidates to a single answer only on strong signals.

    A single candidate resolves directly; a dictionary-frequency prior resolves
    with medium confidence. Population never resolves a multi-candidate set — it
    only orders the array surfaced by resolve_parent_match.

    Returns (winner_uuid, 'parent_resolved'), (winner_uuid, 'freq_resolved'),
    or (None, 'amb').
    """
    if not candidate_ids:
        return (None, 'amb')
    if len(candidate_ids) == 1:
        return (candidate_ids[0], 'parent_resolved')

    # Fetch any missing auth records from FM (skipped in local mode)
    if client is not None:
        missing = [uid for uid in candidate_ids if uid not in auth_cache]
        for i in range(0, len(missing), BATCH):
            batch = missing[i:i + BATCH]
            query = [{"UUID": f"=={uid}"} for uid in batch]
            records = client.find("Authority_Place", query, limit=len(batch))
            for r in records:
                fd = r['fieldData']
                uid = field_str(fd, 'UUID')
                if uid:
                    auth_cache[uid] = fd

    winner = _disambiguate_by_frequency(term, candidate_ids,
                                        _LOCAL.dict_freq or {})
    if winner:
        return (winner, 'freq_resolved')

    # Population never resolves a multi-candidate set. Fall through to amb;
    # the ranked candidate array is surfaced by resolve_parent_match.
    return (None, 'amb')


def _get_parent_level(confirmed_set, auth_cache):
    """Extract the jurisdiction Level from the first candidate (in UUID
    order) that has a valid level. The pick is deterministic but otherwise
    arbitrary when candidates carry different levels; a principled rule
    (e.g. most specific level) is an open design question."""
    for uid in sorted(confirmed_set):
        rec = auth_cache.get(uid, {})
        try:
            return int(field_str(rec, 'Level'))
        except (ValueError, TypeError):
            continue
    return None


PREFERRED_JURISDICTIONS = frozenset({
    'City', 'Town', 'Borough', 'Village', 'Comune', 'Kommune', 'Municipality',
})

FILTERED_JURISDICTIONS = frozenset({
    'Township', 'County', 'Municipio', 'Parish', 'District', 'Arrondissement',
})


def rank_candidates(candidates, auth_cache, parent_level, jurisdiction_hint=None,
                    helper_term=None):
    """Rank candidates by helper-term match, level gap, then population.

    Returns list of (uuid, score) tuples sorted best-first.
    score is (helper_miss, level_gap, neg_population) — lower is better on all axes.
    When parent_level is None (single_term case), level_gap is always 0.
    When helper_term is provided, candidates whose parent chain reaches the
    helper term's UUID get helper_miss=0; others get a penalty that scales
    inversely with the helper term's level (more specific = stronger penalty).
    """
    if not candidates:
        return []

    if jurisdiction_hint is None:
        preferred = [c for c in candidates
                     if field_str(auth_cache.get(c, {}), 'Jurisdiction') in PREFERRED_JURISDICTIONS]
        if preferred:
            candidates = [c for c in candidates
                          if field_str(auth_cache.get(c, {}), 'Jurisdiction') not in FILTERED_JURISDICTIONS]

    # helper term setup
    helper_targets = None
    helper_boost = 0
    if helper_term:
        helper_targets = {helper_term['uuid']}
        helper_boost = max(1, 10 - helper_term['level'])

    def _in_helper_chain(uuid):
        if not helper_targets:
            return False
        current = uuid
        for _ in range(15):
            rec = auth_cache.get(current)
            if not rec:
                return False
            parent_uuid = field_str(rec, 'Parent_UUID')
            if not parent_uuid:
                return False
            if parent_uuid in helper_targets:
                return True
            current = parent_uuid
        return False

    def score(uuid):
        rec = auth_cache.get(uuid, {})
        pop = get_population(rec)
        helper_miss = 0
        if helper_targets:
            if not _in_helper_chain(uuid):
                helper_miss = helper_boost
        if parent_level is None:
            return (helper_miss, 0, -pop)
        try:
            level = int(field_str(rec, 'Level'))
        except (ValueError, TypeError):
            level = 0
        gap = abs(parent_level - level)
        return (helper_miss, gap, -pop)

    scored = [(uuid, score(uuid)) for uuid in candidates]
    scored.sort(key=lambda x: (x[1], x[0]))
    return scored


def detect_tie(ranked_with_scores):
    """Check if top candidates share the same STRUCTURAL score.

    A tie is equality on the structural axes (helper_miss, level_gap) only;
    population (the third score component) is ignored. Population may order the
    array but must never break a tie into a single winner.

    Returns (winner_uuid_or_None, tied_uuids). If tied: winner is None and
    tied_uuids holds every candidate sharing the top structural score, in
    ranked order. If not tied: winner is the top candidate, tied_uuids is empty.
    """
    if not ranked_with_scores:
        return (None, [])
    if len(ranked_with_scores) == 1:
        return (ranked_with_scores[0][0], [])

    top_structural = ranked_with_scores[0][1][:2]
    tied = [uuid for uuid, s in ranked_with_scores if s[:2] == top_structural]

    if len(tied) > 1:
        return (None, tied)
    return (ranked_with_scores[0][0], [])


CONFIDENCE_BY_TYPE = {
    'mnt_full_string': 'high',
    'chain_verified': 'high',
    'single_term': 'high',
    'parent_resolved': 'high',
    'freq_resolved': 'medium',
    'chain_verified_proximity': 'medium',
    'single_amb': 'low',
    'chain_amb': 'low',
    'parent_amb': 'low',
    'parent_rejected': 'low',
}


@dataclass
class MatchResult:
    """Outcome of matching one place string: surviving candidates, how deep
    the right-to-left walk got, and the match_type bucket for reporting."""
    candidate_ids: list = field(default_factory=list)
    depth: int = 0
    match_type: str = 'no_terms'
    skipped_count: int = 0
    skipped_terms: str = ''
    tied_ids: list = field(default_factory=list)
    skipped_had_candidates: bool = False

    @property
    def confidence(self):
        """Trust tier derived from match_type. Population never resolves, so
        match_type alone determines confidence."""
        return CONFIDENCE_BY_TYPE.get(self.match_type, 'none')


def match_entry(terms, name_cache, auth_cache, client, original, jurisdiction_hints=None, ascii_cache=None,
                helper_term=None):
    """Run the right-to-left matching algorithm on a single place string.

    Match types returned:
      - chain_verified: multiple terms connected through the hierarchy
      - chain_amb: chain verified but top candidates tied on level gap + population
      - single_term: only one term in the input, matched directly
      - single_amb: single term but top candidates tied on population
      - parent_only: rightmost term matched but no children verified against it
      - no_auth_match: rightmost term had no candidates in name_cache
      - no_terms: input was empty or whitespace-only
    """
    stripped = [t.strip() for t in terms if t.strip()]
    if not stripped:
        return MatchResult()

    right_to_left = list(reversed(stripped))

    _ascii = ascii_cache or {}
    parent_ids = lookup_name(right_to_left[0], name_cache, _ascii)
    if not parent_ids:
        return MatchResult(match_type='no_auth_match')

    if len(right_to_left) == 1:
        term_key = right_to_left[0].lower()
        hint = (jurisdiction_hints or {}).get(term_key)
        ranked = rank_candidates(list(parent_ids), auth_cache, None,
                                 jurisdiction_hint=hint, helper_term=helper_term)
        if len(ranked) == 1:
            return MatchResult([ranked[0][0]], depth=1, match_type='single_term')
        all_ids = [uuid for uuid, _ in ranked]
        winner = _disambiguate_by_frequency(right_to_left[0], all_ids,
                                            _LOCAL.dict_freq or {})
        if winner:
            return MatchResult([winner], depth=1, match_type='freq_resolved')
        return MatchResult([], depth=1, match_type='single_amb',
                           tied_ids=cap_candidates(all_ids, "single_amb"))

    confirmed = parent_ids
    depth = 1
    skipped = []
    skipped_with_candidates = []
    parent_level_for_ranking = None

    for i in range(1, len(right_to_left)):
        if (right_to_left[i].lower() == right_to_left[i - 1].lower()
                and i < len(right_to_left) - 1):
            skipped.append(right_to_left[i])
            continue

        child_ids = lookup_name(right_to_left[i], name_cache, _ascii)
        if not child_ids:
            skipped.append(right_to_left[i])
            continue

        if len(child_ids) > 50:
            log.debug("    term '%s': %d candidates, prefetching...",
                      right_to_left[i], len(child_ids))
        _prefetch_missing_parents(child_ids, auth_cache, client)
        verified = {
            candidate_id for candidate_id in child_ids
            if walk_up_chain(candidate_id, confirmed, auth_cache, client)
        }

        if verified:
            parent_level_for_ranking = _get_parent_level(confirmed, auth_cache)
            confirmed = verified
            depth += 1
        else:
            skipped.append(right_to_left[i])
            skipped_with_candidates.append((right_to_left[i], child_ids))

    # --- Proximity fallback ---
    # When depth >= 2 and skipped terms had candidates that failed chain
    # verification against the confirmed county, check if any verify against
    # the state (parent of confirmed county) and are within PROXIMITY_THRESHOLD_KM
    # of the confirmed county. This catches likely wrong-county data-entry
    # errors (e.g. a city recorded under the wrong adjacent county).
    proximity_matched = False
    proximity_annotations = []
    if depth >= 2 and skipped_with_candidates:
        confirmed_county_ids = set(confirmed)
        state_ids = set()
        for uid in confirmed:
            rec = auth_cache.get(uid, {})
            parent_uuid = field_str(rec, 'Parent_UUID')
            if parent_uuid:
                state_ids.add(parent_uuid)

        if state_ids:
            proximity_candidates = []
            for skipped_term, candidate_ids in skipped_with_candidates:
                _prefetch_missing_parents(candidate_ids, auth_cache, client)
                state_verified = {
                    cid for cid in candidate_ids
                    if walk_up_chain(cid, state_ids, auth_cache, client)
                }
                if not state_verified:
                    continue

                for cid in sorted(state_verified):
                    cid_rec = auth_cache.get(cid, {})
                    cid_parent = field_str(cid_rec, 'Parent_UUID')
                    if not cid_parent:
                        continue
                    cid_county_rec = auth_cache.get(cid_parent, {})
                    if not cid_county_rec:
                        continue

                    min_dist = float('inf')
                    closest_confirmed = None
                    for conf_uid in confirmed_county_ids:
                        conf_rec = auth_cache.get(conf_uid, {})
                        dist = haversine_km(
                            cid_county_rec.get('Latitude', ''),
                            cid_county_rec.get('Longitude', ''),
                            conf_rec.get('Latitude', ''),
                            conf_rec.get('Longitude', ''),
                        )
                        if dist < min_dist:
                            min_dist = dist
                            closest_confirmed = conf_uid

                    if min_dist <= PROXIMITY_THRESHOLD_KM:
                        proximity_candidates.append(cid)
                        conf_name = field_str(
                            auth_cache.get(closest_confirmed, {}), 'Auth_Place_Name')
                        cid_county_name = field_str(cid_county_rec, 'Auth_Place_Name')
                        proximity_annotations.append(
                            f"{conf_name} County (proximity: {min_dist:.0f}km, "
                            f"actual: {cid_county_name} County)")

                        if skipped_term in skipped:
                            skipped.remove(skipped_term)

            if proximity_candidates:
                levels = []
                for cid in proximity_candidates:
                    rec = auth_cache.get(cid, {})
                    try:
                        levels.append((cid, int(field_str(rec, 'Level'))))
                    except (ValueError, TypeError):
                        levels.append((cid, 99))
                min_level = min(lv for _, lv in levels)
                most_specific = [cid for cid, lv in levels if lv == min_level]
                parent_level_for_ranking = _get_parent_level(confirmed, auth_cache)
                confirmed = set(most_specific)
                depth += 1
                proximity_matched = True

    if depth > 1:
        # Find the leftmost term that actually verified (not skipped)
        leftmost_key = right_to_left[len(right_to_left) - 1].lower()
        for i in range(len(right_to_left) - 1, 0, -1):
            if right_to_left[i] not in skipped:
                leftmost_key = right_to_left[i].lower()
                break
        hint = (jurisdiction_hints or {}).get(leftmost_key)
        ranked = rank_candidates(list(confirmed), auth_cache, parent_level_for_ranking,
                                 jurisdiction_hint=hint)
        winner, tied = detect_tie(ranked)

        skip_count = len(skipped)
        skip_parts = list(skipped)
        skip_parts.extend(proximity_annotations)
        skip_str = '; '.join(skip_parts)

        if tied:
            return MatchResult([], depth, 'chain_amb', skip_count, skip_str,
                               cap_candidates(tied, "chain_amb"))
        mt = 'chain_verified_proximity' if proximity_matched else 'chain_verified'
        ids = [winner] if winner else []
        return MatchResult(ids, depth, mt, skip_count, skip_str)

    # parent_only: pass UUIDs through for resolve_parent_only in main()
    skip_count = len(skipped)
    skip_str = '; '.join(skipped)
    ranked = rank_candidates(list(confirmed), auth_cache, None)
    ids = [uuid for uuid, _ in ranked]
    return MatchResult(ids, depth, 'parent_only', skip_count, skip_str,
                       skipped_had_candidates=bool(skipped_with_candidates))


# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------

def load_entries(path):
    """Read the input TSV, expecting columns: place, guid, frequency."""
    with open(path, 'r', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f, delimiter='\t')
        entries = list(reader)
        if 'place' not in (reader.fieldnames or []):
            raise ValueError("input TSV missing required 'place' column")
        missing = [c for c in ('guid', 'frequency') if c not in reader.fieldnames]
        if missing:
            print(f"WARNING: input TSV missing column(s) {missing}; defaulting to blank values")
        return entries


def parse_entries(entries):
    """Split each entry's place string into comma/semicolon-separated terms
    and collect the full set of unique terms across all entries for bulk lookup.
    Also detects jurisdiction hints (County, Township, etc.) for each term.
    Filters out noise terms (standalone Route/RD/RR references).
    """
    parsed = []
    all_terms = set()
    jurisdiction_hints = {}
    for entry in entries:
        raw_terms = [t.strip() for t in re.split(r'[,;]', entry['place']) if t.strip()]
        terms = [t for t in raw_terms if not NOISE_TERM_RE.match(t)]
        if not terms:
            terms = raw_terms
        parsed.append((entry['place'], entry.get('guid', ''), entry.get('frequency', ''), terms))
        all_terms.update(terms)
        for term in terms:
            hint = detect_jurisdiction_hint(term)
            if hint:
                jurisdiction_hints[term.lower()] = hint
    return parsed, all_terms, jurisdiction_hints


def _resolve_output_paths(input_path, output_dir):
    """Build date-sorted, auto-numbered output paths inside output_dir/.

    Pattern: <output_dir>/MM-DD/<input_stem>_NN.tsv
    where NN increments per input name per day.
    """
    from datetime import datetime
    stem = os.path.splitext(os.path.basename(input_path))[0]
    day_dir = os.path.join(output_dir, datetime.now().strftime('%m-%d'))
    os.makedirs(day_dir, exist_ok=True)

    existing = [f for f in os.listdir(day_dir) if f.startswith(stem + '_') and f.endswith('.tsv') and '_ties' not in f]
    max_num = 0
    for f in existing:
        part = f[len(stem) + 1:].replace('.tsv', '')
        if part.isdigit():
            max_num = max(max_num, int(part))

    num = str(max_num + 1).zfill(2)
    output = os.path.join(day_dir, f'{stem}_{num}.tsv')
    tie_output = os.path.join(day_dir, f'{stem}_{num}_ties.tsv')
    spelling_log = os.path.join(day_dir, f'{stem}_{num}_spelling.tsv')
    return output, tie_output, spelling_log


def build_result_row(match, original, guid, frequency, auth_cache):
    """Build one main-output row for a match. The ranked candidate list is
    inlined into candidate_ids / candidate_names (pipe-delimited) so the main
    file is self-contained: single-answer types carry one candidate, low-
    confidence amb types carry the whole ranked array. authority_* mirror the
    top-ranked candidate as the best guess."""
    all_candidates = match.candidate_ids or match.tied_ids
    # Use the full type-ahead path so same-name candidates are distinguishable
    # (e.g. "Beverly, Essex, Massachusetts" vs "Beverly, ...); fall back to the
    # bare place name when type-ahead is absent.
    names = [(field_str(auth_cache.get(cid, {}), 'Type_Ahead_Value')
              or field_str(auth_cache.get(cid, {}), 'Auth_Place_Name'))
             for cid in all_candidates]
    row = {
        'original': original,
        'guid': guid,
        'frequency': frequency,
        'match_type': match.match_type,
        'confidence': match.confidence,
        'match_depth': match.depth,
        'candidates': len(all_candidates),
        'authority_name': '',
        'type_ahead': '',
        'jurisdiction': '',
        'level': '',
        'authority_id': '',
        'candidate_ids': '|'.join(all_candidates),
        'candidate_names': '|'.join(names),
        'skipped_count': match.skipped_count,
        'skipped_terms': match.skipped_terms,
    }
    # parent_rejected is a non-match: expose the candidate columns for context
    # but leave the authority_* fields blank so it never reads as a resolution.
    if all_candidates and match.match_type != 'parent_rejected':
        best_id = all_candidates[0]
        best_record = auth_cache.get(best_id, {})
        row['authority_name'] = field_str(best_record, 'Auth_Place_Name')
        row['type_ahead'] = field_str(best_record, 'Type_Ahead_Value')
        row['jurisdiction'] = field_str(best_record, 'Jurisdiction')
        row['level'] = field_str(best_record, 'Level')
        row['authority_id'] = best_id
    return row


def write_results(results, path):
    with open(path, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_FIELDS, delimiter='\t')
        writer.writeheader()
        writer.writerows(results)


def write_ties(ties, path):
    with open(path, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=TIE_OUTPUT_FIELDS, delimiter='\t')
        writer.writeheader()
        writer.writerows(ties)


def print_summary(results, call_count, elapsed_sec, output_path):
    types = defaultdict(int)
    for row in results:
        types[row['match_type']] += 1

    print(f"\n{'='*50}")
    print(f"RESULTS — {len(results)} entries")
    print(f"{'='*50}")
    for match_type in ['mnt_full_string', 'chain_verified', 'chain_verified_proximity',
                       'chain_amb', 'single_term', 'single_amb', 'freq_resolved',
                       'parent_resolved', 'parent_rejected', 'parent_only', 'parent_amb',
                       'illegible', 'no_auth_match', 'no_terms']:
        if match_type in types:
            print(f"  {match_type:20s} {types[match_type]:>5}")

    skip_entries = sum(1 for r in results if r['skipped_count'] > 0)
    total_skips = sum(r['skipped_count'] for r in results)
    print(f"\n  Entries with skipped terms: {skip_entries}")
    print(f"  Total skipped terms: {total_skips}")
    print(f"  FM API calls: {call_count}")
    print(f"  Total time: {elapsed_sec:.1f}s")
    print(f"  Output: {output_path}")


# ---------------------------------------------------------------------------
# Helper term resolution
#
# An optional geographic context string (e.g., "Utah, USA") that biases
# single-term matching by providing a set of known ancestor UUIDs. The caller
# passes RTL_HELPER_TERM via env or interactive prompt; this function resolves
# it to a single authority record and walks up its parent chain to collect
# ancestor UUIDs that can be used in Phase 3 scoring.
# ---------------------------------------------------------------------------

def resolve_helper_term(client, term_string, auth_cache, interactive=False):
    """Resolve a helper term string to a single authority record.

    When interactive=False (default), ambiguous matches pick the candidate
    with the highest population. Pass interactive=True to prompt the user.
    """
    if not term_string:
        return None

    terms = [t.strip() for t in re.split(r'[,;]', term_string) if t.strip()]
    if not terms:
        return None

    # Query each term against Authority_Place
    term_candidates = {}
    for term in terms:
        query = [{"Auth_Place_Name": f"=={term}"}]
        records = client.find("Authority_Place", query)
        uuids = set()
        for rec in records:
            fd = rec['fieldData']
            uuid = field_str(fd, 'UUID')
            if uuid:
                auth_cache[uuid] = fd
                uuids.add(uuid)
        if uuids:
            term_candidates[term.lower()] = uuids

    if not term_candidates:
        log.info("  Helper term: no authority records found.")
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
            for i in range(1, len(right_to_left)):
                child_ids = term_candidates.get(right_to_left[i].lower(), set())
                if not child_ids:
                    continue
                _prefetch_missing_parents(child_ids, auth_cache, client)
                verified = {
                    cid for cid in child_ids
                    if walk_up_chain(cid, confirmed, auth_cache, client)
                }
                if verified:
                    confirmed = verified
        candidates = list(confirmed)
    else:
        candidates = list(term_candidates.get(terms[0].lower(), set()))

    if not candidates:
        log.info("  Helper term: chain walk produced no candidates.")
        return None

    if len(candidates) == 1:
        chosen_uuid = candidates[0]
    elif interactive:
        print(f"\n  Helper term '{term_string}' matched {len(candidates)} records:")
        for idx, uuid in enumerate(candidates):
            rec = auth_cache.get(uuid, {})
            name = field_str(rec, 'Auth_Place_Name')
            level = field_str(rec, 'Level')
            jurisdiction = field_str(rec, 'Jurisdiction')
            type_ahead = field_str(rec, 'Type_Ahead_Value')
            print(f"    [{idx + 1}] {name}  level={level}  jurisdiction={jurisdiction}  ({type_ahead})")
        print("    [q] Skip helper term")
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
    else:
        def _pop(uid):
            try:
                return int(field_str(auth_cache.get(uid, {}), 'Population') or 0)
            except (ValueError, TypeError):
                return 0
        chosen_uuid = max(candidates, key=lambda uid: (_pop(uid), uid))
        rec = auth_cache.get(chosen_uuid, {})
        log.info("  Helper term '%s' had %d candidates, auto-picked: %s (%s)",
                 term_string, len(candidates),
                 field_str(rec, 'Auth_Place_Name'),
                 field_str(rec, 'Type_Ahead_Value'))

    chosen_rec = auth_cache.get(chosen_uuid, {})
    try:
        chosen_level = int(field_str(chosen_rec, 'Level'))
    except (ValueError, TypeError):
        chosen_level = 0

    # Walk up parent chain to collect ancestor UUIDs
    ancestor_uuids = set()
    current = chosen_uuid
    for _ in range(20):
        rec = auth_cache.get(current)
        if not rec:
            # Try to fetch it
            query = [{"UUID": f"=={current}"}]
            records = client.find("Authority_Place", query)
            if records:
                fd = records[0]['fieldData']
                uuid = field_str(fd, 'UUID')
                if uuid:
                    auth_cache[uuid] = fd
                    rec = fd
            if not rec:
                break
        parent_uuid = field_str(rec, 'Parent_UUID')
        if not parent_uuid:
            break
        ancestor_uuids.add(parent_uuid)
        current = parent_uuid

    return {
        'uuid': chosen_uuid,
        'level': chosen_level,
        'ancestor_uuids': ancestor_uuids,
    }


def resolve_parent_match(match, terms, auth_cache, client):
    """Decide the fate of a parent_only match: resolve, reject, or amb.

    The rightmost term anchored a parent, but no more-specific term to its left
    verified as a child. resolve_parent_only picks a winner among the parent
    candidates by frequency/population. What happens next turns on whether any
    dropped specific term had authority candidates of its own:

      - Recoverable data present (a specific term matched some authority record
        but failed to chain to this parent): the parent is suspect — we dropped
        a resolvable place. Reject -> parent_rejected.
      - No recoverable data (specifics were pure noise / unmatchable): the parent
        is the best available signal. Keep it -> parent_resolved / freq_resolved.
      - Candidates can't be disambiguated: parent_amb, tie set exposed.

    Skipped terms are carried through unchanged so the dropped specifics stay
    recorded in the output regardless of outcome.
    """
    winner, resolution = resolve_parent_only(
        match.candidate_ids, auth_cache, client, term=terms[-1])

    if resolution in ('parent_resolved', 'freq_resolved'):
        if match.skipped_had_candidates:
            # A resolvable specific was dropped, so the bare parent is suspect.
            # Keep the parent_rejected label but carry the best-guess parent at
            # low confidence rather than discarding it.
            return MatchResult(
                candidate_ids=[winner],
                depth=match.depth,
                match_type='parent_rejected',
                skipped_count=match.skipped_count,
                skipped_terms=match.skipped_terms,
            )
        return MatchResult(
            candidate_ids=[winner],
            depth=match.depth,
            match_type=resolution,
            skipped_count=match.skipped_count,
            skipped_terms=match.skipped_terms,
        )

    # resolution == 'amb'
    return MatchResult(
        candidate_ids=[],
        depth=match.depth,
        match_type='parent_amb',
        skipped_count=match.skipped_count,
        skipped_terms=match.skipped_terms,
        tied_ids=cap_candidates(list(match.candidate_ids), "parent_amb"),
    )


# ---------------------------------------------------------------------------
# Main — orchestrates the three-phase pipeline
# ---------------------------------------------------------------------------

def main(args):
    """Run the full pipeline: Phase 1 name resolution, Phase 2 authority
    record caching, Phase 3 right-to-left matching, then write outputs."""
    start = time.time()
    def elapsed():
        return f"[{time.time() - start:.1f}s]"

    client = None

    if args.local:
        _LOCAL.load(args.mnt, args.pa, dict_source=args.dict, env_path=args.env)
        log.info("Local data ready. %s", elapsed())

        fn_query_mnt = query_mnt_local
        fn_authority_by_name = query_authority_by_name_local
        fn_abbrev = partial(query_abbreviation_expansions_local,
                            jurisdiction_abbreviations=JURISDICTION_ABBREVIATIONS)
        fn_variants = partial(query_name_variants_local,
                              generate_name_variants_fn=_generate_name_variants)
        fn_transforms = partial(query_fallback_transforms_local,
                                transform_term_fn=transform_term)
        fn_preposition = query_preposition_extractions_local
        fn_spelling = query_spelling_corrections_local
        fn_auth_batch = query_authority_batch_local
        fn_prefetch = prefetch_parent_chains_local
        fn_helper = resolve_helper_term_local
        fn_fs = None
    else:
        client = FileMakerClient(args.env)
        log.info("Authenticating with FileMaker...")
        client.auth()
        log.info("Connected. %s", elapsed())

        fn_query_mnt = partial(query_mnt, client)
        fn_authority_by_name = partial(query_authority_by_name, client)
        fn_abbrev = partial(query_abbreviation_expansions, client)
        fn_variants = partial(query_name_variants, client)
        fn_transforms = partial(query_fallback_transforms, client)
        fn_preposition = partial(query_preposition_extractions, client)
        fn_spelling = partial(query_spelling_corrections, client)
        fn_auth_batch = partial(query_authority_batch, client)
        fn_prefetch = partial(prefetch_parent_chains, client)
        fn_helper = partial(resolve_helper_term, client)
        fn_fs = partial(query_fs_places, client)

    entries = load_entries(args.input)
    log.info("Loaded %d entries", len(entries))

    parsed, all_terms, jurisdiction_hints = parse_entries(entries)
    log.info("Unique terms to look up: %d", len(all_terms))

    fs_hits = {}
    if args.local and _LOCAL.fs_by_raw:
        for place, _guid, _freq, _terms in parsed:
            uid = _LOCAL.fs_by_raw.get(canonicalize_place(place))
            if uid:
                fs_hits[place] = uid
        log.info("  Full-string MNT fast path: %d of %d entries pre-resolved",
                 len(fs_hits), len(parsed))

    helper_term_str = args.helper_term or ''

    log.info("\nPhase 1a: MNT lookups %s", elapsed())
    name_cache = fn_query_mnt(all_terms)
    mnt_matched = sum(1 for v in name_cache.values() if v)
    log.info("  %d terms matched via MNT %s", mnt_matched, elapsed())

    log.info("\nPhase 1b: Authority Place lookups by name %s", elapsed())
    fn_authority_by_name(all_terms, name_cache)
    combined = sum(1 for v in name_cache.values() if v)
    log.info("  Combined: %d terms matched %s", combined, elapsed())

    log.info("\nPhase 1b2: Jurisdiction abbreviation expansion %s", elapsed())
    abbrev_added = fn_abbrev(all_terms, name_cache)
    after_abbrev = sum(1 for v in name_cache.values() if v)
    log.info("  +%d UUIDs added from abbreviation expansion "
             "(%d terms matched) %s", abbrev_added, after_abbrev, elapsed())

    log.info("\nPhase 1b3: Name variant expansion %s", elapsed())
    variant_added = fn_variants(all_terms, name_cache)
    after_variants = sum(1 for v in name_cache.values() if v)
    log.info("  +%d UUIDs added from name variants "
             "(%d terms matched) %s", variant_added, after_variants, elapsed())

    log.info("\nPhase 1c: Fallback transforms for unmatched terms %s", elapsed())
    unmatched = [t for t in all_terms if not name_cache.get(t.lower())]
    log.info("  %d terms unmatched, applying transforms...", len(unmatched))
    fn_transforms(unmatched, name_cache)
    after = sum(1 for v in name_cache.values() if v)
    log.info("  After transforms: %d terms matched (+%d new) %s",
             after, after - combined, elapsed())

    transformable_matched = [
        t for t in all_terms
        if name_cache.get(t.lower()) and transform_term(t)[0] is not None
    ]
    if transformable_matched:
        log.info("  Enriching %d MNT-matched transformable terms...",
                 len(transformable_matched))
        enrich_added = fn_transforms(transformable_matched, name_cache)
        log.info("  After enrichment: +%d UUIDs added %s", enrich_added, elapsed())

    # Re-run name variants on transformed forms so that e.g. "near St. Charles"
    # (transformed to "Saint Charles") also tries "St Charles" via PREFIX_SWAPS.
    log.info("\nPhase 1c2: Name variants on transform output %s", elapsed())
    transform_variants = {}
    for t in all_terms:
        cleaned, _ = transform_term(t)
        if cleaned:
            variants = _generate_name_variants(cleaned)
            if variants:
                transform_variants[t] = variants
    if transform_variants:
        tv_added = 0
        for orig_term, variants in transform_variants.items():
            key = orig_term.lower()
            for variant in variants:
                if args.local:
                    uuids = _query_name_local(variant)
                else:
                    query = [{"Auth_Place_Name": f"=={variant}"}]
                    records = client.find("Authority_Place", query, limit=100)
                    uuids = {field_str(r['fieldData'], 'UUID') for r in records
                             if field_str(r['fieldData'], 'UUID')}
                new_uuids = uuids - name_cache.get(key, set())
                if new_uuids:
                    name_cache[key].update(new_uuids)
                    tv_added += len(new_uuids)
        after_tv = sum(1 for v in name_cache.values() if v)
        log.info("  +%d UUIDs from transform variants (%d terms matched) %s",
                 tv_added, after_tv, elapsed())
    else:
        log.info("  No transform variants to try")

    log.info("\nPhase 1c3: Preposition-based extraction for remaining unmatched %s", elapsed())
    still_unmatched = [t for t in all_terms if not name_cache.get(t.lower())]
    log.info("  %d terms still unmatched, scanning for embedded place names...", len(still_unmatched))
    preposition_added = fn_preposition(still_unmatched, name_cache)
    after_preposition = sum(1 for v in name_cache.values() if v)
    log.info("  After preposition extraction: %d terms matched (+%d new) %s",
             after_preposition, after_preposition - after, elapsed())

    log.info("\nPhase 1d: Spelling correction via symspellpy %s", elapsed())
    transform_map = {}
    for t in all_terms:
        cleaned, _ = transform_term(t)
        if cleaned and cleaned.lower() != t.lower():
            transform_map[t] = cleaned
    log.info("  %d terms (%d with transformed forms), building spelling index...",
             len(all_terms), len(transform_map))
    if args.dict:
        sym_spell = build_spelling_index_from_memory()
    else:
        sym_spell = build_spelling_index(args.pa)
    log.info("  Index built: %d entries", len(sym_spell.words))
    spell_terms = [t for t in all_terms if t.lower() not in _LOCAL.illegible]
    if len(spell_terms) < len(all_terms):
        log.info("  %d illegible terms excluded from spelling correction",
                 len(all_terms) - len(spell_terms))
    spelling_added, spelling_corrections = fn_spelling(
        spell_terms, name_cache, sym_spell, transform_map=transform_map)
    after_spelling = sum(1 for v in name_cache.values() if v)
    log.info("  After spelling: %d terms matched (+%d new, %d UUIDs) %s",
             after_spelling, after_spelling - after, spelling_added, elapsed())
    if spelling_corrections:
        log.info("  %d corrections to log", len(spelling_corrections))

    if fn_fs:
        log.info("\nPhase 1e: FamilySearch lookups for unresolved city terms %s", elapsed())
        fn_fs(parsed, name_cache)
        after_fs = sum(1 for v in name_cache.values() if v)
        log.info("  After FS: %d terms matched (+%d new) %s",
                 after_fs, after_fs - after_spelling, elapsed())

    all_auth_ids = set()
    for ids in name_cache.values():
        all_auth_ids.update(ids)
    all_auth_ids.update(fs_hits.values())
    log.info("\n  %d unique authority IDs to resolve", len(all_auth_ids))

    log.info("\nPhase 2: Batch resolve authority records %s", elapsed())
    auth_cache = fn_auth_batch(all_auth_ids)
    log.info("  %d authority records cached %s", len(auth_cache), elapsed())

    log.info("\nPhase 2b: Pre-fetch parent chains %s", elapsed())
    before = len(auth_cache)
    fn_prefetch(auth_cache)
    log.info("  %d parent records added, %d total cached %s",
             len(auth_cache) - before, len(auth_cache), elapsed())

    helper_term = None
    if helper_term_str:
        log.info("\nResolving helper term: '%s' %s", helper_term_str, elapsed())
        helper_term = fn_helper(helper_term_str, auth_cache)
        if helper_term:
            log.info("  Helper term resolved: uuid=%s level=%s ancestors=%d %s",
                     helper_term['uuid'], helper_term['level'],
                     len(helper_term['ancestor_uuids']), elapsed())
        else:
            log.info("  Helper term could not be resolved, proceeding without it.")

    ascii_cache = build_ascii_index(name_cache)
    if ascii_cache:
        log.info("  ASCII fallback index: %d folded entries", len(ascii_cache))

    _run_phase3(args, parsed, name_cache, auth_cache, client, jurisdiction_hints,
                ascii_cache, helper_term, spelling_corrections, start, elapsed,
                fs_hits=fs_hits)


def _run_phase3(args, parsed, name_cache, auth_cache, client, jurisdiction_hints,
                ascii_cache, helper_term, spelling_corrections, start, elapsed,
                fs_hits=None):
    """Phase 3 + output — shared by both local and FM modes."""
    log.info("\nPhase 3: Right-to-left matching (chain walk + skip + rank) %s", elapsed())
    results = []
    ties = []
    recoverable_rejects = 0
    for idx, (place, guid, frequency, terms) in enumerate(parsed):
        fs_uid = (fs_hits or {}).get(place)
        if fs_uid and fs_uid in auth_cache:
            match = MatchResult([fs_uid], depth=len(terms),
                                match_type='mnt_full_string')
        else:
            match = match_entry(terms, name_cache, auth_cache, client, place,
                                jurisdiction_hints=jurisdiction_hints,
                                ascii_cache=ascii_cache,
                                helper_term=helper_term)

        if (match.match_type == 'no_auth_match' and _LOCAL.illegible
                and all(t.lower() in _LOCAL.illegible for t in terms)):
            match = MatchResult(depth=0, match_type='illegible')

        # --- Reversed component fallback ---
        # When RTL produces parent_only (parent matched, children skipped),
        # retry with original (non-reversed) term order. Catches cases like
        # "Italy, Sicily" where the broader term is listed first.
        if match.match_type == 'parent_only' and match.skipped_terms:
            retry = match_entry(list(reversed(terms)), name_cache, auth_cache,
                                client, place,
                                jurisdiction_hints=jurisdiction_hints,
                                ascii_cache=ascii_cache,
                                helper_term=helper_term)
            if retry.match_type in ('chain_verified', 'chain_verified_proximity'):
                match = retry

        if match.match_type == 'parent_only' and match.candidate_ids:
            match = resolve_parent_match(match, terms, auth_cache, client)
            if match.match_type == 'parent_rejected':
                recoverable_rejects += 1

        if match.match_type in ('chain_amb', 'single_amb', 'parent_amb') and match.tied_ids:
            for tid in match.tied_ids:
                rec = auth_cache.get(tid, {})
                ties.append({
                    'original': place,
                    'guid': guid,
                    'frequency': frequency,
                    'match_type': match.match_type,
                    'confidence': match.confidence,
                    'match_depth': match.depth,
                    'authority_id': tid,
                    'authority_name': rec.get('Auth_Place_Name', ''),
                    'type_ahead': rec.get('Type_Ahead_Value', ''),
                    'level': rec.get('Level', ''),
                    'jurisdiction': rec.get('Jurisdiction', ''),
                })

        results.append(build_result_row(match, place, guid, frequency, auth_cache))

        if (idx + 1) % 50 == 0:
            log.info("  Matched %d/%d entries...", idx + 1, len(parsed))

    log.info("  Matched %d/%d entries", len(parsed), len(parsed))
    if recoverable_rejects:
        log.info("  Rejected %d parent matches with recoverable specific terms",
                 recoverable_rejects)

    fm_calls = 0
    output_path, tie_path, spelling_log_path = _resolve_output_paths(
        args.input, args.output_dir)
    write_results(results, output_path)
    if ties:
        write_ties(ties, tie_path)
        log.info("  Wrote %d tied candidate rows to %s", len(ties), tie_path)
    if spelling_corrections:
        write_spelling_log(spelling_corrections, spelling_log_path)
        log.info("  Corrections log: %s", spelling_log_path)
    print_summary(results, fm_calls, time.time() - start, output_path)


def build_cli():
    """Construct the argument parser."""
    parser = argparse.ArgumentParser(
        description="RTL place-name matcher: resolves raw place strings to "
                    "authority records via right-to-left jurisdiction matching.")

    parser.add_argument('--input',
                        help="Input TSV with columns: place, guid, frequency")
    parser.add_argument('--pa',
                        help="Authority Place TSV export")
    parser.add_argument('--mnt',
                        help="Master Normalization Table TSV export")

    parser.add_argument('--output-dir', default='./rtl-outputs',
                        help="Output directory (default: ./rtl-outputs)")
    parser.add_argument('--helper-term',
                        help="Geographic context term for disambiguating "
                             "single-term matches (e.g. 'Utah, USA'). "
                             "Omit to run without one.")

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--local', action='store_true', default=True,
                      help="Use local TSV data (default)")
    mode.add_argument('--api', dest='local', action='store_false',
                      help="Use FileMaker API instead of local TSV data")

    parser.add_argument('--env',
                        help="Path to .env file for FileMaker credentials "
                             "(required with --api) or Supabase password "
                             "(used with --dict live)")
    parser.add_argument('--dict', nargs='?', const='live',
                        help="Supplement MNT with the Supabase place dictionary "
                             "(union). Pass a directory of TSV exports, or no "
                             "value for a live connection (needs SUPABASE_PASSWORD "
                             "via --env or the environment). Local mode only.")
    parser.add_argument('--verbose', '-v', action='store_true',
                        help="Enable debug-level logging")

    return parser


def prompt_missing(args):
    """Interactively prompt for any required paths not given as flags."""
    if not args.input:
        args.input = input("Input TSV path (place, guid, frequency): ").strip().strip("'\"")
    if not args.pa:
        args.pa = input("Authority Place TSV path: ").strip().strip("'\"")
    if not args.mnt:
        args.mnt = input("Master Normalization Table TSV path: ").strip().strip("'\"")
    if not args.output_dir:
        args.output_dir = input("Output directory [./rtl-outputs]: ").strip() or './rtl-outputs'
    if args.helper_term is None:
        ht = input("Helper term (e.g. 'Utah, USA', or blank to skip): ").strip()
        args.helper_term = ht or None
    return args


if __name__ == '__main__':
    parser = build_cli()
    args = parser.parse_args()
    args = prompt_missing(args)

    if not args.local and not args.env:
        parser.error("--env is required when using --api mode")

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format='%(message)s',
        stream=sys.stderr,
    )

    main(args)
