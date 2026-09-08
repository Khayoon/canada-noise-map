"""
Work out what to build from the data itself.

The eight-city version of this project used a hand-written regions.json, one
entry per city, because BBBike only publishes extracts for a curated list of
cities.  That list - not OpenStreetMap - was the thing limiting coverage:
Geofabrik publishes every Canadian province, and a province extract contains
every road in it.

Scaling to the whole country therefore does not need a different kind of
pipeline, just more regions.  What it does need is a way to decide WHERE the
regions go, because writing out a box for every populated place in Canada by
hand is both tedious and a good way to silently miss towns.

So this module derives the region list from the extract:

  1. stream the .osm.pbf once, counting road nodes into a coarse lon/lat
     histogram (about 5 km cells).  Streaming keeps memory flat, so a
     province-sized file costs no more than a city one;
  2. threshold that histogram to find cells with enough road to be worth
     modelling, and dilate slightly so a town and its approaches stay together;
  3. label connected components - these are the populated clusters;
  4. emit one region per cluster, splitting any cluster bigger than
     MAX_REGION_DEG into a grid so no single raster gets unreasonably large,
     and padding each box by HALO_DEG so sources just outside a box still
     influence its edge.

The result is a regions.json in exactly the format the existing build already
consumes, so pipeline/build.py needs no special "national mode": it is the
same per-region loop, just with more regions in it.

    python -m pipeline.regionize data/ontario-latest.osm.pbf --out pipeline/regions-on.json
    python -m pipeline.regionize data/*.osm.pbf --out pipeline/regions-canada.json
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

# Histogram resolution in degrees (~5 km of longitude at 50 N).
CELL_DEG = 0.05

# A cell needs at least this many road nodes to count as worth modelling.
# Low enough to catch a village, high enough to ignore a lone forestry track.
MIN_NODES_PER_CELL = 40

# Grow each populated blob by this many cells so approaches, ring roads and
# the quiet fringe of a town come along with it.
DILATE_CELLS = 3

# No single region may span more than this many degrees; bigger clusters (the
# Toronto-Hamilton-Oshawa belt, the Montreal plain) are split into a grid.
# 0.9 deg of longitude at 45 N is about 70 km, which at zoom 13 is a raster of
# roughly 5000 x 7000 cells - the same order as the current Toronto build.
MAX_REGION_DEG = 0.9

# Sources just outside a region still reach into it, so every box is padded.
# The kernel radius is 2 km for roads and 12 km for runways; 0.05 deg (~4 km)
# covers the road case, which is what sets visible edge artefacts.
HALO_DEG = 0.05

# Ignore clusters smaller than this many populated cells (isolated highway
# junctions, single farms) - they are not worth a region of their own.
MIN_CLUSTER_CELLS = 2


class DensityHandler(osmium.SimpleHandler):
    """Count nodes belonging to modelled roads into a coarse lon/lat grid.

    Only ways carrying a highway tag we model are counted, so forest tracks,
    footpaths and building outlines do not create phantom towns.  Node
    locations come from the way itself, which means a single streaming pass
    with a location cache and no second index.
    """

    def __init__(self):
        super().__init__()
        self.counts: dict[tuple[int, int], int] = {}
        self.ways = 0

    def way(self, w):
        cls = w.tags.get("highway")
        if cls is None or cls not in C.ROAD_TRAFFIC:
            return
        # Sub-arterial classes are what actually mark habitation; motorways
        # alone would string regions along empty inter-city corridors.
        self.ways += 1
        counts = self.counts
        for n in w.nodes:
            if not n.location.valid():
                continue
            key = (int(math.floor(n.location.lat / CELL_DEG)),
                   int(math.floor(n.location.lon / CELL_DEG)))
            counts[key] = counts.get(key, 0) + 1


def density_grid(pbf_paths: list[str], log=print):
    """Merged road-node density histogram across every supplied extract."""
    merged: dict[tuple[int, int], int] = {}
    for path in pbf_paths:
        t0 = time.time()
        h = DensityHandler()
        h.apply_file(path, locations=True, idx="flex_mem")
        for k, v in h.counts.items():
            merged[k] = merged.get(k, 0) + v
        log(f"  {os.path.basename(path)}: {h.ways:,} modelled ways, "
            f"{len(h.counts):,} populated cells ({time.time() - t0:.0f}s)")
    return merged


def clusters_from_counts(counts: dict[tuple[int, int], int], log=print):
    """Populated cells -> labelled connected components on a dense array."""
    from scipy import ndimage
    rows = np.array([k[0] for k in counts])
    cols = np.array([k[1] for k in counts])
    vals = np.array(list(counts.values()))
    keep = vals >= MIN_NODES_PER_CELL
    rows, cols = rows[keep], cols[keep]
    if len(rows) == 0:
        raise SystemExit("no cells passed the density threshold - is this an OSM extract with roads?")
    r0, c0 = rows.min(), cols.min()
    grid = np.zeros((rows.max() - r0 + 1 + 2 * DILATE_CELLS * 2,
                     cols.max() - c0 + 1 + 2 * DILATE_CELLS * 2), dtype=bool)
    grid[rows - r0 + DILATE_CELLS * 2, cols - c0 + DILATE_CELLS * 2] = True
    log(f"  {int(grid.sum()):,} cells above threshold")
    grid = ndimage.binary_dilation(grid, iterations=DILATE_CELLS)
    labels, n = ndimage.label(grid, structure=np.ones((3, 3), dtype=bool))
    log(f"  {n} populated clusters after dilation")
    return labels, n, r0 - DILATE_CELLS * 2, c0 - DILATE_CELLS * 2


def split_box(west, south, east, north):
    """Yield sub-boxes no larger than MAX_REGION_DEG on a side."""
    nx = max(1, int(math.ceil((east - west) / MAX_REGION_DEG)))
    ny = max(1, int(math.ceil((north - south) / MAX_REGION_DEG)))
    dx = (east - west) / nx
    dy = (north - south) / ny
    for i in range(nx):
        for j in range(ny):
            yield (west + i * dx, south + j * dy, west + (i + 1) * dx, south + (j + 1) * dy)


def regions_from_pbfs(pbf_paths: list[str], log=print) -> list[dict]:
    counts = density_grid(pbf_paths, log=log)
    labels, n, row_off, col_off = clusters_from_counts(counts, log=log)
    from scipy import ndimage

    regions = []
    objects = ndimage.find_objects(labels)
    sizes = ndimage.sum(np.ones_like(labels, dtype=bool), labels, range(1, n + 1))
    order = np.argsort(-sizes)  # biggest clusters first, so ids are stable-ish
    pbf_name = os.path.basename(pbf_paths[0]) if len(pbf_paths) == 1 else None

    for idx in order:
        if sizes[idx] < MIN_CLUSTER_CELLS:
            continue
        sl_r, sl_c = objects[idx]
        south = (sl_r.start + row_off) * CELL_DEG
        north = (sl_r.stop + row_off) * CELL_DEG
        west = (sl_c.start + col_off) * CELL_DEG
        east = (sl_c.stop + col_off) * CELL_DEG
        for (w, s, e, nn) in split_box(west, south, east, north):
            regions.append({
                "bbox": [round(w - HALO_DEG, 4), round(s - HALO_DEG, 4),
                         round(e + HALO_DEG, 4), round(nn + HALO_DEG, 4)],
                "cells": int(sizes[idx]),
            })

    # Name and id them by size rank; the build only needs id/name/bbox/pbf.
    out = []
    for i, r in enumerate(regions):
        w, s, e, nn = r["bbox"]
        out.append({
            "id": f"r{i:04d}",
            "name": f"{(s + nn) / 2:.2f}N {(w + e) / 2:.2f}E",
            "bbox": r["bbox"],
            "center": [round((w + e) / 2, 4), round((s + nn) / 2, 4)],
            "zoom": 11,
            **({"pbf": pbf_name} if pbf_name else {}),
        })
    log(f"  {len(out)} build regions "
        f"(clusters split at {MAX_REGION_DEG} deg, {HALO_DEG} deg halo)")
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="Derive build regions from OSM extracts")
    ap.add_argument("pbf", nargs="+")
    ap.add_argument("--out", required=True)
    ap.add_argument("--max-deg", type=float, default=MAX_REGION_DEG)
    ap.add_argument("--min-nodes", type=int, default=MIN_NODES_PER_CELL)
    a = ap.parse_args(argv)

    globals()["MAX_REGION_DEG"] = a.max_deg
    globals()["MIN_NODES_PER_CELL"] = a.min_nodes

    regions = regions_from_pbfs(a.pbf)
    with open(a.out, "w") as f:
        json.dump(regions, f, indent=1)
    print(f"wrote {a.out} ({len(regions)} regions)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
