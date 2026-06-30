#!/usr/bin/env python3
"""Build compact map artifacts for the Puget Sound transit map browser.

Reads the consolidated GTFS feed in ``data/gtfs_puget_sound_consolidated`` and
emits small JSON/GeoJSON files into ``docs/data`` that a static (GitHub Pages)
web app can load directly.

The raw GTFS cannot be served as-is (``stop_times.txt`` alone is ~251 MB, over
GitHub's 100 MB per-file limit), so this script streams the large files and
derives a compact, denormalised view of each route:

  * agency, mode/route_type, names, colors
  * service-day flags (weekday / Saturday / Sunday)
  * time-of-day bucket flags + earliest/latest service minute
  * a representative, simplified shape per direction
  * the set of stops served (for stop <-> route linking)

Standard library only. Re-run after refreshing ``/data``.
"""

import csv
import json
import os
import sys
import math

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GTFS_DIR = os.path.join(ROOT, "data", "gtfs_puget_sound_consolidated")
OUT_DIR = os.path.join(ROOT, "docs", "data")

# ---------------------------------------------------------------------------
# Domain constants
# ---------------------------------------------------------------------------
ROUTE_TYPE_LABELS = {
    "0": "Light Rail",
    "1": "Subway",
    "2": "Rail",
    "3": "Bus",
    "4": "Ferry",
    "5": "Cable Car",
    "6": "Aerial",
    "7": "Funicular",
    "11": "Trolleybus",
    "12": "Monorail",
}

# Time-of-day buckets, in minutes from midnight. Late night extends past 24:00
# to capture GTFS after-midnight times.
BUCKETS = [
    ("early",   "Early morning (12a-6a)",   0,    6 * 60),
    ("midday",  "Daytime (6a-6p)",          6 * 60, 18 * 60),
    ("evening", "Evening (6p-10p)",         18 * 60, 22 * 60),
    ("late",    "Late night (10p-2a)",      22 * 60, 26 * 60),
]

# Shape simplification tolerance in degrees (~10 m at this latitude).
SIMPLIFY_TOLERANCE = 0.0001
COORD_PRECISION = 5


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def open_gtfs(name):
    path = os.path.join(GTFS_DIR, name)
    # utf-8-sig strips a BOM if present; newline="" per csv docs.
    return open(path, "r", encoding="utf-8-sig", newline="")


def parse_time_to_min(value):
    """Parse a GTFS HH:MM:SS time (may exceed 24:00) into minutes; None if blank."""
    if not value:
        return None
    parts = value.split(":")
    if len(parts) < 2:
        return None
    try:
        h = int(parts[0])
        m = int(parts[1])
    except ValueError:
        return None
    return h * 60 + m


# ---------------------------------------------------------------------------
# 1. agency.txt
# ---------------------------------------------------------------------------
def load_agencies():
    agencies = {}
    with open_gtfs("agency.txt") as f:
        for row in csv.DictReader(f):
            aid = (row.get("agency_id") or "").strip()
            if not aid:
                continue
            agencies[aid] = (row.get("agency_name") or aid).strip()
    log(f"agencies: {len(agencies)}")
    return agencies


# ---------------------------------------------------------------------------
# 2. calendar.txt + calendar_dates.txt -> service_id -> {weekday, sat, sun}
# ---------------------------------------------------------------------------
def load_service_days():
    """Return service_id -> (weekday, sat, sun) booleans."""
    days = {}
    weekday_cols = ["monday", "tuesday", "wednesday", "thursday", "friday"]
    try:
        with open_gtfs("calendar.txt") as f:
            for row in csv.DictReader(f):
                sid = (row.get("service_id") or "").strip()
                if not sid:
                    continue
                weekday = any((row.get(c) or "0").strip() == "1" for c in weekday_cols)
                sat = (row.get("saturday") or "0").strip() == "1"
                sun = (row.get("sunday") or "0").strip() == "1"
                days[sid] = [weekday, sat, sun]
    except FileNotFoundError:
        log("calendar.txt not found")

    # Fall back to calendar_dates for services with no calendar.txt row
    # (e.g. Amtrak). exception_type 1 = service added on that date.
    try:
        import datetime
        with open_gtfs("calendar_dates.txt") as f:
            for row in csv.DictReader(f):
                sid = (row.get("service_id") or "").strip()
                if not sid or sid in days:
                    continue
                if (row.get("exception_type") or "").strip() != "1":
                    continue
                date = (row.get("date") or "").strip()
                if len(date) != 8:
                    continue
                try:
                    dt = datetime.date(int(date[:4]), int(date[4:6]), int(date[6:8]))
                except ValueError:
                    continue
                wd = dt.weekday()  # Mon=0 .. Sun=6
                flags = days.setdefault(sid, [False, False, False])
                if wd <= 4:
                    flags[0] = True
                elif wd == 5:
                    flags[1] = True
                else:
                    flags[2] = True
    except FileNotFoundError:
        pass

    log(f"service_ids with day info: {len(days)}")
    return days


# ---------------------------------------------------------------------------
# 3. routes.txt
# ---------------------------------------------------------------------------
def load_routes(agencies):
    routes = {}
    with open_gtfs("routes.txt") as f:
        for row in csv.DictReader(f):
            rid = (row.get("route_id") or "").strip()
            if not rid:
                continue
            aid = (row.get("agency_id") or "").strip()
            short = (row.get("route_short_name") or "").strip()
            long = (row.get("route_long_name") or "").strip()
            desc = (row.get("route_desc") or "").strip()
            color = (row.get("route_color") or "").strip()
            text_color = (row.get("route_text_color") or "").strip()
            routes[rid] = {
                "id": rid,
                "agency_id": aid,
                "agency": agencies.get(aid, aid or "Unknown"),
                "short_name": short,
                "long_name": long or desc,
                "type": (row.get("route_type") or "").strip(),
                "color": color,
                "text_color": text_color,
                "url": (row.get("route_url") or "").strip(),
                # derived below
                "service_days": [False, False, False],  # weekday, sat, sun
                "buckets": [False, False, False, False],
                "start_min": None,
                "end_min": None,
                "shape_ids": set(),
                "service_ids": set(),
                "headsigns": set(),
            }
    log(f"routes: {len(routes)}")
    return routes


# ---------------------------------------------------------------------------
# 4. trips.txt -> trip_id -> route_id ; populate route shapes/services
# ---------------------------------------------------------------------------
def load_trips(routes):
    trip_to_route = {}
    with open_gtfs("trips.txt") as f:
        for row in csv.DictReader(f):
            rid = (row.get("route_id") or "").strip()
            tid = (row.get("trip_id") or "").strip()
            if not rid or not tid or rid not in routes:
                continue
            trip_to_route[tid] = rid
            r = routes[rid]
            shape = (row.get("shape_id") or "").strip()
            if shape:
                r["shape_ids"].add(shape)
            sid = (row.get("service_id") or "").strip()
            if sid:
                r["service_ids"].add(sid)
            hs = (row.get("trip_headsign") or "").strip()
            if hs:
                r["headsigns"].add(hs)
    log(f"trips mapped: {len(trip_to_route)}")
    return trip_to_route


def apply_service_days(routes, service_days):
    for r in routes.values():
        wd = sat = sun = False
        for sid in r["service_ids"]:
            flags = service_days.get(sid)
            if not flags:
                continue
            wd = wd or flags[0]
            sat = sat or flags[1]
            sun = sun or flags[2]
        r["service_days"] = [wd, sat, sun]


# ---------------------------------------------------------------------------
# 5. stop_times.txt (streamed) -> per-route time window, buckets, stops served
# ---------------------------------------------------------------------------
def bucket_indices_for(minute):
    idxs = []
    norm = minute % (24 * 60)  # wrap after-midnight times for early bucket
    for i, (_key, _label, start, end) in enumerate(BUCKETS):
        if start <= minute < end or start <= norm < end:
            idxs.append(i)
    return idxs


def stream_stop_times(routes, trip_to_route):
    route_stops = {}  # route_id -> set(stop_id)
    n = 0
    with open_gtfs("stop_times.txt") as f:
        reader = csv.reader(f)
        header = next(reader)
        col = {name: i for i, name in enumerate(header)}
        i_trip = col.get("trip_id")
        i_stop = col.get("stop_id")
        i_dep = col.get("departure_time", col.get("arrival_time"))
        i_arr = col.get("arrival_time", i_dep)
        for parts in reader:
            n += 1
            if n % 2_000_000 == 0:
                log(f"  stop_times rows: {n:,}")
            try:
                tid = parts[i_trip]
            except IndexError:
                continue
            rid = trip_to_route.get(tid)
            if rid is None:
                continue
            r = routes[rid]
            # stops served
            sid = parts[i_stop] if i_stop is not None and i_stop < len(parts) else ""
            if sid:
                route_stops.setdefault(rid, set()).add(sid)
            # time window + buckets (prefer departure, fall back to arrival)
            tval = parts[i_dep] if i_dep is not None and i_dep < len(parts) else ""
            if not tval and i_arr is not None and i_arr < len(parts):
                tval = parts[i_arr]
            minute = parse_time_to_min(tval)
            if minute is None:
                continue
            if r["start_min"] is None or minute < r["start_min"]:
                r["start_min"] = minute
            if r["end_min"] is None or minute > r["end_min"]:
                r["end_min"] = minute
            for bi in bucket_indices_for(minute):
                r["buckets"][bi] = True
    log(f"stop_times rows total: {n:,}")
    return route_stops


# ---------------------------------------------------------------------------
# 6. shapes.txt -> simplified geometry for shapes used by routes
# ---------------------------------------------------------------------------
def _perp_dist(pt, a, b):
    (x, y), (x1, y1), (x2, y2) = pt, a, b
    dx, dy = x2 - x1, y2 - y1
    if dx == 0 and dy == 0:
        return math.hypot(x - x1, y - y1)
    t = ((x - x1) * dx + (y - y1) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    px, py = x1 + t * dx, y1 + t * dy
    return math.hypot(x - px, y - py)


def simplify(points, tol):
    """Iterative Douglas-Peucker on (lon, lat) tuples."""
    if len(points) < 3:
        return points
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        lo, hi = stack.pop()
        if hi - lo < 2:
            continue
        dmax, idx = 0.0, -1
        a, b = points[lo], points[hi]
        for i in range(lo + 1, hi):
            d = _perp_dist(points[i], a, b)
            if d > dmax:
                dmax, idx = d, i
        if dmax > tol and idx != -1:
            keep[idx] = True
            stack.append((lo, idx))
            stack.append((idx, hi))
    return [p for p, k in zip(points, keep) if k]


def load_and_simplify_shapes(needed_shape_ids):
    """Return shape_id -> simplified list of [lon, lat]."""
    raw = {}  # shape_id -> list of (seq, lon, lat)
    with open_gtfs("shapes.txt") as f:
        reader = csv.reader(f)
        header = next(reader)
        col = {name: i for i, name in enumerate(header)}
        i_id = col["shape_id"]
        i_seq = col["shape_pt_sequence"]
        i_lat = col["shape_pt_lat"]
        i_lon = col["shape_pt_lon"]
        for parts in reader:
            try:
                sid = parts[i_id]
            except IndexError:
                continue
            if sid not in needed_shape_ids:
                continue
            try:
                seq = int(parts[i_seq])
                lat = float(parts[i_lat])
                lon = float(parts[i_lon])
            except (ValueError, IndexError):
                continue
            raw.setdefault(sid, []).append((seq, lon, lat))

    shapes = {}
    for sid, pts in raw.items():
        pts.sort(key=lambda p: p[0])
        coords = [(lon, lat) for _seq, lon, lat in pts]
        simplified = simplify(coords, SIMPLIFY_TOLERANCE)
        shapes[sid] = [
            [round(lon, COORD_PRECISION), round(lat, COORD_PRECISION)]
            for lon, lat in simplified
        ]
    log(f"shapes simplified: {len(shapes)}")
    return shapes


def choose_route_shapes(routes, shapes):
    """Pick a small set of representative shapes per route (longest few)."""
    for r in routes.values():
        present = [(sid, shapes.get(sid)) for sid in r["shape_ids"]]
        present = [(sid, pts) for sid, pts in present if pts]
        present.sort(key=lambda sp: len(sp[1]), reverse=True)
        # keep up to 2 longest distinct shapes (covers both directions)
        r["chosen_shapes"] = [sid for sid, _ in present[:2]]


# ---------------------------------------------------------------------------
# 7. stops.txt -> trimmed geojson with serving route ids
# ---------------------------------------------------------------------------
def build_stops_geojson(route_stops, routes):
    # invert route_stops -> stop_id -> set(route_id), but only routes we kept
    stop_routes = {}
    for rid, stops in route_stops.items():
        for sid in stops:
            stop_routes.setdefault(sid, set()).add(rid)

    features = []
    with open_gtfs("stops.txt") as f:
        for row in csv.DictReader(f):
            sid = (row.get("stop_id") or "").strip()
            if not sid or sid not in stop_routes:
                continue
            try:
                lat = float(row.get("stop_lat"))
                lon = float(row.get("stop_lon"))
            except (TypeError, ValueError):
                continue
            features.append({
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": [round(lon, COORD_PRECISION), round(lat, COORD_PRECISION)],
                },
                "properties": {
                    "id": sid,
                    "name": (row.get("stop_name") or "").strip(),
                    "routes": sorted(stop_routes[sid]),
                },
            })
    log(f"stops kept: {len(features)}")
    return {"type": "FeatureCollection", "features": features}


# ---------------------------------------------------------------------------
# Output assembly
# ---------------------------------------------------------------------------
def write_outputs(routes, shapes, stops_geojson, agencies):
    os.makedirs(OUT_DIR, exist_ok=True)

    # routes.json
    route_list = []
    used_agencies = {}
    used_types = {}
    for r in sorted(routes.values(), key=lambda x: (x["agency"], x["short_name"] or x["long_name"])):
        # skip routes with neither geometry nor service signal
        route_list.append({
            "id": r["id"],
            "agency_id": r["agency_id"],
            "agency": r["agency"],
            "short_name": r["short_name"],
            "long_name": r["long_name"],
            "type": r["type"],
            "color": r["color"],
            "text_color": r["text_color"],
            "url": r["url"],
            "service_days": [int(b) for b in r["service_days"]],
            "buckets": [int(b) for b in r["buckets"]],
            "start_min": r["start_min"],
            "end_min": r["end_min"],
            "shapes": r.get("chosen_shapes", []),
            "headsigns": sorted(r["headsigns"])[:4],
        })
        if r["agency_id"]:
            used_agencies[r["agency_id"]] = r["agency"]
        if r["type"]:
            used_types[r["type"]] = ROUTE_TYPE_LABELS.get(r["type"], f"Type {r['type']}")

    # shapes.geojson: one LineString feature per shape, with route ids referencing it
    shape_to_routes = {}
    for r in routes.values():
        for sid in r.get("chosen_shapes", []):
            shape_to_routes.setdefault(sid, []).append(r["id"])
    shape_features = []
    for sid, coords in shapes.items():
        if sid not in shape_to_routes:
            continue
        if len(coords) < 2:
            continue
        shape_features.append({
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": coords},
            "properties": {"id": sid, "routes": shape_to_routes[sid]},
        })
    shapes_geojson = {"type": "FeatureCollection", "features": shape_features}

    meta = {
        "agencies": [{"id": k, "name": v} for k, v in sorted(used_agencies.items(), key=lambda kv: kv[1])],
        "route_types": [{"id": k, "name": v} for k, v in sorted(used_types.items(), key=lambda kv: int(kv[0]))],
        "service_days": [
            {"key": "weekday", "name": "Weekday"},
            {"key": "saturday", "name": "Saturday"},
            {"key": "sunday", "name": "Sunday"},
        ],
        "buckets": [{"key": k, "name": label} for (k, label, _s, _e) in BUCKETS],
        "route_count": len(route_list),
    }

    def dump(name, obj):
        path = os.path.join(OUT_DIR, name)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f, separators=(",", ":"), ensure_ascii=False)
        size = os.path.getsize(path)
        log(f"wrote {name}: {size / 1_000_000:.2f} MB")
        return size

    total = 0
    total += dump("routes.json", route_list)
    total += dump("shapes.geojson", shapes_geojson)
    total += dump("stops.geojson", stops_geojson)
    total += dump("meta.json", meta)
    log(f"total artifacts: {total / 1_000_000:.2f} MB")


def main():
    log("== Building transit map artifacts ==")
    agencies = load_agencies()
    service_days = load_service_days()
    routes = load_routes(agencies)
    trip_to_route = load_trips(routes)
    apply_service_days(routes, service_days)

    log("streaming stop_times.txt (this is the slow step)...")
    route_stops = stream_stop_times(routes, trip_to_route)

    needed = set()
    for r in routes.values():
        needed |= r["shape_ids"]
    log(f"loading shapes for {len(needed)} shape ids...")
    shapes = load_and_simplify_shapes(needed)
    choose_route_shapes(routes, shapes)

    stops_geojson = build_stops_geojson(route_stops, routes)
    write_outputs(routes, shapes, stops_geojson, agencies)
    log("== Done ==")


if __name__ == "__main__":
    main()
