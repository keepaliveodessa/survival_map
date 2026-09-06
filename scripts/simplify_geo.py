#!/usr/bin/env python3
"""Simplify WKT geometries in postgres/data/geo.csv.

Removes insignificant coordinate points from oversize POLYGON / LINESTRING rows
using the Douglas-Peucker algorithm so that every CSV line stays under 9 500
characters, without altering the visible shape of map objects.

Runs as a one-off utility:  python3 scripts/simplify_geo.py
"""

import csv
import io
import math
import re
import sys
from pathlib import Path

CSV_PATH = Path(__file__).resolve().parent.parent / "postgres" / "data" / "geo.csv"
MAX_LINE_LEN = 9_500
csv.field_size_limit(10_000_000)

_COORD_RE = re.compile(r"(-?\d+\.?\d*)\s+(-?\d+\.?\d*)")
_GEOM_RE = re.compile(r"^(\w+)\(")


def extract_rings(wkt):
    """Return (geom_type, rings) where each ring is a list of (lon_s, lat_s, lon_f, lat_f)."""
    m = _GEOM_RE.match(wkt)
    geom_type = m.group(1) if m else None
    rings = []
    for content in re.findall(r"\(([^()]+)\)", wkt):
        coords = _COORD_RE.findall(content)
        if coords:
            rings.append(
                [(lo_s, la_s, float(lo_s), float(la_s)) for lo_s, la_s in coords]
            )
    return geom_type, rings


def _perp_dist(px, py, x1, y1, x2, y2):
    """Perpendicular distance from (px,py) to segment (x1,y1)-(x2,y2)."""
    dx, dy = x2 - x1, y2 - y1
    seg_sq = dx * dx + dy * dy
    if seg_sq == 0:
        return math.hypot(px - x1, py - y1)
    t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / seg_sq))
    return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))


def _dp_simplify(points, tol):
    """Douglas-Peucker on a list of coordinate tuples."""
    if len(points) <= 2:
        return points
    x1, y1 = points[0][2], points[0][3]
    x2, y2 = points[-1][2], points[-1][3]
    max_d, idx = -1.0, 0
    for i in range(1, len(points) - 1):
        d = _perp_dist(points[i][2], points[i][3], x1, y1, x2, y2)
        if d > max_d:
            max_d, idx = d, i
    if max_d <= tol:
        return [points[0], points[-1]]
    left = _dp_simplify(points[: idx + 1], tol)
    right = _dp_simplify(points[idx:], tol)
    return left[:-1] + right


def _simplify_ring(ring, tol):
    """Simplify a closed ring, preserving first==last closure."""
    if len(ring) <= 4:
        return ring
    if ring[0][2] == ring[-1][2] and ring[0][3] == ring[-1][3]:
        open_ring = ring[:-1]
    else:
        open_ring = ring
    if len(open_ring) <= 2:
        return ring
    simplified = _dp_simplify(open_ring, tol)
    if len(simplified) < 3:
        return ring
    return simplified + [simplified[0]]


def _rebuild_wkt(geom_type, rings):
    """Rebuild WKT string from simplified rings."""
    if geom_type == "POLYGON":
        parts = [", ".join(f"{lo} {la}" for lo, la, _, _ in ring) for ring in rings]
        return f"POLYGON(({'), ('.join(parts)}))"
    if geom_type == "LINESTRING":
        coords = rings[0] if rings else []
        cs = ", ".join(f"{lo} {la}" for lo, la, _, _ in coords)
        return f"LINESTRING({cs})"
    if geom_type == "POINT":
        if rings and rings[0]:
            lo, la = rings[0][0][0], rings[0][0][1]
            return f"POINT({lo} {la})"
    return None


def _line_len(row):
    """Full CSV line length (with quoting) for a 3-field row."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(row)
    return len(buf.getvalue().rstrip("\r\n"))


def simplify_wkt_to_fit(wkt, names, type_val):
    """Simplify WKT until the CSV line fits under MAX_LINE_LEN."""
    geom_type, rings = extract_rings(wkt)
    if geom_type not in ("POLYGON", "LINESTRING"):
        return wkt
    tolerance = 1e-5
    new_wkt = wkt
    while tolerance <= 0.05:
        simplified_rings = []
        for ring in rings:
            if geom_type == "POLYGON":
                simplified_rings.append(_simplify_ring(ring, tolerance))
            else:
                simplified_rings.append(_dp_simplify(ring, tolerance))
        new_wkt = _rebuild_wkt(geom_type, simplified_rings)
        if new_wkt is None:
            return wkt
        test_row = [names, new_wkt, type_val]
        if _line_len(test_row) <= MAX_LINE_LEN:
            return new_wkt
        tolerance *= 2
    return new_wkt


def main():
    with open(CSV_PATH, encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader)
        rows = list(reader)

    modified = 0
    max_len = 0
    for i, row in enumerate(rows):
        ll = _line_len(row)
        if ll > max_len:
            max_len = ll
        if len(row) != 3 or ll <= MAX_LINE_LEN:
            continue
        names, wkt, type_val = row
        name = names.split("|")[0]
        print(f"  Line {i + 2}: {name} — {ll} chars → simplifying...", file=sys.stderr)
        new_wkt = simplify_wkt_to_fit(wkt, names, type_val)
        row[1] = new_wkt
        new_ll = _line_len(row)
        print(f"    → {new_ll} chars (WKT {len(new_wkt)} chars)", file=sys.stderr)
        if ll > max_len:
            max_len = ll
        modified += 1

    with open(CSV_PATH, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(rows)

    final_max = max(_line_len(r) for r in rows)
    print(f"\nSimplified {modified} rows. Max line length now: {final_max}", file=sys.stderr)
    return 0 if final_max <= MAX_LINE_LEN else 1


if __name__ == "__main__":
    sys.exit(main())
