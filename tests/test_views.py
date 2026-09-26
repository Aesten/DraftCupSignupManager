"""Smoke tests: embeds and views build, and stay within Discord's limits."""

from factories import T0, captain, make_signup

from draftcup.models import CaptainStatus, ExportRecord, ExportType, GuildConfig, Role, State, Tournament
from draftcup.notify import captain_status_line, signup_change_line
from draftcup.views import captain_card, captains_list, status_board

NAMES = ["Division 1", "Division 2"]


def _embed_size(embed) -> int:
    return len(embed.title or "") + len(embed.description or "") + sum(len(f.name) + len(f.value) for f in embed.fields)


async def test_status_board_embed():
    config = GuildConfig(guild_id=1, closes_at=T0)
    tournament = Tournament(id=1, guild_id=1, state=State.OPEN, revision=7, created_at=T0)
    signups = [make_signup(f"P{i}", player_class=("inf", "arc", "cav")[i % 3]) for i in range(40)]
    signups += [captain("C1", CaptainStatus.PICKED, 1), captain("C2", CaptainStatus.POOL), captain("C3")]
    exports = {
        ExportType.CSV: ExportRecord(1, ExportType.CSV, 7, T0, 5, None),
        ExportType.TOURNAMENT: ExportRecord(2, ExportType.TOURNAMENT, 4, T0, 5, None),
    }
    embed = status_board.build_embed(config, tournament, signups, NAMES, exports)
    text = "\n".join(f"{f.name}: {f.value}" for f in embed.fields)
    assert "Players: **40**" in text
    assert "1 pending · 1 picked · 1 to pool" in text
    assert "Pool: 41" in text
    assert "Division 1: 1/8" in text
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
