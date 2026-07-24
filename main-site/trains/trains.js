/*
  Trains page logic: tabs, Rapid KL + KTMB schedule directories (searchable),
  and live KTMB vehicle positions. All data comes from this project's own
  /api/trains-* proxies, never directly from data.gov.my.
*/
(function () {
  'use strict';

  var SESSION_TTL_MS = 60 * 60 * 1000; // 1 hour for static schedule data
  var LIVE_AUTO_STALE_MS = 5000; // don't reuse live data beyond a few seconds

  var state = {
    rapidkl: { loaded: false, routes: [], stops: [] },
    ktmb: { loaded: false, routes: [], stops: [] },
    live: { loaded: false, vehicles: [], fetchedAt: null }
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
    if (tab === 'live') loadLive();
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
      cacheKey: 'mb-trains-rapidkl-v1',
      stateEl: 'rapidklState',
      contentEl: 'rapidklContent',
      routeListEl: 'rapidklRouteList',
      stopListEl: 'rapidklStopList',
      routeSearchEl: 'rapidklRouteSearch',
      stopSearchEl: 'rapidklStopSearch',
      loadingLabel: 'Loading Rapid KL routes and stations...'
    },
    ktmb: {
      endpoint: '/api/trains-ktmb',
      cacheKey: 'mb-trains-ktmb-v1',
      stateEl: 'ktmbState',
      contentEl: 'ktmbContent',
      routeListEl: 'ktmbRouteList',
      stopListEl: 'ktmbStopList',
      routeSearchEl: 'ktmbRouteSearch',
      stopSearchEl: 'ktmbStopSearch',
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

    if (!routes.length && !stops.length) {
      renderInfo(cfg.stateEl, 'The upstream feed returned no route or station data right now.');
      return;
    }

    clearState(cfg.stateEl);
    document.getElementById(cfg.contentEl).hidden = false;

    renderRouteList(cfg.routeListEl, routes, kind);
    renderStopList(cfg.stopListEl, stops, kind);

    wireSearch(cfg.routeSearchEl, function (q) { renderRouteList(cfg.routeListEl, filterRoutes(routes, q), kind); });
    wireSearch(cfg.stopSearchEl, function (q) { renderStopList(cfg.stopListEl, filterStops(stops, q), kind); });
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

  function renderRouteList(listId, routes, kind) {
    var el = document.getElementById(listId);
    if (!routes.length) {
      el.innerHTML = '<li class="directory-empty">No matching routes.</li>';
      return;
    }
    el.innerHTML = routes.slice(0, 300).map(function (r) {
      var name = r.route_long_name || r.route_short_name || r.route_id || 'Unnamed route';
      var badge = r.category || r.route_short_name || '';
      var sub = r.route_desc || (kind === 'ktmb' ? (r.route_url ? 'KTMB' : '') : '');
      return '<li class="directory-item">' +
        '<span class="item-title">' + escapeHtml(name) + (badge ? ' <span class="badge">' + escapeHtml(badge) + '</span>' : '') + '</span>' +
        (sub ? '<span class="item-sub">' + escapeHtml(sub) + '</span>' : '') +
        '</li>';
    }).join('');
  }

  function renderStopList(listId, stops, kind) {
    var el = document.getElementById(listId);
    if (!stops.length) {
      el.innerHTML = '<li class="directory-empty">No matching stations.</li>';
      return;
    }
    el.innerHTML = stops.slice(0, 300).map(function (s) {
      var name = s.stop_name || s.stop_id || 'Unnamed station';
      var badge = s.category || '';
      var lat = s.stop_lat, lon = s.stop_lon;
      var sub = (lat && lon) ? (lat + ', ' + lon) : '';
      return '<li class="directory-item">' +
        '<span class="item-title">' + escapeHtml(name) + (badge ? ' <span class="badge">' + escapeHtml(badge) + '</span>' : '') + '</span>' +
        (sub ? '<span class="item-sub">' + escapeHtml(sub) + '</span>' : '') +
        '</li>';
    }).join('');
  }

  /* -- live KTMB positions -- */

  var liveLastFetch = 0;

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
    var body = document.getElementById('liveTableBody');
    body.innerHTML = sorted.map(function (v) {
      var label = v.label || v.vehicleId || v.entityId || 'Unknown vehicle';
      var routeLine = [v.routeId, v.tripId].filter(Boolean).join(' / ') || '-';
      var mapUrl = 'https://www.google.com/maps?q=' + encodeURIComponent(v.lat) + ',' + encodeURIComponent(v.lon);
      var updated = v.timestamp ? formatTime(new Date(v.timestamp * 1000).toISOString()) : '-';
      return '<tr>' +
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
    loadSchedule('rapidkl');
  });
})();
