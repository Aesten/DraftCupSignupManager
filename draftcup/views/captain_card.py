"""Captain signup review (spec §6.2).

Every captain signup gets a card in the admin channel with **Accept** and **Reject**. Accept asks for
the division; Reject turns the signup into a player signup and tells the member by DM. Decisions can be
changed later with /captain edit. Card buttons are DynamicItems: their custom ID carries the signup ID,
so they keep working after a restart.
"""

from __future__ import annotations

import logging
from collections import Counter
from typing import TYPE_CHECKING

import discord

from .. import rules
from ..db import ActionRefused
from ..models import CaptainStatus, Role, Signup
from ..notify import admin_channel
from ..permissions import interaction_is_admin

if TYPE_CHECKING:
    from ..bot import DraftCupBot

    Interaction = discord.Interaction[DraftCupBot]

log = logging.getLogger(__name__)

PANEL_TIMEOUT = 15 * 60


# ----------------------------------------------------------------------- embeds


def signup_details_embed(signup: Signup, title: str, colour: discord.Colour | None = None) -> discord.Embed:
    """Everything organisers need about one signup, including tier and budget."""
    embed = discord.Embed(title=title, colour=colour or discord.Colour.blurple())
    left = " ⚠️ left the server" if signup.left_server else ""
    embed.add_field(name="Discord", value=f"<@{signup.user_id}> (`{signup.username}`){left}")
    embed.add_field(name="Role", value=signup.role.label)
    embed.add_field(name="Class", value=rules.CLASS_LABELS[signup.player_class])
    embed.add_field(name="Highest division", value=signup.highest_division or "None")
    embed.add_field(name="Tier", value=str(signup.tier))
    if signup.role is Role.CAPTAIN:
        embed.add_field(name="Budget", value=f"{signup.budget:.1f}")
    embed.add_field(name="IGL", value="Yes" if signup.igl else "No")
    embed.add_field(name="Steam", value=signup.steam_url, inline=False)
    embed.set_footer(text=f"Signed up {signup.created_at:%Y-%m-%d %H:%M} UTC · updated {signup.updated_at:%Y-%m-%d %H:%M} UTC")
    return embed


def division_name(names: list[str], index: int | None) -> str:
    if index is None:
        return "?"
    return names[index - 1] if index <= len(names) else f"Division {index}"


def status_line(signup: Signup, names: list[str]) -> str:
    if signup.withdrawn_at is not None:
        return "❌ Withdrew their signup."
    if signup.role is not Role.CAPTAIN:
        by = f" by <@{signup.status_set_by}>" if signup.status_set_by else ""
        return f"↩️ Not a captain (rejected{by}): signed up as a player."
    if signup.captain_status is CaptainStatus.PICKED:
        return f"✅ Accepted into **{division_name(names, signup.division_index)}** by <@{signup.status_set_by}>."
    return "⏳ Waiting for a decision."


def card_embed(signup: Signup, names: list[str]) -> discord.Embed:
    pending = signup.role is Role.CAPTAIN and signup.withdrawn_at is None and signup.captain_status is not CaptainStatus.PICKED
    accepted = signup.role is Role.CAPTAIN and signup.withdrawn_at is None and not pending
    colour = discord.Colour.orange() if pending else discord.Colour.green() if accepted else discord.Colour.dark_grey()
    embed = signup_details_embed(signup, f"🎖️ Captain signup: {signup.nickname}", colour)
    embed.description = status_line(signup, names)
    if accepted:
        embed.description += "\n-# Change the division or revoke with `/captain edit`."
    return embed


# ------------------------------------------------------------------------- card


class CaptainAction(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"draftcup:cap:(?P<signup_id>\d+):(?P<action>accept|reject)",
):
    def __init__(self, signup_id: int, action: str) -> None:
        accept = action == "accept"
        super().__init__(discord.ui.Button(
            label="Accept" if accept else "Reject",
            style=discord.ButtonStyle.success if accept else discord.ButtonStyle.danger,
            custom_id=f"draftcup:cap:{signup_id}:{action}",
        ))
        self.signup_id = signup_id
        self.action = action

    @classmethod
    async def from_custom_id(cls, interaction, item, match) -> CaptainAction:  # type: ignore[override]
        return cls(int(match["signup_id"]), match["action"])

    async def callback(self, interaction: Interaction) -> None:  # type: ignore[override]
        signup = await _current_signup(interaction, self.signup_id)
        if signup is None:
            return
        if signup.role is not Role.CAPTAIN or signup.captain_status is CaptainStatus.PICKED:
            await interaction.response.send_message(
                "This captain signup was already decided. Use `/captain edit` to change it.", ephemeral=True
            )
            return
        if self.action == "accept":
            await send_division_picker(interaction, signup)
        else:
            await interaction.response.send_message(
                f"Reject **{signup.nickname}** as captain? They stay signed up **as a player** and get a DM.",
                view=ConfirmToPlayerView(signup.id, "captain_rejected"),
                ephemeral=True,
            )


def card_view(signup: Signup) -> discord.ui.View | None:
    """Accept/Reject while undecided; no buttons once decided (changes go through /captain edit)."""
    if signup.withdrawn_at is not None or signup.role is not Role.CAPTAIN or signup.captain_status is CaptainStatus.PICKED:
        return None
    view = discord.ui.View(timeout=None)
    view.add_item(CaptainAction(signup.id, "accept"))
    view.add_item(CaptainAction(signup.id, "reject"))
    return view


async def _current_signup(interaction: Interaction, signup_id: int) -> Signup | None:
    """The signup, if the user is an organiser and it belongs to this server's current tournament.
    Answers the interaction itself otherwise."""
    if interaction.guild is None:
        return None
    if not await interaction_is_admin(interaction):
        await interaction.response.send_message("Only organisers can review captains.", ephemeral=True)
        return None
    bot = interaction.client
    tournament = await bot.db.active_tournament(interaction.guild.id)
    signup = await bot.db.get_signup(signup_id)
    if tournament is None or signup is None or signup.tournament_id != tournament.id:
        await interaction.response.send_message("This signup belongs to an earlier tournament.", ephemeral=True)
        return None
    if signup.withdrawn_at is not None:
        await interaction.response.send_message(f"**{signup.nickname}** withdrew their signup.", ephemeral=True)
        return None
    return signup


# -------------------------------------------------------------- division picker


async def division_options(bot: DraftCupBot, guild_id: int, signup: Signup) -> list[discord.SelectOption]:
    config = await bot.db.get_config(guild_id)
    names = await bot.db.division_names(signup.tournament_id, config.division_count)
    signups = await bot.db.list_active_signups(signup.tournament_id)
    counts = Counter(
        s.division_index for s in signups if s.role is Role.CAPTAIN and s.captain_status is CaptainStatus.PICKED
    )
    options = []
    for index, name in enumerate(names, start=1):
        current = signup.captain_status is CaptainStatus.PICKED and signup.division_index == index
        full = counts[index] >= config.captains_per_division and not current
        options.append(discord.SelectOption(
            label=name,
            value=str(index),
            description=f"{counts[index]}/{config.captains_per_division} captains" + (" · full" if full else ""),
            default=current,
        ))
    return options


class DivisionSelect(discord.ui.Select):
    def __init__(self, signup_id: int, options: list[discord.SelectOption]) -> None:
        super().__init__(placeholder="Accept into division…", options=options, min_values=1, max_values=1)
        self.signup_id = signup_id

    async def callback(self, interaction: Interaction) -> None:  # type: ignore[override]
        signup = await _current_signup(interaction, self.signup_id)
        if signup is None:
            return
        try:
            signup = await accept_captain(interaction.client, interaction.guild.id, signup.id, int(self.values[0]), interaction.user.id)
        except ActionRefused as exc:
            await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
            return
        config = await interaction.client.db.get_config(interaction.guild.id)
        names = await interaction.client.db.division_names(signup.tournament_id, config.division_count)
        await interaction.response.edit_message(
            content=f"✅ **{signup.nickname}** accepted into **{division_name(names, signup.division_index)}**.", view=None
        )


async def send_division_picker(interaction: Interaction, signup: Signup, *, extra: discord.ui.Item | None = None) -> None:
    assert interaction.guild is not None
    view = discord.ui.View(timeout=PANEL_TIMEOUT)
    view.add_item(DivisionSelect(signup.id, await division_options(interaction.client, interaction.guild.id, signup)))
    if extra is not None:
        view.add_item(extra)
    await interaction.response.send_message(
        f"Which division does **{signup.nickname}** captain? (budget {signup.budget:.1f})", view=view, ephemeral=True
    )


class ConfirmToPlayerView(discord.ui.View):
    """Confirms rejecting (or revoking) a captain: they become a player and get a DM."""

    def __init__(self, signup_id: int, kind: str) -> None:
        super().__init__(timeout=PANEL_TIMEOUT)
        self.signup_id = signup_id
        self.kind = kind
        self.confirm.label = "Reject" if kind == "captain_rejected" else "Revoke"

    @discord.ui.button(label="Reject", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: Interaction, _: discord.ui.Button) -> None:
        signup = await _current_signup(interaction, self.signup_id)
        if signup is None:
            return
        try:
            signup, dm_sent = await captain_to_player(interaction.client, interaction.guild.id, signup.id, interaction.user.id, self.kind)
        except ActionRefused as exc:
            await interaction.response.edit_message(content=f"❌ {exc}", view=None)
            return
        dm = "They were told by DM." if dm_sent else "⚠️ I couldn't DM them (their DMs are closed): tell them yourself."
        await interaction.response.edit_message(content=f"↩️ **{signup.nickname}** is now signed up as a player. {dm}", view=None)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: Interaction, _: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="Nothing changed.", view=None)


# -------------------------------------------------------------------- decisions


async def accept_captain(bot: DraftCupBot, guild_id: int, signup_id: int, division: int, actor_id: int) -> Signup:
    """Accepts a captain into a division (or moves an accepted captain). Raises ActionRefused."""
    config = await bot.db.get_config(guild_id)
    signup, details = await bot.db.set_captain_status(
        signup_id, CaptainStatus.PICKED, division, actor_id,
        division_count=config.division_count, capacity=config.captains_per_division,
    )
    if details:
        names = await bot.db.division_names(signup.tournament_id, config.division_count)
        verb = "moved to" if details.get("from") == CaptainStatus.PICKED.value else "accepted into"
        await bot.feed.line(guild_id, f"✅ Captain **{signup.nickname}** {verb} **{division_name(names, division)}** by <@{actor_id}>")
        await sync_card(bot, guild_id, signup)
        bot.refresher.request(guild_id)
    return signup


_DM_TEXT = {
    "captain_rejected": "Your **captain** signup for **{title}** on **{guild}** wasn't accepted. "
    "You're still signed up, as a **player**. Use **My signup** on the signup post to check or change it.",
    "captain_revoked": "Your acceptance as **captain** for **{title}** on **{guild}** was withdrawn. "
    "You're still signed up, as a **player**. Use **My signup** on the signup post to check or change it.",
}


async def captain_to_player(bot: DraftCupBot, guild_id: int, signup_id: int, actor_id: int, kind: str) -> tuple[Signup, bool]:
    """Rejects or revokes a captain: they become a player and get a DM. Returns (signup, DM sent)."""
    signup, _ = await bot.db.set_role(signup_id, Role.PLAYER, actor_id, kind)
    config = await bot.db.get_config(guild_id)
    guild = bot.get_guild(guild_id)
    dm_sent = await _dm(bot, signup.user_id, _DM_TEXT[kind].format(title=config.title, guild=guild.name if guild else "the server"))
    verb = "rejected as captain" if kind == "captain_rejected" else "no longer accepted as captain"
    note = "" if dm_sent else " ⚠️ couldn't DM them"
    await bot.feed.line(guild_id, f"↩️ **{signup.nickname}** {verb} by <@{actor_id}>: now a player.{note}")
    await sync_card(bot, guild_id, signup)
    bot.refresher.request(guild_id)
    return signup, dm_sent


async def player_to_captain(bot: DraftCupBot, guild_id: int, signup_id: int, actor_id: int) -> Signup:
    """Makes a player a captain candidate again; a new review card is posted."""
    signup, _ = await bot.db.set_role(signup_id, Role.CAPTAIN, actor_id, "captain_added")
    await bot.feed.line(guild_id, f"🎖️ <@{actor_id}> made **{signup.nickname}** a captain candidate.")
    await sync_card(bot, guild_id, signup)
    bot.refresher.request(guild_id)
    return signup


async def _dm(bot: DraftCupBot, user_id: int, text: str) -> bool:
    try:
        user = bot.get_user(user_id) or await bot.fetch_user(user_id)
        await user.send(text)
        return True
    except discord.HTTPException:
        return False


# ------------------------------------------------------------------ card upkeep


async def sync_card(bot: DraftCupBot, guild_id: int, signup: Signup) -> None:
    """Creates, updates or retires the review card of a signup."""
    config = await bot.db.get_config(guild_id)
    names = await bot.db.division_names(signup.tournament_id, config.division_count)
    candidate = signup.withdrawn_at is None and signup.role is Role.CAPTAIN
    embed, view = card_embed(signup, names), card_view(signup)

    if signup.review_message_id is not None:
        channel = await admin_channel(bot, guild_id)
        if channel is not None:
            try:
                await channel.get_partial_message(signup.review_message_id).edit(embed=embed, view=view)
                if not candidate:
                    # Retired: becoming a captain candidate again gets a fresh card at the bottom.
                    await bot.db.set_review_message(signup.id, None)
                return
            except discord.NotFound:
                pass
            except discord.HTTPException:
                log.exception("Could not update the captain card of signup %s", signup.id)
                return
        await bot.db.set_review_message(signup.id, None)

    if candidate:
        message = await bot.feed.send(guild_id, embed=embed, view=view)
        if message is not None:
            await bot.db.set_review_message(signup.id, message.id)


async def repost_pending_cards(bot: DraftCupBot, guild_id: int) -> int:
    """Posts a fresh card for every undecided captain (deleting their old card). Returns how many."""
    tournament = await bot.db.active_tournament(guild_id)
    if tournament is None:
        return 0
    pending = [
        s for s in await bot.db.list_active_signups(tournament.id)
        if s.role is Role.CAPTAIN and s.captain_status is not CaptainStatus.PICKED
    ]
    channel = await admin_channel(bot, guild_id)
    for signup in pending:
        if signup.review_message_id is not None and channel is not None:
            try:
                await channel.get_partial_message(signup.review_message_id).delete()
            except discord.HTTPException:
                pass
        await bot.db.set_review_message(signup.id, None)
        signup.review_message_id = None
        await sync_card(bot, guild_id, signup)
    return len(pending)


async def refresh_all_cards(bot: DraftCupBot, guild_id: int) -> None:
    """Re-renders every captain card, e.g. after the number of divisions changed."""
    tournament = await bot.db.active_tournament(guild_id)
    if tournament is None:
        return
    for signup in await bot.db.list_active_signups(tournament.id):
        if signup.role is Role.CAPTAIN and signup.review_message_id is not None:
            await sync_card(bot, guild_id, signup)
