// Vercel serverless function - JAKIM prayer time zones, via api.waktusolat.app.
//   GET /api/prayer-zones                    every zone: [{ code, state, districts }]
//   GET /api/prayer-zones?lat=3.1&lng=101.7  the zone that point falls in: { zone }

const solat = require('./_lib/solat');

// Generously around Malaysia, so a typo'd or foreign point never reaches upstream.
const BOUNDS = { latMin: 0, latMax: 8, lngMin: 98, lngMax: 120 };

function coord(value, min, max) {
  const n = Number(value);
  return Number.isFinite(n) && n >= min && n <= max ? Math.round(n * 10000) / 10000 : null;
}

module.exports = async (req, res) => {
  try {
    if (req.query.lat !== undefined || req.query.lng !== undefined) {
      const lat = coord(req.query.lat, BOUNDS.latMin, BOUNDS.latMax);
      const lng = coord(req.query.lng, BOUNDS.lngMin, BOUNDS.lngMax);
      if (lat === null || lng === null) {
        res.status(400).json({ success: false, error: 'That location is outside Malaysia.' });
        return;
      }
      const zone = await solat.zoneAt(lat, lng);
      if (!zone) {
        res.status(404).json({ success: false, error: 'No JAKIM zone covers that location.' });
        return;
      }
      res.setHeader('Cache-Control', 'public, s-maxage=604800, stale-while-revalidate=2592000');
      res.status(200).json({ success: true, data: { zone: zone }, fetchedAt: new Date().toISOString() });
      return;
    }

    const zones = await solat.zones();
    res.setHeader('Cache-Control', 'public, s-maxage=604800, stale-while-revalidate=2592000');
    res.status(200).json({ success: true, data: zones, fetchedAt: new Date().toISOString() });
  } catch (err) {
    const status = err instanceof solat.UpstreamError ? (err.status === 504 ? 504 : 502) : 500;
    res.status(status).json({
      success: false,
      error: 'Failed to fetch prayer zones: ' + (err && err.message ? err.message : 'unknown error')
    });
  }
};
