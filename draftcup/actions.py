"""Tournament-level actions shared by dashboard buttons, slash commands and the scheduler."""

from __future__ import annotations

import io
import logging
from typing import TYPE_CHECKING, Any

import discord

from . import events, exports
from .db import utcnow
from .health import channel_problems
from .models import ExportType, GuildConfig, State
from .timeutil import closing_moment, discord_ts, format_day
from .views import captain_card, signup_post

if TYPE_CHECKING:
    from .bot import DraftCupBot

log = logging.getLogger(__name__)

# Settings that change the content of exported files: changing them makes exports stale (spec §9.4).
EXPORT_KEYS = {"title", "format", "team_size", "half_budget_cap", "division_count"}


class ActionError(Exception):
    """An action that can't run now; the message is shown to the organiser."""


def missing_for_opening(config: GuildConfig) -> list[str]:
    missing = []
    if config.signup_channel_id is None or config.admin_channel_id is None:
        missing.append("the channels (`/setup`)")
    if config.close_date is None:
        missing.append("the signup close date")
    elif config.closes_at is not None and config.closes_at <= utcnow():
        missing.append("a signup close date in the future")
    if config.auction_date is None:
        missing.append("the auction date")
    if config.tournament_date is None:
        missing.append("the tournament date")
    return missing


def close_text(config: GuildConfig) -> str:
    return f"{format_day(config.close_date)} at {config.close_time} ({config.timezone}), {discord_ts(config.closes_at, 'R')}"


# ------------------------------------------------------------------- settings


async def update_settings(bot: DraftCupBot, guild_id: int, actor_id: int, changes: dict[str, Any], labels: dict[str, str]) -> list[str]:
    """Saves changed settings and runs what follows. `labels` gives a readable value per key.

    Returns the readable list of what changed (empty when nothing did).
    """
    config = await bot.db.get_config(guild_id)
    changes = {key: value for key, value in changes.items() if getattr(config, key) != value}
    if not changes:
        return []
    tournament = await bot.db.active_tournament(guild_id)
    if "division_count" in changes:
        used = await bot.db.highest_used_division(tournament.id)
        if changes["division_count"] < used:
            raise ActionError(f"Captains are picked in division {used}: move them before removing that division.")
    if (
        tournament.state is State.OPEN
        and changes.keys() & {"close_date", "close_time", "timezone"}
        and changes.get("close_date", config.close_date) is not None
    ):
        preview = closing_moment(
            changes.get("close_date", config.close_date),
            changes.get("close_time", config.close_time),
            changes.get("timezone", config.timezone),
        )
        if preview <= utcnow():
            raise ActionError("Signups are open: the new close time would already be past. Close signups instead.")

    await bot.db.update_config(guild_id, **changes)
    described = [f"{key.replace('_', ' ')} → {labels.get(key, str(value))}" for key, value in changes.items()]
    for key in changes.keys() & EXPORT_KEYS:
        await bot.db.record_config_change(tournament.id, actor_id, key, labels.get(key, str(changes[key])))
    await bot.feed.line(guild_id, f"⚙️ <@{actor_id}> changed " + "; ".join(described))
    await events.tournament_changed(bot, guild_id)
    if "division_count" in changes:
        await captain_card.refresh_all_cards(bot, guild_id)
    return described


async def rename_divisions(bot: DraftCupBot, guild_id: int, actor_id: int, names: list[str]) -> list[str]:
    config = await bot.db.get_config(guild_id)
    tournament = await bot.db.active_tournament(guild_id)
    current = await bot.db.division_names(tournament.id, config.division_count)
    if len({n.lower() for n in names}) != len(names):
        raise ActionError("Two divisions can't have the same name.")
    changed = []
    for index, (old, new) in enumerate(zip(current, names), start=1):
        if old != new:
            await bot.db.rename_division(tournament.id, index, new, actor_id)
            changed.append(f"{old} → {new}")
    if changed:
        await bot.feed.line(guild_id, f"⚙️ <@{actor_id}> renamed divisions: " + "; ".join(changed))
        bot.refresher.request(guild_id)
        await captain_card.refresh_all_cards(bot, guild_id)
    return changed


# ------------------------------------------------------------------ lifecycle


async def open_signups(bot: DraftCupBot, guild_id: int, actor_id: int) -> discord.Message | None:
    """Opens (or reopens) signups and publishes the public post. Raises ActionError."""
    config = await bot.db.get_config(guild_id)
    tournament = await bot.db.active_tournament(guild_id)
    if tournament.state is State.OPEN:
        raise ActionError("Signups are already open.")
    missing = missing_for_opening(config)
    if missing:
        raise ActionError("Set these first: " + ", ".join(missing) + ".")
    problems = channel_problems(bot, config)
    if problems:
        raise ActionError("I can't use the channels yet:\n" + "\n".join(f"- {p}" for p in problems))
    await bot.db.set_state(tournament.id, State.OPEN)
    try:
        message = await signup_post.publish(bot, guild_id)
    except (discord.HTTPException, RuntimeError) as exc:
        await bot.db.set_state(tournament.id, tournament.state)
        raise ActionError(f"Couldn't post in the signup channel ({exc}). Check my permissions there.") from exc
    verb = "reopened" if tournament.state is State.CLOSED else "opened"
    await bot.feed.line(
        guild_id,
        f"🟢 Signups {verb} by <@{actor_id}> ({config.format.label}), closing {close_text(config)}. {message.jump_url}",
    )
    bot.refresher.request(guild_id)
    return message


async def close_signups(bot: DraftCupBot, guild_id: int, actor_id: int | None) -> None:
    """Closes signups now; `actor_id` is None for the scheduled close."""
    tournament = await bot.db.active_tournament(guild_id)
    if tournament.state is not State.OPEN:
        raise ActionError("Signups aren't open.")
    await bot.db.set_state(tournament.id, State.CLOSED)
    who = f"by <@{actor_id}>" if actor_id else "(scheduled close time reached)"
    await bot.feed.line(guild_id, f"🔒 Signups closed {who}.")
    await events.tournament_changed(bot, guild_id)


async def reset_tournament(bot: DraftCupBot, guild_id: int, actor_id: int) -> None:
    """Archives the tournament: the next opening starts from an empty signup list and a new public post."""
    tournament = await bot.db.active_tournament(guild_id)
    if tournament.state is State.OPEN:
        raise ActionError("Close signups first.")
    await bot.db.archive_tournament(tournament.id)
    await bot.db.update_config(guild_id, close_date=None, signup_message_id=None)
    await bot.feed.line(guild_id, f"🗃️ <@{actor_id}> archived the tournament. Earlier captain cards no longer work.")
    bot.refresher.request(guild_id)


# -------------------------------------------------------------------- exports


async def export(interaction: discord.Interaction[DraftCupBot], export_type: ExportType) -> None:
    """Builds an export and posts it in the admin channel; answers the interaction ephemerally."""
    assert interaction.guild is not None
    bot, guild_id = interaction.client, interaction.guild.id
    config = await bot.db.get_config(guild_id)
    if config.admin_channel_id is None:
        await interaction.response.send_message("❌ Run `/setup` first: exports are posted in the admin channel.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    tournament = await bot.db.active_tournament(guild_id)
    signups = await bot.db.list_active_signups(tournament.id)
    names = await bot.db.division_names(tournament.id, config.division_count)
    if export_type is ExportType.CSV:
        result = exports.build_csv(config, signups, names)
    elif export_type is ExportType.PLAYERS:
        result = exports.build_player_list(config, signups)
    else:
        result = exports.build_tournament(config, signups, names)

    if result.errors:
        text = "❌ Can't export yet:\n" + "\n".join(f"- {e}" for e in result.errors)
        if result.warnings:
            text += "\n\nAlso:\n" + "\n".join(f"- {w}" for w in result.warnings)
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
        await interaction.followup.send(
            f"❌ Couldn't post the export in the admin channel: {reason}.{problems}", ephemeral=True
        )
        return
    await bot.db.record_export(tournament.id, export_type, tournament.revision, interaction.user.id)
    await interaction.followup.send(f"✅ Posted: {message.jump_url}{warnings}", ephemeral=True)
    bot.refresher.request(guild_id)
