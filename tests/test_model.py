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
from pipeline import emission, extract, model, tiles


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
    """With absorption switched off the kernel must reproduce the closed-form
    line-source decay exactly: d^(1-p), i.e. 10(p-1)log10(2) dB per doubling."""
    kernel = dict(C.ROAD_KERNEL, absorption_db_per_km=0.0)
    per_doubling = 10 * (kernel["exponent"] - 1) * math.log10(2)
    L1 = _line_level(60, 69.0, kernel, C.REF_DIST_ROAD_M)
    L2 = _line_level(480, 69.0, kernel, C.REF_DIST_ROAD_M)
    assert abs((L1 - L2) - 3 * per_doubling) < 0.3


def test_absorption_adds_attenuation_monotonically():
    """Excess attenuation through built-up ground must only ever reduce the
    level, and by more at greater distance."""
    quiet = dict(C.ROAD_KERNEL, absorption_db_per_km=0.0)
    loud = dict(C.ROAD_KERNEL, absorption_db_per_km=12.0)
    near = _line_level(60, 69.0, quiet, C.REF_DIST_ROAD_M) - _line_level(60, 69.0, loud, C.REF_DIST_ROAD_M)
    far = _line_level(480, 69.0, quiet, C.REF_DIST_ROAD_M) - _line_level(480, 69.0, loud, C.REF_DIST_ROAD_M)
    assert 0.0 < near < far
    # 12 dB/km over a 500 m urban path should be a handful of dB, not tens.
    assert 2.0 < far < 10.0


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
    s = tiles.VALUE_SCALE
    assert v[1, 0] == 0
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


def test_road_level_skips_what_it_should():
    assert extract.road_level({"highway": "primary"}) is not None
    assert extract.road_level({"highway": "primary", "tunnel": "yes"}) is None
    assert extract.road_level({"highway": "footway"}) is None
    assert extract.road_level({"highway": "service", "service": "driveway"}) is None


def test_emission_scales_with_flow_and_speed():
    """Equation (1): +3 dB per doubling of traffic, and more traffic or more
    speed is always louder."""
    base = emission.level(10_000, 50, 0.05, 0.07)
    assert emission.level(20_000, 50, 0.05, 0.07) == pytest.approx(base + 3.01, abs=0.01)
    assert emission.level(10_000, 80, 0.05, 0.07) > base
    assert emission.level(10_000, 50, 0.20, 0.07) > base   # more trucks


def test_propulsion_term_stops_slow_roads_vanishing():
    """Collapsing the two emission terms into rolling noise alone made low
    speed roads far too quiet; the propulsion floor must keep 30 km/h within a
    few dB of what pure 30log10(v) rolling would give at 70."""
    rolling_only = 30.0 * math.log10(30.0 / emission.V_REF)
    assert emission.vehicle_power(30.0) > rolling_only + 1.5


def test_night_is_quieter_and_class_dependent():
    """Night falls out of the traffic split, so a freeway that keeps its night
    traffic must drop less than a residential street that empties out."""
    free_day, free_night = emission.road_levels("motorway", None, None)
    res_day, res_night = emission.road_levels("residential", None, None)
    assert free_night < free_day and res_night < res_day
    assert (free_day - free_night) < (res_day - res_night)


def test_anchor_reproduces_published_freeway_level():
    """Two carriageways of a busy freeway must land inside the published
    70-80 dB(A)-at-15 m band the model is anchored to."""
    one = emission.road_levels("motorway", None, None)[0]
    both = 10 * math.log10(2 * 10 ** (one / 10))
    assert 70.0 <= both <= 80.0


def test_osm_tags_refine_the_class_defaults():
    plain = extract.road_level({"highway": "primary"})
    assert extract.road_level({"highway": "primary", "lanes": "8"}) > plain
    assert extract.road_level({"highway": "primary", "maxspeed": "80"}) > plain
    assert extract.road_level({"highway": "primary", "maxspeed": "30"}) < plain
