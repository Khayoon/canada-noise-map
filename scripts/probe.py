#!/usr/bin/env python3
"""
Read modelled noise levels straight out of the PMTiles archive — the same
thing the web page does when you click, but from the command line.

    python scripts/probe.py -79.3910 43.6385            # lon lat, day and night
    python scripts/probe.py -79.3910 43.6385 -73.567 45.502 ...
    python scripts/probe.py --period night --json -79.39 43.64
"""
import argparse
import io
import json
import math
import os
import sys

from PIL import Image
from pmtiles.reader import MmapSource, Reader

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def lnglat_to_tile(lng, lat, z):
    n = 256 * 2 ** z
    x = (lng + 180) / 360 * n
    latr = math.radians(lat)
    y = (1 - math.log(math.tan(latr) + 1 / math.cos(latr)) / math.pi) / 2 * n
    return int(x // 256), int(y // 256), int(x) % 256, int(y) % 256


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("coords", nargs="+", type=float, help="lon lat pairs")
    ap.add_argument("--tiles", default=None, help="a specific .pmtiles archive")
    ap.add_argument("--period", default=None, choices=["day", "night"],
                    help="read only this period (default: both)")
    ap.add_argument("--meta", default=os.path.join(ROOT, "web", "meta.json"))
    ap.add_argument("--palette", default=os.path.join(ROOT, "web", "palette.json"))
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    if len(a.coords) % 2:
        sys.exit("give lon lat pairs")
    pal = json.load(open(a.palette))
    inv = {tuple(rgb): i for i, rgb in enumerate(pal["lut"]) if i > 0}
    cats = pal["categories"]

    # One archive per period since day/night support landed; meta.json names them.
    if a.tiles:
        archives = {"": a.tiles}
    else:
        meta = json.load(open(a.meta))
        archives = {name: os.path.join(ROOT, "web", spec["tiles"])
                    for name, spec in meta["periods"].items()
                    if a.period is None or name == a.period}

    def read(path, lng, lat):
        with open(path, "rb") as f:
            reader = Reader(MmapSource(f))
            # Sparse country is only stored to a coarser zoom, so walk down.
            for z in range(reader.header()["max_zoom"], reader.header()["min_zoom"] - 1, -1):
                tx, ty, px, py = lnglat_to_tile(lng, lat, z)
                data = reader.get(z, tx, ty)
                if not data:
                    continue
                img = Image.open(io.BytesIO(data)).convert("RGBA")
                r, g, b, alpha = img.getpixel((px, py))
                if not alpha:
                    return None
                idx = inv.get((r, g, b))
                return idx / pal["scale"] if idx else None
        return None

    out = []
    for lng, lat in zip(a.coords[::2], a.coords[1::2]):
        rec = {"lon": lng, "lat": lat}
        for name, path in archives.items():
            db = read(path, lng, lat)
            label = next((c["label"] for c in cats if db is not None and db < c["max_db"]), None)
            rec[name or "db"] = db
            if name:
                rec[name + "_category"] = label
            else:
                rec["category"] = label
        out.append(rec)
        if not a.json:
            parts = []
            for name in archives:
                v = rec[name or "db"]
                parts.append(f"{name or 'db'} {v:5.1f}" if v is not None else f"{name or 'db'}  --  ")
            print(f"{lng:10.5f} {lat:9.5f}  ->  " + "  ".join(parts) + " dB(A)")
    if a.json:
        print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
