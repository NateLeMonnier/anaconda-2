"""Draw the MNT sampling pool from the FileMaker Data API.

The only network step in the MNT eval build, isolated and cached so
`build_mnt_eval_sample.py` stays pure and testable.

Sampling runs per frequency band rather than uniformly over the table. The
head band (`Total` >= 100,000) is 10,159 of 1,481,622 rows, 0.69%, so filling
a 1,500-row head quota by uniform draw would need roughly 218,000 draws. A
band range query with strided offsets gets there in tens of requests.

Offsets are strided across each band's found set rather than read from the
front. FileMaker returns a found set in internal record order, which tracks
insertion order and therefore source project, so a front-loaded read would
sample one era of curation. Many small pages beat few large ones for the same
reason.

Read-only against FileMaker: POST /sessions and POST /_find, nothing else.
Credentials come from `code/place-normalizer/.env` per
`.claude/commands/fm-query.md`.
"""
import argparse
import base64
import csv
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request

csv.field_size_limit(sys.maxsize)

DEFAULT_ENV = '/Users/natelemonnier/storied/code/place-normalizer/.env'
DEFAULT_OUT = 'eval/data/mnt_pool.tsv'
LAYOUT = 'Master%20Normalization%20Table'

SEED = 42
PAGE = 100

# FileMaker numeric find syntax. Rows with an empty Total match none of these
# and are out of the sampling frame by construction — see module docstring of
# build_mnt_eval_sample.
BANDS = (
    ('head', '>=100000'),
    ('mid', '1000...99999'),
    ('low', '10...999'),
    ('tail', '1...9'),
)

# Roughly 2x the final quota in build_mnt_eval_sample, which is the margin the
# drops there need: non-verified status, labels absent from PA, duplicate
# canonical forms, and the two development exclusion lists.
POOL_TARGETS = {'head': 3000, 'mid': 3000, 'low': 2400, 'tail': 1600}

FIELD_MAP = (
    ('place', 'Input_formatted'),
    ('total', 'Total'),
    ('authority_id', 'Match_Authority_ID'),
    ('authority_name', 'Match_Authority_Name'),
    ('typeahead', 'Typeahead'),
    ('match_status', 'Match_Status'),
    ('multiple_uuid', 'Multiple_UUID_Detected'),
    ('geoclass', 'GeoClass'),
    ('project', 'Project(1)'),
)
POOL_FIELDS = ['band'] + [local for local, _ in FIELD_MAP]


def load_env(path):
    """Read KEY=VALUE lines into the environment without clobbering it."""
    if not os.path.exists(path):
        return
    with open(path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, _, val = line.partition('=')
                os.environ.setdefault(key.strip(),
                                      val.strip().strip('"').strip("'"))


# ---------------------------------------------------------------------------
# Pure planning and shaping
# ---------------------------------------------------------------------------

def page_offsets(found_count, want, page, seed=SEED):
    """One-based, non-overlapping offsets striding across a found set.

    Returns enough pages to cover `want` records. When the band is smaller
    than the request the whole band is walked from the front, because there is
    nothing to stride over. Otherwise each page sits at a stride boundary
    plus a seeded jitter, so repeat runs of the same seed draw the same rows
    while different bands do not all sample the same relative positions.
    """
    if found_count <= 0 or want <= 0 or page <= 0:
        return []
    if want >= found_count:
        return list(range(1, found_count + 1, page))

    pages = -(-want // page)
    stride = found_count // pages
    if stride < page:
        # Requested share is dense enough that strided pages would overlap.
        return list(range(1, pages * page + 1, page))

    rng = random.Random(f'{seed}:offsets:{found_count}:{want}:{page}')
    slack = stride - page
    offsets = []
    for i in range(pages):
        jitter = rng.randint(0, slack) if slack > 0 else 0
        start = 1 + i * stride + jitter
        offsets.append(min(start, found_count - page + 1))
    return offsets


def record_to_row(field_data, band):
    """Flatten one Data API record onto POOL_FIELDS."""
    row = {'band': band}
    for local, remote in FIELD_MAP:
        value = field_data.get(remote, '')
        row[local] = '' if value is None else str(value).strip()
    return row


def dedupe_rows(rows):
    """Drop repeat place strings, keeping first seen.

    Strided pages cannot overlap, but a band re-run or a widened target can
    revisit a record, and the pool is a cache that gets appended to across
    invocations.
    """
    seen = set()
    out = []
    for row in rows:
        key = row['place'].lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def write_pool(path, rows, seed):
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    with open(path, 'w', encoding='utf-8', newline='') as f:
        f.write(f'# seed={seed} source=FileMaker/{LAYOUT} '
                f'generated_from=build_mnt_pool.py\n')
        w = csv.DictWriter(f, fieldnames=POOL_FIELDS, delimiter='\t',
                           extrasaction='ignore')
        w.writeheader()
        w.writerows(rows)


def read_pool(path):
    with open(path, encoding='utf-8', newline='') as f:
        lines = [ln for ln in f if not ln.startswith('#')]
    return list(csv.DictReader(lines, delimiter='\t'))


def population_path(pool_path):
    """Sidecar holding each band's exact string count."""
    stem, _ = os.path.splitext(pool_path)
    return f'{stem}_population.json'


def write_population(pool_path, populations):
    """Persist the per-band found counts alongside the pool.

    build_mnt_eval_sample reweights band accuracy by record share, and the
    string count is the exact half of that estimate. Recording it here keeps
    it out of the sample builder as a hardcoded number that would rot as the
    MNT grows.
    """
    path = population_path(pool_path)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump({'source': f'FileMaker/{LAYOUT}',
                   'strings': populations}, f, indent=2)
    return path


def read_population(pool_path):
    with open(population_path(pool_path), encoding='utf-8') as f:
        return json.load(f)['strings']


# ---------------------------------------------------------------------------
# FileMaker Data API
# ---------------------------------------------------------------------------

class FileMaker:
    """Read-only Data API client. Opens a session, never closes one.

    FileMaker expires sessions on its own and the query runbook forbids the
    DELETE, so there is no teardown here.
    """

    def __init__(self, host, database, token, opener=None, sleeper=time.sleep):
        self.host = host.rstrip('/')
        self.database = database
        self.token = token
        self._opener = opener or urllib.request.urlopen
        self._sleep = sleeper

    @classmethod
    def connect(cls, host, database, username, password, opener=None):
        url = (f'{host.rstrip("/")}/fmi/data/v1/databases/{database}/sessions')
        basic = base64.b64encode(
            f'{username}:{password}'.encode()).decode('ascii')
        req = urllib.request.Request(
            url, data=b'{}',
            headers={'Authorization': f'Basic {basic}',
                     'Content-Type': 'application/json'})
        body = json.load((opener or urllib.request.urlopen)(req))
        return cls(host, database, body['response']['token'], opener=opener)

    def _post(self, path, payload, attempts=4, base=2.0):
        url = f'{self.host}/fmi/data/v2/databases/{self.database}{path}'
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode('utf-8'),
            headers={'Authorization': f'Bearer {self.token}',
                     'Content-Type': 'application/json'})
        for attempt in range(attempts):
            try:
                return json.load(self._opener(req))
            except urllib.error.HTTPError as err:
                # 4xx is the server answering, not failing. A no-match find
                # arrives as 404 and the caller turns it into an empty result;
                # retrying it would just burn the attempt budget.
                if err.code < 500 or attempt == attempts - 1:
                    raise
                self._sleep(base * (2 ** attempt))
            except (urllib.error.URLError, OSError):
                if attempt == attempts - 1:
                    raise
                self._sleep(base * (2 ** attempt))
        raise AssertionError('unreachable')

    def find(self, query, limit=1, offset=1):
        """Run a find. Returns (records, found_count).

        A find matching nothing answers HTTP 404 with FileMaker code 401,
        which is a legitimate empty result rather than an error.
        """
        payload = {'query': query, 'limit': limit, 'offset': offset}
        try:
            body = self._post(f'/layouts/{LAYOUT}/_find', payload)
        except urllib.error.HTTPError as err:
            if err.code == 404:
                return [], 0
            raise
        response = body.get('response', {})
        found = response.get('dataInfo', {}).get('foundCount', 0)
        return response.get('data', []), found


def draw_band(client, band, total_range, want, page, seed, log=print):
    """Pull `want` records from one frequency band."""
    _, found = client.find([{'Total': total_range}], limit=1)
    offsets = page_offsets(found, want, page, seed)
    rows = []
    for offset in offsets:
        records, _ = client.find([{'Total': total_range}],
                                 limit=page, offset=offset)
        rows.extend(record_to_row(r.get('fieldData', {}), band)
                    for r in records)
    rows = dedupe_rows(rows)[:want]
    log(f'{band:5s} population={found:>9,} drew={len(rows):>5,} '
        f'pages={len(offsets)}')
    return rows, found


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--out', default=DEFAULT_OUT)
    p.add_argument('--env', default=DEFAULT_ENV)
    p.add_argument('--seed', type=int, default=SEED)
    p.add_argument('--page', type=int, default=PAGE)
    p.add_argument('--force', action='store_true',
                   help='redraw even when the cached pool exists')
    args = p.parse_args(argv)

    if os.path.exists(args.out) and not args.force:
        rows = read_pool(args.out)
        print(f'{args.out} already holds {len(rows)} rows; '
              f'pass --force to redraw')
        return 0

    load_env(args.env)
    missing = [k for k in ('FILEMAKER_HOST', 'FILEMAKER_DATABASE',
                           'FILEMAKER_USERNAME', 'FILEMAKER_PASSWORD')
               if not os.environ.get(k)]
    if missing:
        print(f'missing credentials: {", ".join(missing)}', file=sys.stderr)
        return 1

    client = FileMaker.connect(os.environ['FILEMAKER_HOST'],
                               os.environ['FILEMAKER_DATABASE'],
                               os.environ['FILEMAKER_USERNAME'],
                               os.environ['FILEMAKER_PASSWORD'])
    started = time.time()
    rows = []
    populations = {}
    for band, total_range in BANDS:
        drawn, found = draw_band(client, band, total_range,
                                 POOL_TARGETS[band], args.page, args.seed)
        rows.extend(drawn)
        populations[band] = found

    write_pool(args.out, rows, args.seed)
    write_population(args.out, populations)
    print(f'wrote {len(rows)} rows to {args.out} in {time.time() - started:.0f}s')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
