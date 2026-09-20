# Setup

Everything needed to take this bot from nothing to running on a Debian 13 VPS. Part 1 is Telegram configuration, part 2 is the server.

---

## Part 1: Telegram

### 1.1 Create the bot

Open [@BotFather](https://t.me/BotFather) and send `/newbot`.

```text
You:       /newbot
BotFather: Alright, a new bot. How are we going to call it?
You:       Malaysia Trains
BotFather: Good. Now let's choose a username for your bot.
You:       malaysiatrains_bot
```

BotFather replies with a token that looks like `123456789:AAH...`. Keep it secret; anyone holding it controls the bot. Put it in `.env` as `TELEGRAM_BOT_TOKEN` and never commit that file.

If the bot already exists, skip to 1.3 and use `/mybots` instead.

### 1.2 Get an API id and hash

Telethon connects over MTProto, which needs its own credentials in addition to the bot token.

1. Sign in at [my.telegram.org](https://my.telegram.org) with your phone number.
2. Open **API development tools**.
3. Fill in the form. An app title of `Malaysia Trains Bot` and a short name of `malaysiatrains` are fine, and the platform can be **Other**.
4. Copy the **App api_id** and **App api_hash** into `.env` as `TELEGRAM_API_ID` and `TELEGRAM_API_HASH`.

These belong to your Telegram account rather than to the bot, so the same pair can serve several bots. The buses bot in this repository can reuse them.

### 1.3 Set the description

The description is what people see on the bot's empty chat screen, before they press Start. The limit is 512 characters.

Send `/setdescription` to BotFather, pick the bot, then send:

```text
Train times and live positions for Malaysia. Covers the LRT, MRT, Monorail and BRT lines run by Rapid KL, plus KTM Komuter, ETS and intercity services, using open data from data.gov.my.

Send a station name or share your location to begin. Save the stations you use and get a message before each train.
```

### 1.4 Set the about text

The about text appears on the bot's profile card and is limited to 120 characters.

Send `/setabouttext`, pick the bot, then send:

```text
Train times and live positions for Malaysia. Send a station name or share your location to begin.
```

That is 97 characters, comfortably inside the limit.

### 1.5 Set the command list

This fills the menu next to the chat input. Send `/setcommands`, pick the bot, then paste this block exactly, one command per line, with no leading slashes:

```text
start - What this bot does and every command
next - Next trains from your home station
stations - Find a station and its timetable
lines - Browse lines and the stations they serve
train - Follow one train stop by stop
live - Live KTMB train positions
fav - Save a station to your favourites
unfav - Remove a station from your favourites
sub - Turn on reminders and alerts
unsub - Turn off all notifications
settings - Home station, clock format and quiet hours
```

`/stats` is deliberately left out. It is for administrators and does not belong in a public menu.

There is no `/help` either, because `/start` already lists everything.

### 1.6 Profile picture

Send `/setuserpic` and upload a square image, at least 512 by 512 pixels. A line diagram or a train icon in the project's colours works well.

### 1.7 Recommended settings

| Setting | Command | Value | Why |
| --- | --- | --- | --- |
| Inline mode | `/setinline` | Disabled | The bot has no inline handler |
| Group privacy | `/setprivacy` | Enabled | The bot only needs to see commands aimed at it |
| Join groups | `/setjoingroups` | Your choice | Enable if you want it usable in group chats |

With privacy enabled, the bot in a group sees only messages starting with `/`. Free-text search then works in direct messages only, which is the usual arrangement.

---

## Part 2: The server

Written for a fresh Debian 13 box. Commands that need root are shown with `sudo`.

### 2.1 System packages

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git tmux
```

Check the Python version is 3.11 or newer:

```bash
python3 --version
```

Debian 13 ships 3.13, which is what this bot was built against.

### 2.2 A user for the bot

Running it as your login user is fine, but a dedicated account limits the damage if anything goes wrong.

```bash
sudo adduser --disabled-password --gecos "" botrunner
sudo su - botrunner
```

The rest of this guide assumes you are that user, in its home directory.

### 2.3 Get the code

```bash
git clone <your-repo-url> malaysia
cd malaysia/telegram-bots/trains-bot
```

### 2.4 Virtual environment

```bash
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
```

Everything is version pinned, so this installs the same set that was tested.

### 2.5 Configuration

```bash
cp .env.example .env
nano .env
```

Fill in the four required values:

```dotenv
TELEGRAM_API_ID=1234567
TELEGRAM_API_HASH=your_api_hash_here
TELEGRAM_BOT_TOKEN=123456789:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
DONATION_URL=https://your-donation-link.example
```

Leave `SUPABASE_URL` and `SUPABASE_SERVICE_KEY` blank. The bot keeps all of its state in one SQLite file and never calls Supabase.

Lock the file down, since it holds the token:

```bash
chmod 600 .env
```

To use `/stats`, add your numeric Telegram user id, which [@userinfobot](https://t.me/userinfobot) will tell you:

```dotenv
ADMIN_USER_IDS=123456789
```

### 2.6 Check it starts

Run it in the foreground once to confirm the configuration before putting it in tmux:

```bash
.venv/bin/python -m bot.main
```

A healthy start looks like this:

```
2026-09-21 10:00:00 INFO     trains-bot: Database ready at /home/botrunner/malaysia/telegram-bots/trains-bot/data/trains.sqlite3
2026-09-21 10:00:01 INFO     trains-bot: Signed in as @malaysiatrains_bot
2026-09-21 10:00:01 INFO     trains-bot: Scheduler started with 4 loops
2026-09-21 10:00:01 INFO     trains-bot: Bot is running. Press Ctrl+C to stop.
2026-09-21 10:00:06 INFO     bot.gtfs: Loaded Rapid KL Rail: 187 stations, 8 lines
2026-09-21 10:00:09 INFO     bot.gtfs: Loaded KTMB: 156 stations, 9 lines
```

The two feed lines arrive a few seconds after the rest, because the schedules download in the background. Message the bot `/start` to confirm, then stop it with `Ctrl+C`.

If it exits immediately with a message about a missing setting, `.env` is incomplete. The error names the variable.

### 2.7 Run it under tmux

```bash
tmux new -s trains
```

Inside the session:

```bash
cd ~/malaysia/telegram-bots/trains-bot
.venv/bin/python -m bot.main
```

Detach with `Ctrl+B` then `D`. The bot keeps running with the session in the background.

| Action | Command |
| --- | --- |
| Reattach | `tmux attach -t trains` |
| List sessions | `tmux ls` |
| Detach again | `Ctrl+B` then `D` |
| Stop the bot | Reattach, then `Ctrl+C` |
| Scroll the output | `Ctrl+B` then `[`, then `q` to leave |

To keep a log as well as the live output:

```bash
.venv/bin/python -m bot.main 2>&1 | tee -a trains-bot.log
```

`*.log` is already in `.gitignore`.

### 2.8 After a reboot

tmux sessions do not survive a restart, so start it again by hand:

```bash
tmux new -s trains
cd ~/malaysia/telegram-bots/trains-bot
.venv/bin/python -m bot.main
# Ctrl+B then D
```

Nothing is lost by a reboot. Queued reminders, favourites and live buttons all live in SQLite and resume on the next start.

---

## Updating

```bash
tmux attach -t trains
# Ctrl+C to stop the bot

git pull
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m bot.main
# Ctrl+B then D
```

The database upgrades itself on start, so no manual migration step is needed.

---

## Backups

The single SQLite file holds every user, favourite, subscription and pending job.

```bash
# Safe to run while the bot is running
sqlite3 data/trains.sqlite3 ".backup 'backup-$(date +%F).sqlite3'"
```

A weekly copy kept off the VPS is plenty. The Telethon session file in `data/` authenticates as the bot, so treat any backup containing it as a secret.

---

## Troubleshooting

**`TELEGRAM_API_ID is not set`**
`.env` is missing or incomplete. Confirm it sits in the `trains-bot` directory, beside `requirements.txt`.

**`Signed in as @...` never appears**
The token is wrong or the VPS cannot reach Telegram. Check with:

```bash
curl -s "https://api.telegram.org/bot<YOUR_TOKEN>/getMe"
```

**Two bots replying to everything**
An old copy is still running in another tmux session. `tmux ls`, then attach and stop the extra one.

**`database is locked`**
Two processes share one SQLite file, which is the same cause as above.

**`Could not preload ...` in the log**
data.gov.my rate limits aggressively. The bot serves its cached copy and retries later, so this is usually self-correcting.

**Messages arrive without tables or headings**
Telegram declined the rich path and the bot fell back to classic HTML, which is expected rather than broken. `/stats` shows which path is in use.

**Reading the logs**

```bash
tmux attach -t trains
# Ctrl+B then [ to scroll back, q to exit, Ctrl+B then D to detach
```

---

## Security checklist

- [ ] `.env` is `chmod 600` and never committed
- [ ] `git status` shows no `.env`, `*.session` or `*.sqlite3`
- [ ] The bot token has not been pasted into a chat, issue or screenshot
- [ ] `ADMIN_USER_IDS` lists only ids you control
- [ ] Backups holding the session file are stored somewhere private

If a token is ever exposed, send `/revoke` to BotFather, then put the new token in `.env` and restart.
