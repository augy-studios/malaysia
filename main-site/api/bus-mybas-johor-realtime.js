// Vercel serverless function - read-through proxy for myBAS Johor Bahru live
// vehicle positions via data.gov.my (GTFS-realtime protobuf). See
// bus-prasarana-realtime.js for the full rationale: the upstream returns a raw
// serialised GTFS-realtime FeedMessage (protobuf), confirmed by inspecting the
// bytes directly. We decode it by hand (no npm protobuf dependency available in
// this repo) into plain JSON for the client.

const UPSTREAM_TIMEOUT_MS = 9000;

// ---- minimal protobuf wire-format reader (varint / length-delimited / fixed32) ----

function readVarint(bytes, pos) {
  let result = 0;
  let shift = 0;
  let b;
  do {
    b = bytes[pos++];
    result += (b & 0x7f) * Math.pow(2, shift);
    shift += 7;
  } while (b & 0x80);
  return [result, pos];
}

function parseFields(bytes, start, end) {
  const fields = [];
  let pos = start;
  while (pos < end) {
    let tag;
    [tag, pos] = readVarint(bytes, pos);
    const fieldNum = tag >>> 3;
    const wireType = tag & 0x7;
    if (wireType === 0) {
      let val;
      [val, pos] = readVarint(bytes, pos);
      fields.push({ field: fieldNum, wire: 0, raw: val });
    } else if (wireType === 2) {
      let len;
      [len, pos] = readVarint(bytes, pos);
      fields.push({ field: fieldNum, wire: 2, raw: bytes.subarray(pos, pos + len) });
      pos += len;
    } else if (wireType === 5) {
      fields.push({ field: fieldNum, wire: 5, raw: bytes.subarray(pos, pos + 4) });
      pos += 4;
    } else if (wireType === 1) {
      fields.push({ field: fieldNum, wire: 1, raw: bytes.subarray(pos, pos + 8) });
      pos += 8;
    } else {
      throw new Error(`Unsupported protobuf wire type ${wireType} at byte ${pos}`);
    }
  }
  return fields;
}

function toStr(raw) {
  return Buffer.from(raw).toString("utf8");
}

function toFloat32(raw) {
  return Buffer.from(raw).readFloatLE(0);
}

function firstField(fields, num) {
  return fields.find((f) => f.field === num);
}

function decodeVehiclePosition(raw) {
  const fields = parseFields(raw, 0, raw.length);
  const out = {};

  const tripField = firstField(fields, 1);
  if (tripField) {
    const tripFields = parseFields(tripField.raw, 0, tripField.raw.length);
    const tripId = firstField(tripFields, 1);
    const startDate = firstField(tripFields, 3);
    const routeId = firstField(tripFields, 5);
    out.trip = {
      tripId: tripId ? toStr(tripId.raw) : null,
      startDate: startDate ? toStr(startDate.raw) : null,
      routeId: routeId ? toStr(routeId.raw) : null,
    };
  }

  const posField = firstField(fields, 2);
  if (posField) {
    const posFields = parseFields(posField.raw, 0, posField.raw.length);
    const lat = firstField(posFields, 1);
    const lon = firstField(posFields, 2);
    const bearing = firstField(posFields, 3);
    const speed = firstField(posFields, 5);
    out.position = {
      latitude: lat ? toFloat32(lat.raw) : null,
      longitude: lon ? toFloat32(lon.raw) : null,
      bearing: bearing ? toFloat32(bearing.raw) : null,
      speed: speed ? toFloat32(speed.raw) : null,
    };
  }

  const stopId = firstField(fields, 7);
  if (stopId) out.stopId = toStr(stopId.raw);

  const timestamp = firstField(fields, 5);
  out.timestamp = timestamp ? timestamp.raw : null;

  const vehicleDesc = firstField(fields, 8);
  if (vehicleDesc) {
    const vFields = parseFields(vehicleDesc.raw, 0, vehicleDesc.raw.length);
    const id = firstField(vFields, 1);
    const label = firstField(vFields, 2);
    const plate = firstField(vFields, 3);
    out.vehicle = {
      id: id ? toStr(id.raw) : null,
      label: label ? toStr(label.raw) : null,
      licensePlate: plate ? toStr(plate.raw) : null,
    };
  }

  return out;
}

function decodeFeedMessage(buffer) {
  const bytes = new Uint8Array(buffer);
  const fields = parseFields(bytes, 0, bytes.length);

  let headerTimestamp = null;
  let gtfsVersion = null;
  const vehicles = [];

  for (const f of fields) {
    if (f.field === 1 && f.wire === 2) {
      const hFields = parseFields(f.raw, 0, f.raw.length);
      const version = firstField(hFields, 1);
      const ts = firstField(hFields, 3);
      if (version) gtfsVersion = toStr(version.raw);
      if (ts) headerTimestamp = ts.raw;
    } else if (f.field === 2 && f.wire === 2) {
      const eFields = parseFields(f.raw, 0, f.raw.length);
      const idField = firstField(eFields, 1);
      const vehicleField = firstField(eFields, 4);
      if (vehicleField) {
        const vp = decodeVehiclePosition(vehicleField.raw);
        vehicles.push({
          entityId: idField ? toStr(idField.raw) : null,
          ...vp,
        });
      }
    }
  }

  return { gtfsVersion, headerTimestamp, vehicles };
}

module.exports = async (req, res) => {
  const upstreamUrl = "https://api.data.gov.my/gtfs-realtime/vehicle-position/mybas-johor/";
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), UPSTREAM_TIMEOUT_MS);

  try {
    const upstreamRes = await fetch(upstreamUrl, {
      redirect: "follow",
      signal: controller.signal,
    });

    clearTimeout(timeout);

    if (upstreamRes.status === 429) {
      res.status(429).json({
        success: false,
        error: "Upstream rate limit reached (data.gov.my). Please try again shortly.",
      });
      return;
    }

    if (!upstreamRes.ok) {
      res.status(502).json({
        success: false,
        error: `Upstream returned HTTP ${upstreamRes.status}`,
      });
      return;
    }

    const buffer = await upstreamRes.arrayBuffer();

    let decoded;
    try {
      decoded = decodeFeedMessage(buffer);
    } catch (decodeErr) {
      res.status(502).json({
        success: false,
        error: "Upstream GTFS-realtime feed could not be decoded (unexpected format).",
      });
      return;
    }

    res.setHeader("Cache-Control", "public, s-maxage=20, stale-while-revalidate=40");
    res.status(200).json({
      success: true,
      data: {
        agency: "mybas-johor",
        gtfsVersion: decoded.gtfsVersion,
        headerTimestamp: decoded.headerTimestamp,
        vehicles: decoded.vehicles,
      },
      fetchedAt: new Date().toISOString(),
    });
  } catch (err) {
    clearTimeout(timeout);
    const isAbort = err && (err.name === "AbortError" || err.code === "ABORT_ERR");
    res.status(isAbort ? 504 : 500).json({
      success: false,
      error: isAbort
        ? "Upstream request timed out."
        : "Failed to reach upstream myBAS Johor live position feed.",
    });
  }
};
