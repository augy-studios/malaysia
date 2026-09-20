# Malaysia Weather Bot

A Telegram bot that carries official Malaysian weather forecasts, MET warnings,
earthquake bulletins and river levels, taken straight from the open
[data.gov.my](https://data.gov.my) feeds.

Live as [@malaysiaweather_bot](https://t.me/malaysiaweather_bot).

> This bot is an unofficial convenience and is not an emergency service. During
> a flood, a storm or an earthquake, follow the instructions of the local
> authorities and the official agency channels rather than relying on a
> Telegram message.

## What it does

- **Seven day forecasts** for 442 towns and districts, with morning, afternoon
  and night detail, temperature ranges and a plain answer to whether you need
  an umbrella.
- **Active MET warnings**, including thunderstorm, strong wind and rough sea
  warnings, with their validity period and official instructions.
- **Earthquake bulletins** for Malaysia and the surrounding region, with
  magnitude, depth and distance.
- **River levels** from 1276 flood warning gauges, showing which are at alert,
  warning or danger level and whether the water is rising.
- **Alerts** that reach you when something changes, scoped to the places you
  care about.
- **A morning digest** summarising your saved areas at a time you choose.

## Commands

| Command | What it does |
| --- | --- |
| `/start` | What the bot is, every command, and links to the web app and donations |
| `/weather` | Seven day forecast. `/weather Ipoh` goes straight there |
| `/warnings` | Weather warnings currently in force |
| `/quake` | Recent earthquakes, newest first |
| `/flood` | River gauge levels, worst first. `/flood Klang` searches |
| `/nearby` | Closest forecast area and river gauges to a location you share |
| `/fav` | Your saved places, or `/fav Ipoh` to look one up and save it |
| `/unfav` | Remove something from your favourites |
| `/sub` | Turn alerts on |
| `/unsub` | Turn alerts off |
| `/settings` | Quiet hours, thresholds, digest time and clock format |
| `/about` | Data sources and credits |
| `/stats` | Usage figures. Administrators only |

You do not have to use a command at all. Send a place name such as `Ipoh` and
the bot searches both the forecast towns and the river gauges. Share a location
and it works out what is closest.

## Alerts

Six kinds, each switched on and off from `/sub`:

| Alert | What triggers it |
| --- | --- |
| Weather warnings | A MET warning naming one of your saved areas |
| River levels | One of your saved gauges reaching your threshold |
| Earthquakes | A quake at or above your magnitude threshold |
| Morning digest | A daily summary at your chosen time |
| National flood watch | Any gauge in the country reaching danger level |
| Strong quake watch | Any quake of magnitude 6 or above |

Quiet hours hold back routine alerts overnight. A gauge at danger level or a
major earthquake still comes through, because that is the kind of thing worth
waking up for.

Each event is sent once. A warning is not repeated for six hours, a river level
for three, and an earthquake for a day, so a storm sitting over the country
produces one message rather than one every poll.

## How it is built

```
bot/
  config.py      Settings, read once from the environment
  database.py    SQLite: users, favourites, subscriptions, jobs, buttons, cache
  feeds.py       The four data.gov.my endpoints, parsed and cached
  richtext.py    Rich message building and sending, with a classic fallback
  scheduler.py   The digest queue and the three alert watchers
  timeutils.py   Malaysian time, and the several upstream timestamp formats
  views.py       Every screen, as a rich document
  main.py        Handlers, routing and keyboards
tests/
  test_bot.py    48 tests over parsing, persistence and message building
```

### Everything is a rich message

Every message the bot sends is a
[Rich Message](https://core.telegram.org/bots/features#rich-messages), which is
what allows the tables, headings and collapsible sections you see in the
forecasts and river readings.

Rich messages exist only on the HTTP Bot API, and Telethon speaks MTProto, so
`richtext.py` posts to `sendRichMessage` over HTTP using the same bot token and
falls back to classic HTML through Telethon if Telegram rejects it. Every send
and edit in the project goes through that one module, so neither the rich path
nor its fallback can be bypassed by a handler.

### Buttons stay alive across restarts

Inline button payloads are rows in SQLite, not encoded into the callback data
and not held in memory. A button keeps working however long ago the message was
sent and however many times the bot has restarted since. Pressing one edits the
message in place rather than sending a new one, so a session stays a single
message instead of a wall of them.

Every screen carries a way onward and a way back, so no menu is a dead end.

### Scheduling is a SQLite table

There is no timer wheel. Pending digests are rows in `scheduled_jobs` with a
`run_at` and a deduplication key, and a loop claims whatever is due. Stop the
bot mid-week and nothing is lost: the rows are still there when it comes back.
A digest whose moment passed while the bot was down is dropped rather than sent
late, since a morning briefing at midnight helps nobody.

### Upstream data

Four public endpoints, called directly, with no API key:

| Feed | Endpoint |
| --- | --- |
| Forecasts | `api.data.gov.my/weather/forecast/` |
| Weather warnings | `api.data.gov.my/weather/warning/` |
| Earthquakes | `api.data.gov.my/weather/warning/earthquake/` |
| River levels | `api.data.gov.my/flood-warning/` |

Responses are cached in SQLite, so a restart does not send a burst of requests
upstream. When a fetch fails, the last good copy is served and the message says
how old it is, because slightly stale flood levels are far more useful than an
error.

Three things about these feeds are worth knowing, since each one is handled in
code and each would otherwise be a visible bug:

- The forecast endpoint returns **Malay text** even though the rest is English,
  so `feeds.py` translates the phrasing. The vocabulary is compositional, a
  condition plus a qualifier, so wording MET has not used before still comes
  out in English.
- The warning feed carries a permanently valid **"No Advisory"** row. It is
  genuinely in force, so a date check alone would have the bot announce a
  warning when the real answer is that there is none.
- The forecast is a little over **3000 rows**. Asking for too few does not drop
  towns, it silently truncates the far end of the week.

## Running it

Requires Python 3.11 or newer.

```bash
git clone <your-repo-url>
cd telegram-bots/weather-bot

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

cp .env.example .env
nano .env          # fill in the three required values

.venv/bin/python -m bot.main
```

See [SETUP.md](SETUP.md) for registering the bot with BotFather, filling in its
description and command list, and keeping it running under tmux on a Debian 13
VPS.

## Tests

```bash
.venv/bin/python -m pytest
```

48 tests, none of which touch the network or Telegram. The feeds are exercised
against recorded response shapes, which is what catches an upstream field
rename before a user sees it.

## Configuration

Only three variables are required. Everything else has a working default.

| Variable | Required | Purpose |
| --- | --- | --- |
| `TELEGRAM_API_ID` | Yes | From my.telegram.org |
| `TELEGRAM_API_HASH` | Yes | From my.telegram.org |
| `TELEGRAM_BOT_TOKEN` | Yes | From @BotFather |
| `DONATION_URL` | No | Adds a support button to `/start` |
| `WEB_APP_URL` | No | Web app button target |
| `SUPABASE_URL` | No | Unused. The bot is SQLite only |
| `SUPABASE_SERVICE_KEY` | No | Unused |
| `ADMIN_USER_IDS` | No | Who may run `/stats` and hear about feed trouble |

The rest, covering cache lifetimes, poll intervals and the nearby search
radius, is documented in [.env.example](.env.example).

## Data sources and credits

Every figure comes from the Malaysian government open data portal at
data.gov.my, published on behalf of:

- **MET Malaysia**, the Malaysian Meteorological Department, for forecasts,
  weather warnings and earthquake bulletins.
- **The Department of Irrigation and Drainage**, through the national flood
  warning feed, for river levels.

The bot adds no forecasting of its own. It relays and formats what those
agencies have already released.

## Licence

See [LICENSE](../../LICENSE) at the root of the repository.
