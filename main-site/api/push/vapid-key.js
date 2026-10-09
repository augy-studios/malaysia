// The public half of the VAPID keys, which the page needs to subscribe to Web Push.

module.exports = (req, res) => {
  const publicKey = process.env.VAPID_PUBLIC_KEY;
  if (!publicKey) {
    res.status(503).json({ error: 'Prayer alerts are not set up: VAPID_PUBLIC_KEY is missing' });
    return;
  }
  res.setHeader('Cache-Control', 'public, max-age=3600');
  res.status(200).json({ publicKey: publicKey });
};
