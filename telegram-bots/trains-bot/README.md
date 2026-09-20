# Malaysia Trains bot

A Telegram bot for Malaysian rail. It covers the LRT, MRT, Monorail and BRT
lines run by Rapid KL, together with the KTM Komuter, ETS and intercity
services run by KTMB, and answers with timetables, full train journeys and
live KTMB positions.

Live at [@malaysiatrains_bot](https://t.me/malaysiatrains_bot). It is the rail
sibling of the buses bot in this repository and shares its structure.

All data comes from the open feeds published on
[data.gov.my](https://data.gov.my). The bot calls `api.data.gov.my` directly
and does not depend on the website or any proxy in front of it.

## What it does

- **Search by typing.** Send a station or line name and the search runs
  straight away, with no command needed.
- **Search by location.** Share your location and the nearest stations come
  back sorted by distance.
- **Real timetables.** Departures are filtered to the services that actually
  run on the day you are asking about, including public holiday swaps.
- **Full journeys.** Pick any train and read every station it calls at, with
  times.
- **Live positions.** KTMB broadcasts vehicle positions, shown with map links
  and how recently each train reported.
- **Notifications.** Reminders before your train, proximity alerts, a morning
  digest and a notice when a feed goes stale.

## Commands

| Command | What it does |
| --- | --- |
| `/start` | Overview, the full command list, and buttons to the web app and donation link |
| `/next` | Next departures from your home station |
| `/stations` | Find a station and read its timetable |
| `/lines` | Browse every line and the stations it serves |
| `/train` | Follow one train stop by stop |
| `/live` | Live KTMB train positions with map links |
| `/fav` | Add a station to favourites |
| `/unfav` | Remove something from favourites |
| `/sub` | Turn on reminders, alerts and digests |
| `/unsub` | Turn all notifications back off |
| `/settings` | Home station, preferred operator, clock format, lead time, quiet hours, digest time |

There is no `/help`, because `/start` already lists everything. `/stats` exists
for administrators listed in `ADMIN_USER_IDS` and is hidden from everyone else.

Most of the bot is reached without typing a command at all: send a name, share
a location, then use the inline buttons.

## Notifications

Turn these on individually from `/sub`.

| Kind | What arrives |
| --- | --- |
| Departure reminders | A message a set number of minutes before each scheduled train at your favourite stations |
| Live train alerts | A ping when a KTMB train comes within 2.5 km of a favourite station |
| Daily digest | One summary each morning covering the day at your favourite stations |
| Service notices | A note when a feed stops updating |

Reminders and live alerts follow your favourites, so add at least one station
first. Live alerts only cover KTMB, because Rapid KL does not broadcast train
positions. Quiet hours suppress everything overnight and are on by default.

## How the data is handled

Two quirks in the upstream feeds shape most of this bot, and both are worth
knowing if you plan to change the code.

**Rapid KL publishes headways, not a timetable.** Its feed contains only 48
template trips plus a `frequencies.txt` giving the interval for each part of
the day. Read literally, a station would appear to see six trains a day. The
bot expands those headways into real departures at parse time, which turns one
template into roughly 250 departures and produces the timetable a rider
expects. Times on Rapid KL lines are therefore close estimates derived from the
published frequency. KTMB publishes exact times and is passed through as is.

**Services do not run every day.** Both feeds ship a `calendar.txt`, and KTMB
also ships `calendar_dates.txt` holding public holiday exceptions. Every
departure the bot shows is filtered by whether its service actually runs on the
day in question, so a Sunday timetable never appears on a Tuesday.

One further trap, in case you touch the parser: in the Rapid KL feed,
`stop_times.txt` has its own `route_id` column that holds the route's *short
name* (`AGL`) rather than its id (`AG`). Joining on it matches nothing. The
only reliable path from a stop time to a route is through `trips.txt`.

## Rich messages

Every message the bot sends is a
[Rich Message](https://core.telegram.org/bots/features#rich-messages), which is
what allows the headings, tables and collapsible sections you see in the
replies.

`sendRichMessage` only exists on the HTTP Bot API, while Telethon speaks
MTProto. A bot token is valid on both at once, so `bot/richtext.py` builds Rich
HTML, posts it to the HTTP API, and falls back to classic HTML through Telethon
if Telegram rejects it. Every send and edit in the bot goes through that one
module, so no handler can bypass either the rich path or its fallback. Run
`/stats` as an administrator to see which path is currently in use.

## Buttons that never expire

A Telegram callback payload is capped at 64 bytes, which is not enough to carry
an operator, a station id and a line id together. Instead every button stores
its real payload as a row in SQLite and puts only a short token in the callback
data.

Because those rows outlive the process, a keyboard stays live indefinitely. A
button tapped weeks after a restart still resolves and still works. Nothing is
held in memory, so a restart loses no state.

## Scheduling

Pending notifications live in the `scheduled_jobs` table, not in an in-process
timer. A single loop wakes on a fixed tick, claims whatever is due and runs it.
If the bot is restarted or the VPS reboots, nothing is dropped: the rows are
still there and the next tick picks them up. Jobs carry a natural dedupe key,
so a planner that runs twice cannot produce two identical reminders.

## Requirements

- Python 3.11 or newer (Debian 13 ships 3.13, which is what this was built
  against)
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- An API id and hash from [my.telegram.org](https://my.telegram.org)

## Install

```bash
git clone <your-repo-url>
cd telegram-bots/trains-bot

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

cp .env.example .env
$EDITOR .env
```

See [SETUP.md](SETUP.md) for the full BotFather walkthrough, including the
description, about text and command list to paste in.

## Configuration

| Variable | Required | Purpose |
| --- | --- | --- |
| `TELEGRAM_API_ID` | Yes | Numeric API id from my.telegram.org |
| `TELEGRAM_API_HASH` | Yes | API hash from my.telegram.org |
| `TELEGRAM_BOT_TOKEN` | Yes | Bot token from BotFather |
| `DONATION_URL` | Yes | Target of the support button on `/start` |
| `SUPABASE_URL` | No | Unused. The bot stores everything in SQLite |
| `SUPABASE_SERVICE_KEY` | No | Unused, as above |
| `WEB_APP_URL` | No | Web app button target, defaults to the trains page |
| `DATA_DIR` | No | Where the database and session live, defaults to `./data` |
| `STATIC_REFRESH_HOURS` | No | How often schedules are re-downloaded, default 24 |
| `REALTIME_POLL_SECONDS` | No | Live position poll interval, default 60 |
| `SCHEDULER_TICK_SECONDS` | No | Notification check interval, default 30 |
| `HTTP_TIMEOUT_SECONDS` | No | Upstream timeout, default 30 |
| `NEARBY_RADIUS_METRES` | No | Radius for shared locations, default 2000 |
| `ADMIN_USER_IDS` | No | Comma-separated ids allowed to run `/stats` |

Supabase is listed because the wider project uses it. This bot does not: all
state is in one SQLite file, so leave both Supabase values blank.

## Running it in tmux

The bot is started by hand in a tmux session and keeps running after you
disconnect.

```bash
tmux new -s trains

cd ~/malaysia/telegram-bots/trains-bot
.venv/bin/python -m bot.main
```

Detach with `Ctrl+B` then `D`. The bot keeps running.

```bash
tmux attach -t trains    # come back to it
tmux ls                  # list sessions
```

Stop it with `Ctrl+C` while attached. Shutdown is graceful: the scheduler
stops, connections close, and queued jobs stay in the database for next time.

To keep a log while still watching the output:

```bash
.venv/bin/python -m bot.main 2>&1 | tee -a trains-bot.log
```

`*.log` is already in `.gitignore`.

### First run

The first start downloads and parses both schedule bundles, which takes a few
seconds. This happens in the background, so the bot answers `/start`
immediately and the timetable commands become useful once the logs show both
feeds loaded:

```
Loaded Rapid KL Rail: 187 stations, 8 lines
Loaded KTMB: 156 stations, 9 lines
```

Raw feeds are cached in SQLite afterwards, so later restarts are quick and a
temporary data.gov.my outage falls back to the last good copy.

## Tests

```bash
.venv/bin/python -m pytest
```

The suite runs offline. It builds miniature GTFS bundles in memory rather than
calling data.gov.my, and covers the things most likely to break quietly:
frequency expansion, service calendars and holiday exceptions, the trips.txt
join trap, callback tokens surviving a restart, the classic-HTML fallback, and
opening a database written by an older build.

## Layout

```
trains-bot/
├── bot/
│   ├── config.py      Environment loading and validation
│   ├── database.py    SQLite: users, favourites, jobs, callback registry
│   ├── gtfs.py        Feed fetching, GTFS parsing, protobuf decoding
│   ├── main.py        Entry point, commands, callback dispatch
│   ├── richtext.py    Rich message building and sending
│   ├── scheduler.py   Background loops for notifications
│   ├── timeutils.py   Malaysia time, GTFS time, quiet hours
│   └── views.py       Every user-facing message
├── tests/
├── .env.example
├── requirements.txt
├── README.md
└── SETUP.md
```

## Troubleshooting

**The bot starts but never replies.** Check that `TELEGRAM_BOT_TOKEN` belongs
to the same bot you are messaging, and that privacy mode is off if you are
testing in a group. The log line `Signed in as @...` confirms the token works.

**Replies arrive as plain text with no tables or headings.** Telegram declined
the rich path and the bot fell back to classic HTML, which is expected
behaviour rather than a failure. `/stats` shows which path is active.

**`Could not preload ...` on startup.** data.gov.my rate limits aggressively.
The bot retries on the next request and serves the cached copy meanwhile.

**A station shows very few departures.** Check the day. Services are filtered
by the published calendar, so a weekday-only line shows nothing on a Sunday.

**Buttons say they are no longer available.** That message means the callback
row was not found, which normally only happens if the SQLite file was replaced
or deleted. Send `/start` to get a fresh keyboard.

## Licence

Part of the [malaysia](https://github.com/) project and covered by the licence
at the repository root.
