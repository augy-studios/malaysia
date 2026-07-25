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
      cacheKey: (category) => `bus-static-prasarana-${category}-v4`,
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
      lookupStateEl: "prasaranaLookupState",
      lookupContentEl: "prasaranaLookupContent",
      lookupRouteListEl: "prasaranaLookupRouteList",
      lookupTripListEl: "prasaranaLookupTripList",
      lookupItineraryEl: "prasaranaLookupItinerary",
      lookupRouteSearchEl: "prasaranaLookupRouteSearch",
    },
    mybas: {
      endpoint: () => "/api/bus-mybas-johor",
      cacheKey: () => "bus-static-mybas-johor-v4",
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
      lookupStateEl: "mybasLookupState",
      lookupContentEl: "mybasLookupContent",
      lookupRouteListEl: "mybasLookupRouteList",
      lookupTripListEl: "mybasLookupTripList",
      lookupItineraryEl: "mybasLookupItinerary",
      lookupRouteSearchEl: "mybasLookupRouteSearch",
    },
  };

  const feedState = {
    prasarana: { routes: [], stops: [], routeStops: {}, stopSchedule: {}, trips: [], tripStops: {}, selectedRouteId: null, stopQuery: "", expandedStopId: null, lookupRouteQuery: "", lookupSelectedRouteId: null, lookupSelectedTripId: null },
    mybas: { routes: [], stops: [], routeStops: {}, stopSchedule: {}, trips: [], tripStops: {}, selectedRouteId: null, stopQuery: "", expandedStopId: null, lookupRouteQuery: "", lookupSelectedRouteId: null, lookupSelectedTripId: null },
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
    feedState[kind].trips = Array.isArray(payload.trips) ? payload.trips : [];
    feedState[kind].tripStops = (payload.tripStops && typeof payload.tripStops === "object") ? payload.tripStops : {};
    feedState[kind].selectedRouteId = null;
    feedState[kind].stopQuery = "";
    feedState[kind].expandedStopId = null;
    feedState[kind].lookupRouteQuery = "";
    feedState[kind].lookupSelectedRouteId = null;
    feedState[kind].lookupSelectedTripId = null;
    const lookupRouteSearchInput = document.getElementById(cfg.lookupRouteSearchEl);
    if (lookupRouteSearchInput) lookupRouteSearchInput.value = "";

    if (!routes.length && !stops.length) {
      contentEl.hidden = true;
      renderInfo(stateEl, "The upstream feed returned no route or stop data right now.");
      renderLookup(kind);
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

    wireLookup(kind);
    renderLookup(kind);
  }

  // ---------------------------------------------------------------------
  // Trip lookup (full stop-by-stop journey for one specific bus)
  // ---------------------------------------------------------------------

  function tripsForRoute(kind, routeId) {
    const s = feedState[kind];
    return s.trips
      .filter((t) => t.route_id === routeId)
      .map((t) => {
        const stopTimes = s.tripStops[t.trip_id] || [];
        return { trip: t, firstTime: stopTimes.length ? (stopTimes[0].departure_time || stopTimes[0].arrival_time || "") : "" };
      })
      .sort((a, b) => (a.firstTime || "").localeCompare(b.firstTime || ""));
  }

  function renderLookup(kind) {
    const cfg = STATIC_FEEDS[kind];
    const s = feedState[kind];
    const stateEl = document.getElementById(cfg.lookupStateEl);
    const contentEl = document.getElementById(cfg.lookupContentEl);
    if (!stateEl || !contentEl) return;

    if (!s.trips.length) {
      contentEl.hidden = true;
      renderInfo(stateEl, "No per-bus trip data available for this feed right now.");
      return;
    }

    stateEl.innerHTML = "";
    contentEl.hidden = false;
    renderLookupRouteList(kind);
    renderLookupTripList(kind);
    renderLookupItinerary(kind);
  }

  function renderLookupRouteList(kind) {
    const cfg = STATIC_FEEDS[kind];
    const s = feedState[kind];
    const routeIdsWithTrips = {};
    s.trips.forEach((t) => { routeIdsWithTrips[t.route_id] = true; });
    let routes = s.routes.filter((r) => routeIdsWithTrips[r.route_id]);
    routes = filterRoutes(routes, s.lookupRouteQuery);

    const el = document.getElementById(cfg.lookupRouteListEl);
    if (!routes.length) {
      el.innerHTML = '<li class="directory-empty">No matching routes.</li>';
      return;
    }
    el.innerHTML = routes.slice(0, 300).map((r) => {
      const name = r.route_long_name || r.route_short_name || r.route_id || "Unnamed route";
      const badge = CATEGORY_LABELS[r.category] || r.category || r.route_short_name || "";
      const isSelected = s.lookupSelectedRouteId === r.route_id;
      const cls = "directory-item is-clickable" + (isSelected ? " is-selected" : "");
      return `<li class="${cls}" data-route-id="${escapeHtml(r.route_id)}" role="button" tabindex="0">` +
        `<span class="item-title">${escapeHtml(name)}${badge ? ` <span class="badge">${escapeHtml(badge)}</span>` : ""}</span>` +
        `</li>`;
    }).join("");
  }

  function renderLookupTripList(kind) {
    const cfg = STATIC_FEEDS[kind];
    const s = feedState[kind];
    const el = document.getElementById(cfg.lookupTripListEl);
    if (!s.lookupSelectedRouteId) {
      el.innerHTML = '<li class="directory-empty">Pick a route to see its buses.</li>';
      return;
    }
    const entries = tripsForRoute(kind, s.lookupSelectedRouteId);
    if (!entries.length) {
      el.innerHTML = '<li class="directory-empty">No buses found for this route.</li>';
      return;
    }
    el.innerHTML = entries.map(({ trip: t, firstTime }) => {
      const isSelected = s.lookupSelectedTripId === t.trip_id;
      const cls = "directory-item is-clickable" + (isSelected ? " is-selected" : "");
      const title = firstTime ? fmtScheduleTime(firstTime) : "Unknown start time";
      return `<li class="${cls}" data-trip-id="${escapeHtml(t.trip_id)}" role="button" tabindex="0">` +
        `<span class="item-title">${escapeHtml(title)}</span>` +
        `${t.trip_headsign ? `<span class="item-sub">To ${escapeHtml(t.trip_headsign)}</span>` : ""}` +
        `</li>`;
    }).join("");
  }

  function renderLookupItinerary(kind) {
    const cfg = STATIC_FEEDS[kind];
    const s = feedState[kind];
    const el = document.getElementById(cfg.lookupItineraryEl);
    if (!s.lookupSelectedTripId) {
      el.innerHTML = '<li class="directory-empty">Pick a bus to see its full trip.</li>';
      return;
    }
    const stopTimes = s.tripStops[s.lookupSelectedTripId] || [];
    if (!stopTimes.length) {
      el.innerHTML = '<li class="directory-empty">No stop times available for this bus.</li>';
      return;
    }
    el.innerHTML = stopTimes.map((st, idx) => {
      const stop = s.stops.find((x) => x.stop_id === st.stop_id);
      const name = stop ? (stop.stop_name || st.stop_id) : st.stop_id;
      const time = st.arrival_time || st.departure_time || "";
      return `<li class="directory-item lookup-stop-row">` +
        `<span class="lookup-stop-index">${idx + 1}</span>` +
        `<span class="item-title lookup-stop-name">${escapeHtml(name)}</span>` +
        `<span class="time-chip">${escapeHtml(fmtScheduleTime(time))}</span>` +
        `</li>`;
    }).join("");
  }

  function selectLookupRoute(kind, routeId) {
    const s = feedState[kind];
    s.lookupSelectedRouteId = (s.lookupSelectedRouteId === routeId) ? null : routeId;
    s.lookupSelectedTripId = null;
    renderLookupRouteList(kind);
    renderLookupTripList(kind);
    renderLookupItinerary(kind);
  }

  function selectLookupTrip(kind, tripId) {
    const s = feedState[kind];
    s.lookupSelectedTripId = (s.lookupSelectedTripId === tripId) ? null : tripId;
    renderLookupTripList(kind);
    renderLookupItinerary(kind);
  }

  function wireLookup(kind) {
    const cfg = STATIC_FEEDS[kind];

    const routeSearch = document.getElementById(cfg.lookupRouteSearchEl);
    if (routeSearch && !routeSearch.dataset.wired) {
      routeSearch.dataset.wired = "1";
      routeSearch.addEventListener("input", () => {
        feedState[kind].lookupRouteQuery = routeSearch.value.trim().toLowerCase();
        renderLookupRouteList(kind);
      });
    }

    const routeListEl = document.getElementById(cfg.lookupRouteListEl);
    if (routeListEl && !routeListEl.dataset.clickWired) {
      routeListEl.dataset.clickWired = "1";
      routeListEl.addEventListener("click", (e) => {
        const item = e.target.closest(".directory-item[data-route-id]");
        if (item) selectLookupRoute(kind, item.getAttribute("data-route-id"));
      });
      routeListEl.addEventListener("keydown", (e) => {
        if (e.key !== "Enter" && e.key !== " ") return;
        const item = e.target.closest(".directory-item[data-route-id]");
        if (item) { e.preventDefault(); selectLookupRoute(kind, item.getAttribute("data-route-id")); }
      });
    }

    const tripListEl = document.getElementById(cfg.lookupTripListEl);
    if (tripListEl && !tripListEl.dataset.clickWired) {
      tripListEl.dataset.clickWired = "1";
      tripListEl.addEventListener("click", (e) => {
        const item = e.target.closest(".directory-item[data-trip-id]");
        if (item) selectLookupTrip(kind, item.getAttribute("data-trip-id"));
      });
      tripListEl.addEventListener("keydown", (e) => {
        if (e.key !== "Enter" && e.key !== " ") return;
        const item = e.target.closest(".directory-item[data-trip-id]");
        if (item) { e.preventDefault(); selectLookupTrip(kind, item.getAttribute("data-trip-id")); }
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

  const VEHICLE_PAGE_SIZE = 5;
  const LIVE_CATEGORIES = ["rapid-bus-kl", "rapid-bus-penang", "rapid-bus-mrtfeeder", "mybas-johor"];
  const liveState = { vehicles: [], page: 1, category: "rapid-bus-kl" };

  function vehicleRowHtml(v, idx) {
    const routeId = (v.trip && v.trip.routeId) || "Unknown route";
    const vehicleId = (v.vehicle && (v.vehicle.label || v.vehicle.id)) || v.entityId || "Unknown vehicle";
    const lat = v.position && v.position.latitude;
    const lon = v.position && v.position.longitude;
    const hasCoords = typeof lat === "number" && typeof lon === "number" && !Number.isNaN(lat) && !Number.isNaN(lon);
    const tsMs = vehicleTimestampMs(v);
    const timeLabel = tsMs ? formatTimeAgo(new Date(tsMs)) : "Unknown";
    const mapsUrl = hasCoords ? `https://www.google.com/maps?q=${lat},${lon}` : null;
    const rowAttrs = hasCoords ? ` data-vehicle-idx="${idx}" role="button" tabindex="0"` : "";

    return `<div class="bus-vehicle-row${hasCoords ? " is-clickable" : ""}"${rowAttrs}>
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
  }

  function renderVehiclePage(container) {
    const totalPages = Math.max(1, Math.ceil(liveState.vehicles.length / VEHICLE_PAGE_SIZE));
    liveState.page = Math.min(Math.max(1, liveState.page), totalPages);
    const start = (liveState.page - 1) * VEHICLE_PAGE_SIZE;
    const pageVehicles = liveState.vehicles.slice(start, start + VEHICLE_PAGE_SIZE);
    const rows = pageVehicles.map((v, i) => vehicleRowHtml(v, start + i)).join("");

    const pagerHtml = liveState.vehicles.length > VEHICLE_PAGE_SIZE ? `
      <div class="bus-vehicle-pager">
        <button class="btn btn-small" type="button" data-vehicle-page="prev"${liveState.page <= 1 ? " disabled" : ""}>${iconEl("chevronLeft", 14)}<span>Prev</span></button>
        <span class="bus-vehicle-pager-label">Page ${liveState.page} of ${totalPages}</span>
        <button class="btn btn-small" type="button" data-vehicle-page="next"${liveState.page >= totalPages ? " disabled" : ""}>${iconEl("chevronRight", 14)}<span>Next</span></button>
      </div>` : "";

    container.innerHTML = `<div class="bus-vehicle-list">${rows}</div>${pagerHtml}`;
    hydrateIcons(container);

    const prevBtn = container.querySelector('[data-vehicle-page="prev"]');
    const nextBtn = container.querySelector('[data-vehicle-page="next"]');
    if (prevBtn) prevBtn.addEventListener("click", () => { liveState.page -= 1; renderVehiclePage(container); });
    if (nextBtn) nextBtn.addEventListener("click", () => { liveState.page += 1; renderVehiclePage(container); });

    container.querySelectorAll(".bus-vehicle-row[data-vehicle-idx]").forEach((row) => {
      row.addEventListener("click", () => focusVehicleOnMap(Number(row.getAttribute("data-vehicle-idx"))));
      row.addEventListener("keydown", (e) => {
        if (e.key !== "Enter" && e.key !== " ") return;
        e.preventDefault();
        focusVehicleOnMap(Number(row.getAttribute("data-vehicle-idx")));
      });
    });
  }

  function renderVehicles(container, vehicles) {
    if (!Array.isArray(vehicles) || vehicles.length === 0) {
      liveState.vehicles = [];
      container.innerHTML = infoBanner("No live positions currently reported for this feed.");
      return;
    }

    liveState.vehicles = vehicles.slice().sort((a, b) => vehicleTimestampMs(b) - vehicleTimestampMs(a));
    liveState.page = 1;
    renderVehiclePage(container);
  }

  // ---------------------------------------------------------------------
  // Leaflet map of live vehicle positions
  // ---------------------------------------------------------------------

  let liveMap = null;
  let liveMarkersLayer = null;
  let liveMarkers = [];
  const MALAYSIA_CENTER = [4.2, 108.0];

  function ensureLiveMap() {
    if (liveMap || typeof L === "undefined") return liveMap;
    liveMap = L.map("liveMap", { scrollWheelZoom: false }).setView(MALAYSIA_CENTER, 6);
    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 19,
      attribution: "&copy; <a href=\"https://www.openstreetmap.org/copyright\">OpenStreetMap</a> contributors",
    }).addTo(liveMap);
    liveMarkersLayer = L.layerGroup().addTo(liveMap);
    return liveMap;
  }

  function updateLiveMapMarkers() {
    const map = ensureLiveMap();
    if (!map) return;

    liveMarkersLayer.clearLayers();
    liveMarkers = [];

    liveState.vehicles.forEach((v, idx) => {
      const lat = v.position && v.position.latitude;
      const lon = v.position && v.position.longitude;
      if (typeof lat !== "number" || typeof lon !== "number" || Number.isNaN(lat) || Number.isNaN(lon)) {
        liveMarkers[idx] = null;
        return;
      }
      const routeId = (v.trip && v.trip.routeId) || "Unknown route";
      const vehicleId = (v.vehicle && (v.vehicle.label || v.vehicle.id)) || v.entityId || "Unknown vehicle";
      const marker = L.marker([lat, lon]).addTo(liveMarkersLayer);
      marker.bindPopup(`<strong>${escapeHtml(routeId)}</strong><br>${escapeHtml(vehicleId)}`);
      liveMarkers[idx] = marker;
    });

    const validMarkers = liveMarkers.filter(Boolean);
    if (validMarkers.length) {
      const bounds = L.latLngBounds(validMarkers.map((m) => m.getLatLng()));
      map.fitBounds(bounds, { padding: [24, 24], maxZoom: 15 });
    } else {
      map.setView(MALAYSIA_CENTER, 6);
    }
  }

  function focusVehicleOnMap(idx) {
    const map = ensureLiveMap();
    const marker = liveMarkers[idx];
    if (!map || !marker) return;
    map.setView(marker.getLatLng(), 15);
    marker.openPopup();
  }

  async function loadLiveRealtime(category) {
    const container = document.getElementById("liveRtBody");
    const updatedEl = document.getElementById("liveRtUpdated");
    const refreshBtn = document.getElementById("liveRtRefresh");
    container.innerHTML = loadingHtml("Loading live positions...");
    if (refreshBtn) refreshBtn.classList.add("is-loading");
    const endpoint = category === "mybas-johor"
      ? "/api/bus-mybas-johor-realtime"
      : `/api/bus-prasarana-realtime?category=${encodeURIComponent(category)}`;
    try {
      const payload = await fetchJson(endpoint);
      const vehicles = payload.data && payload.data.vehicles;
      renderVehicles(container, vehicles);
      updateLiveMapMarkers();
      if (updatedEl) updatedEl.textContent = `Last updated ${new Date(payload.fetchedAt || Date.now()).toLocaleTimeString()}`;
    } catch (err) {
      container.innerHTML = errorBanner(err.message || "Could not load live positions.", "liveRtRetry");
      hydrateIcons(container);
      const btn = document.getElementById("liveRtRetry");
      if (btn) btn.addEventListener("click", () => loadLiveRealtime(category));
    } finally {
      if (refreshBtn) refreshBtn.classList.remove("is-loading");
    }
  }

  // ---------------------------------------------------------------------
  // Tabs / category switcher / wiring
  // ---------------------------------------------------------------------

  function selectTab(which) {
    const tabs = { prasarana: "tabPrasarana", mybas: "tabMybas", live: "tabLive" };
    const panels = { prasarana: "panelPrasarana", mybas: "panelMybas", live: "panelLive" };

    Object.keys(tabs).forEach((key) => {
      const isActive = key === which;
      document.getElementById(tabs[key]).setAttribute("aria-selected", String(isActive));
      document.getElementById(panels[key]).hidden = !isActive;
    });

    if (which === "live") {
      // Leaflet needs the container visible with real dimensions before it can size itself.
      requestAnimationFrame(() => {
        const map = ensureLiveMap();
        if (map) map.invalidateSize();
      });
    }
  }

  document.addEventListener("DOMContentLoaded", () => {
    hydrateIcons(document);

    const tabPrasarana = document.getElementById("tabPrasarana");
    const tabMybas = document.getElementById("tabMybas");
    const tabLive = document.getElementById("tabLive");
    const categorySelect = document.getElementById("prasaranaCategory");
    const liveCategorySelect = document.getElementById("liveCategory");

    let mybasLoaded = false;
    let liveLoaded = false;

    tabPrasarana.addEventListener("click", () => selectTab("prasarana"));
    tabMybas.addEventListener("click", () => {
      selectTab("mybas");
      if (!mybasLoaded) {
        mybasLoaded = true;
        loadStaticFeed("mybas", null);
      }
    });
    tabLive.addEventListener("click", () => {
      selectTab("live");
      if (!liveLoaded) {
        liveLoaded = true;
        loadLiveRealtime(liveState.category);
      }
    });

    categorySelect.addEventListener("change", () => {
      const category = PRASARANA_CATEGORIES.includes(categorySelect.value) ? categorySelect.value : "rapid-bus-kl";
      loadStaticFeed("prasarana", category);
    });

    liveCategorySelect.addEventListener("change", () => {
      liveState.category = LIVE_CATEGORIES.includes(liveCategorySelect.value) ? liveCategorySelect.value : "rapid-bus-kl";
      loadLiveRealtime(liveState.category);
    });

    document.getElementById("prasaranaStaticRefresh").addEventListener("click", () => {
      loadStaticFeed("prasarana", categorySelect.value || "rapid-bus-kl", true);
    });
    document.getElementById("mybasStaticRefresh").addEventListener("click", () => {
      loadStaticFeed("mybas", null, true);
    });
    document.getElementById("liveRtRefresh").addEventListener("click", () => {
      loadLiveRealtime(liveState.category);
    });

    // Initial load: Rapid Bus tab, default category. Live positions and myBAS
    // load lazily on first tab visit to avoid unnecessary requests/work.
    loadStaticFeed("prasarana", "rapid-bus-kl");
  });
})();
