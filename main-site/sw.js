/*
  Bump CACHE on every deploy that changes anything this worker serves. The
  browser compares this file byte for byte, so an unchanged version means no
  update is ever found and the update bar in js/update.js never shows.
*/
const CACHE = "malaysia-boleh-v34";
const API_CACHE = "malaysia-boleh-api-v5";

const ASSETS = [
  "/",
  "/index.html",
  "/script.js",
  "/css/base.css",
  "/css/themes.css",
  "/js/icons.js",
  "/js/ui.js",
  "/js/theme.js",
  "/js/update.js",
  "/js/map-fullscreen.js",
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
  "/prayer/",
  "/prayer/index.html",
  "/prayer/prayer.css",
  "/prayer/prayer.js",
  "/prayer/prayer-alerts.js",
  "/prayer/prayer-qibla.js",
  "/MYB-main.png",
  "/MYB-badge.png",
  "/MYB-192.png",
  "/MYB-512.png",
  "/favicon.ico",
  "/manifest.json"
];

/* -- Install: cache shell -- */

/*
  No skipWaiting() here and no clients.claim() in activate. A new worker
  installs and then waits; the only thing that promotes it is the reader
  pressing Reload in the update bar, which arrives as the message below.
*/
self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE)
    .then(cache => cache.addAll(ASSETS))
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
  );
});

/* -- Message: the reader accepted the update -- */

self.addEventListener('message', event => {
  const type = typeof event.data === 'string' ? event.data : event.data && event.data.type;

  // The only place either of these is ever called.
  if (type === 'skip-waiting') {
    event.waitUntil(self.skipWaiting().then(() => self.clients.claim()));
  }
});

/* -- Prayer alerts, sent by the cron (api/cron/prayer-alerts.js) -- */

self.addEventListener('push', event => {
  let data = null;
  try {
    data = event.data && event.data.json();
  } catch (e) { /* not ours */ }
  if (!data || data.type !== 'prayer-alert') return;

  // One tag, so the next prayer replaces the last notification rather than
  // stacking; renotify still makes it heard.
  event.waitUntil(
    self.registration.showNotification(data.title || 'Prayer time', {
      body: data.body || '',
      tag: 'prayer-alert',
      renotify: true,
      icon: '/MYB-192.png',
      // The status bar's icon. Android draws only its shape, so this is the
      // crescent and star on a transparent background, not the app icon.
      badge: '/MYB-badge.png',
      data: { url: data.url || '/prayer/' }
    })
  );
});

// Tapping one opens the prayer page: in an open window if there is one,
// otherwise a new one.
self.addEventListener('notificationclick', event => {
  event.notification.close();
  const url = new URL((event.notification.data && event.notification.data.url) || '/prayer/', self.location.origin).href;
  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(clients => {
      const client = clients.find(c => new URL(c.url).origin === self.location.origin);
      if (!client) return self.clients.openWindow(url);
      // navigate() refuses a window this worker doesn't control; that one
      // stays where it is, focused.
      return client.focus()
        .then(c => (c && c.url !== url ? c.navigate(url) : c))
        .catch(() => client);
    })
  );
});

/* -- Fetch: strategy per route -- */

self.addEventListener('fetch', event => {
  const {
    request
  } = event;
  const url = new URL(request.url);

  // Alerts sign-up and the cron talk to the server and nothing else.
  if (request.method !== 'GET' || url.pathname.startsWith('/api/push/') || url.pathname.startsWith('/api/cron/')) {
    return;
  }

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