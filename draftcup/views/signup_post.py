"""The public signup post and its persistent buttons (spec §5.1)."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING

import discord

from ..db import utcnow
from ..models import GuildConfig, Role, State, Tournament
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


def build_embed(config: GuildConfig, tournament: Tournament) -> discord.Embed:
    is_open = signups_open(tournament, config)
    if is_open:
        status = f"🟢 **Signups are open.** They close {discord_ts(config.closes_at, 'R')} ({discord_ts(config.closes_at, 'f')})."
        colour = discord.Colour.green()
    elif tournament.state is State.DRAFT:
        status = "⏳ Signups are not open yet."
        colour = discord.Colour.light_grey()
    else:
        status = "🔒 **Signups are closed.** Contact an organiser for any change."
        colour = discord.Colour.red()

    embed = discord.Embed(title=config.title, description=status, colour=colour)
    embed.add_field(name="Format", value=config.format.label, inline=True)
    embed.add_field(name="Auction (captains)", value=discord_ts(config.auction_date), inline=True)
    embed.add_field(name="Tournament", value=discord_ts(config.tournament_date), inline=True)
    if config.rules_url:
        embed.add_field(name="Rules", value=config.rules_url, inline=False)
    embed.add_field(
        name="How to sign up",
        value=(
            "Pick **Player** or **Captain**, accept the rules, then fill in the form.\n"
            "Captains who aren't picked play as players.\n"
            "Use **My signup** to check, edit or withdraw your signup while signups are open."
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


async def post_or_refresh(bot: DraftCupBot, guild_id: int, *, repost: bool = False) -> discord.Message | None:
    """Edits the signup post in place, or posts it if it's missing (or `repost` is set)."""
    config = await bot.db.get_config(guild_id)
    if config.signup_channel_id is None:
        return None
    tournament = await bot.db.active_tournament(guild_id)
    embed = build_embed(config, tournament)
    view = SignupPostView(is_open=signups_open(tournament, config))

    channel = bot.get_channel(config.signup_channel_id)
    if channel is None:
        channel = await bot.fetch_channel(config.signup_channel_id)
    if not isinstance(channel, (discord.TextChannel, discord.Thread)):
        raise RuntimeError("The signup channel is not a text channel.")

    if config.signup_message_id is not None and not repost:
        try:
            return await channel.get_partial_message(config.signup_message_id).edit(embed=embed, view=view)
        except discord.NotFound:
            log.info("Signup post of guild %s was deleted, posting a new one", guild_id)

    message = await channel.send(embed=embed, view=view)
    await bot.db.update_config(guild_id, signup_message_id=message.id)
    return message


async def refresh(bot: DraftCupBot, guild_id: int) -> None:
    """Best-effort refresh after a config or state change; failures are logged, not raised."""
    try:
        await post_or_refresh(bot, guild_id)
    except (discord.HTTPException, RuntimeError):
        log.exception("Could not refresh the signup post of guild %s", guild_id)
