// Runtime configuration for the web app.  Edit this file when you deploy.
window.NOISE_CONFIG = {
  // PMTiles archive produced by the pipeline.  Relative paths resolve against
  // the page URL; use an absolute URL if you host the tiles elsewhere
  // (Cloudflare R2, a GitHub release asset, S3 ...).  The host must support
  // HTTP range requests - GitHub Pages, Vercel, Netlify, R2 and S3 all do.
  tilesUrl: "tiles/canada-noise.pmtiles",

  // Any MapLibre style JSON.  OpenFreeMap is free, key-less and OSM-based.
  basemapStyle: "https://tiles.openfreemap.org/styles/liberty",

  // Photon (komoot) geocoder - free, key-less, OSM-based, fair-use policy.
  geocoderUrl: "https://photon.komoot.io/api/",
  reverseUrl: "https://photon.komoot.io/reverse",

  // Shown in the About panel.
  repoUrl: "https://github.com/Khayoon/canada-noise-map",
};
