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

import base64
import csv
import json
import os
import re
import socket
import ssl
import sys
import time
import urllib.request
import urllib.error
from collections import defaultdict
from dataclasses import dataclass, field

INPUT = os.environ.get('RTL_INPUT', os.path.expanduser("~/storied/resources/SnowballLocationsSampled/locations_sample_5k.tsv"))
ENV = os.path.expanduser("~/storied/code/place-normalizer/.env")

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_RTL_OUTPUTS_DIR = os.path.join(_SCRIPT_DIR, 'rtl-outputs')

# FileMaker Data API accepts multiple query objects per request with no
# documented cap, so we batch aggressively to minimize round trips.
BATCH = 1000

FS_BASE = "https://api-integ.familysearch.org/platform/places/search"
FS_TYPE_CITY = "186"

OUTPUT_FIELDS = [
    'original', 'guid', 'frequency', 'match_type', 'match_depth',
    'candidates', 'authority_name', 'type_ahead', 'jurisdiction',
    'level', 'authority_id', 'skipped_count', 'skipped_terms',
]

TIE_OUTPUT_FIELDS = [
    'original', 'guid', 'frequency', 'match_type', 'match_depth',
    'authority_id', 'authority_name', 'type_ahead', 'level', 'jurisdiction',
]


def field_str(field_data, key):
    """Safely extract a string field from a FileMaker record, returning '' if null."""
    val = field_data.get(key)
    if val is None:
        return ''
    return str(val).strip()


# ---------------------------------------------------------------------------
# FileMaker client
#
# Wraps the FileMaker Data API (v2) with session management and automatic
# re-authentication on token expiry. The find() method accepts a list of
# query objects, where each object is an OR condition and fields within an
# object are AND conditions — matching FileMaker's native find semantics.
# ---------------------------------------------------------------------------


class FileMakerClient:
    def __init__(self, env_path=ENV):
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
                print(f"  Connection error ({e}), re-authing and retrying...", file=sys.stderr)
                self.auth()
                return self.find(layout, query, limit, _retry=True)
            print(f"  Connection error on retry ({e}), skipping batch", file=sys.stderr)
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
            print(f"  FM error {e.code}: {body[:200]}", file=sys.stderr)
            return []

    def batch_find(self, layout, keys, key_field, extract_fn, label=""):
        """Run batched _find requests for a list of keys against a single field."""
        key_list = list(keys)
        total = len(key_list)
        for i in range(0, total, BATCH):
            batch = key_list[i:i + BATCH]
            query = [{key_field: f"=={k}"} for k in batch]
            records = self.find(layout, query)
            for rec in records:
                extract_fn(rec['fieldData'])
            if label:
                done = min(i + BATCH, total)
                print(f"  {label}: {done}/{total}")


# ---------------------------------------------------------------------------
# UUID validation
# ---------------------------------------------------------------------------

UUID_RE = re.compile(
    r'^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-'
    r'[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$')


def is_valid_uuid(value):
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
]

TRAILING_DESCRIPTORS = [
    re.compile(r'\s+(?:area|district|community|region)$', re.I),
]

ABBREVIATION_EXPANSIONS = [
    (re.compile(r'^St\.\s*', re.I), 'Saint '),
    (re.compile(r'^Ft\.\s*', re.I), 'Fort '),
    (re.compile(r'^Mt\.\s*', re.I), 'Mount '),
]


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

    for pattern in TRAILING_DESCRIPTORS:
        cleaned = pattern.sub('', cleaned).strip()

    # Only one jurisdiction suffix should match; "Washington County Township"
    # is not a real pattern, so we break on first hit.
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

    if not cleaned or cleaned.lower() == term.lower():
        return None, None

    return cleaned, jurisdiction


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
        print(f"  MNT: {done}/{total} terms, "
              f"{len(name_cache)} matched, {junk_count} junk filtered")
        sys.stdout.flush()

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
            print(f"  MNT whitespace rescan: {rescan_matched} additional terms matched")

    print()
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
        print(f"  Authority by name: {done}/{total} terms, {added} new UUIDs")
    print()
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
        print("  No transformable terms")
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

            records = client.find("Authority_Place", query)
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
            print(f"  Fallback (jurisdiction): {done}/{len(jurisdiction_terms)} terms, {added} UUIDs")
            print()

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
            mnt_records = client.find("Master%20Normalization%20Table", mnt_query)
            for rec in mnt_records:
                field_data = rec['fieldData']
                input_original = field_str(field_data, 'Input_Original')
                authority_id = field_str(field_data, 'Match_Authority_ID')
                if input_original and authority_id and is_valid_uuid(authority_id) and input_original.lower() in lookup:
                    for orig in lookup[input_original.lower()]:
                        name_cache[orig.lower()].add(authority_id)
                        non_jurisdiction_added += 1

            authority_query = [{"Auth_Place_Name": f"=={cleaned}"} for cleaned in cleaned_list]
            authority_records = client.find("Authority_Place", authority_query)
            for rec in authority_records:
                field_data = rec['fieldData']
                name = field_str(field_data, 'Auth_Place_Name')
                uuid = field_str(field_data, 'UUID')
                if uuid and name and name.lower() in lookup:
                    for orig in lookup[name.lower()]:
                        name_cache[orig.lower()].add(uuid)
                        non_jurisdiction_added += 1

            done = min(i + BATCH, len(non_jurisdiction_terms))
            print(f"  Fallback (other): {done}/{len(non_jurisdiction_terms)} terms, "
                  f"{non_jurisdiction_added} UUIDs")
            print()

    return added + non_jurisdiction_added


# ---------------------------------------------------------------------------
# Phase 1d: FamilySearch city resolution
#
# For entries where a city-level term went unresolved through 1a-1c but a
# right-side jurisdiction term (state/country) IS resolved, query the
# FamilySearch Places API to find the canonical city name, then retry
# Authority_Place with that name. This bridges spelling variants and
# historical names that FM knows under a different string.
# ---------------------------------------------------------------------------


def _fs_request(url):
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status == 204:
                return None
            return json.loads(resp.read())
    except Exception:
        return None


def _resolve_fs_id(term, fs_id_cache):
    key = term.lower()
    if key in fs_id_cache:
        return fs_id_cache[key]

    q = urllib.parse.quote(f"name:{term}", safe=':+~')
    data = _fs_request(f"{FS_BASE}?q={q}&count=5")
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
    encoded_city = urllib.parse.quote(city_term)
    q = f"name:{encoded_city}+parentId:{parent_fs_id}~"
    data = _fs_request(f"{FS_BASE}?q={q}&count=10")
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


def query_fs_places(client, parsed, name_cache):
    """Phase 1d: use FamilySearch to resolve city terms that failed 1a-1c.

    Walks parsed entries to find (unresolved_city, jurisdiction) pairs,
    deduplicates them, queries FS for the canonical city name, and retries
    Authority_Place with the result.
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
        print("  No eligible (city, jurisdiction) pairs")
        return 0

    unique_pairs = list(pairs.values())
    print(f"  {len(unique_pairs)} unique (city, jurisdiction) pairs to resolve...")

    fs_id_cache = {}
    fs_hits = 0
    fm_added = 0

    for city_term, jurisdiction_term in unique_pairs:
        parent_id = _resolve_fs_id(jurisdiction_term, fs_id_cache)
        if not parent_id:
            continue

        canonical = _fs_city_lookup(city_term, parent_id)
        if not canonical:
            continue
        fs_hits += 1

        query = [{"Auth_Place_Name": f"=={canonical}"}]
        records = client.find("Authority_Place", query)
        for rec in records:
            fd = rec['fieldData']
            uuid = field_str(fd, 'UUID')
            if uuid:
                name_cache[city_term.lower()].add(uuid)
                fm_added += 1

    print(f"  {fs_hits} FS hits -> {fm_added} new authority records added")
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
        print(f"  Authority: {done}/{total} UUIDs resolved, {len(auth_cache)} found")
    print()
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
            print(f"  Parent pre-fetch: {done}/{total} UUIDs, {fetched} found")
        print()
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
    if not missing:
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


# --- BEGIN RTL-LEVEL-PREF ---

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


def _disambiguate_by_population(candidates, auth_cache):
    """Apply Leafprint population rules to a set of same-tier candidates.
    Returns (winner_uuid, 'parent_resolved') or (None, 'amb')."""
    if len(candidates) == 1:
        return (candidates[0], 'parent_resolved')

    pops = [(uid, get_population(auth_cache.get(uid, {}))) for uid in candidates]
    pops.sort(key=lambda x: x[1], reverse=True)

    if all(p == 0 for _, p in pops):
        return (None, 'amb')

    first_uid, first_pop = pops[0]
    _second_uid, second_pop = pops[1]

    if first_pop >= 50_000 and second_pop == 0:
        return (first_uid, 'parent_resolved')

    if first_pop >= 50_000 and second_pop >= 50_000:
        return (None, 'amb')

    if second_pop > 0 and first_pop > 5 * second_pop:
        return (first_uid, 'parent_resolved')

    return (None, 'amb')


def resolve_parent_only(candidate_ids, auth_cache, client):
    """Disambiguate candidates using jurisdiction level and population.

    Partitions candidates into low-level (<=4) and high-level (>=5), then
    applies population-based tiebreaking rules drawn from Leafprint's
    place authority guidelines.

    Returns (winner_uuid, 'parent_resolved') or (None, 'amb').
    """
    if not candidate_ids:
        return (None, 'amb')
    if len(candidate_ids) == 1:
        return (candidate_ids[0], 'parent_resolved')

    # Fetch any missing auth records from FM
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

    # Partition into low (level <= 4) and high (level >= 5)
    low = []
    high = []
    for uid in candidate_ids:
        rec = auth_cache.get(uid, {})
        try:
            level = int(field_str(rec, 'Level'))
        except (ValueError, TypeError):
            level = 0
        if level >= 5:
            high.append(uid)
        else:
            low.append(uid)

    # No low-level candidates -> disambiguate among high
    if not low:
        return _disambiguate_by_population(high, auth_cache)

    # Sort low candidates by population descending
    low_pops = [(uid, get_population(auth_cache.get(uid, {}))) for uid in low]
    low_pops.sort(key=lambda x: x[1], reverse=True)

    all_low_zero = all(p == 0 for _, p in low_pops)

    # 3a: all low pops zero AND high exists
    if all_low_zero and high:
        return _disambiguate_by_population(high, auth_cache)

    # 3b: all low pops zero AND no high
    if all_low_zero and not high:
        return (None, 'amb')

    first_uid, first_pop = low_pops[0]
    second_pop = low_pops[1][1] if len(low_pops) > 1 else 0

    # 3c: highest low >= 50k AND second low == 0
    if first_pop >= 50_000 and second_pop == 0:
        return (first_uid, 'parent_resolved')

    # 3d: highest low < 50k AND high exists
    if first_pop < 50_000 and high:
        return _disambiguate_by_population(high, auth_cache)

    # 3e: highest low > 5x second low (second > 0)
    if second_pop > 0 and first_pop > 5 * second_pop:
        return (first_uid, 'parent_resolved')

    # 3f: otherwise AND high exists
    if high:
        return _disambiguate_by_population(high, auth_cache)

    # 3g: otherwise AND no high
    return (None, 'amb')

# --- END RTL-LEVEL-PREF ---


def _get_parent_level(confirmed_set, auth_cache):
    """Extract the jurisdiction Level from the first candidate with a valid level."""
    for uid in confirmed_set:
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


def rank_candidates(candidates, auth_cache, parent_level, jurisdiction_hint=None):
    """Rank candidates by level gap from parent anchor, then population.

    Returns list of (uuid, score) tuples sorted best-first.
    score is (level_gap, neg_population) — lower is better on both axes.
    When parent_level is None (single_term case), ranks by population only.
    """
    if not candidates:
        return []

    if jurisdiction_hint is None:
        preferred = [c for c in candidates
                     if field_str(auth_cache.get(c, {}), 'Jurisdiction') in PREFERRED_JURISDICTIONS]
        if preferred:
            candidates = [c for c in candidates
                          if field_str(auth_cache.get(c, {}), 'Jurisdiction') not in FILTERED_JURISDICTIONS]

    def score(uuid):
        rec = auth_cache.get(uuid, {})
        pop = get_population(rec)
        if parent_level is None:
            return (0, -pop)
        try:
            level = int(field_str(rec, 'Level'))
        except (ValueError, TypeError):
            level = 0
        gap = abs(parent_level - level)
        return (gap, -pop)

    scored = [(uuid, score(uuid)) for uuid in candidates]
    scored.sort(key=lambda x: x[1])
    return scored


def detect_tie(ranked_with_scores):
    """Check if top candidates in a ranked list share the same score.

    Returns (winner_uuid_or_None, tied_uuids).
    If tied: winner is None, tied_uuids contains all candidates sharing the top score.
    If not tied: winner is the top candidate, tied_uuids is empty.
    """
    if not ranked_with_scores:
        return (None, [])
    if len(ranked_with_scores) == 1:
        return (ranked_with_scores[0][0], [])

    top_score = ranked_with_scores[0][1]
    tied = [uuid for uuid, s in ranked_with_scores if s == top_score]

    if len(tied) > 1:
        return (None, tied)
    return (ranked_with_scores[0][0], [])


@dataclass
class MatchResult:
    candidate_ids: list = field(default_factory=list)
    depth: int = 0
    match_type: str = 'no_terms'
    skipped_count: int = 0
    skipped_terms: str = ''
    tied_ids: list = field(default_factory=list)


def match_entry(terms, name_cache, auth_cache, client, original):
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

    parent_ids = name_cache.get(right_to_left[0].lower(), set())
    if not parent_ids:
        return MatchResult(match_type='no_auth_match')

    if len(right_to_left) == 1:
        ranked = rank_candidates(list(parent_ids), auth_cache, None)
        if len(ranked) == 1:
            return MatchResult([ranked[0][0]], depth=1, match_type='single_term')
        all_ids = [uuid for uuid, _ in ranked]
        return MatchResult([], depth=1, match_type='single_amb', tied_ids=all_ids)

    confirmed = parent_ids
    depth = 1
    skipped = []
    parent_level_for_ranking = None

    for i in range(1, len(right_to_left)):
        child_ids = name_cache.get(right_to_left[i].lower(), set())
        if not child_ids:
            skipped.append(right_to_left[i])
            continue

        if len(child_ids) > 50:
            print(f"    term '{right_to_left[i]}': {len(child_ids)} candidates, prefetching...", flush=True)
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

    skip_count = len(skipped)
    skip_str = '; '.join(skipped)

    if depth > 1:
        ranked = rank_candidates(list(confirmed), auth_cache, parent_level_for_ranking)
        winner, tied = detect_tie(ranked)
        if tied:
            return MatchResult([], depth, 'chain_amb', skip_count, skip_str, tied)
        ids = [winner] if winner else []
        return MatchResult(ids, depth, 'chain_verified', skip_count, skip_str)

    # parent_only: pass UUIDs through for resolve_parent_only in main()
    ranked = rank_candidates(list(confirmed), auth_cache, None)
    ids = [uuid for uuid, _ in ranked]
    return MatchResult(ids, depth, 'parent_only', skip_count, skip_str)


# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------

def load_entries(path):
    """Read the input TSV, expecting columns: place, guid, frequency."""
    with open(path, 'r', encoding='utf-8-sig') as f:
        return list(csv.DictReader(f, delimiter='\t'))


def parse_entries(entries):
    """Split each entry's place string into comma/semicolon-separated terms
    and collect the full set of unique terms across all entries for bulk lookup.
    Also detects jurisdiction hints (County, Township, etc.) for each term.
    """
    parsed = []
    all_terms = set()
    jurisdiction_hints = {}
    for entry in entries:
        terms = [t.strip() for t in re.split(r'[,;]', entry['place']) if t.strip()]
        parsed.append((entry['place'], entry['guid'], entry['frequency'], terms))
        all_terms.update(terms)
        for term in terms:
            hint = detect_jurisdiction_hint(term)
            if hint:
                jurisdiction_hints[term.lower()] = hint
    return parsed, all_terms, jurisdiction_hints


def _resolve_output_paths(input_path):
    """Build date-sorted, auto-numbered output paths inside rtl-outputs/.

    Pattern: rtl-outputs/MM-DD/<input_stem>_NN.tsv
    where NN increments per input name per day.
    """
    from datetime import datetime
    stem = os.path.splitext(os.path.basename(input_path))[0]
    day_dir = os.path.join(_RTL_OUTPUTS_DIR, datetime.now().strftime('%m-%d'))
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
    return output, tie_output


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
    for match_type in ['chain_verified', 'chain_amb', 'single_term', 'single_amb',
                       'parent_resolved', 'parent_only', 'parent_amb',
                       'no_auth_match', 'no_terms']:
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
# Main — orchestrates the three-phase pipeline
# ---------------------------------------------------------------------------

def main():
    start = time.time()
    def elapsed():
        return f"[{time.time() - start:.1f}s]"

    client = FileMakerClient()
    print("Authenticating with FileMaker...")
    client.auth()
    print(f"Connected. {elapsed()}")

    entries = load_entries(INPUT)
    print(f"Loaded {len(entries)} entries")

    parsed, all_terms, jurisdiction_hints = parse_entries(entries)
    print(f"Unique terms to look up: {len(all_terms)}")

    # Phase 1a: Check the Master Normalization Table for known mappings
    print(f"\nPhase 1a: MNT lookups (filtering junk IDs) {elapsed()}")
    name_cache = query_mnt(client, all_terms)
    mnt_matched = sum(1 for v in name_cache.values() if v)
    print(f"  {mnt_matched} terms matched via MNT {elapsed()}")

    # Phase 1b: Direct name match against Authority_Place for anything MNT missed
    print(f"\nPhase 1b: Authority Place lookups by name {elapsed()}")
    query_authority_by_name(client, all_terms, name_cache)
    combined = sum(1 for v in name_cache.values() if v)
    print(f"  Combined: {combined} terms matched {elapsed()}")

    # Phase 1c: Transform unmatched terms and retry both sources
    print(f"\nPhase 1c: Fallback transforms for unmatched terms {elapsed()}")
    unmatched = [t for t in all_terms if not name_cache.get(t.lower())]
    print(f"  {len(unmatched)} terms unmatched, applying transforms...")
    query_fallback_transforms(client, unmatched, name_cache)
    after = sum(1 for v in name_cache.values() if v)
    print(f"  After transforms: {after} terms matched (+{after - combined} new) {elapsed()}")

    # Phase 1d: FamilySearch city resolution for remaining unmatched city terms
    print(f"\nPhase 1d: FamilySearch lookups for unresolved city terms {elapsed()}")
    query_fs_places(client, parsed, name_cache)
    after_fs = sum(1 for v in name_cache.values() if v)
    print(f"  After FS: {after_fs} terms matched (+{after_fs - after} new) {elapsed()}")

    # Phase 2: Fetch full authority records for every UUID found in Phase 1
    all_auth_ids = set()
    for ids in name_cache.values():
        all_auth_ids.update(ids)
    print(f"\n  {len(all_auth_ids)} unique authority IDs to resolve")

    print(f"\nPhase 2: Batch resolve authority records {elapsed()}")
    auth_cache = query_authority_batch(client, all_auth_ids)
    print(f"  {len(auth_cache)} authority records cached {elapsed()}")

    # Phase 2b: Walk up the hierarchy to cache all ancestor records,
    # so Phase 3 can verify parent chains without per-entry API calls
    print(f"\nPhase 2b: Pre-fetch parent chains {elapsed()}")
    before = len(auth_cache)
    prefetch_parent_chains(client, auth_cache)
    print(f"  {len(auth_cache) - before} parent records added, "
          f"{len(auth_cache)} total cached {elapsed()}")

    # Phase 3: Run right-to-left matching on each entry
    print(f"\nPhase 3: Right-to-left matching (chain walk + skip + rank) {elapsed()}")
    results = []
    ties = []
    for idx, (place, guid, frequency, terms) in enumerate(parsed):
        match = match_entry(terms, name_cache, auth_cache, client, place)

        # --- BEGIN RTL-LEVEL-PREF ---
        if match.match_type == 'parent_only' and match.candidate_ids:
            winner, resolution = resolve_parent_only(
                match.candidate_ids, auth_cache, client)
            if resolution == 'parent_resolved':
                match = MatchResult(
                    candidate_ids=[winner],
                    depth=match.depth,
                    match_type='parent_resolved',
                    skipped_count=match.skipped_count,
                    skipped_terms=match.skipped_terms,
                )
            elif resolution == 'amb':
                match = MatchResult(
                    candidate_ids=[],
                    depth=match.depth,
                    match_type='parent_amb',
                    skipped_count=match.skipped_count,
                    skipped_terms=match.skipped_terms,
                )
        # --- END RTL-LEVEL-PREF ---

        if match.match_type in ('chain_amb', 'single_amb') and match.tied_ids:
            for tid in match.tied_ids:
                rec = auth_cache.get(tid, {})
                ties.append({
                    'original': place,
                    'guid': guid,
                    'frequency': frequency,
                    'match_type': match.match_type,
                    'match_depth': match.depth,
                    'authority_id': tid,
                    'authority_name': rec.get('Auth_Place_Name', ''),
                    'type_ahead': rec.get('Type_Ahead_Value', ''),
                    'level': rec.get('Level', ''),
                    'jurisdiction': rec.get('Jurisdiction', ''),
                })

        row = {
            'original': place,
            'guid': guid,
            'frequency': frequency,
            'match_type': match.match_type,
            'match_depth': match.depth,
            'candidates': len(match.candidate_ids),
            'authority_name': '',
            'type_ahead': '',
            'jurisdiction': '',
            'level': '',
            'authority_id': '',
            'skipped_count': match.skipped_count,
            'skipped_terms': match.skipped_terms,
        }

        # Populate output fields from the top-ranked candidate
        if match.candidate_ids:
            best_id = match.candidate_ids[0]
            best_record = auth_cache.get(best_id, {})
            row['authority_name'] = best_record.get('Auth_Place_Name', '')
            row['type_ahead'] = best_record.get('Type_Ahead_Value', '')
            row['jurisdiction'] = best_record.get('Jurisdiction', '')
            row['level'] = best_record.get('Level', '')
            row['authority_id'] = best_id

        results.append(row)

        if (idx + 1) % 50 == 0:
            print(f"  Matched {idx+1}/{len(parsed)} entries...")

    print(f"  Matched {len(parsed)}/{len(parsed)} entries")

    output_path, tie_path = _resolve_output_paths(INPUT)
    write_results(results, output_path)
    if ties:
        write_ties(ties, tie_path)
        print(f"  Wrote {len(ties)} tied candidate rows to {tie_path}")
    print_summary(results, client.call_count, time.time() - start, output_path)


if __name__ == '__main__':
    main()
