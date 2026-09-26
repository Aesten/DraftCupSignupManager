"""Admin checks (spec §3, Admins)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands

from .models import GuildConfig

if TYPE_CHECKING:
    from .bot import DraftCupBot


# What the bot needs in the signup and admin channels. Pin Messages is for the status board; it replaced
# Manage Messages for pinning, so the bot never gets the right to delete other people's messages.
BOT_PERMISSIONS = discord.Permissions(
    view_channel=True,
    send_messages=True,
    embed_links=True,
    attach_files=True,
    read_message_history=True,
    pin_messages=True,
)
BOT_SCOPES = ("bot", "applications.commands")


def invite_url(application_id: int) -> str:
    return discord.utils.oauth_url(application_id, permissions=BOT_PERMISSIONS, scopes=BOT_SCOPES)


def is_admin(member: discord.Member | discord.User, config: GuildConfig) -> bool:
    if not isinstance(member, discord.Member):
        return False
    if member.guild_permissions.manage_guild:
        return True
    if member.id in config.admin_user_ids:
        return True
    return any(role.id in config.admin_role_ids for role in member.roles)


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
            raise NotAdmin("Only organisers can use this command.")
        return True

    return app_commands.check(predicate)
