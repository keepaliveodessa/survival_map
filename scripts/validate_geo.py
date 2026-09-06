#!/usr/bin/env python3
"""Standalone validation for geo.csv — replicates tests/test_streets_data.py checks."""

import csv
import math
import re
from pathlib import Path

MAX_LEN = 9_500
DATA_DIR = Path(__file__).resolve().parent.parent / "postgres" / "data"
ALLOWED = {"POINT", "LINESTRING", "POLYGON", "MULTIPOLYGON", "MULTILINESTRING", "MULTIPOINT"}
_R_LAT = 111320.0


def _rows(csv_path):
    with open(csv_path, encoding="utf-8") as f:
        r = csv.reader(f)
        header = next(r)
        assert header == ["names", "wkt_geom", "type"], f"header={header}"
        for i, row in enumerate(r, start=2):
            if len(row) >= 2:
                yield i, row


def _gtype(w):
    return w.strip().split("(", 1)[0].strip().upper()


def _first_ring(w):
    m = re.search(r"\(([^()]+)\)", w)
    return m.group(1) if m else ""


def _pts(s):
    out = []
    for pair in s.split(","):
        a = pair.split()
        if len(a) >= 2:
            out.append((float(a[0]), float(a[1])))
    return out


def _area_m2(p):
    lat0 = sum(q[1] for q in p) / len(p)
    mx = _R_LAT * math.cos(math.radians(lat0))
    xs = [q[0] * mx for q in p]
    ys = [q[1] * _R_LAT for q in p]
    a = sum(xs[i] * ys[i + 1] - xs[i + 1] * ys[i] for i in range(len(p) - 1))
    return abs(a) / 2.0


def _per_m(p):
    lat0 = sum(q[1] for q in p) / len(p)
    mx = _R_LAT * math.cos(math.radians(lat0))
    return sum(
        math.hypot((p[i + 1][0] - p[i][0]) * mx, (p[i + 1][1] - p[i][1]) * _R_LAT)
        for i in range(len(p) - 1)
    )


def validate_file(csv_path):
    csv.field_size_limit(10_000_000)
    errors = []

    # 1. Max line length
    max_len = 0
    for i, row in _rows(csv_path):
        import io
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(row)
        ll = len(buf.getvalue().rstrip("\r\n"))
        if ll > MAX_LEN:
            errors.append(f"  Line {i}: {ll} chars > {MAX_LEN}")
        if ll > max_len:
            max_len = ll
    print(f"  Max line length: {max_len} (limit {MAX_LEN})")

    # 2. Three fields
    for i, row in _rows(csv_path):
        if len(row) != 3:
            errors.append(f"  Line {i}: {len(row)} fields (expected 3)")

    # 3. Allowed geometry types
    for i, row in _rows(csv_path):
        gt = _gtype(row[1])
        if gt not in ALLOWED:
            errors.append(f"  Line {i}: unexpected geometry type '{gt}'")

    # 4. No degenerate sliver polygons
    for i, row in _rows(csv_path):
        names, wkt, _ = row
        if _gtype(wkt) != "POLYGON":
            continue
        ring = _pts(_first_ring(wkt))
        if len(ring) < 4:
            errors.append(f"  Line {i}: POLYGON '{names}' has < 4 points")
            continue
        area = _area_m2(ring)
        per = _per_m(ring)
        comp = (4 * math.pi * area / (per * per)) if per else 0.0
        if area < 100 and comp < 0.03:
            errors.append(f"  Line {i}: degenerate sliver polygon '{names}' (area={area:.1f}, comp={comp:.4f})")

    return errors


def main():
    all_errors = []
    for name, path in [("geo.csv", DATA_DIR / "geo.csv"),
                       ("geo_large.csv", DATA_DIR / "geo_large.csv")]:
        print(f"\n=== Validating {name} ===")
        errs = validate_file(path)
        all_errors.extend(errs)
        if not errs:
            print(f"  All checks PASSED for {name}")
        else:
            for e in errs:
                print(e)

    if all_errors:
        print(f"\nFAILED ({len(all_errors)} issues total)")
        return 1
    print(f"\nAll checks PASSED for both files.")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
