"""/prayer, /fuel and /forex."""

from __future__ import annotations

import time
from datetime import timedelta
from typing import Any

import discord
from discord import app_commands

from ..extras import (
    CURRENCY_NAMES,
    MAJOR_CURRENCIES,
    PRAYERS,
    PRAYER_LABELS,
    format_hijri,
)
from ..timeutils import today_myt, ts
from ..ui import (
    AMBER,
    GREEN,
    TEAL,
    Button,
    Ctx,
    Link,
    Option,
    Screen,
    Select,
    action,
    add_field,
    embed,
    message_screen,
)
from .common import mention, respond, updated_footer

# ---------------------------------------------------------------------------
# Prayer times
# ---------------------------------------------------------------------------


async def prayer_screen(bot: Any, user_id: int, zone_code: str, tomorrow: bool = False) -> Screen:
    zone, candidates = await bot.extras.zone(zone_code)
    if zone is None:
        if not candidates:
            return message_screen(
                f"No prayer zone matches “{zone_code}”. Try a district such as Petaling or Kuantan.",
                "Nothing found",
            )
        return Screen(
            embeds=[embed("Which zone?", f"{len(candidates)} zones match. Pick yours below.", TEAL)],
            rows=[
                Select(
                    "Choose a zone",
                    [
                        Option(f"{z.code} {z.state}", "pr.show", {"z": z.code}, description=z.districts)
                        for z in candidates
                    ],
                )
            ],
        )

    day = today_myt() + timedelta(days=1 if tomorrow else 0)
    entry = await bot.extras.prayer_day(zone.code, day)
    if entry is None:
        return message_screen("JAKIM has not published times for that day yet.", "No times yet")

    now = time.time()
    upcoming = entry.upcoming(now)
    lines = []
    for key, label in PRAYERS:
        epoch = entry.times.get(key)
        if not epoch:
            continue
        marker = "▶️" if upcoming and upcoming[0] == key and not tomorrow else "  "
        lines.append(f"{marker} **{label}** {ts(epoch)}")
    heading = "Tomorrow" if tomorrow else "Today"
    description = f"{heading}, {format_hijri(entry.hijri)}\n{zone.districts}\n\n" + "\n".join(lines)
    if upcoming and not tomorrow:
        description += f"\n\nNext is {PRAYER_LABELS[upcoming[0]]} {ts(upcoming[1], 'R')}."
    body = embed(
        f"🕌 Prayer times for {zone.code}, {zone.state}",
        description,
        TEAL,
        footer="JAKIM e-Solat via waktusolat.app · times show in your own time zone",
    )

    user = await bot.db.ensure_user(user_id)
    is_mine = user["prayer_zone"] == zone.code
    reminders = "prayer" in await bot.db.user_subscriptions(user_id)
    return Screen(
        embeds=[body],
        rows=[
            [
                Button(
                    "My zone" if is_mine else "Set as my zone",
                    "pr.mine",
                    {"z": zone.code},
                    style=discord.ButtonStyle.success if is_mine else discord.ButtonStyle.secondary,
                    emoji="📍",
                    disabled=is_mine,
                ),
                Button(
                    "Today" if tomorrow else "Tomorrow",
                    "pr.show",
                    {"z": zone.code, "t": not tomorrow},
                    emoji="📅",
                ),
                Button(
                    "Reminders on" if reminders else "Remind me",
                    "pr.remind",
                    {"z": zone.code},
                    style=discord.ButtonStyle.success if reminders else discord.ButtonStyle.secondary,
                    emoji="🔔",
                ),
            ]
        ],
    )


@action("pr.show")
async def _prayer_show(ctx: Ctx, p: dict[str, Any]) -> Screen:
    return await prayer_screen(ctx.bot, ctx.user_id, str(p.get("z", "")), bool(p.get("t")))


@action("pr.mine")
async def _prayer_mine(ctx: Ctx, p: dict[str, Any]) -> Screen:
    zone, _ = await ctx.bot.extras.zone(str(p.get("z", "")))
    if zone is None:
        return message_screen("That zone no longer exists.")
    await ctx.db.set_pref(ctx.user_id, "prayer_zone", zone.code)
    # Reminders already queued were for the old zone.
    await ctx.db.delete_jobs(f"u:{ctx.user_id}", "prayer")
    await ctx.bot.scheduler.plan_for(ctx.user_id)
    screen = await prayer_screen(ctx.bot, ctx.user_id, zone.code)
    screen.toast = f"{zone.code} is now your zone. {mention(ctx.bot, 'prayer')} opens it straight away."
    return screen


@action("pr.remind")
async def _prayer_remind(ctx: Ctx, p: dict[str, Any]) -> Screen:
    from .account import check_dm

    zone, _ = await ctx.bot.extras.zone(str(p.get("z", "")))
    if zone is None:
        return message_screen("That zone no longer exists.")
    active = "prayer" in await ctx.db.user_subscriptions(ctx.user_id)
    toast: str
    if active:
        await ctx.db.set_subscription(ctx.user_id, "prayer", False)
        toast = "Prayer reminders off."
    else:
        await ctx.db.set_pref(ctx.user_id, "prayer_zone", zone.code)
        await ctx.db.delete_jobs(f"u:{ctx.user_id}", "prayer")
        await ctx.db.set_subscription(ctx.user_id, "prayer", True)
        if await check_dm(ctx.bot, ctx.interaction.user):
            toast = f"You will get a DM at each of the five prayer times in {zone.code}."
        else:
            toast = "Reminders are on, but I could not DM you. Allow direct messages so they can arrive."
        await ctx.bot.scheduler.plan_for(ctx.user_id)
    screen = await prayer_screen(ctx.bot, ctx.user_id, zone.code)
    screen.toast = toast
    return screen


async def zone_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    bot: Any = interaction.client
    return [
        app_commands.Choice(name=f"{z.code} {z.state}: {z.districts}"[:100], value=z.code)
        for z in bot.extras.peek_zones(current)
    ]


@app_commands.command(name="prayer", description="Today's prayer times for a JAKIM zone")
@app_commands.describe(zone="District or zone code. Leave empty for your saved zone")
@app_commands.autocomplete(zone=zone_autocomplete)
async def prayer(interaction: discord.Interaction, zone: str | None = None) -> None:
    bot: Any = interaction.client

    async def build() -> Screen:
        target = zone
        if not target:
            user = await bot.db.ensure_user(interaction.user.id)
            target = user["prayer_zone"]
        if not target:
            return message_screen(
                f"Which zone? Try {mention(bot, 'prayer')} and type your district, then press "
                "Set as my zone so next time it opens straight away.",
                "Pick a zone",
            )
        return await prayer_screen(bot, interaction.user.id, target)

    await respond(interaction, build)


# ---------------------------------------------------------------------------
# Fuel
# ---------------------------------------------------------------------------


def fuel_table(week: Any) -> str:
    out = []
    for label, price, change in week.rows():
        if change > 0:
            delta = f"🔺 up {change:.2f}"
        elif change < 0:
            delta = f"🔻 down {abs(change):.2f}"
        else:
            delta = "unchanged"
        out.append(f"**{label}** RM{price:.2f} per litre, {delta}")
    return "\n".join(out)


async def fuel_screen(bot: Any, user_id: int | None) -> Screen:
    week, fetched_at, stale = await bot.extras.fuel()
    if week is None:
        return message_screen("The fuel price feed is empty right now.", "No prices")
    body = embed(
        f"⛽ Fuel prices from {week.date}",
        fuel_table(week)
        + "\n\nPrices are set weekly by the Ministry of Finance and apply from the date above.",
        AMBER,
        footer=updated_footer("data.gov.my", fetched_at, stale),
    )
    rows: list[Any] = []
    if user_id is not None:
        subscribed = "fuel" in await bot.db.user_subscriptions(user_id)
        rows.append(
            [
                Button(
                    "Weekly DM on" if subscribed else "DM me each week",
                    "fu.toggle",
                    {},
                    style=discord.ButtonStyle.success if subscribed else discord.ButtonStyle.secondary,
                    emoji="🔔",
                ),
                Button("Refresh", "fu.show", {}, emoji="🔄"),
            ]
        )
    return Screen(embeds=[body], rows=rows)


@action("fu.show")
async def _fuel_show(ctx: Ctx, p: dict[str, Any]) -> Screen:
    return await fuel_screen(ctx.bot, ctx.user_id)


@action("fu.toggle")
async def _fuel_toggle(ctx: Ctx, p: dict[str, Any]) -> Screen:
    from .account import check_dm

    active = "fuel" in await ctx.db.user_subscriptions(ctx.user_id)
    await ctx.db.set_subscription(ctx.user_id, "fuel", not active)
    toast = "Weekly fuel DMs off."
    if not active:
        toast = "You will get a DM when next week's prices are out."
        if not await check_dm(ctx.bot, ctx.interaction.user):
            toast = "Turned on, but I could not DM you. Allow direct messages so it can arrive."
    screen = await fuel_screen(ctx.bot, ctx.user_id)
    screen.toast = toast
    return screen


@app_commands.command(name="fuel", description="This week's RON95, RON97 and diesel prices, and the change")
async def fuel(interaction: discord.Interaction) -> None:
    bot: Any = interaction.client
    await respond(interaction, lambda: fuel_screen(bot, interaction.user.id))


# ---------------------------------------------------------------------------
# Exchange rates
# ---------------------------------------------------------------------------


def money(value: float) -> str:
    if value >= 100:
        return f"{value:,.2f}"
    if value >= 1:
        return f"{value:,.4f}"
    return f"{value:,.6f}"


async def forex_screen(bot: Any, code: str = "", amount: float | None = None) -> Screen:
    sheet, fetched_at, stale = await bot.extras.forex()
    footer = updated_footer(
        f"Bank Negara Malaysia, {sheet.session[:2]}:{sheet.session[2:]} session" if sheet.session else "Bank Negara Malaysia",
        fetched_at,
        stale,
    )
    code = code.upper().strip()
    if code:
        rate = sheet.rates.get(code)
        if rate is None or rate.per_one is None:
            return message_screen(f"Bank Negara does not quote {code}.", "Unknown currency")
        amount = amount if amount and amount > 0 else 1.0
        body = embed(
            f"💱 {rate.name} ({rate.code})",
            f"**{money(amount)} {rate.code} = RM{money(amount * rate.per_one)}**\n"
            f"**RM{money(amount)} = {money(amount / rate.per_one)} {rate.code}**",
            GREEN,
            footer=footer,
        )
        unit = f"per {rate.unit} {rate.code}" if rate.unit != 1 else f"per {rate.code}"
        if rate.buying is not None:
            add_field(body, "Bank buys", f"RM{money(rate.buying)} {unit}", inline=True)
        if rate.selling is not None:
            add_field(body, "Bank sells", f"RM{money(rate.selling)} {unit}", inline=True)
        if rate.middle is not None:
            add_field(body, "Middle", f"RM{money(rate.middle)} {unit}", inline=True)
        add_field(body, "Rate date", rate.date, inline=True)
        return Screen(
            embeds=[body],
            rows=[
                [
                    Button("Refresh", "fx.show", {"c": code, "a": amount}, emoji="🔄"),
                    Button("All currencies", "fx.show", {}, emoji="💱"),
                ]
            ],
        )

    lines = []
    for major in MAJOR_CURRENCIES:
        rate = sheet.rates.get(major)
        if rate is None or rate.per_one is None:
            continue
        if rate.per_one < 0.1:
            lines.append(f"**{major}** RM{money(rate.per_one * 100)} per 100, {rate.name}")
        else:
            lines.append(f"**{major}** RM{money(rate.per_one)}, {rate.name}")
    body = embed(
        "💱 Ringgit exchange rates",
        "\n".join(lines) + f"\n\nFor any other currency, or to convert an amount, use {mention(bot, 'forex')}.",
        GREEN,
        footer=footer,
    )
    return Screen(
        embeds=[body],
        rows=[
            Select(
                "Open a currency",
                [
                    Option(f"{c} {CURRENCY_NAMES.get(c, '')}", "fx.show", {"c": c})
                    for c in sorted(sheet.rates)
                ][:25],
            ),
            [Button("Refresh", "fx.show", {}, emoji="🔄")],
        ],
    )


@action("fx.show")
async def _forex_show(ctx: Ctx, p: dict[str, Any]) -> Screen:
    amount = p.get("a")
    return await forex_screen(ctx.bot, str(p.get("c", "")), float(amount) if amount else None)


async def currency_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    needle = current.strip().lower()
    return [
        app_commands.Choice(name=f"{code} {name}", value=code)
        for code, name in sorted(CURRENCY_NAMES.items())
        if needle in code.lower() or needle in name.lower()
    ][:25]


@app_commands.command(name="forex", description="Ringgit exchange rates from Bank Negara, with conversion")
@app_commands.describe(currency="Leave empty for the major currencies", amount="An amount to convert, both ways")
@app_commands.autocomplete(currency=currency_autocomplete)
async def forex(
    interaction: discord.Interaction,
    currency: str | None = None,
    amount: app_commands.Range[float, 0.01, 1_000_000_000.0] | None = None,
) -> None:
    bot: Any = interaction.client
    await respond(interaction, lambda: forex_screen(bot, currency or "", amount))


__all__ = ["prayer", "fuel", "forex", "fuel_table", "Link"]
