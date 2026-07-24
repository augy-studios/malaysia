const CACHE = "malaysia-boleh-v24";
const API_CACHE = "malaysia-boleh-api-v5";

const ASSETS = [
  "/",
  "/index.html",
  "/script.js",
  "/css/base.css",
  "/css/themes.css",
  "/js/icons.js",
  "/js/theme.js",
  "/weather/",
  "/weather/index.html",
  "/weather/weather.css",
  "/weather/weather.js",
  "/quake/",
  "/quake/index.html",
  "/quake/quake.css",
  "/quake/quake.js",
  "/flood-alerts/",
  "/flood-alerts/index.html",
  "/flood-alerts/flood-alerts.css",
  "/flood-alerts/flood-alerts.js",
  "/trains/",
  "/trains/index.html",
  "/trains/trains.css",
  "/trains/trains.js",
  "/bus/",
  "/bus/index.html",
  "/bus/bus.css",
  "/bus/bus.js",
  "/MYB-main.png",
  "/MYB-192.png",
  "/MYB-512.png",
  "/favicon.ico",
  "/manifest.json"
];

/* -- Install: cache shell -- */

self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE)
    .then(cache => cache.addAll(ASSETS))
    .then(() => self.skipWaiting())
  );
});

/* -- Activate: clean old caches -- */

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys()
    .then(keys =>
      Promise.all(
        keys
        .filter(k => k !== CACHE && k !== API_CACHE)
        .map(k => caches.delete(k))
      )
    )
    .then(() => self.clients.claim())
  );
});

/* -- Fetch: strategy per route -- */

self.addEventListener('fetch', event => {
  const {
    request
  } = event;
  const url = new URL(request.url);

  // API - network-first, falling back to last-known cached response when offline
  if (url.pathname.startsWith('/api/')) {
    event.respondWith(networkFirstApi(request));
    return;
  }

  // Google Fonts - cache-first (immutable)
  if (url.hostname === 'fonts.googleapis.com' || url.hostname === 'fonts.gstatic.com') {
    event.respondWith(cacheFirst(request));
    return;
  }

  // static assets - cache-first
  event.respondWith(cacheFirst(request));
});

/* -- Strategies -- */

async function networkFirstApi(request) {
  const cache = await caches.open(API_CACHE);

  try {
    const response = await fetch(request);
    if (response.ok && request.method === 'GET') {
      cache.put(request, response.clone());
    }
    return response;
  } catch {
    const cached = await cache.match(request);
    if (cached) {
      const headers = new Headers(cached.headers);
      headers.set('X-Served-From', 'offline-cache');
      return new Response(cached.body, {
        status: cached.status,
        statusText: cached.statusText,
        headers
      });
    }

    return new Response(
      JSON.stringify({
        success: false,
        error: 'You appear to be offline and no cached data is available yet.'
      }), {
        status: 503,
        headers: {
          'Content-Type': 'application/json'
        },
      }
    );
  }
}

async function cacheFirst(request) {
  const cached = await caches.match(request);
  if (cached) return cached;

  try {
    const response = await fetch(request);
    if (response.ok) {
      const cache = await caches.open(CACHE);
      cache.put(request, response.clone());
    }
    return response;
  } catch {
    // offline - fallback for navigation
    if (request.mode === 'navigate') {
      return caches.match('/index.html');
    }
    return new Response('Offline', {
      status: 503
    });
  }
}