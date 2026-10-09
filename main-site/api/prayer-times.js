// Vercel serverless function - one JAKIM zone's prayer times for a month, via
// api.waktusolat.app.
//   GET /api/prayer-times?zone=WLY01&month=2026-10
//
// A whole month rather than "today", so the URL alone decides the answer and
// the edge can hold it for a day without serving yesterday's times after
// midnight. The page picks today out of it, and asks for next month too on the
// last day, for tomorrow's Subuh.

const solat = require('./_lib/solat');

const MONTH_RE = /^(\d{4})-(\d{2})$/;

module.exports = async (req, res) => {
  const zone = String(req.query.zone || '').toUpperCase();
  const match = MONTH_RE.exec(String(req.query.month || ''));
  const year = match ? Number(match[1]) : 0;
  const monthNumber = match ? Number(match[2]) : 0;

  if (!solat.ZONE_RE.test(zone)) {
    res.status(400).json({ success: false, error: 'zone must be a JAKIM zone code, e.g. WLY01.' });
    return;
  }
  if (!match || year < 2020 || year > 2100 || monthNumber < 1 || monthNumber > 12) {
    res.status(400).json({ success: false, error: 'month must be YYYY-MM.' });
    return;
  }

  try {
    const days = await solat.month(zone, year, monthNumber);
    // JAKIM publishes the year ahead and seldom revises it.
    res.setHeader('Cache-Control', 'public, s-maxage=86400, stale-while-revalidate=604800');
    res.status(200).json({
      success: true,
      data: { zone: zone, month: match[0], days: days },
      fetchedAt: new Date().toISOString()
    });
  } catch (err) {
    const status = err instanceof solat.UpstreamError ? (err.status === 504 ? 504 : 502) : 500;
    res.status(status).json({
      success: false,
      error: 'Failed to fetch prayer times: ' + (err && err.message ? err.message : 'unknown error')
    });
  }
};
