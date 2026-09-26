"""Background jobs and member events: scheduled close (spec §8), stale exports (spec §9.4),
members leaving (spec §6.3)."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import TYPE_CHECKING

import discord
from discord.ext import commands, tasks

from .. import actions, events
from ..db import utcnow
from ..exports import summarize_changes
from ..timeutil import discord_ts

# Stale-export notices wait this long after the first change, so a burst is summarised in one notice.
STALE_NOTICE_DELAY = timedelta(minutes=5)

if TYPE_CHECKING:
    from ..bot import DraftCupBot

log = logging.getLogger(__name__)


class LifecycleCog(commands.Cog):
    def __init__(self, bot: DraftCupBot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self.close_due_signups.start()
        self.check_stale_exports.start()

    async def cog_unload(self) -> None:
        self.close_due_signups.cancel()
        self.check_stale_exports.cancel()

    @tasks.loop(seconds=30)
    async def close_due_signups(self) -> None:
        for tournament in await self.bot.db.open_tournaments_due(utcnow()):
            try:
                await actions.close_signups(self.bot, tournament.guild_id, None)
                log.info("Closed signups of guild %s (scheduled)", tournament.guild_id)
            except Exception:
                # One server's failure must not stop the scheduler for the others.
                log.exception("Scheduled close failed for guild %s", tournament.guild_id)

    @close_due_signups.before_loop
    async def before_close_due_signups(self) -> None:
        await self.bot.wait_until_ready()

    @tasks.loop(seconds=30)
    async def check_stale_exports(self) -> None:
        now = utcnow()
        for tournament in await self.bot.db.active_tournaments():
            try:
                for record in (await self.bot.db.latest_exports(tournament.id)).values():
                    if record.stale_notified_at is not None or tournament.revision <= record.revision:
                        continue
                    changes = await self.bot.db.changes_since(tournament.id, record.revision)
                    if not changes or now - changes[0].time < STALE_NOTICE_DELAY:
                        continue
                    await self.bot.feed.send(
                        tournament.guild_id,
                        f"📦 The **{record.type.label.lower()}** exported {discord_ts(record.time, 'R')} is stale: "
                        f"{summarize_changes(changes)}. Export again when ready.",
                    )
                    await self.bot.db.mark_stale_notified(record.id)
            except Exception:
                log.exception("Stale export check failed for guild %s", tournament.guild_id)

    @check_stale_exports.before_loop
    async def before_check_stale_exports(self) -> None:
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_raw_member_remove(self, payload: discord.RawMemberRemoveEvent) -> None:
        await self._member_presence(payload.guild_id, payload.user.id, left=True)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        await self._member_presence(member.guild.id, member.id, left=False)

    async def _member_presence(self, guild_id: int, user_id: int, *, left: bool) -> None:
        tournament = await self.bot.db.active_tournament(guild_id)
        if tournament is None:
            return
        signup = await self.bot.db.set_left_server(tournament.id, user_id, left)
        if signup is None:
            return
        details = {"nickname": signup.nickname, "role": signup.role.value}
        await events.signup_changed(self.bot, guild_id, signup, "left_server" if left else "rejoined_server", details, None)


async def setup(bot: DraftCupBot) -> None:
    await bot.add_cog(LifecycleCog(bot))
