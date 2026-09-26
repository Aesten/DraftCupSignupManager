"""The pinned status board in the admin channel (spec §6.1)."""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from datetime import datetime
from typing import TYPE_CHECKING

import discord

from .. import exports, rules
from ..models import CaptainStatus, ExportRecord, ExportType, GuildConfig, Role, Signup, State, Tournament
from ..notify import admin_channel
from ..timeutil import discord_ts

if TYPE_CHECKING:
    from ..bot import DraftCupBot

log = logging.getLogger(__name__)

REFRESH_DELAY_SECONDS = 3


def build_embed(
    config: GuildConfig,
    tournament: Tournament,
    signups: list[Signup],
    division_names: list[str],
    latest_exports: dict[ExportType, ExportRecord],
) -> discord.Embed:
    if tournament.state is State.OPEN:
        state = f"🟢 Open, closes {discord_ts(config.closes_at, 'R')} ({discord_ts(config.closes_at, 'f')})"
    elif tournament.state is State.CLOSED:
        state = "🔒 Closed"
    else:
        state = "⏳ Draft (not open yet)"
    embed = discord.Embed(
        title=f"📊 {config.title}: status",
        description=f"{state}\nFormat: **{config.format.label}** · Teams of 1 captain + {config.team_size} players",
        colour=discord.Colour.gold(),
    )

    players = [s for s in signups if s.role is Role.PLAYER]
    candidates = exports.captain_candidates(signups)
    status_counts = Counter(s.captain_status for s in candidates)
    embed.add_field(
        name="Signups",
        value=(
            f"Players: **{len(players)}**\n"
            f"Captain candidates: **{len(candidates)}** "
            f"({status_counts[CaptainStatus.PENDING]} pending · {status_counts[CaptainStatus.PICKED]} picked · "
            f"{status_counts[CaptainStatus.POOL]} to pool)"
        ),
        inline=False,
    )

    pool = exports.pool(signups)
    classes = Counter(s.player_class for s in pool)
    embed.add_field(
        name=f"Pool: {len(pool)}",
        value=" · ".join(f"{rules.CLASS_LABELS[c]} {classes[c]}" for c in rules.CLASSES),
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
    embed.add_field(
        name="Coverage",
        value="\n".join(lines) + "\n-# Captain candidates beyond the captains needed are counted as players.",
        inline=False,
    )

    picked = Counter(s.division_index for s in candidates if s.captain_status is CaptainStatus.PICKED)
    embed.add_field(
        name="Divisions",
        value="\n".join(
            f"{name}: {picked[i]}/{config.captains_per_division}" for i, name in enumerate(division_names, start=1)
        ),
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
    embed.set_footer(text=f"Revision {tournament.revision}")
    embed.timestamp = datetime.now().astimezone()
    return embed


async def refresh_board(bot: DraftCupBot, guild_id: int, *, repost: bool = False) -> None:
    """Edits the board in place, or posts and pins it when it's missing."""
    config = await bot.db.get_config(guild_id)
    channel = await admin_channel(bot, guild_id)
    if channel is None:
        return
    tournament = await bot.db.active_tournament(guild_id)
    signups = await bot.db.list_active_signups(tournament.id)
    names = await bot.db.division_names(tournament.id, config.division_count)
    latest = await bot.db.latest_exports(tournament.id)
    embed = build_embed(config, tournament, signups, names, latest)

    if config.status_message_id is not None and not repost:
        try:
            await channel.get_partial_message(config.status_message_id).edit(embed=embed)
            return
        except discord.NotFound:
            log.info("Status board of guild %s was deleted, posting a new one", guild_id)
    message = await bot.feed.send(guild_id, embed=embed)
    if message is None:
        return
    await bot.db.update_config(guild_id, status_message_id=message.id)
    try:
        await message.pin(reason="Draft Cup status board")
    except discord.HTTPException:
        log.info("Could not pin the status board in guild %s (missing Manage Messages?)", guild_id)


class BoardRefresher:
    """Coalesces refresh requests so a burst of changes makes one edit per server."""

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
            await refresh_board(self.bot, guild_id)
        except Exception:
            log.exception("Status board refresh failed for guild %s", guild_id)
