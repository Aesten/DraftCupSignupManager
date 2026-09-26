"""Smoke tests: embeds, views and forms build, and stay within Discord's limits."""

from datetime import date

from factories import T0, captain, make_signup

from draftcup.models import CaptainStatus, ExportRecord, ExportType, GuildConfig, Role, State, Tournament
from draftcup.notify import signup_change_line
from draftcup.views import captain_card, signup_post, tournament_panel
from draftcup.views.signup_flow import agreement_text

NAMES = ["Division 1", "Division 2"]


def _embed_size(embed) -> int:
    return len(embed.title or "") + len(embed.description or "") + sum(len(f.name) + len(f.value) for f in embed.fields)


async def test_tournament_message_embed():
    config = GuildConfig(guild_id=1, closes_at=T0)
    tournament = Tournament(id=1, guild_id=1, state=State.OPEN, revision=7, created_at=T0)
    signups = [make_signup(f"P{i}", player_class=("inf", "arc", "cav")[i % 3]) for i in range(40)]
    signups += [captain("C1", CaptainStatus.PICKED, 1), captain("C2"), captain("C3")]
    exports = {
        ExportType.CSV: ExportRecord(1, ExportType.CSV, 7, T0, 5, None),
        ExportType.TOURNAMENT: ExportRecord(2, ExportType.TOURNAMENT, 4, T0, 5, None),
    }
    embed = tournament_panel.build_embed(config, tournament, signups, NAMES, exports, ["signup channel #x: allow Send Messages"])
    text = "\n".join(f"{f.name}: {f.value}" for f in embed.fields)
    assert "Players: **40**" in text
    assert "Captain signups: **3** · 1 accepted · **2 waiting for a decision**" in text
    assert "Division 1 1/8" in text
    assert "CSV: ✅ up to date" in text
    assert "Tournament file: ⚠️ stale, 3 changes" in text
    assert "Player list: never exported" in text
    assert "allow Send Messages" in text
    assert _embed_size(embed) < 6000 and all(len(f.value) <= 1024 for f in embed.fields)


async def test_tournament_message_buttons_per_state():
    def labels(state):
        return [child.label for child in tournament_panel.TournamentView(state).children]

    assert labels(State.DRAFT) == ["Settings", "Open signups"]
    assert labels(State.OPEN) == ["Settings", "Change close date", "Close signups"]
    assert labels(State.CLOSED) == ["Settings", "Reopen signups"]
    ids = {child.custom_id.removeprefix("draftcup:dash:") for child in tournament_panel.TournamentView().children}
    assert ids == set(tournament_panel.HANDLERS)


async def test_forms_build():
    config = GuildConfig(guild_id=1, close_date=date(2026, 10, 18))
    for modal in (
        tournament_panel.SettingsModal(config),
        tournament_panel.CloseDateModal(config, "open"),
        tournament_panel.CloseDateModal(config, "change"),
    ):
        assert 1 <= len(modal.to_dict()["components"]) <= 5
    change = tournament_panel.CloseDateModal(config, "change")
    assert change.day.default == "18/10/2026"
    assert tournament_panel.CloseDateModal(config, "open").day.default is None


async def test_captain_card_pending_and_decided():
    pending = captain("Cap", player_class="cav", division="C")
    embed = captain_card.card_embed(pending, NAMES)
    assert "Waiting for a decision" in embed.description
    view = captain_card.card_view(pending)
    assert [(c.item.label, c.item.custom_id) for c in view.children] == [
        ("Accept", f"draftcup:cap:{pending.id}:accept"),
        ("Reject", f"draftcup:cap:{pending.id}:reject"),
    ]

    accepted = captain("Cap2", CaptainStatus.PICKED, 2)
    accepted.status_set_by = 9
    assert captain_card.card_view(accepted) is None
    assert "Accepted into **Division 2** by <@9>" in captain_card.card_embed(accepted, NAMES).description

    rejected = make_signup("Was captain", Role.PLAYER)
    rejected.status_set_by = 9
    assert captain_card.card_view(rejected) is None
    assert "signed up as a player" in captain_card.card_embed(rejected, NAMES).description


async def test_division_picker_and_edit_view():
    from draftcup.cogs.management import CaptainEditView

    options = [
        captain_card.discord.SelectOption(label=name, value=str(i), description="3/8 captains")
        for i, name in enumerate(NAMES, start=1)
    ]
    pending = captain("Cap")
    view = CaptainEditView(pending, options)
    assert view.to_player.label == "Reject: make them a player" and view.kind == "captain_rejected"
    accepted = captain("Cap2", CaptainStatus.PICKED, 1)
    view = CaptainEditView(accepted, options)
    assert view.to_player.label == "Revoke: make them a player" and view.kind == "captain_revoked"
    confirm = captain_card.ConfirmToPlayerView(accepted.id, "captain_revoked")
    assert confirm.confirm.label == "Revoke"


async def test_public_post_counts():
    config = GuildConfig(guild_id=1, title="Draft Cup #13", close_date=date(2099, 1, 4), closes_at=T0.replace(year=2099))
    open_t = Tournament(id=1, guild_id=1, state=State.OPEN, revision=0, created_at=T0)
    signups = [make_signup("A"), make_signup("B"), captain("C")]
    embed = signup_post.build_embed(config, open_t, signups)
    assert embed.title == "Draft Cup #13"
    assert "Signups are open" in embed.description and "**2** players · **1** captain candidate signed up" in embed.description
    closed = Tournament(id=1, guild_id=1, state=State.CLOSED, revision=0, created_at=T0)
    assert "closed" in signup_post.build_embed(config, closed, signups).description


def test_agreement_texts():
    assert agreement_text(Role.PLAYER) == "I have read the rules and can attend the **tournament**."
    captain_text = agreement_text(Role.CAPTAIN)
    assert "can attend the **auction** and the **tournament**" in captain_text and "play as a player" in captain_text


def test_signup_summary():
    from draftcup.views.signup_flow import signup_embed

    signup = make_signup("Aestens", player_class="cav", division="A")
    embed = signup_embed(signup, "Complete")
    assert embed.title == "Player Registration Complete"
    assert embed.description.splitlines() == [
        "**Nickname:** Aestens",
        "**Class:** Cavalry",
        "**Division:** A",
        "**IGL:** No",
        "**Steam:** https://steamcommunity.com/id/Aestens/",
    ]
    assert signup_embed(captain("Cap", division="")).title == "Captain Registration"
    assert "**Division:** None" in signup_embed(captain("Cap2", division="")).description


def test_change_lines():
    line = signup_change_line(5, 9, "edit", {"role": "captain", "nickname": "Bob", "changed": ["igl"], "captain_reset_from": "picked"})
    assert line == "✏️ <@5> edited their captain signup **Bob** (changed: IGL). Their captain acceptance was reset: accept or reject them again (by <@9>)"
    assert "left the server" in signup_change_line(5, None, "left_server", {"nickname": "Bob"})


def test_welcome_embed():
    from types import SimpleNamespace

    from draftcup.cogs.admin import welcome_embed

    embed = welcome_embed(SimpleNamespace(mention="<#1>"))
    assert "Anyone who can see it is an organiser" in embed.description and "<#1>" in embed.description


def test_invite_url():
    from draftcup.permissions import BOT_PERMISSIONS, invite_url

    assert BOT_PERMISSIONS.value == 117760
    assert not BOT_PERMISSIONS.manage_messages and not BOT_PERMISSIONS.administrator
    url = invite_url(123)
    assert url.startswith("https://discord.com/oauth2/authorize?client_id=123")
    assert "scope=bot+applications.commands" in url and "permissions=117760" in url
