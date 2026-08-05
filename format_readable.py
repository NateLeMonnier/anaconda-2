#!/usr/bin/env python3
"""Convert RTL matcher TSV output into a human-readable per-row text file.

Usage: python3 format_readable.py <input.tsv> [output.txt]
If output path omitted, writes <input>_readable.txt next to input.
"""

import csv
import sys
from pathlib import Path

# column -> label. Missing/blank values render as "-".
FIELDS = [
    ("guid", "guid"),
    ("frequency", "frequency"),
    ("match_type", "match_type"),
    ("match_depth", "match_depth"),
    ("candidates", "candidates"),
    ("authority_name", "authority"),
    ("type_ahead", "type_ahead"),
    ("jurisdiction", "jurisdiction"),
    ("level", "level"),
    ("authority_id", "authority_id"),
]


def format_file(in_path: Path, out_path: Path) -> int:
    with in_path.open(newline="", encoding="utf-8") as f_in, \
         out_path.open("w", encoding="utf-8") as f_out:
        reader = csv.DictReader(f_in, delimiter="\t")
        i = 0
        for i, row in enumerate(reader, 1):
            f_out.write(f"[{i}] {row.get('original', '').strip()}\n")
            label_width = max(len(label) for _, label in FIELDS) + 1
            for col, label in FIELDS:
                value = row.get(col, "").strip() or "-"
                f_out.write(f"    {label + ':':<{label_width}} {value}\n")
            skipped_count = row.get("skipped_count", "").strip() or "0"
            skipped_terms = row.get("skipped_terms", "").strip() or "-"
            f_out.write(f"    skipped ({skipped_count}): {skipped_terms}\n")
            f_out.write("\n" + "-" * 80 + "\n\n")
    return i


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: python3 format_readable.py <input.tsv> [output.txt]", file=sys.stderr)
        sys.exit(1)

    in_path = Path(sys.argv[1])
    if len(sys.argv) >= 3:
        out_path = Path(sys.argv[2])
    else:
        out_path = in_path.with_name(in_path.stem + "_readable.txt")

    count = format_file(in_path, out_path)
    print(f"wrote {count} rows to {out_path}")


if __name__ == "__main__":
    main()
