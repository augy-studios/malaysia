/*
  Proxies the KTMB GTFS-realtime vehicle-position feed from data.gov.my.
  The upstream endpoint returns a raw protobuf-encoded GTFS-realtime
  FeedMessage (Content-Type: application/octet-stream, filename
  vehicle-position-ktmb.proto) - confirmed empirically, not JSON. There is no
  npm dependency available in this project (no gtfs-realtime-bindings /
  protobufjs), so this file includes a small, targeted protobuf decoder for
  exactly the well-known GTFS-realtime message shapes (FeedMessage,
  FeedHeader, FeedEntity, VehiclePosition, TripDescriptor, VehicleDescriptor,
  Position) and hands the client clean JSON.
*/

const UPSTREAM_URL = 'https://api.data.gov.my/gtfs-realtime/vehicle-position/ktmb';
const TIMEOUT_MS = 8000;

// -- minimal protobuf wire-format reader --

function readVarint(buf, pos) {
  let result = 0;
  let shift = 0;
  let b;
  do {
    b = buf[pos];
    pos++;
    result += (b & 0x7f) * Math.pow(2, shift);
    shift += 7;
  } while (b >= 0x80);
  return [result, pos];
}

// Parses a protobuf message into { fieldNumber: [rawValue, ...] }.
// wireType 0 (varint) -> Number, wireType 1/5 (fixed64/32) -> Buffer,
// wireType 2 (length-delimited) -> Buffer (string or nested message).
function parseMessage(buf) {
  const fields = {};
  let pos = 0;
  while (pos < buf.length) {
    const [tag, afterTag] = readVarint(buf, pos);
    pos = afterTag;
    const fieldNumber = tag >>> 3;
    const wireType = tag & 0x7;
    let value;
    if (wireType === 0) {
      const [v, afterV] = readVarint(buf, pos);
      value = v;
      pos = afterV;
    } else if (wireType === 1) {
      value = buf.slice(pos, pos + 8);
      pos += 8;
    } else if (wireType === 2) {
      const [len, afterLen] = readVarint(buf, pos);
      pos = afterLen;
      value = buf.slice(pos, pos + len);
      pos += len;
    } else if (wireType === 5) {
      value = buf.slice(pos, pos + 4);
      pos += 4;
    } else {
      throw new Error('Unsupported protobuf wire type ' + wireType);
    }
    if (!fields[fieldNumber]) fields[fieldNumber] = [];
    fields[fieldNumber].push(value);
  }
  return fields;
}

function getString(fields, n) {
  const v = fields[n] && fields[n][0];
  return v && Buffer.isBuffer(v) ? v.toString('utf8') : undefined;
}
function getVarint(fields, n) {
  const v = fields[n] && fields[n][0];
  return typeof v === 'number' ? v : undefined;
}
function getFloat32(fields, n) {
  const v = fields[n] && fields[n][0];
  return v && Buffer.isBuffer(v) && v.length >= 4 ? v.readFloatLE(0) : undefined;
}
function getSubmessages(fields, n) {
  return (fields[n] || []).map(buf => parseMessage(buf));
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

    const buf = Buffer.from(await upstream.arrayBuffer());

    let vehicles = [];
    let feedTimestamp = null;

    try {
      const root = parseMessage(buf);

      const headerArr = getSubmessages(root, 1);
      if (headerArr[0]) {
        const ts = getVarint(headerArr[0], 3);
        feedTimestamp = typeof ts === 'number' ? ts : null;
      }

      const entities = getSubmessages(root, 2);
      vehicles = entities
        .map(entity => {
          const vpArr = getSubmessages(entity, 4); // FeedEntity.vehicle
          if (!vpArr.length) return null;
          const vp = vpArr[0];

          const trip = getSubmessages(vp, 1)[0];   // VehiclePosition.trip
          const veh = getSubmessages(vp, 8)[0];    // VehiclePosition.vehicle
          const pos = getSubmessages(vp, 2)[0];    // VehiclePosition.position

          const lat = pos ? getFloat32(pos, 1) : undefined;
          const lon = pos ? getFloat32(pos, 2) : undefined;
          if (typeof lat !== 'number' || typeof lon !== 'number') return null;

          return {
            entityId: getString(entity, 1) || null,
            tripId: trip ? (getString(trip, 1) || null) : null,
            routeId: trip ? (getString(trip, 5) || null) : null,
            vehicleId: veh ? (getString(veh, 1) || null) : null,
            label: veh ? (getString(veh, 2) || null) : null,
            lat,
            lon,
            bearing: pos ? (getFloat32(pos, 3) ?? null) : null,
            speed: pos ? (getFloat32(pos, 5) ?? null) : null,
            timestamp: getVarint(vp, 5) ?? null
          };
        })
        .filter(Boolean);
    } catch (parseErr) {
      res.status(502).json({ success: false, error: 'Could not decode upstream GTFS-realtime feed: ' + parseErr.message });
      return;
    }

    res.setHeader('Cache-Control', 'public, s-maxage=20, stale-while-revalidate=40');
    res.status(200).json({
      success: true,
      source: UPSTREAM_URL,
      fetchedAt: new Date().toISOString(),
      feedTimestamp,
      count: vehicles.length,
      vehicles
    });
  } catch (err) {
    const isAbort = err && err.name === 'AbortError';
    res.status(isAbort ? 504 : 500).json({
      success: false,
      error: isAbort ? 'Request to upstream timed out.' : ((err && err.message) || 'Unknown error fetching KTMB live positions.')
    });
  }
};
