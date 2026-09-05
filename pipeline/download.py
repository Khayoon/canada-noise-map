"""
Fetch the OpenStreetMap city extracts used by regions.json from BBBike.

    python -m pipeline.download            # all regions
    python -m pipeline.download toronto    # one region

BBBike (https://download.bbbike.org/osm/bbbike/) publishes weekly PBF extracts
for ~200 cities.  Each region's "pbf" file name is looked up under
https://download.bbbike.org/osm/bbbike/<City>/<City>.osm.pbf where <City> is
the file name without ".osm.pbf".  Any other .osm.pbf (Geofabrik, osmium
extract, ...) works too - just drop it in the data folder.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BASE = "https://download.bbbike.org/osm/bbbike/{city}/{city}.osm.pbf"


def download(url: str, dest: str) -> None:
    tmp = dest + ".part"
    req = urllib.request.Request(url, headers={"User-Agent": "canada-noise-map/1.0 (+https://github.com/Khayoon/canada-noise-map)"})
    with urllib.request.urlopen(req) as r, open(tmp, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if total:
                print(f"\r  {done / 1e6:6.1f} / {total / 1e6:.1f} MB", end="", flush=True)
    print()
    os.replace(tmp, dest)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    with open(os.path.join(HERE, "regions.json")) as f:
        regions = json.load(f)
    if argv:
        regions = [r for r in regions if r["id"] in argv]
    data_dir = os.path.join(ROOT, "data")
    os.makedirs(data_dir, exist_ok=True)
    for r in regions:
        dest = os.path.join(data_dir, r["pbf"])
        if os.path.exists(dest):
            print(f"{r['pbf']}: already present, skipping")
            continue
        city = r["pbf"].replace(".osm.pbf", "")
        url = r.get("url") or BASE.format(city=city)
        print(f"{r['pbf']}: downloading {url}")
        download(url, dest)


if __name__ == "__main__":
    main()
