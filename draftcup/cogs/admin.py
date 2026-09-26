"""/setup (channels, once per server) and /tournament new|panel (spec §7)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from .. import actions
from ..actions import ActionError
from ..health import ADMIN_CHANNEL_PERMISSIONS, SIGNUP_CHANNEL_PERMISSIONS, describe, missing_permissions
from ..models import State
from ..permissions import admin_only, server_manager_only
from ..views import signup_post, tournament_panel

if TYPE_CHECKING:
    from ..bot import DraftCupBot

    Interaction = discord.Interaction[DraftCupBot]

log = logging.getLogger(__name__)

_HOW_TO_FIX = (
    "In each channel: **Edit Channel → Permissions → Add members or roles**, pick the bot's role, allow the "
    "permissions above, save, then run `/setup` again."
)


def welcome_embed(signup_channel: discord.abc.GuildChannel) -> discord.Embed:
    embed = discord.Embed(
        title="👋 Draft Cup Signup Manager is here",
        description=(
            "This is the **admin channel**. Anyone who can see it is an organiser and can use the bot's "
            "organiser commands and buttons, so manage access with this channel's permissions.\n\n"
            f"Public signups will be posted in {signup_channel.mention}, but only once you open them."
        ),
        colour=discord.Colour.blurple(),
    )
    embed.add_field(
        name="Getting started",
        value=(
            "1. `/tournament new title:…` posts the tournament message here, with its settings and the "
            "**Open signups** button.\n"
            "2. Captain signups show up here as cards to **Accept** (into a division) or **Reject**.\n"
            "3. `/export` posts the CSV, player list or tournament file here."
        ),
        inline=False,
    )
    embed.add_field(
        name="Commands",
        value=(
            "`/tournament new` · `/tournament panel` (repost the tournament message)\n"
            "`/captain list-pending` · `/captain edit` · `/captain add`\n"
            "`/signup view|edit|add|remove` · `/export`"
        ),
        inline=False,
    )
    return embed


class AdminCog(commands.Cog):
    def __init__(self, bot: DraftCupBot) -> None:
        self.bot = bot

    # ------------------------------------------------------------------ /setup

    @app_commands.command(name="setup", description="Pick the signup and admin channels (Manage Server only).")
    @app_commands.describe(
        signup_channel="Public channel where the signup post goes once signups open",
        admin_channel="Private channel: everyone who can see it is an organiser",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @server_manager_only()
    async def setup_command(self, interaction: Interaction, signup_channel: discord.TextChannel, admin_channel: discord.TextChannel) -> None:
        assert interaction.guild is not None
        guild_id = interaction.guild.id
        me = interaction.guild.me
        problems = [
            describe(channel, me, missing)
            for channel, needed in ((signup_channel, SIGNUP_CHANNEL_PERMISSIONS), (admin_channel, ADMIN_CHANNEL_PERMISSIONS))
            if (missing := missing_permissions(channel, me, needed))
        ]
        if problems:
            await interaction.response.send_message(
                "❌ Nothing was saved: I can't use these channels yet.\n"
                + "\n".join(f"- {p}" for p in problems)
                + f"\n\n{_HOW_TO_FIX}",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)

        db = self.bot.db
        old = await db.get_config(guild_id)
        signup_moved = old.signup_channel_id is not None and old.signup_channel_id != signup_channel.id
        admin_moved = old.admin_channel_id is not None and old.admin_channel_id != admin_channel.id
        changes: dict = {"signup_channel_id": signup_channel.id, "admin_channel_id": admin_channel.id}
        if signup_moved:
            changes["signup_message_id"] = None
        if admin_moved:
            changes["status_message_id"] = None
        await db.update_config(guild_id, **changes)

        welcome = await self.bot.feed.send(guild_id, embed=welcome_embed(signup_channel))
        if welcome is None:
            reason = self.bot.feed.unreachable.get(guild_id, "unknown error, see the bot logs")
            await interaction.followup.send(
                f"❌ The channels are saved, but I couldn't post in {admin_channel.mention}: {reason}.\n{_HOW_TO_FIX}",
                ephemeral=True,
            )
            return

        tournament = await db.active_tournament(guild_id)
        if tournament is not None:
            if admin_moved:
                await tournament_panel.refresh_panel(self.bot, guild_id)
            if signup_moved and tournament.state is not State.DRAFT:
                try:
                    await signup_post.publish(self.bot, guild_id)
                except (discord.HTTPException, RuntimeError):
                    log.exception("Could not move the signup post of guild %s", guild_id)
        await interaction.followup.send(f"✅ Set up. Next: `/tournament new` ({welcome.jump_url}).", ephemeral=True)

    # ------------------------------------------------------------- /tournament

    tournament_group = app_commands.Group(name="tournament", description="The Draft Cup tournament.", guild_only=True)

    @tournament_group.command(name="new", description="Start a tournament and post its message in the admin channel.")
    @app_commands.describe(title="Tournament name, e.g. Draft Cup #13 (used for the export files)")
    @admin_only()
    async def tournament_new(self, interaction: Interaction, title: app_commands.Range[str, 1, 80]) -> None:
        assert interaction.guild is not None
        guild_id = interaction.guild.id
        config = await self.bot.db.get_config(guild_id)
        if config.admin_channel_id is None:
            await interaction.response.send_message("❌ Run `/setup` first.", ephemeral=True)
            return
        current = await self.bot.db.active_tournament(guild_id)
        if current is not None and current.state is State.OPEN:
            await interaction.response.send_message(
                f"❌ Signups of **{config.title}** are open: close them first (🔒 on the tournament message).", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        if current is not None:
            await tournament_panel.retire_panel(self.bot, guild_id, f"{config.title} was archived")
        try:
            await actions.create_tournament(self.bot, guild_id, interaction.user.id, " ".join(title.split()))
        except ActionError as exc:
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)
            return
        message = await tournament_panel.refresh_panel(self.bot, guild_id)
        where = message.jump_url if message else "the admin channel (I couldn't post there, check my permissions)"
        await interaction.followup.send(f"✅ Tournament created: {where}", ephemeral=True)

    @tournament_group.command(name="panel", description="Post the tournament message again (e.g. if it was deleted or scrolled away).")
    @admin_only()
    async def tournament_panel_command(self, interaction: Interaction) -> None:
        assert interaction.guild is not None
        if await self.bot.db.active_tournament(interaction.guild.id) is None:
            await interaction.response.send_message(actions.NO_TOURNAMENT, ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        message = await tournament_panel.refresh_panel(self.bot, interaction.guild.id, repost=True)
        await interaction.followup.send(
            f"✅ Reposted: {message.jump_url}" if message else "❌ I couldn't post in the admin channel.", ephemeral=True
        )


async def setup(bot: DraftCupBot) -> None:
    await bot.add_cog(AdminCog(bot))
