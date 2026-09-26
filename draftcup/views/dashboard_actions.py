"""What each dashboard button does: forms (modals) and ephemeral panels for organisers."""

from __future__ import annotations

import calendar
import logging
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any, Awaitable, Callable

import discord

from .. import actions, rules
from ..actions import ActionError
from ..db import utcnow
from ..models import ExportType, Format, GuildConfig, Role, State
from ..permissions import interaction_is_admin
from ..timeutil import format_day, local_today, parse_time, short_day, week_starts, zone
from .captain_card import signup_details_embed
from .captains_list import CaptainsListView, list_embed
from .signup_flow import ConfirmWithdrawView, SignupModal

if TYPE_CHECKING:
    from ..bot import DraftCupBot

    Interaction = discord.Interaction[DraftCupBot]

log = logging.getLogger(__name__)

PANEL_TIMEOUT = 15 * 60
MAX_DIVISIONS = 5  # one name field per division must fit in a modal (5 fields at most)
TEAM_SIZES = range(5, 11)  # players per team, not counting the captain (auction app limit)


async def _config(interaction: Interaction) -> GuildConfig:
    assert interaction.guild is not None
    return await interaction.client.db.get_config(interaction.guild.id)


async def _reply(interaction: Interaction, content: str, **kwargs: Any) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(content, ephemeral=True, **kwargs)
    else:
        await interaction.response.send_message(content, ephemeral=True, **kwargs)


def _saved_text(changed: list[str]) -> str:
    return "✅ Saved: " + "; ".join(changed) + "." if changed else "Nothing changed."


class OrganiserModal(discord.ui.Modal):
    """Base for dashboard forms: re-checks rights on submit and reports unexpected errors."""

    async def interaction_check(self, interaction: Interaction) -> bool:  # type: ignore[override]
        if await interaction_is_admin(interaction):
            return True
        await interaction.response.send_message("Only organisers can change this.", ephemeral=True)
        return False

    async def on_error(self, interaction: Interaction, error: Exception) -> None:  # type: ignore[override]
        if isinstance(error, (ActionError, ValueError)):
            await _reply(interaction, f"❌ {error}")
            return
        log.exception("Dashboard form failed", exc_info=error)
        await _reply(interaction, "Something went wrong. Check the bot logs.")


class OrganiserView(discord.ui.View):
    """Base for ephemeral organiser panels."""

    def __init__(self) -> None:
        super().__init__(timeout=PANEL_TIMEOUT)

    async def interaction_check(self, interaction: Interaction) -> bool:  # type: ignore[override]
        if await interaction_is_admin(interaction):
            return True
        await interaction.response.send_message("Only organisers can change this.", ephemeral=True)
        return False

    async def on_error(self, interaction: Interaction, error: Exception, item: discord.ui.Item) -> None:  # type: ignore[override]
        if isinstance(error, ActionError):
            await _reply(interaction, f"❌ {error}")
            return
        log.exception("Dashboard panel failed", exc_info=error)
        await _reply(interaction, "Something went wrong. Check the bot logs.")


def _radio(options: list[tuple[str, str]], current: str | None) -> discord.ui.RadioGroup:
    return discord.ui.RadioGroup(
        options=[discord.RadioGroupOption(label=label, value=value, default=value == current) for label, value in options]
    )


# ---------------------------------------------------------------- ⚙️ Tournament


class TournamentModal(OrganiserModal):
    def __init__(self, config: GuildConfig) -> None:
        super().__init__(title="Tournament settings", timeout=PANEL_TIMEOUT)
        self.name = discord.ui.TextInput(default=config.title, max_length=80)
        self.format = _radio([(f.label, f.value) for f in Format], config.format.value)
        self.team_size = _radio([(f"{n} players + captain", str(n)) for n in TEAM_SIZES], str(config.team_size))
        self.divisions = _radio(
            [(f"{n} division{'s' if n > 1 else ''}", str(n)) for n in range(1, MAX_DIVISIONS + 1)],
            str(min(config.division_count, MAX_DIVISIONS)),
        )
        self.teams = discord.ui.TextInput(default=str(config.captains_per_division), max_length=2)
        self.add_item(discord.ui.Label(text="Title", component=self.name))
        self.add_item(discord.ui.Label(
            text="Auction format", description="Tiers are exported in both formats.", component=self.format
        ))
        self.add_item(discord.ui.Label(text="Team size", component=self.team_size))
        self.add_item(discord.ui.Label(text="Divisions", component=self.divisions))
        self.add_item(discord.ui.Label(text="Teams (captains) per division", component=self.teams))

    async def on_submit(self, interaction: Interaction) -> None:  # type: ignore[override]
        title = " ".join(self.name.value.split())
        if not title:
            raise ValueError("The title can't be empty.")
        try:
            teams = int(self.teams.value)
        except ValueError:
            raise ValueError("Teams per division must be a number.") from None
        if not 2 <= teams <= 20:
            raise ValueError("Teams per division must be between 2 and 20.")
        fmt = Format(self.format.value)
        changes = {
            "title": title,
            "format": fmt,
            "team_size": int(self.team_size.value),
            "division_count": int(self.divisions.value),
            "captains_per_division": teams,
        }
        labels = {"format": fmt.label}
        assert interaction.guild is not None
        changed = await actions.update_settings(interaction.client, interaction.guild.id, interaction.user.id, changes, labels)
        await _reply(interaction, _saved_text(changed))


# -------------------------------------------------------------- 🔧 More settings


class AdvancedModal(OrganiserModal):
    def __init__(self, config: GuildConfig) -> None:
        super().__init__(title="More settings", timeout=PANEL_TIMEOUT)
        self.rules_url = discord.ui.TextInput(
            required=False, default=config.rules_url, placeholder="https://…", max_length=300
        )
        self.timezone = discord.ui.TextInput(default=config.timezone, max_length=50)
        self.close_time = discord.ui.TextInput(default=config.close_time, max_length=5)
        self.half_cap = _radio([("Yes", "yes"), ("No", "no")], "yes" if config.half_budget_cap else "no")
        self.add_item(discord.ui.Label(text="Rules link", description="Shown to everyone signing up.", component=self.rules_url))
        self.add_item(discord.ui.Label(
            text="Timezone", description="e.g. Europe/Paris (CET/CEST). Used for the close time.", component=self.timezone
        ))
        self.add_item(discord.ui.Label(
            text="Signups close at", description="Time of day on the close date, e.g. 23:59.", component=self.close_time
        ))
        self.add_item(discord.ui.Label(
            text="Half budget cap at auction start", description="Written into the tournament file.", component=self.half_cap
        ))

    async def on_submit(self, interaction: Interaction) -> None:  # type: ignore[override]
        url = self.rules_url.value.strip() or None
        if url and not url.startswith(("https://", "http://")):
            raise ValueError("The rules link must start with https://")
        tz_name = self.timezone.value.strip()
        zone(tz_name)  # raises ValueError with a readable message
        changes = {
            "rules_url": url,
            "timezone": tz_name,
            "close_time": parse_time(self.close_time.value),
            "half_budget_cap": self.half_cap.value == "yes",
        }
        labels = {"rules_url": url or "none", "half_budget_cap": self.half_cap.value}
        assert interaction.guild is not None
        changed = await actions.update_settings(interaction.client, interaction.guild.id, interaction.user.id, changes, labels)
        await _reply(interaction, _saved_text(changed))


# ------------------------------------------------------------------- 📅 Dates


class DatePickerModal(OrganiserModal):
    """Discord has no date picker: pick the week, then the day of that week."""

    def __init__(
        self,
        title: str,
        current: date | None,
        today: date,
        on_pick: Callable[[Interaction, date], Awaitable[None]],
        hint: str | None = None,
    ) -> None:
        super().__init__(title=title[:45], timeout=PANEL_TIMEOUT)
        self.today = today
        self.on_pick = on_pick
        weeks = week_starts(today)
        if current is not None and not weeks[0] <= current < weeks[-1] + timedelta(days=7):
            current = None
        self.week = discord.ui.Select(
            placeholder="Pick a week",
            required=True,
            options=[
                discord.SelectOption(
                    label=f"{short_day(monday)} – {short_day(monday + timedelta(days=6))}",
                    value=monday.isoformat(),
                    description="This week" if i == 0 else None,
                    default=current is not None and monday <= current < monday + timedelta(days=7),
                )
                for i, monday in enumerate(weeks)
            ],
        )
        self.day = _radio(
            [(calendar.day_name[i], str(i)) for i in range(7)], str(current.weekday()) if current else None
        )
        self.add_item(discord.ui.Label(text="Week", description=hint, component=self.week))
        self.add_item(discord.ui.Label(text="Day", component=self.day))

    async def on_submit(self, interaction: Interaction) -> None:  # type: ignore[override]
        picked = date.fromisoformat(self.week.values[0]) + timedelta(days=int(self.day.value))
        if picked < self.today:
            raise ValueError(f"{format_day(picked)} is in the past.")
        await self.on_pick(interaction, picked)


_DATE_FIELDS = {
    "close_date": "Signups close",
    "auction_date": "Auction",
    "tournament_date": "Tournament",
}


def dates_text(config: GuildConfig) -> str:
    lines = [
        "**📅 Dates**",
        f"Signups close: **{format_day(config.close_date)}** at {config.close_time} ({config.timezone})",
        f"Auction: **{format_day(config.auction_date)}**",
        f"Tournament: **{format_day(config.tournament_date)}**",
    ]
    if config.close_date and config.auction_date and config.close_date > config.auction_date:
        lines.append("⚠️ Signups close after the auction.")
    if config.auction_date and config.tournament_date and config.auction_date > config.tournament_date:
        lines.append("⚠️ The auction is after the tournament.")
    lines.append("-# Pick a button to change a date. The close time and timezone are under **More settings**.")
    return "\n".join(lines)


class DatesView(OrganiserView):
    def __init__(self, config: GuildConfig) -> None:
        super().__init__()
        for key, label in _DATE_FIELDS.items():
            value = getattr(config, key)
            button = discord.ui.Button(
                label=f"{label}: {short_day(value) if value else 'set'}", style=discord.ButtonStyle.primary, emoji="📅"
            )
            button.callback = self._opener(key, label)
            self.add_item(button)

    @staticmethod
    def _opener(key: str, label: str) -> Callable[[Interaction], Awaitable[None]]:
        async def open_picker(interaction: Interaction) -> None:
            config = await _config(interaction)

            async def on_pick(submit: Interaction, picked: date) -> None:
                assert submit.guild is not None
                await actions.update_settings(
                    submit.client, submit.guild.id, submit.user.id, {key: picked}, {key: format_day(picked)}
                )
                config = await _config(submit)
                await submit.response.edit_message(content=dates_text(config), view=DatesView(config))

            hint = f"Signups close at {config.close_time} ({config.timezone}) on that day." if key == "close_date" else None
            today = local_today(config.timezone)
            await interaction.response.send_modal(DatePickerModal(label, getattr(config, key), today, on_pick, hint))

        return open_picker


# --------------------------------------------------------------- 🏷️ Divisions


class DivisionsModal(OrganiserModal):
    def __init__(self, names: list[str]) -> None:
        super().__init__(title="Division names", timeout=PANEL_TIMEOUT)
        self.inputs = []
        for index, name in enumerate(names[:MAX_DIVISIONS], start=1):
            field = discord.ui.TextInput(default=name, max_length=40)
            self.inputs.append(field)
            self.add_item(discord.ui.Label(text=f"Division {index}", component=field))

    async def on_submit(self, interaction: Interaction) -> None:  # type: ignore[override]
        names = [" ".join(field.value.split()) for field in self.inputs]
        if not all(names):
            raise ValueError("Division names can't be empty.")
        assert interaction.guild is not None
        changed = await actions.rename_divisions(interaction.client, interaction.guild.id, interaction.user.id, names)
        await _reply(interaction, _saved_text(changed))


# -------------------------------------------------------------- 👮 Organisers


class OrganisersView(OrganiserView):
    def __init__(self, config: GuildConfig) -> None:
        super().__init__()
        self.roles = discord.ui.RoleSelect(
            placeholder="Organiser roles", min_values=0, max_values=25,
            default_values=[discord.Object(r) for r in config.admin_role_ids],
        )
        self.users = discord.ui.UserSelect(
            placeholder="Organiser members", min_values=0, max_values=25,
            default_values=[discord.Object(u) for u in config.admin_user_ids],
        )
        self.roles.callback = self._save_roles
        self.users.callback = self._save_users
        self.add_item(self.roles)
        self.add_item(self.users)

    async def _save(self, interaction: Interaction, kind: str, ids: set[int]) -> None:
        assert interaction.guild is not None
        bot, guild_id = interaction.client, interaction.guild.id
        if kind == "roles":
            await bot.db.replace_admins(guild_id, role_ids=ids)
            mentions = ", ".join(f"<@&{i}>" for i in ids) or "none"
        else:
            await bot.db.replace_admins(guild_id, user_ids=ids)
            mentions = ", ".join(f"<@{i}>" for i in ids) or "none"
        await interaction.response.edit_message(content=organisers_text(), view=self)
        await bot.feed.line(guild_id, f"👮 <@{interaction.user.id}> set the organiser {kind}: {mentions}")
        bot.refresher.request(guild_id)

    async def _save_roles(self, interaction: Interaction) -> None:
        await self._save(interaction, "roles", {role.id for role in self.roles.values})

    async def _save_users(self, interaction: Interaction) -> None:
        await self._save(interaction, "members", {user.id for user in self.users.values})


def organisers_text() -> str:
    return (
        "**👮 Organisers**\nPick the roles and members who can use the dashboard and the organiser commands. "
        "Changes are saved as soon as you close a menu.\n-# Members with *Manage Server* are always organisers."
    )


# ------------------------------------------------------ 🟢 Open / 🔒 Close / reset


class ConfirmView(OrganiserView):
    """One confirm button that runs `action`, then replaces the panel with its result text."""

    def __init__(self, label: str, style: discord.ButtonStyle, action: Callable[[Interaction], Awaitable[str]]) -> None:
        super().__init__()
        self.action = action
        confirm = discord.ui.Button(label=label, style=style)
        confirm.callback = self._confirm
        cancel = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary)
        cancel.callback = self._cancel
        self.add_item(confirm)
        self.add_item(cancel)

    async def _confirm(self, interaction: Interaction) -> None:
        await interaction.response.defer()
        try:
            text = await self.action(interaction)
        except ActionError as exc:
            text = f"❌ {exc}"
        await interaction.edit_original_response(content=text, view=None)

    async def _cancel(self, interaction: Interaction) -> None:
        await interaction.response.edit_message(content="Cancelled.", view=None)


async def _open_now(interaction: Interaction) -> str:
    assert interaction.guild is not None
    message = await actions.open_signups(interaction.client, interaction.guild.id, interaction.user.id)
    return f"🟢 Signups are open: {message.jump_url if message else ''}"


async def open_signups(interaction: Interaction) -> None:
    config = await _config(interaction)
    missing = actions.missing_for_opening(config)
    if missing:
        await _reply(interaction, "Before opening signups, set " + ", ".join(missing) + ".")
        return
    text = (
        f"**Open signups to the public?**\nThe signup post goes up in <#{config.signup_channel_id}>.\n"
        f"Format: **{config.format.label}**\nCloses: **{actions.close_text(config)}**\n"
        f"Auction: {format_day(config.auction_date)} · Tournament: {format_day(config.tournament_date)}"
    )
    await _reply(interaction, text, view=ConfirmView("Open signups now", discord.ButtonStyle.success, _open_now))


async def close_signups(interaction: Interaction) -> None:
    async def close_now(i: Interaction) -> str:
        assert i.guild is not None
        await actions.close_signups(i.client, i.guild.id, i.user.id)
        return "🔒 Signups are closed. The public post now says so."

    await _reply(
        interaction,
        "**Close signups now?** Only organisers will be able to change signups afterwards.",
        view=ConfirmView("Close signups now", discord.ButtonStyle.danger, close_now),
    )


async def reopen_signups(interaction: Interaction) -> None:
    """Reopening needs a close date in the future, so it starts with the date picker."""
    config = await _config(interaction)

    async def on_pick(submit: Interaction, picked: date) -> None:
        assert submit.guild is not None
        await actions.update_settings(submit.client, submit.guild.id, submit.user.id, {"close_date": picked}, {"close_date": format_day(picked)})
        await submit.response.defer(ephemeral=True, thinking=True)
        message = await actions.open_signups(submit.client, submit.guild.id, submit.user.id)
        await submit.followup.send(f"🟢 Signups reopened until {format_day(picked)} at {config.close_time}: {message.jump_url}", ephemeral=True)

    today = local_today(config.timezone)
    hint = f"Signups close at {config.close_time} ({config.timezone}) on that day."
    await interaction.response.send_modal(DatePickerModal("Reopen signups until…", config.close_date, today, on_pick, hint))


async def reset_tournament(interaction: Interaction) -> None:
    config = await _config(interaction)
    assert interaction.guild is not None
    tournament = await interaction.client.db.active_tournament(interaction.guild.id)
    if tournament.state is State.OPEN:
        await _reply(interaction, "Close signups before starting a new tournament.")
        return

    async def reset_now(i: Interaction) -> str:
        assert i.guild is not None
        await actions.reset_tournament(i.client, i.guild.id, i.user.id)
        return "🗃️ Archived. The dashboard is ready for the next tournament: set the dates, then open signups."

    await _reply(
        interaction,
        f"**Start a new tournament?** Every signup of **{config.title}** is archived (not deleted) and captain "
        "cards stop working. Settings, organisers and division names are kept. Export first if you still need the files.",
        view=ConfirmView("Archive and start over", discord.ButtonStyle.danger, reset_now),
    )


# ---------------------------------------------------------- 🔎 Manage a signup


class ManageSignupView(OrganiserView):
    """Pick a member to see their signup, then edit, switch role, withdraw, or add one for them."""

    def __init__(self, member: discord.abc.User | None = None, signup=None) -> None:
        super().__init__()
        self.member_select = discord.ui.UserSelect(
            placeholder="Pick a member", min_values=1, max_values=1,
            default_values=[member] if member is not None else [],
        )
        self.member_select.callback = self._picked
        self.add_item(self.member_select)
        self.member = member
        self.signup = signup
        if member is None:
            return
        if signup is None:
            for role in Role:
                button = discord.ui.Button(label=f"Add as {role.label.lower()}", style=discord.ButtonStyle.success)
                button.callback = self._adder(role)
                self.add_item(button)
            return
        other = Role.CAPTAIN if signup.role is Role.PLAYER else Role.PLAYER
        for label, style, callback in (
            ("Edit", discord.ButtonStyle.primary, self._edit),
            (f"Make {other.label.lower()}", discord.ButtonStyle.secondary, self._switch),
            ("Withdraw", discord.ButtonStyle.danger, self._withdraw),
        ):
            button = discord.ui.Button(label=label, style=style)
            button.callback = callback
            self.add_item(button)

    @classmethod
    async def render(cls, interaction: Interaction, member: discord.abc.User | None) -> tuple[str, discord.Embed | None, ManageSignupView]:
        if member is None:
            return manage_text(), None, cls()
        assert interaction.guild is not None
        db = interaction.client.db
        tournament = await db.active_tournament(interaction.guild.id)
        signup = await db.get_active_signup(tournament.id, member.id)
        if signup is None:
            return f"{member.mention} isn't signed up.", None, cls(member, None)
        return "", signup_details_embed(signup, signup.nickname), cls(member, signup)

    async def _show(self, interaction: Interaction, member: discord.abc.User | None) -> None:
        content, embed, view = await self.render(interaction, member)
        await interaction.response.edit_message(content=content or None, embed=embed, view=view)

    async def _picked(self, interaction: Interaction) -> None:
        await self._show(interaction, self.member_select.values[0])

    def _adder(self, role: Role) -> Callable[[Interaction], Awaitable[None]]:
        async def add(interaction: Interaction) -> None:
            assert self.member is not None
            # The organiser vouches for the agreement, so it's recorded as accepted now.
            modal = SignupModal(role, rules.SignupForm(), agreed_at=utcnow(), target=(self.member.id, self.member.name))
            await interaction.response.send_modal(modal)

        return add

    async def _edit(self, interaction: Interaction) -> None:
        signup = self.signup
        await interaction.response.send_modal(
            SignupModal(signup.role, signup.fields.to_form(), agreed_at=None, target=(signup.user_id, signup.username))
        )

    async def _switch(self, interaction: Interaction) -> None:
        from .. import events

        assert interaction.guild is not None
        signup = self.signup
        new_role = Role.CAPTAIN if signup.role is Role.PLAYER else Role.PLAYER
        saved, kind, details = await interaction.client.db.save_signup(
            signup.tournament_id, signup.user_id, signup.username, new_role, signup.fields, None, interaction.user.id
        )
        await self._show(interaction, self.member)
        await events.signup_changed(interaction.client, interaction.guild.id, saved, kind, details, interaction.user.id)

    async def _withdraw(self, interaction: Interaction) -> None:
        signup = self.signup
        await interaction.response.send_message(
            f"Withdraw the {signup.role.value} signup of **{signup.nickname}** (<@{signup.user_id}>)?",
            view=ConfirmWithdrawView(signup.id, admin=True),
            ephemeral=True,
        )


def manage_text() -> str:
    return (
        "**🔎 Manage a signup**\nPick a member to see, edit, switch or withdraw their signup, or to sign them up yourself."
        "\n-# For someone who left the server, use `/signup` with their nickname."
    )


# -------------------------------------------------------------------- handlers


async def _settings(interaction: Interaction) -> None:
    await interaction.response.send_modal(TournamentModal(await _config(interaction)))


async def _advanced(interaction: Interaction) -> None:
    await interaction.response.send_modal(AdvancedModal(await _config(interaction)))


async def _dates(interaction: Interaction) -> None:
    config = await _config(interaction)
    await _reply(interaction, dates_text(config), view=DatesView(config))


async def _divisions(interaction: Interaction) -> None:
    config = await _config(interaction)
    assert interaction.guild is not None
    tournament = await interaction.client.db.active_tournament(interaction.guild.id)
    names = await interaction.client.db.division_names(tournament.id, config.division_count)
    await interaction.response.send_modal(DivisionsModal(names))


async def _admins(interaction: Interaction) -> None:
    await _reply(interaction, organisers_text(), view=OrganisersView(await _config(interaction)))


async def _captains(interaction: Interaction) -> None:
    assert interaction.guild is not None
    db = interaction.client.db
    config = await _config(interaction)
    tournament = await db.active_tournament(interaction.guild.id)
    signups = await db.list_active_signups(tournament.id)
    names = await db.division_names(tournament.id, config.division_count)
    await interaction.response.send_message(
        embed=list_embed(config, signups, names), view=CaptainsListView(signups, names), ephemeral=True
    )


async def _signup(interaction: Interaction) -> None:
    await _reply(interaction, manage_text(), view=ManageSignupView())


def _exporter(export_type: ExportType) -> Callable[[Interaction], Awaitable[None]]:
    async def run(interaction: Interaction) -> None:
        await actions.export(interaction, export_type)

    return run


HANDLERS: dict[str, Callable[[Interaction], Awaitable[None]]] = {
    "settings": _settings,
    "dates": _dates,
    "divisions": _divisions,
    "admins": _admins,
    "advanced": _advanced,
    "open": open_signups,
    "close": close_signups,
    "reopen": reopen_signups,
    "captains": _captains,
    "signup": _signup,
    "export:csv": _exporter(ExportType.CSV),
    "export:players": _exporter(ExportType.PLAYERS),
    "export:tournament": _exporter(ExportType.TOURNAMENT),
    "reset": reset_tournament,
}
