/*
  Flood Alerts page logic.
  Fetches our own /api/flood-warning proxy (which itself proxies data.gov.my's
  JPS flood-warning feed), caches the payload in sessionStorage for a few
  minutes, and renders a searchable/filterable list of stations.
*/
(function () {
  "use strict";

  const CACHE_KEY = "fa-flood-warning-cache-v1";
  const CACHE_TTL_MS = 4 * 60 * 1000; // 4 minutes

  const ELEVATED = ["ALERT", "WARNING", "DANGER"];
  const PAGE_SIZE = 15;

  const els = {};

  let allStations = [];
  let activeFilter = "alert"; // "alert" | "all"
  let searchTerm = "";
  let searchDebounce = null;
  let currentPage = 1;

  document.addEventListener("DOMContentLoaded", init);

  function init() {
    cacheEls();
    wireEvents();
    loadData(false);
  }

  function cacheEls() {
    els.statusHero = document.getElementById("statusHero");
    els.loadingState = document.getElementById("loadingState");
    els.errorState = document.getElementById("errorState");
    els.errorText = document.getElementById("errorText");
    els.errorIcon = document.getElementById("errorIcon");
    els.retryButton = document.getElementById("retryButton");
    els.retryIcon = document.getElementById("retryIcon");
    els.emptyState = document.getElementById("emptyState");
    els.emptyIcon = document.getElementById("emptyIcon");
    els.contentState = document.getElementById("contentState");
    els.searchInput = document.getElementById("searchInput");
    els.updatedText = document.getElementById("updatedText");
    els.refreshButton = document.getElementById("refreshButton");
    els.refreshIcon = document.getElementById("refreshIcon");
    els.filterRow = document.getElementById("filterRow");
    els.resultCount = document.getElementById("resultCount");
    els.stationList = document.getElementById("stationList");
    els.noResults = document.getElementById("noResults");
    els.paginationRow = document.getElementById("paginationRow");
    els.pagePrev = document.getElementById("pagePrev");
    els.pageNext = document.getElementById("pageNext");
    els.pagePrevIcon = document.getElementById("pagePrevIcon");
    els.pageNextIcon = document.getElementById("pageNextIcon");
    els.pageIndicator = document.getElementById("pageIndicator");
  }

  function wireEvents() {
    if (window.Icons) {
      els.errorIcon.innerHTML = Icons.html("alertOctagon", { size: 20 });
      els.retryIcon.innerHTML = Icons.html("refresh", { size: 16 });
      els.emptyIcon.innerHTML = Icons.html("droplet", { size: 20 });
      els.refreshIcon.innerHTML = Icons.html("refresh", { size: 16 });
      els.pagePrevIcon.innerHTML = Icons.html("chevronLeft", { size: 16 });
      els.pageNextIcon.innerHTML = Icons.html("chevronRight", { size: 16 });
    }

    els.retryButton.addEventListener("click", function () {
      loadData(true);
    });

    els.refreshButton.addEventListener("click", function () {
      loadData(true);
    });

    els.searchInput.addEventListener("input", function (e) {
      const value = e.target.value;
      clearTimeout(searchDebounce);
      searchDebounce = setTimeout(function () {
        searchTerm = (value || "").trim().toLowerCase();
        currentPage = 1;
        renderList();
      }, 120);
    });

    els.filterRow.addEventListener("click", function (e) {
      const btn = e.target.closest(".fa-filter-chip");
      if (!btn) return;
      activeFilter = btn.getAttribute("data-filter") || "alert";
      updateFilterChips();
      currentPage = 1;
      renderList();
    });

    els.pagePrev.addEventListener("click", function () {
      if (currentPage > 1) {
        currentPage -= 1;
        renderList();
        els.stationList.scrollIntoView({ behavior: "smooth", block: "start" });
      }
    });

    els.pageNext.addEventListener("click", function () {
      currentPage += 1;
      renderList();
      els.stationList.scrollIntoView({ behavior: "smooth", block: "start" });
    });

    updateFilterChips();
  }

  function updateFilterChips() {
    const chips = els.filterRow.querySelectorAll(".fa-filter-chip");
    chips.forEach(function (chip) {
      const isActive = chip.getAttribute("data-filter") === activeFilter;
      chip.classList.toggle("is-active", isActive);
    });
  }

  function showState(state) {
    els.loadingState.hidden = state !== "loading";
    els.errorState.hidden = state !== "error";
    els.emptyState.hidden = state !== "empty";
    els.contentState.hidden = state !== "content";
  }

  function readCache() {
    try {
      const raw = sessionStorage.getItem(CACHE_KEY);
      if (!raw) return null;
      const parsed = JSON.parse(raw);
      if (!parsed || typeof parsed.savedAt !== "number") return null;
      if (Date.now() - parsed.savedAt > CACHE_TTL_MS) return null;
      return parsed;
    } catch (err) {
      return null;
    }
  }

  function writeCache(payload) {
    try {
      sessionStorage.setItem(
        CACHE_KEY,
        JSON.stringify({
          savedAt: Date.now(),
          data: payload.data,
          updated_at: payload.updated_at,
        })
      );
    } catch (err) {
      // sessionStorage may be unavailable (private mode, quota) - non-fatal
    }
  }

  function loadData(forceRefresh) {
    showState("loading");

    if (!forceRefresh) {
      const cached = readCache();
      if (cached) {
        applyData(cached.data, cached.updated_at);
        return;
      }
    } else {
      try {
        sessionStorage.removeItem(CACHE_KEY);
      } catch (err) {
        /* ignore */
      }
    }

    fetch("/api/flood-warning")
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
        if (!result.ok || !result.body || result.body.success !== true) {
          const message =
            (result.body && result.body.error) ||
            "Could not load flood warning data (status " + result.status + ").";
          throw new Error(message);
        }
        const data = Array.isArray(result.body.data) ? result.body.data : null;
        if (!data) {
          throw new Error("Unexpected response shape from flood warning feed.");
        }
        writeCache(result.body);
        applyData(data, result.body.updated_at);
      })
      .catch(function (err) {
        showError((err && err.message) || "Failed to load flood warning data.");
      });
  }

  function showError(message) {
    els.errorText.textContent = message;
    showState("error");
  }

  function applyData(data, updatedAt) {
    allStations = Array.isArray(data) ? data : [];

    if (allStations.length === 0) {
      showState("empty");
      return;
    }

    if (els.updatedText) {
      els.updatedText.textContent = "Last updated " + formatRelativeOrTime(updatedAt);
    }

    currentPage = 1;
    renderHero();
    renderList();
    showState("content");
  }

  function severity(station) {
    const indicator = (station && station.water_level_indicator) || null;
    if (indicator === "DANGER") return "danger";
    if (indicator === "WARNING") return "warning";
    if (indicator === "ALERT") return "alert";
    if (indicator === "NORMAL") return "normal";
    return "unknown";
  }

  function isElevated(station) {
    const indicator = station && station.water_level_indicator;
    return ELEVATED.indexOf(indicator) !== -1;
  }

  function renderHero() {
    const elevatedCount = allStations.filter(isElevated).length;
    const dangerCount = allStations.filter(function (s) {
      return severity(s) === "danger";
    }).length;

    let heroClass = "is-normal";
    let icon = "waves";
    let title = "All stations reporting normal levels";
    let sub = allStations.length + " stations monitored nationwide.";

    if (elevatedCount > 0) {
      heroClass = dangerCount > 0 ? "is-danger" : "is-warn";
      icon = dangerCount > 0 ? "alertOctagon" : "alertTriangle";
      title =
        elevatedCount +
        " station" +
        (elevatedCount === 1 ? "" : "s") +
        " above alert level";
      sub =
        (dangerCount > 0
          ? dangerCount + " at danger level. "
          : "") +
        "Out of " +
        allStations.length +
        " stations monitored nationwide.";
    }

    els.statusHero.className = "fa-status-hero glass " + heroClass;
    els.statusHero.hidden = false;
    els.statusHero.innerHTML =
      '<span class="fa-status-hero-icon">' +
      (window.Icons ? Icons.html(icon, { size: 32 }) : "") +
      "</span>" +
      '<div><div class="fa-status-hero-title">' +
      escapeHtml(title) +
      '</div><div class="fa-status-hero-sub">' +
      escapeHtml(sub) +
      "</div></div>";
  }

  function matchesSearch(station, term) {
    if (!term) return true;
    const haystack = [
      station && station.station_name,
      station && station.sub_basin,
      station && station.main_basin,
      station && station.district,
      station && station.state,
    ]
      .filter(Boolean)
      .join(" ")
      .toLowerCase();
    return haystack.indexOf(term) !== -1;
  }

  function renderList() {
    let filtered = allStations;

    if (activeFilter === "alert") {
      filtered = filtered.filter(isElevated);
    }

    if (searchTerm) {
      filtered = filtered.filter(function (s) {
        return matchesSearch(s, searchTerm);
      });
    }

    const totalResults = filtered.length;
    const totalPages = Math.max(1, Math.ceil(totalResults / PAGE_SIZE));
    if (currentPage > totalPages) currentPage = totalPages;
    if (currentPage < 1) currentPage = 1;

    const start = (currentPage - 1) * PAGE_SIZE;
    const toRender = filtered.slice(start, start + PAGE_SIZE);

    if (totalResults === 0) {
      els.resultCount.textContent =
        activeFilter === "alert" && !searchTerm
          ? "No stations currently at alert level or above."
          : "No stations found" + (searchTerm ? ' for "' + searchTerm + '"' : "") + ".";
    } else {
      const rangeStart = start + 1;
      const rangeEnd = Math.min(start + PAGE_SIZE, totalResults);
      els.resultCount.textContent =
        "Showing " +
        rangeStart +
        "-" +
        rangeEnd +
        " of " +
        totalResults +
        (activeFilter === "alert" && !searchTerm
          ? " station(s) at alert level or above."
          : " station(s)" + (searchTerm ? ' for "' + searchTerm + '"' : "") + ".");
    }

    els.noResults.hidden = toRender.length !== 0;
    els.stationList.innerHTML = toRender.map(renderCard).join("");

    els.paginationRow.hidden = totalResults <= PAGE_SIZE;
    els.pagePrev.disabled = currentPage <= 1;
    els.pageNext.disabled = currentPage >= totalPages;
    els.pageIndicator.textContent = "Page " + currentPage + " of " + totalPages;
  }

  function renderCard(station) {
    const sev = severity(station);
    const name = safeText(station && station.station_name, "Unnamed station");
    const state = safeText(station && station.state, "");
    const district = safeText(station && station.district, "");
    const location = [district, state].filter(Boolean).join(", ") || "Location unknown";

    const current = formatLevel(station && station.water_level_current);
    const normalLevel = formatLevel(station && station.water_level_normal_level);
    const alertLevel = formatLevel(station && station.water_level_alert_level);
    const dangerLevel = formatLevel(station && station.water_level_danger_level);

    const trend = (station && station.water_level_trend) || null;
    const updatedAt = (station && station.water_level_update_datetime) || null;

    const badgeIcon =
      sev === "danger" || sev === "warning"
        ? "alertOctagon"
        : sev === "alert"
        ? "alertTriangle"
        : "droplet";

    const badgeLabel =
      sev === "unknown" ? "No data" : sev.charAt(0).toUpperCase() + sev.slice(1);

    return (
      '<article class="fa-station-card glass">' +
      '<div class="fa-station-head">' +
      (window.Icons ? Icons.html("waves", { size: 20 }) : "") +
      "<div>" +
      '<div class="fa-station-name">' +
      escapeHtml(name) +
      "</div>" +
      '<div class="fa-station-loc">' +
      (window.Icons ? Icons.html("mapPin", { size: 14 }) : "") +
      "<span>" +
      escapeHtml(location) +
      "</span></div>" +
      "</div>" +
      "</div>" +
      '<span class="fa-station-badge status-' +
      sev +
      '">' +
      (window.Icons ? Icons.html(badgeIcon, { size: 14 }) : "") +
      "<span>" +
      escapeHtml(badgeLabel) +
      "</span></span>" +
      '<div class="fa-station-levels">' +
      '<div class="fa-station-level"><span>Current</span><strong>' +
      current +
      "</strong></div>" +
      '<div class="fa-station-level"><span>Normal</span><strong>' +
      normalLevel +
      "</strong></div>" +
      '<div class="fa-station-level"><span>Alert</span><strong>' +
      alertLevel +
      "</strong></div>" +
      '<div class="fa-station-level"><span>Danger</span><strong>' +
      dangerLevel +
      "</strong></div>" +
      "</div>" +
      '<div class="fa-station-foot">' +
      (window.Icons ? Icons.html("clock", { size: 14 }) : "") +
      "<span>" +
      (trend ? "Trend: " + escapeHtml(trend.toLowerCase()) + " &middot; " : "") +
      "Updated " +
      escapeHtml(formatRelativeOrTime(updatedAt)) +
      "</span>" +
      "</div>" +
      "</article>"
    );
  }

  function formatLevel(value) {
    if (value === null || value === undefined || value === "" || Number.isNaN(Number(value))) {
      return "&ndash;";
    }
    return Number(value).toFixed(2) + "m";
  }

  function safeText(value, fallback) {
    if (value === null || value === undefined || value === "") return fallback;
    return String(value);
  }

  function formatRelativeOrTime(isoLike) {
    if (!isoLike) return "unknown time";
    const date = new Date(isoLike.replace(" ", "T"));
    if (Number.isNaN(date.getTime())) return "unknown time";
    const diffMs = Date.now() - date.getTime();
    const diffMin = Math.round(diffMs / 60000);
    if (diffMin < 1) return "just now";
    if (diffMin < 60) return diffMin + " min ago";
    const diffHr = Math.round(diffMin / 60);
    if (diffHr < 24) return diffHr + " hr ago";
    return date.toLocaleString();
  }

  function escapeHtml(str) {
    return String(str == null ? "" : str).replace(/[&<>"']/g, function (ch) {
      return (
        {
          "&": "&amp;",
          "<": "&lt;",
          ">": "&gt;",
          '"': "&quot;",
          "'": "&#39;",
        }[ch] || ch
      );
    });
  }
})();
