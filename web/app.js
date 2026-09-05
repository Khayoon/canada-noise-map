// Canada Noise Map – front end.
// Static page: MapLibre GL JS + a single PMTiles archive of pre-rendered
// noise tiles.  No server, no API keys.  See config.js for the knobs.

import * as maplibregl from "./vendor/maplibre-gl.mjs";

const CFG = window.NOISE_CONFIG;
const BASE_ZOOM = 12;            // overwritten from meta.json once loaded
const CANADA_BBOX = "-141.1,41.6,-52.5,83.2";

const $ = (id) => document.getElementById(id);
const els = {
  search: $("search"), results: $("results"), city: $("city"), opacity: $("opacity"),
  ramp: $("ramp"), ticks: $("ticks"), toast: $("toast"),
  about: $("about"), aboutBtn: $("about-btn"), aboutClose: $("about-close"),
  catTable: $("cat-table"), regionTable: $("region-table"), repoLink: $("repo-link"), builtAt: $("built-at"),
};

let palette, regions, meta;
let baseZoom = BASE_ZOOM;
let map, pm, popup, marker;
const tileCache = new Map();  // "z/x/y" -> ImageData | null
let colorIndex = null;        // "r,g,b" -> palette index

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------

async function loadJSON(path) {
  const r = await fetch(path, { cache: "no-cache" });
  if (!r.ok) throw new Error(`${path}: ${r.status}`);
  return r.json();
}

async function boot() {
  [palette, regions, meta] = await Promise.all([
    loadJSON("palette.json"), loadJSON("regions.json"), loadJSON("meta.json"),
  ]);
  baseZoom = meta.base_zoom ?? BASE_ZOOM;
  buildColorIndex();
  buildLegend();
  buildCityPicker();
  buildAbout();

  const tilesUrl = new URL(CFG.tilesUrl, location.href).href;
  pm = new pmtiles.PMTiles(tilesUrl);
  const protocol = new pmtiles.Protocol();
  protocol.add(pm);
  maplibregl.addProtocol("pmtiles", protocol.tile);

  const style = await loadBasemapStyle();
  const start = initialView();
  map = new maplibregl.Map({
    container: "map",
    style,
    center: start.center,
    zoom: start.zoom,
    hash: true,
    attributionControl: false,
    maxZoom: 18,
    minZoom: 3,
  });
  map.addControl(new maplibregl.AttributionControl({ compact: true }), "bottom-right");
  map.addControl(new maplibregl.NavigationControl({ visualizePitch: false }), "top-right");
  map.addControl(new maplibregl.GeolocateControl({ positionOptions: { enableHighAccuracy: true }, trackUserLocation: false }), "top-right");
  map.addControl(new maplibregl.ScaleControl({ unit: "metric" }), "bottom-right");

  map.on("load", () => {
    addNoiseLayer(tilesUrl);
    addCoverageOutline();
    map.on("click", onMapClick);
    map.getCanvas().style.cursor = "crosshair";
  });
  map.on("error", (e) => {
    if (e?.error?.message) console.warn("map error:", e.error.message);
  });

  wireSearch();
  els.opacity.addEventListener("input", () => {
    if (map.getLayer("noise")) map.setPaintProperty("noise", "raster-opacity", els.opacity.value / 100);
  });
  els.aboutBtn.addEventListener("click", () => (els.about.hidden = false));
  els.aboutClose.addEventListener("click", () => (els.about.hidden = true));
  els.about.addEventListener("click", (e) => { if (e.target === els.about) els.about.hidden = true; });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") { els.about.hidden = true; hideResults(); } });
}

function initialView() {
  // Respect a URL hash if present, otherwise start on the first region.
  if (location.hash.length > 3) return { center: [-79.38, 43.65], zoom: 10 };
  const r = regions[0];
  return { center: r.center, zoom: r.zoom };
}

async function loadBasemapStyle() {
  try {
    const r = await fetch(CFG.basemapStyle);
    if (!r.ok) throw new Error(r.status);
    return await r.json();
  } catch (err) {
    console.warn("basemap unavailable, using plain background:", err);
    toast("Basemap could not be loaded – showing the noise layer on a plain background.");
    return {
      version: 8,
      sources: {},
      layers: [{ id: "bg", type: "background", paint: { "background-color": "#f2f4f6" } }],
    };
  }
}

// ---------------------------------------------------------------------------
// Layers
// ---------------------------------------------------------------------------

function firstSymbolLayerId() {
  for (const l of map.getStyle().layers) if (l.type === "symbol") return l.id;
  return undefined;
}

function addNoiseLayer(tilesUrl) {
  map.addSource("noise", {
    type: "raster",
    url: "pmtiles://" + tilesUrl,
    tileSize: 256,
    attribution: "Noise model: <a href='" + CFG.repoUrl + "' target='_blank' rel='noopener'>Canada Noise Map</a>",
  });
  map.addLayer({
    id: "noise",
    type: "raster",
    source: "noise",
    paint: {
      "raster-opacity": els.opacity.value / 100,
      "raster-resampling": "linear",
      "raster-fade-duration": 150,
    },
  }, firstSymbolLayerId());
}

function addCoverageOutline() {
  const fc = {
    type: "FeatureCollection",
    features: regions.map((r) => {
      const [w, s, e, n] = r.bbox;
      return { type: "Feature", properties: { id: r.id, name: r.name },
        geometry: { type: "Polygon", coordinates: [[[w, s], [e, s], [e, n], [w, n], [w, s]]] } };
    }),
  };
  map.addSource("coverage", { type: "geojson", data: fc });
  map.addLayer({
    id: "coverage-outline", type: "line", source: "coverage",
    paint: { "line-color": "#1f6feb", "line-width": 1.2, "line-dasharray": [3, 2], "line-opacity": ["interpolate", ["linear"], ["zoom"], 8, 0.9, 11, 0.0] },
  });
  if (!map.getStyle().glyphs) return;  // plain fallback style has no fonts
  map.addLayer({
    id: "coverage-label", type: "symbol", source: "coverage", maxzoom: 8,
    layout: { "text-field": ["get", "name"], "text-size": 12, "text-font": ["Noto Sans Regular"], "text-allow-overlap": false },
    paint: { "text-color": "#1f6feb", "text-halo-color": "#fff", "text-halo-width": 1.5 },
  });
}

// ---------------------------------------------------------------------------
// Reading values back out of the tiles
// ---------------------------------------------------------------------------

function buildColorIndex() {
  colorIndex = new Map();
  palette.lut.forEach((rgb, i) => { if (i > 0) colorIndex.set(rgb.join(","), i); });
}

function lngLatToTile(lng, lat, z) {
  const n = 256 * Math.pow(2, z);
  const x = (lng + 180) / 360 * n;
  const latr = lat * Math.PI / 180;
  const y = (1 - Math.log(Math.tan(latr) + 1 / Math.cos(latr)) / Math.PI) / 2 * n;
  return { tx: Math.floor(x / 256), ty: Math.floor(y / 256), px: Math.floor(x) % 256, py: Math.floor(y) % 256 };
}

async function tileImageData(z, x, y) {
  const key = `${z}/${x}/${y}`;
  if (tileCache.has(key)) return tileCache.get(key);
  let out = null;
  try {
    const res = await pm.getZxy(z, x, y);
    if (res && res.data) {
      const blob = new Blob([res.data], { type: "image/png" });
      const bmp = await createImageBitmap(blob, { colorSpaceConversion: "none", premultiplyAlpha: "none" });
      const canvas = document.createElement("canvas");
      canvas.width = 256; canvas.height = 256;
      const ctx = canvas.getContext("2d", { willReadFrequently: true });
      ctx.drawImage(bmp, 0, 0);
      out = ctx.getImageData(0, 0, 256, 256);
    }
  } catch (err) {
    console.warn("tile read failed", key, err);
  }
  if (tileCache.size > 64) tileCache.delete(tileCache.keys().next().value);
  tileCache.set(key, out);
  return out;
}

function nearestIndex(r, g, b) {
  const exact = colorIndex.get(`${r},${g},${b}`);
  if (exact !== undefined) return exact;
  let best = -1, bestD = Infinity;
  for (const [k, i] of colorIndex) {
    const [pr, pg, pb] = k.split(",").map(Number);
    const d = (pr - r) ** 2 + (pg - g) ** 2 + (pb - b) ** 2;
    if (d < bestD) { bestD = d; best = i; }
  }
  return best;
}

/** Returns {db} for a covered location, {db:null} for no-data, or null outside all cities. */
async function readNoise(lng, lat) {
  const inside = regions.some((r) => lng >= r.bbox[0] && lng <= r.bbox[2] && lat >= r.bbox[1] && lat <= r.bbox[3]);
  if (!inside) return null;
  const { tx, ty, px, py } = lngLatToTile(lng, lat, baseZoom);
  const img = await tileImageData(baseZoom, tx, ty);
  if (!img) return { db: null };
  const o = (py * 256 + px) * 4;
  const a = img.data[o + 3];
  if (a === 0) return { db: null };
  const idx = nearestIndex(img.data[o], img.data[o + 1], img.data[o + 2]);
  if (idx <= 0) return { db: null };
  return { db: idx / palette.scale };
}

// ---------------------------------------------------------------------------
// Interpretation helpers
// ---------------------------------------------------------------------------

function category(db) {
  for (const c of palette.categories) if (db < c.max_db) return c;
  return palette.categories[palette.categories.length - 1];
}

function quietScore(db) {
  // 100 = 40 dB or less, 50 = 80 dB or more (a Soundscore-style scale).
  return Math.round(Math.max(50, Math.min(100, 100 - 1.25 * Math.max(0, db - 40))));
}

function colorFor(db) {
  const i = Math.max(1, Math.min(255, Math.round(db * palette.scale)));
  const [r, g, b] = palette.lut[i];
  return `rgb(${r},${g},${b})`;
}

function textColorOn(db) {
  const i = Math.max(1, Math.min(255, Math.round(db * palette.scale)));
  const [r, g, b] = palette.lut[i];
  return (0.299 * r + 0.587 * g + 0.114 * b) > 150 ? "#1b1f23" : "#fff";
}

// ---------------------------------------------------------------------------
// Click / lookup
// ---------------------------------------------------------------------------

async function onMapClick(e) {
  await showReading(e.lngLat.lng, e.lngLat.lat);
}

async function showReading(lng, lat, addressHint) {
  if (popup) popup.remove();
  if (!marker) marker = new maplibregl.Marker({ color: "#1f6feb" });
  marker.setLngLat([lng, lat]).addTo(map);

  const reading = await readNoise(lng, lat);
  let html;
  if (reading === null) {
    html = `<div class="pop-none"><b>Not covered yet.</b><br>Cities so far: ${regions.map((r) => r.name).join(", ")}.</div>`;
  } else if (reading.db === null) {
    html = `<div class="pop-none"><b>No estimate here.</b><br>No road, railway or runway within 1.5 km of this point (water, parkland or beyond the modelled area).</div>`;
  } else {
    const db = reading.db;
    const cat = category(db);
    html = `
      <div class="pop-level">
        <div class="pop-db">${Math.round(db)}<small>dB(A)</small></div>
        <span class="pop-cat" style="background:${colorFor(db)};color:${textColorOn(db)}">${cat.label}</span>
      </div>
      <p class="pop-blurb">${cat.blurb}</p>
      <p class="pop-score">Quiet score <b>${quietScore(db)}</b> / 100 · estimated daytime average</p>
      <p class="pop-addr" id="pop-addr">${addressHint ? escapeHtml(addressHint) : ""}</p>`;
  }
  popup = new maplibregl.Popup({ closeOnClick: false, offset: 28, maxWidth: "300px" })
    .setLngLat([lng, lat]).setHTML(html).addTo(map);
  popup.on("close", () => marker && marker.remove());

  if (reading && reading.db !== null && !addressHint) reverseGeocode(lng, lat);
}

async function reverseGeocode(lng, lat) {
  try {
    const r = await fetch(`${CFG.reverseUrl}?lon=${lng.toFixed(6)}&lat=${lat.toFixed(6)}&lang=en`);
    if (!r.ok) return;
    const j = await r.json();
    const f = j.features && j.features[0];
    if (!f) return;
    const label = formatPlace(f.properties);
    const el = document.getElementById("pop-addr");
    if (el && label) el.textContent = label;
  } catch { /* offline or blocked: silently skip */ }
}

// ---------------------------------------------------------------------------
// Search (Photon)
// ---------------------------------------------------------------------------

function formatPlace(p) {
  const line1 = [p.housenumber, p.street].filter(Boolean).join(" ") || p.name || "";
  const line2 = [p.city || p.town || p.village || p.county, p.state, p.postcode].filter(Boolean).join(", ");
  return [line1, line2].filter(Boolean).join(", ");
}

let searchTimer = null, activeIdx = -1, currentResults = [];

function wireSearch() {
  els.search.addEventListener("input", () => {
    clearTimeout(searchTimer);
    const q = els.search.value.trim();
    if (q.length < 3) { hideResults(); return; }
    searchTimer = setTimeout(() => runSearch(q), 300);
  });
  els.search.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown") { e.preventDefault(); moveActive(1); }
    else if (e.key === "ArrowUp") { e.preventDefault(); moveActive(-1); }
    else if (e.key === "Enter") {
      e.preventDefault();
      if (currentResults.length) pickResult(currentResults[Math.max(0, activeIdx)]);
      else runSearch(els.search.value.trim(), true);
    }
  });
  document.addEventListener("click", (e) => { if (!e.target.closest(".search-row")) hideResults(); });
}

async function runSearch(q, pickFirst = false) {
  if (!q) return;
  const c = map ? map.getCenter() : { lng: -79.38, lat: 43.65 };
  const url = `${CFG.geocoderUrl}?q=${encodeURIComponent(q)}&limit=6&lang=en&lat=${c.lat.toFixed(4)}&lon=${c.lng.toFixed(4)}&bbox=${CANADA_BBOX}`;
  try {
    const r = await fetch(url);
    if (!r.ok) throw new Error(r.status);
    const j = await r.json();
    currentResults = (j.features || []).map((f) => ({
      label: f.properties.name || formatPlace(f.properties),
      sub: formatPlace(f.properties),
      lng: f.geometry.coordinates[0], lat: f.geometry.coordinates[1],
      type: f.properties.osm_value,
    }));
    if (pickFirst && currentResults.length) return pickResult(currentResults[0]);
    renderResults();
  } catch (err) {
    console.warn("search failed", err);
    toast("Search is unavailable right now (the geocoder could not be reached).");
  }
}

function renderResults() {
  els.results.innerHTML = "";
  activeIdx = -1;
  if (!currentResults.length) {
    els.results.innerHTML = "<li><small>No results</small></li>";
    els.results.hidden = false;
    return;
  }
  currentResults.forEach((res, i) => {
    const li = document.createElement("li");
    li.innerHTML = `${escapeHtml(res.label)}<small>${escapeHtml(res.sub)}</small>`;
    li.addEventListener("click", () => pickResult(res));
    li.dataset.idx = i;
    els.results.appendChild(li);
  });
  els.results.hidden = false;
}

function moveActive(delta) {
  if (!currentResults.length) return;
  activeIdx = (activeIdx + delta + currentResults.length) % currentResults.length;
  [...els.results.children].forEach((li, i) => li.classList.toggle("active", i === activeIdx));
}

function hideResults() { els.results.hidden = true; }

async function pickResult(res) {
  hideResults();
  els.search.value = res.label;
  const isAddress = ["house", "residential", "building", "yes", "apartments", "detached"].includes(res.type) || /\d/.test(res.label);
  map.flyTo({ center: [res.lng, res.lat], zoom: isAddress ? 15.5 : Math.max(map.getZoom(), 12), duration: 900 });
  map.once("moveend", () => showReading(res.lng, res.lat, res.sub));
}

// ---------------------------------------------------------------------------
// UI bits
// ---------------------------------------------------------------------------

function buildLegend() {
  const lo = 35, hi = 85;
  const stops = palette.stops.filter((s) => s.db >= lo && s.db <= hi)
    .map((s) => `${s.color} ${((s.db - lo) / (hi - lo) * 100).toFixed(1)}%`);
  els.ramp.style.background = `linear-gradient(90deg, ${stops.join(", ")})`;
  els.ticks.innerHTML = [35, 45, 55, 65, 75, 85].map((v) => `<span>${v}</span>`).join("");
}

function buildCityPicker() {
  els.city.innerHTML = regions.map((r) => `<option value="${r.id}">${r.name}${r.province ? " · " + r.province : ""}</option>`).join("");
  els.city.addEventListener("change", () => {
    const r = regions.find((x) => x.id === els.city.value);
    if (!r) return;
    map.fitBounds([[r.bbox[0], r.bbox[1]], [r.bbox[2], r.bbox[3]]], { padding: 20, duration: 900 });
  });
  if (map) return;
}

function buildAbout() {
  els.catTable.innerHTML = palette.categories.map((c, i) => {
    const prev = i === 0 ? palette.floor_db : palette.categories[i - 1].max_db;
    const range = c.max_db > 200 ? `≥ ${prev} dB` : `${prev}–${c.max_db} dB`;
    const mid = c.max_db > 200 ? prev + 5 : (prev + c.max_db) / 2;
    return `<tr><td><span class="swatch" style="background:${colorFor(mid)}"></span><b>${c.label}</b></td><td>${range}</td><td>${c.blurb}</td></tr>`;
  }).join("");
  els.regionTable.innerHTML = regions.map((r) => {
    const airports = (r.airports || []).filter((a) => a[2] !== "minor").map((a) => a[1] || a[0]).join(", ") || "–";
    return `<tr><td>${r.name}</td><td>${(r.osm_timestamp || "").slice(0, 10)}</td><td>${r.stats.median_db} dB</td><td>${Math.round(r.stats.share_over_65 * 100)}%</td><td>${airports}</td></tr>`;
  }).join("");
  els.repoLink.href = CFG.repoUrl;
  els.builtAt.textContent = meta.built_at ? ` Tiles built ${meta.built_at.slice(0, 10)} (${meta.tiles_mb} MB).` : "";
}

let toastTimer = null;
function toast(msg, ms = 4000) {
  els.toast.textContent = msg;
  els.toast.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (els.toast.hidden = true), ms);
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

boot().catch((err) => {
  console.error(err);
  toast("Could not start the map: " + err.message, 10000);
});

// Expose a little test hook (used by the Playwright smoke test).
window.__noise = { readNoise: (lng, lat) => readNoise(lng, lat), ready: () => !!map };
