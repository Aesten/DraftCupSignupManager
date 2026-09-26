"""Organiser commands on signups, captains and exports (spec §7)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Awaitable, Callable

import discord
from discord import app_commands
from discord.ext import commands

from .. import actions, rules
from ..db import ActionRefused, utcnow
from ..models import CaptainStatus, ExportType, Role, Signup
from ..permissions import admin_only, interaction_is_admin
from ..views import captain_card
from ..views.captain_card import ConfirmToPlayerView, DivisionSelect, signup_details_embed
from ..views.signup_flow import ConfirmWithdrawView, SignupModal

if TYPE_CHECKING:
    from ..bot import DraftCupBot

    Interaction = discord.Interaction[DraftCupBot]

log = logging.getLogger(__name__)

ROLE_CHOICES = [app_commands.Choice(name=r.label, value=r.value) for r in Role]
EXPORT_CHOICES = [
    app_commands.Choice(name="CSV: full signup data (players.csv + captains.csv)", value=ExportType.CSV.value),
    app_commands.Choice(name="Player list JSON: late signups into the auction app", value=ExportType.PLAYERS.value),
    app_commands.Choice(name="Tournament file (.draftcup.json): players + accepted captains", value=ExportType.TOURNAMENT.value),
]


def signup_autocomplete(role: Role | None = None) -> Callable[[Interaction, str], Awaitable[list[app_commands.Choice[str]]]]:
    """Active signups by nickname or Discord username (optionally of one role). Empty for non-organisers."""

    async def complete(interaction: Interaction, current: str) -> list[app_commands.Choice[str]]:
        if interaction.guild is None or not await interaction_is_admin(interaction):
            return []
        db = interaction.client.db
        tournament = await db.active_tournament(interaction.guild.id)
        if tournament is None:
            return []
        found = [s for s in await db.find_signups(tournament.id, current, limit=50) if role is None or s.role is role]
        return [
            app_commands.Choice(name=f"{s.nickname} ({s.role.value}, @{s.username})"[:100], value=str(s.id))
            for s in found[:25]
        ]

    return complete


async def resolve_signup(interaction: Interaction, value: str) -> Signup | None:
    """Turns an autocomplete value (signup ID) or a typed nickname into an active signup of this server.
    Answers the interaction itself when there's none."""
    assert interaction.guild is not None
    db = interaction.client.db
    tournament = await db.active_tournament(interaction.guild.id)
    if tournament is None:
        await interaction.response.send_message(actions.NO_TOURNAMENT, ephemeral=True)
        return None
    signup = await db.get_signup(int(value)) if value.isdigit() else None
    if signup is None or signup.tournament_id != tournament.id:
        matches = [s for s in await db.find_signups(tournament.id, value) if s.nickname.lower() == value.strip().lower()]
        signup = matches[0] if matches else None
    if signup is None or signup.withdrawn_at is not None:
        await interaction.response.send_message(f"❌ No signup matches `{value}`.", ephemeral=True)
        return None
    return signup


class CaptainEditView(discord.ui.View):
    """/captain edit: pick (or change) the division, or send the captain back to the players."""

    def __init__(self, signup: Signup, options: list[discord.SelectOption]) -> None:
        super().__init__(timeout=15 * 60)
        self.signup_id = signup.id
        accepted = signup.captain_status is CaptainStatus.PICKED
        self.kind = "captain_revoked" if accepted else "captain_rejected"
        self.add_item(DivisionSelect(signup.id, options))
        self.to_player.label = "Revoke: make them a player" if accepted else "Reject: make them a player"

    @discord.ui.button(label="Make them a player", style=discord.ButtonStyle.danger)
    async def to_player(self, interaction: Interaction, _: discord.ui.Button) -> None:
        await interaction.response.edit_message(
            content="They'll stay signed up **as a player** and get a DM. Continue?",
            embed=None,
            view=ConfirmToPlayerView(self.signup_id, self.kind),
        )


class ManagementCog(commands.Cog):
    def __init__(self, bot: DraftCupBot) -> None:
        self.bot = bot

    # ----------------------------------------------------------------- /signup

    signup_group = app_commands.Group(name="signup", description="View or change someone's signup.", guild_only=True)

    @signup_group.command(name="view", description="Show a signup with its tier and budget.")
    @app_commands.describe(signup="Nickname or Discord username")
    @app_commands.autocomplete(signup=signup_autocomplete())
    @admin_only()
    async def signup_view(self, interaction: Interaction, signup: str) -> None:
        found = await resolve_signup(interaction, signup)
        if found is not None:
            await interaction.response.send_message(embed=signup_details_embed(found, found.nickname), ephemeral=True)

    @signup_group.command(name="edit", description="Edit a signup's details, whether signups are open or closed.")
    @app_commands.describe(signup="Nickname or Discord username")
    @app_commands.autocomplete(signup=signup_autocomplete())
    @admin_only()
    async def signup_edit(self, interaction: Interaction, signup: str) -> None:
        found = await resolve_signup(interaction, signup)
        if found is not None:
            modal = SignupModal(found.role, found.fields.to_form(), agreed_at=None, target=(found.user_id, found.username))
            await interaction.response.send_modal(modal)

    @signup_group.command(name="add", description="Sign someone up yourself, e.g. after they asked by DM.")
    @app_commands.choices(role=ROLE_CHOICES)
    @admin_only()
    async def signup_add(self, interaction: Interaction, user: discord.Member, role: str) -> None:
        assert interaction.guild is not None
        tournament = await self.bot.db.active_tournament(interaction.guild.id)
        if tournament is None:
            await interaction.response.send_message(actions.NO_TOURNAMENT, ephemeral=True)
            return
        if await self.bot.db.get_active_signup(tournament.id, user.id) is not None:
            await interaction.response.send_message(f"{user.mention} is already signed up: use `/signup edit`.", ephemeral=True)
            return
        # The organiser vouches for the agreement, so it's recorded as accepted now.
        modal = SignupModal(Role(role), rules.SignupForm(), agreed_at=utcnow(), target=(user.id, user.name))
        await interaction.response.send_modal(modal)

    @signup_group.command(name="remove", description="Withdraw a signup.")
    @app_commands.describe(signup="Nickname or Discord username")
    @app_commands.autocomplete(signup=signup_autocomplete())
    @admin_only()
    async def signup_remove(self, interaction: Interaction, signup: str) -> None:
        found = await resolve_signup(interaction, signup)
        if found is not None:
            await interaction.response.send_message(
                f"Withdraw the {found.role.value} signup of **{found.nickname}** (<@{found.user_id}>)?",
                view=ConfirmWithdrawView(found.id, admin=True),
                ephemeral=True,
            )

    # ---------------------------------------------------------------- /captain

    captain_group = app_commands.Group(name="captain", description="Review captain signups.", guild_only=True)

    @captain_group.command(name="list-pending", description="Post a fresh Accept/Reject card for every captain signup waiting for a decision.")
    @admin_only()
    async def captain_list_pending(self, interaction: Interaction) -> None:
        assert interaction.guild is not None
        if await self.bot.db.active_tournament(interaction.guild.id) is None:
            await interaction.response.send_message(actions.NO_TOURNAMENT, ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        count = await captain_card.repost_pending_cards(self.bot, interaction.guild.id)
        text = f"✅ Posted {count} card{'s' if count != 1 else ''} in the admin channel." if count else "No captain signup is waiting for a decision."
        await interaction.followup.send(text, ephemeral=True)

    @captain_group.command(name="edit", description="Change a captain's division, accept a waiting one, or make them a player.")
    @app_commands.describe(captain="Nickname or Discord username of a captain signup")
    @app_commands.autocomplete(captain=signup_autocomplete(Role.CAPTAIN))
    @admin_only()
    async def captain_edit(self, interaction: Interaction, captain: str) -> None:
        found = await resolve_signup(interaction, captain)
        if found is None:
            return
        if found.role is not Role.CAPTAIN:
            await interaction.response.send_message(
                f"**{found.nickname}** is signed up as a player. Use `/captain add` to make them a captain candidate.", ephemeral=True
            )
            return
        assert interaction.guild is not None
        config = await self.bot.db.get_config(interaction.guild.id)
        names = await self.bot.db.division_names(found.tournament_id, config.division_count)
        embed = signup_details_embed(found, f"🎖️ {found.nickname}")
        embed.description = captain_card.status_line(found, names)
        options = await captain_card.division_options(self.bot, interaction.guild.id, found)
        await interaction.response.send_message(embed=embed, view=CaptainEditView(found, options), ephemeral=True)

    @captain_group.command(name="add", description="Make a signed-up player a captain candidate (posts an Accept/Reject card).")
    @app_commands.describe(player="Nickname or Discord username of a player signup")
    @app_commands.autocomplete(player=signup_autocomplete(Role.PLAYER))
    @admin_only()
    async def captain_add(self, interaction: Interaction, player: str) -> None:
        found = await resolve_signup(interaction, player)
        if found is None:
            return
        assert interaction.guild is not None
        try:
            signup = await captain_card.player_to_captain(self.bot, interaction.guild.id, found.id, interaction.user.id)
        except ActionRefused as exc:
            await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
            return
        await interaction.response.send_message(
            f"🎖️ **{signup.nickname}** is now a captain candidate: accept or reject their card in the admin channel.", ephemeral=True
        )

    # ----------------------------------------------------------------- /export

    @app_commands.command(name="export", description="Post an export file in the admin channel.")
    @app_commands.describe(what="Which file")
    @app_commands.choices(what=EXPORT_CHOICES)
    @app_commands.guild_only()
    @admin_only()
    async def export(self, interaction: Interaction, what: str) -> None:
        await actions.export(interaction, ExportType(what))


async def setup(bot: DraftCupBot) -> None:
    await bot.add_cog(ManagementCog(bot))
