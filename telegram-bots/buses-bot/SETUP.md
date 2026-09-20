# Setup

Everything needed to take this bot from nothing to running on a Debian 13 VPS. Part 1 is Telegram configuration, part 2 is the server.

---

## Part 1: Telegram

### 1.1 Create the bot

Open [@BotFather](https://t.me/BotFather) and send `/newbot`.

```
You:       /newbot
BotFather: Alright, a new bot. How are we going to call it?
You:       Malaysia Buses
BotFather: Good. Now let's choose a username for your bot.
You:       malaysiabuses_bot
```

BotFather replies with a token that looks like `123456789:AAH...`. Keep it secret; anyone holding it controls the bot. Put it in `.env` as `TELEGRAM_BOT_TOKEN` and never commit that file.

If the bot already exists, skip to 1.3 and use `/mybots` instead.

### 1.2 Get an API id and hash

Telethon connects over MTProto, which needs its own credentials in addition to the bot token.

1. Sign in at [my.telegram.org](https://my.telegram.org) with your phone number.
2. Open **API development tools**.
3. Fill in the form. An app title of `Malaysia Buses Bot` and a short name of `malaysiabuses` are fine, and the platform can be **Other**.
4. Copy the **App api_id** and **App api_hash** into `.env` as `TELEGRAM_API_ID` and `TELEGRAM_API_HASH`.

These belong to your Telegram account rather than to the bot, so the same pair can serve several bots.

### 1.3 Set the description

The description is what people see on the bot's empty chat screen, before they press Start.

Send `/setdescription` to BotFather, pick the bot, then send:

```
Live bus times and positions for Malaysia. Covers Rapid Bus KL, Rapid Bus Penang, Rapid Bus MRT Feeder and myBAS Johor, using open data from data.gov.my.

Send a stop name or share your location to begin. Save your regular stops and get a message before each bus arrives.
```

### 1.4 Set the about text

The about text appears on the bot's profile card and is limited to 120 characters.

Send `/setabouttext`, pick the bot, then send:

```
Live bus times and positions for KL, Penang and Johor. Send a stop name or share your location to begin.
```

### 1.5 Set the command list

This fills the menu next to the chat input. Send `/setcommands`, pick the bot, then paste this block exactly, one command per line, with no leading slashes:

```
start - What this bot does and every command
fav - Save a stop to your favourites
unfav - Remove a stop from your favourites
sub - Turn on reminders and alerts
unsub - Turn off all notifications
settings - Operator, clock format and quiet hours
live - Live bus positions on a map
routes - Browse routes by operator
stops - Find a stop and its timetable
trip - Follow one bus stop by stop
```

Keep `/stats` out of this list. It is for the operator, and listing it invites people to try it.

### 1.6 Profile picture

Send `/setuserpic` and upload a square image, at least 512 by 512 pixels.

### 1.7 Recommended settings

| BotFather command | Choose | Why |
| --- | --- | --- |
| `/setprivacy` | **Enable** | In groups the bot then only sees commands aimed at it. Free-text search still works in direct messages. |
| `/setjoingroups` | your call | **Disable** it if the bot is meant for one-to-one use only. |
| `/setinline` | Disable | The bot does not implement inline queries. |

Privacy mode is worth keeping enabled. With it off, the bot receives every message in any group it joins, which is a lot of data you have no reason to hold.

---

## Part 2: The server

Tested on a fresh Debian 13 (Trixie) VPS.

### 2.1 System packages

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git tmux
```

Debian 13 ships Python 3.13, which is new enough. Check with `python3 --version`.

### 2.2 A user for the bot

Running a network service as root is worth avoiding.

```bash
sudo adduser --disabled-password --gecos "" botuser
sudo su - botuser
```

Everything from here runs as `botuser`.

### 2.3 Get the code

```bash
git clone https://github.com/augystudios/malaysia.git
cd malaysia/telegram-bots/buses-bot
```

### 2.4 Virtual environment

```bash
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
```

Debian marks its system Python as externally managed, so installing into a venv rather than globally is required, not merely tidy.

### 2.5 Configuration

```bash
cp .env.example .env
nano .env
```

Fill in the four required values from part 1:

```ini
TELEGRAM_API_ID=1234567
TELEGRAM_API_HASH=0123456789abcdef0123456789abcdef
TELEGRAM_BOT_TOKEN=123456789:AAH...
DONATION_URL=https://your-donation-link
```

Then lock the file down, because it holds credentials:

```bash
chmod 600 .env
```

### 2.6 Check it starts

```bash
chmod +x run.sh
./run.sh
```

A healthy start looks like this:

```
2026-09-20 21:30:00 INFO     buses-bot: Database ready at /home/botuser/.../data/buses.sqlite3
2026-09-20 21:30:01 INFO     buses-bot: Signed in as @malaysiabuses_bot
2026-09-20 21:30:01 INFO     buses-bot: Bot is running. Press Ctrl+C to stop.
2026-09-20 21:30:04 INFO     buses-bot: Loaded Rapid Bus KL: 4053 stops, 137 routes
2026-09-20 21:30:07 INFO     buses-bot: Loaded Rapid Bus Penang: 1931 stops, 47 routes
```

Message the bot `/start` to confirm. Then stop it with `Ctrl+C` before setting up tmux.

### 2.7 Run it under tmux

```bash
tmux new -s buses
cd ~/malaysia/telegram-bots/buses-bot
./run.sh
```

Detach with **`Ctrl+B`** then **`D`**. The bot keeps running after you disconnect.

Useful commands:

| Command | What it does |
| --- | --- |
| `tmux attach -t buses` | Go back to the session |
| `tmux ls` | List sessions |
| `Ctrl+B` then `D` | Detach, leaving it running |
| `Ctrl+B` then `[` | Scroll back through output, `q` to exit |
| `tmux kill-session -t buses` | Stop the session entirely |

### 2.8 Start on boot

```bash
crontab -e
```

Add:

```cron
@reboot sleep 30 && tmux new-session -d -s buses -c /home/botuser/malaysia/telegram-bots/buses-bot './run.sh'
```

The `sleep 30` gives the network time to come up before the bot tries to reach Telegram.

---

## systemd instead of tmux

If you would rather have systemd supervise it, use this instead of tmux. It restarts the bot on failure and on boot without a cron entry.

```bash
sudo nano /etc/systemd/system/buses-bot.service
```

```ini
[Unit]
Description=Malaysia Buses Telegram Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=botuser
WorkingDirectory=/home/botuser/malaysia/telegram-bots/buses-bot
ExecStart=/home/botuser/malaysia/telegram-bots/buses-bot/.venv/bin/python -m bot.main
Restart=always
RestartSec=10

# Hardening. The bot only needs to write to its own data directory.
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=/home/botuser/malaysia/telegram-bots/buses-bot/data
ReadWritePaths=/home/botuser/malaysia/telegram-bots/buses-bot/logs

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now buses-bot
sudo systemctl status buses-bot
journalctl -u buses-bot -f      # follow the logs
```

Pick one or the other. Running both means two bots polling the same token, which Telegram will not accept.

---

## Updating

```bash
tmux attach -t buses
# Ctrl+C to stop
git pull
.venv/bin/pip install -r requirements.txt
./run.sh
# Ctrl+B then D
```

The database is untouched by a `git pull`, so favourites and settings survive updates.

---

## Backups

Everything that matters is one SQLite file.

```bash
# Safe to run while the bot is running
sqlite3 data/buses.sqlite3 ".backup '/home/botuser/backups/buses-$(date +%F).sqlite3'"
```

A nightly copy via cron:

```cron
0 3 * * * sqlite3 /home/botuser/malaysia/telegram-bots/buses-bot/data/buses.sqlite3 ".backup '/home/botuser/backups/buses-$(date +\%F).sqlite3'"
```

Do not back up `.env` or the `.session` file to anywhere public. Both grant control of the bot.

---

## Troubleshooting

**`TELEGRAM_API_ID is not set`**
The `.env` file is missing or in the wrong directory. It belongs next to `run.sh`, not in `bot/`.

**`database is locked`**
Two copies of the bot are running against one database. Check with `ps aux | grep bot.main` and stop the extra one. This is the usual outcome of enabling both tmux and systemd.

**The bot starts but does not reply**
Confirm the token is right, that you messaged the correct username, and that `/setprivacy` has not been left enabled while you test free-text search inside a group. In direct messages privacy mode has no effect.

**Search finds nothing just after starting**
The GTFS bundles are still downloading. Warm-up takes a few seconds per operator and the log prints a line as each one lands.

**`Upstream returned 429`**
data.gov.my is rate limiting. The bot serves its cached copy meanwhile. Raising `STATIC_REFRESH_HOURS` reduces how often it asks.

**Formatting arrives as plain text**
Telegram declined the rich message and the bot fell back to classic HTML. The log records the reason once. Content is preserved; only tables and headings are flattened.

**Checking on it**

```bash
tail -f logs/bot-$(date +%F).log      # under tmux
journalctl -u buses-bot -f            # under systemd
sqlite3 data/buses.sqlite3 "SELECT COUNT(*) FROM users;"
```

---

## Security checklist

- [ ] `.env` is `chmod 600` and is not in git
- [ ] The `.session` file is not in git, since it authenticates as the bot
- [ ] The bot runs as an unprivileged user, not root
- [ ] `ADMIN_USER_IDS` is set if you intend to use `/stats`
- [ ] Backups exclude `.env` and `.session`
- [ ] The token has not been pasted into an issue, a screenshot or a chat

If a token is exposed, send `/revoke` to BotFather straight away and put the new one in `.env`.
