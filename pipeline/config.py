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

# Tile image codec: "webp" (lossless, ~28% smaller than PNG and pixel-exact, so
# the palette still inverts back to decibels) or "png" for maximum compatibility.
TILE_FORMAT = "webp"

# Variable detail: full BASE_ZOOM resolution is only worth storing where there
# is something to resolve.  Each chunk is classified by how much of it is
# within earshot of a source, and gets a max zoom accordingly.  Sparse chunks
# stop at DETAIL_MIN_ZOOM and are stretched by the renderer, which is why the
# web app declares a base source (<= DETAIL_MIN_ZOOM) and a detail source
# (> DETAIL_MIN_ZOOM) over the same archive.
VARIABLE_DETAIL = True
DETAIL_MIN_ZOOM = 11
DETAIL_RULES = [
    # (minimum fraction of the chunk carrying a modelled value, max zoom)
    (0.55, 13),   # built-up: full resolution
    (0.18, 12),   # suburban / small town
    (0.00, 11),   # rural: highways and rail across empty land
]

# Cells further than this (ground metres) from any modelled source are marked
# "no data" (transparent).  This hides lakes and the un-modelled rural void
# instead of painting them a misleading "very quiet".
NODATA_DISTANCE_M = 1500.0

# Levels below this are displayed as the floor (typical urban ambient).
DISPLAY_FLOOR_DB = 35.0
DISPLAY_CEIL_DB = 95.0

# ---------------------------------------------------------------------------
# Road sources.
#
# Emission is NOT a hard-coded decibel number per class any more.  Each OSM
# highway class carries a traffic description - annual average daily traffic
# per carriageway, typical speed, heavy-vehicle share and the fraction of the
# day's traffic that runs in the 23:00-07:00 night window - and pipeline
# emission.py turns that into a level with the standard line-source
# relationship (see the module docstring for the equation and its provenance).
#
# These traffic figures are ordinary planning-scale assumptions, not counts.
# They are the honest weak point of the map and are stated in vehicles per day
# so that they can be argued with directly, and so that real counts (Ontario
# MTO publishes AADT for provincial highways; several cities publish municipal
# counts) can replace them without touching the acoustics.
#
# AADT here is the traffic carried by ONE OSM WAY, which is not the same thing
# as the traffic on the road.  A divided highway is mapped as two ways, each
# carrying roughly half the total, and the model sums them in the energy
# domain.  An undivided arterial is a single way carrying the whole two-way
# volume.  Getting this wrong is worth several decibels on every arterial in
# the country, so the two cases are marked explicitly below.
#
# Sanity anchors for the arterial figures: Toronto's major arterials (Yonge,
# Bloor, Dufferin, Steeles) run in the mid-20,000s to around 40,000 vehicles a
# day two-way, minor arterials around half that.
# ---------------------------------------------------------------------------

REF_DIST_ROAD_M = 15.0  # distance from centreline the emission levels refer to

ROAD_TRAFFIC = {
    # highway=*          AADT/way  lanes  speed  heavy  night   divided?
    "motorway":        dict(aadt=40000, lanes=3, speed=100, heavy=0.12, night_share=0.11),  # per carriageway
    "motorway_link":   dict(aadt= 6000, lanes=1, speed= 60, heavy=0.10, night_share=0.11),
    "trunk":           dict(aadt=22000, lanes=2, speed= 80, heavy=0.10, night_share=0.10),  # per carriageway
    "trunk_link":      dict(aadt= 4000, lanes=1, speed= 60, heavy=0.08, night_share=0.10),
    "primary":         dict(aadt=28000, lanes=4, speed= 60, heavy=0.07, night_share=0.08),  # two-way arterial
    "primary_link":    dict(aadt= 3000, lanes=1, speed= 50, heavy=0.05, night_share=0.08),
    "secondary":       dict(aadt=15000, lanes=4, speed= 50, heavy=0.05, night_share=0.07),  # two-way
    "secondary_link":  dict(aadt= 2200, lanes=1, speed= 50, heavy=0.04, night_share=0.07),
    "tertiary":        dict(aadt= 7000, lanes=2, speed= 50, heavy=0.04, night_share=0.06),  # two-way
    "tertiary_link":   dict(aadt= 1500, lanes=1, speed= 40, heavy=0.03, night_share=0.06),
    "unclassified":    dict(aadt= 2000, lanes=2, speed= 40, heavy=0.03, night_share=0.06),
    "residential":     dict(aadt=  900, lanes=2, speed= 40, heavy=0.02, night_share=0.05),
    "living_street":   dict(aadt=  200, lanes=1, speed= 20, heavy=0.02, night_share=0.04),
    "busway":          dict(aadt= 1200, lanes=1, speed= 50, heavy=0.80, night_share=0.06),
    "service":         dict(aadt=  150, lanes=1, speed= 20, heavy=0.03, night_share=0.04),
}

# Backwards-compatible view: the set of highway values the extractor accepts.
ROAD_CLASSES = ROAD_TRAFFIC

# OSM lanes= scales the assumed AADT, clamped so a mis-tagged way cannot run
# away with the model.  OSM maxspeed= replaces the class default speed, clamped
# to a sane multiple of it.
LANE_AADT_CLAMP = (0.5, 2.5)
SPEED_CLAMP = (0.6, 1.6)

# Extra attenuation per metre-ish parameters for the road/rail kernel.
ROAD_KERNEL = {
    # Point-source exponent p: energy ~ (d^2 + h^2)^(-p/2).  Integrated along a
    # line this gives d^(1-p): p=2 is the ideal 3 dB/doubling of a line source
    # over hard ground, p=2.5 (4.5 dB/doubling) is the usual soft-ground value.
    "exponent": 2.5,
    # Near-field softening (metres): stands in for the effective source height
    # and keeps the level finite on the road itself.  4 m is about the exhaust
    # height of a heavy vehicle.  The Toronto validation is very flat in this
    # parameter (2 m, 4 m and 8 m score within 0.3 points of each other), so it
    # is set on physical grounds rather than by fit.
    "height_m": 4.0,
    # Air absorption plus excess attenuation through built-up ground, dB/km.
    #
    # Pure atmospheric absorption is only a few dB/km.  The rest of this figure
    # stands in for the single biggest thing this model does not do: shielding
    # by buildings.  ISO 9613-2 - the propagation standard used by Toronto
    # Public Health's own model - carries an explicit attenuation term for
    # sound travelling THROUGH a housing area, worth up to about 10 dB over a
    # built-up path.  A shift-invariant kernel cannot trace a path, so that
    # clutter is represented on average here: over a typical 200-500 m urban
    # path this yields 2-6 dB, which is the range ISO 9613-2 would give.
    # Without it, arterial noise washes unshielded across whole neighbourhoods
    # and the modelled population piles up in the 55-65 dB band.
    # Chosen against the Toronto validation in pipeline/calibrate.py.
    "absorption_db_per_km": 12.0,
    # Kernel cut-off radius (ground metres).
    "radius_m": 2000.0,
}

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
# Day / night
#
# Night is not day minus a constant.  For roads the difference is no longer a
# table at all: the level follows 10*log10(hourly flow), so feeding the night
# hourly flow (night_share of AADT spread over 8 hours) through the same
# emission equation as the day flow ((1 - night_share) over 16 hours) produces
# the offset automatically.  A freeway holding 11% of its daily traffic
# overnight comes out about 6 dB down; a residential street at 5% about 10 dB.
#
# Rail and aircraft keep explicit offsets, because their night behaviour is
# driven by operations (freight runs all night, transit stops, the big airports
# have night-flight restriction programmes) rather than by a traffic share.
# ---------------------------------------------------------------------------

PERIODS = {
    "day":   {"label": "Daytime",   "hours": "07:00-23:00", "metric": "LAeq,16h"},
    "night": {"label": "Nighttime", "hours": "23:00-07:00", "metric": "LAeq,8h"},
}

RAIL_NIGHT_DELTA = {
    "main": -2.0,        # freight runs overnight
    "branch": -3.0,
    "rail": -3.0,
    "yard": -3.0,        # yards work around the clock
    "light_rail": -9.0,
    "tram": -9.0,
    "subway": -9.0,
    "narrow_gauge": -6.0,
    "monorail": -9.0,
    "preserved": -20.0,  # tourist operations do not run at night
}

RUNWAY_NIGHT_DELTA = {
    "major": -9.0,       # night-flight restriction programmes at the big hubs
    "regional": -10.0,
    "minor": -15.0,
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
