/*
  Earthquake warnings page.
  Fetches our own /api/quake-warning proxy (never data.gov.my directly),
  renders a hero summary + list of .glass cards, and defends against
  schema drift, empty payloads, and upstream failures at every step.
*/
(function () {
  "use strict";

  var CACHE_KEY = "quake-cache-v1";
  var CACHE_TTL_MS = 2.5 * 60 * 1000; // 2.5 min - safety-critical-ish data, keep fresh
  var ACTIVE_WINDOW_MS = 72 * 60 * 60 * 1000; // treat last 72h as "active"
  var CLIENT_TIMEOUT_MS = 10000;
  var MAX_LIST_ITEMS = 25;

  var heroTitle = document.getElementById("quakeHeroTitle");
  var heroSub = document.getElementById("quakeHeroSub");
  var updatedEl = document.getElementById("quakeUpdated");
  var refreshBtn = document.getElementById("quakeRefresh");
  var stateEl = document.getElementById("quakeState");
  var listEl = document.getElementById("quakeList");

  function init() {
    if (refreshBtn && window.Icons) {
      refreshBtn.innerHTML = Icons.html("refresh", { size: 16 }) + ' <span class="btn-label">Refresh</span>';
      refreshBtn.addEventListener("click", function () {
        load(true);
      });
    }
    load(false);
  }

  function readCache() {
    try {
      var raw = sessionStorage.getItem(CACHE_KEY);
      if (!raw) return null;
      var parsed = JSON.parse(raw);
      if (!parsed || typeof parsed.cachedAt !== "number") return null;
      if (Date.now() - parsed.cachedAt > CACHE_TTL_MS) return null;
      return parsed;
    } catch (e) {
      return null;
    }
  }

  function writeCache(payload) {
    try {
      sessionStorage.setItem(
        CACHE_KEY,
        JSON.stringify({ cachedAt: Date.now(), data: payload.data, fetchedAt: payload.fetchedAt })
      );
    } catch (e) {
      // sessionStorage unavailable/full - ignore, not critical
    }
  }

  function load(forceRefresh) {
    if (!forceRefresh) {
      var cached = readCache();
      if (cached) {
        render(cached.data, cached.fetchedAt);
        return;
      }
    }

    renderLoading();

    var controller = typeof AbortController !== "undefined" ? new AbortController() : null;
    var timeoutId = controller ? setTimeout(function () { controller.abort(); }, CLIENT_TIMEOUT_MS) : null;

    fetch("/api/quake-warning", { signal: controller ? controller.signal : undefined })
      .then(function (res) {
        return res
          .json()
          .catch(function () {
            return null;
          })
          .then(function (body) {
            return { ok: res.ok, status: res.status, body: body };
          });
      })
      .then(function (result) {
        if (timeoutId) clearTimeout(timeoutId);

        if (!result.ok || !result.body || result.body.success !== true) {
          var message =
            (result.body && result.body.error) ||
            "Could not load earthquake warnings (HTTP " + result.status + ").";
          renderError(message);
          return;
        }

        var data = Array.isArray(result.body.data) ? result.body.data : null;
        if (data === null) {
          renderError("Upstream data was not in the expected shape.");
          return;
        }

        writeCache({ data: data, fetchedAt: result.body.fetchedAt });
        render(data, result.body.fetchedAt);
      })
      .catch(function (err) {
        if (timeoutId) clearTimeout(timeoutId);
        var isAbort = err && err.name === "AbortError";
        renderError(isAbort ? "Request timed out. Please try again." : "Network error while fetching earthquake warnings.");
      });
  }

  function renderLoading() {
    heroTitle.textContent = "Checking current earthquake activity…";
    heroSub.textContent = "Live earthquake warnings, updated automatically.";
    updatedEl.textContent = "";
    stateEl.innerHTML =
      '<div class="state-banner info"><span class="spinner"></span><span>Loading earthquake warnings…</span></div>';
    listEl.innerHTML = "";
  }

  function renderError(message) {
    heroTitle.textContent = "Unable to check earthquake activity";
    heroSub.textContent = "Something went wrong reaching the earthquake warning feed.";
    updatedEl.textContent = "";
    var icon = window.Icons ? Icons.html("alertOctagon", { size: 18 }) : "";
    stateEl.innerHTML =
      '<div class="state-banner danger">' +
      icon +
      '<span>' + escapeHtml(message || "Unknown error.") + '</span>' +
      '</div>' +
      '<button class="btn btn-primary" id="quakeRetry" type="button">Retry</button>';
    listEl.innerHTML = "";

    var retryBtn = document.getElementById("quakeRetry");
    if (retryBtn) {
      retryBtn.addEventListener("click", function () {
        load(true);
      });
    }
  }

  function normalizeRecord(raw) {
    if (!raw || typeof raw !== "object") return null;

    var mag = typeof raw.magdefault === "number" ? raw.magdefault : parseFloat(raw.magdefault);
    if (isNaN(mag)) mag = null;

    var when = raw.localdatetime || raw.utcdatetime || null;
    var epochMs = NaN;
    if (raw.utcdatetime) {
      var parsedUtc = Date.parse(raw.utcdatetime + "Z");
      if (!isNaN(parsedUtc)) epochMs = parsedUtc;
    }
    if (isNaN(epochMs) && raw.localdatetime) {
      var parsedLocal = Date.parse(raw.localdatetime);
      if (!isNaN(parsedLocal)) epochMs = parsedLocal;
    }

    return {
      magnitude: mag,
      magType: raw.magtypedefault || "",
      location: raw.location || raw.location_original || "N/A",
      distance: raw.n_distancemas || raw.n_distancerest || "",
      depth: typeof raw.depth === "number" ? raw.depth : parseFloat(raw.depth),
      when: when,
      epochMs: epochMs,
      visible: raw.visible !== false,
      status: raw.status || "",
    };
  }

  function severityOf(mag) {
    if (typeof mag !== "number" || isNaN(mag)) return "low";
    if (mag >= 6) return "high";
    if (mag >= 5) return "medium";
    return "low";
  }

  function formatWhen(record) {
    if (!record.when) return "N/A";
    if (!isNaN(record.epochMs)) {
      try {
        return new Date(record.epochMs).toLocaleString("en-MY", {
          dateStyle: "medium",
          timeStyle: "short",
        });
      } catch (e) {
        return record.when;
      }
    }
    return record.when;
  }

  function escapeHtml(str) {
    return String(str)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function cardHtml(record) {
    var severity = severityOf(record.magnitude);
    var iconName = severity === "low" ? "activity" : severity === "medium" ? "alertTriangle" : "alertOctagon";
    var iconSeverityClass = severity === "high" ? " severity-high" : severity === "medium" ? " severity-medium" : "";
    var magText = typeof record.magnitude === "number" ? record.magnitude.toFixed(1) : "N/A";
    var depthText =
      typeof record.depth === "number" && !isNaN(record.depth) ? record.depth + " km depth" : null;

    var meta = [];
    meta.push(
      '<span>' + (window.Icons ? Icons.html("clock", { size: 14 }) : "") + " " + escapeHtml(formatWhen(record)) + "</span>"
    );
    if (depthText) {
      meta.push('<span>' + (window.Icons ? Icons.html("activity", { size: 14 }) : "") + " " + escapeHtml(depthText) + "</span>");
    }
    if (record.magType) {
      meta.push("<span>Type: " + escapeHtml(record.magType) + "</span>");
    }

    return (
      '<article class="glass quake-card">' +
      '<div class="quake-card-icon' + iconSeverityClass + '">' +
      (window.Icons ? Icons.html(iconName, { size: 20 }) : "") +
      "</div>" +
      '<div class="quake-card-body">' +
      '<div class="quake-card-top">' +
      '<span class="quake-mag' + (severity !== "low" ? " severity-" + severity : "") + '">M ' + magText + "</span>" +
      '<span class="quake-location">' + escapeHtml(record.location) + "</span>" +
      "</div>" +
      '<div class="quake-meta">' + meta.join("") + "</div>" +
      (record.distance ? '<div class="quake-distance">' + escapeHtml(record.distance) + "</div>" : "") +
      "</div>" +
      "</article>"
    );
  }

  function render(rawData, fetchedAt) {
    if (!Array.isArray(rawData)) {
      renderError("Upstream data was not in the expected shape.");
      return;
    }

    var updatedText = "";
    if (fetchedAt) {
      try {
        updatedText = "Last updated " + new Date(fetchedAt).toLocaleTimeString("en-MY", { timeStyle: "short" });
      } catch (e) {
        updatedText = "Last updated recently";
      }
    }
    updatedEl.innerHTML = (window.Icons ? Icons.html("clock", { size: 14 }) : "") + " " + escapeHtml(updatedText);

    if (rawData.length === 0) {
      heroTitle.textContent = "No active earthquake warnings";
      heroSub.textContent = "The earthquake warning feed currently has no records.";
      stateEl.innerHTML =
        '<div class="state-banner info">' +
        (window.Icons ? Icons.html("check", { size: 18 }) : "") +
        "<span>No active earthquake warnings right now.</span></div>";
      listEl.innerHTML = "";
      return;
    }

    var records = rawData
      .map(normalizeRecord)
      .filter(function (r) {
        return r && r.visible;
      })
      .sort(function (a, b) {
        var ae = isNaN(a.epochMs) ? -Infinity : a.epochMs;
        var be = isNaN(b.epochMs) ? -Infinity : b.epochMs;
        return be - ae;
      });

    var now = Date.now();
    var active = records.filter(function (r) {
      return !isNaN(r.epochMs) && now - r.epochMs <= ACTIVE_WINDOW_MS;
    });

    if (active.length === 0) {
      heroTitle.textContent = "No active earthquake warnings";
      heroSub.textContent = "No seismic activity reported in the last 72 hours. Showing recent history below.";
      stateEl.innerHTML =
        '<div class="state-banner info">' +
        (window.Icons ? Icons.html("check", { size: 18 }) : "") +
        "<span>No active earthquake warnings in the last 72 hours.</span></div>";
      listEl.innerHTML = records
        .slice(0, 10)
        .map(cardHtml)
        .join("");
      return;
    }

    heroTitle.textContent = active.length + " active warning" + (active.length === 1 ? "" : "s");
    heroSub.textContent = "Seismic activity reported in the last 72 hours.";
    stateEl.innerHTML = "";
    listEl.innerHTML = active.slice(0, MAX_LIST_ITEMS).map(cardHtml).join("");
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
