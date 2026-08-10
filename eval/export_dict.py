"""Export Storied's place dictionary to the TSV pair `--dict` reads.

One network step, cached, so the eval can run offline and reproducibly. The
matcher's own `--dict live` path pulls the same two tables straight into
memory; this writes them to disk instead, which is what lets
`build_mnt_holdout.py` filter the dictionary before the matcher sees it.

Loading the dictionary is not optional for a representative run. Its per-term
frequency prior is the only thing feeding `_disambiguate_by_frequency`
(`rtl_matcher.py:1974`), and without it every ambiguous candidate set falls
through unresolved — a 2,500-row eval run produced zero `freq_resolved` rows
against 457 in a 5,000-row production run. The illegible stop-list matters
twice more: it keeps junk terms out of spelling correction
(`rtl_matcher.py:3254`) and turns all-junk rows into a clean `illegible`
abstention (`rtl_matcher.py:3345`).

Read-only. Two SELECTs, no writes.
"""
import argparse
import csv
import os
import sys

from build_mnt_pool import load_env

csv.field_size_limit(sys.maxsize)

DEFAULT_ENV = '/Users/natelemonnier/storied/code/anaconda-2/.env'
DEFAULT_OUT_DIR = 'eval/data/dict'

DICT_FILE = 'place_term_dictionary.tsv'
ILLEGIBLE_FILE = 'place_term_illegible.tsv'

# Connection parameters as used by LocalData._load_dict_live.
HOST = 'aws-1-us-west-1.pooler.supabase.com'
PORT = 5432
DBNAME = 'postgres'
USER = 'parser_readonly.ncahtzbmazzqrorjkjwm'

DICT_QUERY = 'SELECT term, authority_uuid, frequency FROM place_term_dictionary'
ILLEGIBLE_QUERY = 'SELECT term FROM place_term_illegible'


def write_dict_tsv(path, rows):
    """Write (term, authority_uuid, frequency) triples in `--dict` shape."""
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    with open(path, 'w', encoding='utf-8', newline='') as f:
        w = csv.writer(f, delimiter='\t')
        w.writerow(['term', 'authority_uuid', 'frequency'])
        n = 0
        for term, uuid, freq in rows:
            term = (term or '').strip()
            uuid = str(uuid or '').strip().upper()
            if not term or not uuid:
                continue
            w.writerow([term, uuid, int(freq or 0)])
            n += 1
    return n


def write_illegible_tsv(path, terms):
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    with open(path, 'w', encoding='utf-8', newline='') as f:
        w = csv.writer(f, delimiter='\t')
        w.writerow(['term'])
        n = 0
        for row in terms:
            term = (row[0] if isinstance(row, (tuple, list)) else row) or ''
            term = term.strip()
            if not term:
                continue
            w.writerow([term])
            n += 1
    return n


def export(out_dir, cursor):
    """Run both queries and write both files. Returns (dict_rows, ill_rows)."""
    cursor.execute(DICT_QUERY)
    n_dict = write_dict_tsv(os.path.join(out_dir, DICT_FILE), cursor)
    cursor.execute(ILLEGIBLE_QUERY)
    n_ill = write_illegible_tsv(os.path.join(out_dir, ILLEGIBLE_FILE), cursor)
    return n_dict, n_ill


def cached(out_dir):
    return all(os.path.exists(os.path.join(out_dir, f))
               for f in (DICT_FILE, ILLEGIBLE_FILE))


def connect(password):
    import psycopg2
    return psycopg2.connect(host=HOST, port=PORT, dbname=DBNAME,
                            user=USER, password=password)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--out-dir', default=DEFAULT_OUT_DIR)
    p.add_argument('--env', default=DEFAULT_ENV)
    p.add_argument('--force', action='store_true',
                   help='re-pull even when the cached export exists')
    args = p.parse_args(argv)

    if cached(args.out_dir) and not args.force:
        print(f'{args.out_dir} already holds both files; pass --force to re-pull')
        return 0

    load_env(args.env)
    password = os.environ.get('SUPABASE_PASSWORD')
    if not password:
        print('SUPABASE_PASSWORD not set; pass --env or export it',
              file=sys.stderr)
        return 1

    conn = connect(password)
    try:
        cur = conn.cursor()
        n_dict, n_ill = export(args.out_dir, cur)
        cur.close()
    finally:
        conn.close()
    print(f'{args.out_dir}: {n_dict} dictionary terms, {n_ill} illegible terms')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
