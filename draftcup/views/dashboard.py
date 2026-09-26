"""The admin dashboard: one pinned message in the admin channel with the live status and every
organiser action as a button (spec §6.1)."""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from datetime import datetime
from typing import TYPE_CHECKING, Awaitable, Callable

import discord

from .. import exports, rules
from ..models import CaptainStatus, ExportRecord, ExportType, GuildConfig, Role, Signup, State, Tournament
from ..notify import admin_channel
from ..permissions import interaction_is_admin
from ..timeutil import discord_ts, format_day
from . import signup_post

if TYPE_CHECKING:
    from ..bot import DraftCupBot

    Interaction = discord.Interaction[DraftCupBot]

log = logging.getLogger(__name__)

REFRESH_DELAY_SECONDS = 3


# ------------------------------------------------------------------------ embed


def _setup_field(config: GuildConfig, division_names: list[str]) -> str:
    from ..actions import missing_for_opening

    lines = [
        f"**Format:** {config.format.label} · teams of 1 captain + {config.team_size} players",
        f"**Divisions:** {', '.join(division_names)} · {config.captains_per_division} teams each"
        + (" · half budget cap" if config.half_budget_cap else ""),
        "**Signups close:** "
        + (f"{format_day(config.close_date)} at {config.close_time} ({config.timezone})" if config.close_date else "*not set*"),
        f"**Auction:** {format_day(config.auction_date)} · **Tournament:** {format_day(config.tournament_date)}",
        f"**Rules:** {config.rules_url or '*not set*'}",
    ]
    missing = missing_for_opening(config)
    if missing:
        lines.append(f"⚠️ Before opening signups, set {', '.join(missing)}.")
    return "\n".join(lines)


def _access_field(config: GuildConfig) -> str:
    admins = [f"<@&{r}>" for r in config.admin_role_ids] + [f"<@{u}>" for u in config.admin_user_ids]
    shown = ", ".join(admins[:8]) + (f" +{len(admins) - 8} more" if len(admins) > 8 else "")
    return (
        f"**Channels:** signups <#{config.signup_channel_id}> · admin <#{config.admin_channel_id}>\n"
        f"**Organisers:** {shown or '*only members with Manage Server*'}"
    )


def build_embed(
    config: GuildConfig,
    tournament: Tournament,
    signups: list[Signup],
    division_names: list[str],
    latest_exports: dict[ExportType, ExportRecord],
) -> discord.Embed:
    if tournament.state is State.OPEN:
        state = f"🟢 **Signups open**, closing {discord_ts(config.closes_at, 'R')}"
        colour = discord.Colour.green()
    elif tournament.state is State.CLOSED:
        state = "🔒 **Signups closed**"
        colour = discord.Colour.red()
    else:
        state = "⏳ **Preparing**: configure below, then press **Open signups**. Nothing is public yet."
        colour = discord.Colour.light_grey()
    embed = discord.Embed(title=f"🛠️ {config.title}", description=state, colour=colour)
    embed.add_field(name="Setup", value=_setup_field(config, division_names), inline=False)
    embed.add_field(name="Access", value=_access_field(config), inline=False)

    players = [s for s in signups if s.role is Role.PLAYER]
    candidates = exports.captain_candidates(signups)
    status_counts = Counter(s.captain_status for s in candidates)
    pool = exports.pool(signups)
    classes = Counter(s.player_class for s in pool)
    embed.add_field(
        name="Signups",
        value=(
            f"Players: **{len(players)}** · Captain candidates: **{len(candidates)}** "
            f"({status_counts[CaptainStatus.PENDING]} pending · {status_counts[CaptainStatus.PICKED]} picked · "
            f"{status_counts[CaptainStatus.POOL]} to pool)\n"
            f"Pool: **{len(pool)}** · " + " · ".join(f"{rules.CLASS_LABELS[c]} {classes[c]}" for c in rules.CLASSES)
        ),
        inline=False,
    )

    lines = []
    for row in exports.coverage(config, signups):
        captains_ok = "✅" if row.captains_available >= row.captains_needed else "⚠️"
        players_gap = row.players_available - row.players_needed
        players_ok = "✅" if players_gap >= 0 else f"⚠️ ({players_gap})"
        lines.append(
            f"**{row.divisions} div.**: captains {row.captains_available}/{row.captains_needed} {captains_ok}"
            f" · players {row.players_available}/{row.players_needed} {players_ok}"
        )
    picked = Counter(s.division_index for s in candidates if s.captain_status is CaptainStatus.PICKED)
    lines.append(
        "Picked: " + " · ".join(
            f"{name} {picked[i]}/{config.captains_per_division}" for i, name in enumerate(division_names, start=1)
        )
    )
    embed.add_field(
        name="Coverage",
        value="\n".join(lines) + "\n-# Captain candidates beyond the captains needed are counted as players.",
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
    embed.add_field(name="Exports", value="\n".join(export_lines), inline=False)
    embed.set_footer(text=f"Revision {tournament.revision} · updated")
    embed.timestamp = datetime.now().astimezone()
    return embed


# ------------------------------------------------------------------------ view


def _button(
    label: str, key: str, style: discord.ButtonStyle, row: int, emoji: str | None = None
) -> discord.ui.Button:
    return discord.ui.Button(label=label, custom_id=f"draftcup:dash:{key}", style=style, row=row, emoji=emoji)


class DashboardView(discord.ui.View):
    """The dashboard buttons. `state` picks the open/close button; None (the instance registered at
    startup to handle clicks on every server's dashboard) includes all of them."""

    def __init__(self, state: State | None = None) -> None:
        super().__init__(timeout=None)
        grey, blue = discord.ButtonStyle.secondary, discord.ButtonStyle.primary
        green, red = discord.ButtonStyle.success, discord.ButtonStyle.danger
        buttons = [
            _button("Tournament", "settings", grey, 0, "⚙️"),
            _button("Dates", "dates", grey, 0, "📅"),
            _button("Divisions", "divisions", grey, 0, "🏷️"),
            _button("Organisers", "admins", grey, 0, "👮"),
            _button("More settings", "advanced", grey, 0, "🔧"),
        ]
        if state in (None, State.DRAFT):
            buttons.append(_button("Open signups", "open", green, 1, "🟢"))
        if state in (None, State.OPEN):
            buttons.append(_button("Close signups", "close", red, 1, "🔒"))
        if state in (None, State.CLOSED):
            buttons.append(_button("Reopen signups", "reopen", green, 1, "🟢"))
        buttons += [
            _button("Captains", "captains", blue, 1, "🎖️"),
            _button("Manage a signup", "signup", blue, 1, "🔎"),
            _button("Export CSV", "export:csv", grey, 2, "📄"),
            _button("Export player list", "export:players", grey, 2, "📄"),
            _button("Export tournament file", "export:tournament", grey, 2, "📦"),
            _button("New tournament", "reset", red, 3, "🗃️"),
        ]
        for button in buttons:
            button.callback = self._callback_for(button.custom_id)
            self.add_item(button)

    @staticmethod
    def _callback_for(custom_id: str) -> Callable[[Interaction], Awaitable[None]]:
        key = custom_id.removeprefix("draftcup:dash:")

        async def callback(interaction: Interaction) -> None:
            if interaction.guild is None:
                return
            if not await interaction_is_admin(interaction):
                await interaction.response.send_message("Only organisers can use the dashboard.", ephemeral=True)
                return
            from . import dashboard_actions  # imports this module

            await dashboard_actions.HANDLERS[key](interaction)

        return callback


# --------------------------------------------------------------------- refresh


async def refresh_dashboard(bot: DraftCupBot, guild_id: int, *, repost: bool = False) -> discord.Message | None:
    """Edits the dashboard in place, or posts and pins it when it's missing (or `repost` is set)."""
    config = await bot.db.get_config(guild_id)
    channel = await admin_channel(bot, guild_id)
    if channel is None:
        return None
    tournament = await bot.db.active_tournament(guild_id)
    signups = await bot.db.list_active_signups(tournament.id)
    names = await bot.db.division_names(tournament.id, config.division_count)
    latest = await bot.db.latest_exports(tournament.id)
    embed = build_embed(config, tournament, signups, names, latest)
    view = DashboardView(tournament.state)

    if config.status_message_id is not None and not repost:
        try:
            return await channel.get_partial_message(config.status_message_id).edit(embed=embed, view=view)
        except discord.NotFound:
            log.info("Dashboard of guild %s was deleted, posting a new one", guild_id)
    message = await bot.feed.send(guild_id, embed=embed, view=view)
    if message is None:
        return None
    await bot.db.update_config(guild_id, status_message_id=message.id)
    try:
        await message.pin(reason="Draft Cup dashboard")
    except discord.HTTPException:
        log.info("Could not pin the dashboard in guild %s (missing Pin Messages?)", guild_id)
    return message


class Refresher:
    """Coalesces refresh requests, so a burst of changes makes one edit of the dashboard and of the
    public post per server."""

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
            await refresh_dashboard(self.bot, guild_id)
        except Exception:
            log.exception("Dashboard refresh failed for guild %s", guild_id)
        await signup_post.refresh(self.bot, guild_id)
