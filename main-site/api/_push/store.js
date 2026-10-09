// Where prayer alerts keep their state between function runs: each device's push
// subscription and choices, the day's times for every zone, the last minute the
// cron handled, a lock so two runs never overlap, and a per-IP request count.
// Upstash Redis, over its REST API with plain fetch. Follows sg-psi's
// api/_push/store.js.
//
// Connected from the Vercel Marketplace, Upstash sets KV_REST_API_URL and
// KV_REST_API_TOKEN; set up on Upstash directly, it's UPSTASH_REDIS_REST_URL and
// UPSTASH_REDIS_REST_TOKEN. Either pair works.

const REDIS_URL = process.env.KV_REST_API_URL || process.env.UPSTASH_REDIS_REST_URL;
const TOKEN = process.env.KV_REST_API_TOKEN || process.env.UPSTASH_REDIS_REST_TOKEN;

// Every key starts with this, so one store can serve several projects.
const KEY = 'myb:prayer';
const DEVICES = KEY + ':devices';
// One set of device ids per zone, so the cron reads only the devices in a zone
// with something due, never the whole list every minute.
const zoneKey = function (zone) { return KEY + ':zone:' + zone; };

const storeConfigured = function () { return Boolean(REDIS_URL && TOKEN); };

async function redis() {
  const command = Array.prototype.slice.call(arguments);
  const res = await fetch(REDIS_URL, {
    method: 'POST',
    headers: { Authorization: 'Bearer ' + TOKEN, 'Content-Type': 'application/json' },
    body: JSON.stringify(command.map(String)),
    cache: 'no-store'
  });
  const data = await res.json().catch(function () { return null; });
  if (!res.ok || (data && data.error)) {
    throw new Error('redis ' + command[0] + ' failed: ' + ((data && data.error) || res.status));
  }
  return data.result;
}

const parse = function (value) { return value == null ? null : JSON.parse(value); };

// ---------- devices: { subscription, zone, prayers, early, updatedAt } by device id ----------

const devices = {
  async has(id) {
    return (await redis('HEXISTS', DEVICES, id)) === 1;
  },
  async get(id) {
    return parse(await redis('HGET', DEVICES, id));
  },
  async set(id, device) {
    const before = await this.get(id);
    await redis('HSET', DEVICES, id, JSON.stringify(device));
    await redis('SADD', zoneKey(device.zone), id);
    if (before && before.zone !== device.zone) await redis('SREM', zoneKey(before.zone), id);
  },
  async delete(id) {
    const before = await this.get(id);
    await redis('HDEL', DEVICES, id);
    if (before) await redis('SREM', zoneKey(before.zone), id);
  },
  async size() {
    return Number(await redis('HLEN', DEVICES)) || 0;
  },
  // Every device in a zone, as [id, device] pairs.
  async inZone(zone) {
    const ids = (await redis('SMEMBERS', zoneKey(zone))) || [];
    if (!ids.length) return [];
    const values = await redis.apply(null, ['HMGET', DEVICES].concat(ids));
    const out = [];
    ids.forEach(function (id, i) {
      const device = parse(values[i]);
      if (device) out.push([id, device]);
    });
    return out;
  }
};

// ---------- small JSON values: the day's times, the last minute handled ----------

async function getState(name) {
  return parse(await redis('GET', KEY + ':state:' + name));
}

async function setState(name, value, ttlSeconds) {
  if (value == null) await redis('DEL', KEY + ':state:' + name);
  else if (ttlSeconds) await redis('SET', KEY + ':state:' + name, JSON.stringify(value), 'EX', ttlSeconds);
  else await redis('SET', KEY + ':state:' + name, JSON.stringify(value));
}

// ---------- a lock, so a slow run and the next one can't both send ----------

async function takeLock(name, seconds) {
  return (await redis('SET', KEY + ':lock:' + name, '1', 'NX', 'EX', seconds)) === 'OK';
}

async function releaseLock(name) {
  await redis('DEL', KEY + ':lock:' + name);
}

// ---------- requests per IP per minute ----------

async function rateLimited(ip, limit) {
  const key = KEY + ':rate:' + ip;
  const n = Number(await redis('INCR', key));
  if (n === 1) await redis('EXPIRE', key, 60);
  return n > limit;
}

module.exports = {
  storeConfigured,
  devices,
  getState,
  setState,
  takeLock,
  releaseLock,
  rateLimited
};
