"""
Validate the modelled noise surface against Toronto Public Health's 2017
Environmental Noise Study.

Why this comparison and not a simpler one
-----------------------------------------
TPH measured 220 sites, but those sites were chosen deliberately (schools,
long-term care homes, transit yards, complaint locations), so their mean is
not the mean of Toronto and comparing our city-wide median against it would be
meaningless.  What IS comparable is the population-exposure table TPH computed
from their own modelled surface: the share of Toronto residents above a set of
level thresholds, day and night.  Those come from a model of the same city on
the same metric over the same night window (23:00-07:00), so the same quantity
can be computed from our raster and the two put side by side.

TPH's model is better resourced than ours in three specific ways - real AADT
counts refined by hourly histograms, FHWA TNM 2.5 emission levels, and ISO
9613-2 propagation in SoundPLAN with a land-use-regression correction - and it
reports its own validation against measurements (day R2 0.64 / RMSE 3.70 dB,
night R2 0.71 / RMSE 4.10 dB).  So this is a comparison against a better
model, not against ground truth, and the gap should be read that way.

Population weighting
--------------------
We have no census geography (Statistics Canada's servers are not reachable
from the build environment), so population is proxied by residential building
floor area from OSM: footprint area times storeys, which tracks dwelling count
far better than land area does.  It is a proxy, and the numbers below are
reported as such.

    python -m pipeline.validate                    # Toronto vs TPH 2017
    python -m pipeline.validate --region montreal  # distribution only
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time

import numpy as np
import osmium

from . import config as C
from . import model

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# Exposure statistics in the END/CNOSSOS tradition, and in the TPH study, are
# evaluated at the most exposed building facade, which includes the reflection
# off the wall behind the receiver - about +3 dB over a free-field level.  The
# tiles stay free-field, which is the right thing for a map you can stand
# anywhere on; the correction is applied only when comparing exposure.
FACADE_DB = 3.0

# Toronto Public Health, "Environmental Noise Study in the City of Toronto"
# (April 2017), Table 9 - share of Toronto residents above each threshold on
# the modelled surface.  "Traffic" is the road-traffic-only surface, which is
# the one comparable with this map; "total" additionally includes rail,
# aircraft and stationary sources.
TPH_2017 = {
    "source": "Toronto Public Health, Environmental Noise Study in the City of Toronto (April 2017), Table 9",
    "url": "https://www.toronto.ca/wp-content/uploads/2017/11/8f4d-tph-Environmental-Noise-Study-2017.pdf",
    "traffic": {("day", 65): 27.1, ("day", 55): 60.2, ("night", 55): 33.1, ("night", 45): 77.4},
    "total":   {("day", 65): 38.8, ("day", 55): 88.7, ("night", 55): 43.4, ("night", 45): 92.3},
    # Table 1, measured levels across the 220 monitoring sites (full week).
    "measured_mean": {"day": 64.1, "night": 57.5},
    "measured_range": {"day": (51.6, 79.5), "night": (42.6, 74.4)},
    # Table 8, TPH's own model validation against those measurements.
    "model_skill": {"day": {"r2": 0.64, "rmse": 3.70}, "night": {"r2": 0.71, "rmse": 4.10}},
}

# Building tags that house people.  "yes" is by far the most common tag on
# Canadian residential buildings so it has to be included.
RESIDENTIAL = {
    "yes", "house", "residential", "apartments", "detached", "semidetached_house",
    "terrace", "bungalow", "dormitory", "static_caravan", "cabin", "duplex",
    "hut", "semi", "townhouse",
}
# Typical storeys when building:levels is missing.
DEFAULT_LEVELS = {"apartments": 6, "dormitory": 6, "residential": 3, "terrace": 2,
                  "townhouse": 2, "duplex": 2, "semidetached_house": 2, "house": 2,
                  "detached": 2, "yes": 2}


class BuildingHandler(osmium.SimpleHandler):
    """Centroid + floor-area proxy for every closed residential building way,
    plus the City of Toronto administrative boundary if it is in the extract."""

    def __init__(self, boundary_name: str | None = None):
        super().__init__()
        self.lon: list[float] = []
        self.lat: list[float] = []
        self.weight: list[float] = []
        self.kind: list[int] = []   # 0 = multi-unit, 1 = ground-oriented
        self.size: list[float] = []  # plan size (m), sqrt of footprint area
        self.boundary_name = boundary_name
        self.boundary = None  # shapely polygon
        self.n_seen = 0

    def way(self, w):
        b = w.tags.get("building")
        if b is None or b not in RESIDENTIAL:
            return
        if not w.is_closed():
            return
        pts = [(n.location.lon, n.location.lat) for n in w.nodes if n.location.valid()]
        if len(pts) < 4:
            return
        self.n_seen += 1
        a = np.asarray(pts)
        lon = float(a[:, 0].mean())
        lat = float(a[:, 1].mean())
        # Shoelace area in m^2 on a local equirectangular approximation.
        mx = (a[:, 0] - lon) * 111_320.0 * math.cos(math.radians(lat))
        my = (a[:, 1] - lat) * 110_540.0
        area = 0.5 * abs(np.dot(mx, np.roll(my, 1)) - np.dot(my, np.roll(mx, 1)))
        if not (10.0 <= area <= 60_000.0):
            return
        levels = w.tags.get("building:levels")
        n_levels = None
        if levels:
            try:
                n_levels = float(levels)
            except ValueError:
                n_levels = None
        if n_levels is None:
            n_levels = DEFAULT_LEVELS.get(b, 2)
            # Only ~half of Toronto's apartment buildings carry
            # building:levels, and defaulting the rest to a walk-up badly
            # under-weights the towers, which is exactly where the population
            # closest to arterials lives.  Footprint size separates the two:
            # a large plate is a tower, a small one a low-rise block.
            if b in ("apartments", "residential", "dormitory"):
                n_levels = 14.0 if area >= 700.0 else 4.0
        n_levels = min(max(n_levels, 1.0), 60.0)
        self.lon.append(lon)
        self.lat.append(lat)
        self.weight.append(area * n_levels)
        self.kind.append(0 if b in ("apartments", "residential", "dormitory") else 1)
        self.size.append(math.sqrt(area))

    def area(self, a):
        if self.boundary_name is None or self.boundary is not None:
            return
        t = a.tags
        if t.get("boundary") != "administrative" or t.get("name") != self.boundary_name:
            return
        if t.get("admin_level") not in ("6", "8"):
            return
        try:
            import shapely.geometry as sg
            rings = []
            for ring in a.outer_rings():
                pts = [(n.location.lon, n.location.lat) for n in ring if n.location.valid()]
                if len(pts) >= 4:
                    rings.append(sg.Polygon(pts))
            if rings:
                self.boundary = max(rings, key=lambda p: p.area)
        except Exception:
            pass


def load_buildings(pbf: str, boundary_name: str | None, cache: str):
    if os.path.exists(cache):
        d = np.load(cache, allow_pickle=True)
        return d["lon"], d["lat"], d["weight"], d["kind"], d["size"], (json.loads(str(d["bnd"])) if "bnd" in d.files else None)
    h = BuildingHandler(boundary_name)
    h.apply_file(pbf, locations=True, idx="flex_mem")
    lon = np.asarray(h.lon)
    lat = np.asarray(h.lat)
    weight = np.asarray(h.weight)
    bnd = None
    if h.boundary is not None:
        bnd = [list(c) for c in h.boundary.exterior.coords]
    kind = np.asarray(h.kind, dtype=np.int8)
    size = np.asarray(h.size, dtype=np.float32)
    np.savez_compressed(cache, lon=lon, lat=lat, weight=weight, kind=kind, size=size, bnd=json.dumps(bnd))
    return lon, lat, weight, kind, size, bnd


def sample(db: np.ndarray, valid: np.ndarray, grid, lon: np.ndarray, lat: np.ndarray,
           footprint_m: np.ndarray | None = None):
    """Sample the raster at each building.

    With `footprint_m` (the building's plan size in metres) this returns the
    MOST EXPOSED level over the building's footprint rather than the level at
    its centroid, by taking the raster maximum over a window the size of the
    building.  That is the quantity TPH's exposure table is built on - "sound
    levels predicted for 10x10 m parcels were used to assess levels on the most
    exposed facade for all buildings in Toronto" - and for a large building set
    back from an arterial it is several dB above the centroid value.
    """
    from scipy import ndimage
    px, py = model.lonlat_to_pixels(lon, lat, grid.zoom)
    ix = np.round(px - grid.px0).astype(np.int64)
    iy = np.round(py - grid.py0).astype(np.int64)
    ok = (ix >= 0) & (iy >= 0) & (ix < grid.width) & (iy < grid.height)
    out = np.full(lon.shape, np.nan, dtype=np.float64)

    masked = np.where(valid, db, np.nan)
    if footprint_m is None:
        radii = np.zeros(len(lon), dtype=np.int8)
    else:
        radii = np.clip(np.round(footprint_m / (2.0 * grid.cell_m)), 0, 4).astype(np.int8)

    for r in np.unique(radii):
        sel = ok & (radii == r)
        if not sel.any():
            continue
        src = masked if r == 0 else ndimage.maximum_filter(
            np.nan_to_num(masked, nan=-999.0), size=int(2 * r + 1), mode="nearest")
        v = src[iy[sel], ix[sel]].astype(np.float64)
        v[v < -100] = np.nan
        out[sel] = v
    return out


def weighted_share_above(levels: np.ndarray, weight: np.ndarray, threshold: float) -> float:
    ok = ~np.isnan(levels)
    if not ok.any():
        return float("nan")
    w = weight[ok]
    return 100.0 * float(w[levels[ok] >= threshold].sum() / w.sum())


def run(region_id: str, data_dir: str, work_dir: str, boundary_name: str | None):
    with open(os.path.join(HERE, "regions.json")) as f:
        regions = {r["id"]: r for r in json.load(f)}
    if region_id not in regions:
        raise SystemExit(f"unknown region {region_id}; have {sorted(regions)}")
    reg = regions[region_id]
    pbf = os.path.join(data_dir, reg["pbf"])
    npz = os.path.join(work_dir, f"{region_id}.npz")
    if not os.path.exists(npz):
        from . import extract
        os.makedirs(work_dir, exist_ok=True)
        print(f"extracting sources from {pbf} ...")
        extract.extract(pbf, npz)

    print(f"modelling {reg['name']} ...")
    res = model.compute(npz, log=lambda m: print(m))
    grid, valid = res["grid"], res["valid"]

    print("loading residential buildings ...")
    t0 = time.time()
    lon, lat, weight, kind, size, bnd = load_buildings(
        pbf, boundary_name, os.path.join(work_dir, f"{region_id}-buildings.npz"))
    print(f"  {len(lon):,} residential buildings ({time.time()-t0:.0f}s)"
          + (f", boundary '{boundary_name}' found" if bnd else ""))

    if bnd:
        import shapely.geometry as sg
        import shapely.vectorized as sv
        poly = sg.Polygon(bnd)
        try:
            inside = sv.contains(poly, lon, lat)
        except Exception:
            from shapely import points as _pts, contains as _contains
            inside = _contains(poly, _pts(lon, lat))
        print(f"  {int(inside.sum()):,} inside the {boundary_name} boundary")
        lon, lat, weight, kind, size = lon[inside], lat[inside], weight[inside], kind[inside], size[inside]

    rows = {}
    for period in ("day", "night"):
        rows[period] = sample(res["db"][period], valid, grid, lon, lat, size) + FACADE_DB

    print()
    print(f"=== {reg['name']}: modelled exposure at residential buildings ===")
    print(f"    population proxy: OSM residential floor area (footprint x storeys)")
    covered = ~np.isnan(rows["day"])
    print(f"    {int(covered.sum()):,} of {len(lon):,} buildings inside modelled coverage")
    for period in ("day", "night"):
        v = rows[period][~np.isnan(rows[period])]
        w = weight[~np.isnan(rows[period])]
        order = np.argsort(v)
        cw = np.cumsum(w[order]) / w.sum()
        pct = lambda q: float(v[order][np.searchsorted(cw, q)])
        print(f"    {period:5s}  pop-weighted mean {np.average(v, weights=w):5.1f} dB   "
              f"p10 {pct(0.10):4.1f}  median {pct(0.50):4.1f}  p90 {pct(0.90):4.1f}  max {v.max():4.1f}")

    if region_id == "toronto":
        print()
        print("=== vs Toronto Public Health 2017 (Table 9, traffic-noise-only surface) ===")
        print(f"    {'threshold':22s} {'this map':>10s} {'TPH 2017':>10s} {'diff':>8s}")
        diffs = []
        for (period, thr), tph in TPH_2017["traffic"].items():
            ours = weighted_share_above(rows[period], weight, thr)
            diffs.append(ours - tph)
            print(f"    {period + ' >= ' + str(thr) + ' dB':22s} {ours:9.1f}% {tph:9.1f}% {ours - tph:+7.1f}")
        print(f"    {'mean absolute gap':22s} {np.mean(np.abs(diffs)):9.1f} points")
        print()
        print("    TPH measured means (220 sites, deliberately road-biased, NOT a city mean):")
        print(f"      day {TPH_2017['measured_mean']['day']} dB, night {TPH_2017['measured_mean']['night']} dB")
        print("    TPH model's own skill vs those measurements:")
        for p, s in TPH_2017["model_skill"].items():
            print(f"      {p:5s} R2 {s['r2']:.2f}  RMSE {s['rmse']:.2f} dB")
        print(f"    source: {TPH_2017['url']}")

    return rows, weight


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--region", default="toronto")
    ap.add_argument("--data", default=os.path.join(ROOT, "data"))
    ap.add_argument("--work", default=os.path.join(ROOT, "data", "build"))
    ap.add_argument("--boundary", default=None,
                    help="administrative boundary name to clip to (default: Toronto for the toronto region)")
    a = ap.parse_args(argv)
    boundary = a.boundary
    if boundary is None and a.region == "toronto":
        boundary = "Toronto"
    run(a.region, a.data, a.work, boundary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
