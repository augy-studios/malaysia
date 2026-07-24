(function () {
  "use strict";

  const STATIC_CACHE_MS = 60 * 60 * 1000; // 1 hour, generous - schedules barely change
  const MAX_TIMES_SHOWN = 40; // per route, per stop - defensive cap on high-frequency routes
  const PRASARANA_CATEGORIES = ["rapid-bus-kl", "rapid-bus-penang", "rapid-bus-mrtfeeder"];
  const CATEGORY_LABELS = {
    "rapid-bus-kl": "Rapid Bus KL",
    "rapid-bus-penang": "Rapid Bus Penang",
    "rapid-bus-mrtfeeder": "Rapid Bus MRT Feeder",
    "mybas-johor": "myBAS Johor",
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
  // Static schedule (GTFS routes/stops directory) sections
  // ---------------------------------------------------------------------

  const STATIC_FEEDS = {
    prasarana: {
      endpoint: (category) => `/api/bus-prasarana?category=${encodeURIComponent(category)}`,
      cacheKey: (category) => `bus-static-prasarana-${category}-v3`,
      stateEl: "prasaranaState",
      contentEl: "prasaranaContent",
      routeListEl: "prasaranaRouteList",
      stopListEl: "prasaranaStopList",
      routeSearchEl: "prasaranaRouteSearch",
      stopSearchEl: "prasaranaStopSearch",
      filterNoteEl: "prasaranaRouteFilterNote",
      filterNameEl: "prasaranaRouteFilterName",
      filterClearEl: "prasaranaRouteFilterClear",
      loadingLabel: "Loading Rapid Bus routes and stops...",
    },
    mybas: {
      endpoint: () => "/api/bus-mybas-johor",
      cacheKey: () => "bus-static-mybas-johor-v3",
      stateEl: "mybasState",
      contentEl: "mybasContent",
      routeListEl: "mybasRouteList",
      stopListEl: "mybasStopList",
      routeSearchEl: "mybasRouteSearch",
      stopSearchEl: "mybasStopSearch",
      filterNoteEl: "mybasRouteFilterNote",
      filterNameEl: "mybasRouteFilterName",
      filterClearEl: "mybasRouteFilterClear",
      loadingLabel: "Loading myBAS Johor routes and stops...",
    },
  };

  const feedState = {
    prasarana: { routes: [], stops: [], routeStops: {}, stopSchedule: {}, selectedRouteId: null, stopQuery: "", expandedStopId: null },
    mybas: { routes: [], stops: [], routeStops: {}, stopSchedule: {}, selectedRouteId: null, stopQuery: "", expandedStopId: null },
  };

  function readStaticCache(key) {
    try {
      const raw = sessionStorage.getItem(key);
      if (!raw) return null;
      const parsed = JSON.parse(raw);
      if (!parsed || typeof parsed.savedAt !== "number") return null;
      if (Date.now() - parsed.savedAt > STATIC_CACHE_MS) return null;
      return parsed.data;
    } catch (e) {
      return null;
    }
  }

  function writeStaticCache(key, data) {
    try {
      sessionStorage.setItem(key, JSON.stringify({ savedAt: Date.now(), data }));
    } catch (e) {
      /* sessionStorage unavailable/full - degrade silently */
    }
  }

  function renderInfo(container, message) {
    container.innerHTML = infoBanner(message);
    hydrateIcons(container);
  }

  function wireSearch(inputId, onFilter) {
    const input = document.getElementById(inputId);
    if (!input) return;
    if (input.dataset.wired) {
      input.value = "";
      return;
    }
    input.dataset.wired = "1";
    input.addEventListener("input", () => onFilter(input.value.trim().toLowerCase()));
  }

  function filterRoutes(routes, q) {
    if (!q) return routes;
    return routes.filter((r) => {
      const hay = [r.route_short_name, r.route_long_name, r.route_id, r.category].join(" ").toLowerCase();
      return hay.indexOf(q) !== -1;
    });
  }

  function filterStops(stops, q) {
    if (!q) return stops;
    return stops.filter((s) => {
      const hay = [s.stop_name, s.stop_id, s.category].join(" ").toLowerCase();
      return hay.indexOf(q) !== -1;
    });
  }

  function renderRouteList(kind, query) {
    const cfg = STATIC_FEEDS[kind];
    const s = feedState[kind];
    const routes = filterRoutes(s.routes, query || "");
    const el = document.getElementById(cfg.routeListEl);
    if (!routes.length) {
      el.innerHTML = '<li class="directory-empty">No matching routes.</li>';
      return;
    }
    el.innerHTML = routes.slice(0, 300).map((r) => {
      const name = r.route_long_name || r.route_short_name || r.route_id || "Unnamed route";
      const badge = CATEGORY_LABELS[r.category] || r.category || r.route_short_name || "";
      const sub = r.route_desc || "";
      const hasStops = r.route_id && s.routeStops[r.route_id] && s.routeStops[r.route_id].length;
      const isSelected = s.selectedRouteId === r.route_id;
      const cls = "directory-item" + (hasStops ? " is-clickable" : "") + (isSelected ? " is-selected" : "");
      const attrs = hasStops ? ` data-route-id="${escapeHtml(r.route_id)}" role="button" tabindex="0"` : "";
      return `<li class="${cls}"${attrs}>` +
        `<span class="item-title">${escapeHtml(name)}${badge ? ` <span class="badge">${escapeHtml(badge)}</span>` : ""}</span>` +
        `${sub ? `<span class="item-sub">${escapeHtml(sub)}</span>` : ""}` +
        `</li>`;
    }).join("");
  }

  function selectRoute(kind, routeId) {
    const s = feedState[kind];
    s.selectedRouteId = (s.selectedRouteId === routeId) ? null : routeId;
    s.expandedStopId = null;
    renderRouteList(kind, currentSearchValue(STATIC_FEEDS[kind].routeSearchEl));
    refreshStopList(kind);
    updateFilterNote(kind);
  }

  function currentSearchValue(inputId) {
    const input = document.getElementById(inputId);
    return input ? input.value.trim().toLowerCase() : "";
  }

  function updateFilterNote(kind) {
    const cfg = STATIC_FEEDS[kind];
    const noteEl = document.getElementById(cfg.filterNoteEl);
    if (!noteEl) return;
    const s = feedState[kind];
    if (!s.selectedRouteId) {
      noteEl.hidden = true;
      return;
    }
    const route = s.routes.find((r) => r.route_id === s.selectedRouteId);
    const name = route ? (route.route_long_name || route.route_short_name || route.route_id) : s.selectedRouteId;
    document.getElementById(cfg.filterNameEl).textContent = name;
    noteEl.hidden = false;
  }

  function refreshStopList(kind) {
    const s = feedState[kind];
    let pool = s.stops;
    if (s.selectedRouteId) {
      const allowed = s.routeStops[s.selectedRouteId];
      const allowedSet = allowed ? new Set(allowed) : new Set();
      pool = pool.filter((st) => allowedSet.has(st.stop_id));
    }
    renderStopList(kind, filterStops(pool, s.stopQuery));
  }

  function toggleStopSchedule(kind, stopId) {
    const s = feedState[kind];
    s.expandedStopId = (s.expandedStopId === stopId) ? null : stopId;
    refreshStopList(kind);
  }

  function fmtScheduleTime(t) {
    const m = /^(\d{1,2}):(\d{2})(?::\d{2})?$/.exec(t);
    if (!m) return t;
    let h = parseInt(m[1], 10);
    const nextDay = h >= 24;
    h = h % 24;
    const period = h >= 12 ? "pm" : "am";
    const h12 = h % 12 === 0 ? 12 : h % 12;
    return `${h12}:${m[2]}${period}${nextDay ? " (+1d)" : ""}`;
  }

  function buildScheduleHtml(kind, stopId) {
    const s = feedState[kind];
    const schedule = s.stopSchedule[stopId] || {};
    let routeIds = Object.keys(schedule);
    if (s.selectedRouteId) routeIds = routeIds.filter((id) => id === s.selectedRouteId);

    if (!routeIds.length) {
      return '<p class="stop-schedule-empty">No scheduled times available for this stop.</p>';
    }

    return routeIds.map((routeId) => {
      const route = s.routes.find((r) => r.route_id === routeId);
      const label = route ? (route.route_short_name || route.route_long_name || routeId) : routeId;
      const times = schedule[routeId] || [];
      const shown = times.slice(0, MAX_TIMES_SHOWN);
      const extra = times.length - shown.length;
      return `<div class="stop-schedule-route">` +
        (routeIds.length > 1 ? `<span class="badge">${escapeHtml(label)}</span>` : "") +
        `<div class="stop-schedule-times">` +
        shown.map((t) => `<span class="time-chip">${escapeHtml(fmtScheduleTime(t))}</span>`).join("") +
        (extra > 0 ? `<span class="time-chip time-chip-more">+${extra} more</span>` : "") +
        `</div></div>`;
    }).join("");
  }

  function renderStopList(kind, stops) {
    const cfg = STATIC_FEEDS[kind];
    const s = feedState[kind];
    const el = document.getElementById(cfg.stopListEl);
    if (!stops.length) {
      el.innerHTML = '<li class="directory-empty">No matching stops.</li>';
      return;
    }
    el.innerHTML = stops.slice(0, 300).map((stop) => {
      const name = stop.stop_name || stop.stop_id || "Unnamed stop";
      const badge = CATEGORY_LABELS[stop.category] || stop.category || "";
      const lat = stop.stop_lat, lon = stop.stop_lon;
      const sub = (lat && lon) ? `${lat}, ${lon}` : "";
      const hasSchedule = stop.stop_id && s.stopSchedule[stop.stop_id] &&
        Object.keys(s.stopSchedule[stop.stop_id]).length;
      const isExpanded = s.expandedStopId === stop.stop_id;
      const cls = "directory-item" + (hasSchedule ? " is-clickable" : "") + (isExpanded ? " is-selected" : "");
      const attrs = hasSchedule ? ` data-stop-id="${escapeHtml(stop.stop_id)}" role="button" tabindex="0"` : "";
      const scheduleHtml = isExpanded ? `<div class="stop-schedule">${buildScheduleHtml(kind, stop.stop_id)}</div>` : "";
      return `<li class="${cls}"${attrs}>` +
        `<span class="item-title">${escapeHtml(name)}${badge ? ` <span class="badge">${escapeHtml(badge)}</span>` : ""}</span>` +
        `${sub ? `<span class="item-sub">${escapeHtml(sub)}</span>` : ""}` +
        scheduleHtml +
        `</li>`;
    }).join("");
  }

  function applyStaticFeed(kind, payload) {
    const cfg = STATIC_FEEDS[kind];
    const routes = Array.isArray(payload.routes) ? payload.routes : [];
    const stops = Array.isArray(payload.stops) ? payload.stops : [];

    const stateEl = document.getElementById(cfg.stateEl);
    const contentEl = document.getElementById(cfg.contentEl);

    feedState[kind].routes = routes;
    feedState[kind].stops = stops;
    feedState[kind].routeStops = (payload.routeStops && typeof payload.routeStops === "object") ? payload.routeStops : {};
    feedState[kind].stopSchedule = (payload.stopSchedule && typeof payload.stopSchedule === "object") ? payload.stopSchedule : {};
    feedState[kind].selectedRouteId = null;
    feedState[kind].stopQuery = "";
    feedState[kind].expandedStopId = null;

    if (!routes.length && !stops.length) {
      contentEl.hidden = true;
      renderInfo(stateEl, "The upstream feed returned no route or stop data right now.");
      return;
    }

    stateEl.innerHTML = "";
    contentEl.hidden = false;
    renderRouteList(kind);
    refreshStopList(kind);
    updateFilterNote(kind);

    wireSearch(cfg.routeSearchEl, (q) => renderRouteList(kind, q));
    wireSearch(cfg.stopSearchEl, (q) => {
      feedState[kind].stopQuery = q;
      refreshStopList(kind);
    });

    const routeListEl = document.getElementById(cfg.routeListEl);
    if (!routeListEl.dataset.clickWired) {
      routeListEl.dataset.clickWired = "1";
      routeListEl.addEventListener("click", (e) => {
        const item = e.target.closest(".directory-item[data-route-id]");
        if (item) selectRoute(kind, item.getAttribute("data-route-id"));
      });
      routeListEl.addEventListener("keydown", (e) => {
        if (e.key !== "Enter" && e.key !== " ") return;
        const item = e.target.closest(".directory-item[data-route-id]");
        if (item) {
          e.preventDefault();
          selectRoute(kind, item.getAttribute("data-route-id"));
        }
      });
    }

    const clearBtn = document.getElementById(cfg.filterClearEl);
    if (clearBtn && !clearBtn.dataset.wired) {
      clearBtn.dataset.wired = "1";
      clearBtn.addEventListener("click", () => selectRoute(kind, null));
    }

    const stopListEl = document.getElementById(cfg.stopListEl);
    if (!stopListEl.dataset.clickWired) {
      stopListEl.dataset.clickWired = "1";
      stopListEl.addEventListener("click", (e) => {
        const item = e.target.closest(".directory-item[data-stop-id]");
        if (item) toggleStopSchedule(kind, item.getAttribute("data-stop-id"));
      });
      stopListEl.addEventListener("keydown", (e) => {
        if (e.key !== "Enter" && e.key !== " ") return;
        const item = e.target.closest(".directory-item[data-stop-id]");
        if (item) {
          e.preventDefault();
          toggleStopSchedule(kind, item.getAttribute("data-stop-id"));
        }
      });
    }
  }

  async function loadStaticFeed(kind, category, forceFresh) {
    const cfg = STATIC_FEEDS[kind];
    const cacheKey = cfg.cacheKey(category);
    const stateEl = document.getElementById(cfg.stateEl);
    const contentEl = document.getElementById(cfg.contentEl);

    contentEl.hidden = true;
    stateEl.innerHTML = loadingHtml(cfg.loadingLabel);

    if (!forceFresh) {
      const cached = readStaticCache(cacheKey);
      if (cached) {
        applyStaticFeed(kind, cached);
        return;
      }
    }

    try {
      const payload = await fetchJson(cfg.endpoint(category));
      writeStaticCache(cacheKey, payload);
      applyStaticFeed(kind, payload);
    } catch (err) {
      stateEl.innerHTML = errorBanner(err.message || "Could not load routes and stops.", `${kind}StaticRetry`);
      hydrateIcons(stateEl);
      const btn = document.getElementById(`${kind}StaticRetry`);
      if (btn) btn.addEventListener("click", () => loadStaticFeed(kind, category, true));
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

    let mybasLoaded = false;

    tabPrasarana.addEventListener("click", () => selectTab("prasarana"));
    tabMybas.addEventListener("click", () => {
      selectTab("mybas");
      if (!mybasLoaded) {
        mybasLoaded = true;
        loadStaticFeed("mybas", null);
        loadMybasRealtime();
      }
    });

    categorySelect.addEventListener("change", () => {
      const category = PRASARANA_CATEGORIES.includes(categorySelect.value) ? categorySelect.value : "rapid-bus-kl";
      loadStaticFeed("prasarana", category);
      loadPrasaranaRealtime(category);
    });

    document.getElementById("prasaranaStaticRefresh").addEventListener("click", () => {
      loadStaticFeed("prasarana", categorySelect.value || "rapid-bus-kl", true);
    });
    document.getElementById("mybasStaticRefresh").addEventListener("click", () => {
      loadStaticFeed("mybas", null, true);
    });
    document.getElementById("prasaranaRtRefresh").addEventListener("click", () => {
      loadPrasaranaRealtime(categorySelect.value || "rapid-bus-kl");
    });
    document.getElementById("mybasRtRefresh").addEventListener("click", loadMybasRealtime);

    // Initial load: Rapid Bus tab, default category.
    loadStaticFeed("prasarana", "rapid-bus-kl");
    loadPrasaranaRealtime("rapid-bus-kl");
  });
})();
