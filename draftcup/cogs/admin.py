"""Setup and configuration commands (spec §7): /setup, /config, /admins, /signups."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable

import discord
from discord import app_commands
from discord.ext import commands

from ..db import utcnow
from ..models import Format, GuildConfig, State
from ..notify import notify_admins
from ..permissions import admin_only
from ..timeutil import INPUT_HINT, discord_ts, format_local, parse_local, zone
from ..views import signup_post

if TYPE_CHECKING:
    from ..bot import DraftCupBot

log = logging.getLogger(__name__)

MAX_DIVISIONS = 10


# ------------------------------------------------------------------- /config set


def _int_between(low: int, high: int) -> Callable[[str, GuildConfig], int]:
    def parse(text: str, _: GuildConfig) -> int:
        try:
            value = int(text)
        except ValueError as exc:
            raise ValueError("Enter a whole number.") from exc
        if not low <= value <= high:
            raise ValueError(f"Must be between {low} and {high}.")
        return value

    return parse


def _parse_title(text: str, _: GuildConfig) -> str:
    title = text.strip()
    if not 1 <= len(title) <= 80:
        raise ValueError("The title must be 1–80 characters long.")
    return title


def _parse_format(text: str, _: GuildConfig) -> Format:
    try:
        return Format(text.strip())
    except ValueError as exc:
        raise ValueError("Format must be `captainPick` or `randomPick`.") from exc


def _parse_bool(text: str, _: GuildConfig) -> bool:
    value = text.strip().lower()
    if value in ("yes", "true", "on", "1"):
        return True
    if value in ("no", "false", "off", "0"):
        return False
    raise ValueError("Answer `yes` or `no`.")


def _parse_timezone(text: str, _: GuildConfig) -> str:
    zone(text.strip())
    return text.strip()


def _parse_date(text: str, config: GuildConfig) -> datetime | None:
    if text.strip().lower() == "none":
        return None
    return parse_local(text, config.timezone)


def _parse_url(text: str, _: GuildConfig) -> str | None:
    value = text.strip()
    if value.lower() == "none":
        return None
    if not value.startswith(("https://", "http://")):
        raise ValueError("Enter a link starting with `https://`, or `none`.")
    return value


CONFIG_KEYS: dict[str, tuple[Callable[[str, GuildConfig], Any], str]] = {
    "title": (_parse_title, "Tournament title (max 80 chars)"),
    "format": (_parse_format, "captainPick or randomPick"),
    "captains_per_division": (_int_between(2, 20), "Teams per division"),
    "team_size": (_int_between(5, 10), "Players per team, not counting the captain"),
    "division_count": (_int_between(1, MAX_DIVISIONS), "Number of divisions"),
    "half_budget_cap": (_parse_bool, "yes/no"),
    "timezone": (_parse_timezone, "IANA timezone, e.g. Europe/Paris"),
    "tournament_date": (_parse_date, f"{INPUT_HINT} or none"),
    "auction_date": (_parse_date, f"{INPUT_HINT} or none"),
    "closes_at": (_parse_date, INPUT_HINT),
    "rules_url": (_parse_url, "https://… or none"),
}


def config_embed(config: GuildConfig) -> discord.Embed:
    def date(value: datetime | None) -> str:
        if value is None:
            return "*not set*"
        return f"`{format_local(value, config.timezone)}` ({discord_ts(value, 'R')})"

    embed = discord.Embed(title=f"Configuration: {config.title}", colour=discord.Colour.blurple())
    embed.add_field(name="format", value=config.format.value)
    embed.add_field(name="captains_per_division", value=str(config.captains_per_division))
    embed.add_field(name="team_size", value=str(config.team_size))
    embed.add_field(name="division_count", value=str(config.division_count))
    embed.add_field(name="half_budget_cap", value="yes" if config.half_budget_cap else "no")
    embed.add_field(name="timezone", value=config.timezone)
    embed.add_field(name="tournament_date", value=date(config.tournament_date))
    embed.add_field(name="auction_date", value=date(config.auction_date))
    embed.add_field(name="closes_at", value=date(config.closes_at))
    embed.add_field(name="rules_url", value=config.rules_url or "*not set*", inline=False)
    embed.add_field(
        name="Channels",
        value=(
            f"Signups: {f'<#{config.signup_channel_id}>' if config.signup_channel_id else '*not set*'}\n"
            f"Admin: {f'<#{config.admin_channel_id}>' if config.admin_channel_id else '*not set*'}"
        ),
        inline=False,
    )
    embed.add_field(
        name="Admins",
        value=(
            "Roles: " + (", ".join(f"<@&{r}>" for r in config.admin_role_ids) or "none") + "\n"
            "Users: " + (", ".join(f"<@{u}>" for u in config.admin_user_ids) or "none") + "\n"
            "Anyone with *Manage Server* is also an admin."
        ),
        inline=False,
    )
    return embed


def missing_for_opening(config: GuildConfig) -> list[str]:
    missing = []
    if config.signup_channel_id is None or config.admin_channel_id is None:
        missing.append("channels (run `/setup`)")
    if config.tournament_date is None:
        missing.append("`tournament_date`")
    if config.auction_date is None:
        missing.append("`auction_date`")
    return missing


FORMAT_CHOICES = [app_commands.Choice(name=f.label, value=f.value) for f in Format]


class AdminCog(commands.Cog):
    def __init__(self, bot: DraftCupBot) -> None:
        self.bot = bot

    # ------------------------------------------------------------------ /setup

    @app_commands.command(name="setup", description="Register the signup and admin channels, and post the signup post.")
    @app_commands.guild_only()
    @admin_only()
    async def setup_command(
        self,
        interaction: discord.Interaction[DraftCupBot],
        signup_channel: discord.TextChannel,
        admin_channel: discord.TextChannel,
    ) -> None:
        assert interaction.guild is not None
        await interaction.response.defer(ephemeral=True)
        guild_id = interaction.guild.id
        old = await self.bot.db.get_config(guild_id)
        await self.bot.db.update_config(
            guild_id, signup_channel_id=signup_channel.id, admin_channel_id=admin_channel.id
        )
        moved = old.signup_channel_id != signup_channel.id
        try:
            await signup_post.post_or_refresh(self.bot, guild_id, repost=moved)
        except discord.Forbidden:
            await interaction.followup.send(
                f"I can't post in {signup_channel.mention}. I need *View Channel*, *Send Messages* and *Embed Links* there.",
                ephemeral=True,
            )
            return
        if moved and old.signup_channel_id and old.signup_message_id:
            await self._delete_message(old.signup_channel_id, old.signup_message_id)
        await interaction.followup.send(
            f"Signup post is in {signup_channel.mention}, admin messages go to {admin_channel.mention}.",
            ephemeral=True,
        )
        await notify_admins(self.bot, guild_id, f"⚙️ Channels set up by {interaction.user.mention}.")

    async def _delete_message(self, channel_id: int, message_id: int) -> None:
        channel = self.bot.get_channel(channel_id)
        if isinstance(channel, (discord.TextChannel, discord.Thread)):
            try:
                await channel.get_partial_message(message_id).delete()
            except discord.HTTPException:
                pass

    # ------------------------------------------------------------------ /config

    config_group = app_commands.Group(name="config", description="Tournament settings.", guild_only=True)

    @config_group.command(name="show", description="Show the tournament settings.")
    @admin_only()
    async def config_show(self, interaction: discord.Interaction[DraftCupBot]) -> None:
        assert interaction.guild is not None
        config = await self.bot.db.get_config(interaction.guild.id)
        await interaction.response.send_message(embed=config_embed(config), ephemeral=True)

    @config_group.command(name="set", description="Change one tournament setting.")
    @app_commands.describe(key="The setting to change", value="The new value (dates: YYYY-MM-DD HH:MM, server timezone)")
    @app_commands.choices(key=[app_commands.Choice(name=f"{k}: {hint}"[:100], value=k) for k, (_, hint) in CONFIG_KEYS.items()])
    @admin_only()
    async def config_set(self, interaction: discord.Interaction[DraftCupBot], key: str, value: str) -> None:
        assert interaction.guild is not None
        guild_id = interaction.guild.id
        config = await self.bot.db.get_config(guild_id)
        parse, _ = CONFIG_KEYS[key]
        try:
            parsed = parse(value, config)
            if key == "closes_at":
                tournament = await self.bot.db.active_tournament(guild_id)
                if tournament.state is State.OPEN and (parsed is None or parsed <= utcnow()):
                    raise ValueError("Signups are open: the close time must be in the future. Use `/signups close` to close now.")
        except ValueError as exc:
            await interaction.response.send_message(f"❌ `{key}`: {exc}", ephemeral=True)
            return
        await self.bot.db.update_config(guild_id, **{key: parsed})
        await interaction.response.send_message(f"✅ `{key}` updated.", ephemeral=True)
        await signup_post.refresh(self.bot, guild_id)
        await notify_admins(self.bot, guild_id, f"⚙️ {interaction.user.mention} set `{key}` to `{value.strip()}`.")

    # ------------------------------------------------------------------ /admins

    admins_group = app_commands.Group(name="admins", description="Who can manage the tournament.", guild_only=True)

    async def _set_admins(
        self,
        interaction: discord.Interaction[DraftCupBot],
        role: discord.Role | None,
        user: discord.Member | None,
        enabled: bool,
    ) -> None:
        assert interaction.guild is not None
        if role is None and user is None:
            await interaction.response.send_message("Give a role, a user, or both.", ephemeral=True)
            return
        guild_id = interaction.guild.id
        changed = []
        if role is not None:
            await self.bot.db.set_admin_role(guild_id, role.id, enabled)
            changed.append(role.mention)
        if user is not None:
            await self.bot.db.set_admin_user(guild_id, user.id, enabled)
            changed.append(user.mention)
        verb = "added to" if enabled else "removed from"
        text = f"{' and '.join(changed)} {verb} the admins."
        await interaction.response.send_message(f"✅ {text}", ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        await notify_admins(self.bot, guild_id, f"⚙️ {interaction.user.mention}: {text}")

    @admins_group.command(name="add", description="Give admin rights to a role and/or a user.")
    @admin_only()
    async def admins_add(
        self, interaction: discord.Interaction[DraftCupBot], role: discord.Role | None = None, user: discord.Member | None = None
    ) -> None:
        await self._set_admins(interaction, role, user, True)

    @admins_group.command(name="remove", description="Remove admin rights from a role and/or a user.")
    @admin_only()
    async def admins_remove(
        self, interaction: discord.Interaction[DraftCupBot], role: discord.Role | None = None, user: discord.Member | None = None
    ) -> None:
        await self._set_admins(interaction, role, user, False)

    # ----------------------------------------------------------------- /signups

    signups_group = app_commands.Group(name="signups", description="Open and close signups.", guild_only=True)

    async def _open(
        self, interaction: discord.Interaction[DraftCupBot], closes_at: str, format: str | None, reopen: bool
    ) -> None:
        assert interaction.guild is not None
        guild_id = interaction.guild.id
        db = self.bot.db
        config = await db.get_config(guild_id)
        tournament = await db.active_tournament(guild_id)

        expected = State.CLOSED if reopen else State.DRAFT
        if tournament.state is not expected:
            hint = "Use `/signups reopen`." if tournament.state is State.CLOSED else f"Signups are {tournament.state.value}."
            await interaction.response.send_message(f"❌ Can't do that now. {hint}", ephemeral=True)
            return
        missing = missing_for_opening(config)
        if missing:
            await interaction.response.send_message(
                "❌ Set these first (`/config set`, `/setup`): " + ", ".join(missing), ephemeral=True
            )
            return
        try:
            close_time = parse_local(closes_at, config.timezone)
        except ValueError as exc:
            await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
            return
        if close_time <= utcnow():
            await interaction.response.send_message("❌ The close time must be in the future.", ephemeral=True)
            return

        changes: dict[str, Any] = {"closes_at": close_time}
        if format is not None:
            changes["format"] = Format(format)
        config = await db.update_config(guild_id, **changes)
        await db.set_state(tournament.id, State.OPEN)
        verb = "reopened" if reopen else "opened"
        await interaction.response.send_message(
            f"✅ Signups {verb} ({config.format.label}). They close {discord_ts(close_time, 'f')}.", ephemeral=True
        )
        await signup_post.refresh(self.bot, guild_id)
        await notify_admins(
            self.bot, guild_id,
            f"🟢 Signups {verb} by {interaction.user.mention} ({config.format.label}), closing {discord_ts(close_time, 'f')}.",
        )

    @signups_group.command(name="open", description="Open signups for a format, with a close date.")
    @app_commands.describe(format="Auction format", closes_at="When signups close (YYYY-MM-DD HH:MM, server timezone)")
    @app_commands.choices(format=FORMAT_CHOICES)
    @admin_only()
    async def signups_open(self, interaction: discord.Interaction[DraftCupBot], format: str, closes_at: str) -> None:
        await self._open(interaction, closes_at, format, reopen=False)

    @signups_group.command(name="reopen", description="Reopen signups after closing them, with a new close date.")
    @app_commands.describe(closes_at="When signups close (YYYY-MM-DD HH:MM, server timezone)")
    @admin_only()
    async def signups_reopen(self, interaction: discord.Interaction[DraftCupBot], closes_at: str) -> None:
        await self._open(interaction, closes_at, None, reopen=True)

    @signups_group.command(name="close", description="Close signups now, before the scheduled time.")
    @admin_only()
    async def signups_close(self, interaction: discord.Interaction[DraftCupBot]) -> None:
        assert interaction.guild is not None
        guild_id = interaction.guild.id
        tournament = await self.bot.db.active_tournament(guild_id)
        if tournament.state is not State.OPEN:
            await interaction.response.send_message("❌ Signups aren't open.", ephemeral=True)
            return
        await self.bot.db.set_state(tournament.id, State.CLOSED)
        await interaction.response.send_message("✅ Signups closed.", ephemeral=True)
        await signup_post.refresh(self.bot, guild_id)
        await notify_admins(self.bot, guild_id, f"🔒 Signups closed by {interaction.user.mention}.")


async def setup(bot: DraftCupBot) -> None:
    await bot.add_cog(AdminCog(bot))
