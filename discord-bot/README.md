# Malaysia Boleh for Discord

A Discord bot that brings live Malaysian public data into slash commands:
official weather forecasts and warnings, earthquakes, river levels, train and
bus timetables with live positions, prayer times, fuel prices and exchange
rates. It is the Discord side of [Malaysia Boleh](https://malaysia.uwuapps.org),
and does in one bot what the three Telegram bots in `telegram-bots/` do
separately, plus three everyday extras.

It works in servers, in its own DMs, and anywhere else through a user install
(DMs with friends, group DMs, and servers it has not been added to).

> Not an emergency service. During a flood, a storm or an earthquake, follow
> the local authorities and the official agency channels rather than a Discord
> message.

## Commands

| Command | What it does |
| --- | --- |
| `/help` | What the bot does, every command, and buttons to the web app, donations and install links |
| `/weather forecast` | Seven day forecast for any of 440 towns and districts, with an umbrella verdict |
| `/weather warnings` | MET weather warnings in force |
| `/weather quake` | Recent earthquakes in and around Malaysia |
| `/weather flood` | Gauges at alert level or above, one gauge in detail, or one state |
| `/train next` | Next departures from your home station, or any station |
| `/train station` | A station's timetable, line by line |
| `/train line` | Every station on a line, in order |
| `/train trip` | Follow one train stop by stop |
| `/train live` | Live KTMB train positions with map links |
| `/bus stop` | A bus stop's next departures, route by route |
| `/bus route` | Every stop on a route, in order |
| `/bus trip` | Follow one bus stop by stop |
| `/bus live` | Live bus positions for an operator, optionally one route |
| `/prayer` | Today's or tomorrow's prayer times for a JAKIM zone |
| `/fuel` | This week's RON95, RON97 and diesel prices and the change from last week |
| `/forex` | Ringgit exchange rates from Bank Negara, and conversion both ways |
| `/fav list` | Everything you have saved, to open or remove |
| `/fav add` | Save a town, river gauge, station or bus stop |
| `/fav remove` | Remove a favourite |
| `/alerts on` | Choose which alerts reach you by DM |
| `/alerts off` | Turn every personal alert off |
| `/alerts channel` | Post public alerts in a server channel (needs Manage Server) |
| `/settings` | Quiet hours, digest time, reminder lead, alert thresholds, delete my data |
| `/stats` | Usage figures and feed health, for the app owner only |

Every place, station, stop, line, zone and currency autocompletes as you type.
Discord has no "share location", so autocomplete is how you find things
nearby: start typing the name.

Times are shown with Discord timestamps, so everyone sees them in their own
clock format and time zone. Quiet hours and the digest time are set in
Malaysian time.

## Alerts

**Personal alerts** arrive by DM. Switch each on or off from `/alerts on`:

| Alert | What triggers it |
| --- | --- |
| Weather warnings | A MET warning naming one of your saved towns |
| River levels | A saved gauge reaching your threshold (alert, warning or danger) |
| Earthquakes | A quake at or above your magnitude threshold |
| National flood watch | Any gauge in Malaysia reaching danger level |
| Morning digest | Your saved towns, gauges, stations and prayer times, daily at your chosen time |
| Departure reminders | A set number of minutes before trains and buses at saved stops |
| Live vehicle alerts | A train or bus on a route serving a saved stop coming within 1.5 km (bus) or 2.5 km (rail) |
| Prayer reminders | At each of the five prayer times in your zone |
| Weekly fuel prices | When the new week's prices are published |

Quiet hours hold weather, river and earthquake alerts back overnight rather
than dropping them, so they arrive when quiet hours end if they still apply.
Departure reminders and live vehicle alerts that fall in quiet hours are
skipped, since they are only useful in the moment. A gauge at danger level, a
red warning, a magnitude 6 quake, prayer reminders and the digest you
scheduled all come through regardless.

When you switch on your first alert, the bot sends you a test DM. If your DMs
are closed it says so straight away, rather than failing silently later.

**Server alerts** post to one channel per server. Someone with Manage Server
runs `/alerts channel #channel`, then picks weather warnings, rivers at danger
level, earthquakes above a magnitude, and weekly fuel prices, and can limit
warnings and rivers to one state. The bot has to be a member of the server for
this. Through a user install it can answer commands in a server but cannot
post there on its own, and `/alerts channel` offers an invite button in that
case.

Each event is sent once. A warning is announced again only when MET reissues
or extends it. A gauge that stays high is mentioned again after 3 hours for a
person and 12 hours for a server. Several gauges reaching danger at once
arrive as one message, not one each.

## How it is built

```text
discord-bot/
├── bot/
│   ├── main.py          Entry point, command tree, DM and channel senders, single-instance lock
│   ├── config.py        Environment and tunable constants
│   ├── database.py      SQLite: people, favourites, alerts, servers, jobs, buttons, cache
│   ├── ui.py            Screens, persistent buttons and select menus, the action dispatcher
│   ├── scheduler.py     The job queue, the alert watchers and every alert message
│   ├── weather.py       Forecasts, warnings, quakes and river levels from data.gov.my
│   ├── transit.py       GTFS timetables and live positions for six operators
│   ├── extras.py        Prayer times, fuel prices and exchange rates
│   ├── timeutils.py     Malaysian time, GTFS times past midnight, Discord timestamps
│   └── features/
│       ├── help.py      /help
│       ├── weather.py   /weather
│       ├── transit.py   /train and /bus
│       ├── extras.py    /prayer, /fuel, /forex
│       └── account.py   /fav, /alerts, /settings, /stats
└── tests/
    └── test_bot.py      26 offline tests
```

### Buttons that never expire

Discord forgets every view a bot built in memory when the bot restarts, which
is why most bots' buttons die with the process. Here, each button's action and
payload is a row in the `buttons` table, and only a short token travels in the
button's custom id (`mb:b:<token>`). Select menus work the same way, with a
token as each option's value.

Both are discord.py `DynamicItem`s, matched by pattern against any component
on any message. So a button pressed months later, after any number of
restarts, is looked up by its token and runs as it did the day it was sent.
Identical buttons reuse their token, so drawing the same menu again does not
grow the table.

Pressing a button edits that message in place. A menu belongs to whoever
opened it: if someone else presses a navigation button they get their own
private copy instead of changing the message under its owner, and personal
menus (settings, favourites, alerts) refuse them politely.

Every reply and every edit goes through the interaction's own token, never
the bot's. In a user install the bot is usually not a member of the channel,
so this is the only way it can touch its own messages there.

### Scheduling is a SQLite table

There is no in-process timer. Digests, departure reminders and prayer
reminders are rows in `scheduled_jobs` with a `run_at` and a dedupe key, and a
loop claims whatever is due every 30 seconds. Stop the bot for a week and
nothing is lost: the rows are still there when it comes back. A reminder whose
moment passed while the bot was down is dropped rather than sent late, since a
reminder for a train that has already left helps nobody. A planner tops the
queue up every 15 minutes.

### The data, and its traps

| Data | Source |
| --- | --- |
| Forecasts, warnings, earthquakes | MET Malaysia via `api.data.gov.my` |
| River levels | JPS flood warning via `api.data.gov.my` |
| Rail timetables and positions | Prasarana (Rapid KL Rail) and KTMB GTFS via `api.data.gov.my` |
| Bus timetables and positions | Rapid Bus KL, Penang and MRT Feeder, and myBAS Johor GTFS via `api.data.gov.my` |
| Prayer times | JAKIM e-Solat via [waktusolat.app](https://api.waktusolat.app), an open source API |
| Fuel prices | The weekly `fuelprice` set in the `api.data.gov.my` data catalogue |
| Exchange rates | Bank Negara Malaysia's public API at `api.bnm.gov.my` |

None needs a key. Every response is cached in SQLite, so a restart does not
send a burst of requests upstream, and when a source is down the last good
copy is served with a note saying how old it is.

The feeds have quirks that would each be a visible bug, and each is handled in
code with a test:

- **Rapid KL Rail publishes headways, not a timetable.** Its feed has 48
  template trips plus `frequencies.txt`. Read literally, a station sees six
  trains a day. The bot expands each template into its real runs, about 7300
  across the network.
- **Services do not run every day.** `calendar.txt`, and KTMB's
  `calendar_dates.txt` public holiday swaps, filter every departure, so a
  Sunday timetable never shows on a Tuesday.
- **Rapid KL's `stop_times.txt` has a `route_id` column holding the short
  name** (`AGL`) rather than the id (`AG`). Joining on it matches nothing, so
  the route always comes from `trips.txt`.
- **GTFS times run past 24:00.** A 25:10 departure belongs to the previous
  service day, and "next departures" looks back a day to find it.
- **KTMB reports speed in km/h** although GTFS-realtime specifies m/s.
- **MET forecasts are in Malay** on the English endpoint, and are translated.
- **MET keeps a permanent "No Advisory" row** that a date check alone would
  report as a live warning.
- **Some river gauges stopped reporting years ago** and still carry their last
  indicator, DANGER included. Readings older than two days are shown as "no
  recent reading" and never raise an alert.

## Setting it up

### 1. Create the application

At <https://discord.com/developers/applications>, **New Application**, named
Malaysia Boleh. The name is what people see on the bot's messages and next to
its commands, so it never needs to appear in the commands themselves.

Under **General Information**, upload the icon from `main-site/MYB-512.png`
and paste this as the description (it shows on the bot's profile):

```text
Live Malaysian public data in Discord: official forecasts and weather warnings, earthquakes, river levels, train and bus timetables with live positions, prayer times, fuel prices and exchange rates. Save places and get alerts by DM, or post alerts to a server channel. Free and open source. Start with /help.
```

### 2. Bot

| Setting | Value |
| --- | --- |
| **Reset Token** | Copy it. This is `DISCORD_TOKEN`. If it ever leaks, reset it again at once. |
| Public Bot | On, or off to keep installs to yourself |
| Requires OAuth2 Code Grant | Off |
| Presence, Server Members, Message Content intents | All off. Everything arrives as an interaction. |

### 3. Installation

This is where user installs are won or lost.

| Setting | Value |
| --- | --- |
| Installation Contexts | **Guild Install** and **User Install** both ticked |
| Install Link | Discord Provided Link |
| Guild Install scopes | `applications.commands`, `bot` |
| Guild Install permissions | View Channels, Send Messages, Send Messages in Threads, Embed Links |
| User Install scopes | `applications.commands` |

The install link then asks whether to add the bot to a server or to your
account. `/help` also carries both links as buttons.

The bot declares every command for all three contexts (servers, its own DM,
and other DMs and group DMs) and both install types, and it re-syncs the
commands with Discord on every start. Both matter. A command declared without
user installs never appears in one whatever the Portal says. And a command
synced before user installs were allowed stays off them until the next sync.
A test checks every command's declaration.

### 4. Commands

Nothing to paste. The bot registers its slash commands itself; the log shows
`Synced 11 slash commands` on start (11 top level, 25 in all counting
subcommands). A change can take a minute to reach the client, and restarting
Discord shows it at once.

## Running it on Debian 13

Requires Python 3.11 or newer. Debian 13 ships 3.13.

```bash
sudo apt install python3-venv git tmux

git clone https://github.com/augystudios/malaysia.git
cd malaysia/discord-bot

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

cp .env.example .env
nano .env          # at least DISCORD_TOKEN
```

| Variable | Required | Purpose |
| --- | --- | --- |
| `DISCORD_TOKEN` | Yes | From the Bot page of the Developer Portal |
| `DONATION_URL` | No | Adds a support button to `/help` |
| `SUPABASE_URL` | No | Unused. Everything lives in SQLite |
| `SUPABASE_SERVICE_KEY` | No | Unused |

Everything else (cache lifetimes, poll intervals, alert radii) is a named
constant in `bot/config.py` and `bot/scheduler.py`.

### In tmux

```bash
tmux new -s malaysia
cd ~/malaysia/discord-bot
.venv/bin/python -m bot.main
```

Detach with `Ctrl+B` then `D`, and the bot keeps running.

```bash
tmux attach -t malaysia    # come back to it
tmux ls                    # list sessions
```

| Keys | What they do |
| --- | --- |
| `Ctrl+B` then `D` | Detach, leaving the bot running |
| `Ctrl+B` then `[` | Scroll back through the log, `q` to leave |
| `Ctrl+C` | Stop the bot (while attached) |

To keep a log file while still watching the output:

```bash
.venv/bin/python -m bot.main 2>&1 | tee -a bot.log
```

Nothing restarts the bot automatically, so if it exits the reason is sitting
in the tmux scrollback. After a VPS reboot, start it again by hand. Being
offline costs nothing but the time it was down: favourites, alerts, buttons
and queued reminders are all in `data/malaysia.sqlite3`.

A lock on `data/bot.lock` stops a second copy starting by accident in another
tmux window, which would otherwise answer every button and send every alert
twice. The OS releases it when the process ends, however it ends.

Exit codes: 0 clean stop, 2 bad `.env` or token, 3 already running, 1 other.

### First start

The log shows the sync, the connection, then each timetable loading in the
background:

```text
Synced 11 slash commands
Connected as Malaysia Boleh#1234, in 3 servers
Loaded Rapid KL Rail: 187 stops, 8 routes
Loaded KTMB: 156 stops, 9 routes
...
```

The bot answers straight away. Station and stop autocomplete fills in once the
timetables have loaded, which takes a few seconds. With all six operators
loaded the process uses about 350 MB of memory, most of it the bus timetables,
so a 1 GB VPS is plenty.

### Updating

```bash
cd ~/malaysia && git pull
cd discord-bot && .venv/bin/pip install -r requirements.txt
```

Then `Ctrl+C` the running bot in its tmux window and start it again. The
restart re-syncs the commands, and every old button keeps working.

## Tests

```bash
.venv/bin/python -m pytest
```

26 tests, none of which touch the network or Discord. They cover the feed
quirks above, button tokens and scheduled jobs surviving a reopened database,
the dispatcher (owner edits in place, others get a private copy, personal
menus refuse others, unknown tokens say so), and every command's install and
context declaration.

## Troubleshooting

**Commands do not show in a user install.** Check that **User Install** is
ticked under Installation with the `applications.commands` scope, then restart
the bot so it syncs again, then restart Discord. A server can also switch off
external apps, and in servers with more than 25 members Discord shows a user
install's replies only to the person who asked.

**Commands do not show at all.** The log should say `Synced 11 slash
commands`. If it says it could not sync, the token is for a different
application than the one you installed.

**No DMs arrive.** `/alerts on` says so when a DM was refused. Allow direct
messages from the app, or from members of a server you share with it, then
switch an alert off and on to test again.

**Server alerts stopped.** If the channel was deleted, the setup is forgotten
and the log says so. If the bot lost permission to post, the log says that
instead; fix the channel permissions and nothing else is needed.

**A timetable command says it is still warming up.** It has just restarted
and that operator's timetable is loading. Give it a minute.

**"This button is no longer available".** The button's row was not found,
which only happens if `data/malaysia.sqlite3` was deleted or replaced. Run
the command again for a fresh one.

## Privacy

The bot stores your Discord user id, your favourites, your alert choices and
settings, your home station and prayer zone, and which alerts you have already
been sent. For a server, it stores the alert channel and its settings. Nothing
is sold or shared. The only outgoing requests are to the data sources above.
`/settings` has a **Delete my data** button that removes all of it.

## Licence

MIT, as for the rest of the repository. See [LICENSE](../LICENSE).
