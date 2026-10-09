// Checks what the page sends before it is stored. Anything that fails throws a
// ValidationError, which the server turns into a 400. The subscription checks are
// sg-psi's api/_push/validate.js.

const solat = require('../_lib/solat');

// Minutes before each chosen time for the heads-up; 0 is none.
const EARLY_MINUTES = [0, 5, 10, 15];

// The server POSTs to whatever endpoint a subscription names, so only the browsers'
// own push services are accepted. Otherwise anyone could point it at an arbitrary URL.
const PUSH_HOSTS = [
  'fcm.googleapis.com', // Chrome, Edge on Android, Samsung Internet, Opera
  '.push.services.mozilla.com', // Firefox
  'web.push.apple.com', // Safari, and iOS home screen apps
  '.notify.windows.com' // Edge on Windows
];

const ID_RE = /^[A-Za-z0-9-]{8,64}$/;
const B64URL_RE = /^[A-Za-z0-9_-]+=*$/;

class ValidationError extends Error {}

function fail(message) {
  throw new ValidationError(message);
}

function isId(value) {
  return typeof value === 'string' && ID_RE.test(value);
}

// { subscription, zone, prayers, early }: where, which times, and how far ahead.
// `zones` is the list of JAKIM zone codes the zone must be one of.
function parseDevice(body, zones) {
  if (!body || typeof body !== 'object') fail('body must be an object');
  if (typeof body.zone !== 'string' || !zones.includes(body.zone)) fail('zone must be a JAKIM zone code');

  if (!Array.isArray(body.prayers) || !body.prayers.length) fail('prayers must list at least one time');
  const prayers = solat.PRAYER_KEYS.filter(function (k) { return body.prayers.includes(k); });
  if (prayers.length !== new Set(body.prayers).size) fail('prayers must be from ' + solat.PRAYER_KEYS.join(', '));

  if (!EARLY_MINUTES.includes(body.early)) fail('early must be one of ' + EARLY_MINUTES.join(', '));

  return { subscription: parseSubscription(body.subscription), zone: body.zone, prayers: prayers, early: body.early };
}

function parseSubscription(sub) {
  if (!sub || typeof sub !== 'object') fail('subscription is required');

  let url;
  try {
    url = new URL(sub.endpoint);
  } catch (e) {
    fail('subscription.endpoint must be a URL');
  }
  if (url.protocol !== 'https:') fail('subscription.endpoint must be https');
  const host = url.hostname;
  const known = PUSH_HOSTS.some(function (h) { return h.startsWith('.') ? host.endsWith(h) : host === h; });
  if (!known) fail('subscription.endpoint is not a known push service');

  const keys = sub.keys || {};
  const p256dh = keys.p256dh;
  const auth = keys.auth;
  if (typeof p256dh !== 'string' || p256dh.length > 200 || !B64URL_RE.test(p256dh)) fail('bad subscription.keys.p256dh');
  if (typeof auth !== 'string' || auth.length > 100 || !B64URL_RE.test(auth)) fail('bad subscription.keys.auth');

  return { endpoint: url.href, keys: { p256dh: p256dh, auth: auth } };
}

module.exports = { EARLY_MINUTES, ValidationError, isId, parseDevice };
