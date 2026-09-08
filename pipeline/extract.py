"""
Pull noise sources out of an OpenStreetMap .osm.pbf extract.

Produces a compact .npz with one polyline per source and the emission level
already resolved from the tags, so the modelling step never has to look at
OSM again.  Uses pyosmium, which streams the file and needs little memory.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
from dataclasses import dataclass, field

import numpy as np
import osmium

from . import config as C
from . import emission

MAJOR_ICAO = {
    # Hubs that should be "major" even if a runway is under 2400 m.
    "CYYZ", "CYUL", "CYVR", "CYYC", "CYEG", "CYOW", "CYWG", "CYHZ", "CYQB",
}

_SPEED_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(mph|km/h|kmh|kph)?\s*$", re.I)


def parse_maxspeed(value: str | None) -> float | None:
    """'50', '50 km/h', '35 mph' -> km/h.  Anything else -> None."""
    if not value:
        return None
    m = _SPEED_RE.match(value.split(";")[0])
    if not m:
        return None
    v = float(m.group(1))
    unit = (m.group(2) or "").lower()
    if unit == "mph":
        v *= 1.609344
    return v if 5 <= v <= 140 else None


def parse_lanes(value: str | None) -> int | None:
    if not value:
        return None
    try:
        n = int(float(value.split(";")[0]))
    except ValueError:
        return None
    return n if 1 <= n <= 16 else None


def road_class(tags) -> str | None:
    """The ROAD_CLASSES key a highway way models as, or None to skip it."""
    cls = tags.get("highway")
    if cls not in C.ROAD_CLASSES:
        return None
    if tags.get("tunnel") in ("yes", "true", "1"):
        return None
    if tags.get("area") == "yes":
        return None
    if cls == "service" and tags.get("service") in ("driveway", "parking_aisle", "drive-through", "emergency_access"):
        return None
    return cls


def road_day_night(tags) -> tuple[float, float] | None:
    """(day, night) emission level in dB at REF_DIST_ROAD_M for a highway way.

    Levels come from the traffic-flow model in pipeline/emission.py: the OSM
    class supplies default AADT / speed / heavy share, and the way's own
    `lanes` and `maxspeed` tags refine them where they are present.
    """
    cls = road_class(tags)
    if cls is None:
        return None
    return emission.road_levels(
        cls,
        parse_lanes(tags.get("lanes")),
        parse_maxspeed(tags.get("maxspeed")),
    )


def road_level(tags) -> float | None:
    """Daytime emission level only (kept for probes and tests)."""
    dn = road_day_night(tags)
    return None if dn is None else dn[0]


def rail_class(tags) -> str | None:
    """The RAIL_LEVELS key a railway way models as, or None to skip it."""
    rw = tags.get("railway")
    if rw is None:
        return None
    if tags.get("tunnel") in ("yes", "true", "1"):
        return None
    if tags.get("railway:traffic_mode") == "none":
        return None
    service = tags.get("service")
    usage = tags.get("usage")
    if rw == "rail":
        if service in ("yard", "spur", "siding", "crossover") or usage in ("industrial", "military"):
            return "yard"
        if usage == "main":
            return "main"
        if usage == "branch":
            return "branch"
        if usage == "tourism":
            return "preserved"
        return "rail"
    if rw in ("light_rail", "tram", "subway", "narrow_gauge", "monorail", "preserved"):
        if service in ("yard", "spur", "siding", "crossover"):
            return "yard"
        return rw
    return None  # abandoned, disused, construction, platform, ...


def rail_day_night(tags) -> tuple[float, float] | None:
    cls = rail_class(tags)
    return None if cls is None else emission.rail_levels(cls)


def rail_level(tags) -> float | None:
    dn = rail_day_night(tags)
    return None if dn is None else dn[0]


def night_delta_road(tags) -> float:
    cls = road_class(tags)
    return 0.0 if cls is None else emission.night_delta_for(cls)


def night_delta_rail(tags) -> float:
    return C.RAIL_NIGHT_DELTA.get(rail_class(tags) or "", -5.0)


@dataclass
class Sources:
    """Polylines grouped by kernel type."""
    road_coords: list = field(default_factory=list)   # list of (N,2) lon/lat arrays
    road_levels: list = field(default_factory=list)
    road_levels_night: list = field(default_factory=list)
    road_kinds: list = field(default_factory=list)    # 'road' | 'rail'
    runway_coords: list = field(default_factory=list)
    runway_levels: list = field(default_factory=list)
    runway_levels_night: list = field(default_factory=list)
    runway_info: list = field(default_factory=list)
    aerodromes: list = field(default_factory=list)    # dicts with bbox + tags


class Handler(osmium.SimpleHandler):
    """Pull modelled sources out of an extract.

    `clip` is an optional (west, south, east, north) box.  When set, only ways
    that touch the box are kept, which is what lets one province-sized .osm.pbf
    feed many city-sized build regions: the file is streamed once per region
    and everything outside that region's box is dropped before it costs any
    memory.  The box passed in should already include the halo, since a source
    just outside a region still radiates into it.
    """

    def __init__(self, clip=None):
        super().__init__()
        self.s = Sources()
        self._runways = []  # (coords, tags dict) resolved after aerodromes are known
        self.clip = tuple(clip) if clip is not None else None
        self.stats = {"ways": 0, "roads": 0, "rails": 0, "runways": 0, "aerodromes": 0}

    def _coords(self, way):
        pts = []
        for n in way.nodes:
            if n.location.valid():
                pts.append((n.location.lon, n.location.lat))
        if len(pts) < 2:
            return None
        a = np.asarray(pts, dtype=np.float64)
        if self.clip is not None:
            w, s, e, n = self.clip
            if a[:, 0].max() < w or a[:, 0].min() > e or a[:, 1].max() < s or a[:, 1].min() > n:
                return None
        return a

    def way(self, w):
        self.stats["ways"] += 1
        tags = w.tags
        if "highway" in tags:
            dn = road_day_night(tags)
            if dn is not None:
                c = self._coords(w)
                if c is not None:
                    self.s.road_coords.append(c)
                    self.s.road_levels.append(dn[0])
                    self.s.road_levels_night.append(dn[1])
                    self.s.road_kinds.append(0)
                    self.stats["roads"] += 1
        elif "railway" in tags:
            dn = rail_day_night(tags)
            if dn is not None:
                c = self._coords(w)
                if c is not None:
                    self.s.road_coords.append(c)
                    self.s.road_levels.append(dn[0])
                    self.s.road_levels_night.append(dn[1])
                    self.s.road_kinds.append(1)
                    self.stats["rails"] += 1
        if tags.get("aeroway") == "runway" and tags.get("area") != "yes" and not w.is_closed():
            c = self._coords(w)
            if c is not None:
                self._runways.append((c, {k: v for k, v in ((t.k, t.v) for t in tags)}))

    def area(self, a):
        tags = a.tags
        if tags.get("aeroway") != "aerodrome":
            return
        lons, lats = [], []
        try:
            for ring in a.outer_rings():
                for n in ring:
                    if n.location.valid():
                        lons.append(n.location.lon)
                        lats.append(n.location.lat)
        except Exception:
            return
        if not lons:
            return
        self.s.aerodromes.append({
            "bbox": [min(lons), min(lats), max(lons), max(lats)],
            "name": tags.get("name"),
            "iata": tags.get("iata"),
            "icao": tags.get("icao"),
            "type": tags.get("aerodrome:type") or tags.get("aerodrome"),
        })
        self.stats["aerodromes"] += 1

    def resolve_runways(self):
        for coords, tags in self._runways:
            if tags.get("surface") in ("grass", "dirt", "ground", "gravel", "turf"):
                cls = "minor"
                aero = None
            else:
                mid = coords[len(coords) // 2]
                aero = None
                for a in self.s.aerodromes:
                    b = a["bbox"]
                    if b[0] - 0.02 <= mid[0] <= b[2] + 0.02 and b[1] - 0.02 <= mid[1] <= b[3] + 0.02:
                        aero = a
                        break
                length = polyline_length_m(coords)
                iata = aero.get("iata") if aero else None
                icao = aero.get("icao") if aero else None
                if icao in MAJOR_ICAO or (iata and length >= 2400):
                    cls = "major"
                elif (iata and length >= 1200) or length >= 1800:
                    cls = "regional"
                else:
                    cls = "minor"
            self.s.runway_coords.append(coords)
            self.s.runway_levels.append(C.RUNWAY_LEVELS[cls])
            self.s.runway_levels_night.append(C.RUNWAY_LEVELS[cls] + C.RUNWAY_NIGHT_DELTA[cls])
            self.s.runway_info.append({
                "class": cls,
                "airport": (aero or {}).get("name"),
                "iata": (aero or {}).get("iata"),
                "length_m": round(polyline_length_m(coords)),
            })
            self.stats["runways"] += 1


def polyline_length_m(coords: np.ndarray) -> float:
    lat = math.radians(float(coords[:, 1].mean()))
    dx = np.diff(coords[:, 0]) * 111_320.0 * math.cos(lat)
    dy = np.diff(coords[:, 1]) * 110_540.0
    return float(np.hypot(dx, dy).sum())


def pbf_bbox(path: str):
    r = osmium.io.Reader(path)
    try:
        box = r.header().box()
        if box.valid():
            return [box.bottom_left.lon, box.bottom_left.lat, box.top_right.lon, box.top_right.lat]
    finally:
        r.close()
    return None


def pack(seqs: list[np.ndarray]):
    """List of (N,2) arrays -> flat (M,2) array + offsets."""
    if not seqs:
        return np.zeros((0, 2)), np.zeros(1, dtype=np.int64)
    offsets = np.zeros(len(seqs) + 1, dtype=np.int64)
    offsets[1:] = np.cumsum([len(s) for s in seqs])
    return np.concatenate(seqs), offsets


def extract(pbf_path: str, out_path: str, bbox=None, clip=None) -> dict:
    """Extract modelled sources from `pbf_path` into `out_path`.

    `bbox` is the region the raster will cover.  `clip`, if given, restricts
    which ways are read - pass the bbox plus a halo so one large extract can
    serve many regions.  Passing bbox without clip reads the whole file, which
    is what the single-city builds do.
    """
    t0 = time.time()
    h = Handler(clip=clip)
    h.apply_file(pbf_path, locations=True, idx="flex_mem")
    h.resolve_runways()
    s = h.s
    bbox = bbox or pbf_bbox(pbf_path)
    if bbox is None:
        allc = s.road_coords + s.runway_coords
        cat = np.concatenate(allc)
        bbox = [float(cat[:, 0].min()), float(cat[:, 1].min()), float(cat[:, 0].max()), float(cat[:, 1].max())]
    rc, ro = pack(s.road_coords)
    ac, ao = pack(s.runway_coords)
    meta = {
        "pbf": pbf_path,
        "bbox": bbox,
        "stats": h.stats,
        "runways": s.runway_info,
        "aerodromes": [a for a in s.aerodromes if a.get("iata") or a.get("icao")],
        "seconds": round(time.time() - t0, 1),
    }
    np.savez_compressed(
        out_path,
        road_coords=rc, road_offsets=ro,
        road_levels=np.asarray(s.road_levels, dtype=np.float32),
        road_levels_night=np.asarray(s.road_levels_night, dtype=np.float32),
        road_kinds=np.asarray(s.road_kinds, dtype=np.int8),
        runway_coords=ac, runway_offsets=ao,
        runway_levels=np.asarray(s.runway_levels, dtype=np.float32),
        runway_levels_night=np.asarray(s.runway_levels_night, dtype=np.float32),
        bbox=np.asarray(bbox, dtype=np.float64),
        meta=json.dumps(meta),
    )
    return meta


def main(argv=None):
    ap = argparse.ArgumentParser(description="Extract noise sources from an OSM PBF")
    ap.add_argument("pbf")
    ap.add_argument("out", help="output .npz")
    a = ap.parse_args(argv)
    meta = extract(a.pbf, a.out)
    print(json.dumps({k: v for k, v in meta.items() if k != "runways"}, indent=1))
    for r in meta["runways"]:
        print("  runway", r)


if __name__ == "__main__":
    sys.exit(main())
