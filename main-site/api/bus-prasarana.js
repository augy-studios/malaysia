// Vercel serverless function - read-through proxy for Prasarana bus GTFS static
// feeds via data.gov.my. The upstream endpoint does NOT return JSON - it 302s to
// an S3-hosted GTFS zip bundle (stops/routes/trips/shapes CSVs), which is too
// large and time/memory-costly to unzip + parse inside a serverless function on
// every request. Instead we HEAD the upstream (following the redirect) to grab
// the real download URL, size and last-modified date, and hand the client a
// small JSON manifest so the page can offer a direct "download GTFS feed" link
// instead of faking parsed route/stop data.

const CATEGORIES = ["rapid-bus-kl", "rapid-bus-penang", "rapid-bus-mrtfeeder"];
const UPSTREAM_TIMEOUT_MS = 9000;

module.exports = async (req, res) => {
  const category = String((req.query && req.query.category) || "rapid-bus-kl");

  if (!CATEGORIES.includes(category)) {
    res.status(400).json({
      success: false,
      error: `Invalid category. Expected one of: ${CATEGORIES.join(", ")}`,
    });
    return;
  }

  const upstreamUrl = `https://api.data.gov.my/gtfs-static/prasarana/?category=${encodeURIComponent(category)}`;
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), UPSTREAM_TIMEOUT_MS);

  try {
    const upstreamRes = await fetch(upstreamUrl, {
      method: "HEAD",
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

    const downloadUrl = upstreamRes.url || upstreamUrl;
    const contentLength = upstreamRes.headers.get("content-length");
    const lastModified = upstreamRes.headers.get("last-modified");

    res.setHeader(
      "Cache-Control",
      "public, s-maxage=86400, stale-while-revalidate=172800"
    );
    res.status(200).json({
      success: true,
      data: {
        category,
        format: "gtfs-zip",
        downloadUrl,
        sizeBytes: contentLength ? Number(contentLength) : null,
        lastModified: lastModified || null,
        categories: CATEGORIES,
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
        : "Failed to reach upstream Prasarana GTFS feed.",
    });
  }
};
