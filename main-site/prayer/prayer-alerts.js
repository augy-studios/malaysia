/*
  Prayer alerts: a notification at each chosen time, for the zone picked on the
  page, while the site is closed. The page subscribes to Web Push and tells
  /api/push/devices which zone, times and early reminder it wants; the
  per-minute cron (api/cron/prayer-alerts.js) pushes. Follows sg-psi's
  js/alerts.js.
  Classic script; reads the zone from window.PrayerPage (prayer.js).
*/
(function () {
  "use strict";

  const API_BASE = "/api/push";
  const PREFS_KEY = "malaysiaboleh.prayerAlerts";
  const DEVICE_KEY = "malaysiaboleh.pushDeviceId";
  const PRAYERS = ["imsak", "fajr", "syuruk", "dhuhr", "asr", "maghrib", "isha"];
  const LABELS = { imsak: "Imsak", fajr: "Subuh", syuruk: "Syuruk", dhuhr: "Zohor", asr: "Asar", maghrib: "Maghrib", isha: "Isyak" };
  const EARLY = [0, 5, 10, 15];
  const DEFAULTS = { on: false, prayers: ["fajr", "dhuhr", "asr", "maghrib", "isha"], early: 0 };

  const $ = function (id) { return document.getElementById(id); };
  const page = window.PrayerPage;

  const PROBLEMS = {
    unsupported: "This browser can't receive notifications. On iPhone or iPad, add Malaysia Boleh to your Home Screen, open it from there, and try again.",
    denied: "Notifications are blocked for this site. Allow them in your browser's site settings, then try again.",
    failed: "Couldn't reach the alerts server. Try again in a minute."
  };

  function prefs() {
    try {
      const saved = JSON.parse(localStorage.getItem(PREFS_KEY) || "null");
      if (saved && Array.isArray(saved.prayers) && EARLY.indexOf(saved.early) !== -1) {
        const prayers = PRAYERS.filter(function (k) { return saved.prayers.indexOf(k) !== -1; });
        if (prayers.length) return { on: saved.on === true, prayers: prayers, early: saved.early };
      }
    } catch (e) { /* storage unavailable or garbled */ }
    return Object.assign({}, DEFAULTS);
  }

  function save(p) {
    try { localStorage.setItem(PREFS_KEY, JSON.stringify(p)); } catch (e) { /* storage unavailable */ }
  }

  function deviceId() {
    let id = null;
    try {
      id = localStorage.getItem(DEVICE_KEY);
      if (!id) {
        id = crypto.randomUUID();
        localStorage.setItem(DEVICE_KEY, id);
      }
    } catch (e) { /* storage unavailable */ }
    return id || crypto.randomUUID();
  }

  const pushSupported = function () {
    return "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
  };

  function listText(keys) {
    const names = keys.map(function (k) { return LABELS[k]; });
    return names.length > 1 ? names.slice(0, -1).join(", ") + " and " + names[names.length - 1] : names[0];
  }

  function noteFor(p) {
    if (!p.on) return "Alerts are off. Pick the times you want, then turn them on.";
    const early = p.early ? ", and " + p.early + " minutes before each" : "";
    return "You'll be notified at " + listText(p.prayers) + early + ". Alerts follow the zone picked on the page.";
  }

  // Draws the modal's state, and the page button's icon.
  function show(p, problem) {
    document.querySelectorAll("[data-alert-prayer]").forEach(function (btn) {
      const on = p.prayers.indexOf(btn.dataset.alertPrayer) !== -1;
      btn.classList.toggle("active", on);
      btn.setAttribute("aria-pressed", String(on));
    });
    document.querySelectorAll("[data-alert-early]").forEach(function (btn) {
      const on = Number(btn.dataset.alertEarly) === p.early;
      btn.classList.toggle("active", on);
      btn.setAttribute("aria-pressed", String(on));
    });
    const toggle = $("alertsToggle");
    toggle.textContent = p.on ? "Turn alerts off" : "Turn alerts on";
    toggle.classList.toggle("btn-primary", !p.on);
    $("alertsTest").hidden = !p.on;
    note(problem ? PROBLEMS[problem] : noteFor(p), problem ? "bad" : "");
    showZone();

    const btn = $("alertsBtn");
    btn.classList.toggle("is-on", p.on);
    btn.querySelector("[data-icon]").setAttribute("data-icon", p.on ? "bellRing" : "bell");
    btn.setAttribute("aria-label", p.on ? "Prayer alerts, on" : "Prayer alerts");
    if (window.UI) UI.hydrateIcons(btn);
  }

  function showZone() {
    const code = page.zone();
    $("alertsZone").innerHTML = "";
    $("alertsZone").append("For ");
    const strong = document.createElement("strong");
    strong.textContent = page.zoneLabel(code) + " (" + code + ")";
    $("alertsZone").append(strong, ".");
  }

  function note(text, tone) {
    $("alertsNote").textContent = text;
    $("alertsNote").dataset.tone = tone;
  }

  // navigator.serviceWorker.ready never settles if registration failed, so it gets a
  // time limit.
  function readyRegistration() {
    return Promise.race([
      navigator.serviceWorker.ready,
      new Promise(function (_, reject) { setTimeout(function () { reject(new Error("service worker not ready")); }, 10000); })
    ]);
  }

  async function fetchPublicKey() {
    const res = await fetch(API_BASE + "/vapid-key");
    if (!res.ok) throw new Error("alerts server replied " + res.status);
    const publicKey = (await res.json()).publicKey;
    const b64 = (publicKey + "=".repeat((4 - (publicKey.length % 4)) % 4)).replace(/-/g, "+").replace(/_/g, "/");
    return Uint8Array.from(atob(b64), function (c) { return c.charCodeAt(0); });
  }

  async function subscription() {
    const reg = await readyRegistration();
    return (await reg.pushManager.getSubscription()) || await reg.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: await fetchPublicKey()
    });
  }

  async function subscribe(p) {
    const sub = await subscription();
    const res = await fetch(API_BASE + "/devices/" + deviceId(), {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ subscription: sub.toJSON(), zone: page.zone(), prayers: p.prayers, early: p.early })
    });
    if (!res.ok) throw new Error("alerts server replied " + res.status);
  }

  // Unsubscribe and have the server forget this device. Browsers don't let a page
  // revoke notification permission itself; this is the closest it gets.
  async function unsubscribe() {
    try {
      const reg = await navigator.serviceWorker.getRegistration();
      const sub = reg ? await reg.pushManager.getSubscription() : null;
      if (sub) await sub.unsubscribe();
    } catch (err) {
      console.warn("unsubscribe failed:", err);
    }
    try {
      await fetch(API_BASE + "/devices/" + deviceId(), { method: "DELETE" });
    } catch (err) {
      // The subscription is already gone, so the server drops the device the first
      // time a push to it fails.
      console.warn("device delete failed:", err);
    }
  }

  let busy = false;

  // `change` is what was just picked; turning on asks for permission first.
  async function apply(change) {
    if (busy) return;
    const previous = prefs();
    const next = Object.assign({}, previous, change);

    if (!next.on) {
      save(next);
      show(next);
      if (previous.on && pushSupported()) await unsubscribe();
      return;
    }
    if (!pushSupported()) return show(Object.assign({}, next, { on: false }), "unsupported");

    busy = true;
    try {
      const perm = Notification.permission === "granted" ? "granted" : await Notification.requestPermission();
      if (perm !== "granted") return show(Object.assign({}, next, { on: false }), "denied");
      show(next);
      await subscribe(next);
      save(next);
    } catch (err) {
      console.warn("prayer alerts unavailable:", err);
      show(previous, "failed");
    } finally {
      busy = false;
    }
  }

  // A choice made while alerts are off is only remembered; while on, it is sent.
  function choose(change) {
    const p = prefs();
    if (p.on) apply(change);
    else {
      save(Object.assign({}, p, change));
      show(prefs());
    }
  }

  const postTest = function () { return fetch(API_BASE + "/devices/" + deviceId(), { method: "POST" }); };

  // Has the server push to this device the way a real alert comes, so a missing key, a
  // dead subscription or muted notifications show up now, not at the next prayer.
  async function test() {
    if (busy) return;
    busy = true;
    $("alertsTest").disabled = true;
    try {
      let res = await postTest();
      // The server doesn't know this device, or its subscription has died: sign up
      // again, with a fresh subscription if it died, and try once more.
      if (res.status === 404 || res.status === 410) {
        if (res.status === 410) {
          const sub = await (await readyRegistration()).pushManager.getSubscription();
          if (sub) await sub.unsubscribe();
        }
        await subscribe(prefs());
        res = await postTest();
      }
      if (res.ok) {
        note("Test alert sent. Nothing within a minute? Check that notifications from this browser aren't muted or held back by Do Not Disturb.", "");
      } else if (res.status === 429) {
        note("A test alert was just sent. Try again in a few seconds.", "");
      } else {
        const body = await res.json().catch(function () { return {}; });
        note("Couldn't send a test alert: " + (body.error || "the server replied " + res.status) + ".", "bad");
      }
    } catch (err) {
      console.warn("test alert failed:", err);
      note(PROBLEMS.failed, "bad");
    } finally {
      busy = false;
      $("alertsTest").disabled = false;
    }
  }

  // Re-sends the choice on every load, which also refreshes the subscription. A
  // permission revoked in the browser's settings turns alerts off here too.
  function resync() {
    const p = prefs();
    if (!p.on) return;
    if (!pushSupported() || Notification.permission !== "granted") {
      const off = Object.assign({}, p, { on: false });
      save(off);
      show(off);
      return;
    }
    subscribe(p).catch(function (err) { console.warn("prayer alerts resync failed:", err); });
  }

  function init() {
    if (window.UI) UI.hydrateIcons($("alertsModal"));
    $("alertPrayers").addEventListener("click", function (e) {
      const btn = e.target.closest("[data-alert-prayer]");
      if (!btn) return;
      const key = btn.dataset.alertPrayer;
      const current = prefs().prayers;
      const has = current.indexOf(key) !== -1;
      if (has && current.length === 1) {
        note("Keep at least one time. To stop every alert, turn alerts off.", "");
        return;
      }
      const prayers = PRAYERS.filter(function (k) { return k === key ? !has : current.indexOf(k) !== -1; });
      choose({ prayers: prayers });
    });
    $("alertEarly").addEventListener("click", function (e) {
      const btn = e.target.closest("[data-alert-early]");
      if (!btn) return;
      choose({ early: Number(btn.dataset.alertEarly) });
    });
    $("alertsToggle").addEventListener("click", function () { apply({ on: !prefs().on }); });
    $("alertsTest").addEventListener("click", test);
    $("alertsBtn").addEventListener("click", function () {
      show(prefs());
      if (window.UI) UI.openModal("alertsModal");
    });

    // Alerts follow the page's zone.
    document.addEventListener("prayer:zonechange", function () {
      showZone();
      resync();
    });

    show(prefs());
    resync();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
