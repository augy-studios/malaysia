# Malaysia Boleh

An open-source, installable PWA by [Augy Studios](https://uwuapps.org) that puts live Malaysian public data in one place - weather, earthquakes, floods, and public transit - built entirely on free, keyless feeds from [data.gov.my](https://data.gov.my).

Live at: **<https://malaysia.uwuapps.org>**

## Why this exists

Malaysia's government publishes a good amount of real-time open data (MET Malaysia, JPS, Prasarana, KTMB, myBAS) via `data.gov.my`, but there's no single friendly place to actually look at it. This project is a small, static, no-login dashboard that proxies those feeds through cached serverless functions and renders them as plain, readable pages - no accounts, no tracking beyond basic analytics, no paywalls.

## What's in this repo

All of the actual site code lives in [`main-site/`](main-site/README.md) - see that README for the page list, tech stack, and design system. In short: static HTML/CSS/JS deployed on Vercel, with a handful of serverless functions in `main-site/api/` that proxy and cache each upstream government feed, and a service worker that makes the whole thing installable and partially usable offline.

There is no backend database - every page is a stateless, cached read of a public feed.

## Contributing

Issues and pull requests are welcome. This project follows the [Contributor Covenant Code of Conduct](CODE_OF_CONDUCT.md) - please read it before participating.

If you want to add a new data source or page, follow the existing pattern in `main-site/`: one folder per page (`index.html` + `<page>.css` + `<page>.js`), one serverless function per upstream feed in `main-site/api/`, and reuse the shared design system in `main-site/css/` and `main-site/js/` rather than introducing new styling or icon conventions.

## License

[MIT](LICENSE) © Augy Studios
