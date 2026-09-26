"""Organiser commands on signups, captains and exports (spec §7)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from .. import actions, events, rules
from ..db import NicknameTaken, utcnow
from ..models import CaptainStatus, ExportType, Role, Signup
from ..permissions import admin_only, interaction_is_admin
from ..views.captain_card import apply_captain_status, signup_details_embed
from ..views.captains_list import CaptainsListView, list_embed
from ..views.signup_flow import ConfirmWithdrawView, SignupModal

if TYPE_CHECKING:
    from ..bot import DraftCupBot

    Interaction = discord.Interaction[DraftCupBot]

log = logging.getLogger(__name__)

ROLE_CHOICES = [app_commands.Choice(name=r.label, value=r.value) for r in Role]
STATUS_CHOICES = [
    app_commands.Choice(name="Pending", value=CaptainStatus.PENDING.value),
    app_commands.Choice(name="Picked (give a division)", value=CaptainStatus.PICKED.value),
    app_commands.Choice(name="Pool (plays as a player)", value=CaptainStatus.POOL.value),
]


async def signup_autocomplete(interaction: Interaction, current: str) -> list[app_commands.Choice[str]]:
    """Active signups by nickname or Discord username. Empty for non-organisers."""
    if interaction.guild is None or not await interaction_is_admin(interaction):
        return []
    db = interaction.client.db
    tournament = await db.active_tournament(interaction.guild.id)
    return [
        app_commands.Choice(name=f"{s.nickname} ({s.role.value}, @{s.username})"[:100], value=str(s.id))
        for s in await db.find_signups(tournament.id, current)
    ]


async def resolve_signup(interaction: Interaction, value: str) -> Signup | None:
    """Turns an autocomplete value (signup ID) or a typed nickname into an active signup of this server."""
    assert interaction.guild is not None
    db = interaction.client.db
    tournament = await db.active_tournament(interaction.guild.id)
    signup = None
    if value.isdigit():
        signup = await db.get_signup(int(value))
    if signup is None or signup.tournament_id != tournament.id:
        matches = [s for s in await db.find_signups(tournament.id, value) if s.nickname.lower() == value.strip().lower()]
        signup = matches[0] if matches else None
    if signup is None or signup.withdrawn_at is not None:
        await interaction.response.send_message(f"❌ No active signup matches `{value}`.", ephemeral=True)
        return None
    return signup


class ManagementCog(commands.Cog):
    def __init__(self, bot: DraftCupBot) -> None:
        self.bot = bot

    # ----------------------------------------------------------------- /signup

    signup_group = app_commands.Group(name="signup", description="View or change someone's signup.", guild_only=True)

    @signup_group.command(name="view", description="Show a signup with its tier and budget.")
    @app_commands.describe(signup="Nickname or Discord username")
    @app_commands.autocomplete(signup=signup_autocomplete)
    @admin_only()
    async def signup_view(self, interaction: Interaction, signup: str) -> None:
        found = await resolve_signup(interaction, signup)
        if found is not None:
            title = f"{found.nickname}" + (f" · captain {found.captain_status.value}" if found.captain_status else "")
            await interaction.response.send_message(embed=signup_details_embed(found, title), ephemeral=True)

    @signup_group.command(name="edit", description="Edit a signup, whether signups are open or closed.")
    @app_commands.describe(signup="Nickname or Discord username")
    @app_commands.autocomplete(signup=signup_autocomplete)
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
        if await self.bot.db.get_active_signup(tournament.id, user.id) is not None:
            await interaction.response.send_message(
                f"{user.mention} is already signed up. Use `/signup edit` or `/signup role`.", ephemeral=True
            )
            return
        # The organiser vouches for the agreement, so it's recorded as accepted now.
        modal = SignupModal(Role(role), rules.SignupForm(), agreed_at=utcnow(), target=(user.id, user.name))
        await interaction.response.send_modal(modal)

    @signup_group.command(name="role", description="Switch a signup between player and captain.")
    @app_commands.describe(signup="Nickname or Discord username")
    @app_commands.autocomplete(signup=signup_autocomplete)
    @app_commands.choices(role=ROLE_CHOICES)
    @admin_only()
    async def signup_role(self, interaction: Interaction, signup: str, role: str) -> None:
        assert interaction.guild is not None
        found = await resolve_signup(interaction, signup)
        if found is None:
            return
        new_role = Role(role)
        if found.role is new_role:
            await interaction.response.send_message(f"**{found.nickname}** is already a {role}.", ephemeral=True)
            return
        try:
            saved, kind, details = await self.bot.db.save_signup(
                found.tournament_id, found.user_id, found.username, new_role, found.fields, None, interaction.user.id
            )
        except NicknameTaken as exc:  # can't happen for an unchanged nickname, but keep the reply clean
            await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
            return
        await interaction.response.send_message(f"✅ **{saved.nickname}** is now a {role}.", ephemeral=True)
        await events.signup_changed(self.bot, interaction.guild.id, saved, kind, details, interaction.user.id)

    @signup_group.command(name="remove", description="Withdraw a signup.")
    @app_commands.describe(signup="Nickname or Discord username")
    @app_commands.autocomplete(signup=signup_autocomplete)
    @admin_only()
    async def signup_remove(self, interaction: Interaction, signup: str) -> None:
        found = await resolve_signup(interaction, signup)
        if found is not None:
            await interaction.response.send_message(
                f"Withdraw the {found.role.value} signup of **{found.nickname}** (<@{found.user_id}>)?",
                view=ConfirmWithdrawView(found.id, admin=True),
                ephemeral=True,
            )

    # ---------------------------------------------------------------- captains

    @app_commands.command(name="captain", description="Pick a captain for a division, move them to the pool, or reset.")
    @app_commands.guild_only()
    @app_commands.describe(signup="Captain candidate", status="Decision", division="Division number, for Picked")
    @app_commands.autocomplete(signup=signup_autocomplete)
    @app_commands.choices(status=STATUS_CHOICES)
    @admin_only()
    async def captain_set(
        self, interaction: Interaction, signup: str, status: str, division: app_commands.Range[int, 1, 10] | None = None
    ) -> None:
        found = await resolve_signup(interaction, signup)
        if found is not None:
            await apply_captain_status(interaction, found.id, CaptainStatus(status), division, edit_card=False)

    @app_commands.command(name="captains", description="List captain candidates and decide for several at once.")
    @app_commands.guild_only()
    @admin_only()
    async def captains_list(self, interaction: Interaction) -> None:
        assert interaction.guild is not None
        db = self.bot.db
        config = await db.get_config(interaction.guild.id)
        tournament = await db.active_tournament(interaction.guild.id)
        signups = await db.list_active_signups(tournament.id)
        names = await db.division_names(tournament.id, config.division_count)
        await interaction.response.send_message(
            embed=list_embed(config, signups, names), view=CaptainsListView(signups, names), ephemeral=True
        )

    # ----------------------------------------------------------------- exports

    export_group = app_commands.Group(name="export", description="Export signups (posted in the admin channel).", guild_only=True)

    async def _export(self, interaction: Interaction, export_type: ExportType) -> None:
        await actions.export(interaction, export_type)

    @export_group.command(name="csv", description="Full signup data as players.csv and captains.csv.")
    @admin_only()
    async def export_csv(self, interaction: Interaction) -> None:
        await self._export(interaction, ExportType.CSV)

    @export_group.command(name="players", description="Player list JSON, for late signups into the auction app.")
    @admin_only()
    async def export_players(self, interaction: Interaction) -> None:
        await self._export(interaction, ExportType.PLAYERS)

    @export_group.command(name="tournament", description="The .draftcup.json file (every captain must be decided).")
    @admin_only()
    async def export_tournament(self, interaction: Interaction) -> None:
        await self._export(interaction, ExportType.TOURNAMENT)


async def setup(bot: DraftCupBot) -> None:
    await bot.add_cog(ManagementCog(bot))
