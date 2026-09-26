"""The tournament message in the admin channel: live status for organisers, with the buttons to
configure the tournament and open or close signups (spec §6.1).

Posted by /tournament new, reposted by /tournament panel, edited in place after every change.
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from datetime import datetime
from typing import TYPE_CHECKING, Any, Awaitable, Callable

import discord

from .. import actions, exports, health, rules
from ..actions import ActionError
from ..models import CaptainStatus, ExportRecord, ExportType, GuildConfig, Role, Signup, State, Tournament
from ..notify import admin_channel
from ..permissions import interaction_is_admin
from ..timeutil import discord_ts, input_day, parse_day
from . import signup_post

if TYPE_CHECKING:
    from ..bot import DraftCupBot

    Interaction = discord.Interaction[DraftCupBot]

log = logging.getLogger(__name__)

REFRESH_DELAY_SECONDS = 3
FORM_TIMEOUT = 15 * 60
TEAM_SIZES = range(5, 11)  # players per team, not counting the captain (auction app limit)
MAX_DIVISIONS = 5


# ------------------------------------------------------------------------ embed


def build_embed(
    config: GuildConfig,
    tournament: Tournament,
    signups: list[Signup],
    division_names: list[str],
    latest_exports: dict[ExportType, ExportRecord],
    channel_problems: list[str] | None = None,
) -> discord.Embed:
    if tournament.state is State.OPEN:
        state = f"🟢 **Signups open** until {actions.close_text(config)}"
        colour = discord.Colour.green()
    elif tournament.state is State.CLOSED:
        state = "🔒 **Signups closed**"
        colour = discord.Colour.red()
    else:
        state = "⏳ **Preparing.** Nothing is public yet: check ⚙️ Settings, then press **Open signups**."
        colour = discord.Colour.light_grey()
    embed = discord.Embed(title=f"🏆 {config.title}", description=state, colour=colour)
    embed.add_field(
        name="Settings",
        value=(
            f"Teams of 1 captain + {config.team_size} players · {config.division_count} "
            f"division{'s' if config.division_count > 1 else ''} of {config.captains_per_division} teams"
        ),
        inline=False,
    )

    players = [s for s in signups if s.role is Role.PLAYER]
    candidates = exports.captain_candidates(signups)
    accepted = [s for s in candidates if s.captain_status is CaptainStatus.PICKED]
    pending = len(candidates) - len(accepted)
    classes = Counter(s.player_class for s in exports.pool(signups))
    embed.add_field(
        name="Signups",
        value=(
            f"Players: **{len(players)}** (" + " · ".join(f"{rules.CLASS_LABELS[c]} {classes[c]}" for c in rules.CLASSES) + ")\n"
            f"Captain signups: **{len(candidates)}** · {len(accepted)} accepted · "
            + (f"**{pending} waiting for a decision** (`/captain list-pending`)" if pending else "none waiting")
        ),
        inline=False,
    )

    picked = Counter(s.division_index for s in accepted)
    lines = [
        "Accepted: " + " · ".join(
            f"{name} {picked[i]}/{config.captains_per_division}" for i, name in enumerate(division_names, start=1)
        )
    ]
    for row in exports.coverage(config, signups):
        captains_ok = "✅" if row.captains_available >= row.captains_needed else "⚠️"
        players_gap = row.players_available - row.players_needed
        players_ok = "✅" if players_gap >= 0 else f"⚠️ ({players_gap})"
        lines.append(
            f"**{row.divisions} div.**: captains {row.captains_available}/{row.captains_needed} {captains_ok}"
            f" · players {row.players_available}/{row.players_needed} {players_ok}"
        )
    embed.add_field(
        name="Coverage",
        value="\n".join(lines) + "\n-# Captain signups beyond the captains needed are counted as players.",
        inline=False,
    )

    export_lines = []
    for export_type in ExportType:
        record = latest_exports.get(export_type)
        if record is None:
            export_lines.append(f"{export_type.label}: never exported")
        elif record.revision >= tournament.revision:
            export_lines.append(f"{export_type.label}: ✅ up to date ({discord_ts(record.time, 'R')})")
        else:
            behind = tournament.revision - record.revision
            export_lines.append(
                f"{export_type.label}: ⚠️ stale, {behind} change{'s' if behind > 1 else ''} since {discord_ts(record.time, 'R')}"
            )
    embed.add_field(name="Exports (`/export`)", value="\n".join(export_lines), inline=False)
    if channel_problems:
        embed.add_field(name="⚠️ Channels", value="\n".join(p[:200] for p in channel_problems)[:1024], inline=False)
    embed.set_footer(text=f"Revision {tournament.revision} · updated")
    embed.timestamp = datetime.now().astimezone()
    return embed


# ------------------------------------------------------------------------ forms


async def _reply(interaction: Interaction, content: str, **kwargs: Any) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(content, ephemeral=True, **kwargs)
    else:
        await interaction.response.send_message(content, ephemeral=True, **kwargs)


class OrganiserModal(discord.ui.Modal):
    async def interaction_check(self, interaction: Interaction) -> bool:  # type: ignore[override]
        if await interaction_is_admin(interaction):
            return True
        await interaction.response.send_message("Only organisers can change this.", ephemeral=True)
        return False

    async def on_error(self, interaction: Interaction, error: Exception) -> None:  # type: ignore[override]
        if isinstance(error, (ActionError, ValueError)):
            await _reply(interaction, f"❌ {error}")
            return
        log.exception("Tournament form failed", exc_info=error)
        await _reply(interaction, "Something went wrong. Check the bot logs.")


def _radio(options: list[tuple[str, str]], current: str) -> discord.ui.RadioGroup:
    return discord.ui.RadioGroup(
        options=[discord.RadioGroupOption(label=label, value=value, default=value == current) for label, value in options]
    )


class SettingsModal(OrganiserModal):
    def __init__(self, config: GuildConfig) -> None:
        super().__init__(title="Tournament settings", timeout=FORM_TIMEOUT)
        self.name = discord.ui.TextInput(default=config.title, max_length=80)
        self.team_size = _radio([(f"{n} players + captain", str(n)) for n in TEAM_SIZES], str(config.team_size))
        self.divisions = _radio(
            [(f"{n} division{'s' if n > 1 else ''}", str(n)) for n in range(1, MAX_DIVISIONS + 1)],
            str(min(config.division_count, MAX_DIVISIONS)),
        )
        self.teams = discord.ui.TextInput(default=str(config.captains_per_division), max_length=2)
        self.add_item(discord.ui.Label(text="Title", component=self.name))
        self.add_item(discord.ui.Label(text="Team size", component=self.team_size))
        self.add_item(discord.ui.Label(text="Divisions", component=self.divisions))
        self.add_item(discord.ui.Label(text="Teams (captains) per division", component=self.teams))

    async def on_submit(self, interaction: Interaction) -> None:  # type: ignore[override]
        title = " ".join(self.name.value.split())
        if not title:
            raise ValueError("The title can't be empty.")
        try:
            teams = int(self.teams.value)
        except ValueError:
            raise ValueError("Teams per division must be a number.") from None
        if not 2 <= teams <= 20:
            raise ValueError("Teams per division must be between 2 and 20.")
        changes = {
            "title": title,
            "team_size": int(self.team_size.value),
            "division_count": int(self.divisions.value),
            "captains_per_division": teams,
        }
        assert interaction.guild is not None
        changed = await actions.update_settings(interaction.client, interaction.guild.id, interaction.user.id, changes)
        await _reply(interaction, "✅ Saved: " + "; ".join(changed) + "." if changed else "Nothing changed.")


class CloseDateModal(OrganiserModal):
    """Asks for the signup close day (DD/MM/YYYY). `mode`: "open", "reopen" or "change"."""

    TITLES = {"open": "Open signups", "reopen": "Reopen signups", "change": "Change the close date"}

    def __init__(self, config: GuildConfig, mode: str) -> None:
        super().__init__(title=self.TITLES[mode], timeout=FORM_TIMEOUT)
        self.mode = mode
        current = config.close_date if mode == "change" else None
        self.day = discord.ui.TextInput(default=input_day(current) or None, placeholder="DD/MM/YYYY", min_length=8, max_length=10)
        self.add_item(discord.ui.Label(
            text="Signups close on",
            description=f"Day as DD/MM/YYYY. Signups close at {config.close_time} Paris time (CET/CEST) that day.",
            component=self.day,
        ))

    async def on_submit(self, interaction: Interaction) -> None:  # type: ignore[override]
        assert interaction.guild is not None
        day = parse_day(self.day.value)
        bot, guild_id = interaction.client, interaction.guild.id
        if self.mode == "change":
            config = await actions.set_close_day(bot, guild_id, interaction.user.id, day)
            await _reply(interaction, f"📅 Signups now close {actions.close_text(config)}.")
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        message = await actions.open_signups(bot, guild_id, interaction.user.id, day)
        config = await bot.db.get_config(guild_id)
        await interaction.followup.send(f"🟢 Signups are open until {actions.close_text(config)}: {message.jump_url}", ephemeral=True)


class ConfirmCloseView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=FORM_TIMEOUT)

    @discord.ui.button(label="Close signups now", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: Interaction, _: discord.ui.Button) -> None:
        if not await interaction_is_admin(interaction):
            await interaction.response.send_message("Only organisers can do this.", ephemeral=True)
            return
        try:
            await actions.close_signups(interaction.client, interaction.guild.id, interaction.user.id)
            text = "🔒 Signups are closed. The public post now says so."
        except ActionError as exc:
            text = f"❌ {exc}"
        await interaction.response.edit_message(content=text, view=None)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: Interaction, _: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="Nothing changed.", view=None)


# ------------------------------------------------------------------------ view


async def _settings(interaction: Interaction, config: GuildConfig, tournament: Tournament) -> None:
    await interaction.response.send_modal(SettingsModal(config))


async def _open(interaction: Interaction, config: GuildConfig, tournament: Tournament) -> None:
    if tournament.state is not State.DRAFT:
        await _reply(interaction, "Signups were already opened for this tournament.")
        return
    await interaction.response.send_modal(CloseDateModal(config, "open"))


async def _reopen(interaction: Interaction, config: GuildConfig, tournament: Tournament) -> None:
    if tournament.state is not State.CLOSED:
        await _reply(interaction, "Signups aren't closed.")
        return
    await interaction.response.send_modal(CloseDateModal(config, "reopen"))


async def _change_date(interaction: Interaction, config: GuildConfig, tournament: Tournament) -> None:
    if tournament.state is not State.OPEN:
        await _reply(interaction, "Signups aren't open.")
        return
    await interaction.response.send_modal(CloseDateModal(config, "change"))


async def _close(interaction: Interaction, config: GuildConfig, tournament: Tournament) -> None:
    if tournament.state is not State.OPEN:
        await _reply(interaction, "Signups aren't open.")
        return
    await _reply(interaction, "**Close signups now?** Only organisers will be able to change signups afterwards.", view=ConfirmCloseView())


HANDLERS: dict[str, Callable[[Interaction, GuildConfig, Tournament], Awaitable[None]]] = {
    "settings": _settings,
    "open": _open,
    "reopen": _reopen,
    "date": _change_date,
    "close": _close,
}


class TournamentView(discord.ui.View):
    """The tournament message's buttons. `state` picks which ones are shown; None (the instance
    registered at startup to handle clicks on every server's message) includes all of them."""

    def __init__(self, state: State | None = None) -> None:
        super().__init__(timeout=None)
        grey, green, red = discord.ButtonStyle.secondary, discord.ButtonStyle.success, discord.ButtonStyle.danger
        buttons = [("Settings", "settings", grey, "⚙️")]
        if state in (None, State.DRAFT):
            buttons.append(("Open signups", "open", green, "🟢"))
        if state in (None, State.OPEN):
            buttons += [("Change close date", "date", grey, "📅"), ("Close signups", "close", red, "🔒")]
        if state in (None, State.CLOSED):
            buttons.append(("Reopen signups", "reopen", green, "🟢"))
        for label, key, style, emoji in buttons:
            button = discord.ui.Button(label=label, custom_id=f"draftcup:dash:{key}", style=style, emoji=emoji)
            button.callback = self._callback_for(key)
            self.add_item(button)

    @staticmethod
    def _callback_for(key: str) -> Callable[[Interaction], Awaitable[None]]:
        async def callback(interaction: Interaction) -> None:
            if interaction.guild is None:
                return
            if not await interaction_is_admin(interaction):
                await interaction.response.send_message("Only organisers can use this.", ephemeral=True)
                return
            bot = interaction.client
            tournament = await bot.db.active_tournament(interaction.guild.id)
            config = await bot.db.get_config(interaction.guild.id)
            if tournament is None or (interaction.message and interaction.message.id != config.status_message_id):
                await _reply(interaction, "This message is outdated. Use `/tournament panel` for the current one.")
                return
            await HANDLERS[key](interaction, config, tournament)

        return callback


# --------------------------------------------------------------------- refresh


async def refresh_panel(bot: DraftCupBot, guild_id: int, *, repost: bool = False) -> discord.Message | None:
    """Edits the tournament message in place, or posts and pins it when it's missing (or `repost`)."""
    tournament = await bot.db.active_tournament(guild_id)
    if tournament is None:
        return None
    config = await bot.db.get_config(guild_id)
    channel = await admin_channel(bot, guild_id)
    if channel is None:
        return None
    signups = await bot.db.list_active_signups(tournament.id)
    names = await bot.db.division_names(tournament.id, config.division_count)
    latest = await bot.db.latest_exports(tournament.id)
    embed = build_embed(config, tournament, signups, names, latest, health.channel_problems(bot, config))
    view = TournamentView(tournament.state)

    if config.status_message_id is not None:
        old = channel.get_partial_message(config.status_message_id)
        if not repost:
            try:
                return await old.edit(embed=embed, view=view)
            except discord.NotFound:
                log.info("Tournament message of guild %s was deleted, posting a new one", guild_id)
        else:
            await retire_panel(bot, guild_id, "reposted below")
    message = await bot.feed.send(guild_id, embed=embed, view=view)
    if message is None:
        return None
    await bot.db.update_config(guild_id, status_message_id=message.id)
    try:
        await message.pin(reason="Draft Cup tournament message")
    except discord.HTTPException:
        log.info("Could not pin the tournament message in guild %s (missing Pin Messages?)", guild_id)
    return message


async def retire_panel(bot: DraftCupBot, guild_id: int, reason: str) -> None:
    """Strips the buttons from the current tournament message and unpins it."""
    config = await bot.db.get_config(guild_id)
    channel = await admin_channel(bot, guild_id)
    if config.status_message_id is None or channel is None:
        return
    old = channel.get_partial_message(config.status_message_id)
    try:
        await old.edit(content=f"-# This message is no longer updated ({reason}).", view=None)
        await old.unpin()
    except discord.HTTPException:
        pass
    await bot.db.update_config(guild_id, status_message_id=None)


class Refresher:
    """Coalesces refresh requests, so a burst of changes makes one edit of the tournament message and
    of the public post per server."""

    def __init__(self, bot: DraftCupBot) -> None:
        self.bot = bot
        self._pending: dict[int, asyncio.Task[None]] = {}

    def request(self, guild_id: int) -> None:
        if guild_id not in self._pending:
            self._pending[guild_id] = asyncio.create_task(self._run(guild_id))

    async def _run(self, guild_id: int) -> None:
        try:
            await asyncio.sleep(REFRESH_DELAY_SECONDS)
        finally:
            # Cleared before refreshing, so a change made during the refresh schedules another one.
            self._pending.pop(guild_id, None)
        try:
            await refresh_panel(self.bot, guild_id)
        except Exception:
            log.exception("Tournament message refresh failed for guild %s", guild_id)
        await signup_post.refresh(self.bot, guild_id)
