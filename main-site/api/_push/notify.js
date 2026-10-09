// Prayer alerts: each minute, works out which zones have a time due (or a time
// 5, 10 or 15 minutes away, for early reminders) and pushes to the devices there
// that asked for it. Called by the prayer alerts cron (api/cron/prayer-alerts.js).
// Follows sg-psi's api/_push/notify.js.
//
// The day's times for all 60 zones are fetched once, just after midnight, and kept
// in Redis, so a minute with nothing due costs a single read.

const webpush = require('web-push');
const solat = require('../_lib/solat');
const store = require('./store');

// A push not delivered within this long is dropped: "Time for Asar" ten minutes
// on is still worth having, but no later.
const AT_TIME_TTL = 10 * 60;
// A test that turns up long after the button was pressed only confuses.
const TEST_TTL = 5 * 60;
// Sends in flight at once, so a long device list doesn't open thousands of connections.
const SEND_BATCH = 50;
// Months fetched at once when building the day.
const FETCH_BATCH = 6;
// A zone that failed to fetch is tried again after this long, not every minute.
const RETRY_MS = 10 * 60 * 1000;
// Minutes a late run goes back for. Vercel Cron can start a run late or skip
// one; an alert is still worth sending a few minutes late, not an hour.
const CATCH_UP_MINUTES = 5;
const EARLY_STEPS = [5, 10, 15];

const LABELS = Object.fromEntries(solat.PRAYERS.map(function (p) { return [p.key, p.label]; }));

function pushConfigured() {
  const env = process.env;
  return store.storeConfigured() && Boolean(env.VAPID_PUBLIC_KEY && env.VAPID_PRIVATE_KEY && env.VAPID_SUBJECT);
}

function useVapid() {
  webpush.setVapidDetails(process.env.VAPID_SUBJECT, process.env.VAPID_PUBLIC_KEY, process.env.VAPID_PRIVATE_KEY);
}

// ---------- the day's times, every zone ----------

async function inBatches(items, size, fn) {
  const out = [];
  for (let i = 0; i < items.length; i += size) {
    out.push.apply(out, await Promise.all(items.slice(i, i + size).map(fn)));
  }
  return out;
}

// { date, zones: { WLY01: { label, times } }, missing: [codes], triedAt }
// "*" in missing means the zone list itself couldn't be fetched.
async function loadDay(date, nowMs) {
  const name = 'day:' + date;
  let day = await store.getState(name);
  if (day && !day.missing.length) return day;
  if (day && nowMs - day.triedAt < RETRY_MS) return day;

  day = day || { date: date, zones: {}, missing: ['*'], triedAt: 0 };
  let zones;
  try {
    zones = await solat.zones();
  } catch (err) {
    console.warn('prayer alerts: zone list failed:', err.message);
    day.triedAt = nowMs;
    await store.setState(name, day, 2 * 86400);
    return day;
  }

  const year = Number(date.slice(0, 4));
  const monthNumber = Number(date.slice(5, 7));
  const todo = zones.filter(function (z) { return !day.zones[z.code]; });
  const results = await inBatches(todo, FETCH_BATCH, async function (z) {
    try {
      const entry = (await solat.month(z.code, year, monthNumber)).find(function (d) { return d.date === date; });
      return entry ? [z, entry] : [z, null];
    } catch (err) {
      console.warn('prayer alerts: ' + z.code + ' ' + date + ' failed:', err.message);
      return [z, null];
    }
  });

  day.missing = [];
  for (const [z, entry] of results) {
    if (entry) day.zones[z.code] = { label: z.districts, times: entry.times };
    else day.missing.push(z.code);
  }
  day.triedAt = nowMs;
  await store.setState(name, day, 2 * 86400);
  return day;
}

// What falls on one minute in one zone: [{ key, early }], at-time first.
function dueAt(times, minute) {
  const out = [];
  for (const key of solat.PRAYER_KEYS) {
    if (times[key] === minute) out.push({ key: key, early: 0 });
  }
  for (const early of EARLY_STEPS) {
    for (const key of solat.PRAYER_KEYS) {
      if (times[key] - early * 60 === minute) out.push({ key: key, early: early });
    }
  }
  return out;
}

function message(event, zone) {
  const label = LABELS[event.key];
  const at = solat.mytTime(zone.times[event.key]);
  const where = zone.label;
  const subuh = solat.mytTime(zone.times.fajr);

  if (event.early) {
    const title = label + ' in ' + event.early + ' minutes';
    if (event.key === 'imsak') return { title: title, body: 'At ' + at + ' in ' + where + '. Subuh is at ' + subuh + '.' };
    if (event.key === 'syuruk') return { title: title, body: 'Subuh time ends at ' + at + ' in ' + where + '.' };
    return { title: title, body: 'At ' + at + ' in ' + where + '.' };
  }
  if (event.key === 'imsak') return { title: 'Imsak', body: at + ' in ' + where + '. Subuh is at ' + subuh + '.' };
  if (event.key === 'syuruk') return { title: 'Syuruk', body: 'Sunrise at ' + at + ' in ' + where + '. Subuh time has ended.' };
  return { title: 'Time for ' + label, body: at + ' in ' + where + '.' };
}

// The minutes this run is for: everything since the last run, back at most
// CATCH_UP_MINUTES. The last minute is recorded before sending, so a run that
// fails halfway never sends anything twice.
async function claimMinutes(nowMs) {
  const now = Math.floor(nowMs / 60000) * 60;
  const last = await store.getState('lastMinute');
  const from = Math.max(Number.isInteger(last) ? last + 60 : now, now - CATCH_UP_MINUTES * 60);
  if (from > now) return { now: now, minutes: [] };
  await store.setState('lastMinute', now);
  const minutes = [];
  for (let m = from; m <= now; m += 60) minutes.push(m);
  return { now: now, minutes: minutes };
}

async function notifyDue(nowMs) {
  const claimed = await claimMinutes(nowMs);
  if (!claimed.minutes.length) return { skipped: 'minute already handled' };

  const days = {};
  // zone -> [{ minute, key, early }]
  const due = {};
  for (const minute of claimed.minutes) {
    const date = solat.mytDate(minute * 1000);
    if (!days[date]) days[date] = await loadDay(date, nowMs);
    for (const [code, zone] of Object.entries(days[date].zones)) {
      for (const event of dueAt(zone.times, minute)) {
        // A reminder that turns up late says the wrong thing; a late "time for" doesn't.
        if (event.early && minute < claimed.now) continue;
        (due[code] = due[code] || []).push(Object.assign({ minute: minute, zone: zone }, event));
      }
    }
  }

  const missing = Object.values(days).reduce(function (n, d) { return n + d.missing.length; }, 0);
  const zones = Object.keys(due);
  if (!zones.length) return { minutes: claimed.minutes.length, due: 0, missing: missing };

  useVapid();

  const sends = [];
  for (const code of zones) {
    const payloads = {};
    for (const [id, device] of await store.devices.inZone(code)) {
      const sentFor = {};
      for (const event of due[code]) {
        if (!device.prayers.includes(event.key)) continue;
        if (event.early && device.early !== event.early) continue;
        // One per device per minute. Imsak falls exactly 10 minutes before
        // Subuh, so "Imsak" and "Subuh in 10 minutes" land together; the
        // at-time one, listed first, wins.
        if (sentFor[event.minute]) continue;
        sentFor[event.minute] = true;

        const key = event.minute + '|' + event.key + '|' + event.early;
        payloads[key] = payloads[key] || JSON.stringify(Object.assign(
          { type: 'prayer-alert', url: '/prayer/' },
          message(event, event.zone)
        ));
        const ttl = event.early ? event.early * 60 : AT_TIME_TTL;
        sends.push([id, device, payloads[key], { TTL: ttl, urgency: 'high' }]);
      }
    }
  }

  let sent = 0;
  let dropped = 0;
  for (let i = 0; i < sends.length; i += SEND_BATCH) {
    const results = await Promise.all(sends.slice(i, i + SEND_BATCH).map(function (s) { return send(s[0], s[1], s[2], s[3]); }));
    sent += results.filter(function (r) { return r === 'sent'; }).length;
    dropped += results.filter(function (r) { return r === 'dropped'; }).length;
  }
  return { minutes: claimed.minutes.length, due: zones, sent: sent, dropped: dropped, missing: missing };
}

// One push to one device, through the same keys, payload shape and service worker
// handler as a real alert, so it proves the whole path. "sent", "dropped" or "failed".
async function notifyTest(id, device) {
  useVapid();
  const zone = (await solat.zones()).find(function (z) { return z.code === device.zone; });
  const which = device.prayers.map(function (k) { return LABELS[k]; });
  const list = which.length > 1 ? which.slice(0, -1).join(', ') + ' and ' + which[which.length - 1] : which[0];
  const early = device.early ? ', and ' + device.early + ' minutes before each' : '';
  const payload = JSON.stringify({
    type: 'prayer-alert',
    title: 'Test alert from Malaysia Boleh',
    body: 'Alerts work on this device. You\'ll hear at ' + list + early + ', in ' + (zone ? zone.districts : device.zone) + '.',
    url: '/prayer/'
  });
  return send(id, device, payload, { TTL: TEST_TTL, urgency: 'high' });
}

async function send(id, device, payload, options) {
  try {
    await webpush.sendNotification(device.subscription, payload, options);
    return 'sent';
  } catch (err) {
    // 404 and 410 mean the subscription is gone for good: permission revoked, app
    // uninstalled, or site data cleared.
    if (err.statusCode === 404 || err.statusCode === 410) {
      await store.devices.delete(id).catch(function () {});
      console.info('dropped expired subscription ' + id);
      return 'dropped';
    }
    console.error('push to ' + id + ' failed:', err.statusCode || '', err.body || err.message);
    return 'failed';
  }
}

module.exports = { pushConfigured, notifyDue, notifyTest, dueAt, message };
