"""Admin checks (spec §3, Admins)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands

from .models import GuildConfig

if TYPE_CHECKING:
    from .bot import DraftCupBot


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
