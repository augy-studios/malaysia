// JAKIM e-Solat prayer times, through api.waktusolat.app (an open source mirror
// that needs no key). Shared by the page's endpoints and the prayer alerts cron.
// The Discord bot reads the same source, so the two always agree.
//
// Times come per JAKIM zone, a month at a time, as Unix seconds on whole minutes.

const ZONES_URL = 'https://api.waktusolat.app/zones';
const MONTH_URL = 'https://api.waktusolat.app/v2/solat/';
const TIMEOUT_MS = 8000;

// What the page and alerts show, in the order of the day. Dhuha is in the feed
// but left out: it is a window, not a time anybody is called to.
const PRAYERS = [
  { key: 'imsak', label: 'Imsak' },
  { key: 'fajr', label: 'Subuh' },
  { key: 'syuruk', label: 'Syuruk' },
  { key: 'dhuhr', label: 'Zohor' },
  { key: 'asr', label: 'Asar' },
  { key: 'maghrib', label: 'Maghrib' },
  { key: 'isha', label: 'Isyak' }
];
const PRAYER_KEYS = PRAYERS.map(function (p) { return p.key; });

const ZONE_RE = /^[A-Z]{3}\d{2}$/;
const MYT_OFFSET_MS = 8 * 3600 * 1000;

class UpstreamError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

async function getJson(url) {
  const controller = new AbortController();
  const timeout = setTimeout(function () { controller.abort(); }, TIMEOUT_MS);
  try {
    const res = await fetch(url, { signal: controller.signal, headers: { accept: 'application/json' } });
    if (!res.ok) throw new UpstreamError('waktusolat.app returned status ' + res.status, res.status);
    return await res.json();
  } catch (err) {
    if (err && err.name === 'AbortError') throw new UpstreamError('waktusolat.app timed out', 504);
    throw err;
  } finally {
    clearTimeout(timeout);
  }
}

// [{ code, state, districts }], in JAKIM's order (by state, then zone number).
// Kept for the life of the function instance: zones change once in years.
let zonesMemo = null;

async function zones() {
  if (zonesMemo) return zonesMemo;
  const data = await getJson(ZONES_URL);
  const out = (Array.isArray(data) ? data : [])
    .filter(function (z) { return z && ZONE_RE.test(z.jakimCode); })
    .map(function (z) { return { code: z.jakimCode, state: String(z.negeri || ''), districts: String(z.daerah || '') }; });
  if (!out.length) throw new UpstreamError('waktusolat.app returned no zones', 502);
  zonesMemo = out;
  return out;
}

// The zone a point falls in, or null outside Malaysia.
async function zoneAt(lat, lng) {
  const data = await getJson(ZONES_URL + '/' + lat + '/' + lng);
  return data && ZONE_RE.test(data.zone) ? data.zone : null;
}

// One zone's month: [{ date: 'YYYY-MM-DD', hijri, times: { imsak: unixSeconds, ... } }].
async function month(zone, year, monthNumber) {
  const data = await getJson(MONTH_URL + zone + '?year=' + year + '&month=' + monthNumber);
  const mm = String(monthNumber).padStart(2, '0');
  const days = [];
  for (const row of (data && Array.isArray(data.prayers) ? data.prayers : [])) {
    if (!row || !Number.isInteger(row.day)) continue;
    const times = {};
    for (const key of PRAYER_KEYS) {
      if (typeof row[key] === 'number') times[key] = row[key];
    }
    if (Object.keys(times).length !== PRAYER_KEYS.length) continue;
    days.push({ date: year + '-' + mm + '-' + String(row.day).padStart(2, '0'), hijri: String(row.hijri || ''), times: times });
  }
  if (!days.length) throw new UpstreamError('waktusolat.app returned no prayer times for ' + zone, 502);
  return days;
}

// Malaysia has no daylight saving, so MYT is always UTC+8.
function mytDate(ms) {
  return new Date(ms + MYT_OFFSET_MS).toISOString().slice(0, 10);
}

function mytTime(seconds) {
  return new Date(seconds * 1000)
    .toLocaleTimeString('en-MY', { timeZone: 'Asia/Kuala_Lumpur', hour: 'numeric', minute: '2-digit', hour12: true })
    .toLowerCase();
}

module.exports = {
  PRAYERS,
  PRAYER_KEYS,
  ZONE_RE,
  UpstreamError,
  zones,
  zoneAt,
  month,
  mytDate,
  mytTime
};
