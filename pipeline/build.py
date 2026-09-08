"""
End-to-end build: OSM extracts -> noise rasters -> one PMTiles archive + JSON
sidecars for the web app.

    python -m pipeline.build                      # everything in pipeline/regions.json
    python -m pipeline.build --only toronto,ottawa
    python -m pipeline.build --regions my-regions.json --data ./data --out ./web

Each region entry in regions.json:
    {"id": "toronto", "name": "Toronto", "pbf": "Toronto.osm.pbf",
     "center": [-79.38, 43.65], "zoom": 10}
The PBF path is relative to --data.  bbox is read from the PBF header (BBBike
and Geofabrik extracts carry one); add "bbox": [w, s, e, n] to override.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time

import numpy as np
import osmium

from . import config as C
from . import emission
from . import extract, model, tiles

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def _is_empty(npz_path: str) -> bool:
    """True when a region's extract contains nothing worth modelling.

    Regions derived automatically from a province extract can legitimately come
    up empty (a cluster that fell entirely inside a neighbouring region's halo,
    or a box over water), and an empty region must be skipped rather than
    crash the run.
    """
    d = np.load(npz_path)
    return len(d["road_levels"]) == 0 and len(d["runway_levels"]) == 0


def log(msg):
    print(msg, flush=True)


def pbf_timestamp(path: str) -> str | None:
    try:
        r = osmium.io.Reader(path)
        h = r.header()
        r.close()
        for key in ("osmosis_replication_timestamp", "timestamp"):
            v = h.get(key)
            if v:
                return v
    except Exception:
        pass
    return dt.datetime.fromtimestamp(os.path.getmtime(path), dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build(regions_path: str, data_dir: str, out_dir: str, only: set[str] | None = None,
          work_dir: str | None = None, force_extract: bool = False) -> dict:
    t_start = time.time()
    with open(regions_path) as f:
        regions = json.load(f)
    if only:
        regions = [r for r in regions if r["id"] in only]
    if not regions:
        raise SystemExit("no regions selected")

    work_dir = work_dir or os.path.join(data_dir, "build")
    os.makedirs(work_dir, exist_ok=True)
    tiles_dir = os.path.join(out_dir, "tiles")
    os.makedirs(tiles_dir, exist_ok=True)

    periods = list(C.PERIODS)
    stores = {}
    for period in periods:
        sp = os.path.join(work_dir, f"values-{period}.sqlite")
        if os.path.exists(sp):
            os.remove(sp)
        stores[period] = tiles.ValueStore(sp)

    region_out = []
    union = [180, 90, -180, -90]
    for reg in regions:
        t0 = time.time()
        log(f"[{reg['id']}] {reg['name']}")
        pbf = os.path.join(data_dir, reg["pbf"])
        if not os.path.exists(pbf):
            raise SystemExit(f"missing extract: {pbf}")
        npz = os.path.join(work_dir, f"{reg['id']}.npz")
        if force_extract or not os.path.exists(npz) or os.path.getmtime(npz) < os.path.getmtime(pbf):
            log("  extracting sources from OSM ...")
            # A region derived by pipeline/regionize.py carries its own bbox and
            # may share one province-sized extract with dozens of other
            # regions, so only the ways touching this box are read.  The bbox
            # already includes a halo (see regionize.HALO_DEG), and overlapping
            # regions resolve correctly because ValueStore.merge keeps the
            # louder of two values - an edge always UNDER-estimates, so the
            # region that owns the interior wins.
            meta = extract.extract(pbf, npz, bbox=reg.get("bbox"),
                                   clip=reg.get("bbox") if reg.get("clip", True) else None)
            log(f"  {meta['stats']} in {meta['seconds']}s")
        else:
            log("  using cached extraction")
        if _is_empty(npz):
            log("  no modelled sources in this region - skipping")
            continue
        res = model.compute(npz, log=log, periods=periods)
        g = res["grid"]
        for period in periods:
            counts = tiles.cut_tiles(stores[period], res["db"][period], res["valid"], g.px0, g.py0)
        log(f"  tiles per zoom: {counts} ({time.time() - t0:.0f}s)")

        d = np.load(npz)
        emeta = json.loads(str(d["meta"]))
        bbox = res["bbox"]
        union = [min(union[0], bbox[0]), min(union[1], bbox[1]), max(union[2], bbox[2]), max(union[3], bbox[3])]
        valid = res["valid"]
        dbv = res["db"]["day"][valid]
        nightv = res["db"]["night"][valid]
        region_out.append({
            "id": reg["id"],
            "name": reg["name"],
            "province": reg.get("province"),
            "bbox": [round(v, 4) for v in bbox],
            "center": reg.get("center") or [round((bbox[0] + bbox[2]) / 2, 4), round((bbox[1] + bbox[3]) / 2, 4)],
            "zoom": reg.get("zoom", 10),
            "cell_m": round(g.cell_m, 1),
            "osm_timestamp": pbf_timestamp(pbf),
            "sources": emeta["stats"],
            "airports": sorted({(r.get("airport"), r.get("iata"), r["class"]) for r in emeta["runways"] if r.get("airport")},
                               key=lambda t: (t[2], t[0] or "")),
            "stats": {
                "median_db": round(float(np.median(dbv)), 1),
                "p90_db": round(float(np.percentile(dbv, 90)), 1),
                "share_over_65": round(float((dbv >= 65).mean()), 3),
                "share_under_50": round(float((dbv < 50).mean()), 3),
                "median_db_night": round(float(np.median(nightv)), 1),
                "share_over_55_night": round(float((nightv >= 55).mean()), 3),
            },
        })
        del res, d
    lut = tiles.build_palette()
    attribution = "Noise model © Canada Noise Map contributors · Map data © OpenStreetMap contributors (ODbL)"
    period_out = {}
    size_mb = 0.0
    for period in periods:
        store = stores[period]
        store.commit()
        log(f"[{period}] value store: {store.count()} tiles")
        mbtiles_path = os.path.join(work_dir, f"canada-noise-{period}.mbtiles")
        rel = f"tiles/canada-noise-{period}.pmtiles"
        pmtiles_path = os.path.join(out_dir, rel)
        spec = C.PERIODS[period]
        tiles.write_mbtiles(store, lut, mbtiles_path, union,
                            {"name": f"Canada Noise Map ({spec['label']})", "attribution": attribution,
                             "description": f"Modelled {spec['label'].lower()} transportation noise "
                                            f"({spec['metric']}, {spec['hours']}) from OpenStreetMap "
                                            f"roads, railways and runways."},
                            log=log)
        store.close()
        tiles.write_pmtiles(mbtiles_path, pmtiles_path)
        mb = os.path.getsize(pmtiles_path) / 1e6
        size_mb += mb
        period_out[period] = {**spec, "tiles": rel, "mb": round(mb, 1)}
        log(f"wrote {pmtiles_path} ({mb:.1f} MB)")

    with open(os.path.join(out_dir, "regions.json"), "w") as f:
        json.dump(region_out, f, indent=1)
    with open(os.path.join(out_dir, "palette.json"), "w") as f:
        json.dump(tiles.palette_json(lut), f)
    meta = {
        "built_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "periods": period_out,
        "default_period": "day",
        "tiles_mb": round(size_mb, 1),
        "base_zoom": C.BASE_ZOOM,
        "min_zoom": C.MIN_ZOOM,
        "tile_format": C.TILE_FORMAT,
        "detail_min_zoom": C.DETAIL_MIN_ZOOM,
        "bounds": [round(v, 4) for v in union],
        "model": {
            # Road levels are derived from traffic, not tabulated, so the
            # traffic table and the resulting levels are both published here.
            "road_traffic": C.ROAD_TRAFFIC,
            "road_levels": {
                cls: {"day": round(d, 1), "night": round(n, 1)}
                for cls in C.ROAD_TRAFFIC
                for d, n in [emission.road_levels(cls, None, None)]
            },
            "emission_anchor": emission.ANCHOR,
            "rail_levels": C.RAIL_LEVELS,
            "runway_levels": C.RUNWAY_LEVELS,
            "road_kernel": C.ROAD_KERNEL,
            "air_kernel": C.AIR_KERNEL,
            "ref_dist_road_m": C.REF_DIST_ROAD_M,
            "ref_dist_air_m": C.REF_DIST_AIR_M,
            "nodata_distance_m": C.NODATA_DISTANCE_M,
            "rail_night_delta": C.RAIL_NIGHT_DELTA,
            "runway_night_delta": C.RUNWAY_NIGHT_DELTA,
        },
        "regions": [r["id"] for r in region_out],
        "build_seconds": round(time.time() - t_start),
    }
    with open(os.path.join(out_dir, "meta.json"), "w") as f:
        json.dump(meta, f, indent=1)
    log(f"done in {meta['build_seconds']}s")
    return meta


def main(argv=None):
    ap = argparse.ArgumentParser(description="Build the Canada Noise Map tileset")
    ap.add_argument("--regions", default=os.path.join(HERE, "regions.json"))
    ap.add_argument("--data", default=os.path.join(ROOT, "data"))
    ap.add_argument("--out", default=os.path.join(ROOT, "web"))
    ap.add_argument("--work", default=None, help="scratch dir (default: <data>/build)")
    ap.add_argument("--only", default=None, help="comma-separated region ids")
    ap.add_argument("--force-extract", action="store_true")
    a = ap.parse_args(argv)
    only = set(a.only.split(",")) if a.only else None
    build(a.regions, a.data, a.out, only=only, work_dir=a.work, force_extract=a.force_extract)


if __name__ == "__main__":
    sys.exit(main())
