"""
Cut the dB raster into XYZ tiles, merge regions, and write a PMTiles archive.

Tiles are stored twice on the way out:

1. A small SQLite "value store" holding the raw uint8 dB*2 values per tile
   (0 = no data).  Regions are merged here (cell-wise maximum) so overlapping
   low-zoom tiles from neighbouring cities end up in one tile.
2. The final MBTiles/PMTiles with paletted PNGs.  The palette is fixed at
   build time and written to web/palette.json so the front end can invert it
   and read decibel values back out of the pixels.
"""
from __future__ import annotations

import io
import json
import sqlite3
import zlib

import numpy as np
from PIL import Image
from pmtiles.convert import mbtiles_to_pmtiles

from . import config as C

VALUE_SCALE = 2  # stored value = round(dB * 2); 0 reserved for no data


# ---------------------------------------------------------------------------
# Palette
# ---------------------------------------------------------------------------

def _hex(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def build_palette() -> np.ndarray:
    """256x4 uint8 RGBA lookup table indexed by stored value; every used entry unique."""
    stops = [(db, _hex(col)) for db, col in C.PALETTE_STOPS]
    xs = np.array([s[0] for s in stops], dtype=np.float64)
    cols = np.array([s[1] for s in stops], dtype=np.float64)
    lut = np.zeros((256, 4), dtype=np.uint8)
    db = np.arange(256) / VALUE_SCALE
    for ch in range(3):
        lut[:, ch] = np.clip(np.round(np.interp(db, xs, cols[:, ch])), 0, 255)
    lut[:, 3] = 255
    lut[0] = 0  # no data -> transparent
    # Make every entry in the displayable range unique so the palette inverts.
    seen = {}
    lo = int(C.DISPLAY_FLOOR_DB * VALUE_SCALE)
    hi = int(C.DISPLAY_CEIL_DB * VALUE_SCALE)
    for i in range(lo, hi + 1):
        key = tuple(int(v) for v in lut[i, :3])
        bump = 0
        while key in seen:
            bump += 1
            # nudge the blue channel by one step, wrapping in a tiny range
            b = (int(lut[i, 2]) + bump) % 256
            key = (int(lut[i, 0]), int(lut[i, 1]), b)
        lut[i, 2] = key[2]
        seen[key] = i
    return lut


def palette_json(lut: np.ndarray) -> dict:
    return {
        "scale": VALUE_SCALE,
        "floor_db": C.DISPLAY_FLOOR_DB,
        "ceil_db": C.DISPLAY_CEIL_DB,
        "stops": [{"db": d, "color": c} for d, c in C.PALETTE_STOPS],
        "categories": [{"max_db": m, "label": l, "blurb": b} for m, l, b in C.CATEGORIES],
        "lut": [[int(v) for v in row[:3]] for row in lut],
    }


# ---------------------------------------------------------------------------
# Value store (intermediate)
# ---------------------------------------------------------------------------

class ValueStore:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path)
        self.conn.execute("CREATE TABLE IF NOT EXISTS v (z INTEGER, x INTEGER, y INTEGER, data BLOB, PRIMARY KEY (z, x, y))")
        self.conn.execute("PRAGMA journal_mode=OFF")
        self.conn.execute("PRAGMA synchronous=OFF")

    def get(self, z, x, y):
        row = self.conn.execute("SELECT data FROM v WHERE z=? AND x=? AND y=?", (z, x, y)).fetchone()
        if row is None:
            return None
        return np.frombuffer(zlib.decompress(row[0]), dtype=np.uint8).reshape(C.TILE_SIZE, C.TILE_SIZE)

    def merge(self, z, x, y, values: np.ndarray):
        old = self.get(z, x, y)
        if old is not None:
            values = np.maximum(old, values)
        blob = zlib.compress(values.tobytes(), 6)
        self.conn.execute("INSERT OR REPLACE INTO v (z, x, y, data) VALUES (?, ?, ?, ?)", (z, x, y, blob))

    def tiles(self):
        for z, x, y in self.conn.execute("SELECT z, x, y FROM v ORDER BY z, x, y"):
            yield z, x, y, self.get(z, x, y)

    def count(self):
        return self.conn.execute("SELECT COUNT(*) FROM v").fetchone()[0]

    def commit(self):
        self.conn.commit()

    def close(self):
        self.conn.commit()
        self.conn.close()


# ---------------------------------------------------------------------------
# Pyramid
# ---------------------------------------------------------------------------

def encode_values(db: np.ndarray, valid: np.ndarray) -> np.ndarray:
    v = np.round(np.clip(db, 0, 127.5) * VALUE_SCALE).astype(np.uint8)
    v = np.maximum(v, 1)  # never let a valid cell collide with the nodata code
    v[~valid] = 0
    return v


def downsample(db: np.ndarray, valid: np.ndarray):
    """2x2 energy-mean downsample, honouring the validity mask."""
    H, W = db.shape
    if H % 2 or W % 2:
        db = np.pad(db, ((0, H % 2), (0, W % 2)))
        valid = np.pad(valid, ((0, H % 2), (0, W % 2)))
        H, W = db.shape
    e = np.where(valid, 10.0 ** (db / 10.0), 0.0).astype(np.float64)
    e = e.reshape(H // 2, 2, W // 2, 2).sum(axis=(1, 3))
    n = valid.reshape(H // 2, 2, W // 2, 2).sum(axis=(1, 3))
    v2 = n > 0
    with np.errstate(divide="ignore", invalid="ignore"):
        db2 = np.where(v2, 10.0 * np.log10(e / np.maximum(n, 1)), 0.0).astype(np.float32)
    return db2, v2


def cut_tiles(store: ValueStore, db: np.ndarray, valid: np.ndarray, px0: int, py0: int,
              base_zoom: int = C.BASE_ZOOM, min_zoom: int = C.MIN_ZOOM) -> dict:
    """Write tiles for every zoom from base_zoom down to min_zoom into the store."""
    T = C.TILE_SIZE
    counts = {}
    ox, oy = px0, py0
    for z in range(base_zoom, min_zoom - 1, -1):
        values = encode_values(db, valid)
        H, W = values.shape
        tx0, tx1 = ox // T, (ox + W - 1) // T
        ty0, ty1 = oy // T, (oy + H - 1) // T
        n = 0
        for ty in range(ty0, ty1 + 1):
            for tx in range(tx0, tx1 + 1):
                # window of this tile in array coordinates
                a0, b0 = ty * T - oy, tx * T - ox
                a1, b1 = a0 + T, b0 + T
                sa0, sb0 = max(a0, 0), max(b0, 0)
                sa1, sb1 = min(a1, H), min(b1, W)
                if sa0 >= sa1 or sb0 >= sb1:
                    continue
                sub = values[sa0:sa1, sb0:sb1]
                if not sub.any():
                    continue
                tile = np.zeros((T, T), dtype=np.uint8)
                tile[sa0 - a0:sa1 - a0, sb0 - b0:sb1 - b0] = sub
                store.merge(z, tx, ty, tile)
                n += 1
        counts[z] = n
        store.commit()
        if z > min_zoom:
            db, valid = downsample(db, valid)
            ox //= 2
            oy //= 2
    return counts


# ---------------------------------------------------------------------------
# PNG / MBTiles / PMTiles
# ---------------------------------------------------------------------------

def values_to_png(values: np.ndarray, lut: np.ndarray) -> bytes:
    img = Image.fromarray(values, mode="P")
    img.putpalette(lut[:, :3].astype(np.uint8).tobytes(), rawmode="RGB")
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True, transparency=0)
    return buf.getvalue()


def write_mbtiles(store: ValueStore, lut: np.ndarray, out_path: str, bounds, metadata: dict, log=print):
    import os
    if os.path.exists(out_path):
        os.remove(out_path)
    conn = sqlite3.connect(out_path)
    conn.execute("CREATE TABLE metadata (name TEXT, value TEXT)")
    conn.execute("CREATE TABLE tiles (zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, tile_data BLOB)")
    conn.execute("CREATE UNIQUE INDEX tile_index ON tiles (zoom_level, tile_column, tile_row)")
    zooms = set()
    n = 0
    total_bytes = 0
    for z, x, y, values in store.tiles():
        png = values_to_png(values, lut)
        tms_y = (1 << z) - 1 - y
        conn.execute("INSERT INTO tiles VALUES (?, ?, ?, ?)", (z, x, tms_y, sqlite3.Binary(png)))
        zooms.add(z)
        n += 1
        total_bytes += len(png)
        if n % 2000 == 0:
            conn.commit()
    meta = {
        "name": metadata.get("name", "Canada Noise Map"),
        "format": "png",
        "type": "overlay",
        "version": "1",
        "description": metadata.get("description", ""),
        "attribution": metadata.get("attribution", ""),
        "bounds": ",".join(f"{v:.5f}" for v in bounds),
        "center": f"{(bounds[0] + bounds[2]) / 2:.5f},{(bounds[1] + bounds[3]) / 2:.5f},10",
        "minzoom": str(min(zooms)) if zooms else "0",
        "maxzoom": str(max(zooms)) if zooms else "0",
    }
    conn.executemany("INSERT INTO metadata VALUES (?, ?)", list(meta.items()))
    conn.commit()
    conn.close()
    log(f"  wrote {n} PNG tiles, {total_bytes / 1e6:.1f} MB of PNG data")
    return n


def write_pmtiles(mbtiles_path: str, pmtiles_path: str):
    import os
    if os.path.exists(pmtiles_path):
        os.remove(pmtiles_path)
    mbtiles_to_pmtiles(mbtiles_path, pmtiles_path, None)
