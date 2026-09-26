"""The public signup post and its persistent buttons (spec §5.1).

It only exists once signups have opened: posted on the first opening, then edited in place for live
counts, closing and reopening. Each tournament gets its own post.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING

import discord

from ..db import utcnow
from ..models import GuildConfig, Role, Signup, State, Tournament
from ..timeutil import discord_ts
from . import signup_flow

if TYPE_CHECKING:
    from ..bot import DraftCupBot

log = logging.getLogger(__name__)

PLAYER_ID = "draftcup:signup:player"
CAPTAIN_ID = "draftcup:signup:captain"
MINE_ID = "draftcup:signup:mine"


def signups_open(tournament: Tournament, config: GuildConfig, now: datetime | None = None) -> bool:
    """Open state, and the close time hasn't passed (the scheduler may lag by up to 30 s)."""
    if tournament.state is not State.OPEN:
        return False
    return config.closes_at is None or (now or utcnow()) < config.closes_at


def build_embed(config: GuildConfig, tournament: Tournament, signups: list[Signup]) -> discord.Embed:
    """Title, close moment, live counts. Rules and dates are in the server's own channels."""
    players = sum(1 for s in signups if s.role is Role.PLAYER)
    captains = sum(1 for s in signups if s.role is Role.CAPTAIN)
    counts = f"**{players}** player{'s' if players != 1 else ''} · **{captains}** captain candidate{'s' if captains != 1 else ''}"
    if signups_open(tournament, config):
        status = (
            f"🟢 **Signups are open** until {discord_ts(config.closes_at, 'f')} ({discord_ts(config.closes_at, 'R')}).\n"
            f"{counts} signed up so far."
        )
        colour = discord.Colour.green()
    else:
        status = f"🔒 **Signups are closed.** Contact an organiser for any change.\n{counts} signed up."
        colour = discord.Colour.red()

    embed = discord.Embed(title=config.title, description=status, colour=colour)
    embed.add_field(
        name="How to sign up",
        value=(
            "Read the rules and announcements first. Then pick **Player** or **Captain**, confirm, and fill in the form.\n"
            "Captain signups are reviewed by the organisers; if you're not accepted, you play as a player.\n"
            "Use **My signup** to check, change or withdraw your registration while signups are open."
        ),
        inline=False,
    )
    return embed


class SignupPostView(discord.ui.View):
    """Persistent view: one instance is registered at startup and handles clicks on every server's post."""

    def __init__(self, is_open: bool = True) -> None:
        super().__init__(timeout=None)
        self.player.disabled = not is_open
        self.captain.disabled = not is_open

    @discord.ui.button(label="Sign up as Player", style=discord.ButtonStyle.primary, custom_id=PLAYER_ID)
    async def player(self, interaction: discord.Interaction[DraftCupBot], _: discord.ui.Button) -> None:
        await signup_flow.start_signup(interaction, Role.PLAYER)

    @discord.ui.button(label="Sign up as Captain", style=discord.ButtonStyle.success, custom_id=CAPTAIN_ID)
    async def captain(self, interaction: discord.Interaction[DraftCupBot], _: discord.ui.Button) -> None:
        await signup_flow.start_signup(interaction, Role.CAPTAIN)

    @discord.ui.button(label="My signup", style=discord.ButtonStyle.secondary, custom_id=MINE_ID)
    async def mine(self, interaction: discord.Interaction[DraftCupBot], _: discord.ui.Button) -> None:
        await signup_flow.show_my_signup(interaction)


async def _signup_channel(bot: DraftCupBot, config: GuildConfig) -> discord.TextChannel | discord.Thread:
    if config.signup_channel_id is None:
        raise RuntimeError("no signup channel is set")
    channel = bot.get_channel(config.signup_channel_id)
    if channel is None:
        channel = await bot.fetch_channel(config.signup_channel_id)
    if not isinstance(channel, (discord.TextChannel, discord.Thread)):
        raise RuntimeError("the signup channel is not a text channel")
    return channel


async def _render(bot: DraftCupBot, guild_id: int) -> tuple[GuildConfig, discord.Embed, SignupPostView]:
    config = await bot.db.get_config(guild_id)
    tournament = await bot.db.active_tournament(guild_id)
    if tournament is None:
        raise RuntimeError("no tournament is running")
    signups = await bot.db.list_active_signups(tournament.id)
    return config, build_embed(config, tournament, signups), SignupPostView(is_open=signups_open(tournament, config))


async def publish(bot: DraftCupBot, guild_id: int) -> discord.Message:
    """Called when signups open: edits this tournament's post back to open, or posts it the first time."""
    config, embed, view = await _render(bot, guild_id)
    channel = await _signup_channel(bot, config)
    if config.signup_message_id is not None:
        try:
            return await channel.get_partial_message(config.signup_message_id).edit(embed=embed, view=view)
        except discord.NotFound:
            pass
    message = await channel.send(embed=embed, view=view)
    await bot.db.update_config(guild_id, signup_message_id=message.id)
    return message


async def refresh(bot: DraftCupBot, guild_id: int) -> None:
    """Edits the public post (counts, closed state…) if there is one. Never posts a new message,
    except to replace a post deleted while signups are open. Failures are logged, not raised."""
    try:
        config = await bot.db.get_config(guild_id)
        if config.signup_message_id is None:
            return
        config, embed, view = await _render(bot, guild_id)
        channel = await _signup_channel(bot, config)
        try:
            await channel.get_partial_message(config.signup_message_id).edit(embed=embed, view=view)
        except discord.NotFound:
            await bot.db.update_config(guild_id, signup_message_id=None)
            if view.player.disabled is False:
                log.info("Signup post of guild %s was deleted while open, posting it again", guild_id)
                await publish(bot, guild_id)
    except (discord.HTTPException, RuntimeError):
        log.exception("Could not refresh the signup post of guild %s", guild_id)
