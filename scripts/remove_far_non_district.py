#!/usr/bin/env python3
"""Remove non-district objects further than 50 km from Odessa center.

For postgres/data/geo.csv and postgres/data/geo_large.csv, delete rows whose
haversine distance from (46.4825, 30.7233) exceeds 50 km AND whose ``type``
is not ``district``. ``district`` rows are kept unconditionally.

Run as a one-off utility:  python3 scripts/remove_far_non_district.py
"""

import csv
import math
import re
import shutil
import sys
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "postgres" / "data"
CENTER_LAT = 46.4825
CENTER_LON = 30.7233
THRESHOLD_KM = 50.0
EARTH_RADIUS_M = 6_371_000.0

CSV_FIELD_LIMIT = 10_000_000

_GEOM_TYPE_RE = re.compile(r"^\s*(\w+)\s*\(")
_COORD_RE = re.compile(r"(-?\d+\.?\d*)\s+(-?\d+\.?\d*)")


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in meters between two (lat, lon) points."""
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2.0) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2.0) ** 2
    )
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return EARTH_RADIUS_M * c


def wkt_to_centroid(wkt: str):
    """Return (lon, lat) centroid for POINT/LINESTRING/POLYGON/MULTI* geometries.

    For POINT: coordinates parsed directly. For other types: average of all
    (lon, lat) pairs across every ring. Returns None on failure / empty input.
    """
    if not wkt:
        return None
    head = _GEOM_TYPE_RE.match(wkt)
    geom_type = head.group(1).upper() if head else ""

    if geom_type == "POINT":
        body = wkt[head.end():]
        body = body.rstrip()
        if body.endswith(")"):
            body = body[:-1]
        parts = body.replace(",", " ").split()
        if len(parts) < 2:
            return None
        try:
            return float(parts[0]), float(parts[1])
        except ValueError:
            return None

    # LINESTRING / POLYGON / MULTILINESTRING / MULTIPOLYGON / MULTIPOINT
    lons: list[float] = []
    lats: list[float] = []
    for lon_s, lat_s in _COORD_RE.findall(wkt):
        try:
            lons.append(float(lon_s))
            lats.append(float(lat_s))
        except ValueError:
            continue
    if not lons:
        return None
    return sum(lons) / len(lons), sum(lats) / len(lats)


def distance_km(wkt: str, center_lat: float, center_lon: float):
    """Return haversine distance in km from centroid to center, or None."""
    centroid = wkt_to_centroid(wkt)
    if centroid is None:
        return None
    lon, lat = centroid
    return haversine_m(lat, lon, center_lat, center_lon) / 1000.0


def should_keep(wkt: str, obj_type: str, center_lat: float, center_lon: float,
                threshold_km: float) -> bool:
    if obj_type == "district":
        return True
    d = distance_km(wkt, center_lat, center_lon)
    if d is None:
        return True
    return d <= threshold_km


def filter_csv(input_path: Path, output_path: Path, center_lat: float,
               center_lon: float, threshold_km: float) -> dict:
    """Filter input_path → output_path. Returns summary stats."""
    import tempfile
    import os

    csv.field_size_limit(CSV_FIELD_LIMIT)
    with open(input_path, encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        try:
            header = next(reader)
        except StopIteration:
            return {"input_rows": 0, "kept": 0, "removed": 0, "skipped": 0}

    if "wkt_geom" not in header or "type" not in header:
        raise ValueError(f"{input_path}: header missing wkt_geom/type — got {header}")
    geom_idx = header.index("wkt_geom")
    type_idx = header.index("type")

    kept = 0
    removed = 0
    skipped = 0
    samples_removed: list[tuple[str, str, float]] = []

    fd, tmp_path = tempfile.mkstemp(prefix=output_path.name + ".", suffix=".tmp",
                                    dir=str(output_path.parent))
    os.close(fd)
    try:
        with open(input_path, encoding="utf-8", newline="") as fin, \
                open(tmp_path, "w", encoding="utf-8", newline="") as fout:
            reader = csv.reader(fin)
            writer = csv.writer(fout)
            next(reader, None)  # skip header
            writer.writerow(header)
            for row in reader:
                if len(row) <= max(geom_idx, type_idx):
                    writer.writerow(row)
                    kept += 1
                    continue
                wkt = row[geom_idx]
                obj_type = row[type_idx].strip()
                if not wkt:
                    writer.writerow(row)
                    kept += 1
                    continue
                if should_keep(wkt, obj_type, center_lat, center_lon, threshold_km):
                    writer.writerow(row)
                    kept += 1
                else:
                    removed += 1
                    if len(samples_removed) < 5:
                        d = distance_km(wkt, center_lat, center_lon)
                        samples_removed.append((row[0] if row else "", obj_type,
                                                d if d is not None else -1.0))
        os.replace(tmp_path, output_path)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise

    return {
        "input_rows": kept + removed,
        "kept": kept,
        "removed": removed,
        "skipped": skipped,
        "samples_removed": samples_removed,
    }


def backup_if_missing(path: Path) -> bool:
    bak = path.with_suffix(path.suffix + ".bak")
    if bak.exists():
        return False
    shutil.copy2(path, bak)
    return True


def main() -> int:
    targets = [
        DATA_DIR / "geo.csv",
        DATA_DIR / "geo_large.csv",
    ]
    print(f"Center: ({CENTER_LAT}, {CENTER_LON}); threshold: {THRESHOLD_KM} km\n")
    total_removed = 0
    for p in targets:
        if not p.exists():
            print(f"SKIP: {p} does not exist")
            continue
        created = backup_if_missing(p)
        print(f"--- {p.name} ---")
        if created:
            print(f"  backup created: {p.name}.bak")
        stats = filter_csv(p, p, CENTER_LAT, CENTER_LON, THRESHOLD_KM)
        print(f"  rows: {stats['input_rows']} total, "
              f"kept {stats['kept']}, removed {stats['removed']}")
        for name, t, d in stats.get("samples_removed", []):
            print(f"    removed sample: type={t!r} name={name!r} dist={d:.1f} km")
        total_removed += stats["removed"]
    print(f"\nDone. Total rows removed: {total_removed}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
