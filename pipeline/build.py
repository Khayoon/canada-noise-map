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
from . import extract, model, tiles

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


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

    store_path = os.path.join(work_dir, "values.sqlite")
    if os.path.exists(store_path):
        os.remove(store_path)
    store = tiles.ValueStore(store_path)

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
            meta = extract.extract(pbf, npz, bbox=reg.get("bbox"))
            log(f"  {meta['stats']} in {meta['seconds']}s")
        else:
            log("  using cached extraction")
        res = model.compute(npz, log=log)
        g = res["grid"]
        counts = tiles.cut_tiles(store, res["db"], res["valid"], g.px0, g.py0)
        log(f"  tiles per zoom: {counts} ({time.time() - t0:.0f}s)")

        d = np.load(npz)
        emeta = json.loads(str(d["meta"]))
        bbox = res["bbox"]
        union = [min(union[0], bbox[0]), min(union[1], bbox[1]), max(union[2], bbox[2]), max(union[3], bbox[3])]
        valid = res["valid"]
        dbv = res["db"][valid]
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
            },
        })
        del res, d
    store.commit()
    log(f"value store: {store.count()} tiles")

    lut = tiles.build_palette()
    mbtiles_path = os.path.join(work_dir, "canada-noise.mbtiles")
    pmtiles_path = os.path.join(tiles_dir, "canada-noise.pmtiles")
    attribution = "Noise model © Canada Noise Map contributors · Map data © OpenStreetMap contributors (ODbL)"
    tiles.write_mbtiles(store, lut, mbtiles_path, union,
                        {"name": "Canada Noise Map", "attribution": attribution,
                         "description": "Modelled daytime transportation noise (LAeq, dB) from OpenStreetMap roads, railways and runways."},
                        log=log)
    store.close()
    tiles.write_pmtiles(mbtiles_path, pmtiles_path)
    size_mb = os.path.getsize(pmtiles_path) / 1e6
    log(f"wrote {pmtiles_path} ({size_mb:.1f} MB)")

    with open(os.path.join(out_dir, "regions.json"), "w") as f:
        json.dump(region_out, f, indent=1)
    with open(os.path.join(out_dir, "palette.json"), "w") as f:
        json.dump(tiles.palette_json(lut), f)
    meta = {
        "built_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "tiles": "tiles/canada-noise.pmtiles",
        "tiles_mb": round(size_mb, 1),
        "base_zoom": C.BASE_ZOOM,
        "min_zoom": C.MIN_ZOOM,
        "bounds": [round(v, 4) for v in union],
        "model": {
            "road_classes": C.ROAD_CLASSES,
            "rail_levels": C.RAIL_LEVELS,
            "runway_levels": C.RUNWAY_LEVELS,
            "road_kernel": C.ROAD_KERNEL,
            "air_kernel": C.AIR_KERNEL,
            "ref_dist_road_m": C.REF_DIST_ROAD_M,
            "ref_dist_air_m": C.REF_DIST_AIR_M,
            "nodata_distance_m": C.NODATA_DISTANCE_M,
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
