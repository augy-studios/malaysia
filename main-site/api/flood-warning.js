// Vercel serverless function - read-through proxy for data.gov.my flood-warning feed.
// No auth, no database. Fetches server-side so we can set our own cache headers
// and shield the client from upstream slowness / rate limits.

const UPSTREAM_URL = "https://api.data.gov.my/flood-warning/";
const UPSTREAM_TIMEOUT_MS = 8000;

module.exports = async (req, res) => {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), UPSTREAM_TIMEOUT_MS);

  try {
    const upstreamRes = await fetch(UPSTREAM_URL, {
      signal: controller.signal,
      headers: { accept: "application/json" },
    });

    clearTimeout(timeout);

    if (upstreamRes.status === 429) {
      res.status(429).json({
        success: false,
        error: "Upstream data.gov.my rate limit hit. Please try again shortly.",
      });
      return;
    }

    if (!upstreamRes.ok) {
      res.status(502).json({
        success: false,
        error: `Upstream returned status ${upstreamRes.status}`,
      });
      return;
    }

    let data;
    try {
      data = await upstreamRes.json();
    } catch (parseErr) {
      res.status(502).json({
        success: false,
        error: "Upstream response was not valid JSON.",
      });
      return;
    }

    if (!Array.isArray(data)) {
      res.status(502).json({
        success: false,
        error: "Upstream response was not in the expected array shape.",
      });
      return;
    }

    res.setHeader(
      "Cache-Control",
      "public, s-maxage=180, stale-while-revalidate=300"
    );
    res.status(200).json({
      success: true,
      count: data.length,
      updated_at: new Date().toISOString(),
      data,
    });
  } catch (err) {
    clearTimeout(timeout);
    const isAbort = err && (err.name === "AbortError" || err.code === "ABORT_ERR");
    res.status(isAbort ? 504 : 500).json({
      success: false,
      error: isAbort
        ? "Upstream request timed out."
        : "Failed to fetch flood warning data.",
    });
  }
};
