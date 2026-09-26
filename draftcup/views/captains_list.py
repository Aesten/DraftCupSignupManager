"""/captains list: bulk captain decisions in one ephemeral message (spec §6.2)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord

from .. import exports
from ..models import CaptainStatus, GuildConfig, Signup
from ..db import ActionRefused
from ..permissions import interaction_is_admin
from .captain_card import after_captain_status, set_captain_status

if TYPE_CHECKING:
    from ..bot import DraftCupBot

    Interaction = discord.Interaction[DraftCupBot]

SELECT_LIMIT = 25  # Discord's maximum number of options in a select menu
_STATUS_ORDER = {CaptainStatus.PENDING: 0, CaptainStatus.PICKED: 1, CaptainStatus.POOL: 2}


def _line(signup: Signup) -> str:
    return f"**{signup.nickname}** · {signup.player_class} · div {signup.highest_division or '—'} · {signup.budget:.1f}"


def list_embed(config: GuildConfig, signups: list[Signup], division_names: list[str]) -> discord.Embed:
    candidates = exports.captain_candidates(signups)
    embed = discord.Embed(title="Captain candidates", colour=discord.Colour.gold())
    groups: list[tuple[str, list[Signup]]] = [
        ("⏳ Pending", [s for s in candidates if s.captain_status is CaptainStatus.PENDING]),
    ]
    for index, name in enumerate(division_names, start=1):
        picked = [s for s in candidates if s.captain_status is CaptainStatus.PICKED and s.division_index == index]
        groups.append((f"✅ {name} ({len(picked)}/{config.captains_per_division})", picked))
    groups.append(("↩️ Pool", [s for s in candidates if s.captain_status is CaptainStatus.POOL]))
    for title, members in groups:
        value = "\n".join(_line(s) for s in members) or "—"
        if len(value) > 1024:
            value = value[:1000].rsplit("\n", 1)[0] + "\n…"
        embed.add_field(name=title, value=value, inline=False)
    if not candidates:
        embed.description = "No captain candidates yet."
    return embed


class CaptainsListView(discord.ui.View):
    """Pick captains in the select menu, then press a division, Pool or Reset to apply it to all of them."""

    def __init__(self, signups: list[Signup], division_names: list[str]) -> None:
        super().__init__(timeout=15 * 60)
        self.selected: list[int] = []
        candidates = sorted(
            exports.captain_candidates(signups),
            key=lambda s: (_STATUS_ORDER[s.captain_status or CaptainStatus.PENDING], s.nickname.lower()),
        )
        shown = candidates[:SELECT_LIMIT]
        if shown:
            select = discord.ui.Select(
                placeholder="Select captains…" if len(candidates) <= SELECT_LIMIT else f"Select captains (first {SELECT_LIMIT}, pending first)…",
                min_values=1,
                max_values=len(shown),
                options=[
                    discord.SelectOption(
                        label=s.nickname,
                        value=str(s.id),
                        description=f"{(s.captain_status or CaptainStatus.PENDING).value} · {s.player_class} · budget {s.budget:.1f}",
                    )
                    for s in shown
                ],
                row=0,
            )
            select.callback = self._on_select
            self.select = select
            self.add_item(select)
        for index, name in enumerate(division_names, start=1):
            self._add_action(name[:80], CaptainStatus.PICKED, index, discord.ButtonStyle.primary)
        self._add_action("Pool", CaptainStatus.POOL, None, discord.ButtonStyle.secondary)
        self._add_action("Reset", CaptainStatus.PENDING, None, discord.ButtonStyle.danger)

    def _add_action(self, label: str, status: CaptainStatus, division: int | None, style: discord.ButtonStyle) -> None:
        button = discord.ui.Button(label=label, style=style)

        async def callback(interaction: Interaction) -> None:
            await self._apply(interaction, status, division)

        button.callback = callback
        self.add_item(button)

    async def _on_select(self, interaction: Interaction) -> None:
        self.selected = [int(value) for value in self.select.values]
        await interaction.response.defer()

    async def _apply(self, interaction: Interaction, status: CaptainStatus, division: int | None) -> None:
        if not self.selected:
            await interaction.response.send_message("Select captains in the menu first.", ephemeral=True)
            return
        assert interaction.guild is not None
        if not await interaction_is_admin(interaction):
            await interaction.response.send_message("Only organisers can pick captains.", ephemeral=True)
            return
        bot, guild_id = interaction.client, interaction.guild.id
        await interaction.response.defer()
        failures = []
        applied = []
        for signup_id in self.selected:
            try:
                applied.append(await set_captain_status(bot, guild_id, signup_id, status, division, interaction.user.id))
            except ActionRefused as exc:
                signup = await bot.db.get_signup(signup_id)
                failures.append(f"❌ {signup.nickname if signup else signup_id}: {exc}")

        config = await bot.db.get_config(guild_id)
        tournament = await bot.db.active_tournament(guild_id)
        signups = await bot.db.list_active_signups(tournament.id)
        names = await bot.db.division_names(tournament.id, config.division_count)
        await interaction.edit_original_response(
            content="\n".join(failures) or None,
            embed=list_embed(config, signups, names),
            view=CaptainsListView(signups, names),
        )
        for signup, details, division_names in applied:
            await after_captain_status(
                bot, guild_id, signup, details, division_names, interaction.user.id, update_card=True
            )
