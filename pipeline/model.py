"""
Turn extracted sources into a decibel raster on the Web-Mercator grid.

The physics, briefly:

* Every source polyline is burned into an "emission energy" raster.  Each
  burned cell is a point source whose energy is calibrated so that an infinite
  straight line of such cells reproduces the class emission level L_ref at the
  reference distance (15 m for roads/rail, 1 km for runways).
* The energy raster is convolved with a distance kernel
      K(d) = (d^2 + h^2)^(-p/2) * 10^(-alpha*d/10)
  which, integrated along a line, yields the familiar d^(1-p) line-source
  decay (p = 2.5 -> 4.5 dB per doubling of distance over soft ground).
* Roads and rail share a kernel and a single FFT convolution; runways use a
  longer-range kernel applied directly (there are only a few hundred cells).
* Everything is summed in the energy domain and converted back to dB.

The raster is aligned to the zoom-`BASE_ZOOM` tile grid so tiles can be cut
without any resampling.
"""
from __future__ import annotations

import math
import time

import numpy as np
import shapely
from rasterio import Affine
from rasterio.enums import MergeAlg
from rasterio.features import rasterize
from scipy import ndimage, signal, special

from . import config as C

EARTH_CIRCUMFERENCE = 2 * math.pi * 6378137.0  # metres, EPSG:3857 world width


def lonlat_to_pixels(lon, lat, zoom):
    """Web-Mercator pixel coordinates (x right, y down) at the given zoom."""
    n = C.TILE_SIZE * (2 ** zoom)
    x = (np.asarray(lon) + 180.0) / 360.0 * n
    latr = np.radians(np.asarray(lat))
    y = (1.0 - np.log(np.tan(latr) + 1.0 / np.cos(latr)) / math.pi) / 2.0 * n
    return x, y


def pixels_to_lonlat(px, py, zoom):
    n = C.TILE_SIZE * (2 ** zoom)
    lon = px / n * 360.0 - 180.0
    lat = np.degrees(np.arctan(np.sinh(math.pi * (1 - 2 * py / n))))
    return lon, lat


class Grid:
    """A tile-aligned raster window at BASE_ZOOM covering a lon/lat bbox."""

    def __init__(self, bbox, zoom=C.BASE_ZOOM):
        self.zoom = zoom
        west, south, east, north = bbox
        x0, y0 = lonlat_to_pixels(west, north, zoom)
        x1, y1 = lonlat_to_pixels(east, south, zoom)
        t = C.TILE_SIZE
        self.px0 = int(math.floor(x0 / t) * t)
        self.py0 = int(math.floor(y0 / t) * t)
        self.px1 = int(math.ceil(x1 / t) * t)
        self.py1 = int(math.ceil(y1 / t) * t)
        self.width = self.px1 - self.px0
        self.height = self.py1 - self.py0
        self.res = EARTH_CIRCUMFERENCE / (t * 2 ** zoom)  # Mercator metres per cell
        lat_c = 0.5 * (south + north)
        self.cell_m = self.res * math.cos(math.radians(lat_c))  # ground metres per cell
        half = EARTH_CIRCUMFERENCE / 2
        self.transform = Affine(self.res, 0, self.px0 * self.res - half,
                                0, -self.res, half - self.py0 * self.res)
        self.bbox = bbox

    @property
    def shape(self):
        return (self.height, self.width)

    def bbox_mask(self):
        """Boolean mask of cells whose centre lies inside the extract bbox."""
        west, south, east, north = self.bbox
        px = self.px0 + np.arange(self.width) + 0.5
        py = self.py0 + np.arange(self.height) + 0.5
        lon, _ = pixels_to_lonlat(px, np.zeros_like(px), self.zoom)
        _, lat = pixels_to_lonlat(np.zeros_like(py), py, self.zoom)
        return ((lat >= south) & (lat <= north))[:, None] & ((lon >= west) & (lon <= east))[None, :]


# ---------------------------------------------------------------------------
# Kernel and calibration
# ---------------------------------------------------------------------------

def line_integral_constant(p: float) -> float:
    """B(p) = integral of (1+u^2)^(-p/2) du over the real line."""
    return math.sqrt(math.pi) * special.gamma((p - 1) / 2) / special.gamma(p / 2)


def cell_energy(level_db: np.ndarray, ref_dist_m: float, kernel: dict, cell_m: float) -> np.ndarray:
    """Energy to burn into one cell so an infinite line of cells gives level_db at ref_dist_m."""
    p = kernel["exponent"]
    h = kernel["height_m"]
    r_ref = math.hypot(ref_dist_m, h)
    return (10.0 ** (level_db / 10.0)) * cell_m * r_ref ** (p - 1) / line_integral_constant(p)


def make_kernel(kernel: dict, cell_m: float, supersample: int = 4) -> np.ndarray:
    """Distance kernel sampled on the cell grid.

    Each kernel cell holds the *average* of K over the receiver cell's area
    (supersampled), not the value at its centre, so the cell that contains the
    source itself gets a sensible "average level across this cell" rather than
    the near-singular on-axis value.
    """
    p = kernel["exponent"]
    h = kernel["height_m"]
    alpha = kernel["absorption_db_per_km"]
    r = int(math.ceil(kernel["radius_m"] / cell_m))
    n = 2 * r + 1
    ss = max(1, int(supersample))
    # sub-cell sample offsets, centred on each cell
    sub = (np.arange(ss) + 0.5) / ss - 0.5
    ij = np.arange(-r, r + 1, dtype=np.float64)
    ii = (ij[:, None] + sub[None, :]).ravel()  # (n*ss,)
    d = np.hypot(ii[:, None], ii[None, :]) * cell_m
    k = (d ** 2 + h ** 2) ** (-p / 2.0) * 10.0 ** (-alpha * d / 1000.0 / 10.0)
    k[d > kernel["radius_m"]] = 0.0
    k = k.reshape(n, ss, n, ss).mean(axis=(1, 3))
    return k.astype(np.float32)


# ---------------------------------------------------------------------------
# Rasterisation
# ---------------------------------------------------------------------------

def _linestrings(coords: np.ndarray, offsets: np.ndarray, zoom: int, grid: Grid):
    """Packed lon/lat polylines -> shapely LineStrings in Mercator metres."""
    if len(coords) == 0:
        return np.zeros(0, dtype=object)
    px, py = lonlat_to_pixels(coords[:, 0], coords[:, 1], zoom)
    half = EARTH_CIRCUMFERENCE / 2
    mx = px * grid.res - half
    my = half - py * grid.res
    idx = np.repeat(np.arange(len(offsets) - 1), np.diff(offsets))
    return shapely.linestrings(np.column_stack([mx, my]), indices=idx)


def burn(grid: Grid, geoms, energies: np.ndarray) -> np.ndarray:
    """Sum per-cell energies of all polylines into a float32 raster."""
    out = np.zeros(grid.shape, dtype=np.float32)
    if len(geoms) == 0:
        return out
    # rasterio wants python-level (geom, value) pairs; group by value to keep
    # the number of distinct burn passes small for very large inputs.
    shapes = ((g, float(e)) for g, e in zip(geoms, energies) if g is not None and not g.is_empty)
    rasterize(shapes, out=out, transform=grid.transform, merge_alg=MergeAlg.add,
              all_touched=False, dtype=np.float32)
    return out


# ---------------------------------------------------------------------------
# Propagation
# ---------------------------------------------------------------------------

def convolve_blocks(src: np.ndarray, kernel: np.ndarray, block: int = 2048) -> np.ndarray:
    """FFT convolution in overlapping blocks so memory stays bounded."""
    r = kernel.shape[0] // 2
    H, W = src.shape
    out = np.zeros_like(src, dtype=np.float32)
    for i0 in range(0, H, block):
        for j0 in range(0, W, block):
            i1, j1 = min(i0 + block, H), min(j0 + block, W)
            a0, b0 = max(i0 - r, 0), max(j0 - r, 0)
            a1, b1 = min(i1 + r, H), min(j1 + r, W)
            win = src[a0:a1, b0:b1]
            if not win.any():
                continue
            res = signal.fftconvolve(win, kernel, mode="same")
            out[i0:i1, j0:j1] = res[i0 - a0:i1 - a0, j0 - b0:j1 - b0]
    np.maximum(out, 0.0, out=out)  # FFT round-off can go slightly negative
    return out


def add_point_sources(dest: np.ndarray, src: np.ndarray, kernel: np.ndarray) -> None:
    """Direct (non-FFT) kernel stamping for sparse sources such as runways."""
    r = kernel.shape[0] // 2
    H, W = dest.shape
    ys, xs = np.nonzero(src)
    for y, x in zip(ys, xs):
        e = src[y, x]
        a0, a1 = max(y - r, 0), min(y + r + 1, H)
        b0, b1 = max(x - r, 0), min(x + r + 1, W)
        dest[a0:a1, b0:b1] += e * kernel[a0 - (y - r):a1 - (y - r), b0 - (x - r):b1 - (x - r)]


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def compute(npz_path: str, log=print) -> dict:
    """Return {'db': float32 HxW, 'valid': bool HxW, 'grid': Grid, ...}."""
    t0 = time.time()
    d = np.load(npz_path)
    bbox = [float(v) for v in d["bbox"]]
    grid = Grid(bbox)
    log(f"  grid {grid.width}x{grid.height} cells, {grid.cell_m:.1f} m on the ground")

    # Roads + rail (same kernel)
    road_kernel = make_kernel(C.ROAD_KERNEL, grid.cell_m)
    energies = cell_energy(d["road_levels"].astype(np.float64), C.REF_DIST_ROAD_M, C.ROAD_KERNEL, grid.cell_m)
    geoms = _linestrings(d["road_coords"], d["road_offsets"], grid.zoom, grid)
    src_road = burn(grid, geoms, energies)
    log(f"  burned {len(geoms)} road/rail ways into {int((src_road > 0).sum())} cells ({time.time() - t0:.0f}s)")
    e_total = convolve_blocks(src_road, road_kernel)
    log(f"  road kernel {road_kernel.shape[0]}px, convolution done ({time.time() - t0:.0f}s)")

    # Runways
    src_air = None
    if len(d["runway_levels"]):
        air_kernel = make_kernel(C.AIR_KERNEL, grid.cell_m)
        energies_air = cell_energy(d["runway_levels"].astype(np.float64), C.REF_DIST_AIR_M, C.AIR_KERNEL, grid.cell_m)
        geoms_air = _linestrings(d["runway_coords"], d["runway_offsets"], grid.zoom, grid)
        src_air = burn(grid, geoms_air, energies_air)
        if int((src_air > 0).sum()) > 1500:
            e_total += convolve_blocks(src_air, air_kernel)
        else:
            add_point_sources(e_total, src_air, air_kernel)
        log(f"  {int((src_air > 0).sum())} runway cells stamped with {air_kernel.shape[0]}px kernel ({time.time() - t0:.0f}s)")

    # dB, floor/ceiling, validity
    with np.errstate(divide="ignore"):
        db = 10.0 * np.log10(e_total, dtype=np.float32)
    np.clip(db, C.DISPLAY_FLOOR_DB, C.DISPLAY_CEIL_DB, out=db)

    src_mask = src_road > 0
    if src_air is not None:
        src_mask |= src_air > 0
    dist = ndimage.distance_transform_edt(~src_mask) * grid.cell_m
    valid = (dist <= C.NODATA_DISTANCE_M) & grid.bbox_mask()
    db[~valid] = 0.0
    log(f"  {int(valid.sum())} valid cells; dB range {db[valid].min():.1f}-{db[valid].max():.1f}; "
        f"median {np.median(db[valid]):.1f} ({time.time() - t0:.0f}s)")
    return {"db": db, "valid": valid, "grid": grid, "bbox": bbox}
