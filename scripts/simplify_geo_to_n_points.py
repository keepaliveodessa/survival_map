#!/usr/bin/env python3
"""Simplify each geometry in geo_large.csv to <= 100 points per object.

Uses Douglas-Peucker per ring/line with adaptive tolerance so that the total
number of coordinate pairs across all rings/lines in the geometry is <= 100,
without materially changing the visible shape.

After simplification the script appends the rows to geo.csv (replacing any
existing rows with the same names) and deletes geo_large.csv.

Run as a one-off utility:  python3 scripts/simplify_geo_to_n_points.py
"""

import csv
import math
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "postgres" / "data"
GEO_CSV = DATA_DIR / "geo.csv"
LARGE_CSV = DATA_DIR / "geo_large.csv"

CSV_FIELD_LIMIT = 10_000_000
MAX_POINTS_PER_OBJ = 100

_GEOM_TYPE_RE = re.compile(r"^\s*(\w+)\s*\(")
_COORD_RE = re.compile(r"(-?\d+\.?\d*)\s+(-?\d+\.?\d*)")
_RING_RE = re.compile(r"\(([^()]+)\)")


def _perp_dist(px, py, x1, y1, x2, y2):
    dx, dy = x2 - x1, y2 - y1
    seg_sq = dx * dx + dy * dy
    if seg_sq == 0:
        return math.hypot(px - x1, py - y1)
    t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / seg_sq))
    return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))


def _dp_simplify(points, tol):
    """Douglas-Peucker on list of (lon_s, lat_s, x, y)."""
    if len(points) <= 2:
        return list(points)
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


def _parse_ring(s):
    out = []
    for lo_s, la_s in _COORD_RE.findall(s):
        out.append((lo_s, la_s, float(lo_s), float(la_s)))
    return out


def _format_ring(ring, closed):
    coords = ", ".join(f"{lo} {la}" for lo, la, _, _ in ring)
    if closed and (not ring or ring[0][2:] != ring[-1][2:]):
        if ring:
            coords += f", {ring[0][0]} {ring[0][1]}"
        else:
            return ""
    return coords


def _simplify_poly_ring(ring, tol):
    """Simplify a polygon ring, preserving closure (first==last)."""
    if len(ring) <= 4:
        return ring
    closed = (ring[0][2] == ring[-1][2] and ring[0][3] == ring[-1][3])
    if closed:
        open_ring = ring[:-1]
    else:
        open_ring = ring
    if len(open_ring) <= 3:
        return ring
    simp = _dp_simplify(open_ring, tol)
    if len(simp) < 3:
        return ring
    return simp + [simp[0]]


def _total_pts(geom_type, rings):
    if geom_type == "POINT":
        return 1
    return sum(len(r) for r in rings)


def simplify_wkt(wkt, max_points):
    """Return simplified WKT string with total points <= max_points, or None.

    None is returned only if the input has no recognizable geometry.
    """
    if not wkt:
        return wkt
    head = _GEOM_TYPE_RE.match(wkt)
    geom_type = head.group(1).upper() if head else ""

    if geom_type == "POINT":
        return wkt
    if geom_type == "MULTIPOINT":
        return wkt

    # Collect all (lon_s, lat_s) sequences for the inner parentheses
    # For MULTI* we must not collapse across top-level paren groups.
    # Strategy: split the body by "), (" at top-level (outside inner parens).
    body = wkt[head.end():]
    body = body.rstrip()
    if body.endswith(")"):
        body = body[:-1]
    # Split top-level parts on "), "
    parts = []
    depth = 0
    cur = []
    for ch in body:
        if ch == "(":
            depth += 1
            cur.append(ch)
        elif ch == ")":
            depth -= 1
            cur.append(ch)
            if depth == 0:
                parts.append("".join(cur))
                cur = []
        elif depth == 0 and ch == ",":
            # ignore top-level commas
            continue
        else:
            cur.append(ch)
    if cur:
        parts.append("".join(cur))

    def parse_parts(parts, is_polygon):
        out = []
        for p in parts:
            # Each part is like "(x y, x y, ...)" or "((...),(...))"
            m = re.match(r"^\((.*)\)$", p.strip())
            if not m:
                continue
            inner = m.group(1)
            rings = []
            if is_polygon:
                # Try to find nested rings first (multi-ring polygon part)
                nested = _RING_RE.findall(inner)
                if nested:
                    for sub in nested:
                        rings.append(_parse_ring(sub))
                else:
                    # Single ring: comma-separated coords directly
                    rings.append(_parse_ring(inner))
            else:
                rings.append(_parse_ring(inner))
            out.append(rings)
        return out

    is_polygon = geom_type in ("POLYGON", "MULTIPOLYGON")
    nested_parts = parse_parts(parts, is_polygon)
    if not nested_parts:
        return wkt

    # Check if already small enough
    total_before = sum(len(r) for rings in nested_parts for r in rings)
    if total_before <= max_points:
        return wkt

    # Adaptive tolerance: start at 1e-6 and grow until total <= max_points
    tolerance = 1e-6
    max_tol = 0.5
    while tolerance <= max_tol:
        simplified = []
        for rings in nested_parts:
            new_rings = []
            for ring in rings:
                if is_polygon:
                    new_rings.append(_simplify_poly_ring(ring, tolerance))
                else:
                    new_rings.append(_dp_simplify(ring, tolerance))
            simplified.append(new_rings)
        total = sum(len(r) for rs in simplified for r in rs)
        if total <= max_points:
            break
        tolerance *= 2

    # Rebuild WKT
    if geom_type == "POLYGON":
        # single part, possibly multiple rings
        rings = simplified[0]
        parts_out = [_format_ring(r, closed=True) for r in rings]
        return "POLYGON((" + "), (".join(parts_out) + "))"
    if geom_type == "LINESTRING":
        coords = _format_ring(simplified[0][0], closed=False)
        return f"LINESTRING({coords})"
    if geom_type == "MULTILINESTRING":
        parts_out = [_format_ring(rs[0], closed=False) for rs in simplified]
        return "MULTILINESTRING((" + "), (".join(parts_out) + "))"
    if geom_type == "MULTIPOLYGON":
        parts_out = []
        for rings in simplified:
            sub = [_format_ring(r, closed=True) for r in rings]
            parts_out.append("((" + "), (".join(sub) + "))")
        return "MULTIPOLYGON(" + ", ".join(parts_out) + ")"

    return wkt


def read_csv(path):
    csv.field_size_limit(CSV_FIELD_LIMIT)
    with open(path, encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        header = next(reader)
        rows = list(reader)
    return header, rows


def write_csv_atomic(path, header, rows):
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp",
                               dir=str(path.parent))
    os.close(fd)
    try:
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(header)
            w.writerows(rows)
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def count_points(wkt):
    return len(_COORD_RE.findall(wkt))


def main():
    if not LARGE_CSV.exists():
        print(f"ERROR: {LARGE_CSV} not found")
        return 1

    header_large, rows_large = read_csv(LARGE_CSV)
    geom_idx = header_large.index("wkt_geom")
    name_idx = header_large.index("names")
    type_idx = header_large.index("type")

    print(f"Loaded {len(rows_large)} rows from {LARGE_CSV.name}")

    simplified_rows = []
    for row in rows_large:
        names = row[name_idx]
        wkt = row[geom_idx]
        t = row[type_idx]
        before = count_points(wkt)
        new_wkt = simplify_wkt(wkt, MAX_POINTS_PER_OBJ)
        after = count_points(new_wkt)
        status = "ok" if after <= MAX_POINTS_PER_OBJ else "OVER"
        print(f"  [{status}] {names[:40]:40s} type={t:10s} "
              f"{before:4d} -> {after:4d} pts")
        new_row = list(row)
        new_row[geom_idx] = new_wkt
        simplified_rows.append(new_row)

    # Read geo.csv and remove any rows whose names match simplified_rows
    header_geo, rows_geo = read_csv(GEO_CSV)
    geo_name_idx = header_geo.index("names")
    large_names = {row[name_idx] for row in rows_large}
    before_count = len(rows_geo)
    rows_geo = [r for r in rows_geo if r[geo_name_idx] not in large_names]
    removed = before_count - len(rows_geo)
    rows_geo.extend(simplified_rows)
    print(f"\nReplaced {removed} existing rows in {GEO_CSV.name}; "
          f"appended {len(simplified_rows)} simplified rows "
          f"(total now: {len(rows_geo)})")

    # Backup geo.csv before overwrite (only if no .bak exists)
    geo_bak = GEO_CSV.with_suffix(GEO_CSV.suffix + ".bak")
    if not geo_bak.exists():
        shutil.copy2(GEO_CSV, geo_bak)
        print(f"Created backup: {geo_bak.name}")

    write_csv_atomic(GEO_CSV, header_geo, rows_geo)
    print(f"Wrote {GEO_CSV}")

    # Delete geo_large.csv and its .bak
    for p in [LARGE_CSV, LARGE_CSV.with_suffix(LARGE_CSV.suffix + ".bak")]:
        if p.exists():
            p.unlink()
            print(f"Deleted {p.name}")

    # Clean any leftover empty files / tmp files in DATA_DIR
    for entry in os.listdir(DATA_DIR):
        full = DATA_DIR / entry
        if full.is_file() and (entry.endswith(".tmp") or entry.endswith(".bak2")):
            full.unlink()
            print(f"Cleaned leftover: {entry}")
        if full.is_file() and full.stat().st_size == 0:
            full.unlink()
            print(f"Cleaned empty file: {entry}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
