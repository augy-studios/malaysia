/*
  Proxies the Rapid KL (Prasarana) GTFS-static feed from data.gov.my.
  The upstream endpoint (api.data.gov.my/gtfs-static/prasarana) 302-redirects
  to a real GTFS zip bundle (routes.txt, stops.txt, trips.txt, stop_times.txt,
  shapes.txt, calendar.txt, frequencies.txt, agency.txt - plus __MACOSX junk
  entries). routes.txt/stops.txt/trips.txt/stop_times.txt are all plain CSV
  (well under 100KB total for this feed), so we unzip and parse them on every
  request (no npm dependencies are configured in this project - ZIP central
  directory + DEFLATE are handled with Node's built-in zlib) and derive a
  route_id -> [stop_id] map from trips.txt + stop_times.txt so the client can
  show "which stations does this route serve" without a second round trip.
*/
const zlib = require('zlib');

const UPSTREAM_URL = 'https://api.data.gov.my/gtfs-static/prasarana?category=rapid-rail-kl';
const TIMEOUT_MS = 10000;

function unzipEntries(buffer, wantedNames) {
  const EOCD_SIG = 0x06054b50;
  const CD_SIG = 0x02014b50;
  const LFH_SIG = 0x04034b50;

  let eocdPos = -1;
  const searchStart = Math.max(0, buffer.length - 22 - 65557);
  for (let i = buffer.length - 22; i >= searchStart; i--) {
    if (buffer.readUInt32LE(i) === EOCD_SIG) {
      eocdPos = i;
      break;
    }
  }
  if (eocdPos === -1) throw new Error('EOCD record not found - not a valid zip');

  const totalEntries = buffer.readUInt16LE(eocdPos + 10);
  let pos = buffer.readUInt32LE(eocdPos + 16);
  const result = {};
  let found = 0;

  for (let i = 0; i < totalEntries && found < wantedNames.length; i++) {
    if (buffer.readUInt32LE(pos) !== CD_SIG) break;
    const compMethod = buffer.readUInt16LE(pos + 10);
    const compSize = buffer.readUInt32LE(pos + 20);
    const nameLen = buffer.readUInt16LE(pos + 28);
    const extraLen = buffer.readUInt16LE(pos + 30);
    const commentLen = buffer.readUInt16LE(pos + 32);
    const localHeaderOffset = buffer.readUInt32LE(pos + 42);
    const name = buffer.toString('utf8', pos + 46, pos + 46 + nameLen);

    if (wantedNames.includes(name)) {
      if (buffer.readUInt32LE(localHeaderOffset) === LFH_SIG) {
        const lhNameLen = buffer.readUInt16LE(localHeaderOffset + 26);
        const lhExtraLen = buffer.readUInt16LE(localHeaderOffset + 28);
        const dataStart = localHeaderOffset + 30 + lhNameLen + lhExtraLen;
        const compData = buffer.slice(dataStart, dataStart + compSize);
        let data;
        if (compMethod === 0) data = compData;
        else if (compMethod === 8) data = zlib.inflateRawSync(compData);
        else throw new Error('Unsupported zip compression method ' + compMethod);
        result[name] = data.toString('utf8');
        found++;
      }
    }
    pos += 46 + nameLen + extraLen + commentLen;
  }
  return result;
}

function parseCSV(text) {
  const rows = [];
  let row = [];
  let field = '';
  let inQuotes = false;

  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (inQuotes) {
      if (c === '"') {
        if (text[i + 1] === '"') { field += '"'; i++; }
        else inQuotes = false;
      } else {
        field += c;
      }
    } else if (c === '"') {
      inQuotes = true;
    } else if (c === ',') {
      row.push(field);
      field = '';
    } else if (c === '\r') {
      // skip, \n handles line end
    } else if (c === '\n') {
      row.push(field);
      rows.push(row);
      row = [];
      field = '';
    } else {
      field += c;
    }
  }
  if (field.length || row.length) {
    row.push(field);
    rows.push(row);
  }
  while (rows.length && rows[rows.length - 1].length === 1 && rows[rows.length - 1][0] === '') {
    rows.pop();
  }
  if (!rows.length) return [];
  const header = rows.shift().map(h => h.trim());
  return rows
    .filter(r => r.length === header.length)
    .map(r => {
      const obj = {};
      header.forEach((h, idx) => { obj[h] = r[idx]; });
      return obj;
    });
}

module.exports = async (req, res) => {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), TIMEOUT_MS);

  try {
    let upstream;
    try {
      upstream = await fetch(UPSTREAM_URL, { signal: controller.signal, redirect: 'follow' });
    } finally {
      clearTimeout(timeout);
    }

    if (upstream.status === 429) {
      res.status(429).json({ success: false, error: 'Upstream (data.gov.my) rate-limited this request. Try again shortly.' });
      return;
    }
    if (!upstream.ok) {
      res.status(502).json({ success: false, error: 'Upstream returned status ' + upstream.status });
      return;
    }

    const buffer = Buffer.from(await upstream.arrayBuffer());

    let entries;
    try {
      entries = unzipEntries(buffer, ['routes.txt', 'stops.txt', 'trips.txt', 'stop_times.txt']);
    } catch (zipErr) {
      res.status(502).json({ success: false, error: 'Could not read upstream GTFS bundle: ' + zipErr.message });
      return;
    }

    const routes = entries['routes.txt'] ? parseCSV(entries['routes.txt']) : [];
    const stops = entries['stops.txt'] ? parseCSV(entries['stops.txt']) : [];
    const trips = entries['trips.txt'] ? parseCSV(entries['trips.txt']) : [];
    const stopTimes = entries['stop_times.txt'] ? parseCSV(entries['stop_times.txt']) : [];

    const tripRoute = {};
    trips.forEach(t => { if (t.trip_id) tripRoute[t.trip_id] = t.route_id; });

    const routeStopSets = {};
    const stopScheduleSets = {}; // stop_id -> route_id -> Set(HH:MM:SS)
    stopTimes.forEach(st => {
      const routeId = tripRoute[st.trip_id];
      if (!routeId || !st.stop_id) return;
      if (!routeStopSets[routeId]) routeStopSets[routeId] = new Set();
      routeStopSets[routeId].add(st.stop_id);

      const time = st.departure_time || st.arrival_time;
      if (!time) return;
      if (!stopScheduleSets[st.stop_id]) stopScheduleSets[st.stop_id] = {};
      if (!stopScheduleSets[st.stop_id][routeId]) stopScheduleSets[st.stop_id][routeId] = new Set();
      stopScheduleSets[st.stop_id][routeId].add(time);
    });
    const routeStops = {};
    Object.keys(routeStopSets).forEach(id => { routeStops[id] = Array.from(routeStopSets[id]); });

    const stopSchedule = {};
    Object.keys(stopScheduleSets).forEach(stopId => {
      stopSchedule[stopId] = {};
      Object.keys(stopScheduleSets[stopId]).forEach(routeId => {
        stopSchedule[stopId][routeId] = Array.from(stopScheduleSets[stopId][routeId]).sort();
      });
    });

    res.setHeader('Cache-Control', 'public, s-maxage=86400, stale-while-revalidate=172800');
    res.status(200).json({
      success: true,
      source: UPSTREAM_URL,
      fetchedAt: new Date().toISOString(),
      counts: { routes: routes.length, stops: stops.length },
      routes,
      stops,
      routeStops,
      stopSchedule
    });
  } catch (err) {
    const isAbort = err && err.name === 'AbortError';
    res.status(isAbort ? 504 : 500).json({
      success: false,
      error: isAbort ? 'Request to upstream timed out.' : ((err && err.message) || 'Unknown error fetching Rapid KL feed.')
    });
  }
};
