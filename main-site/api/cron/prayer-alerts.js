// Run by Vercel Cron every minute (vercel.json). Sends the prayer alerts due this
// minute; see api/_push/notify.js for how it decides.

const { notifyDue, pushConfigured } = require('../_push/notify');
const { releaseLock, takeLock } = require('../_push/store');

// Shorter than the minute between runs. A run that holds it longer (only ever the
// one just after midnight, fetching the new day) makes the next one skip, and
// the run after that catches up.
const LOCK_SECONDS = 55;

module.exports = async (req, res) => {
  // Vercel Cron sends CRON_SECRET as a bearer token; nobody else can make the server send.
  const secret = process.env.CRON_SECRET;
  if (!secret || req.headers.authorization !== 'Bearer ' + secret) {
    return res.status(401).json({ error: 'unauthorized' });
  }
  if (!pushConfigured()) return res.status(200).json({ skipped: 'prayer alerts are not set up' });

  if (!(await takeLock('cron', LOCK_SECONDS))) {
    return res.status(200).json({ skipped: 'a run is already going' });
  }
  try {
    return res.status(200).json(await notifyDue(Date.now()));
  } catch (err) {
    console.error('prayer alerts failed:', err);
    return res.status(500).json({ error: 'prayer alerts failed' });
  } finally {
    await releaseLock('cron').catch(function () {});
  }
};
