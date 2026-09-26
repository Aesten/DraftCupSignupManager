"""Admin checks (spec §3, Admins)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands

from .models import GuildConfig

if TYPE_CHECKING:
    from .bot import DraftCupBot


# What the bot needs in the signup and admin channels. Nothing more: it never manages other people's
# messages and doesn't pin its own (/tournament panel and /captain list-pending repost them).
BOT_PERMISSIONS = discord.Permissions(
    view_channel=True,
    send_messages=True,
    embed_links=True,
    attach_files=True,
    read_message_history=True,
)
BOT_SCOPES = ("bot", "applications.commands")


def invite_url(application_id: int) -> str:
    return discord.utils.oauth_url(application_id, permissions=BOT_PERMISSIONS, scopes=BOT_SCOPES)


def is_admin(member: discord.Member | discord.User, config: GuildConfig) -> bool:
    """Organisers are the members who can see the admin channel, plus anyone with Manage Server
    (so /setup is always possible). Control who organises with the channel's own permissions."""
    if not isinstance(member, discord.Member):
        return False
    if member.guild_permissions.manage_guild:
        return True
    if config.admin_channel_id is None:
        return False
    channel = member.guild.get_channel(config.admin_channel_id)
    return channel is not None and channel.permissions_for(member).view_channel


class NotAdmin(app_commands.CheckFailure):
    pass


async def interaction_is_admin(interaction: discord.Interaction[DraftCupBot]) -> bool:
    if interaction.guild is None:
        return False
    config = await interaction.client.db.get_config(interaction.guild.id)
    return is_admin(interaction.user, config)


def admin_only():
    """Slash command check: the user must be an organiser of this server."""

    async def predicate(interaction: discord.Interaction[DraftCupBot]) -> bool:
        if not await interaction_is_admin(interaction):
            raise NotAdmin("Only organisers (members who can see the admin channel) can use this command.")
        return True

    return app_commands.check(predicate)


def server_manager_only():
    """Slash command check: Manage Server, for /setup (it decides who the organisers are)."""

    async def predicate(interaction: discord.Interaction[DraftCupBot]) -> bool:
        member = interaction.user
        if not isinstance(member, discord.Member) or not member.guild_permissions.manage_guild:
            raise NotAdmin("Only members with the Manage Server permission can run /setup.")
        return True

    return app_commands.check(predicate)
