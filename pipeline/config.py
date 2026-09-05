"""
Model parameters for the Canada Noise Map.

Everything that turns OpenStreetMap geometry into decibels lives here so the
assumptions are in one place and easy to argue with.  All levels are A-weighted
day-time equivalent levels (LAeq, roughly 07:00-22:00) in dB.

The model is a simplified transportation-noise model in the spirit of the US
DOT/BTS National Transportation Noise Map: every road, railway and runway is a
line source with a class-based emission level, sound spreads with distance
(plus ground/air absorption) and all contributions are summed in the energy
domain.  See README.md for the derivation and the limitations.
"""

# ---------------------------------------------------------------------------
# Grid
# ---------------------------------------------------------------------------

# The noise raster is computed directly on the Web-Mercator tile grid at this
# zoom level so that tiles are plain slices of the raster.  Zoom 13 gives
# ~19 m Mercator cells (~14 m on the ground at Toronto's latitude).
BASE_ZOOM = 13
MIN_ZOOM = 6
TILE_SIZE = 256

# Cells further than this (ground metres) from any modelled source are marked
# "no data" (transparent).  This hides lakes and the un-modelled rural void
# instead of painting them a misleading "very quiet".
NODATA_DISTANCE_M = 1500.0

# Levels below this are displayed as the floor (typical urban ambient).
DISPLAY_FLOOR_DB = 35.0
DISPLAY_CEIL_DB = 95.0

# ---------------------------------------------------------------------------
# Road sources: OSM highway=* -> (L_ref dB at REF_DIST_ROAD_M, default lanes,
# reference speed km/h).  Levels are per OSM way, i.e. per carriageway; a
# divided road contributes twice (energy sum) which is intended.
# ---------------------------------------------------------------------------

REF_DIST_ROAD_M = 15.0  # distance from centreline the emission levels refer to

ROAD_CLASSES = {
    #  highway=*        L_ref  lanes  v_ref
    "motorway":        (75.0,   3,    100),
    "motorway_link":   (65.0,   1,     60),
    "trunk":           (72.0,   2,     80),
    "trunk_link":      (63.0,   1,     60),
    "primary":         (69.0,   2,     60),
    "primary_link":    (60.0,   1,     50),
    "secondary":       (65.0,   2,     50),
    "secondary_link":  (58.0,   1,     50),
    "tertiary":        (61.0,   2,     50),
    "tertiary_link":   (56.0,   1,     40),
    "unclassified":    (55.0,   2,     40),
    "residential":     (53.0,   2,     40),
    "living_street":   (47.0,   1,     20),
    "busway":          (58.0,   1,     50),
    "service":         (44.0,   1,     20),
}

# Extra attenuation per metre-ish parameters for the road/rail kernel.
ROAD_KERNEL = {
    # Point-source exponent p: energy ~ (d^2 + h^2)^(-p/2).  Integrated along a
    # line this gives d^(1-p): p=2 is the ideal 3 dB/doubling of a line source
    # over hard ground, p=2.5 (4.5 dB/doubling) is the usual soft-ground value.
    "exponent": 2.5,
    # Near-field softening (metres): keeps the level finite on the road itself.
    "height_m": 12.0,
    # Air + excess ground absorption in dB per km.
    "absorption_db_per_km": 2.0,
    # Kernel cut-off radius (ground metres).
    "radius_m": 2000.0,
}

# Lane and speed corrections (dB), applied when OSM has the tags.
LANE_CORRECTION_CLAMP = (-3.0, 4.0)     # 10*log10(lanes / default_lanes)
SPEED_CORRECTION_CLAMP = (-4.0, 4.0)    # 20*log10(v / v_ref)

# ---------------------------------------------------------------------------
# Rail sources: L_ref dB at REF_DIST_ROAD_M (same kernel as roads).
# ---------------------------------------------------------------------------

RAIL_LEVELS = {
    "main": 68.0,        # railway=rail + usage=main (busy intercity/commuter)
    "branch": 60.0,      # railway=rail + usage=branch
    "rail": 62.0,        # railway=rail with no usage tag
    "yard": 50.0,        # service=yard/spur/siding/crossover, usage=industrial
    "light_rail": 60.0,
    "tram": 56.0,
    "subway": 60.0,      # only surface sections (tunnels are skipped)
    "narrow_gauge": 55.0,
    "monorail": 55.0,
    "preserved": 50.0,
}

# ---------------------------------------------------------------------------
# Aircraft: runways are line sources with their own, longer-range kernel.
# Levels refer to REF_DIST_AIR_M from the runway centreline.
# ---------------------------------------------------------------------------

REF_DIST_AIR_M = 1000.0

RUNWAY_LEVELS = {
    "major": 65.0,     # IATA airport with a >= 2400 m runway (YYZ, YUL, YVR ...)
    "regional": 57.0,  # other IATA airports or runways >= 1500 m
    "minor": 47.0,     # everything else (GA strips)
}

AIR_KERNEL = {
    "exponent": 2.2,
    "height_m": 150.0,
    "absorption_db_per_km": 1.5,
    "radius_m": 12000.0,
}

# ---------------------------------------------------------------------------
# Presentation
# ---------------------------------------------------------------------------

# Colour ramp stops (dB -> hex).  Encoded into the tiles at build time; the
# web app inverts the same palette to read values back out of the PNGs.
PALETTE_STOPS = [
    (35, "#3d8f3a"),
    (45, "#7fc97f"),
    (50, "#c7e9a0"),
    (55, "#ffffb2"),
    (60, "#fecc5c"),
    (65, "#fd8d3c"),
    (70, "#f03b20"),
    (75, "#bd0026"),
    (82, "#7a0177"),
    (95, "#2d004b"),
]

CATEGORIES = [
    (45, "Very quiet", "Comparable to a quiet suburb or library."),
    (55, "Quiet", "Typical of a residential street with light traffic."),
    (65, "Moderate", "Busy street or ordinary urban background."),
    (75, "Loud", "Near an arterial road, rail line or highway."),
    (999, "Very loud", "Adjacent to a highway, rail corridor or airport."),
]
