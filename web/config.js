// Runtime configuration for the web app.  Edit this file when you deploy.
window.NOISE_CONFIG = {
  // Where the PMTiles archives live.  The build writes one archive per period
  // (tiles/canada-noise-day.pmtiles, tiles/canada-noise-night.pmtiles) and
  // records their paths in meta.json; this is just a prefix in front of them.
  // Leave "" to serve them from this site, or point it at object storage
  // (Cloudflare R2, S3 ...) if the tileset outgrows the repo.  Whatever host
  // you use must support HTTP range requests - GitHub Pages, Vercel, Netlify,
  // R2 and S3 all do.
  tilesBaseUrl: "",

  // Any MapLibre style JSON.  OpenFreeMap is free, key-less and OSM-based.
  basemapStyle: "https://tiles.openfreemap.org/styles/liberty",

  // Photon (komoot) geocoder - free, key-less, OSM-based, fair-use policy.
  geocoderUrl: "https://photon.komoot.io/api/",
  reverseUrl: "https://photon.komoot.io/reverse",

  // Shown in the About panel.
  repoUrl: "https://github.com/Khayoon/canada-noise-map",
};
