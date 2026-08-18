#!/bin/bash
# Build a scratch working copy of code/place-normalizer with the two hardcoded
# authority paths swapped for the decontaminated ones.
#
# The old pipeline reads utils/Master_Normalization_File.tsv
# (master_normalizer_parallel.py:541) and utils/PA.tsv
# (place_authority_normalizer_parallel.py:278) as literals. Pointing it at the
# repo's own copies would let it answer eval rows out of the table it is being
# measured against, which is the same contamination build_mnt_holdout.py exists
# to prevent on the new matcher's side. Everything except those two files is a
# symlink, so nothing in the repo is written to.
#
# Usage: mirror_oldpipe.sh <work-dir> [holdout-mnt] [pa-export]
set -euo pipefail

WORK=${1:?usage: mirror_oldpipe.sh <work-dir> [holdout-mnt] [pa-export]}
HOLDOUT=${2:-/Users/natelemonnier/storied/code/anaconda-2/eval/data/mnt_holdout.tsv}
PA=${3:-/Users/natelemonnier/storied/resources/place-authority-mnt-tsv/PA6_16_2026v77.tsv}
SRC=/Users/natelemonnier/storied/code/place-normalizer

for f in "$HOLDOUT" "$PA"; do
  [ -f "$f" ] || { echo "missing: $f" >&2; exit 1; }
done

rm -rf "$WORK"
mkdir -p "$WORK"/{utils,temp,outputs,logs,data}

# top level: symlink everything except the dirs the run writes into
for e in "$SRC"/*; do
  case "$(basename "$e")" in
    utils|temp|outputs|logs|data) continue ;;
  esac
  ln -s "$e" "$WORK/$(basename "$e")"
done

# utils: symlink everything, then override the two hardcoded lookups
for e in "$SRC"/utils/*; do
  ln -s "$e" "$WORK/utils/$(basename "$e")"
done
rm -f "$WORK/utils/Master_Normalization_File.tsv" "$WORK/utils/PA.tsv"
ln -s "$PA" "$WORK/utils/PA.tsv"

# the holdout is in the MNT's own _raw/_value/_ID shape; stage 01 and
# master_normalizer read InputString/MatchAuthName/MatchAuthID
python3 - "$HOLDOUT" "$WORK/utils/Master_Normalization_File.tsv" <<'PY'
import csv, sys
csv.field_size_limit(sys.maxsize)
src, dst = sys.argv[1], sys.argv[2]
n = 0
with open(src, encoding='utf-8-sig', newline='') as fin, \
     open(dst, 'w', encoding='utf-8', newline='') as fout:
    rd = csv.DictReader(fin, delimiter='\t')
    w = csv.writer(fout, delimiter='\t')
    w.writerow(['InputString', 'MatchAuthName', 'MatchAuthID'])
    for r in rd:
        w.writerow([(r.get('_raw') or '').strip(),
                    (r.get('_value') or '').strip(),
                    (r.get('_ID') or '').strip()])
        n += 1
print(f'master file: {n:,} rows from {src}')
PY

echo "mirror ready: $WORK"
echo "next:"
echo "  cd $WORK"
echo "  python 01_automatch_and_split.py <input.tsv> --output-dir outputs"
echo "  cp outputs/<stem>_anaconda_food.tsv data/food.tsv"
echo "  bash parallel_pipeline.sh data/food.tsv place --no-verbose"
