/*
  Weather page: 7-day MET Malaysia forecast (by district) + active warnings.
  Fetches through our own /api/weather-forecast and /api/weather-warning
  proxies (never data.gov.my directly, avoids CORS + centralizes caching).
*/
(function () {
  const CACHE_TTL_MS = 8 * 60 * 1000; // 8 minutes
  const CACHE_KEYS = {
    forecast: 'mb-weather-forecast-cache',
    warning: 'mb-weather-warning-cache'
  };
  const PAGE_SIZE = 3;

  const els = {};

  document.addEventListener('DOMContentLoaded', function () {
    els.umbrellaCard = document.getElementById('umbrellaCard');
    els.umbrellaIcon = document.getElementById('umbrellaIcon');
    els.umbrellaHeadline = document.getElementById('umbrellaHeadline');
    els.umbrellaSub = document.getElementById('umbrellaSub');
    els.locationSelect = document.getElementById('locationSelect');
    els.districtSearch = document.getElementById('districtSearchInput');
    els.lastUpdated = document.getElementById('lastUpdated');
    els.refreshBtn = document.getElementById('refreshBtn');
    els.refreshIcon = document.getElementById('refreshIcon');
    els.forecastState = document.getElementById('forecastState');
    els.forecastGrid = document.getElementById('forecastGrid');
    els.forecastPagination = document.getElementById('forecastPagination');
    els.forecastPagePrev = document.getElementById('forecastPagePrev');
    els.forecastPageNext = document.getElementById('forecastPageNext');
    els.forecastPagePrevIcon = document.getElementById('forecastPagePrevIcon');
    els.forecastPageNextIcon = document.getElementById('forecastPageNextIcon');
    els.forecastPageIndicator = document.getElementById('forecastPageIndicator');
    els.warningState = document.getElementById('warningState');
    els.warningList = document.getElementById('warningList');

    if (els.refreshIcon && window.Icons) {
      els.refreshIcon.innerHTML = window.Icons.html('refresh', { size: 16 });
    }
    if (window.Icons) {
      els.forecastPagePrevIcon.innerHTML = window.Icons.html('chevronLeft', { size: 16 });
      els.forecastPageNextIcon.innerHTML = window.Icons.html('chevronRight', { size: 16 });
    }

    els.refreshBtn.addEventListener('click', function () {
      loadAll(true);
    });

    els.locationSelect.addEventListener('change', function () {
      if (els.districtSearch) els.districtSearch.value = '';
      state.query = '';
      state.page = 1;
      renderForecast(state.forecastByLocation, els.locationSelect.value, state.query);
      renderUmbrella(state.forecastByLocation, els.locationSelect.value);
    });

    els.districtSearch.addEventListener('input', function () {
      state.query = els.districtSearch.value.trim().toLowerCase();
      state.page = 1;
      if (state.query && els.locationSelect.value) els.locationSelect.value = '';
      renderForecast(state.forecastByLocation, els.locationSelect.value, state.query);
    });

    els.forecastPagePrev.addEventListener('click', function () {
      if (state.page > 1) {
        state.page -= 1;
        renderForecast(state.forecastByLocation, els.locationSelect.value, state.query);
        els.forecastGrid.scrollIntoView({ behavior: 'smooth', block: 'start' });
      }
    });

    els.forecastPageNext.addEventListener('click', function () {
      state.page += 1;
      renderForecast(state.forecastByLocation, els.locationSelect.value, state.query);
      els.forecastGrid.scrollIntoView({ behavior: 'smooth', block: 'start' });
    });

    loadAll(false);
  });

  const state = {
    forecastByLocation: null,
    warnings: null,
    query: '',
    page: 1
  };

  function icon(name, opts) {
    return window.Icons ? window.Icons.html(name, opts || { size: 20 }) : '';
  }

  /* -- session cache helpers -- */

  function getCached(key) {
    try {
      const raw = sessionStorage.getItem(key);
      if (!raw) return null;
      const parsed = JSON.parse(raw);
      if (!parsed || typeof parsed.ts !== 'number') return null;
      if (Date.now() - parsed.ts > CACHE_TTL_MS) return null;
      return parsed.payload;
    } catch (e) {
      return null;
    }
  }

  function setCached(key, payload) {
    try {
      sessionStorage.setItem(key, JSON.stringify({ ts: Date.now(), payload: payload }));
    } catch (e) { /* storage unavailable/full - ignore */ }
  }

  /* -- fetch helpers -- */

  function fetchJson(url) {
    return fetch(url, { headers: { accept: 'application/json' } }).then(function (res) {
      return res.json().catch(function () { return null; }).then(function (body) {
        if (!res.ok || !body || body.success === false) {
          const msg = (body && body.error) ? body.error : ('Request failed with status ' + res.status);
          throw new Error(msg);
        }
        return body;
      });
    });
  }

  function loadAll(forceRefresh) {
    setForecastState('loading');
    setWarningState('loading');

    if (forceRefresh) {
      try {
        sessionStorage.removeItem(CACHE_KEYS.forecast);
        sessionStorage.removeItem(CACHE_KEYS.warning);
      } catch (e) { /* ignore */ }
      els.refreshBtn.classList.add('refresh-spinning');
    }

    const cachedForecast = forceRefresh ? null : getCached(CACHE_KEYS.forecast);
    const cachedWarning = forceRefresh ? null : getCached(CACHE_KEYS.warning);

    const forecastPromise = cachedForecast
      ? Promise.resolve(cachedForecast)
      : fetchJson('/api/weather-forecast').then(function (body) {
        setCached(CACHE_KEYS.forecast, body);
        return body;
      });

    const warningPromise = cachedWarning
      ? Promise.resolve(cachedWarning)
      : fetchJson('/api/weather-warning').then(function (body) {
        setCached(CACHE_KEYS.warning, body);
        return body;
      });

    forecastPromise.then(function (body) {
      handleForecast(body);
    }).catch(function (err) {
      handleForecastError(err);
    });

    warningPromise.then(function (body) {
      handleWarnings(body);
    }).catch(function (err) {
      handleWarningError(err);
    }).finally(function () {
      els.refreshBtn.classList.remove('refresh-spinning');
    });
  }

  /* -- condition -> icon mapping -- */

  function iconForCondition(text) {
    const t = (text || '').toLowerCase();
    if (t.indexOf('petir') !== -1 || t.indexOf('ribut') !== -1) return 'cloudLightning';
    if (t.indexOf('hujan') !== -1) return 'cloudRain';
    if (t.indexOf('cerah') !== -1 && (t.indexOf('berawan') !== -1 || t.indexOf('sebahagian') !== -1)) return 'cloudSun';
    if (t.indexOf('cerah') !== -1 || t.indexOf('panas') !== -1) return 'sun';
    if (t.indexOf('angin') !== -1) return 'wind';
    if (t.indexOf('berawan') !== -1 || t.indexOf('mendung') !== -1 || t.indexOf('jerebu') !== -1) return 'cloud';
    return 'cloudSun';
  }

  function fmtDate(dateStr) {
    if (!dateStr) return 'N/A';
    const d = new Date(dateStr);
    if (isNaN(d.getTime())) return dateStr;
    return d.toLocaleDateString('en-MY', { weekday: 'short', day: 'numeric', month: 'short' });
  }

  function fmtDateTime(dateStr) {
    if (!dateStr) return 'N/A';
    const d = new Date(dateStr);
    if (isNaN(d.getTime())) return dateStr;
    return d.toLocaleString('en-MY', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
  }

  /* -- forecast -- */

  function setForecastState(kind, message) {
    if (kind === 'loading') {
      els.forecastState.innerHTML =
        '<div class="state-banner info"><span class="spinner"></span><span>' +
        (message || 'Loading forecast…') + '</span></div>';
    } else if (kind === 'error') {
      els.forecastState.innerHTML =
        '<div class="state-banner danger">' + icon('alertTriangle', { size: 18 }) +
        '<span>' + escapeHtml(message || 'Could not load the forecast.') + '</span>' +
        '<button class="btn" id="forecastRetry" type="button" style="margin-left:auto;">Retry</button></div>';
      const retry = document.getElementById('forecastRetry');
      if (retry) retry.addEventListener('click', function () { loadAll(true); });
    } else if (kind === 'empty') {
      els.forecastState.innerHTML =
        '<div class="state-banner info">' + icon('cloud', { size: 18 }) +
        '<span>' + escapeHtml(message || 'No forecast data is available right now.') + '</span></div>';
    } else {
      els.forecastState.innerHTML = '';
    }
  }

  function escapeHtml(str) {
    return String(str == null ? '' : str).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function handleForecastError(err) {
    setForecastState('error', err && err.message ? err.message : 'Could not load the forecast.');
    els.forecastGrid.innerHTML = '';
    els.umbrellaHeadline.textContent = "Couldn't load today's outlook";
    els.umbrellaSub.textContent = 'Try refreshing in a moment.';
    if (els.umbrellaIcon) els.umbrellaIcon.innerHTML = icon('alertTriangle', { size: 32 });
  }

  function handleForecast(body) {
    const data = body && Array.isArray(body.data) ? body.data : null;
    if (!data) {
      handleForecastError(new Error('Unexpected forecast response shape.'));
      return;
    }
    if (data.length === 0) {
      setForecastState('empty');
      els.forecastGrid.innerHTML = '';
      state.forecastByLocation = {};
      return;
    }

    setForecastState('none');

    const byLocation = {};
    data.forEach(function (entry) {
      if (!entry || typeof entry !== 'object') return;
      const locName = entry.location && entry.location.location_name
        ? entry.location.location_name
        : (entry.location_name || 'Unknown');
      const locId = entry.location && entry.location.location_id ? entry.location.location_id : locName;
      if (!byLocation[locId]) byLocation[locId] = { name: locName, days: [] };
      byLocation[locId].days.push(entry);
    });

    Object.keys(byLocation).forEach(function (id) {
      byLocation[id].days.sort(function (a, b) {
        return (a.date || '').localeCompare(b.date || '');
      });
    });

    state.forecastByLocation = byLocation;

    populateLocationSelect(byLocation);
    if (els.lastUpdated) {
      els.lastUpdated.textContent = 'Updated ' + fmtDateTime(body.fetchedAt || new Date().toISOString());
    }

    state.page = 1;
    renderUmbrella(byLocation, els.locationSelect.value);
    renderForecast(byLocation, els.locationSelect.value, state.query);
  }

  function populateLocationSelect(byLocation) {
    const current = els.locationSelect.value;
    const ids = Object.keys(byLocation).sort(function (a, b) {
      return byLocation[a].name.localeCompare(byLocation[b].name);
    });
    const options = ['<option value="">All districts</option>'].concat(
      ids.map(function (id) {
        return '<option value="' + escapeHtml(id) + '">' + escapeHtml(byLocation[id].name) + '</option>';
      })
    );
    els.locationSelect.innerHTML = options.join('');
    if (current && byLocation[current]) els.locationSelect.value = current;
  }

  function renderUmbrella(byLocation, selectedId) {
    if (!byLocation) return;
    const ids = selectedId && byLocation[selectedId] ? [selectedId] : Object.keys(byLocation);
    if (ids.length === 0) {
      els.umbrellaHeadline.textContent = 'No forecast available';
      els.umbrellaSub.textContent = 'Check back later.';
      if (els.umbrellaIcon) els.umbrellaIcon.innerHTML = icon('cloud', { size: 32 });
      return;
    }

    const today = new Date().toISOString().slice(0, 10);
    let rainy = 0;
    let sample = null;
    ids.forEach(function (id) {
      const loc = byLocation[id];
      const todayEntry = loc.days.find(function (d) { return d.date === today; }) || loc.days[0];
      if (!todayEntry) return;
      if (!sample) sample = { loc: loc, entry: todayEntry };
      const summary = (todayEntry.summary_forecast || todayEntry.afternoon_forecast || '').toLowerCase();
      if (summary.indexOf('hujan') !== -1 || summary.indexOf('petir') !== -1 || summary.indexOf('ribut') !== -1) {
        rainy += 1;
      }
    });

    if (!sample) {
      els.umbrellaHeadline.textContent = 'No forecast available for today';
      els.umbrellaSub.textContent = 'Try a different district.';
      if (els.umbrellaIcon) els.umbrellaIcon.innerHTML = icon('cloud', { size: 32 });
      return;
    }

    if (selectedId) {
      const entry = sample.entry;
      const cond = entry.summary_forecast || entry.afternoon_forecast || 'No data';
      if (els.umbrellaIcon) els.umbrellaIcon.innerHTML = icon(iconForCondition(cond), { size: 32 });
      const bringUmbrella = cond.toLowerCase().indexOf('hujan') !== -1 || cond.toLowerCase().indexOf('petir') !== -1;
      els.umbrellaHeadline.textContent = bringUmbrella ? 'Bawa payung! Rain is likely today.' : 'Should be fine, no rain expected.';
      els.umbrellaSub.textContent = sample.loc.name + ': ' + cond +
        (entry.min_temp != null && entry.max_temp != null ? ' · ' + entry.min_temp + '–' + entry.max_temp + '°C' : '');
    } else {
      const pct = Math.round((rainy / ids.length) * 100);
      if (els.umbrellaIcon) {
        els.umbrellaIcon.innerHTML = icon(rainy > ids.length / 2 ? 'cloudRain' : 'cloudSun', { size: 32 });
      }
      els.umbrellaHeadline.textContent = rainy > 0
        ? 'Bawa payung: rain expected in ' + rainy + ' of ' + ids.length + ' districts today (' + pct + '%).'
        : 'Looking dry, with no rain reported across districts today.';
      els.umbrellaSub.textContent = 'Pick a district below for a detailed outlook.';
    }
  }

  function renderForecast(byLocation, selectedId, query) {
    if (!byLocation) return;
    const q = (query || '').toLowerCase();

    let ids;
    if (selectedId && byLocation[selectedId]) {
      ids = [selectedId];
    } else {
      ids = Object.keys(byLocation).sort(function (a, b) {
        return byLocation[a].name.localeCompare(byLocation[b].name);
      });
      if (q) {
        ids = ids.filter(function (id) {
          return byLocation[id].name.toLowerCase().indexOf(q) !== -1;
        });
      }
    }

    const isPaged = !selectedId;
    const totalPages = Math.max(1, Math.ceil(ids.length / PAGE_SIZE));
    if (state.page > totalPages) state.page = totalPages;
    if (state.page < 1) state.page = 1;

    const pageIds = isPaged ? ids.slice((state.page - 1) * PAGE_SIZE, state.page * PAGE_SIZE) : ids;

    if (els.forecastPagination) {
      els.forecastPagination.hidden = !isPaged || ids.length <= PAGE_SIZE;
      if (isPaged) {
        els.forecastPagePrev.disabled = state.page <= 1;
        els.forecastPageNext.disabled = state.page >= totalPages;
        els.forecastPageIndicator.textContent = 'Page ' + state.page + ' of ' + totalPages;
      }
    }

    if (ids.length === 0) {
      els.forecastGrid.innerHTML = q
        ? '<div class="state-banner info">' + icon('cloud', { size: 18 }) +
          '<span>No districts match "' + escapeHtml(query) + '".</span></div>'
        : '';
      return;
    }

    els.forecastGrid.innerHTML = pageIds.map(function (id) {
      const loc = byLocation[id];
      const days = loc.days.slice(0, 7);
      const dayRows = days.map(function (d) {
        const cond = d.summary_forecast || d.afternoon_forecast || d.morning_forecast || 'No data';
        return '<div class="forecast-day">' +
          '<span class="forecast-day-icon">' + icon(iconForCondition(cond), { size: 20 }) + '</span>' +
          '<div class="forecast-day-info">' +
          '<div class="forecast-day-date">' + escapeHtml(fmtDate(d.date)) + '</div>' +
          '<div class="forecast-day-text">' + escapeHtml(cond) + '</div>' +
          '</div>' +
          '<div class="forecast-day-temp">' +
          (d.min_temp != null && d.max_temp != null ? escapeHtml(d.min_temp + '–' + d.max_temp + '°C') : 'N/A') +
          '</div>' +
          '</div>';
      }).join('') || '<p class="forecast-day-text">No daily breakdown available.</p>';

      const first = days[0];
      const tempRange = first && first.min_temp != null && first.max_temp != null
        ? '<strong>' + escapeHtml(first.min_temp + '–' + first.max_temp) + '°C</strong>'
        : 'N/A';

      return '<article class="glass forecast-card">' +
        '<div class="forecast-card-head"><h3>' + escapeHtml(loc.name) + '</h3>' +
        '<span class="forecast-temp">' + tempRange + '</span></div>' +
        '<div class="forecast-days">' + dayRows + '</div>' +
        '</article>';
    }).join('');
  }

  /* -- warnings -- */

  function setWarningState(kind, message) {
    if (kind === 'loading') {
      els.warningState.innerHTML =
        '<div class="state-banner info"><span class="spinner"></span><span>' +
        (message || 'Loading warnings…') + '</span></div>';
    } else if (kind === 'error') {
      els.warningState.innerHTML =
        '<div class="state-banner danger">' + icon('alertTriangle', { size: 18 }) +
        '<span>' + escapeHtml(message || 'Could not load weather warnings.') + '</span>' +
        '<button class="btn" id="warningRetry" type="button" style="margin-left:auto;">Retry</button></div>';
      const retry = document.getElementById('warningRetry');
      if (retry) retry.addEventListener('click', function () { loadAll(true); });
    } else if (kind === 'empty') {
      els.warningState.innerHTML =
        '<div class="state-banner info">' + icon('check', { size: 18 }) +
        '<span>No active warnings right now. Stay safe out there.</span></div>';
    } else {
      els.warningState.innerHTML = '';
    }
  }

  function warningSeverity(w) {
    const haystack = ((w.heading_en || '') + ' ' + (w.text_en || '') + ' ' + (w.warning_issue && w.warning_issue.title_en || '')).toLowerCase();
    if (haystack.indexOf('third category') !== -1 || haystack.indexOf('second category') !== -1 ||
      haystack.indexOf('bahaya') !== -1 || haystack.indexOf('severe') !== -1) {
      return 'danger';
    }
    return 'warn';
  }

  function handleWarningError(err) {
    setWarningState('error', err && err.message ? err.message : 'Could not load weather warnings.');
    els.warningList.innerHTML = '';
  }

  function handleWarnings(body) {
    const data = body && Array.isArray(body.data) ? body.data : null;
    if (!data) {
      handleWarningError(new Error('Unexpected warning response shape.'));
      return;
    }

    const now = Date.now();
    const active = data.filter(function (w) {
      if (!w || typeof w !== 'object') return false;
      const to = w.valid_to ? new Date(w.valid_to).getTime() : NaN;
      if (isNaN(to)) return true; // keep if we can't parse - better to show than hide
      return to >= now;
    });

    if (active.length === 0) {
      setWarningState('empty');
      els.warningList.innerHTML = '';
      return;
    }

    setWarningState('none');

    active.sort(function (a, b) {
      return (a.valid_from || '').localeCompare(b.valid_from || '');
    });

    els.warningList.innerHTML = active.map(function (w) {
      const severity = warningSeverity(w);
      const title = (w.heading_en || (w.warning_issue && w.warning_issue.title_en) || 'Weather Warning');
      const text = w.text_en || w.text_bm || 'No further details provided.';
      const iconName = severity === 'danger' ? 'alertOctagon' : (title.toLowerCase().indexOf('wind') !== -1 ? 'wind' : 'alertTriangle');
      return '<div class="state-banner ' + severity + ' warning-item">' +
        '<span class="warning-item-icon">' + icon(iconName, { size: 20 }) + '</span>' +
        '<div class="warning-item-body">' +
        '<div class="warning-item-title">' + escapeHtml(title) + '</div>' +
        '<div class="warning-item-meta">Valid ' + escapeHtml(fmtDateTime(w.valid_from)) + ' → ' + escapeHtml(fmtDateTime(w.valid_to)) + '</div>' +
        '<div class="warning-item-text">' + formatWarningText(text) + '</div>' +
        '</div></div>';
    }).join('');
  }

  // MET Malaysia sometimes packs several "Label:  value" fields into one
  // string, delimited by long runs of spaces rather than real line breaks
  // (e.g. tropical storm advisories). Split on those runs so each field
  // renders on its own line instead of collapsing into one clump.
  function formatWarningText(text) {
    const segments = String(text)
      .split(/ {3,}/)
      .map(function (s) { return s.trim(); })
      .filter(Boolean);
    return segments.map(escapeHtml).join('<br>');
  }
})();
