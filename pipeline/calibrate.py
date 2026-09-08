"""
Sensitivity sweep for the two modelling choices that control the SPREAD of the
noise surface, checked against Toronto Public Health's population-exposure
table (see pipeline/validate.py for why that is the comparable quantity).

The first validation run matched TPH closely at the low thresholds (day >=55,
night >=45) but fell ~20 points short at day >=65.  That signature means the
surface is too compressed: the loud band around busy roads is too narrow
and/or too quiet, while the quiet background is about right.  Two model
choices govern that, and both were set by assumption rather than by evidence:

  1. ROAD_KERNEL["exponent"] p - the point-source decay exponent, which after
     integration along a line gives d^(1-p).  p = 2.5 is the classic
     soft-ground value (4.5 dB per doubling of distance).  Over the hard,
     partly reflecting ground of a built-up city, ISO 9613-2 - the propagation
     standard TPH used - attenuates much less at short range, closer to plain
     geometric line spreading (3 dB per doubling, p = 2).

  2. The assumed AADT on arterial classes.  Toronto's major arterials
     (Yonge, Bloor, Dufferin, Lake Shore) carry well over the 16,000 the
     primary class was given.

A third correction is not a free parameter at all but a convention: exposure
statistics in the END/CNOSSOS tradition, and in the TPH study, are evaluated
at the most exposed building FACADE, which includes the reflection off the
wall behind the receiver - about +3 dB relative to a free-field level.  Our
raster is free-field, which is the right thing for a map you can stand
anywhere on, so the facade correction is applied when computing exposure
statistics rather than baked into the tiles.

    python -m pipeline.calibrate
"""
from __future__ import annotations

import argparse
import copy
import itertools
import os
import sys

import numpy as np

from . import config as C
from . import model
from . import validate as V

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# Sound level added to a free-field level to represent a facade assessment
# point (reflection off the building wall behind the receiver).
FACADE_DB = 3.0


def score(rows, weight, facade_db=FACADE_DB):
    """Mean absolute gap, in percentage points, against the TPH traffic table."""
    diffs = {}
    for (period, thr), tph in V.TPH_2017["traffic"].items():
        ours = V.weighted_share_above(rows[period] + facade_db, weight, thr)
        diffs[(period, thr)] = ours - tph
    return float(np.mean([abs(v) for v in diffs.values()])), diffs


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=os.path.join(ROOT, "data"))
    ap.add_argument("--work", default=os.path.join(ROOT, "data", "build"))
    a = ap.parse_args(argv)

    npz = os.path.join(a.work, "toronto.npz")
    pbf = os.path.join(a.data, "Toronto.osm.pbf")
    lon, lat, weight, kind, size, bnd = V.load_buildings(
        pbf, "Toronto", os.path.join(a.work, "toronto-buildings.npz"))
    if bnd:
        import shapely.geometry as sg
        from shapely import points as _pts, contains as _contains
        poly = sg.Polygon(bnd)
        inside = _contains(poly, _pts(lon, lat))
        lon, lat, weight, size = lon[inside], lat[inside], weight[inside], size[inside]
    print(f"{len(lon):,} residential buildings inside the City of Toronto\n")

    base_traffic = copy.deepcopy(C.ROAD_TRAFFIC)
    base_h = C.ROAD_KERNEL["height_m"]

    # The near-field softening height h in K(d) = (d^2 + h^2)^(-p/2) sets how
    # sharply the level peaks on top of the source.  It stands in for the
    # effective source height, and 12 m - the original guess - is unphysically
    # high for a road: tyre noise radiates from ~0.01 m and exhaust/engine
    # noise from ~0.3 m (light) to ~4 m (heavy).  A smaller h raises levels for
    # dwellings that front directly onto a busy road without touching the far
    # field, which is exactly the part of the distribution that is short.
    # Calibration keeps L_ref at 15 m fixed as h changes, so this reshapes the
    # curve rather than moving it.
    base_abs = C.ROAD_KERNEL["absorption_db_per_km"]

    # The dominant missing physic is shielding: with no buildings in the model,
    # noise from an arterial washes across the whole neighbourhood behind it at
    # a moderate level, which piles population into the 55-65 dB band and
    # starves both tails.  ISO 9613-2 - the propagation standard TPH used -
    # carries an explicit attenuation term for sound propagating THROUGH
    # housing (its A_hous term, worth up to about 10 dB across a built-up
    # path).  Our kernel is shift-invariant so it cannot trace a path, but the
    # per-kilometre absorption coefficient is the right place to represent that
    # clutter on average: it leaves short paths (a dwelling fronting the road)
    # almost untouched while cutting long paths through housing.
    absorptions = [2.0, 5.0, 8.0, 12.0]
    heights = [8.0, 4.0, 2.0]

    keys = list(V.TPH_2017["traffic"])
    print(f"{'a dB/km':>8s} {'h_m':>5s} | "
          + " ".join(f"{p[0]}>={t}" for p, t in keys)
          + f" | {'mean |gap|':>10s}")
    results = []
    from . import extract
    tmp = os.path.join(a.work, "_cal1.0.npz")
    if not os.path.exists(tmp):
        extract.extract(pbf, tmp)
    for alpha, h in itertools.product(absorptions, heights):
        C.ROAD_KERNEL["absorption_db_per_km"] = alpha
        C.ROAD_KERNEL["height_m"] = h
        res = model.compute(tmp, log=lambda m: None)
        rows = {per: V.sample(res["db"][per], res["valid"], res["grid"], lon, lat, size)
                for per in ("day", "night")}
        s, diffs = score(rows, weight)
        cells = " ".join(f"{diffs[k]:+6.1f}" for k in keys)
        print(f"{alpha:8.1f} {h:5.1f} | {cells} | {s:10.2f}")
        results.append((s, alpha, h))
        del res, rows

    results.sort()
    print(f"\nbest by mean absolute gap: absorption={results[0][1]} dB/km, "
          f"h={results[0][2]} m ({results[0][0]:.2f} points)")
    C.ROAD_KERNEL["height_m"] = base_h
    C.ROAD_KERNEL["absorption_db_per_km"] = base_abs
    for cls in base_traffic:
        C.ROAD_TRAFFIC[cls]["aadt"] = base_traffic[cls]["aadt"]
    return 0


if __name__ == "__main__":
    sys.exit(main())
