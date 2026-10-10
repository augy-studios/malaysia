# Malaysia Boleh

A free installable PWA by **UwU Apps** that puts live Malaysian public data in one place, built on government feeds from [data.gov.my](https://data.gov.my) and JAKIM.

Live at: **<https://malaysia.uwuapps.org>**

## What's here

| Page | Data | Upstream source |
| --- | --- | --- |
| `/` | Directory of every page | - |
| `/weather/` | 7-day forecast + active weather warnings | MET Malaysia |
| `/quake/` | Live earthquake warnings | MET Malaysia |
| `/flood-alerts/` | River water levels & flood warnings | JPS |
| `/trains/` | Rapid KL (LRT/MRT/monorail) + KTMB schedules & live positions | Prasarana / KTMB |
| `/bus/` | Rapid KL/Penang/Kuantan + myBAS Johor Bahru schedules & live positions | Prasarana / myBAS |
| `/prayer/` | Prayer times for all 60 JAKIM zones + push notifications + Qibla compass | JAKIM e-Solat, via [api.waktusolat.app](https://api.waktusolat.app) |

Every page fetches its data from this site's own `/api/*` serverless functions rather than calling upstream directly, so responses can be cached at the edge and shielded from upstream downtime, rate limits, or schema changes.

## Stack

- Static HTML/CSS/JS, no build step, no framework.
- Deployed on **Vercel** (`vercel.json`: Singapore region, clean URLs, one cron).
- Serverless functions in `api/*.js` proxy each upstream feed with `Cache-Control` (edge caching), request timeouts, and defensive error handling.
- No database for the pages - each is a stateless read-through cache of a public feed, so **Supabase is not used**. Prayer alerts alone keep state, in Upstash Redis.
- Installable PWA (`manifest.json` + `sw.js`): the service worker precaches the app shell and each page, uses network-first for `/api/*` so live data stays fresh while the shell works offline, and shows prayer alert notifications.

## Design system

- Font: **Jua** everywhere.
- Glassmorphism styling only - static/flat theme colours, no gradients, orbs, or blobs in backgrounds.
- Seven selectable accent themes (all light, picked via the palette button in the top bar): Classic, Not green 1-5, and Really really light green. See `css/themes.css`.
- Shared CSS in `css/base.css`, shared theme/modal/service-worker logic in `js/theme.js`, shared inline-SVG icon set in `js/icons.js` (the site uses no emoji anywhere - every icon is an SVG).

## Structure

```text
main-site/
  css/            shared base styles + theme palettes
  js/             shared icon set + theme picker/service worker registration
  api/            Vercel serverless functions (one per upstream feed)
    prayer-zones.js, prayer-times.js   JAKIM zones and a zone's month of times
    push/vapid-key.js                  public VAPID key for subscribing
    push/devices/[id].js               turn a device's alerts on, change them, off, or test them
    cron/prayer-alerts.js              every minute: send the prayer alerts due
    _lib/solat.js                      the waktusolat.app client
    _push/                             Upstash store, validation, the alert rules
  weather/ quake/ flood-alerts/ trains/ bus/ prayer/
                  one folder per page: index.html + <page>.css + <page>.js
  index.html      homepage / directory
  sw.js           service worker (precache, caching strategies, push)
  manifest.json   PWA manifest
  sitemap.xml, robots.txt, llms.txt
  .env.example    every environment variable, explained
```

## Prayer alerts

A notification at each prayer the person picks (Imsak, Subuh, Syuruk, Zohor,
Asar, Maghrib, Isyak), optionally with a reminder 5, 10 or 15 minutes before,
for the JAKIM zone picked on the page. Follows sg-psi's PSI alerts.

### Setting it up

Every variable is in [`.env.example`](.env.example). Set them in the Vercel project
under Settings, Environment Variables, for Production (and Preview if wanted).
Variables reach only deployments made after they are added, so redeploy afterwards.

1. **Cron.** `CRON_SECRET`, any long random string (`openssl rand -hex 32`). Vercel
   sends it to `/api/cron/prayer-alerts` every minute (`vercel.json`). The
   every-minute schedule needs the Pro plan.
2. **Upstash.** In the Vercel project, Storage, Create Database, Upstash, Redis, and
   connect it without a prefix. That sets `KV_REST_API_URL` and `KV_REST_API_TOKEN`.
   Every key this site writes starts with `myb:`, so the store sg-psi uses (keys
   starting `sgpsi:`) can serve this site too.
3. **VAPID keys.** Make them once, and keep them: new keys mean every device has to
   turn alerts off and on again.

   ```sh
   cd main-site
   npm install
   npx web-push generate-vapid-keys
   ```

   Set `VAPID_PUBLIC_KEY`, `VAPID_PRIVATE_KEY` (mark it Sensitive) and
   `VAPID_SUBJECT` (`mailto:` an address push services can reach).

### Checking it

1. `/api/push/vapid-key` shows your public key (a `503` names what is missing).
2. In Settings, Cron Jobs, press **Run** on `/api/cron/prayer-alerts` and open its
   logs. The first run of each day fetches every zone's times; `"missing": 0`
   means all 60 arrived. A zone that failed is tried again 10 minutes later.
3. On a phone, open `/prayer/`, pick a zone, open **Alerts**, pick times and turn
   alerts on. On iPhone and iPad, only from the site added to the Home Screen.
   **Send test alert** pushes one straight away, by the same path: if it errors,
   the note names what is missing; if it says sent but nothing shows, the phone is
   holding notifications back.

### How it decides

Each minute, the cron reads the day's times for all 60 zones from Redis (fetched
once from waktusolat.app just after midnight) and finds the zones with a time due
now, or due in 5, 10 or 15 minutes. Only those zones' devices are read. A run that
starts late, or follows a skipped one, catches up on the last 5 minutes; a late
early reminder is skipped rather than sent with the wrong countdown. Imsak always
falls 10 minutes before Subuh, so "Imsak" and "Subuh in 10 minutes" would land
together: the device gets just the Imsak one.

Redis holds each device's push subscription, zone, times and reminder, under a
random ID the browser made up. No names or locations. Turning alerts off deletes
the device.

## Local development

No build step - just serve the `main-site/` directory statically. To exercise the `/api/*` serverless functions locally, use the [Vercel CLI](https://vercel.com/docs/cli):

```sh
npm i -g vercel
npm install
vercel dev
```

`vercel env pull .env.local` brings the variables down. Without any, every page
still works; only prayer alerts need them.
