"""Tournament-level actions shared by the tournament message's buttons, slash commands and the scheduler."""

from __future__ import annotations

import io
import logging
from datetime import date
from typing import TYPE_CHECKING, Any

import discord

from . import exports
from .db import utcnow
from .health import channel_problems
from .models import ExportType, GuildConfig, State, Tournament
from .timeutil import closing_moment, discord_ts, format_day, local_today

if TYPE_CHECKING:
    from .bot import DraftCupBot

log = logging.getLogger(__name__)

# Settings that change the content of exported files: changing them makes exports stale (spec §9.4).
EXPORT_KEYS = {"title", "team_size", "division_count"}
NO_TOURNAMENT = "There's no tournament yet. Start one with `/tournament new`."


class ActionError(Exception):
    """An action that can't run now; the message is shown to the organiser."""


async def require_tournament(bot: DraftCupBot, guild_id: int) -> Tournament:
    tournament = await bot.db.active_tournament(guild_id)
    if tournament is None:
        raise ActionError(NO_TOURNAMENT)
    return tournament


def close_text(config: GuildConfig) -> str:
    if config.closes_at is None:
        return "*not set*"
    return f"{discord_ts(config.closes_at, 'f')} ({discord_ts(config.closes_at, 'R')})"


def check_close_day(config: GuildConfig, day: date) -> None:
    """The close moment (day at the close time, Paris time) must be in the future."""
    if day < local_today(config.timezone) or closing_moment(day, config.close_time, config.timezone) <= utcnow():
        raise ActionError(f"{format_day(day)} at {config.close_time} is already past. Pick a later day.")


# --------------------------------------------------------------------- settings


async def update_settings(bot: DraftCupBot, guild_id: int, actor_id: int, changes: dict[str, Any]) -> list[str]:
    """Saves changed tournament settings and runs what follows. Returns what changed, readable."""
    tournament = await require_tournament(bot, guild_id)
    config = await bot.db.get_config(guild_id)
    changes = {key: value for key, value in changes.items() if getattr(config, key) != value}
    if not changes:
        return []
    if "division_count" in changes:
        used = await bot.db.highest_used_division(tournament.id)
        if changes["division_count"] < used:
            raise ActionError(f"Captains are accepted into division {used}: move them (`/captain edit`) before removing it.")
    await bot.db.update_config(guild_id, **changes)
    described = [f"{key.replace('_', ' ')} → {value}" for key, value in changes.items()]
    for key in changes.keys() & EXPORT_KEYS:
        await bot.db.record_config_change(tournament.id, actor_id, key, str(changes[key]))
    await bot.feed.line(guild_id, f"⚙️ <@{actor_id}> changed " + "; ".join(described))
    bot.refresher.request(guild_id)
    if "division_count" in changes:
        from .views import captain_card

        await captain_card.refresh_all_cards(bot, guild_id)
    return described


async def set_close_day(bot: DraftCupBot, guild_id: int, actor_id: int, day: date) -> GuildConfig:
    await require_tournament(bot, guild_id)
    config = await bot.db.get_config(guild_id)
    check_close_day(config, day)
    if config.close_date != day:
        config = await bot.db.update_config(guild_id, close_date=day)
        await bot.feed.line(guild_id, f"📅 <@{actor_id}> set the signup close to {close_text(config)}.")
        bot.refresher.request(guild_id)
    return config


# ------------------------------------------------------------------ lifecycle


async def create_tournament(bot: DraftCupBot, guild_id: int, actor_id: int, title: str) -> Tournament:
    """Archives the current tournament (if closed or never opened) and starts a new one."""
    current = await bot.db.active_tournament(guild_id)
    if current is not None and current.state is State.OPEN:
        raise ActionError("Signups of the current tournament are open: close them first.")
    tournament = await bot.db.create_tournament(guild_id)
    await bot.db.update_config(guild_id, title=title, close_date=None, signup_message_id=None, status_message_id=None)
    archived = " The previous tournament was archived; its captain cards no longer work." if current else ""
    await bot.feed.line(guild_id, f"🆕 <@{actor_id}> started the tournament **{title}**.{archived}")
    return tournament


async def open_signups(bot: DraftCupBot, guild_id: int, actor_id: int, day: date) -> discord.Message:
    """Opens (or reopens) signups until `day` at the close time and publishes the public post."""
    from .views import signup_post

    tournament = await require_tournament(bot, guild_id)
    if tournament.state is State.OPEN:
        raise ActionError("Signups are already open.")
    config = await bot.db.get_config(guild_id)
    check_close_day(config, day)
    if config.signup_channel_id is None:
        raise ActionError("No signup channel: run `/setup` first.")
    problems = channel_problems(bot, config)
    if problems:
        raise ActionError("I can't use the channels yet:\n" + "\n".join(f"- {p}" for p in problems))

    previous_day = config.close_date
    config = await bot.db.update_config(guild_id, close_date=day)
    await bot.db.set_state(tournament.id, State.OPEN)
    try:
        message = await signup_post.publish(bot, guild_id)
    except (discord.HTTPException, RuntimeError) as exc:
        await bot.db.set_state(tournament.id, tournament.state)
        await bot.db.update_config(guild_id, close_date=previous_day)
        raise ActionError(f"Couldn't post in the signup channel ({exc}). Check my permissions there.") from exc
    verb = "reopened" if tournament.state is State.CLOSED else "opened"
    await bot.feed.line(guild_id, f"🟢 Signups {verb} by <@{actor_id}> until {close_text(config)}. {message.jump_url}")
    bot.refresher.request(guild_id)
    return message


async def close_signups(bot: DraftCupBot, guild_id: int, actor_id: int | None) -> None:
    """Closes signups now; `actor_id` is None for the scheduled close."""
    tournament = await require_tournament(bot, guild_id)
    if tournament.state is not State.OPEN:
        raise ActionError("Signups aren't open.")
    await bot.db.set_state(tournament.id, State.CLOSED)
    who = f"by <@{actor_id}>" if actor_id else "(close time reached)"
    await bot.feed.line(guild_id, f"🔒 Signups closed {who}.")
    bot.refresher.request(guild_id)


# -------------------------------------------------------------------- exports


async def export(interaction: discord.Interaction[DraftCupBot], export_type: ExportType) -> None:
    """Builds an export and posts it in the admin channel; answers the interaction ephemerally."""
    assert interaction.guild is not None
    bot, guild_id = interaction.client, interaction.guild.id
    tournament = await bot.db.active_tournament(guild_id)
    if tournament is None:
        await interaction.response.send_message(NO_TOURNAMENT, ephemeral=True)
        return
    config = await bot.db.get_config(guild_id)
    await interaction.response.defer(ephemeral=True, thinking=True)
    signups = await bot.db.list_active_signups(tournament.id)
    names = await bot.db.division_names(tournament.id, config.division_count)
    if export_type is ExportType.CSV:
        result = exports.build_csv(config, signups, names)
    elif export_type is ExportType.PLAYERS:
        result = exports.build_player_list(config, signups)
    else:
        result = exports.build_tournament(config, signups, names)

    if result.errors:
        text = "❌ The tournament file can't be exported yet:\n" + "\n".join(f"- {e}" for e in result.errors)
        if result.warnings:
            text += "\n\nAlso worth checking:\n" + "\n".join(f"- {w}" for w in result.warnings)
        await interaction.followup.send(text, ephemeral=True)
        return

    warnings = "".join(f"\n⚠️ {w}" for w in result.warnings)
    files = [discord.File(io.BytesIO(content), filename=name) for name, content in result.files.items()]
    message = await bot.feed.send(
        guild_id,
        f"📦 **{export_type.label}** exported by {interaction.user.mention} (revision {tournament.revision}).{warnings}",
        files=files,
    )
    if message is None:
        reason = bot.feed.unreachable.get(guild_id, "see the bot logs")
        problems = "".join(f"\n- {p}" for p in channel_problems(bot, config))
        await interaction.followup.send(f"❌ Couldn't post the export in the admin channel: {reason}.{problems}", ephemeral=True)
        return
    await bot.db.record_export(tournament.id, export_type, tournament.revision, interaction.user.id)
    await interaction.followup.send(f"✅ Posted: {message.jump_url}{warnings}", ephemeral=True)
    bot.refresher.request(guild_id)
