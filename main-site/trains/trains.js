/*
  Trains page logic: tabs, Rapid KL + KTMB schedule directories (searchable),
  and live KTMB vehicle positions. All data comes from this project's own
  /api/trains-* proxies, never directly from data.gov.my.
*/
(function () {
  'use strict';

  var SESSION_TTL_MS = 60 * 60 * 1000; // 1 hour for static schedule data
  var LIVE_AUTO_STALE_MS = 5000; // don't reuse live data beyond a few seconds
  var MAX_TIMES_SHOWN = 40; // per route, per stop - defensive cap on high-frequency routes

  var state = {
    rapidkl: { loaded: false, routes: [], stops: [], routeStops: {}, stopSchedule: {}, trips: [], tripStops: {}, selectedRouteId: null, stopQuery: '', expandedStopId: null },
    ktmb: { loaded: false, routes: [], stops: [], routeStops: {}, stopSchedule: {}, trips: [], tripStops: {}, selectedRouteId: null, stopQuery: '', expandedStopId: null },
    live: { loaded: false, vehicles: [], fetchedAt: null },
    lookup: { system: 'rapidkl', routeQuery: '', selectedRouteId: null, selectedTripId: null }
  };

  /* -- icons -- */

  function paintIcons(root) {
    if (!window.Icons) return;
    (root || document).querySelectorAll('[data-icon]').forEach(function (el) {
      if (el.dataset.iconSet) return;
      el.innerHTML = window.Icons.html(el.dataset.icon, { size: el.dataset.iconSize ? Number(el.dataset.iconSize) : 18 });
      el.dataset.iconSet = '1';
    });
  }

  /* -- tabs -- */

  function initTabs() {
    var buttons = document.querySelectorAll('.tab-btn');
    buttons.forEach(function (btn) {
      btn.addEventListener('click', function () {
        var target = btn.dataset.tab;
        buttons.forEach(function (b) {
          var active = b === btn;
          b.classList.toggle('active', active);
          b.setAttribute('aria-selected', String(active));
        });
        document.querySelectorAll('.tab-panel').forEach(function (panel) {
          var show = panel.id === 'panel-' + target;
          panel.hidden = !show;
          panel.classList.toggle('active', show);
        });
        ensureLoaded(target);
      });
    });
  }

  function ensureLoaded(tab) {
    if (tab === 'rapidkl' && !state.rapidkl.loaded) loadSchedule('rapidkl');
    if (tab === 'ktmb' && !state.ktmb.loaded) loadSchedule('ktmb');
    if (tab === 'live') {
      loadLive();
      // If the map already exists and its container is already visible (e.g. a
      // repeat visit to this tab), make sure its size is current.
      if (liveMap && !document.getElementById('liveContent').hidden) {
        requestAnimationFrame(function () { liveMap.invalidateSize(); });
      }
    }
    if (tab === 'lookup') ensureLookupLoaded();
  }

  /* -- generic state banner helpers -- */

  function renderLoading(containerId, label) {
    var el = document.getElementById(containerId);
    el.innerHTML = '<div class="state-inline"><span class="spinner"></span><span>' + escapeHtml(label) + '</span></div>';
  }

  function renderError(containerId, message, retryTab) {
    var el = document.getElementById(containerId);
    el.innerHTML =
      '<div class="state-banner danger">' +
      '<span data-icon="alertOctagon"></span>' +
      '<span>' + escapeHtml(message) + '</span>' +
      '<button class="btn btn-small" type="button" data-inline-retry="' + retryTab + '">' +
      '<span data-icon="refresh"></span><span class="btn-label">Retry</span></button>' +
      '</div>';
    paintIcons(el);
    var btn = el.querySelector('[data-inline-retry]');
    if (btn) {
      btn.addEventListener('click', function () {
        if (retryTab === 'live') loadLive(true);
        else loadSchedule(retryTab, true);
      });
    }
  }

  function renderInfo(containerId, message) {
    var el = document.getElementById(containerId);
    el.innerHTML = '<div class="state-banner info"><span data-icon="alertTriangle"></span><span>' + escapeHtml(message) + '</span></div>';
    paintIcons(el);
  }

  function clearState(containerId) {
    document.getElementById(containerId).innerHTML = '';
  }

  function escapeHtml(str) {
    return String(str == null ? '' : str).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  /* -- sessionStorage cache for static schedule feeds -- */

  function readCache(key) {
    try {
      var raw = sessionStorage.getItem(key);
      if (!raw) return null;
      var parsed = JSON.parse(raw);
      if (!parsed || typeof parsed.savedAt !== 'number') return null;
      if (Date.now() - parsed.savedAt > SESSION_TTL_MS) return null;
      return parsed.data;
    } catch (e) {
      return null;
    }
  }

  function writeCache(key, data) {
    try {
      sessionStorage.setItem(key, JSON.stringify({ savedAt: Date.now(), data: data }));
    } catch (e) {
      /* storage unavailable/full - ignore, data just won't be cached */
    }
  }

  /* -- schedule (Rapid KL / KTMB) loading + rendering -- */

  var SCHEDULE_CONFIG = {
    rapidkl: {
      endpoint: '/api/trains-prasarana',
      cacheKey: 'mb-trains-rapidkl-v4',
      stateEl: 'rapidklState',
      contentEl: 'rapidklContent',
      routeListEl: 'rapidklRouteList',
      stopListEl: 'rapidklStopList',
      routeSearchEl: 'rapidklRouteSearch',
      stopSearchEl: 'rapidklStopSearch',
      filterNoteEl: 'rapidklRouteFilterNote',
      filterNameEl: 'rapidklRouteFilterName',
      filterClearEl: 'rapidklRouteFilterClear',
      loadingLabel: 'Loading Rapid KL routes and stations...'
    },
    ktmb: {
      endpoint: '/api/trains-ktmb',
      cacheKey: 'mb-trains-ktmb-v4',
      stateEl: 'ktmbState',
      contentEl: 'ktmbContent',
      routeListEl: 'ktmbRouteList',
      stopListEl: 'ktmbStopList',
      routeSearchEl: 'ktmbRouteSearch',
      stopSearchEl: 'ktmbStopSearch',
      filterNoteEl: 'ktmbRouteFilterNote',
      filterNameEl: 'ktmbRouteFilterName',
      filterClearEl: 'ktmbRouteFilterClear',
      loadingLabel: 'Loading KTMB routes and stations...'
    }
  };

  function loadSchedule(kind, forceFresh) {
    var cfg = SCHEDULE_CONFIG[kind];
    document.getElementById(cfg.contentEl).hidden = true;
    renderLoading(cfg.stateEl, cfg.loadingLabel);

    if (!forceFresh) {
      var cached = readCache(cfg.cacheKey);
      if (cached) {
        applySchedule(kind, cached);
        return;
      }
    }

    fetch(cfg.endpoint)
      .then(function (resp) {
        return resp.json().catch(function () { return null; }).then(function (body) {
          return { ok: resp.ok, status: resp.status, body: body };
        });
      })
      .then(function (result) {
        if (!result.body || result.body.success !== true) {
          var msg = (result.body && result.body.error) || ('Could not load data (HTTP ' + result.status + ').');
          renderError(cfg.stateEl, msg, kind);
          return;
        }
        writeCache(cfg.cacheKey, result.body);
        applySchedule(kind, result.body);
      })
      .catch(function () {
        renderError(cfg.stateEl, 'Network error while loading data. Check your connection and retry.', kind);
      });
  }

  function applySchedule(kind, payload) {
    var cfg = SCHEDULE_CONFIG[kind];
    var routes = Array.isArray(payload.routes) ? payload.routes : [];
    var stops = Array.isArray(payload.stops) ? payload.stops : [];

    state[kind].loaded = true;
    state[kind].routes = routes;
    state[kind].stops = stops;
    state[kind].routeStops = (payload.routeStops && typeof payload.routeStops === 'object') ? payload.routeStops : {};
    state[kind].stopSchedule = (payload.stopSchedule && typeof payload.stopSchedule === 'object') ? payload.stopSchedule : {};
    state[kind].trips = Array.isArray(payload.trips) ? payload.trips : [];
    state[kind].tripStops = (payload.tripStops && typeof payload.tripStops === 'object') ? payload.tripStops : {};
    state[kind].selectedRouteId = null;
    state[kind].stopQuery = '';
    state[kind].expandedStopId = null;

    if (!routes.length && !stops.length) {
      renderInfo(cfg.stateEl, 'The upstream feed returned no route or station data right now.');
      return;
    }

    clearState(cfg.stateEl);
    document.getElementById(cfg.contentEl).hidden = false;

    renderRouteList(kind);
    refreshStopList(kind);
    updateFilterNote(kind);

    if (state.lookup.system === kind) renderLookup();

    wireSearch(cfg.routeSearchEl, function (q) { renderRouteList(kind, q); });
    wireSearch(cfg.stopSearchEl, function (q) {
      state[kind].stopQuery = q;
      refreshStopList(kind);
    });

    var routeListEl = document.getElementById(cfg.routeListEl);
    if (!routeListEl.dataset.clickWired) {
      routeListEl.dataset.clickWired = '1';
      routeListEl.addEventListener('click', function (e) {
        var item = e.target.closest('.directory-item[data-route-id]');
        if (item) selectRoute(kind, item.getAttribute('data-route-id'));
      });
      routeListEl.addEventListener('keydown', function (e) {
        if (e.key !== 'Enter' && e.key !== ' ') return;
        var item = e.target.closest('.directory-item[data-route-id]');
        if (item) {
          e.preventDefault();
          selectRoute(kind, item.getAttribute('data-route-id'));
        }
      });
    }

    var clearBtn = document.getElementById(cfg.filterClearEl);
    if (clearBtn && !clearBtn.dataset.wired) {
      clearBtn.dataset.wired = '1';
      clearBtn.addEventListener('click', function () { selectRoute(kind, null); });
    }

    var stopListEl = document.getElementById(cfg.stopListEl);
    if (!stopListEl.dataset.clickWired) {
      stopListEl.dataset.clickWired = '1';
      stopListEl.addEventListener('click', function (e) {
        var item = e.target.closest('.directory-item[data-stop-id]');
        if (item) toggleStopSchedule(kind, item.getAttribute('data-stop-id'));
      });
      stopListEl.addEventListener('keydown', function (e) {
        if (e.key !== 'Enter' && e.key !== ' ') return;
        var item = e.target.closest('.directory-item[data-stop-id]');
        if (item) {
          e.preventDefault();
          toggleStopSchedule(kind, item.getAttribute('data-stop-id'));
        }
      });
    }
  }

  function toggleStopSchedule(kind, stopId) {
    var s = state[kind];
    s.expandedStopId = (s.expandedStopId === stopId) ? null : stopId;
    refreshStopList(kind);
  }

  function fmtScheduleTime(t) {
    var m = /^(\d{1,2}):(\d{2})(?::\d{2})?$/.exec(t);
    if (!m) return t;
    var h = parseInt(m[1], 10);
    var nextDay = h >= 24;
    h = h % 24;
    var period = h >= 12 ? 'pm' : 'am';
    var h12 = h % 12 === 0 ? 12 : h % 12;
    return h12 + ':' + m[2] + period + (nextDay ? ' (+1d)' : '');
  }

  function buildScheduleHtml(kind, stopId) {
    var s = state[kind];
    var schedule = s.stopSchedule[stopId] || {};
    var routeIds = Object.keys(schedule);
    if (s.selectedRouteId) routeIds = routeIds.filter(function (id) { return id === s.selectedRouteId; });

    if (!routeIds.length) {
      return '<p class="stop-schedule-empty">No scheduled times available for this stop.</p>';
    }

    return routeIds.map(function (routeId) {
      var route = s.routes.find(function (r) { return r.route_id === routeId; });
      var label = route ? (route.route_short_name || route.route_long_name || routeId) : routeId;
      var times = schedule[routeId] || [];
      var shown = times.slice(0, MAX_TIMES_SHOWN);
      var extra = times.length - shown.length;
      return '<div class="stop-schedule-route">' +
        (routeIds.length > 1 ? '<span class="badge">' + escapeHtml(label) + '</span>' : '') +
        '<div class="stop-schedule-times">' +
        shown.map(function (t) { return '<span class="time-chip">' + escapeHtml(fmtScheduleTime(t)) + '</span>'; }).join('') +
        (extra > 0 ? '<span class="time-chip time-chip-more">+' + extra + ' more</span>' : '') +
        '</div></div>';
    }).join('');
  }

  function selectRoute(kind, routeId) {
    var s = state[kind];
    s.selectedRouteId = (s.selectedRouteId === routeId) ? null : routeId;
    s.expandedStopId = null;
    renderRouteList(kind, currentSearchValue(SCHEDULE_CONFIG[kind].routeSearchEl));
    refreshStopList(kind);
    updateFilterNote(kind);
  }

  function currentSearchValue(inputId) {
    var input = document.getElementById(inputId);
    return input ? input.value.trim().toLowerCase() : '';
  }

  function updateFilterNote(kind) {
    var cfg = SCHEDULE_CONFIG[kind];
    var noteEl = document.getElementById(cfg.filterNoteEl);
    if (!noteEl) return;
    var s = state[kind];
    if (!s.selectedRouteId) {
      noteEl.hidden = true;
      return;
    }
    var route = s.routes.find(function (r) { return r.route_id === s.selectedRouteId; });
    var name = route ? (route.route_long_name || route.route_short_name || route.route_id) : s.selectedRouteId;
    document.getElementById(cfg.filterNameEl).textContent = name;
    noteEl.hidden = false;
  }

  function refreshStopList(kind) {
    var s = state[kind];
    var pool = s.stops;
    if (s.selectedRouteId) {
      var allowed = s.routeStops[s.selectedRouteId];
      var allowedSet = allowed ? new Set(allowed) : new Set();
      pool = pool.filter(function (st) { return allowedSet.has(st.stop_id); });
    }
    renderStopList(kind, filterStops(pool, s.stopQuery));
  }

  function wireSearch(inputId, onFilter) {
    var input = document.getElementById(inputId);
    if (!input || input.dataset.wired) {
      if (input) input.value = '';
      return;
    }
    input.dataset.wired = '1';
    input.addEventListener('input', function () {
      onFilter(input.value.trim().toLowerCase());
    });
  }

  function filterRoutes(routes, q) {
    if (!q) return routes;
    return routes.filter(function (r) {
      var hay = [r.route_short_name, r.route_long_name, r.route_id, r.category].join(' ').toLowerCase();
      return hay.indexOf(q) !== -1;
    });
  }

  function filterStops(stops, q) {
    if (!q) return stops;
    return stops.filter(function (s) {
      var hay = [s.stop_name, s.stop_id, s.category].join(' ').toLowerCase();
      return hay.indexOf(q) !== -1;
    });
  }

  function renderRouteList(kind, query) {
    var cfg = SCHEDULE_CONFIG[kind];
    var s = state[kind];
    var routes = filterRoutes(s.routes, query || '');
    var el = document.getElementById(cfg.routeListEl);
    if (!routes.length) {
      el.innerHTML = '<li class="directory-empty">No matching routes.</li>';
      return;
    }
    el.innerHTML = routes.slice(0, 300).map(function (r) {
      var name = r.route_long_name || r.route_short_name || r.route_id || 'Unnamed route';
      var badge = r.category || r.route_short_name || '';
      var sub = r.route_desc || (kind === 'ktmb' ? (r.route_url ? 'KTMB' : '') : '');
      var hasStops = r.route_id && s.routeStops[r.route_id] && s.routeStops[r.route_id].length;
      var isSelected = s.selectedRouteId === r.route_id;
      var cls = 'directory-item' + (hasStops ? ' is-clickable' : '') + (isSelected ? ' is-selected' : '');
      var attrs = hasStops ? ' data-route-id="' + escapeHtml(r.route_id) + '" role="button" tabindex="0"' : '';
      return '<li class="' + cls + '"' + attrs + '>' +
        '<span class="item-title">' + escapeHtml(name) + (badge ? ' <span class="badge">' + escapeHtml(badge) + '</span>' : '') + '</span>' +
        (sub ? '<span class="item-sub">' + escapeHtml(sub) + '</span>' : '') +
        '</li>';
    }).join('');
  }

  function renderStopList(kind, stops) {
    var cfg = SCHEDULE_CONFIG[kind];
    var s = state[kind];
    var el = document.getElementById(cfg.stopListEl);
    if (!stops.length) {
      el.innerHTML = '<li class="directory-empty">No matching stations.</li>';
      return;
    }
    el.innerHTML = stops.slice(0, 300).map(function (stop) {
      var name = stop.stop_name || stop.stop_id || 'Unnamed station';
      var badge = stop.category || '';
      var lat = stop.stop_lat, lon = stop.stop_lon;
      var sub = (lat && lon) ? (lat + ', ' + lon) : '';
      var hasSchedule = stop.stop_id && s.stopSchedule[stop.stop_id] &&
        Object.keys(s.stopSchedule[stop.stop_id]).length;
      var isExpanded = s.expandedStopId === stop.stop_id;
      var cls = 'directory-item' + (hasSchedule ? ' is-clickable' : '') + (isExpanded ? ' is-selected' : '');
      var attrs = hasSchedule ? ' data-stop-id="' + escapeHtml(stop.stop_id) + '" role="button" tabindex="0"' : '';
      var scheduleHtml = isExpanded ? '<div class="stop-schedule">' + buildScheduleHtml(kind, stop.stop_id) + '</div>' : '';
      return '<li class="' + cls + '"' + attrs + '>' +
        '<span class="item-title">' + escapeHtml(name) + (badge ? ' <span class="badge">' + escapeHtml(badge) + '</span>' : '') + '</span>' +
        (sub ? '<span class="item-sub">' + escapeHtml(sub) + '</span>' : '') +
        scheduleHtml +
        '</li>';
    }).join('');
  }

  /* -- train lookup (full journey for one specific train) -- */

  function ensureLookupLoaded() {
    var kind = state.lookup.system;
    if (!state[kind].loaded) {
      loadSchedule(kind); // applySchedule() calls renderLookup() once this lands
      return;
    }
    renderLookup();
  }

  function tripsForRoute(kind, routeId) {
    var s = state[kind];
    return s.trips
      .filter(function (t) { return t.route_id === routeId; })
      .map(function (t) {
        var stopTimes = s.tripStops[t.trip_id] || [];
        var first = stopTimes[0];
        var last = stopTimes[stopTimes.length - 1];
        return {
          trip: t,
          firstTime: first ? (first.departure_time || first.arrival_time || '') : '',
          firstStopId: first ? first.stop_id : null,
          lastStopId: last ? last.stop_id : null
        };
      })
      .sort(function (a, b) { return (a.firstTime || '').localeCompare(b.firstTime || ''); });
  }

  function lookupStopName(kind, stopId) {
    var stop = state[kind].stops.find(function (x) { return x.stop_id === stopId; });
    return stop ? (stop.stop_name || stopId) : stopId;
  }

  function renderLookup() {
    var kind = state.lookup.system;
    var s = state[kind];
    var stateEl = document.getElementById('lookupState');
    var contentEl = document.getElementById('lookupContent');

    if (!s.loaded) {
      renderLoading('lookupState', 'Loading ' + (kind === 'ktmb' ? 'KTMB' : 'Rapid KL') + ' data...');
      contentEl.hidden = true;
      return;
    }
    if (!s.trips.length) {
      renderInfo('lookupState', 'No per-train journey data available for this feed right now.');
      contentEl.hidden = true;
      return;
    }

    stateEl.innerHTML = '';
    contentEl.hidden = false;

    renderLookupRouteList();
    renderLookupTripList();
    renderLookupItinerary();
  }

  function renderLookupRouteList() {
    var kind = state.lookup.system;
    var s = state[kind];
    var routeIdsWithTrips = {};
    s.trips.forEach(function (t) { routeIdsWithTrips[t.route_id] = true; });
    var routes = s.routes.filter(function (r) { return routeIdsWithTrips[r.route_id]; });
    routes = filterRoutes(routes, state.lookup.routeQuery);

    var el = document.getElementById('lookupRouteList');
    if (!routes.length) {
      el.innerHTML = '<li class="directory-empty">No matching lines.</li>';
      return;
    }
    el.innerHTML = routes.slice(0, 300).map(function (r) {
      var name = r.route_long_name || r.route_short_name || r.route_id || 'Unnamed route';
      var badge = r.category || r.route_short_name || '';
      var isSelected = state.lookup.selectedRouteId === r.route_id;
      var cls = 'directory-item is-clickable' + (isSelected ? ' is-selected' : '');
      return '<li class="' + cls + '" data-route-id="' + escapeHtml(r.route_id) + '" role="button" tabindex="0">' +
        '<span class="item-title">' + escapeHtml(name) + (badge ? ' <span class="badge">' + escapeHtml(badge) + '</span>' : '') + '</span>' +
        '</li>';
    }).join('');
  }

  function renderLookupTripList() {
    var kind = state.lookup.system;
    var el = document.getElementById('lookupTripList');
    if (!state.lookup.selectedRouteId) {
      el.innerHTML = '<li class="directory-empty">Pick a line to see its trains.</li>';
      return;
    }
    var entries = tripsForRoute(kind, state.lookup.selectedRouteId);
    if (!entries.length) {
      el.innerHTML = '<li class="directory-empty">No trains found for this line.</li>';
      return;
    }
    el.innerHTML = entries.map(function (entry) {
      var t = entry.trip;
      var isSelected = state.lookup.selectedTripId === t.trip_id;
      var cls = 'directory-item is-clickable' + (isSelected ? ' is-selected' : '');
      var title = entry.firstTime ? fmtScheduleTime(entry.firstTime) : 'Unknown start time';
      var startName = entry.firstStopId ? lookupStopName(kind, entry.firstStopId) : '';
      var endName = entry.lastStopId ? lookupStopName(kind, entry.lastStopId) : '';
      var sub = (startName && endName) ? (startName + ' → ' + endName) : (t.trip_headsign ? 'To ' + t.trip_headsign : '');
      return '<li class="' + cls + '" data-trip-id="' + escapeHtml(t.trip_id) + '" role="button" tabindex="0">' +
        '<span class="item-title">' + escapeHtml(title) + '</span>' +
        (sub ? '<span class="item-sub">' + escapeHtml(sub) + '</span>' : '') +
        '</li>';
    }).join('');
  }

  function renderLookupItinerary() {
    var kind = state.lookup.system;
    var s = state[kind];
    var el = document.getElementById('lookupItinerary');
    if (!state.lookup.selectedTripId) {
      el.innerHTML = '<li class="directory-empty">Pick a train to see its full journey.</li>';
      return;
    }
    var stopTimes = s.tripStops[state.lookup.selectedTripId] || [];
    if (!stopTimes.length) {
      el.innerHTML = '<li class="directory-empty">No stop times available for this train.</li>';
      return;
    }
    el.innerHTML = stopTimes.map(function (st, idx) {
      var stop = s.stops.find(function (x) { return x.stop_id === st.stop_id; });
      var name = stop ? (stop.stop_name || st.stop_id) : st.stop_id;
      var time = st.arrival_time || st.departure_time || '';
      return '<li class="directory-item lookup-stop-row">' +
        '<span class="lookup-stop-index">' + (idx + 1) + '</span>' +
        '<span class="item-title lookup-stop-name">' + escapeHtml(name) + '</span>' +
        '<span class="time-chip">' + escapeHtml(fmtScheduleTime(time)) + '</span>' +
        '</li>';
    }).join('');
  }

  function selectLookupRoute(routeId) {
    state.lookup.selectedRouteId = (state.lookup.selectedRouteId === routeId) ? null : routeId;
    state.lookup.selectedTripId = null;
    renderLookupRouteList();
    renderLookupTripList();
    renderLookupItinerary();
  }

  function selectLookupTrip(tripId) {
    state.lookup.selectedTripId = (state.lookup.selectedTripId === tripId) ? null : tripId;
    renderLookupTripList();
    renderLookupItinerary();
  }

  function initLookup() {
    document.querySelectorAll('[data-lookup-system]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var system = btn.dataset.lookupSystem;
        if (system === state.lookup.system) return;
        state.lookup.system = system;
        state.lookup.selectedRouteId = null;
        state.lookup.selectedTripId = null;
        state.lookup.routeQuery = '';
        var routeSearch = document.getElementById('lookupRouteSearch');
        if (routeSearch) routeSearch.value = '';
        document.querySelectorAll('[data-lookup-system]').forEach(function (b) {
          var active = b === btn;
          b.classList.toggle('active', active);
          b.setAttribute('aria-selected', String(active));
        });
        ensureLookupLoaded();
      });
    });

    var routeSearch = document.getElementById('lookupRouteSearch');
    if (routeSearch) {
      routeSearch.addEventListener('input', function () {
        state.lookup.routeQuery = routeSearch.value.trim().toLowerCase();
        renderLookupRouteList();
      });
    }

    var routeListEl = document.getElementById('lookupRouteList');
    routeListEl.addEventListener('click', function (e) {
      var item = e.target.closest('.directory-item[data-route-id]');
      if (item) selectLookupRoute(item.getAttribute('data-route-id'));
    });
    routeListEl.addEventListener('keydown', function (e) {
      if (e.key !== 'Enter' && e.key !== ' ') return;
      var item = e.target.closest('.directory-item[data-route-id]');
      if (item) { e.preventDefault(); selectLookupRoute(item.getAttribute('data-route-id')); }
    });

    var tripListEl = document.getElementById('lookupTripList');
    tripListEl.addEventListener('click', function (e) {
      var item = e.target.closest('.directory-item[data-trip-id]');
      if (item) selectLookupTrip(item.getAttribute('data-trip-id'));
    });
    tripListEl.addEventListener('keydown', function (e) {
      if (e.key !== 'Enter' && e.key !== ' ') return;
      var item = e.target.closest('.directory-item[data-trip-id]');
      if (item) { e.preventDefault(); selectLookupTrip(item.getAttribute('data-trip-id')); }
    });
  }

  /* -- live KTMB positions -- */

  var liveLastFetch = 0;
  var liveMap = null;
  var liveMarkersLayer = null;
  var liveMarkers = [];
  var MALAYSIA_CENTER = [4.2, 108.0];

  function ensureLiveMap() {
    if (liveMap || typeof L === 'undefined') return liveMap;
    liveMap = L.map('liveMap', { scrollWheelZoom: false }).setView(MALAYSIA_CENTER, 6);
    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
      maxZoom: 19,
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
    }).addTo(liveMap);
    liveMarkersLayer = L.layerGroup().addTo(liveMap);
    return liveMap;
  }

  function updateLiveMapMarkers(sortedVehicles) {
    var map = ensureLiveMap();
    if (!map) return;

    liveMarkersLayer.clearLayers();
    liveMarkers = [];

    sortedVehicles.forEach(function (v, idx) {
      if (typeof v.lat !== 'number' || typeof v.lon !== 'number' || isNaN(v.lat) || isNaN(v.lon)) {
        liveMarkers[idx] = null;
        return;
      }
      var label = v.label || v.vehicleId || v.entityId || 'Unknown vehicle';
      var routeLine = [v.routeId, v.tripId].filter(Boolean).join(' / ') || 'Unknown route';
      var marker = L.marker([v.lat, v.lon]).addTo(liveMarkersLayer);
      marker.bindPopup('<strong>' + escapeHtml(label) + '</strong><br>' + escapeHtml(routeLine));
      liveMarkers[idx] = marker;
    });

    var validMarkers = liveMarkers.filter(Boolean);
    if (validMarkers.length) {
      var bounds = L.latLngBounds(validMarkers.map(function (m) { return m.getLatLng(); }));
      map.fitBounds(bounds, { padding: [24, 24], maxZoom: 15 });
    } else {
      map.setView(MALAYSIA_CENTER, 6);
    }
  }

  function focusVehicleOnMap(idx) {
    var map = ensureLiveMap();
    var marker = liveMarkers[idx];
    if (!map || !marker) return;
    map.setView(marker.getLatLng(), 15);
    marker.openPopup();
  }

  function loadLive(force) {
    var now = Date.now();
    if (!force && state.live.loaded && (now - liveLastFetch) < LIVE_AUTO_STALE_MS) return;

    document.getElementById('liveContent').hidden = true;
    renderLoading('liveState', 'Loading live KTMB positions...');

    fetch('/api/trains-ktmb-realtime')
      .then(function (resp) {
        return resp.json().catch(function () { return null; }).then(function (body) {
          return { ok: resp.ok, status: resp.status, body: body };
        });
      })
      .then(function (result) {
        liveLastFetch = Date.now();
        if (!result.body || result.body.success !== true) {
          var msg = (result.body && result.body.error) || ('Could not load live positions (HTTP ' + result.status + ').');
          renderError('liveState', msg, 'live');
          return;
        }
        var vehicles = Array.isArray(result.body.vehicles) ? result.body.vehicles : [];
        state.live.loaded = true;
        state.live.vehicles = vehicles;
        state.live.fetchedAt = result.body.fetchedAt || new Date().toISOString();

        document.getElementById('liveUpdatedAt').textContent = formatTime(state.live.fetchedAt);

        if (!vehicles.length) {
          renderInfo('liveState', 'No live positions currently reported by KTMB.');
          return;
        }

        clearState('liveState');
        document.getElementById('liveContent').hidden = false;
        // The container is only guaranteed a real size once it's unhidden, so
        // (re)create/resize the map here rather than earlier on tab switch.
        var map = ensureLiveMap();
        if (map) map.invalidateSize();
        renderLiveTable(vehicles);
      })
      .catch(function () {
        renderError('liveState', 'Network error while loading live positions. Check your connection and retry.', 'live');
      });
  }

  function formatTime(iso) {
    try {
      var d = new Date(iso);
      if (isNaN(d.getTime())) return iso;
      return d.toLocaleString();
    } catch (e) {
      return iso;
    }
  }

  function renderLiveTable(vehicles) {
    var sorted = vehicles.slice().sort(function (a, b) {
      return (b.timestamp || 0) - (a.timestamp || 0);
    });

    updateLiveMapMarkers(sorted);

    var body = document.getElementById('liveTableBody');
    body.innerHTML = sorted.map(function (v, idx) {
      var label = v.label || v.vehicleId || v.entityId || 'Unknown vehicle';
      var routeLine = [v.routeId, v.tripId].filter(Boolean).join(' / ') || '-';
      var hasCoords = typeof v.lat === 'number' && typeof v.lon === 'number' && !isNaN(v.lat) && !isNaN(v.lon);
      var mapUrl = 'https://www.google.com/maps?q=' + encodeURIComponent(v.lat) + ',' + encodeURIComponent(v.lon);
      var updated = v.timestamp ? formatTime(new Date(v.timestamp * 1000).toISOString()) : '-';
      var rowAttrs = hasCoords ? ' class="is-clickable" data-vehicle-idx="' + idx + '" tabindex="0"' : '';
      return '<tr' + rowAttrs + '>' +
        '<td><span data-icon="mapPin"></span></td>' +
        '<td>' + escapeHtml(label) + '</td>' +
        '<td>' + escapeHtml(routeLine) + '</td>' +
        '<td><a href="' + mapUrl + '" target="_blank" rel="noopener noreferrer">' +
        v.lat.toFixed(5) + ', ' + v.lon.toFixed(5) +
        '<span data-icon="externalLink" data-icon-size="14"></span></a></td>' +
        '<td>' + escapeHtml(updated) + '</td>' +
        '</tr>';
    }).join('');
    paintIcons(body);

    body.querySelectorAll('tr[data-vehicle-idx]').forEach(function (row) {
      row.addEventListener('click', function (e) {
        if (e.target.closest('a')) return; // let the "open in Google Maps" link work normally
        focusVehicleOnMap(Number(row.getAttribute('data-vehicle-idx')));
      });
      row.addEventListener('keydown', function (e) {
        if (e.key !== 'Enter' && e.key !== ' ') return;
        e.preventDefault();
        focusVehicleOnMap(Number(row.getAttribute('data-vehicle-idx')));
      });
    });
  }

  /* -- retry buttons in section headers -- */

  function initRetryButtons() {
    document.querySelectorAll('[data-retry]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var tab = btn.dataset.retry;
        if (tab === 'live') loadLive(true);
        else loadSchedule(tab, true);
      });
    });
  }

  document.addEventListener('DOMContentLoaded', function () {
    paintIcons(document);
    initTabs();
    initRetryButtons();
    initLookup();
    loadSchedule('rapidkl');
  });
})();
