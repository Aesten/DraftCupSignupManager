"""Messages to the admin channel (spec §6.3)."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from typing import TYPE_CHECKING, Any

import discord

if TYPE_CHECKING:
    from .bot import DraftCupBot

log = logging.getLogger(__name__)

GROUP_WINDOW_SECONDS = 60
MESSAGE_LIMIT = 2000


async def admin_channel(bot: DraftCupBot, guild_id: int) -> discord.TextChannel | discord.Thread | None:
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
    return channel if isinstance(channel, (discord.TextChannel, discord.Thread)) else None


class AdminFeed:
    """Posts to each server's admin channel.

    Plain lines posted within 60 s of each other are grouped by editing the previous message, so a
    burst of signups doesn't flood the channel. Anything else (cards, files, notices) ends the group,
    which keeps the channel in chronological order.
    """

    def __init__(self, bot: DraftCupBot) -> None:
        self.bot = bot
        self._groups: dict[int, tuple[discord.Message, float]] = {}
        self._locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        # guild ID → why the last post failed; cleared by the next successful post.
        self.unreachable: dict[int, str] = {}

    def _failed(self, guild_id: int, error: discord.HTTPException) -> None:
        if isinstance(error, discord.Forbidden):
            reason = "missing permissions (the bot needs View Channel, Send Messages, Embed Links, Attach Files, Read Message History)"
        elif isinstance(error, discord.NotFound):
            reason = "the channel no longer exists (run /setup again)"
        else:
            log.exception("Could not post in the admin channel of guild %s", guild_id, exc_info=error)
            reason = f"Discord error {error.status}"
        if self.unreachable.get(guild_id) != reason:
            # Logged once per problem instead of once per message.
            log.warning("Can't post in the admin channel of guild %s: %s", guild_id, reason)
        self.unreachable[guild_id] = reason

    def _succeeded(self, guild_id: int) -> None:
        if self.unreachable.pop(guild_id, None) is not None:
            log.info("The admin channel of guild %s works again", guild_id)

    async def line(self, guild_id: int, text: str) -> None:
        """Posts one line, grouped with recent ones. Mentions render but never ping anyone."""
        async with self._locks[guild_id]:
            channel = await admin_channel(self.bot, guild_id)
            if channel is None:
                return
            group = self._groups.get(guild_id)
            if group is not None:
                message, started = group
                fits = len(message.content) + 1 + len(text) <= MESSAGE_LIMIT
                if message.channel.id == channel.id and time.monotonic() - started < GROUP_WINDOW_SECONDS and fits:
                    try:
                        edited = await message.edit(content=f"{message.content}\n{text}")
                        self._groups[guild_id] = (edited, started)
                        return
                    except discord.HTTPException:
                        pass  # deleted, or not editable: start a new group
            try:
                message = await channel.send(text, allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException as exc:
                self._failed(guild_id, exc)
                return
            self._succeeded(guild_id)
            self._groups[guild_id] = (message, time.monotonic())

    async def send(self, guild_id: int, content: str | None = None, **kwargs: Any) -> discord.Message | None:
        """Posts a standalone message (embed, view, files…) and ends the current group."""
        async with self._locks[guild_id]:
            self._groups.pop(guild_id, None)
            channel = await admin_channel(self.bot, guild_id)
            if channel is None:
                return None
            kwargs.setdefault("allowed_mentions", discord.AllowedMentions.none())
            try:
                message = await channel.send(content, **kwargs)
            except discord.HTTPException as exc:
                self._failed(guild_id, exc)
                return None
            self._succeeded(guild_id)
            return message


async def notify_admins(bot: DraftCupBot, guild_id: int, text: str) -> None:
    await bot.feed.line(guild_id, text)


# ------------------------------------------------------------------- change lines

_FIELD_NAMES = {
    "nickname": "nickname",
    "steam_url": "Steam link",
    "player_class": "class",
    "highest_division": "division",
    "igl": "IGL",
}


def signup_change_line(
    user_id: int, actor_id: int | None, kind: str, details: dict, player_class: str = "", division: str = ""
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
    elif kind == "left_server":
        line = f"⚠️ <@{user_id}> (**{nickname}**) left the server. Their signup is kept"
    elif kind == "rejoined_server":
        line = f"ℹ️ <@{user_id}> (**{nickname}**) rejoined the server"
    else:
        line = f"ℹ️ <@{user_id}>: {kind} ({nickname})"
    if details.get("captain_reset_from") == "picked":
        line += ". Their captain acceptance was reset: accept or reject them again"
    if actor_id is not None and actor_id != user_id:
        line += f" (by <@{actor_id}>)"
    return line
