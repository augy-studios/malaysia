// Vercel serverless function - read-through proxy for myBAS Johor Bahru GTFS
// static feed via data.gov.my. Same situation as bus-prasarana.js: the upstream
// 302s to an S3-hosted GTFS zip bundle rather than returning JSON. We HEAD the
// upstream (following the redirect) and hand the client a small JSON manifest
// with the real download URL/size/last-modified instead of trying to unzip and
// parse the full GTFS bundle inside a serverless function.

const UPSTREAM_TIMEOUT_MS = 9000;

module.exports = async (req, res) => {
  const upstreamUrl = "https://api.data.gov.my/gtfs-static/mybas-johor/";
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
        agency: "mybas-johor",
        format: "gtfs-zip",
        downloadUrl,
        sizeBytes: contentLength ? Number(contentLength) : null,
        lastModified: lastModified || null,
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
        : "Failed to reach upstream myBAS Johor GTFS feed.",
    });
  }
};
