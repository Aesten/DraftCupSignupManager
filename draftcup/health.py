"""Checks that the bot can actually use the channels it was given."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord

from .models import GuildConfig

if TYPE_CHECKING:
    from .bot import DraftCupBot

# Permission attribute → name shown in Discord's channel settings.
_BASE = {
    "view_channel": "View Channel",
    "send_messages": "Send Messages",
    "embed_links": "Embed Links",
    "attach_files": "Attach Files",
    "read_message_history": "Read Message History",
}
SIGNUP_CHANNEL_PERMISSIONS = _BASE
ADMIN_CHANNEL_PERMISSIONS = _BASE


def missing_permissions(channel: discord.abc.GuildChannel, me: discord.Member, needed: dict[str, str]) -> list[str]:
    permissions = channel.permissions_for(me)
    return [label for attr, label in needed.items() if not getattr(permissions, attr)]


def bot_role_name(me: discord.Member) -> str:
    """The role to grant permissions to: the bot's own managed role, else its top role."""
    managed = [role for role in me.roles if role.managed]
    role = managed[0] if managed else me.top_role
    return f"@{role.name}"


def describe(channel: discord.abc.GuildChannel, me: discord.Member, missing: list[str]) -> str:
    return f"#{channel.name}: allow {', '.join(missing)} for {bot_role_name(me)} (or for {me.display_name})"


def channel_problems(bot: DraftCupBot, config: GuildConfig) -> list[str]:
    """Readable problems with the configured channels, from the cache (no API calls)."""
    guild = bot.get_guild(config.guild_id)
    if guild is None or guild.me is None:
        return []
    problems = []
    for channel_id, needed, label in (
        (config.signup_channel_id, SIGNUP_CHANNEL_PERMISSIONS, "signup channel"),
        (config.admin_channel_id, ADMIN_CHANNEL_PERMISSIONS, "admin channel"),
    ):
        if channel_id is None:
            continue
        channel = guild.get_channel(channel_id)
        if channel is None:
            problems.append(f"the {label} no longer exists; run /setup again")
            continue
        missing = missing_permissions(channel, guild.me, needed)
        if missing:
            problems.append(f"{label} {describe(channel, guild.me, missing)}")
    return problems
