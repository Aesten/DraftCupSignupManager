"""Smoke tests: embeds and views build, and stay within Discord's limits."""

from factories import T0, captain, make_signup

from draftcup.models import CaptainStatus, ExportRecord, ExportType, GuildConfig, Role, State, Tournament
from draftcup.notify import captain_status_line, signup_change_line
from draftcup.views import captain_card, captains_list, dashboard, signup_post

NAMES = ["Division 1", "Division 2"]


def _embed_size(embed) -> int:
    return len(embed.title or "") + len(embed.description or "") + sum(len(f.name) + len(f.value) for f in embed.fields)


async def test_dashboard_embed():
    config = GuildConfig(guild_id=1, closes_at=T0)
    tournament = Tournament(id=1, guild_id=1, state=State.OPEN, revision=7, created_at=T0)
    signups = [make_signup(f"P{i}", player_class=("inf", "arc", "cav")[i % 3]) for i in range(40)]
    signups += [captain("C1", CaptainStatus.PICKED, 1), captain("C2", CaptainStatus.POOL), captain("C3")]
    exports = {
        ExportType.CSV: ExportRecord(1, ExportType.CSV, 7, T0, 5, None),
        ExportType.TOURNAMENT: ExportRecord(2, ExportType.TOURNAMENT, 4, T0, 5, None),
    }
    embed = dashboard.build_embed(config, tournament, signups, NAMES, exports)
    text = "\n".join(f"{f.name}: {f.value}" for f in embed.fields)
    assert "Players: **40**" in text
    assert "1 pending · 1 picked · 1 to pool" in text
    assert "Pool: **41**" in text
    assert "Division 1 1/8" in text
    assert "Before opening signups, set the channels" in text
    assert "CSV: ✅ up to date" in text
    assert "Tournament file: ⚠️ stale, 3 changes" in text
    assert "Player list: never exported" in text
    assert _embed_size(embed) < 6000


async def test_captain_card():
    signup = captain("Cap", CaptainStatus.PICKED, 2, player_class="cav", division="C")
    embed = captain_card.card_embed(signup, NAMES)
    assert "Division 2" in embed.description
    view = captain_card.card_view(signup, NAMES)
    buttons = {child.item.custom_id: child.item for child in view.children}
    assert set(buttons) == {
        f"draftcup:cap:{signup.id}:pick:1", f"draftcup:cap:{signup.id}:pick:2",
        f"draftcup:cap:{signup.id}:pool", f"draftcup:cap:{signup.id}:reset",
    }
    assert buttons[f"draftcup:cap:{signup.id}:pick:2"].disabled

    retired = make_signup("Was captain", Role.PLAYER)
    assert captain_card.card_view(retired, NAMES) is None
    assert "switched to player" in captain_card.card_embed(retired, NAMES).description


async def test_captain_card_ten_divisions_fit():
    names = [f"Division {i}" for i in range(1, 11)]
    view = captain_card.card_view(captain("Cap"), names)
    assert len(view.children) == 12  # 10 divisions + Pool + Reset, spread over 3 rows


async def test_captains_list():
    config = GuildConfig(guild_id=1)
    signups = [captain(f"C{i}") for i in range(30)] + [captain("Picked", CaptainStatus.PICKED, 1)]
    embed = captains_list.list_embed(config, signups, NAMES)
    assert all(len(f.value) <= 1024 for f in embed.fields)
    view = captains_list.CaptainsListView(signups, NAMES)
    select = view.select
    assert len(select.options) == 25 and select.max_values == 25


def test_change_lines():
    line = signup_change_line(5, 9, "edit", {"role": "captain", "nickname": "Bob", "changed": ["igl"], "captain_reset_from": "picked"})
    assert line == "✏️ <@5> edited their captain signup **Bob** (changed: IGL). Captain status reset from **picked** to **pending** (by <@9>)"
    assert "left the server" in signup_change_line(5, None, "left_server", {"nickname": "Bob"})
    details = {"nickname": "Bob", "to": "picked", "to_division": 2}
    assert captain_status_line(details, 9, NAMES) == "🎖️ Captain **Bob** picked for **Division 2** by <@9>"


def test_invite_url():
    from draftcup.permissions import BOT_PERMISSIONS, invite_url

    assert BOT_PERMISSIONS.value == 2251799813803008
    assert not BOT_PERMISSIONS.manage_messages and not BOT_PERMISSIONS.administrator
    url = invite_url(123)
    assert url.startswith("https://discord.com/oauth2/authorize?client_id=123")
    assert "scope=bot+applications.commands" in url and "permissions=2251799813803008" in url


async def test_dashboard_view_buttons_per_state():
    def labels(state):
        return [child.label for child in dashboard.DashboardView(state).children]

    assert "Open signups" in labels(State.DRAFT) and "Close signups" not in labels(State.DRAFT)
    assert "Close signups" in labels(State.OPEN) and "Open signups" not in labels(State.OPEN)
    assert "Reopen signups" in labels(State.CLOSED)
    registered = dashboard.DashboardView()
    ids = {child.custom_id for child in registered.children}
    assert {"draftcup:dash:open", "draftcup:dash:close", "draftcup:dash:reopen"} <= ids
    from draftcup.views.dashboard_actions import HANDLERS

    assert {i.removeprefix("draftcup:dash:") for i in ids} == set(HANDLERS)


async def test_public_post_counts():
    from datetime import date

    config = GuildConfig(guild_id=1, close_date=date(2099, 1, 4), closes_at=T0.replace(year=2099))
    open_t = Tournament(id=1, guild_id=1, state=State.OPEN, revision=0, created_at=T0)
    signups = [make_signup("A"), make_signup("B"), captain("C")]
    embed = signup_post.build_embed(config, open_t, signups)
    assert "Signups are open" in embed.description and "**2** players · **1** captain candidate signed up" in embed.description
    closed = Tournament(id=1, guild_id=1, state=State.CLOSED, revision=0, created_at=T0)
    assert "closed" in signup_post.build_embed(config, closed, signups).description


async def test_forms_build():
    from datetime import date

    from draftcup.views import dashboard_actions as da

    config = GuildConfig(guild_id=1, division_count=2, auction_date=date(2026, 10, 24))
    for modal in (
        da.TournamentModal(config),
        da.AdvancedModal(config),
        da.DivisionsModal(["North", "South"]),
        da.DatePickerModal("Auction", config.auction_date, date(2026, 10, 1), None, "hint"),
    ):
        payload = modal.to_dict()
        assert 1 <= len(payload["components"]) <= 5
    picker = da.DatePickerModal("Auction", date(2026, 10, 24), date(2026, 10, 1), None)
    assert [o.label for o in picker.week.options if o.default] == ["Mon 19 Oct – Sun 25 Oct"]
    assert [o.value for o in picker.day.options if o.default] == ["5"]
    da.DatesView(config)
    da.OrganisersView(GuildConfig(guild_id=1, admin_role_ids=frozenset({5})))
    da.ManageSignupView()


async def test_dashboard_setup_field_stays_short():
    from datetime import date

    config = GuildConfig(
        guild_id=1, signup_channel_id=10**18, admin_channel_id=10**18, rules_url="https://example.com/" + "r" * 250,
        close_date=date(2026, 10, 18), admin_role_ids=frozenset(range(10**18, 10**18 + 25)),
        admin_user_ids=frozenset(range(10**18, 10**18 + 25)),
    )
    tournament = Tournament(id=1, guild_id=1, state=State.DRAFT, revision=0, created_at=T0)
    embed = dashboard.build_embed(config, tournament, [], ["A" * 40] * 5, {})
    assert all(len(f.value) <= 1024 for f in embed.fields)
