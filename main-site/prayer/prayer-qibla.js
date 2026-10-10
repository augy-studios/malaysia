/*
  Kiblat compass: the Qibla's bearing from where the reader is, on a dial that
  turns with the device's compass, so they can see when they are facing it.
  The bearing is the great-circle one to the Kaaba, from the device's location
  if it shares one, else from the capital of the zone's state. Heading comes
  from deviceorientationabsolute (Chrome on Android) or webkitCompassHeading
  (Safari on iOS, after asking permission). Both read magnetic north, which in
  Malaysia is under a degree from true north, well inside the compass's own
  error, so no declination is applied.
  Classic script; reads the zone from window.PrayerPage (prayer.js).
*/
(function () {
  "use strict";

  const KAABA = { lat: 21.4225, lng: 39.8262 };
  // Within this many degrees either way counts as facing the Qibla.
  const ALIGNED_DEG = 5;
  // No usable reading this long after listening starts means no compass.
  const NO_COMPASS_MS = 3000;
  // How far each reading moves the dial toward the new heading, to steady it.
  const SMOOTHING = 0.35;

  // Where the bearing is taken from when the device won't share its location:
  // each state's capital, which keeps it within a degree or so of the reader.
  const STATE_POINTS = {
    JHR: { name: "Johor Bahru", lat: 1.4927, lng: 103.7414 },
    KDH: { name: "Alor Setar", lat: 6.1184, lng: 100.3685 },
    KTN: { name: "Kota Bharu", lat: 6.1254, lng: 102.2381 },
    MLK: { name: "Melaka", lat: 2.1896, lng: 102.2501 },
    NGS: { name: "Seremban", lat: 2.7258, lng: 101.9424 },
    PHG: { name: "Kuantan", lat: 3.8077, lng: 103.326 },
    PLS: { name: "Kangar", lat: 6.4414, lng: 100.1986 },
    PNG: { name: "George Town", lat: 5.4141, lng: 100.3288 },
    PRK: { name: "Ipoh", lat: 4.5975, lng: 101.0901 },
    SBH: { name: "Kota Kinabalu", lat: 5.9804, lng: 116.0735 },
    SGR: { name: "Shah Alam", lat: 3.0733, lng: 101.5185 },
    SWK: { name: "Kuching", lat: 1.5533, lng: 110.3592 },
    TRG: { name: "Kuala Terengganu", lat: 5.3302, lng: 103.1408 },
    WLY: { name: "Kuala Lumpur", lat: 3.139, lng: 101.6869 }
  };
  const LABUAN = { name: "Labuan", lat: 5.2831, lng: 115.2308 };

  const POINTS = ["north", "north-northeast", "northeast", "east-northeast", "east", "east-southeast",
    "southeast", "south-southeast", "south", "south-southwest", "southwest", "west-southwest",
    "west", "west-northwest", "northwest", "north-northwest"];

  const SVG_NS = "http://www.w3.org/2000/svg";
  const $ = function (id) { return document.getElementById(id); };
  const page = window.PrayerPage;

  // The device's own position, once it has shared one.
  let fix = null;
  let bearing = null;
  let eventName = null;
  let noCompassTimer = null;
  // The dial's heading as drawn: unwrapped, so easing never spins it the long way round.
  let shown = null;
  let aligned = false;

  /* -- Geometry -- */

  function qiblaBearing(lat, lng) {
    const r = Math.PI / 180;
    const p1 = lat * r;
    const p2 = KAABA.lat * r;
    const dl = (KAABA.lng - lng) * r;
    const y = Math.sin(dl) * Math.cos(p2);
    const x = Math.cos(p1) * Math.sin(p2) - Math.sin(p1) * Math.cos(p2) * Math.cos(dl);
    return (Math.atan2(y, x) / r + 360) % 360;
  }

  // -180 to 180: how far, and which way, to turn from `from` to face `to`.
  function turn(from, to) {
    return ((to - from) % 360 + 540) % 360 - 180;
  }

  function pointName(deg) {
    return POINTS[Math.round(deg / 22.5) % 16];
  }

  function onCircle(deg, radius) {
    const a = deg * Math.PI / 180;
    return { x: 100 + radius * Math.sin(a), y: 100 - radius * Math.cos(a) };
  }

  /* -- Where the bearing is taken from -- */

  function zonePoint() {
    const code = page.zone();
    if (code === "WLY02") return LABUAN;
    return STATE_POINTS[code.slice(0, 3)] || STATE_POINTS.WLY;
  }

  function setBearing() {
    const from = fix || zonePoint();
    bearing = qiblaBearing(from.lat, from.lng);
    drawKaaba();
    const deg = Math.round(bearing) + "°";
    $("qiblaBearing").textContent = fix
      ? "The Qibla is " + deg + " from north (" + pointName(bearing) + ") from where you are."
      : "The Qibla is " + deg + " from north (" + pointName(bearing) + ") from around " + from.name +
        ". Share your location for an exact bearing.";
    draw();
  }

  function locate() {
    if (fix || !("geolocation" in navigator)) return;
    navigator.geolocation.getCurrentPosition(function (pos) {
      fix = { lat: pos.coords.latitude, lng: pos.coords.longitude };
      setBearing();
    }, function () {
      // The state capital's bearing stands, and says so.
    }, { timeout: 15000, maximumAge: 600000 });
  }

  /* -- Dial -- */

  function svgEl(tag, attrs) {
    const el = document.createElementNS(SVG_NS, tag);
    Object.keys(attrs).forEach(function (k) { el.setAttribute(k, attrs[k]); });
    return el;
  }

  // The rose turns with the device; the pointer at the top stays where it is.
  function buildDial() {
    const rose = $("qiblaRose");
    rose.appendChild(svgEl("circle", { cx: 100, cy: 100, r: 86, class: "qibla-ring" }));
    for (let deg = 0; deg < 360; deg += 5) {
      const major = deg % 30 === 0;
      const a = onCircle(deg, 86);
      const b = onCircle(deg, major ? 76 : 81);
      rose.appendChild(svgEl("line", {
        x1: a.x, y1: a.y, x2: b.x, y2: b.y, class: major ? "qibla-tick major" : "qibla-tick"
      }));
    }
    ["N", "E", "S", "W"].forEach(function (letter, i) {
      const p = onCircle(i * 90, 64);
      const text = svgEl("text", {
        x: p.x, y: p.y, class: letter === "N" ? "qibla-letter north" : "qibla-letter",
        "text-anchor": "middle", "dominant-baseline": "central",
        transform: "rotate(" + i * 90 + " " + p.x + " " + p.y + ")"
      });
      text.textContent = letter;
      rose.appendChild(text);
    });
    rose.appendChild(svgEl("line", { id: "qiblaNeedle", x1: 100, y1: 100, x2: 100, y2: 100, class: "qibla-needle" }));
    // The Kaaba, drawn to sit upright when it lines up with the pointer.
    const kaaba = svgEl("g", { id: "qiblaKaaba", class: "qibla-kaaba" });
    kaaba.appendChild(svgEl("rect", { x: -9, y: -9, width: 18, height: 18, rx: 2, class: "qibla-kaaba-body" }));
    kaaba.appendChild(svgEl("rect", { x: -9, y: -4.5, width: 18, height: 3.5, class: "qibla-kaaba-band" }));
    rose.appendChild(kaaba);
    rose.appendChild(svgEl("circle", { cx: 100, cy: 100, r: 4, class: "qibla-hub" }));
  }

  function drawKaaba() {
    const tip = onCircle(bearing, 50);
    const at = onCircle(bearing, 64);
    const needle = $("qiblaNeedle");
    needle.setAttribute("x2", tip.x);
    needle.setAttribute("y2", tip.y);
    $("qiblaKaaba").setAttribute("transform", "translate(" + at.x + " " + at.y + ") rotate(" + bearing + ")");
  }

  function draw() {
    $("qiblaRose").setAttribute("transform", "rotate(" + -(shown || 0) + " 100 100)");
    if (shown === null || bearing === null) return;

    const diff = turn(shown, bearing);
    const nowAligned = Math.abs(diff) <= ALIGNED_DEG;
    $("qiblaDial").classList.toggle("aligned", nowAligned);
    if (nowAligned !== aligned) {
      aligned = nowAligned;
      if (aligned && navigator.vibrate) {
        try { navigator.vibrate(40); } catch (e) { /* not allowed here */ }
      }
    }
    // Only the turn's direction is announced; the degrees change too often to read out.
    setText("qiblaTurn", aligned ? "You're facing the Qibla" : diff > 0 ? "Turn right" : "Turn left");
    setText("qiblaDeg", aligned ? "" : Math.round(Math.abs(diff)) + "°");
  }

  function setText(id, text) {
    const el = $(id);
    if (el.textContent !== text) el.textContent = text;
  }

  /* -- Compass -- */

  function screenAngle() {
    if (screen.orientation && typeof screen.orientation.angle === "number") return screen.orientation.angle;
    return typeof window.orientation === "number" ? window.orientation : 0;
  }

  // Degrees clockwise from north that the top of the screen points, or null when
  // the reading isn't tied to north.
  function headingOf(e) {
    let h = null;
    if (typeof e.webkitCompassHeading === "number" && e.webkitCompassHeading >= 0) h = e.webkitCompassHeading;
    else if (e.absolute && typeof e.alpha === "number") h = 360 - e.alpha;
    if (h === null) return null;
    return (h + screenAngle() + 360) % 360;
  }

  function onOrientation(e) {
    const heading = headingOf(e);
    if (heading === null) return;
    if (noCompassTimer) {
      clearTimeout(noCompassTimer);
      noCompassTimer = null;
    }
    shown = shown === null ? heading : shown + turn(shown, heading) * SMOOTHING;

    // Rolled sideways, or stood upright, the heading wanders.
    const tilted = Math.abs(e.gamma || 0) > 35 || Math.abs(e.beta || 0) > 75;
    const unsure = typeof e.webkitCompassAccuracy === "number" &&
      (e.webkitCompassAccuracy < 0 || e.webkitCompassAccuracy > 25);
    note(tilted ? "Hold your phone flat, screen up, for a steady reading."
      : unsure ? "The compass is unsure of north. Move your phone in a figure-8 to calibrate it."
        : "Keep away from metal and magnets. If the reading seems off, move your phone in a figure-8.");
    draw();
  }

  function listen() {
    if (eventName) return;
    eventName = "ondeviceorientationabsolute" in window ? "deviceorientationabsolute" : "deviceorientation";
    window.addEventListener(eventName, onOrientation);
    noCompassTimer = setTimeout(noCompass, NO_COMPASS_MS);
  }

  function stop() {
    if (eventName) window.removeEventListener(eventName, onOrientation);
    eventName = null;
    clearTimeout(noCompassTimer);
    noCompassTimer = null;
  }

  function noCompass() {
    stop();
    shown = null;
    $("qiblaDial").classList.remove("aligned");
    draw();
    setText("qiblaTurn", "No compass found");
    setText("qiblaDeg", "");
    note("This device isn't sharing a compass reading, so the dial shows north at the top. " +
      "Use the bearing above with a compass, or open this page on your phone.");
  }

  function note(text) {
    setText("qiblaNote", text);
  }

  // Must run inside the tap: Safari only asks for motion access from a user gesture.
  function open() {
    if (window.UI) UI.openModal("qiblaModal");
    setBearing();
    locate();
    if (eventName) return;

    aligned = false;
    setText("qiblaTurn", "Finding north…");
    setText("qiblaDeg", "");
    note("");

    if (typeof DeviceOrientationEvent === "undefined") return noCompass();
    if (typeof DeviceOrientationEvent.requestPermission === "function") {
      DeviceOrientationEvent.requestPermission().then(function (state) {
        if (state === "granted") return listen();
        setText("qiblaTurn", "Compass access is off");
        note("Allow motion and orientation access for this site in Safari's settings, then open Kiblat again.");
      }).catch(function () { noCompass(); });
      return;
    }
    listen();
  }

  function init() {
    if (window.UI) UI.hydrateIcons($("qiblaModal"));
    buildDial();
    $("qiblaBtn").addEventListener("click", open);

    // However the modal closes (button, backdrop, Escape), the compass stops with it.
    new MutationObserver(function () {
      if ($("qiblaModal").classList.contains("hidden")) stop();
    }).observe($("qiblaModal"), { attributes: true, attributeFilter: ["class"] });

    document.addEventListener("prayer:zonechange", function () {
      if (!fix && !$("qiblaModal").classList.contains("hidden")) setBearing();
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
