"""
Unit tests for the noise model.  Run with `pytest`.

They check the physics and the tile encoding, not the OSM plumbing: an
infinite straight road must reproduce its emission level at the reference
distance and decay at the configured rate; two identical roads must add 3 dB;
palette encoding must round-trip.
"""
import math

import numpy as np
import pytest

from pipeline import config as C
from pipeline import extract, model, tiles


def _line_level(distance_m, level_db, kernel, ref_dist, cell_m=14.0, n=800):
    """Level at `distance_m` from an infinite straight line of burned cells."""
    e = model.cell_energy(np.array([level_db]), ref_dist, kernel, cell_m)[0]
    k = np.arange(-n, n + 1) * cell_m
    d = np.hypot(distance_m, k)
    p, h, a = kernel["exponent"], kernel["height_m"], kernel["absorption_db_per_km"]
    contrib = e * (d ** 2 + h ** 2) ** (-p / 2) * 10 ** (-a * d / 1000 / 10)
    return 10 * math.log10(contrib.sum())


def test_reference_level_is_reproduced():
    L = _line_level(C.REF_DIST_ROAD_M, 69.0, C.ROAD_KERNEL, C.REF_DIST_ROAD_M)
    assert abs(L - 69.0) < 0.6


def test_line_source_decay_rate():
    # Between 60 m and 480 m (3 doublings) the drop should be ~4.5 dB per
    # doubling for p = 2.5, plus a little absorption.
    p = C.ROAD_KERNEL["exponent"]
    per_doubling = 10 * (p - 1) * math.log10(2)
    L1 = _line_level(60, 69.0, C.ROAD_KERNEL, C.REF_DIST_ROAD_M)
    L2 = _line_level(480, 69.0, C.ROAD_KERNEL, C.REF_DIST_ROAD_M)
    drop = L1 - L2
    absorption = C.ROAD_KERNEL["absorption_db_per_km"] * (480 - 60) / 1000
    assert abs(drop - (3 * per_doubling + absorption)) < 1.0


def test_two_roads_add_three_db():
    a = 10 ** (_line_level(50, 65.0, C.ROAD_KERNEL, C.REF_DIST_ROAD_M) / 10)
    both = 10 * math.log10(2 * a)
    single = 10 * math.log10(a)
    assert abs((both - single) - 3.01) < 0.01


def test_kernel_is_symmetric_and_bounded():
    k = model.make_kernel(C.ROAD_KERNEL, 14.0)
    assert k.shape[0] == k.shape[1] and k.shape[0] % 2 == 1
    assert np.allclose(k, k.T)
    assert np.allclose(k, k[::-1, ::-1])
    assert k.max() == k[k.shape[0] // 2, k.shape[1] // 2]
    assert k.min() >= 0


def test_grid_is_tile_aligned():
    g = model.Grid([-79.79, 43.48, -79.0, 43.92])
    assert g.px0 % C.TILE_SIZE == 0 and g.py0 % C.TILE_SIZE == 0
    assert g.width % C.TILE_SIZE == 0 and g.height % C.TILE_SIZE == 0
    lon, lat = model.pixels_to_lonlat(np.array([g.px0]), np.array([g.py0]), g.zoom)
    assert lon[0] <= -79.79 and lat[0] >= 43.92


def test_palette_round_trip():
    lut = tiles.build_palette()
    lo, hi = int(C.DISPLAY_FLOOR_DB * tiles.VALUE_SCALE), int(C.DISPLAY_CEIL_DB * tiles.VALUE_SCALE)
    seen = {}
    for i in range(lo, hi + 1):
        key = tuple(lut[i, :3])
        assert key not in seen, f"palette collision at {i} and {seen[key]}"
        seen[key] = i
    assert tuple(lut[0]) == (0, 0, 0, 0)


def test_encode_values_reserves_zero_for_nodata():
    db = np.array([[35.0, 60.0], [0.0, 95.0]], dtype=np.float32)
    valid = np.array([[True, True], [False, True]])
    v = tiles.encode_values(db, valid)
    assert v[1, 0] == 0
    s = tiles.VALUE_SCALE
    assert v[0, 0] == 35 * s and v[0, 1] == 60 * s and v[1, 1] == 95 * s


def test_downsample_is_energy_mean():
    db = np.array([[60.0, 60.0], [60.0, 70.0]], dtype=np.float32)
    valid = np.ones((2, 2), dtype=bool)
    d2, v2 = tiles.downsample(db, valid)
    expected = 10 * math.log10((3 * 1e6 + 1e7) / 4)
    assert v2[0, 0] and abs(d2[0, 0] - expected) < 0.01


def test_maxspeed_parsing():
    assert extract.parse_maxspeed("50") == 50
    assert abs(extract.parse_maxspeed("35 mph") - 56.3) < 0.1
    assert extract.parse_maxspeed("signals") is None
    assert extract.parse_lanes("4") == 4 and extract.parse_lanes("many") is None


def test_road_level_corrections():
    base = C.ROAD_CLASSES["primary"][0]
    assert extract.road_level({"highway": "primary"}) == base
    assert extract.road_level({"highway": "primary", "lanes": "4"}) == pytest.approx(base + 10 * math.log10(2))
    assert extract.road_level({"highway": "primary", "tunnel": "yes"}) is None
    assert extract.road_level({"highway": "footway"}) is None
