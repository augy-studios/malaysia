# Malaysia Boleh

A free installable PWA by **UwU Apps** that puts live Malaysian public data in one place, built entirely on government feeds from [data.gov.my](https://data.gov.my).

Live at: **<https://malaysia.uwuapps.org>**

## What's here

| Page             | Data                                                          | Upstream source   |
| ---------------- | -------------------------------------------------------------- | ----------------- |
| `/`              | Directory of every page                                        | -                 |
| `/weather/`      | 7-day forecast + active weather warnings                       | MET Malaysia      |
| `/quake/`        | Live earthquake warnings                                       | MET Malaysia      |
| `/flood-alerts/` | River water levels & flood warnings                             | JPS               |
| `/trains/`       | Rapid KL (LRT/MRT/monorail) + KTMB schedules & live positions   | Prasarana / KTMB  |
| `/bus/`          | Rapid KL/Penang/Kuantan + myBAS Johor Bahru schedules & live positions | Prasarana / myBAS |

Every page fetches its data from this site's own `/api/*` serverless functions rather than calling data.gov.my directly, so responses can be cached at the edge and shielded from upstream downtime, rate limits, or schema changes.

## Stack

- Static HTML/CSS/JS, no build step, no framework.
- Deployed on **Vercel** (`vercel.json`: Singapore region, clean URLs).
- Serverless functions in `api/*.js` proxy each upstream feed with `Cache-Control` (edge caching), request timeouts, and defensive error handling.
- No database - every page is a stateless read-through cache of a public feed, so **Supabase is not used**.
- Installable PWA (`manifest.json` + `sw.js`): the service worker precaches the app shell and each page, and uses network-first for `/api/*` so live data stays fresh while the shell works offline.

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
  weather/ quake/ flood-alerts/ trains/ bus/
                  one folder per page: index.html + <page>.css + <page>.js
  index.html      homepage / directory
  sw.js           service worker (precache + caching strategies)
  manifest.json   PWA manifest
  sitemap.xml, robots.txt, llms.txt
```

## Local development

No build step - just serve the `main-site/` directory statically. To exercise the `/api/*` serverless functions locally, use the [Vercel CLI](https://vercel.com/docs/cli):

```sh
npm i -g vercel
vercel dev
```
