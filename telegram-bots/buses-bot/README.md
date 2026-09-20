# Malaysia Buses Bot

A Telegram bot that puts Malaysian bus timetables and live bus positions into a chat. It covers **Rapid Bus KL**, **Rapid Bus Penang**, **Rapid Bus MRT Feeder** and **myBAS Johor**, reading the open GTFS feeds published on [data.gov.my](https://data.gov.my).

This is the companion bot to the bus page on [malaysia.uwuapps.org](https://malaysia.uwuapps.org/bus/), part of the [Malaysia Boleh](https://github.com/augystudios/malaysia) project.

Running as [@malaysiabuses_bot](https://t.me/malaysiabuses_bot).

---

## What it does

- **Search without a command.** Send a stop or route name in chat and the search runs on its own. There is no command to remember.
- **Find what is close by.** Share your location and the nearest stops come back sorted by distance, each one tappable.
- **See the timetable.** Every stop shows the next departures per route, with next-day buses marked clearly.
- **Watch buses move.** Live vehicle positions come from the GTFS-realtime feeds, each with a map link.
- **Follow one bus.** Pick a route, pick a departure, and see the whole journey stop by stop.
- **Get told before the bus.** Save the stops you use and the bot messages you ahead of each departure.

All times are Malaysia time (UTC+8).

---

## Commands

| Command | What it does |
| --- | --- |
| `/start` | What the bot is, every command, and links to the web app and donations |
| `/fav` | Show your favourites, or search for something to add |
| `/unfav` | Remove a favourite |
| `/sub` | Turn notifications on |
| `/unsub` | Turn every notification off at once |
| `/settings` | Operator, clock format, lead time, quiet hours, digest time |
| `/live` | Live bus positions for an operator |
| `/routes` | Browse routes, or `/routes 780` to jump straight to one |
| `/stops` | How to find a stop |
| `/trip` | Follow a single bus stop by stop |

`/stats` exists for the operator and is restricted to the ids in `ADMIN_USER_IDS`.

You do not need most of these. Sending a stop name or your location covers the common cases.

---

## Notifications

Turn these on in `/sub`. The first two follow your favourites, so save a stop first.

| Notification | What arrives |
| --- | --- |
| **Departure reminders** | A message before each scheduled bus at a favourite stop. The lead time is yours to set, from 5 to 45 minutes. |
| **Live bus alerts** | A ping when a bus on a favourited route comes within 1.5 km of that stop. One alert per bus, with a 20 minute cooldown. |
| **Daily digest** | One summary each morning of the day's departures at your favourite stops. |
| **Service notices** | A note when a feed stops updating, which is the closest thing these feeds offer to a disruption alert. |

Quiet hours suppress every notification overnight and are on by default, from 23:00 to 06:00.

A word on what is possible: the operators publish timetables and vehicle positions, but no official service alerts or delay data. Departure reminders therefore come from the **scheduled** timetable, not from a live prediction of where the bus actually is. Use the live alerts when you want to know where the bus really is.

---

## Requirements

- Python 3.11 or newer (Debian 13 ships 3.13, which is fine)
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- An API id and hash from [my.telegram.org](https://my.telegram.org)

No database server is needed. Everything persists in a single SQLite file.

---

## Setup

Telegram-side configuration, including the description, about text and command list, is covered separately in [SETUP.md](SETUP.md). Do that part first if the bot is new.

```bash
git clone https://github.com/augystudios/malaysia.git
cd malaysia/telegram-bots/buses-bot

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

cp .env.example .env
nano .env          # fill in the four required values
```

The four values you must set:

```ini
TELEGRAM_API_ID=1234567
TELEGRAM_API_HASH=your_api_hash
TELEGRAM_BOT_TOKEN=123456789:AA...
DONATION_URL=https://your-donation-link
```

`SUPABASE_URL` and `SUPABASE_SERVICE_KEY` are accepted but unused. This bot stores everything in SQLite, so leave them blank unless you are wiring up something of your own.

Then start it:

```bash
.venv/bin/python -m bot.main
```

Run it from the project root, the directory holding `.env`, since that is where the bot looks for its configuration and its `data/` folder.

On first run the bot downloads roughly 12 MB of GTFS bundles and caches them. That takes a few seconds per operator. It happens in the background, so the bot answers commands immediately and search improves as the feeds land.

Stop it with `Ctrl+C`. Shutdown is handled cleanly: the scheduler loops are cancelled, pending work stays in SQLite, and the next start picks it up.

---

## Running under tmux

Start a tmux session so the bot outlives your SSH connection.

```bash
tmux new -s buses                        # start a named session
cd ~/malaysia/telegram-bots/buses-bot
.venv/bin/python -m bot.main
# Ctrl+B then D to detach and leave it running
```

Coming back later:

```bash
tmux attach -t buses       # reattach
tmux ls                    # list sessions
```

| Keys | What they do |
| --- | --- |
| `Ctrl+B` then `D` | Detach, leaving the bot running |
| `Ctrl+B` then `[` | Scroll back through the log, `q` to exit |
| `Ctrl+C` | Stop the bot (while attached) |

Nothing restarts the bot automatically, which keeps failures visible: if it exits, the reason is sitting in the tmux scrollback rather than buried under a wrapper looping over the same error.

Being offline for a while costs nothing. Pending notifications live in SQLite rather than in memory, so the next start resumes from where it stopped. Reminders whose moment passed while the bot was down are dropped rather than delivered late.

---

## How it works

```text
bot/
├── config.py      Environment loading and validation
├── database.py    SQLite: users, favourites, subscriptions, jobs, buttons, cache
├── gtfs.py        data.gov.my feeds: zip/CSV parsing and protobuf decoding
├── richtext.py    sendRichMessage with an automatic fallback
├── timeutils.py   Malaysia time, GTFS times past midnight, quiet hours
├── views.py       Every user-facing message
├── scheduler.py   The notification loops
└── main.py        Telethon handlers and startup
```

A few decisions worth explaining:

**Rich messages with a fallback.** Messages are sent with [`sendRichMessage`](https://core.telegram.org/bots/api#sendrichmessage), which gives real tables, headings and collapsible sections. That method only exists on the HTTP Bot API, while Telethon speaks MTProto, so the bot uses both: Telethon for the event loop, and a small HTTP call for sending. If Telegram ever rejects a rich message, the same content is downgraded to classic HTML and sent through Telethon instead. The bot remembers the rejection and stops retrying. Nothing breaks, and the user sees plain formatting rather than an error.

**Buttons that never expire.** Telegram allows 64 bytes of callback data, which is not enough for an operator, a stop id and a route id. Instead every button's real payload is written to the `callbacks` table and only a short token travels in the message. Because the row is on disk rather than in memory, a keyboard tapped weeks after a restart still works. Identical buttons reuse their token, so the table does not grow without bound.

**Scheduling in SQLite.** Pending notifications are rows in `scheduled_jobs`, not entries in an in-process timer. A tick loop claims whatever is due. Restart the bot, reboot the VPS, and nothing is lost. Every job carries a dedupe key, so a planner that runs twice cannot send you the same reminder twice.

**Talking to data.gov.my directly.** The website proxies these feeds through Vercel functions, but the bot goes to the source so it does not depend on the site being up. That means reimplementing the GTFS zip parsing and the GTFS-realtime protobuf decoding in Python. The zip side is short, since Python has `zipfile` and `csv` built in. The protobuf side is a small hand-written wire-format reader, ported from the same approach used in `main-site/api/`, which avoids pulling in a protobuf dependency for four field types.

**Staying online when upstream is not.** Feed responses are cached in SQLite. When data.gov.my fails or rate limits, the bot serves the last good copy rather than showing nothing, and records the failure for the service-notice subscribers.

---

## Tests

```bash
.venv/bin/python -m pytest tests/ -q
```

The suite covers GTFS times past midnight, quiet hours that wrap midnight, the zip parser, the protobuf decoder, the rich-to-classic downgrade, and the persistence guarantees for buttons and scheduled jobs. It needs no network access and no Telegram connection.

---

## Configuration reference

Everything below `DONATION_URL` is optional and has a working default.

| Variable | Default | Purpose |
| --- | --- | --- |
| `TELEGRAM_API_ID` | required | From my.telegram.org |
| `TELEGRAM_API_HASH` | required | From my.telegram.org |
| `TELEGRAM_BOT_TOKEN` | required | From @BotFather |
| `DONATION_URL` | required | The donation button on `/start` |
| `SUPABASE_URL` | empty | Accepted, unused |
| `SUPABASE_SERVICE_KEY` | empty | Accepted, unused |
| `WEB_APP_URL` | the bus page | The web app button on `/start` |
| `DATA_DIR` | `./data` | Where the database and session live |
| `STATIC_REFRESH_HOURS` | `24` | How often timetables are re-downloaded |
| `REALTIME_POLL_SECONDS` | `60` | Live position polling for alert subscribers |
| `SCHEDULER_TICK_SECONDS` | `30` | How often due notifications are checked |
| `HTTP_TIMEOUT_SECONDS` | `30` | Upstream request timeout |
| `NEARBY_RADIUS_METRES` | `1200` | Radius for a shared location |
| `ADMIN_USER_IDS` | empty | Comma-separated ids allowed to run `/stats` |

---

## Privacy

The bot stores your Telegram user id, your favourites, your notification settings, and the last location you shared, which is used once to find nearby stops. Nothing is sold, shared or sent anywhere beyond the requests to data.gov.my that fetch the timetables.

`/settings` has a **Delete my data** button that removes all of it.

---

## Data and licence

Timetables and live positions come from [data.gov.my](https://data.gov.my) under the terms published there. The operators are Prasarana Malaysia and Causeway Link.

Bot code is MIT licensed, in line with the rest of the repository.

Live positions depend on each operator broadcasting them. A bus with no position is usually one that is not reporting rather than one that is missing, and coverage outside service hours is thin.
