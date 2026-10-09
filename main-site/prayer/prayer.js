/*
  Prayer times page.
  Fetches our own /api/prayer-zones and /api/prayer-times proxies (never
  waktusolat.app directly), shows today's times for the chosen JAKIM zone and
  counts down to the next one. Every time is shown in Malaysia time, whatever
  the device's own time zone, since that is what the zone's times are for.
  Alerts live in prayer-alerts.js, which reads the zone from window.PrayerPage.
*/
(function () {
  "use strict";

  const ZONE_KEY = "malaysiaboleh.prayerZone";
  const DEFAULT_ZONE = "WLY01";
  const CLIENT_TIMEOUT_MS = 10000;
  const TICK_MS = 15000;
  const MYT_OFFSET_MS = 8 * 3600 * 1000;
  const TIME_ZONE = "Asia/Kuala_Lumpur";

  const PRAYERS = [
    { key: "imsak", label: "Imsak", icon: "moonStar", marker: true },
    { key: "fajr", label: "Subuh", icon: "sunrise" },
    { key: "syuruk", label: "Syuruk", icon: "sun", marker: true },
    { key: "dhuhr", label: "Zohor", icon: "sun" },
    { key: "asr", label: "Asar", icon: "sun" },
    { key: "maghrib", label: "Maghrib", icon: "sunset" },
    { key: "isha", label: "Isyak", icon: "moon" }
  ];
  // What the countdown counts to. Imsak is left out: outside Ramadan nobody
  // waits for it, and Subuh follows ten minutes later anyway.
  const COUNTDOWN_KEYS = ["fajr", "syuruk", "dhuhr", "asr", "maghrib", "isha"];
  // The prayer whose time it is, and what ends it.
  const PERIODS = [
    { key: "fajr", until: "syuruk" },
    { key: "dhuhr", until: "asr" },
    { key: "asr", until: "maghrib" },
    { key: "maghrib", until: "isha" },
    { key: "isha", until: null }
  ];
  const HIJRI_MONTHS = ["Muharam", "Safar", "Rabiulawal", "Rabiulakhir", "Jamadilawal", "Jamadilakhir",
    "Rejab", "Syaaban", "Ramadan", "Syawal", "Zulkaedah", "Zulhijah"];

  const $ = function (id) { return document.getElementById(id); };
  const zoneSelect = $("zoneSelect");
  const locateBtn = $("locateBtn");
  const stateEl = $("prayerState");

  let zones = [];
  let zone = readZone();
  // "YYYY-MM-DD" -> { date, hijri, times }
  let days = {};
  let loadedFor = null;
  let loading = false;

  /* -- Malaysia time -- */

  // Malaysia keeps no daylight saving, so MYT is always UTC+8.
  function mytDate(ms) {
    return new Date(ms + MYT_OFFSET_MS).toISOString().slice(0, 10);
  }

  function formatTime(seconds) {
    return new Date(seconds * 1000)
      .toLocaleTimeString("en-MY", { timeZone: TIME_ZONE, hour: "numeric", minute: "2-digit", hour12: true })
      .toLowerCase();
  }

  function formatDate(date) {
    return new Date(date + "T12:00:00+08:00").toLocaleDateString("en-MY", {
      timeZone: TIME_ZONE, weekday: "long", day: "numeric", month: "long", year: "numeric"
    });
  }

  function formatHijri(hijri) {
    const m = /^(\d+)-(\d+)-(\d+)$/.exec(hijri || "");
    if (!m || !HIJRI_MONTHS[Number(m[2]) - 1]) return "";
    return Number(m[3]) + " " + HIJRI_MONTHS[Number(m[2]) - 1] + " " + m[1] + "H";
  }

  function untilText(ms) {
    const minutes = Math.ceil(ms / 60000);
    if (minutes <= 1) return "in under a minute";
    if (minutes < 60) return "in " + minutes + " min";
    const h = Math.floor(minutes / 60);
    const rest = minutes % 60;
    return "in " + h + " h" + (rest ? " " + rest + " min" : "");
  }

  /* -- Storage -- */

  function readZone() {
    try {
      const saved = localStorage.getItem(ZONE_KEY);
      if (saved && /^[A-Z]{3}\d{2}$/.test(saved)) return saved;
    } catch (e) { /* storage unavailable */ }
    return DEFAULT_ZONE;
  }

  function saveZone(code) {
    try { localStorage.setItem(ZONE_KEY, code); } catch (e) { /* storage unavailable */ }
  }

  /* -- Fetching -- */

  function getJson(url) {
    const controller = typeof AbortController !== "undefined" ? new AbortController() : null;
    const timeoutId = controller ? setTimeout(function () { controller.abort(); }, CLIENT_TIMEOUT_MS) : null;
    return fetch(url, { signal: controller ? controller.signal : undefined })
      .then(function (res) {
        return res.json().catch(function () { return null; }).then(function (body) {
          if (!res.ok || !body || body.success !== true) {
            throw new Error((body && body.error) || "The server replied " + res.status + ".");
          }
          return body.data;
        });
      })
      .catch(function (err) {
        if (err && err.name === "AbortError") throw new Error("The request timed out. Please try again.");
        if (err instanceof TypeError) throw new Error("Network error while fetching prayer times.");
        throw err;
      })
      .finally(function () { if (timeoutId) clearTimeout(timeoutId); });
  }

  function loadZones() {
    return getJson("/api/prayer-zones").then(function (data) {
      zones = Array.isArray(data) ? data : [];
      renderZoneSelect();
    }).catch(function (err) {
      // The select keeps the one zone it was drawn with; times still load.
      console.warn("prayer zones unavailable:", err);
    });
  }

  // This month, and next month too on the last day, for tomorrow's Subuh.
  function loadTimes() {
    if (loading) return;
    loading = true;
    const now = Date.now();
    const today = mytDate(now);
    const tomorrow = mytDate(now + 86400000);
    const months = [today.slice(0, 7)];
    if (tomorrow.slice(0, 7) !== months[0]) months.push(tomorrow.slice(0, 7));
    const forZone = zone;

    if (!days[today]) renderLoading();

    Promise.all(months.map(function (month) {
      return getJson("/api/prayer-times?zone=" + encodeURIComponent(forZone) + "&month=" + month);
    })).then(function (results) {
      if (forZone !== zone) return;
      days = {};
      results.forEach(function (data) {
        (data && Array.isArray(data.days) ? data.days : []).forEach(function (d) { days[d.date] = d; });
      });
      loadedFor = today;
      if (!days[today]) {
        renderError("JAKIM has no times for today in this zone yet.");
        return;
      }
      stateEl.innerHTML = "";
      render();
    }).catch(function (err) {
      if (forZone !== zone) return;
      renderError(err.message);
    }).finally(function () {
      loading = false;
      // The zone changed while this was in flight.
      if (forZone !== zone) loadTimes();
    });
  }

  /* -- Drawing -- */

  function escapeHtml(str) {
    return String(str)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function icon(name, size) {
    return window.Icons ? Icons.html(name, { size: size || 20 }) : "";
  }

  function zoneLabel(code) {
    const z = zones.find(function (x) { return x.code === code; });
    return z ? z.districts : code;
  }

  function renderZoneSelect() {
    if (!zones.length) return;
    if (!zones.some(function (z) { return z.code === zone; })) zone = DEFAULT_ZONE;

    const byState = {};
    zones.forEach(function (z) { (byState[z.state] = byState[z.state] || []).push(z); });
    zoneSelect.innerHTML = Object.keys(byState).map(function (state) {
      return '<optgroup label="' + escapeHtml(state) + '">' + byState[state].map(function (z) {
        return '<option value="' + z.code + '">' + escapeHtml(z.districts) + " (" + z.code + ")</option>";
      }).join("") + "</optgroup>";
    }).join("");
    zoneSelect.value = zone;
    if (days[mytDate(Date.now())]) render();
  }

  function renderLoading() {
    $("nextIcon").innerHTML = icon("clock", 32);
    $("nextHeadline").textContent = "Loading prayer times…";
    $("nextSub").textContent = "Fetching today's times from JAKIM.";
    $("timesCard").hidden = true;
    stateEl.innerHTML =
      '<div class="state-banner info"><span class="spinner"></span><span>Loading prayer times…</span></div>';
  }

  function renderError(message) {
    $("nextIcon").innerHTML = icon("alertTriangle", 32);
    $("nextHeadline").textContent = "Couldn't load prayer times";
    $("nextSub").textContent = "Something went wrong reaching JAKIM's times.";
    $("timesCard").hidden = true;
    stateEl.innerHTML =
      '<div class="state-banner danger">' + icon("alertOctagon", 18) +
      "<span>" + escapeHtml(message || "Unknown error.") + "</span></div>" +
      '<button class="btn btn-primary" id="prayerRetry" type="button">Retry</button>';
    $("prayerRetry").addEventListener("click", loadTimes);
  }

  // The prayer whose time it is now, as { key, start, end } in seconds, or null
  // between Syuruk and Zohor. Before Subuh it is still last night's Isyak.
  function currentPeriod(today, yesterday, nowS) {
    if (nowS < today.times.fajr) {
      return yesterday ? { key: "isha", start: yesterday.times.isha, end: today.times.fajr, today: false } : null;
    }
    for (let i = PERIODS.length - 1; i >= 0; i--) {
      const p = PERIODS[i];
      if (nowS >= today.times[p.key]) {
        const end = p.until ? today.times[p.until] : null;
        if (end !== null && nowS >= end) return null;
        return { key: p.key, start: today.times[p.key], end: end, today: true };
      }
    }
    return null;
  }

  function nextTime(today, tomorrow, nowS) {
    for (const key of COUNTDOWN_KEYS) {
      if (today.times[key] > nowS) return { key: key, at: today.times[key], day: today };
    }
    return tomorrow ? { key: "fajr", at: tomorrow.times.fajr, day: tomorrow } : null;
  }

  function labelOf(key) {
    return PRAYERS.find(function (p) { return p.key === key; }).label;
  }

  function render() {
    const nowMs = Date.now();
    const nowS = nowMs / 1000;
    const today = days[mytDate(nowMs)];
    if (!today) return;
    const yesterday = days[mytDate(nowMs - 86400000)];
    const tomorrow = days[mytDate(nowMs + 86400000)];

    const current = currentPeriod(today, yesterday, nowS);
    const next = nextTime(today, tomorrow, nowS);

    // Next prayer card.
    if (next) {
      const p = PRAYERS.find(function (x) { return x.key === next.key; });
      const until = untilText(next.at * 1000 - nowMs);
      $("nextIcon").innerHTML = icon(p.icon, 32);
      $("nextHeadline").textContent = next.key === "syuruk"
        ? "Subuh ends " + until
        : p.label + " " + until;
      const at = (next.key === "syuruk" ? "Syuruk at " : "At ") + formatTime(next.at) +
        (next.day !== today ? " tomorrow" : "");
      const now = current ? " Now: " + labelOf(current.key) + ", since " + formatTime(current.start) + "." : "";
      $("nextSub").textContent = at + "." + now;
    }

    // Today's list.
    $("timesDate").textContent = formatDate(today.date);
    $("timesHijri").textContent = formatHijri(today.hijri);
    $("timesList").innerHTML = PRAYERS.map(function (p) {
      const classes = ["times-row"];
      if (p.marker) classes.push("marker");
      if (current && current.today && current.key === p.key) classes.push("current");
      if (next && next.day === today && next.key === p.key) classes.push("next");
      const tag = classes.indexOf("current") !== -1 ? '<span class="times-row-tag">now</span>'
        : classes.indexOf("next") !== -1 ? '<span class="times-row-tag">next</span>' : "";
      return '<li class="' + classes.join(" ") + '">' +
        '<span class="times-row-icon">' + icon(p.icon, 20) + "</span>" +
        '<span class="times-row-name">' + p.label + tag + "</span>" +
        '<span class="times-row-time">' + formatTime(today.times[p.key]) + "</span>" +
        "</li>";
    }).join("");
    $("timesCard").hidden = false;
  }

  // Redraws the countdown, and fetches again once the day has turned.
  function tick() {
    if (loadedFor && mytDate(Date.now()) !== loadedFor) loadTimes();
    else render();
  }

  /* -- Zone choice -- */

  function setZone(code) {
    if (!code || code === zone) return;
    zone = code;
    saveZone(code);
    zoneSelect.value = code;
    days = {};
    loadTimes();
    document.dispatchEvent(new CustomEvent("prayer:zonechange", { detail: { zone: code } }));
  }

  function locate() {
    if (!("geolocation" in navigator)) {
      stateEl.innerHTML = '<div class="state-banner warn">' + icon("alertTriangle", 18) +
        "<span>This browser can't share its location. Pick your zone from the list instead.</span></div>";
      return;
    }
    locateBtn.disabled = true;
    navigator.geolocation.getCurrentPosition(function (pos) {
      const lat = pos.coords.latitude.toFixed(4);
      const lng = pos.coords.longitude.toFixed(4);
      getJson("/api/prayer-zones?lat=" + lat + "&lng=" + lng).then(function (data) {
        stateEl.innerHTML = "";
        setZone(data.zone);
      }).catch(function (err) {
        stateEl.innerHTML = '<div class="state-banner warn">' + icon("alertTriangle", 18) +
          "<span>" + escapeHtml(err.message) + " Pick your zone from the list instead.</span></div>";
      }).finally(function () { locateBtn.disabled = false; });
    }, function () {
      locateBtn.disabled = false;
      stateEl.innerHTML = '<div class="state-banner warn">' + icon("alertTriangle", 18) +
        "<span>Couldn't get your location. Allow it for this site, or pick your zone from the list.</span></div>";
    }, { timeout: 15000, maximumAge: 600000 });
  }

  function init() {
    if (window.UI) UI.hydrateIcons(document.querySelector(".prayer-toolbar"));
    // Until the zone list arrives, the saved zone is the only choice shown.
    if (zone !== DEFAULT_ZONE) zoneSelect.innerHTML = '<option value="' + zone + '">' + zone + "</option>";
    zoneSelect.value = zone;
    zoneSelect.addEventListener("change", function () { setZone(zoneSelect.value); });
    locateBtn.addEventListener("click", locate);

    loadZones();
    loadTimes();
    setInterval(tick, TICK_MS);
    document.addEventListener("visibilitychange", function () {
      if (document.visibilityState === "visible") tick();
    });
  }

  window.PrayerPage = {
    zone: function () { return zone; },
    zoneLabel: zoneLabel
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
