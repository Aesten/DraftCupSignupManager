"""Background jobs and member events: scheduled close (spec §8), members leaving (spec §6.3)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord.ext import commands, tasks

from ..db import utcnow
from ..models import State
from ..notify import notify_admins
from ..views import signup_post

if TYPE_CHECKING:
    from ..bot import DraftCupBot

log = logging.getLogger(__name__)


class LifecycleCog(commands.Cog):
    def __init__(self, bot: DraftCupBot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self.close_due_signups.start()

    async def cog_unload(self) -> None:
        self.close_due_signups.cancel()

    @tasks.loop(seconds=30)
    async def close_due_signups(self) -> None:
        for tournament in await self.bot.db.open_tournaments_due(utcnow()):
            try:
                await self.bot.db.set_state(tournament.id, State.CLOSED)
                log.info("Closed signups of guild %s (scheduled)", tournament.guild_id)
                await signup_post.refresh(self.bot, tournament.guild_id)
                await notify_admins(self.bot, tournament.guild_id, "🔒 Signups closed (scheduled close time reached).")
            except Exception:
                # One server's failure must not stop the scheduler for the others.
                log.exception("Scheduled close failed for guild %s", tournament.guild_id)

    @close_due_signups.before_loop
    async def before_close_due_signups(self) -> None:
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_raw_member_remove(self, payload: discord.RawMemberRemoveEvent) -> None:
        await self._member_presence(payload.guild_id, payload.user.id, left=True)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        await self._member_presence(member.guild.id, member.id, left=False)

    async def _member_presence(self, guild_id: int, user_id: int, *, left: bool) -> None:
        tournament = await self.bot.db.active_tournament(guild_id)
        signup = await self.bot.db.set_left_server(tournament.id, user_id, left)
        if signup is None:
            return
        if left:
            text = f"⚠️ <@{user_id}> (**{signup.nickname}**, {signup.role.value}) left the server. Their signup is kept."
        else:
            text = f"ℹ️ <@{user_id}> (**{signup.nickname}**) rejoined the server."
        await notify_admins(self.bot, guild_id, text)


async def setup(bot: DraftCupBot) -> None:
    await bot.add_cog(LifecycleCog(bot))
