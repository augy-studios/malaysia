// Vercel serverless function - proxies MET Malaysia active weather warnings
// from data.gov.my, with server-side caching so we don't hammer upstream.

const UPSTREAM_URL = 'https://api.data.gov.my/weather/warning/';
const TIMEOUT_MS = 8000;

module.exports = async (req, res) => {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), TIMEOUT_MS);

  try {
    const upstreamRes = await fetch(UPSTREAM_URL, {
      signal: controller.signal,
      headers: { accept: 'application/json' }
    });

    clearTimeout(timeout);

    if (upstreamRes.status === 429) {
      res.status(429).json({
        success: false,
        error: 'Upstream rate limit reached (data.gov.my). Please try again shortly.'
      });
      return;
    }

    if (!upstreamRes.ok) {
      res.status(502).json({
        success: false,
        error: 'Upstream weather warning API returned status ' + upstreamRes.status
      });
      return;
    }

    const data = await upstreamRes.json();

    if (!Array.isArray(data)) {
      res.status(502).json({
        success: false,
        error: 'Upstream weather warning API returned an unexpected shape.'
      });
      return;
    }

    res.setHeader('Cache-Control', 'public, s-maxage=120, stale-while-revalidate=300');
    res.status(200).json({ success: true, data: data, fetchedAt: new Date().toISOString() });
  } catch (err) {
    clearTimeout(timeout);
    const isAbort = err && (err.name === 'AbortError');
    res.status(isAbort ? 504 : 500).json({
      success: false,
      error: isAbort ? 'Upstream weather warning API timed out.' : 'Failed to fetch weather warnings: ' + (err && err.message ? err.message : 'unknown error')
    });
  }
};
