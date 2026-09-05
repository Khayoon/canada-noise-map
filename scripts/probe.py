#!/usr/bin/env python3
"""
Read modelled noise levels straight out of the PMTiles archive — the same
thing the web page does when you click, but from the command line.

    python scripts/probe.py -79.3910 43.6385            # lon lat
    python scripts/probe.py -79.3910 43.6385 -73.567 45.502 ...
    python scripts/probe.py --tiles web/tiles/canada-noise.pmtiles --json -79.39 43.64
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
    ap.add_argument("--tiles", default=os.path.join(ROOT, "web", "tiles", "canada-noise.pmtiles"))
    ap.add_argument("--palette", default=os.path.join(ROOT, "web", "palette.json"))
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    if len(a.coords) % 2:
        sys.exit("give lon lat pairs")
    pal = json.load(open(a.palette))
    inv = {tuple(rgb): i for i, rgb in enumerate(pal["lut"]) if i > 0}
    cats = pal["categories"]
    with open(a.tiles, "rb") as f:
        reader = Reader(MmapSource(f))
        z = reader.header()["max_zoom"]
        out = []
        for lng, lat in zip(a.coords[::2], a.coords[1::2]):
            tx, ty, px, py = lnglat_to_tile(lng, lat, z)
            data = reader.get(z, tx, ty)
            db = None
            if data:
                img = Image.open(io.BytesIO(data)).convert("RGBA")
                r, g, b, alpha = img.getpixel((px, py))
                if alpha:
                    db = inv.get((r, g, b))
                    db = db / pal["scale"] if db else None
            label = next((c["label"] for c in cats if db is not None and db < c["max_db"]), None)
            out.append({"lon": lng, "lat": lat, "db": db, "category": label})
            if not a.json:
                print(f"{lng:10.5f} {lat:9.5f}  ->  " + (f"{db:5.1f} dB(A)  {label}" if db is not None else "no data"))
        if a.json:
            print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
