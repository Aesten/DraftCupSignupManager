"""/setup: the only configuration command. Everything else is on the admin dashboard (spec §7)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from ..health import ADMIN_CHANNEL_PERMISSIONS, SIGNUP_CHANNEL_PERMISSIONS, describe, missing_permissions
from ..models import State
from ..permissions import admin_only
from ..views import dashboard, signup_post

if TYPE_CHECKING:
    from ..bot import DraftCupBot

log = logging.getLogger(__name__)

_HOW_TO_FIX = (
    "In each channel: **Edit Channel → Permissions → Add members or roles**, pick the bot's role, allow the "
    "permissions above, save, then run `/setup` again."
)


class AdminCog(commands.Cog):
    def __init__(self, bot: DraftCupBot) -> None:
        self.bot = bot

    @app_commands.command(
        name="setup",
        description="Pick the signup and admin channels, and post the organiser dashboard in the admin channel.",
    )
    @app_commands.describe(
        signup_channel="Where the public signup post goes once signups open",
        admin_channel="Private channel for organisers: dashboard, captain cards, notifications, exports",
    )
    @app_commands.guild_only()
    @admin_only()
    async def setup_command(
        self,
        interaction: discord.Interaction[DraftCupBot],
        signup_channel: discord.TextChannel,
        admin_channel: discord.TextChannel,
    ) -> None:
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
        changes: dict = {"signup_channel_id": signup_channel.id, "admin_channel_id": admin_channel.id}
        signup_moved = old.signup_channel_id is not None and old.signup_channel_id != signup_channel.id
        if signup_moved:
            changes["signup_message_id"] = None  # the post follows the channel (see below)
        await db.update_config(guild_id, **changes)

        admin_moved = old.admin_channel_id != admin_channel.id
        message = await dashboard.refresh_dashboard(self.bot, guild_id, repost=admin_moved)
        if message is None:
            reason = self.bot.feed.unreachable.get(guild_id, "unknown error, see the bot logs")
            await interaction.followup.send(
                f"❌ The channels are saved, but I couldn't post the dashboard in {admin_channel.mention}: {reason}.\n"
                f"{_HOW_TO_FIX}",
                ephemeral=True,
            )
            return

        tournament = await db.active_tournament(guild_id)
        if signup_moved and tournament.state is not State.DRAFT:
            # Signups are already public: move the post to the new channel.
            try:
                await signup_post.publish(self.bot, guild_id)
            except (discord.HTTPException, RuntimeError):
                log.exception("Could not move the signup post of guild %s", guild_id)

        await interaction.followup.send(
            f"✅ The dashboard is in {admin_channel.mention}: {message.jump_url}\n"
            f"Nothing is posted in {signup_channel.mention} until you press **Open signups** there.",
            ephemeral=True,
        )
        await self.bot.feed.line(
            guild_id, f"⚙️ <@{interaction.user.id}> set the channels: signups {signup_channel.mention}, admin {admin_channel.mention}."
        )


async def setup(bot: DraftCupBot) -> None:
    await bot.add_cog(AdminCog(bot))
