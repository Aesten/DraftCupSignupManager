"""Ephemeral signup flow: agreement step, signup modal, "My signup" (spec §5)."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING

import discord

from .. import rules
from ..db import NicknameTaken, utcnow
from ..models import Role, Signup

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
    return tournament is not None and signups_open(tournament, config)


def agreement_text(role: Role) -> str:
    """Rules and dates are announced in the server's own channels; users confirm they read them."""
    if role is Role.CAPTAIN:
        return (
            "I have read the rules and can attend the **auction** and the **tournament**. "
            "If I'm not accepted as captain, I'll play as a player."
        )
    return "I have read the rules and can attend the **tournament**."


def signup_embed(signup: Signup, status: str = "") -> discord.Embed:
    """User-facing summary, titled "<Role> Registration[ <status>]". Tier and budget are left out on
    purpose: they're for organisers."""
    title = f"{signup.role.label} Registration" + (f" {status}" if status else "")
    lines = [
        f"**Nickname:** {signup.nickname}",
        f"**Class:** {rules.CLASS_LABELS[signup.player_class]}",
        f"**Division:** {signup.highest_division or 'None'}",
        f"**IGL:** {'Yes' if signup.igl else 'No'}",
        f"**Steam:** {signup.steam_url}",
    ]
    return discord.Embed(title=title, description="\n".join(lines), colour=discord.Colour.blurple())


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
    assert tournament is not None  # signups are open
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
    signup = await db.get_active_signup(tournament.id, interaction.user.id) if tournament else None
    if signup is None:
        await interaction.response.send_message("You're not signed up.", ephemeral=True)
        return
    if await _is_open(interaction):
        await interaction.response.send_message(
            embed=signup_embed(signup), view=MySignupView(signup), ephemeral=True
        )
    else:
        await interaction.response.send_message(
            "Signups are closed, so this is read-only. Contact an organiser for any change.",
            embed=signup_embed(signup),
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
    lines = [f"## {role.label} signup"]
    if switching_from is not None:
        lines.append(
            f"You're currently signed up as a **{switching_from.label.lower()}**. "
            f"Continuing switches your signup to **{role.label.lower()}**."
        )
    lines.append(f"> {agreement_text(role)}")
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
    `target` is set when an organiser fills in someone else's signup: (user ID, username). The open/closed
    check is then skipped.
    """

    def __init__(
        self,
        role: Role,
        form: rules.SignupForm,
        agreed_at: datetime | None,
        *,
        target: tuple[int, str] | None = None,
    ) -> None:
        title = f"{role.label} signup" if target is None else f"{role.label} signup of {target[1]}"
        super().__init__(title=title[:45], timeout=VIEW_TIMEOUT)
        self.role = role
        self.form = form
        self.agreed_at = agreed_at
        self.target = target

        # Discord checks lengths and required fields inside the form; everything else is checked on submit.
        self.nickname = discord.ui.TextInput(
            min_length=rules.NICKNAME_MIN, max_length=rules.NICKNAME_MAX, default=form.nickname or None,
            placeholder="Your in-game name",
        )
        self.steam = discord.ui.TextInput(
            min_length=len("steamcommunity.com/id/xx"), max_length=120, default=form.steam_url or None,
            placeholder="https://steamcommunity.com/id/…",
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
        from .. import events  # events imports signup_post, which imports this module

        assert interaction.guild is not None
        if self.target is None and not await _is_open(interaction):
            await interaction.response.send_message(CLOSED_MESSAGE, ephemeral=True)
            return

        form = self.typed_form()
        target_id, target_name = self.target or (interaction.user.id, interaction.user.name)
        db = interaction.client.db
        fields, errors = rules.validate_form(form)
        tournament = await db.active_tournament(interaction.guild.id)
        if tournament is None:
            await interaction.response.send_message("There's no tournament running.", ephemeral=True)
            return
        if fields is not None:
            try:
                signup, kind, details = await db.save_signup(
                    tournament.id, target_id, target_name, self.role, fields, self.agreed_at, interaction.user.id
                )
            except NicknameTaken as exc:
                errors = [str(exc)]

        if errors:
            retry = RetryView(self.role, form, self.agreed_at, target=self.target)
            await interaction.response.send_message(
                "The signup couldn't be saved:\n" + "\n".join(f"- {error}" for error in errors),
                view=retry,
                ephemeral=True,
            )
            return

        status = {"signup": "Complete", "unchanged": "(nothing changed)"}.get(kind, "Updated")
        embed = signup_embed(signup, status)
        content = f"Saved for <@{target_id}>." if self.target is not None else None
        await interaction.response.send_message(content, embed=embed, ephemeral=True)
        await events.signup_changed(interaction.client, interaction.guild.id, signup, kind, details, interaction.user.id)

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
        target: tuple[int, str] | None,
    ) -> None:
        super().__init__(timeout=VIEW_TIMEOUT)
        self.modal_args = (role, form, agreed_at)
        self.target = target

    @discord.ui.button(label="Try again", style=discord.ButtonStyle.primary)
    async def retry(self, interaction: Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(SignupModal(*self.modal_args, target=self.target))


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
    """`admin`: an organiser removing someone's signup, allowed whether signups are open or not."""

    def __init__(self, signup_id: int, *, admin: bool = False) -> None:
        super().__init__(timeout=VIEW_TIMEOUT)
        self.signup_id = signup_id
        self.admin = admin

    @discord.ui.button(label="Yes, withdraw", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: Interaction, _: discord.ui.Button) -> None:
        from .. import events

        assert interaction.guild is not None
        if not self.admin and not await _is_open(interaction):
            await interaction.response.edit_message(content=CLOSED_MESSAGE, view=None)
            return
        signup = await interaction.client.db.withdraw_signup(self.signup_id, interaction.user.id)
        if signup is None:
            await interaction.response.edit_message(content="This signup was already withdrawn.", view=None)
            return
        who = f"The signup of **{signup.nickname}** was" if self.admin else "Your signup was"
        await interaction.response.edit_message(content=f"{who} withdrawn.", view=None)
        details = {"role": signup.role.value, "nickname": signup.nickname}
        await events.signup_changed(interaction.client, interaction.guild.id, signup, "withdraw", details, interaction.user.id)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: Interaction, _: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="Nothing changed.", view=None)
