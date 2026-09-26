"""Ephemeral signup flow: agreement step, signup modal, "My signup" (spec §5)."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING

import discord

from .. import rules
from ..db import NicknameTaken, utcnow
from ..models import GuildConfig, Role, Signup
from ..notify import notify_admins, signup_change_line
from ..timeutil import discord_ts

if TYPE_CHECKING:
    from ..bot import DraftCupBot

    Interaction = discord.Interaction[DraftCupBot]

log = logging.getLogger(__name__)

VIEW_TIMEOUT = 15 * 60
CLOSED_MESSAGE = "🔒 Signups are closed, contact an organiser."


async def _is_open(interaction: Interaction) -> bool:
    # Imported here: signup_post imports this module for its button callbacks.
    from .signup_post import signups_open

    assert interaction.guild is not None
    db = interaction.client.db
    config = await db.get_config(interaction.guild.id)
    tournament = await db.active_tournament(interaction.guild.id)
    return signups_open(tournament, config)


def agreement_text(role: Role, config: GuildConfig) -> str:
    rules_part = f"the [rules]({config.rules_url})" if config.rules_url else "the rules"
    if role is Role.CAPTAIN:
        return (
            f"I agree to {rules_part} and can attend both the **auction** on {discord_ts(config.auction_date)} "
            f"and the **tournament** on {discord_ts(config.tournament_date)}. "
            "If I'm not picked as captain, I'll play as a player."
        )
    return f"I agree to {rules_part} and can attend the **tournament** on {discord_ts(config.tournament_date)}."


def signup_embed(signup: Signup, heading: str) -> discord.Embed:
    """User-facing summary. Tier and budget are left out on purpose: they're for organisers."""
    embed = discord.Embed(title=heading, colour=discord.Colour.blurple())
    embed.add_field(name="Role", value=signup.role.label)
    embed.add_field(name="Nickname", value=signup.nickname)
    embed.add_field(name="Class", value=rules.CLASS_LABELS[signup.player_class])
    embed.add_field(name="Highest division", value=signup.highest_division or "None")
    embed.add_field(name="In-game leader", value="Yes" if signup.igl else "No")
    embed.add_field(name="Steam", value=signup.steam_url, inline=False)
    embed.set_footer(text=f"Agreed to the rules on {signup.agreed_at:%Y-%m-%d %H:%M} UTC")
    return embed


# --------------------------------------------------------------------- entry points


async def start_signup(interaction: Interaction, role: Role) -> None:
    """Handles the "Sign up as Player/Captain" buttons of the signup post."""
    if interaction.guild is None:
        return
    if not await _is_open(interaction):
        await interaction.response.send_message(CLOSED_MESSAGE, ephemeral=True)
        return
    db = interaction.client.db
    tournament = await db.active_tournament(interaction.guild.id)
    existing = await db.get_active_signup(tournament.id, interaction.user.id)

    if existing is not None and existing.role is role:
        # Already signed up with this role: straight to the pre-filled form, no new agreement needed.
        await interaction.response.send_modal(SignupModal(role, existing.fields.to_form(), agreed_at=None))
        return

    prefill = existing.fields.to_form() if existing else rules.SignupForm()
    await send_agreement(interaction, role, prefill, switching_from=existing.role if existing else None)


async def show_my_signup(interaction: Interaction) -> None:
    """Handles the "My signup" button of the signup post."""
    if interaction.guild is None:
        return
    db = interaction.client.db
    tournament = await db.active_tournament(interaction.guild.id)
    signup = await db.get_active_signup(tournament.id, interaction.user.id)
    if signup is None:
        await interaction.response.send_message("You're not signed up.", ephemeral=True)
        return
    if await _is_open(interaction):
        await interaction.response.send_message(
            embed=signup_embed(signup, "Your signup"), view=MySignupView(signup), ephemeral=True
        )
    else:
        await interaction.response.send_message(
            "Signups are closed, so this is read-only. Contact an organiser for any change.",
            embed=signup_embed(signup, "Your signup"),
            ephemeral=True,
        )


# ------------------------------------------------------------------ agreement step


async def send_agreement(
    interaction: Interaction,
    role: Role,
    prefill: rules.SignupForm,
    switching_from: Role | None,
    *,
    edit_message: bool = False,
) -> None:
    assert interaction.guild is not None
    config = await interaction.client.db.get_config(interaction.guild.id)
    lines = [f"## {role.label} signup"]
    if switching_from is not None:
        lines.append(
            f"You're currently signed up as a **{switching_from.label.lower()}**. "
            f"Continuing switches your signup to **{role.label.lower()}**."
        )
    lines.append(f"> {agreement_text(role, config)}")
    content = "\n".join(lines)
    view = AgreementView(role, prefill)
    if edit_message:
        await interaction.response.edit_message(content=content, embed=None, view=view)
    else:
        await interaction.response.send_message(content, view=view, ephemeral=True)


class AgreementView(discord.ui.View):
    def __init__(self, role: Role, prefill: rules.SignupForm) -> None:
        super().__init__(timeout=VIEW_TIMEOUT)
        self.role = role
        self.prefill = prefill

    @discord.ui.button(label="I agree and can attend", style=discord.ButtonStyle.success)
    async def agree(self, interaction: Interaction, _: discord.ui.Button) -> None:
        if not await _is_open(interaction):
            await interaction.response.send_message(CLOSED_MESSAGE, ephemeral=True)
            return
        await interaction.response.send_modal(SignupModal(self.role, self.prefill, agreed_at=utcnow()))


# ---------------------------------------------------------------------- the modal


class SignupModal(discord.ui.Modal):
    """The 5-field signup form. Discord allows at most 5 fields, hence the separate agreement step.

    `agreed_at` is when the agreement step was accepted, or None for an edit (keeps the stored time).
    `target` and `admin` are for organisers editing someone else's signup: the open/closed check is skipped.
    """

    def __init__(
        self,
        role: Role,
        form: rules.SignupForm,
        agreed_at: datetime | None,
        *,
        target: discord.abc.User | None = None,
        admin: bool = False,
    ) -> None:
        super().__init__(title=f"{role.label} signup", timeout=VIEW_TIMEOUT)
        self.role = role
        self.agreed_at = agreed_at
        self.target = target
        self.admin = admin

        self.nickname = discord.ui.TextInput(
            max_length=rules.NICKNAME_MAX, default=form.nickname or None, placeholder="Your in-game name"
        )
        self.steam = discord.ui.TextInput(
            max_length=120, default=form.steam_url or None, placeholder="https://steamcommunity.com/id/…"
        )
        self.player_class = discord.ui.RadioGroup(
            options=[
                discord.RadioGroupOption(label=label, value=code, default=form.player_class == code)
                for code, label in rules.CLASS_LABELS.items()
            ]
        )
        self.division = discord.ui.TextInput(
            required=False, max_length=1, default=form.highest_division or None, placeholder="A, B, C…"
        )
        self.igl = discord.ui.RadioGroup(
            options=[
                discord.RadioGroupOption(label="Yes, I can lead", value="yes", default=form.igl == "yes"),
                discord.RadioGroupOption(label="No", value="no", default=form.igl == "no"),
            ]
        )
        self.add_item(discord.ui.Label(
            text="Nickname",
            description=f"{rules.NICKNAME_MIN}–{rules.NICKNAME_MAX} characters: letters, digits, spaces, _ and -",
            component=self.nickname,
        ))
        self.add_item(discord.ui.Label(text="Steam profile link", component=self.steam))
        self.add_item(discord.ui.Label(text="Class", component=self.player_class))
        self.add_item(discord.ui.Label(
            text="Highest division played",
            description="The letter of the highest competitive division you played in. Leave empty if none.",
            component=self.division,
        ))
        self.add_item(discord.ui.Label(text="Can you lead in game (IGL)?", component=self.igl))

    def typed_form(self) -> rules.SignupForm:
        return rules.SignupForm(
            nickname=self.nickname.value,
            steam_url=self.steam.value,
            player_class=self.player_class.value,
            highest_division=self.division.value,
            igl=self.igl.value,
        )

    async def on_submit(self, interaction: Interaction) -> None:
        assert interaction.guild is not None
        if not self.admin and not await _is_open(interaction):
            await interaction.response.send_message(CLOSED_MESSAGE, ephemeral=True)
            return

        form = self.typed_form()
        target = self.target or interaction.user
        db = interaction.client.db
        fields, errors = rules.validate_form(form)
        if fields is not None:
            tournament = await db.active_tournament(interaction.guild.id)
            try:
                signup, kind, details = await db.save_signup(
                    tournament.id, target.id, target.name, self.role, fields, self.agreed_at, interaction.user.id
                )
            except NicknameTaken as exc:
                errors = [str(exc)]

        if errors:
            retry = RetryView(self.role, form, self.agreed_at, target=self.target, admin=self.admin)
            await interaction.response.send_message(
                "Your signup couldn't be saved:\n" + "\n".join(f"- {error}" for error in errors),
                view=retry,
                ephemeral=True,
            )
            return

        heading = {"signup": "✅ You're signed up!", "unchanged": "Nothing changed"}.get(kind, "✅ Signup updated")
        if self.target is not None:
            heading = f"Signup of {target.name} saved"
        await interaction.response.send_message(embed=signup_embed(signup, heading), ephemeral=True)
        if kind != "unchanged":
            line = signup_change_line(
                target.id, interaction.user.id, kind, details, signup.player_class, signup.highest_division
            )
            await notify_admins(interaction.client, interaction.guild.id, line)

    async def on_error(self, interaction: Interaction, error: Exception) -> None:
        log.exception("Signup modal failed", exc_info=error)
        message = "Something went wrong while saving your signup. Please try again or contact an organiser."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)


class RetryView(discord.ui.View):
    def __init__(
        self,
        role: Role,
        form: rules.SignupForm,
        agreed_at: datetime | None,
        *,
        target: discord.abc.User | None,
        admin: bool,
    ) -> None:
        super().__init__(timeout=VIEW_TIMEOUT)
        self.modal_args = (role, form, agreed_at)
        self.modal_kwargs = {"target": target, "admin": admin}

    @discord.ui.button(label="Try again", style=discord.ButtonStyle.primary)
    async def retry(self, interaction: Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(SignupModal(*self.modal_args, **self.modal_kwargs))


# ------------------------------------------------------------------- My signup


class MySignupView(discord.ui.View):
    def __init__(self, signup: Signup) -> None:
        super().__init__(timeout=VIEW_TIMEOUT)
        self.signup_id = signup.id
        self.other_role = Role.CAPTAIN if signup.role is Role.PLAYER else Role.PLAYER
        self.switch.label = f"Switch to {self.other_role.label.lower()}"

    async def _current(self, interaction: Interaction) -> Signup | None:
        """Re-reads the signup and re-checks that signups are open; replies itself when not."""
        if not await _is_open(interaction):
            await interaction.response.send_message(CLOSED_MESSAGE, ephemeral=True)
            return None
        signup = await interaction.client.db.get_signup(self.signup_id)
        if signup is None or signup.withdrawn_at is not None:
            await interaction.response.send_message("This signup no longer exists.", ephemeral=True)
            return None
        return signup

    @discord.ui.button(label="Edit", style=discord.ButtonStyle.primary)
    async def edit(self, interaction: Interaction, _: discord.ui.Button) -> None:
        signup = await self._current(interaction)
        if signup is not None:
            await interaction.response.send_modal(SignupModal(signup.role, signup.fields.to_form(), agreed_at=None))

    @discord.ui.button(label="Switch", style=discord.ButtonStyle.secondary)
    async def switch(self, interaction: Interaction, _: discord.ui.Button) -> None:
        signup = await self._current(interaction)
        if signup is not None:
            await send_agreement(
                interaction, self.other_role, signup.fields.to_form(), switching_from=signup.role, edit_message=True
            )

    @discord.ui.button(label="Withdraw", style=discord.ButtonStyle.danger)
    async def withdraw(self, interaction: Interaction, _: discord.ui.Button) -> None:
        signup = await self._current(interaction)
        if signup is not None:
            await interaction.response.edit_message(
                content="Withdraw your signup? Your nickname becomes available to others.",
                embed=None,
                view=ConfirmWithdrawView(signup.id),
            )


class ConfirmWithdrawView(discord.ui.View):
    def __init__(self, signup_id: int) -> None:
        super().__init__(timeout=VIEW_TIMEOUT)
        self.signup_id = signup_id

    @discord.ui.button(label="Yes, withdraw", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: Interaction, _: discord.ui.Button) -> None:
        assert interaction.guild is not None
        if not await _is_open(interaction):
            await interaction.response.edit_message(content=CLOSED_MESSAGE, view=None)
            return
        signup = await interaction.client.db.withdraw_signup(self.signup_id, interaction.user.id)
        if signup is None:
            await interaction.response.edit_message(content="This signup was already withdrawn.", view=None)
            return
        await interaction.response.edit_message(content="Your signup was withdrawn.", view=None)
        line = signup_change_line(
            signup.user_id, interaction.user.id, "withdraw", {"role": signup.role.value, "nickname": signup.nickname}
        )
        await notify_admins(interaction.client, interaction.guild.id, line)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: Interaction, _: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="Nothing changed.", view=None)
