"""Entry point. Run from the discord-bot directory, inside tmux:

    .venv/bin/python -m bot.main

One process: the gateway connection, the slash commands, and the scheduler
loops. All state lives in one SQLite file under ./data.

Exit codes: 0 clean stop, 2 bad configuration or token, 3 already running,
1 anything else.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any

import discord
from discord import app_commands

from .config import ConfigError, Settings, load_settings
from .database import Database
from .extras import Extras
from .features import account, extras as extras_commands, help as help_commands, transit as transit_commands, weather as weather_commands
from .scheduler import Scheduler
from .transit import Transit
from .ui import SHARED, Screen, TokenButton, TokenSelect, materialise
from .weather import WeatherFeeds

log = logging.getLogger("bot")


class MalaysiaBot(discord.Client):
    def __init__(self, settings: Settings, db: Database) -> None:
        # Default intents only. Everything arrives as an interaction, so no
        # privileged intent (message content, members, presence) is needed.
        super().__init__(intents=discord.Intents.default())

        # Installable on a server and on a user account, and usable in
        # servers, the bot's own DM, and other DMs and group DMs.
        #
        # Both halves matter. Declaring user=False here, or leaving
        # private_channel off, keeps every command out of user installs even
        # with User Install ticked in the Developer Portal. And the scopes
        # reach Discord only through a sync, which is why the tree is synced
        # on every start rather than only when a command changes.
        self.tree = app_commands.CommandTree(
            self,
            allowed_contexts=app_commands.AppCommandContext(guild=True, dm_channel=True, private_channel=True),
            allowed_installs=app_commands.AppInstallationType(guild=True, user=True),
        )
        self.settings = settings
        self.db = db
        self.weather = WeatherFeeds(db)
        self.transit = Transit(db)
        self.extras = Extras(db)
        self.scheduler = Scheduler(self)
        self.command_ids: dict[str, int] = {}
        self.owner_ids: set[int] = set()
        self.started_at = time.time()
        self._closed_resources = False

        for command in (
            help_commands.help_command,
            weather_commands.group,
            transit_commands.train,
            transit_commands.bus,
            extras_commands.prayer,
            extras_commands.fuel,
            extras_commands.forex,
            account.fav,
            account.alerts,
            account.settings,
            account.stats,
        ):
            self.tree.add_command(command)
        self.tree.on_error = self.on_command_error

    # -- startup ----------------------------------------------------------

    async def setup_hook(self) -> None:
        # Buttons and select menus are matched by custom id pattern, so every
        # one ever sent is live from here on, with nothing else to register.
        self.add_dynamic_items(TokenButton, TokenSelect)
        await self._sync()

        app = await self.application_info()
        if app.team:
            self.owner_ids = {member.id for member in app.team.members}
        elif app.owner:
            self.owner_ids = {app.owner.id}

        self.scheduler.start()
        asyncio.create_task(self._warm(), name="warm-feeds")

    async def _sync(self) -> None:
        try:
            synced = await self.tree.sync()
            log.info("Synced %d slash commands", len(synced))
        except discord.HTTPException as exc:
            # The commands from the last successful sync still work.
            log.warning("Could not sync slash commands: %r", exc)
            try:
                synced = await self.tree.fetch_commands()
            except discord.HTTPException:
                return
        # Kept so /help can print clickable command mentions.
        self.command_ids = {command.name: command.id for command in synced}

    async def _warm(self) -> None:
        """Load the searchable feeds in the background, smallest first."""

        await self.weather.warm()
        try:
            await self.extras.zones()
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not load prayer zones: %s", exc)
        await self.transit.warm()
        # Departure reminders need the timetables, so plan once they exist.
        await self.scheduler.plan_all()

    async def on_ready(self) -> None:
        log.info("Connected as %s, in %d servers", self.user, len(self.guilds))
        # on_ready also fires after a gateway reconnect, which resets presence.
        await self.update_presence()

    async def on_guild_join(self, guild: discord.Guild) -> None:
        await self.update_presence()

    async def on_guild_remove(self, guild: discord.Guild) -> None:
        await self.db.delete_guild_alerts(guild.id)
        log.info("Removed from %s, its alert channel is forgotten", guild.id)
        await self.update_presence()

    async def update_presence(self) -> None:
        """The status under the bot's name, counting the servers it is in.

        User installs are not counted: Discord does not tell a bot how many
        accounts have added it.
        """

        await self.change_presence(activity=discord.CustomActivity(name=presence_text(len(self.guilds))))

    async def on_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        name = interaction.command.qualified_name if interaction.command else "?"
        log.error("Command /%s failed", name, exc_info=error)
        text = "Something went wrong on this side. Try again in a minute."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(text, ephemeral=True)
            else:
                await interaction.response.send_message(text, ephemeral=True)
        except discord.HTTPException:
            pass

    # -- sending, for the scheduler ---------------------------------------

    async def send_dm(self, user_id: int, screen: Screen) -> bool:
        try:
            user = self.get_user(user_id) or await self.fetch_user(user_id)
            view = await materialise(self.db, screen, user_id)
            kwargs: dict[str, Any] = {"embeds": screen.embeds[:10]}
            if view is not None:
                kwargs["view"] = view
            await user.send(**kwargs)
            return True
        except discord.Forbidden:
            # DMs closed. Stop trying until they switch an alert on again,
            # which tests the DM afresh.
            await self.db.set_pref(user_id, "dm_ok", 0)
            log.info("DMs to %s are closed, pausing their alerts", user_id)
        except discord.HTTPException as exc:
            log.warning("DM to %s failed: %s", user_id, exc)
        return False

    async def send_channel(self, guild_row: Any, screen: Screen) -> bool:
        guild_id, channel_id = int(guild_row["guild_id"]), int(guild_row["channel_id"])
        guild = self.get_guild(guild_id)
        if guild is None:
            # Removed from the server while offline.
            await self.db.delete_guild_alerts(guild_id)
            return False
        try:
            channel = guild.get_channel_or_thread(channel_id) or await self.fetch_channel(channel_id)
            view = await materialise(self.db, screen, SHARED)
            kwargs: dict[str, Any] = {"embeds": screen.embeds[:10]}
            if view is not None:
                kwargs["view"] = view
            await channel.send(**kwargs)  # type: ignore[union-attr]
            return True
        except discord.NotFound:
            log.info("Alert channel %s in %s is gone, forgetting it", channel_id, guild_id)
            await self.db.delete_guild_alerts(guild_id)
        except discord.Forbidden:
            log.warning("No permission to post alerts in %s of %s", channel_id, guild_id)
        except discord.HTTPException as exc:
            log.warning("Alert post to %s failed: %s", channel_id, exc)
        return False

    # -- shutdown ---------------------------------------------------------

    async def close(self) -> None:
        if not self._closed_resources:
            self._closed_resources = True
            await self.scheduler.stop()
            for client in (self.weather, self.transit, self.extras):
                await client.close()
        await super().close()


def presence_text(guilds: int) -> str:
    return f"Truly Asia in {guilds} guild{'' if guilds == 1 else 's'}"


# ---------------------------------------------------------------------------
# One copy at a time
# ---------------------------------------------------------------------------


class AlreadyRunning(RuntimeError):
    pass


class SingleInstance:
    """An exclusive lock on data/bot.lock.

    Two copies started in two tmux windows would both answer every button and
    send every alert twice. The lock is released by the OS when the process
    ends, however it ends, so there is never a stale lock to clear by hand.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle: Any = None

    def __enter__(self) -> "SingleInstance":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = open(self.path, "a+")
        try:
            import fcntl
        except ImportError:  # Windows, for local development only
            return self
        try:
            fcntl.flock(self._handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._handle.seek(0)
            pid = self._handle.read().strip() or "unknown"
            self._handle.close()
            raise AlreadyRunning(f"Another copy is already running (pid {pid}). Stop it first.")
        self._handle.seek(0)
        self._handle.truncate()
        self._handle.write(str(os.getpid()))
        self._handle.flush()
        return self

    def __exit__(self, *exc: Any) -> None:
        if self._handle is not None:
            self._handle.close()


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------


async def run(settings: Settings) -> int:
    db = Database(settings.database_path)
    await db.connect()
    bot = MalaysiaBot(settings, db)

    loop = asyncio.get_running_loop()
    try:
        loop.add_signal_handler(signal.SIGTERM, lambda: asyncio.ensure_future(bot.close()))
    except (NotImplementedError, AttributeError):
        pass  # Windows

    code = 0
    try:
        async with bot:
            await bot.start(settings.discord_token)
    except discord.LoginFailure:
        print("Discord refused DISCORD_TOKEN. Reset it in the Developer Portal and update .env.", file=sys.stderr)
        code = 2
    finally:
        await bot.close()
        await db.close()
    log.info("Stopped cleanly" if code == 0 else "Stopped")
    return code


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("discord").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    try:
        with SingleInstance(settings.data_dir / "bot.lock"):
            return asyncio.run(run(settings))
    except AlreadyRunning as exc:
        print(str(exc), file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        log.info("Interrupted, stopped")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
