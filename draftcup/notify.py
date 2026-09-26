"""Messages to the admin channel (spec §6.3)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord

if TYPE_CHECKING:
    from .bot import DraftCupBot

log = logging.getLogger(__name__)


async def admin_channel(bot: DraftCupBot, guild_id: int) -> discord.abc.Messageable | None:
    config = await bot.db.get_config(guild_id)
    if config.admin_channel_id is None:
        return None
    channel = bot.get_channel(config.admin_channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(config.admin_channel_id)
        except discord.HTTPException:
            log.warning("Admin channel %s of guild %s is unreachable", config.admin_channel_id, guild_id)
            return None
    return channel if isinstance(channel, discord.abc.Messageable) else None


async def notify_admins(bot: DraftCupBot, guild_id: int, text: str) -> None:
    """Posts one line in the admin channel. Mentions render but never ping anyone."""
    channel = await admin_channel(bot, guild_id)
    if channel is None:
        return
    try:
        await channel.send(text, allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException:
        log.exception("Could not post in the admin channel of guild %s", guild_id)


_FIELD_NAMES = {
    "nickname": "nickname",
    "steam_url": "Steam link",
    "player_class": "class",
    "highest_division": "division",
    "igl": "IGL",
}


def signup_change_line(
    user_id: int, actor_id: int, kind: str, details: dict, player_class: str = "", division: str = ""
) -> str:
    """One admin-channel line describing a signup change (the change-log kinds of db.py)."""
    nickname = details.get("nickname", "?")
    role = details.get("role", "?")
    if kind == "signup":
        line = f"🆕 <@{user_id}> signed up as **{role}**: **{nickname}** ({player_class}, division {division or '—'})"
    elif kind == "edit":
        changed = ", ".join(_FIELD_NAMES.get(name, name) for name in details.get("changed", [])) or "agreement renewed"
        line = f"✏️ <@{user_id}> edited their {role} signup **{nickname}** (changed: {changed})"
    elif kind == "switch":
        line = f"🔁 <@{user_id}> switched from {details.get('from_role', '?')} to **{role}**: **{nickname}**"
    elif kind == "withdraw":
        line = f"❌ <@{user_id}> withdrew their {role} signup **{nickname}**"
    else:
        line = f"ℹ️ <@{user_id}>: {kind} ({nickname})"
    if details.get("captain_reset_from"):
        line += f". Captain status reset from **{details['captain_reset_from']}** to **pending**"
    if actor_id != user_id:
        line += f" (by <@{actor_id}>)"
    return line
