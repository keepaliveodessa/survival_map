#!/usr/bin/env python3
"""Identify geo objects with > 100 coordinate points and move them to a separate file."""

import csv
import re
import sys
from pathlib import Path

CSV_PATH = Path(__file__).resolve().parent.parent / "postgres" / "data" / "geo.csv"
OUTPUT_PATH = Path(__file__).resolve().parent.parent / "postgres" / "data" / "geo_large.csv"
THRESHOLD = 100

_COORD_RE = re.compile(r"(-?\d+\.?\d*)\s+(-?\d+\.?\d*)")

csv.field_size_limit(10_000_000)


def count_points(wkt):
    return len(_COORD_RE.findall(wkt))


def main():
    with open(CSV_PATH, encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader)
        all_rows = list(reader)

    large_rows = []
    kept_rows = []
    detail = []

    for i, row in enumerate(all_rows, start=2):
        if len(row) < 2:
            kept_rows.append(row)
            continue
        names, wkt, gtype = row[0], row[1], row[2]
        pts = count_points(wkt)
        gtype_wkt = wkt.strip().split("(", 1)[0].strip().upper()
        first_name = names.split("|")[0]
        if pts > THRESHOLD:
            large_rows.append(row)
            detail.append((i, first_name, gtype_wkt, pts))
        else:
            kept_rows.append(row)

    # Report
    print(f"Objects with > {THRESHOLD} points: {len(large_rows)}")
    print(f"{'Line':>5}  {'Name':<30} {'Geom':<16} {'Points':>6}")
    print("-" * 65)
    for ln, name, gt, pts in sorted(detail, key=lambda x: -x[3]):
        print(f"{ln:>5}  {name:<30.30} {gt:<16} {pts:>6}")

    # Write large objects to separate file (with header)
    with open(OUTPUT_PATH, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(large_rows)

    # Rewrite geo.csv without the large rows
    with open(CSV_PATH, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(kept_rows)

    print(f"\nMoved {len(large_rows)} rows to {OUTPUT_PATH}")
    print(f"Remaining in {CSV_PATH}: {len(kept_rows)} rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
