# Canada Noise Map

A free, open **noise-pollution map for Canadian cities** — the kind of "how loud is this address?" layer that US real-estate sites get from commercial data and the US DOT's [National Transportation Noise Map](https://www.bts.gov/geospatial/national-transportation-noise-map), which Canada does not have.

Everything is pre-computed: a Python pipeline turns OpenStreetMap roads, railways and runways into a decibel raster, packs it into [PMTiles](https://github.com/protomaps/PMTiles) archives, and a static MapLibre page reads values straight out of the tiles. No server, no API keys, no per-request cost — it runs on GitHub Pages, Vercel, Netlify or any static host.

**Coverage:** Toronto, Montréal, Vancouver, Ottawa–Gatineau, Calgary, Kitchener–Waterloo, Halifax, Victoria. The pipeline is no longer limited to these — see [Building more of Canada](#building-more-of-canada).

![Toronto overview](tests/screenshots/toronto_overview.png)

## What you can do on the map

* Toggle between **daytime** (07:00–23:00, LAeq,16h) and **nighttime** (23:00–07:00, LAeq,8h).
* Click or tap anywhere to read the estimated level (dB(A)), a plain-English category, a 50–100 "quiet score", and how much quieter the spot gets at night.
* Search an address or place (Photon geocoder), jump between cities, adjust overlay opacity, share a view with the URL hash.
* Read the *About* panel for the method, per-city statistics and a frank list of limitations.

## How accurate is it?

Short answer: **within about 4 percentage points of the City of Toronto's own noise model** on the one quantity the two can be compared on, while being built from far worse input data.

Toronto Public Health's [2017 Environmental Noise Study](https://www.toronto.ca/wp-content/uploads/2017/11/8f4d-tph-Environmental-Noise-Study-2017.pdf) modelled the whole city with SoundPLAN, using real AADT counts refined by hourly traffic histograms, FHWA TNM 2.5 emission levels, ISO 9613-2 propagation and a land-use-regression correction surface. It published the share of Toronto residents above a set of thresholds, day and night, on the same LAeq metric over the same 23:00–07:00 night window. That table is directly reproducible from this map's raster:

| Threshold | This map | TPH 2017 | Difference |
|---|---|---|---|
| Day ≥ 65 dB | 21.5 % | 27.1 % | −5.6 |
| Day ≥ 55 dB | 68.6 % | 60.2 % | +8.4 |
| Night ≥ 55 dB | 30.6 % | 33.1 % | −2.5 |
| Night ≥ 45 dB | 77.6 % | 77.4 % | +0.2 |
| | | **mean absolute gap** | **4.2 points** |

Reproduce it with `python -m pipeline.validate`.

**Read that table honestly.** It is a comparison against a better-resourced *model*, not against ground truth, and TPH's own model is only so good itself: it reports R² 0.64 / RMSE 3.70 dB by day and R² 0.71 / RMSE 4.10 dB by night against its 220 measurement sites. Ours also has a visible bias — it over-predicts the middle of the distribution (+8.4 at day ≥55) and under-predicts the loud tail (−5.6 at day ≥65), which is exactly what you would expect from a model with no building shielding: noise from an arterial washes across the neighbourhood behind it instead of stopping at the first row of houses.

Two details matter for the comparison to be fair, and both are in `pipeline/validate.py`: TPH assesses each building at its **most exposed façade**, so the raster is sampled at the loudest point over each building's footprint rather than at its centroid, plus the conventional **+3 dB façade reflection**; and because Statistics Canada's servers are not reachable from the build environment, population is proxied by OSM residential **floor area** (footprint × storeys) rather than census counts.

## Run it locally

```bash
git clone https://github.com/Khayoon/canada-noise-map
cd canada-noise-map
python scripts/serve.py          # http://localhost:8000  (plain http.server won't do: PMTiles needs HTTP Range support)
```

The repository ships with the built tilesets, so the site works immediately.

## Rebuild the tiles

```bash
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m pipeline.download                        # fetches the 8 city extracts from BBBike into data/
python -m pipeline.build                           # ~12 min on a laptop -> web/tiles/*.pmtiles + JSON sidecars
python -m pytest                                   # physics + emission + encoding unit tests
python -m pipeline.validate                        # the TPH comparison above
python scripts/probe.py -79.3910 43.6385           # read a value from the built tiles (lon lat)
python tests/smoke_web.py                          # headless-Chromium test (needs: pip install playwright && playwright install chromium)
```

## How the model works

All knobs are in [`pipeline/config.py`](pipeline/config.py); the emission model is [`pipeline/emission.py`](pipeline/emission.py).

1. **Sources.** Every OSM way tagged `highway=*` (motorway … service), `railway=*` (rail, light rail, tram, surface subway) and `aeroway=runway` is a line source. Tunnels are skipped.

2. **Emission.** Levels are *not* hard-coded per road class. Each source's level comes from the standard line-source relationship used by every published road-noise method:

   *L*<sub>W',line</sub> = *L*<sub>W,vehicle</sub> + 10 log₁₀( *Q* / (1000 *v*) )
   *L*<sub>W,vehicle</sub> = 10 log₁₀( 10<sup>*L*<sub>WR</sub>/10</sup> + 10<sup>*L*<sub>WP</sub>/10</sup> ),  rolling *L*<sub>WR</sub> = *A*<sub>R</sub> + *B*<sub>R</sub> log₁₀(*v*/*v*<sub>ref</sub>),  propulsion *L*<sub>WP</sub> = *A*<sub>P</sub> + *B*<sub>P</sub>(*v*−*v*<sub>ref</sub>)/*v*<sub>ref</sub>

   with *v*<sub>ref</sub> = 70 km/h. This is the [CNOSSOS-EU](https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:32015L0996) road source formulation (Commission Directive (EU) 2015/996, Annex II). We work in A-weighted overall levels rather than octave bands, which is a deliberate simplification: without measured counts the dominant error is the flow *Q*, not the emission spectrum. Heavy vehicles enter through a car-equivalent flow at the standard 10:1 energy equivalence. The net behaviour is +3 dB per doubling of traffic and about +6 dB per doubling of speed.

   The single absolute constant is pinned to a published figure: a busy freeway measures **70–80 dB(A) at 50 ft (~15 m)**, the standard environmental-noise comparison value carried on state DOT noise pages such as [Colorado DOT's](https://www.codot.gov/programs/environmental/noise/noise-faqs). One carriageway is calibrated so a divided freeway reproduces the middle of that band.

   Rail and runways keep explicit per-class levels, because their emission is set by rolling stock and fleet mix and OSM carries no service-frequency information at all.

3. **Propagation.** Sources are burned into an energy raster on the Web-Mercator grid at zoom 13 (≈14 m on the ground) and convolved with a kernel
   *K(d) = (d² + h²)<sup>−p/2</sup> · 10<sup>−α·d/10</sup>*
   whose line integral gives the classic line-source decay *d*<sup>1−p</sup>: with *p* = 2.5 that is 4.5 dB per doubling of distance. α is the one parameter fitted rather than derived — see [What is assumed](#what-is-assumed-and-what-is-sourced).

4. **Summation.** All contributions are added in the energy domain — two identical roads side by side give +3 dB, a divided freeway mapped as two carriageways sums accordingly.

5. **Day and night.** Because level follows 10 log₁₀(flow), night is not a fixed offset: feeding the night hourly flow (the night share of AADT spread over 8 hours) through the same emission equation as the day flow produces the difference automatically. A freeway holding 11 % of its traffic overnight comes out about 6 dB down; a residential street at 5 % about 10 dB. Rail and aircraft keep explicit offsets, because their night behaviour is set by operations — freight runs all night, transit stops, the big airports have night-flight restriction programmes.

6. **Tiles.** Each period's raster is sliced into 256-px lossless-WebP tiles at zooms 6–13, coloured with a fixed palette and written to its own PMTiles archive. WebP is ~28 % smaller than optimised PNG and pixel-exact, so the page can decode a tile and invert the colour to recover the decibel value — no separate data tiles. Sparse country is only stored to zoom 11 and stretched by the renderer, which is what keeps a national archive small.

Cells more than 1.5 km from any modelled source are transparent ("no estimate") rather than painted a misleading "very quiet".

### Spot checks

Read straight out of the shipped tiles with `scripts/probe.py`:

| Location | Day | Night | Note |
|---|---|---|---|
| Highway 401 near Yonge, on the roadway | 71 dB | 63 dB | widest freeway in the country |
| Gardiner Expressway at Spadina | 69 dB | 62 dB | beside an elevated freeway |
| Pearson Airport, Terminal 1 | 73 dB | 64 dB | between the runways |
| Toronto financial district | 63 dB | 55 dB | dense street grid, moderate speeds |
| Autoroute 40 at Décarie, Montréal | 65 dB | 59 dB | |
| Rouge Park, beside the CN main line | 58 dB | 55 dB | only −3 dB at night: freight runs all night |
| Kitsilano residential, Vancouver | 52 dB | 43 dB | |
| Leaside residential side street | 49 dB | 40 dB | −9 dB at night as the street empties |

The Rouge Park and Leaside rows are the day/night model working as intended: a street empties out overnight and drops ~10 dB, while a freight corridor barely changes.

### What is assumed, and what is sourced

This is the part critics are right to go after, so it is stated plainly.

**Sourced:** the emission equations (CNOSSOS-EU); the line-source and energy-summation mathematics; the absolute anchor (70–80 dB(A) at 15 m for freeway traffic); the road, rail and runway geometry (OpenStreetMap).

**Assumed:** the traffic itself. `ROAD_TRAFFIC` in `pipeline/config.py` gives each OSM class an AADT, a speed, a heavy-vehicle share and a night share. These are ordinary planning-scale figures, not counts — they are stated in vehicles per day precisely so anyone can disagree with a number, and so that real counts (Ontario MTO publishes AADT for provincial highways; several cities publish municipal counts) drop straight in without touching the acoustics. OSM's own `lanes` and `maxspeed` tags refine them wherever they are present.

**Fitted:** one parameter. The kernel's absorption coefficient (12 dB/km) is mostly not air absorption — it stands in for the biggest thing the model does not do, which is shielding by buildings. ISO 9613-2 carries an explicit attenuation term for sound travelling *through* a housing area, worth up to about 10 dB over a built-up path; a shift-invariant kernel cannot trace a path, so that clutter is represented on average. It was chosen against the Toronto validation above (`python -m pipeline.calibrate`), which means the Toronto agreement is partly fitted and should not be read as an independent test. The other cities are.

## Building more of Canada

Coverage was never limited by OpenStreetMap — it was limited by BBBike, which only publishes extracts for a curated list of cities. [Geofabrik](https://download.geofabrik.de/north-america/canada.html) publishes every province, and a province extract contains every road in it. So going national needs *more regions*, not a different pipeline.

`pipeline/regionize.py` works out the regions from the data instead of making you write them by hand:

```bash
# 1. download whichever provinces you want from Geofabrik into data/
# 2. derive build regions from them
python -m pipeline.regionize data/ontario-latest.osm.pbf --out pipeline/regions-on.json
# 3. build exactly as before
python -m pipeline.build --regions pipeline/regions-on.json
```

It streams each extract once, counts road nodes into ~5 km cells, finds the connected clusters of populated cells, and emits one region per cluster — splitting anything larger than ~70 km into a grid and padding each box with a halo so sources just outside it still influence its edge. Overlapping halos resolve correctly because tiles merge by taking the louder value, and an edge always *under*-estimates. Regions that come out empty are skipped.

The build itself is unchanged: it is the same per-region loop, with more regions in it, and each region reads only the ways inside its own box, so one province file can feed dozens of regions without ever loading the province into memory.

This path is verified: splitting Halifax into four automatically-derived regions and rebuilding produces **identical** decibel values to the single-region build at every test point, because the halos overlap and tiles merge by taking the louder value (an edge always under-estimates, so the region that owns the interior wins).

Two things to watch at national scale: build time is roughly a minute of CPU per populated region, and archive size. Variable detail (`VARIABLE_DETAIL` in config) is what controls the second — full zoom-13 resolution is only stored where there is something to resolve, and empty country is stored coarse and stretched. The eight cities here come to 34 MB across both periods; a national build will need the tilesets moved to object storage (see [Deploying](#deploying)).

## Limitations — read before quoting a number

* **It is a model, not a measurement.** Traffic volumes are inferred from road class, lane count and speed limit, not counted. Real levels on any given street can differ by 5 dB or more.
* **No buildings, terrain or barriers.** Shielding is approximated by an average attenuation term, not computed. Back yards, courtyards and streets behind a highway berm are overestimated; the validation above shows this as a systematic over-prediction in the middle of the distribution.
* **Airports are crude.** Runways are line sources; real flight tracks, runway usage, fleet mix and night curfews are not modelled.
* **Extract edges.** Sources just outside a region's box are missing, so the last kilometre near an un-haloed edge reads quieter than it is.
* **Night is modelled, not counted.** The day/night split comes from class-based diurnal traffic assumptions, not measured hourly counts or real curfews. No Lden composite yet.
* **The Toronto validation is partly fitted**, as noted above. It is a consistency check, not an independent one.
* Local sources (construction, bars, industry, sirens) are absent.

## Data, licences and credits

* Map data © [OpenStreetMap contributors](https://www.openstreetmap.org/copyright), licensed under the [ODbL](https://opendatacommons.org/licenses/odbl/). City extracts from [BBBike](https://download.bbbike.org/osm/bbbike/); province extracts from [Geofabrik](https://download.geofabrik.de/). The noise raster is a *derivative database* of OSM and is therefore also released under the ODbL.
* Emission model after [CNOSSOS-EU](https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:32015L0996) (Commission Directive (EU) 2015/996, Annex II). Reference level from published state-DOT environmental-noise comparison figures. Validation target: [Toronto Public Health, *Environmental Noise Study in the City of Toronto*, April 2017](https://www.toronto.ca/wp-content/uploads/2017/11/8f4d-tph-Environmental-Noise-Study-2017.pdf).
* Basemap: [OpenFreeMap](https://openfreemap.org). Geocoding: [Photon](https://photon.komoot.io) by komoot (fair use — swap in your own instance for heavy traffic).
* Front end: [MapLibre GL JS](https://maplibre.org) (BSD-3), [PMTiles](https://github.com/protomaps/PMTiles) (BSD-3), both vendored in `web/vendor/`.
* Method inspiration: the BTS National Transportation Noise Map and the WHO / EU END noise-mapping conventions.
* Code: MIT (see `LICENSE`).

## Deploying

The site is the `web/` folder — plain static files, both tilesets included (~34 MB total: 18.8 MB day + 15.6 MB night).

**GitHub Pages (zero config):** push to GitHub, enable *Settings → Pages → Source: GitHub Actions*. The workflow in `.github/workflows/pages.yml` publishes `web/` on every push to `main`. GitHub Pages serves HTTP range requests, which is all PMTiles needs.

**Vercel:** `vercel --prod` from the repo root; `vercel.json` sets the output directory to `web`. Netlify and Cloudflare Pages work the same way (Cloudflare Pages caps files at 25 MB, so host the `.pmtiles` files on R2 and point `tilesBaseUrl` in `web/config.js` at the bucket).

If a tileset outgrows GitHub's 100 MB file limit — which a national build will — move the archives to Cloudflare R2 (free egress) and set `tilesBaseUrl`.

## Project layout

```
pipeline/            OSM -> noise raster -> PMTiles
  config.py          every model parameter, the traffic table and the palette
  emission.py        CNOSSOS-style flow-based emission model
  extract.py         pyosmium pass: ways -> line sources, optionally bbox-clipped
  model.py           grid, calibration, kernel, FFT convolution, dB raster
  tiles.py           pyramid, variable detail, palette, MBTiles -> PMTiles
  regionize.py       derive build regions from an extract (national coverage)
  build.py           the end-to-end CLI
  validate.py        population-weighted exposure vs Toronto Public Health 2017
  calibrate.py       sensitivity sweep behind the one fitted parameter
  download.py        fetch city extracts from BBBike
  regions.json       the eight-city region list
web/                 the static site (deploy this folder)
  index.html / app.js / style.css / config.js
  tiles/canada-noise-{day,night}.pmtiles, regions.json, palette.json, meta.json  (generated)
  vendor/            MapLibre GL JS + pmtiles.js
scripts/serve.py     dev server with Range support
tests/               pytest unit tests + Playwright smoke test
```

## Ideas for a next version

* Real traffic counts where they exist (Ontario MTO AADT for provincial highways, municipal count open data) instead of the class-based traffic table — the emission model already takes AADT directly, so this is a data job, not a modelling one.
* Building footprints as actual barriers (OSM has them) instead of an average attenuation term. This is the single biggest remaining error.
* Validation against a second city, so the fitted parameter can be tested somewhere it was not chosen.
* An Lden composite alongside the existing day and night layers.
