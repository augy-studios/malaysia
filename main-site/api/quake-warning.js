// Vercel serverless function - read-through proxy for MET Malaysia earthquake
// warnings via data.gov.my. No auth, no database - just fetch, cache briefly,
// and pass a normalised { success, data, fetchedAt } envelope to the client.

const UPSTREAM_URL = "https://api.data.gov.my/weather/warning/earthquake/";
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

    let payload;
    try {
      payload = await upstreamRes.json();
    } catch (parseErr) {
      res.status(502).json({
        success: false,
        error: "Upstream returned an unparseable response.",
      });
      return;
    }

    if (!Array.isArray(payload)) {
      res.status(502).json({
        success: false,
        error: "Upstream response was not in the expected array shape.",
      });
      return;
    }

    res.setHeader(
      "Cache-Control",
      "public, s-maxage=60, stale-while-revalidate=120"
    );
    res.status(200).json({
      success: true,
      data: payload,
      fetchedAt: new Date().toISOString(),
    });
  } catch (err) {
    clearTimeout(timeout);
    const isAbort = err && (err.name === "AbortError" || err.code === "ABORT_ERR");
    res.status(isAbort ? 504 : 500).json({
      success: false,
      error: isAbort
        ? "Upstream request timed out."
        : "Failed to reach upstream earthquake warning feed.",
    });
  }
};
