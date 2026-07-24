(function () {
  "use strict";

  const STATIC_CACHE_MS = 60 * 60 * 1000; // 1 hour, generous - schedules barely change
  const PRASARANA_CATEGORIES = ["rapid-bus-kl", "rapid-bus-penang", "rapid-bus-mrtfeeder"];
  const CATEGORY_LABELS = {
    "rapid-bus-kl": "Rapid Bus KL",
    "rapid-bus-penang": "Rapid Bus Penang",
    "rapid-bus-mrtfeeder": "Rapid Bus MRT Feeder",
  };

  function iconEl(name, size) {
    return window.Icons ? window.Icons.html(name, { size: size || 20 }) : "";
  }

  function hydrateIcons(root) {
    (root || document).querySelectorAll("[data-icon]").forEach((el) => {
      const name = el.getAttribute("data-icon");
      if (!el.querySelector("svg")) {
        el.innerHTML = iconEl(name, el.classList.contains("bus-tab-icon") ? 18 : 20);
      }
    });
  }

  function escapeHtml(str) {
    return String(str == null ? "" : str).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  function formatBytes(bytes) {
    if (bytes == null || Number.isNaN(bytes)) return "Unknown size";
    const units = ["B", "KB", "MB", "GB"];
    let val = bytes;
    let i = 0;
    while (val >= 1024 && i < units.length - 1) {
      val /= 1024;
      i += 1;
    }
    return `${val.toFixed(val >= 10 || i === 0 ? 0 : 1)} ${units[i]}`;
  }

  function formatTimeAgo(date) {
    if (!date) return "";
    const secs = Math.max(0, Math.round((Date.now() - date.getTime()) / 1000));
    if (secs < 5) return "just now";
    if (secs < 60) return `${secs}s ago`;
    const mins = Math.round(secs / 60);
    if (mins < 60) return `${mins}m ago`;
    const hrs = Math.round(mins / 60);
    return `${hrs}h ago`;
  }

  function loadingHtml(text) {
    return `<div class="bus-loading"><span class="spinner"></span><span>${escapeHtml(text)}</span></div>`;
  }

  function errorBanner(message, retryId) {
    return `<div class="state-banner danger">${iconEl("alertOctagon", 18)}<span>${escapeHtml(message)}</span></div>` +
      `<button class="btn" id="${retryId}" type="button">${iconEl("refresh", 16)} Retry</button>`;
  }

  function infoBanner(message) {
    return `<div class="state-banner info">${iconEl("alertTriangle", 18)}<span>${escapeHtml(message)}</span></div>`;
  }

  async function fetchJson(url, timeoutMs) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), timeoutMs || 10000);
    try {
      const res = await fetch(url, { signal: controller.signal });
      clearTimeout(timeout);
      let payload = null;
      try {
        payload = await res.json();
      } catch (e) {
        throw new Error("Server returned an unreadable response.");
      }
      if (!res.ok || !payload || payload.success === false) {
        throw new Error((payload && payload.error) || `Request failed (HTTP ${res.status}).`);
      }
      return payload;
    } catch (err) {
      clearTimeout(timeout);
      if (err && err.name === "AbortError") {
        throw new Error("Request timed out. Please try again.");
      }
      throw err;
    }
  }

  // ---------------------------------------------------------------------
  // Static schedule (GTFS manifest) sections
  // ---------------------------------------------------------------------

  function staticCacheKey(kind) {
    return `bus-static-${kind}`;
  }

  function readStaticCache(kind) {
    try {
      const raw = sessionStorage.getItem(staticCacheKey(kind));
      if (!raw) return null;
      const parsed = JSON.parse(raw);
      if (!parsed || !parsed.savedAt || Date.now() - parsed.savedAt > STATIC_CACHE_MS) return null;
      return parsed.data;
    } catch (e) {
      return null;
    }
  }

  function writeStaticCache(kind, data) {
    try {
      sessionStorage.setItem(staticCacheKey(kind), JSON.stringify({ savedAt: Date.now(), data }));
    } catch (e) {
      /* sessionStorage unavailable/full - degrade silently */
    }
  }

  function renderStaticManifest(container, manifest, label) {
    const rows = [
      ["Feed", label],
      ["Format", "GTFS static bundle (zip)"],
      ["Size", formatBytes(manifest.sizeBytes)],
      ["Last modified", manifest.lastModified ? new Date(manifest.lastModified).toLocaleString() : "Unknown"],
    ];

    const rowsHtml = rows.map(([k, v]) => `<div class="bus-manifest-row"><dt>${escapeHtml(k)}</dt><dd>${escapeHtml(v)}</dd></div>`).join("");

    container.innerHTML = infoBanner(
      "This is a full GTFS bundle (stops, routes, trips and shapes as CSV inside a zip) - too large to parse and search in the browser, so here's the manifest with a direct download link instead of faked data."
    ) + `<dl class="bus-manifest">${rowsHtml}</dl>` +
      `<a class="btn bus-download-link" href="${escapeHtml(manifest.downloadUrl)}" target="_blank" rel="noopener noreferrer">${iconEl("externalLink", 16)} Download GTFS feed</a>`;

    hydrateIcons(container);
  }

  async function loadPrasaranaStatic(category) {
    const container = document.getElementById("prasaranaStaticBody");
    const cacheKind = `prasarana-${category}`;
    const cached = readStaticCache(cacheKind);
    if (cached) {
      renderStaticManifest(container, cached, CATEGORY_LABELS[category] || category);
      return;
    }
    container.innerHTML = loadingHtml("Loading GTFS feed info...");
    try {
      const payload = await fetchJson(`/api/bus-prasarana?category=${encodeURIComponent(category)}`);
      writeStaticCache(cacheKind, payload.data);
      renderStaticManifest(container, payload.data, CATEGORY_LABELS[category] || category);
    } catch (err) {
      container.innerHTML = errorBanner(err.message || "Could not load GTFS feed info.", "prasaranaStaticRetry");
      hydrateIcons(container);
      const btn = document.getElementById("prasaranaStaticRetry");
      if (btn) btn.addEventListener("click", () => loadPrasaranaStatic(category));
    }
  }

  async function loadMybasStatic() {
    const container = document.getElementById("mybasStaticBody");
    const cached = readStaticCache("mybas-johor");
    if (cached) {
      renderStaticManifest(container, cached, "myBAS Johor Bahru");
      return;
    }
    container.innerHTML = loadingHtml("Loading GTFS feed info...");
    try {
      const payload = await fetchJson("/api/bus-mybas-johor");
      writeStaticCache("mybas-johor", payload.data);
      renderStaticManifest(container, payload.data, "myBAS Johor Bahru");
    } catch (err) {
      container.innerHTML = errorBanner(err.message || "Could not load GTFS feed info.", "mybasStaticRetry");
      hydrateIcons(container);
      const btn = document.getElementById("mybasStaticRetry");
      if (btn) btn.addEventListener("click", loadMybasStatic);
    }
  }

  // ---------------------------------------------------------------------
  // Live vehicle position sections
  // ---------------------------------------------------------------------

  function vehicleTimestampMs(vehicle) {
    const ts = vehicle && (vehicle.timestamp != null ? vehicle.timestamp : null);
    if (ts == null) return 0;
    const num = Number(ts);
    return Number.isFinite(num) ? num * 1000 : 0;
  }

  function renderVehicles(container, vehicles) {
    if (!Array.isArray(vehicles) || vehicles.length === 0) {
      container.innerHTML = infoBanner("No live positions currently reported for this feed.");
      return;
    }

    const sorted = vehicles.slice().sort((a, b) => vehicleTimestampMs(b) - vehicleTimestampMs(a));

    const rows = sorted.map((v) => {
      const routeId = (v.trip && v.trip.routeId) || "Unknown route";
      const vehicleId = (v.vehicle && (v.vehicle.label || v.vehicle.id)) || v.entityId || "Unknown vehicle";
      const lat = v.position && v.position.latitude;
      const lon = v.position && v.position.longitude;
      const hasCoords = typeof lat === "number" && typeof lon === "number" && !Number.isNaN(lat) && !Number.isNaN(lon);
      const tsMs = vehicleTimestampMs(v);
      const timeLabel = tsMs ? formatTimeAgo(new Date(tsMs)) : "Unknown";
      const mapsUrl = hasCoords ? `https://www.google.com/maps?q=${lat},${lon}` : null;

      return `<div class="bus-vehicle-row">
        <span class="bus-vehicle-icon" data-icon="mapPin"></span>
        <span class="bus-vehicle-main">
          <span class="bus-vehicle-route">${escapeHtml(routeId)} &middot; ${escapeHtml(vehicleId)}</span>
          <span class="bus-vehicle-meta">${hasCoords
          ? `${lat.toFixed(5)}, ${lon.toFixed(5)}${mapsUrl ? ` &middot; <a class="bus-vehicle-link" href="${escapeHtml(mapsUrl)}" target="_blank" rel="noopener noreferrer">${iconEl("externalLink", 13)} Map</a>` : ""}`
          : "Coordinates unavailable"
        }</span>
        </span>
        <span class="bus-vehicle-time">${escapeHtml(timeLabel)}</span>
      </div>`;
    }).join("");

    container.innerHTML = `<div class="bus-vehicle-list">${rows}</div>`;
    hydrateIcons(container);
  }

  async function loadPrasaranaRealtime(category) {
    const container = document.getElementById("prasaranaRtBody");
    const updatedEl = document.getElementById("prasaranaRtUpdated");
    const refreshBtn = document.getElementById("prasaranaRtRefresh");
    container.innerHTML = loadingHtml("Loading live positions...");
    if (refreshBtn) refreshBtn.classList.add("is-loading");
    try {
      const payload = await fetchJson(`/api/bus-prasarana-realtime?category=${encodeURIComponent(category)}`);
      renderVehicles(container, payload.data && payload.data.vehicles);
      if (updatedEl) updatedEl.textContent = `Last updated ${new Date(payload.fetchedAt || Date.now()).toLocaleTimeString()}`;
    } catch (err) {
      container.innerHTML = errorBanner(err.message || "Could not load live positions.", "prasaranaRtRetry");
      hydrateIcons(container);
      const btn = document.getElementById("prasaranaRtRetry");
      if (btn) btn.addEventListener("click", () => loadPrasaranaRealtime(category));
    } finally {
      if (refreshBtn) refreshBtn.classList.remove("is-loading");
    }
  }

  async function loadMybasRealtime() {
    const container = document.getElementById("mybasRtBody");
    const updatedEl = document.getElementById("mybasRtUpdated");
    const refreshBtn = document.getElementById("mybasRtRefresh");
    container.innerHTML = loadingHtml("Loading live positions...");
    if (refreshBtn) refreshBtn.classList.add("is-loading");
    try {
      const payload = await fetchJson("/api/bus-mybas-johor-realtime");
      renderVehicles(container, payload.data && payload.data.vehicles);
      if (updatedEl) updatedEl.textContent = `Last updated ${new Date(payload.fetchedAt || Date.now()).toLocaleTimeString()}`;
    } catch (err) {
      container.innerHTML = errorBanner(err.message || "Could not load live positions.", "mybasRtRetry");
      hydrateIcons(container);
      const btn = document.getElementById("mybasRtRetry");
      if (btn) btn.addEventListener("click", loadMybasRealtime);
    } finally {
      if (refreshBtn) refreshBtn.classList.remove("is-loading");
    }
  }

  // ---------------------------------------------------------------------
  // Tabs / category switcher / wiring
  // ---------------------------------------------------------------------

  function selectTab(which) {
    const tabPrasarana = document.getElementById("tabPrasarana");
    const tabMybas = document.getElementById("tabMybas");
    const panelPrasarana = document.getElementById("panelPrasarana");
    const panelMybas = document.getElementById("panelMybas");

    const isPrasarana = which === "prasarana";
    tabPrasarana.setAttribute("aria-selected", String(isPrasarana));
    tabMybas.setAttribute("aria-selected", String(!isPrasarana));
    panelPrasarana.hidden = !isPrasarana;
    panelMybas.hidden = isPrasarana;
  }

  document.addEventListener("DOMContentLoaded", () => {
    hydrateIcons(document);

    const tabPrasarana = document.getElementById("tabPrasarana");
    const tabMybas = document.getElementById("tabMybas");
    const categorySelect = document.getElementById("prasaranaCategory");

    tabPrasarana.addEventListener("click", () => selectTab("prasarana"));
    tabMybas.addEventListener("click", () => {
      selectTab("mybas");
      loadMybasStatic();
      loadMybasRealtime();
    });

    categorySelect.addEventListener("change", () => {
      const category = PRASARANA_CATEGORIES.includes(categorySelect.value) ? categorySelect.value : "rapid-bus-kl";
      loadPrasaranaStatic(category);
      loadPrasaranaRealtime(category);
    });

    document.getElementById("prasaranaRtRefresh").addEventListener("click", () => {
      loadPrasaranaRealtime(categorySelect.value || "rapid-bus-kl");
    });
    document.getElementById("mybasRtRefresh").addEventListener("click", loadMybasRealtime);

    // Initial load: Rapid Bus tab, default category.
    loadPrasaranaStatic("rapid-bus-kl");
    loadPrasaranaRealtime("rapid-bus-kl");
  });
})();
