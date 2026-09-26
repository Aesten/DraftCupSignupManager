"""Captain review cards in the admin channel (spec §6.2).

Each captain candidate gets one card with a button per division, plus Pool and Reset. The buttons are
DynamicItems: their custom ID carries the signup ID, so they keep working after a restart.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord

from .. import rules
from ..db import ActionRefused
from ..models import CaptainStatus, Role, Signup
from ..notify import admin_channel, captain_status_text
from ..permissions import interaction_is_admin

if TYPE_CHECKING:
    from ..bot import DraftCupBot

log = logging.getLogger(__name__)

_STATUS_STYLE = {
    CaptainStatus.PENDING: ("⏳", discord.Colour.orange()),
    CaptainStatus.PICKED: ("✅", discord.Colour.green()),
    CaptainStatus.POOL: ("↩️", discord.Colour.blue()),
}


def signup_details_embed(signup: Signup, title: str, colour: discord.Colour | None = None) -> discord.Embed:
    """Everything organisers need about one signup, including tier and budget."""
    embed = discord.Embed(title=title, colour=colour or discord.Colour.blurple())
    embed.add_field(name="Discord", value=f"<@{signup.user_id}> (`{signup.username}`)" + (" ⚠️ left the server" if signup.left_server else ""))
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


def card_embed(signup: Signup, division_names: list[str]) -> discord.Embed:
    if signup.withdrawn_at is not None or signup.role is not Role.CAPTAIN:
        reason = "withdrew" if signup.withdrawn_at is not None else "switched to player"
        embed = signup_details_embed(signup, f"~~{signup.nickname}~~", discord.Colour.dark_grey())
        embed.description = f"No longer a captain candidate ({reason})."
        return embed
    status = signup.captain_status or CaptainStatus.PENDING
    icon, colour = _STATUS_STYLE[status]
    embed = signup_details_embed(signup, f"🎖️ Captain candidate: {signup.nickname}", colour)
    text = captain_status_text(status.value, signup.division_index, division_names)
    by = f" by <@{signup.status_set_by}>" if signup.status_set_by and status is not CaptainStatus.PENDING else ""
    embed.description = f"{icon} {text[0].upper()}{text[1:]}{by}"
    return embed


class CaptainAction(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"draftcup:cap:(?P<signup_id>\d+):(?P<action>pick|pool|reset)(?::(?P<division>\d+))?",
):
    def __init__(
        self,
        signup_id: int,
        action: str,
        division: int | None = None,
        *,
        label: str = "",
        style: discord.ButtonStyle = discord.ButtonStyle.secondary,
        disabled: bool = False,
    ) -> None:
        custom_id = f"draftcup:cap:{signup_id}:{action}" + (f":{division}" if division is not None else "")
        super().__init__(discord.ui.Button(label=label, style=style, custom_id=custom_id, disabled=disabled))
        self.signup_id = signup_id
        self.action = action
        self.division = division

    @classmethod
    async def from_custom_id(cls, interaction, item, match) -> CaptainAction:  # type: ignore[override]
        division = match["division"]
        return cls(int(match["signup_id"]), match["action"], int(division) if division else None)

    async def callback(self, interaction: discord.Interaction[DraftCupBot]) -> None:  # type: ignore[override]
        status = {"pick": CaptainStatus.PICKED, "pool": CaptainStatus.POOL, "reset": CaptainStatus.PENDING}[self.action]
        await apply_captain_status(interaction, self.signup_id, status, self.division, edit_card=True)


def card_view(signup: Signup, division_names: list[str]) -> discord.ui.View | None:
    if signup.withdrawn_at is not None or signup.role is not Role.CAPTAIN:
        return None
    view = discord.ui.View(timeout=None)
    status = signup.captain_status or CaptainStatus.PENDING
    for index, name in enumerate(division_names, start=1):
        current = status is CaptainStatus.PICKED and signup.division_index == index
        view.add_item(CaptainAction(
            signup.id, "pick", index, label=name[:80],
            style=discord.ButtonStyle.success if current else discord.ButtonStyle.primary, disabled=current,
        ))
    view.add_item(CaptainAction(
        signup.id, "pool", label="Pool", style=discord.ButtonStyle.secondary, disabled=status is CaptainStatus.POOL
    ))
    view.add_item(CaptainAction(
        signup.id, "reset", label="Reset", style=discord.ButtonStyle.danger, disabled=status is CaptainStatus.PENDING
    ))
    return view


async def set_captain_status(
    bot: DraftCupBot, guild_id: int, signup_id: int, status: CaptainStatus, division: int | None, actor_id: int
) -> tuple[Signup, dict, list[str]]:
    """Validates and saves a captain decision. Raises ActionRefused.

    Returns the signup, the change details (empty when nothing changed) and the division names.
    Call `after_captain_status` afterwards, once the interaction was answered.
    """
    config = await bot.db.get_config(guild_id)
    tournament = await bot.db.active_tournament(guild_id)
    signup = await bot.db.get_signup(signup_id)
    if signup is None or signup.tournament_id != tournament.id:
        raise ActionRefused("This signup isn't part of the current tournament.")
    signup, details = await bot.db.set_captain_status(
        signup_id, status, division, actor_id,
        division_count=config.division_count, capacity=config.captains_per_division,
    )
    names = await bot.db.division_names(tournament.id, config.division_count)
    return signup, details, names


async def after_captain_status(
    bot: DraftCupBot, guild_id: int, signup: Signup, details: dict, names: list[str], actor_id: int, *, update_card: bool
) -> None:
    from .. import events  # events imports this module

    if not details:
        return
    if update_card:
        await sync_card(bot, guild_id, signup)
    await events.captain_status_changed(bot, guild_id, details, actor_id, names)


async def apply_captain_status(
    interaction: discord.Interaction[DraftCupBot],
    signup_id: int,
    status: CaptainStatus,
    division: int | None,
    *,
    edit_card: bool,
) -> Signup | None:
    """Captain decision from a card button (`edit_card`, answered by editing the card) or /captain set."""
    if interaction.guild is None:
        return None
    if not await interaction_is_admin(interaction):
        await interaction.response.send_message("Only organisers can pick captains.", ephemeral=True)
        return None
    bot, guild_id = interaction.client, interaction.guild.id
    try:
        signup, details, names = await set_captain_status(bot, guild_id, signup_id, status, division, interaction.user.id)
    except ActionRefused as exc:
        await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
        return None

    clicked_card = edit_card and signup.review_message_id == getattr(interaction.message, "id", None)
    if edit_card:
        await interaction.response.edit_message(embed=card_embed(signup, names), view=card_view(signup, names))
    elif not details:
        await interaction.response.send_message("Nothing changed.", ephemeral=True)
    else:
        text = captain_status_text(status.value, signup.division_index, names)
        await interaction.response.send_message(f"✅ **{signup.nickname}** {text}.", ephemeral=True)
    await after_captain_status(bot, guild_id, signup, details, names, interaction.user.id, update_card=not clicked_card)
    return signup


async def sync_card(bot: DraftCupBot, guild_id: int, signup: Signup) -> None:
    """Creates, updates or retires the card of a signup, depending on whether it's still a candidate."""
    config = await bot.db.get_config(guild_id)
    names = await bot.db.division_names(signup.tournament_id, config.division_count)
    active = signup.withdrawn_at is None and signup.role is Role.CAPTAIN
    embed, view = card_embed(signup, names), card_view(signup, names)

    if signup.review_message_id is not None:
        channel = await admin_channel(bot, guild_id)
        if channel is not None:
            try:
                await channel.get_partial_message(signup.review_message_id).edit(embed=embed, view=view)
                if not active:
                    # Retired: a later switch back to captain gets a fresh card at the bottom.
                    await bot.db.set_review_message(signup.id, None)
                return
            except discord.NotFound:
                pass
            except discord.HTTPException:
                log.exception("Could not update the captain card of signup %s", signup.id)
                return
        await bot.db.set_review_message(signup.id, None)

    if active:
        message = await bot.feed.send(guild_id, embed=embed, view=view)
        if message is not None:
            await bot.db.set_review_message(signup.id, message.id)


async def refresh_all_cards(bot: DraftCupBot, guild_id: int) -> None:
    """Re-renders every active card, e.g. after divisions were renamed or added."""
    tournament = await bot.db.active_tournament(guild_id)
    for signup in await bot.db.list_active_signups(tournament.id):
        if signup.role is Role.CAPTAIN and signup.review_message_id is not None:
            await sync_card(bot, guild_id, signup)
