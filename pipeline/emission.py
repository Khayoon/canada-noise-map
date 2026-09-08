"""
Traffic-flow based emission model.

This module replaces the old approach of hard-coding one invented decibel
number per OSM road class.  Instead every source gets its level from the
standard line-source relationship used by essentially every published road
noise method (CNOSSOS-EU, CRTN, RLS-90, FHWA TNM):

    L_W',line  =  L_W,vehicle  +  10 log10( Q / (1000 v) )          (1)

    L_W,vehicle = 10 log10( 10^(L_WR/10) + 10^(L_WP/10) )           (2)
    L_WR = A_R + B_R log10(v / v_ref)          (rolling noise)
    L_WP = A_P + B_P (v - v_ref) / v_ref       (propulsion noise)

with v_ref = 70 km/h.  Equations (1) and (2) are the CNOSSOS-EU road source
formulation (Commission Directive (EU) 2015/996, Annex II; the per-octave-band
A/B coefficients live in that Annex's Table F-1).

We work with A-weighted overall levels rather than octave bands.  That is a
deliberate simplification: without measured traffic counts the dominant error
in this map is the flow Q, not the emission spectrum, so spectral detail would
be false precision.  Reducing (1) and (2) to A-weighted overall levels and
collecting the speed terms gives the classic result

    L  =  L0  +  10 log10(Qe / Qe0)  +  20 log10(v / v0)            (3)

  * +3 dB per doubling of traffic flow  (energy is additive)
  * +6 dB per doubling of speed         (30 log10 v rolling emission,
                                         less 10 log10 v vehicle spacing)
  * heavy vehicles enter through the car-equivalent flow
        Qe = Q_light + HEAVY_EQUIVALENT * Q_heavy
    i.e. one truck is worth about ten cars, the standard equivalence used
    across road-noise practice.

WHAT IS ASSUMED AND WHAT IS SOURCED
-----------------------------------
Sourced:
  * the functional form (3), from the CNOSSOS-EU formulation above;
  * the absolute anchor.  A busy urban freeway measures 70-80 dB(A) at 50 ft
    (~15 m) - the standard environmental-noise comparison figure published on
    state DOT noise pages, e.g. Colorado DOT's noise FAQ.  ANCHOR below pins
    the single free constant L0 to the middle of that published band.

Assumed (and this is the honest weak point of the whole map):
  * AADT, heavy-vehicle share and the night traffic share for each OSM road
    class, in ROAD_TRAFFIC / config.py.  These are ordinary traffic-planning
    order-of-magnitude figures, not counts.  They are stated in vehicles per
    day precisely so that anybody can disagree with a number, and so that real
    counts (Ontario MTO AADT, municipal count programmes) drop straight in
    without touching the acoustics.

Because the level now depends on flow, the day/night difference is no longer a
separate hand-made table: it falls out of the same equation from the day and
night hourly flows.
"""
from __future__ import annotations

import math

from . import config as C

# CNOSSOS-EU reference speed for the emission equations (km/h).
V_REF = 70.0

# Car-equivalent weight of one heavy vehicle.  A heavy goods vehicle radiates
# roughly 10 dB more acoustic power than a car at urban/highway speeds, hence
# an energy equivalence of about 10:1.
HEAVY_EQUIVALENT = 10.0

# Vehicle sound power, equation (2), as A-weighted overall levels relative to
# the rolling term at v_ref (the absolute offset is absorbed into L0 by the
# anchor below, so only the SHAPE of these curves matters here).
#
# Keeping rolling and propulsion as two separate terms rather than collapsing
# them into a single 30 log10(v) is what stops the model from making slow roads
# far too quiet: below about 50 km/h propulsion noise dominates and the total
# flattens out instead of continuing to fall.  An earlier single-term version
# of this module made 60 km/h arterials roughly 4 dB quieter than they should
# be, which showed up directly in the Toronto validation.
B_R = 30.0    # rolling noise grows as 30 log10(v)
A_P = -4.0    # propulsion sits ~4 dB below rolling at v_ref for light vehicles
B_P = 10.0    # and grows only slowly with speed

FLOW_EXPONENT = 10.0    # energy is additive in the number of vehicles

# Night window length and day window length in hours (see config.PERIODS).
NIGHT_HOURS = 8.0
DAY_HOURS = 16.0

# ---------------------------------------------------------------------------
# Absolute calibration
#
# One published reference level fixes the only free constant in the model.
# A busy urban freeway reads 70-80 dB(A) at 50 ft (~15 m).  OSM maps a divided
# freeway as two separate carriageway ways and the model sums them in the
# energy domain, so a single carriageway is calibrated to sit 3 dB below the
# middle of that band: two carriageways then reproduce ~75 dB(A) at 15 m.
# ---------------------------------------------------------------------------

ANCHOR = {
    "aadt": 40_000.0,     # vehicles/day on one carriageway of a busy freeway
    "speed_kmh": 100.0,
    "heavy_frac": 0.12,
    "night_share": 0.11,  # fraction of AADT in the 23:00-07:00 window
    "level_db": 72.0,     # daytime LAeq at 15 m from this one carriageway
}


def car_equivalent_flow(aadt: float, heavy_frac: float, night_share: float, period: str) -> float:
    """Car-equivalent vehicles per hour in the given period."""
    if period == "night":
        veh_per_hour = aadt * night_share / NIGHT_HOURS
    else:
        veh_per_hour = aadt * (1.0 - night_share) / DAY_HOURS
    light = veh_per_hour * (1.0 - heavy_frac)
    heavy = veh_per_hour * heavy_frac
    return light + HEAVY_EQUIVALENT * heavy


def vehicle_power(speed_kmh: float) -> float:
    """A-weighted sound power of one car-equivalent vehicle, equation (2),
    in dB relative to its rolling term at v_ref."""
    v = max(speed_kmh, 5.0)
    l_roll = B_R * math.log10(v / V_REF)
    l_prop = A_P + B_P * (v - V_REF) / V_REF
    return 10.0 * math.log10(10.0 ** (l_roll / 10.0) + 10.0 ** (l_prop / 10.0))


def _line_power(aadt: float, speed_kmh: float, heavy_frac: float,
                night_share: float, period: str) -> float | None:
    """Equation (1): sound power per metre of line source, relative to L0."""
    qe = car_equivalent_flow(aadt, heavy_frac, night_share, period)
    if qe <= 0.0:
        return None
    v = max(speed_kmh, 5.0)
    return vehicle_power(v) + FLOW_EXPONENT * math.log10(qe / (1000.0 * v))


def _anchor_constant() -> float:
    """Solve for L0 using the published anchor level."""
    lw = _line_power(ANCHOR["aadt"], ANCHOR["speed_kmh"], ANCHOR["heavy_frac"],
                     ANCHOR["night_share"], "day")
    return ANCHOR["level_db"] - lw


L0 = _anchor_constant()


def level(aadt: float, speed_kmh: float, heavy_frac: float, night_share: float,
          period: str = "day") -> float:
    """A-weighted equivalent level (dB) at config.REF_DIST_ROAD_M from the
    centreline of a line source carrying this traffic."""
    lw = _line_power(aadt, speed_kmh, heavy_frac, night_share, period)
    return 0.0 if lw is None else L0 + lw


def road_levels(cls: str, lanes: int | None, speed_kmh: float | None) -> tuple[float, float]:
    """(day, night) level in dB at REF_DIST_ROAD_M for an OSM highway class.

    `lanes` and `maxspeed` from the OSM tags refine the class defaults: lane
    count scales the assumed AADT proportionally (a 4-lane arterial carries
    about twice a 2-lane one) and the tagged speed replaces the default.
    """
    t = C.ROAD_TRAFFIC[cls]
    aadt = t["aadt"]
    if lanes:
        aadt *= _clamp(lanes / t["lanes"], C.LANE_AADT_CLAMP[0], C.LANE_AADT_CLAMP[1])
    v = speed_kmh or t["speed"]
    v = _clamp(v, t["speed"] * C.SPEED_CLAMP[0], t["speed"] * C.SPEED_CLAMP[1])
    return (
        level(aadt, v, t["heavy"], t["night_share"], "day"),
        level(aadt, v, t["heavy"], t["night_share"], "night"),
    )


def rail_levels(cls: str) -> tuple[float, float]:
    """(day, night) level in dB at REF_DIST_ROAD_M for a railway class.

    Rail is kept as an explicit level per class rather than a flow model: the
    emission of a train is dominated by rolling stock type, length and speed,
    and OSM carries no service-frequency information at all.  The night value
    uses the same 10 log10(flow) reasoning as roads - freight runs through the
    night and barely drops, transit that stops running falls away.
    """
    day = C.RAIL_LEVELS[cls]
    night = day + C.RAIL_NIGHT_DELTA[cls]
    return day, night


def runway_levels(cls: str) -> tuple[float, float]:
    """(day, night) level in dB at REF_DIST_AIR_M for a runway class."""
    day = C.RUNWAY_LEVELS[cls]
    return day, day + C.RUNWAY_NIGHT_DELTA[cls]


def night_delta_for(cls: str) -> float:
    """The day->night offset equation (3) produces for a road class."""
    d, n = road_levels(cls, None, None)
    return n - d


def _clamp(x: float, lo: float, hi: float) -> float:
    return min(max(x, lo), hi)
