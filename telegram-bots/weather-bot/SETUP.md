# Setting up the bot

End to end: registering with BotFather, filling in its public profile, getting
it onto a Debian 13 VPS and keeping it running under tmux.

Allow about twenty minutes the first time.

---

## 1. Telegram API credentials

Telethon signs in over MTProto, which needs an API ID and hash. These identify
the application, not the bot, and one pair covers every bot you run.

1. Go to [my.telegram.org](https://my.telegram.org) and log in with your phone
   number.
2. Open **API development tools**.
3. Fill in the form. The title and short name can be anything; `Malaysia
   Weather Bot` and `malaysiaweather` are fine. Leave the URL blank.
4. Copy the **App api_id** and **App api_hash**.

Treat the hash like a password.

---

## 2. Create the bot with BotFather

Open [@BotFather](https://t.me/BotFather) and send `/newbot`.

```
You:       /newbot
BotFather: Alright, a new bot. How are we going to call it?
You:       Malaysia Weather
BotFather: Good. Now let's choose a username for your bot.
You:       malaysiaweather_bot
BotFather: Done! Congratulations on your new bot.
           Use this token to access the HTTP API:
           123456789:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

Copy the token. If it ever leaks, `/revoke` in BotFather issues a new one.

---

## 3. Fill in the public profile

These are what a new user sees before they press Start, so they are worth
getting right. Each is a separate BotFather command.

### Description

Shown on the empty chat screen, above the Start button. Limit 512 characters.

Send `/setdescription`, pick the bot, then paste:

```
Official Malaysian weather, earthquake and flood information, straight from the data.gov.my open feeds.

Seven day forecasts for 442 towns, active MET warnings, earthquake bulletins and live river levels from 1276 flood gauges.

Save the places you care about and get told when a warning is issued, a river rises or the ground shakes. Send any town name to look it up, or share your location to see what is closest.

Not an emergency service. In an emergency, follow the local authorities.
```

### About text

Shown on the bot's profile page. Limit 120 characters.

Send `/setabouttext`, pick the bot, then paste:

```
Malaysian weather forecasts, MET warnings, earthquake bulletins and river levels from the data.gov.my feeds.
```

### Profile picture

Send `/setuserpic` and upload a square image, at least 512 by 512 pixels.

### Command list

This is what fills the menu beside the message box. Send `/setcommands`, pick
the bot, then paste the block below **exactly**, one command per line, no
leading slashes:

```
start - What this bot does and every command available
weather - Seven day forecast for any town
warnings - Weather warnings currently in force
quake - Recent earthquakes in and around Malaysia
flood - River levels and which gauges are rising
nearby - Closest forecast and river gauges to your location
fav - Save a town or river gauge to your favourites
unfav - Remove something from your favourites
sub - Turn alerts on
unsub - Turn alerts off
settings - Quiet hours, thresholds and preferences
about - Where the data comes from
```

`/stats` is deliberately absent. It is administrator only, and listing it would
invite everyone to try it.

### Privacy mode

Only matters if you plan to add the bot to group chats. Left alone, the bot
sees only messages that start with a command, which is what you want.

To confirm: `/setprivacy`, pick the bot, choose **Enable**.

---

## 4. Prepare the VPS

On Debian 13, as a normal user with sudo:

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git tmux
```

Check the Python version is 3.11 or newer:

```bash
python3 --version
```

---

## 5. Deploy

```bash
cd ~
git clone <your-repo-url> malaysia
cd malaysia/telegram-bots/weather-bot

python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
```

---

## 6. Configure

```bash
cp .env.example .env
nano .env
```

Fill in the three required values:

```ini
TELEGRAM_API_ID=1234567
TELEGRAM_API_HASH=your_api_hash_here
TELEGRAM_BOT_TOKEN=123456789:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
DONATION_URL=https://your-donation-link.example
```

Then lock the file down, since it holds credentials:

```bash
chmod 600 .env
```

To use `/stats` and hear about feed trouble, add your own Telegram user id.
[@userinfobot](https://t.me/userinfobot) will tell you what it is:

```ini
ADMIN_USER_IDS=123456789
```

---

## 7. First run

Run it in the foreground once, to see that it starts cleanly:

```bash
.venv/bin/python -m bot.main
```

Expect:

```
2026-09-21 14:30:00 INFO     bot.database: Database ready at data/weather.sqlite3
2026-09-21 14:30:01 INFO     bot.main: Signed in as @malaysiaweather_bot
2026-09-21 14:30:01 INFO     bot.scheduler: Scheduler started with 4 loops
2026-09-21 14:30:01 INFO     bot.main: Bot is running. Press Ctrl+C to stop.
```

Message the bot on Telegram and send `/start`. If the menu comes back with
buttons, everything is working. Press Ctrl+C to stop.

---

## 8. Run it under tmux

tmux keeps the bot running after you disconnect.

```bash
tmux new -s weather
```

Inside the session:

```bash
cd ~/malaysia/telegram-bots/weather-bot
.venv/bin/python -m bot.main 2>&1 | tee -a weather-bot.log
```

Detach with **Ctrl+B** then **D**. The bot keeps running.

Useful afterwards:

```bash
tmux ls                    # list sessions
tmux attach -t weather     # go back in
tmux kill-session -t weather
```

Inside a session, **Ctrl+B** then **[** scrolls back through the output. Press
**q** to leave scroll mode.

To stop the bot, attach and press Ctrl+C.

---

## 9. Updating

```bash
tmux attach -t weather
# Ctrl+C to stop the bot

cd ~/malaysia
git pull
cd telegram-bots/weather-bot
.venv/bin/pip install -r requirements.txt

.venv/bin/python -m bot.main 2>&1 | tee -a weather-bot.log
# Ctrl+B then D
```

Favourites, subscriptions and pending digests live in `data/weather.sqlite3`
and survive this untouched. So do the inline buttons in old messages, which is
why one from last week still works after an update.

---

## Verifying it works

| Check | How | Expected |
| --- | --- | --- |
| Bot responds | Send `/start` | Menu with buttons |
| Forecasts | Send `/weather Ipoh` | Seven day table, in English |
| Free text | Send `Kuantan` | Forecast, or a chooser |
| Location | Share a location | Nearest town and river gauges |
| Warnings | Send `/warnings` | Active warnings, or a clear "none in force" |
| Rivers | Send `/flood` | Gauge counts and the highest readings |
| Buttons persist | Press a button in an old message after a restart | It still works |
| Editing in place | Press any button | The message changes, no new one appears |

---

## Troubleshooting

**`Missing required environment variables`**

The `.env` file is absent or incomplete. It must sit in the directory you run
from, which is `weather-bot`, not the repository root.

**`TELEGRAM_API_ID must be a number`**

The value has quotes or spaces around it. Write `TELEGRAM_API_ID=1234567` with
nothing else on the line.

**The bot starts but does not reply**

Usually the wrong token, or another copy already running. Check with:

```bash
ps aux | grep bot.main
```

Two copies of the same bot will fight over updates and neither will behave.
Kill the older one.

**`database is locked`**

Two processes are sharing one SQLite file. Same cause, same fix.

**Messages arrive without tables or headings**

Telegram rejected the rich message and the bot fell back to classic HTML. It
keeps working, just more plainly. The log says why:

```bash
grep -i "sendRichMessage" weather-bot.log
```

**`data.gov.my is rate limiting requests`**

The poll interval is too aggressive. Raise `ALERT_POLL_SECONDS` in `.env`,
which defaults to 300, and never set it below 120.

**Readings look out of date**

The bot says so when it is serving a cached copy after a failed fetch. If it
persists, check that the VPS can reach the feed:

```bash
curl -s "https://api.data.gov.my/weather/warning/?limit=1" | head -c 200
```

**Alerts are not arriving**

In order: is the subscription on, under `/sub`; are you inside quiet hours,
under `/settings`; and is the event actually above your threshold. A gauge at
alert level will not notify someone whose threshold is danger.

---

## Backups

Everything the bot remembers is one file:

```bash
cp ~/malaysia/telegram-bots/weather-bot/data/weather.sqlite3 ~/weather-backup.sqlite3
```

Copy it while the bot is running and you may catch it mid-write. Either stop
the bot first, or use the SQLite backup command, which is safe on a live
database:

```bash
sudo apt install -y sqlite3
sqlite3 ~/malaysia/telegram-bots/weather-bot/data/weather.sqlite3 ".backup '/home/$USER/weather-backup.sqlite3'"
```

Do not commit the database, the `.env` file or the `.session` file. The
included `.gitignore` already excludes all three.
