from datetime import timedelta

import pytest

from draftcup.db import ActionRefused, Database, NicknameTaken, utcnow
from draftcup.models import CaptainStatus, ExportType, Format, Role, State
from draftcup.rules import SignupFields

GUILD = 1


def fields(nickname="Bob", player_class="inf", division="B", igl=False):
    return SignupFields(nickname, f"https://steamcommunity.com/id/{nickname.replace(' ', '')}/", player_class, division, igl)


@pytest.fixture
async def db(tmp_path):
    database = await Database.open(str(tmp_path / "test.db"))
    yield database
    await database.close()


async def test_config_defaults_and_update(db):
    config = await db.get_config(GUILD)
    assert config.format is Format.CAPTAIN_PICK
    assert (config.captains_per_division, config.team_size, config.division_count) == (8, 6, 2)

    closes = utcnow() + timedelta(days=2)
    config = await db.update_config(GUILD, closes_at=closes, format=Format.RANDOM_PICK, half_budget_cap=False)
    assert config.closes_at == closes
    assert config.format is Format.RANDOM_PICK
    assert config.half_budget_cap is False

    await db.set_admin_role(GUILD, 10, True)
    await db.set_admin_user(GUILD, 20, True)
    config = await db.get_config(GUILD)
    assert config.admin_role_ids == {10} and config.admin_user_ids == {20}
    await db.set_admin_role(GUILD, 10, False)
    assert (await db.get_config(GUILD)).admin_role_ids == set()


async def test_update_config_rejects_unknown_key(db):
    with pytest.raises(ValueError):
        await db.update_config(GUILD, not_a_setting=5)


async def test_active_tournament_is_stable(db):
    first = await db.active_tournament(GUILD)
    assert first.state is State.DRAFT and first.revision == 0
    assert (await db.active_tournament(GUILD)).id == first.id
    assert (await db.active_tournament(GUILD + 1)).id != first.id


async def test_signup_create_edit_and_revision(db):
    t = await db.active_tournament(GUILD)
    signup, kind, _ = await db.save_signup(t.id, 100, "bob", Role.PLAYER, fields(), utcnow(), 100)
    assert kind == "signup"
    assert signup.captain_status is None
    assert signup.tier == 2

    _, kind, details = await db.save_signup(t.id, 100, "bob", Role.PLAYER, fields(player_class="arc"), None, 100)
    assert kind == "edit"
    assert details["changed"] == ["player_class"]

    _, kind, _ = await db.save_signup(t.id, 100, "bob", Role.PLAYER, fields(player_class="arc"), None, 100)
    assert kind == "unchanged"
    assert (await db.active_tournament(GUILD)).revision == 2


async def test_nickname_unique_case_insensitive(db):
    t = await db.active_tournament(GUILD)
    await db.save_signup(t.id, 100, "bob", Role.PLAYER, fields("Big Bob"), utcnow(), 100)
    with pytest.raises(NicknameTaken):
        await db.save_signup(t.id, 200, "other", Role.CAPTAIN, fields("big bob"), utcnow(), 200)
    # The owner can keep (or re-case) their own nickname.
    _, kind, _ = await db.save_signup(t.id, 100, "bob", Role.PLAYER, fields("BIG BOB"), None, 100)
    assert kind == "edit"


async def test_withdraw_frees_nickname(db):
    t = await db.active_tournament(GUILD)
    signup, _, _ = await db.save_signup(t.id, 100, "bob", Role.PLAYER, fields(), utcnow(), 100)
    withdrawn = await db.withdraw_signup(signup.id, 100)
    assert withdrawn is not None and withdrawn.withdrawn_at is not None
    assert await db.withdraw_signup(signup.id, 100) is None
    assert await db.get_active_signup(t.id, 100) is None

    other, kind, _ = await db.save_signup(t.id, 200, "other", Role.PLAYER, fields(), utcnow(), 200)
    assert kind == "signup" and other.nickname == "Bob"
    # The first user can sign up again, as a new signup.
    again, kind, _ = await db.save_signup(t.id, 100, "bob", Role.PLAYER, fields("Bob2"), utcnow(), 100)
    assert kind == "signup" and again.id != signup.id


async def test_role_switch_and_captain_reset(db, tmp_path):
    t = await db.active_tournament(GUILD)
    signup, _, _ = await db.save_signup(t.id, 100, "bob", Role.PLAYER, fields(), utcnow(), 100)

    signup, kind, details = await db.save_signup(t.id, 100, "bob", Role.CAPTAIN, fields(), utcnow(), 100)
    assert kind == "switch" and details["from_role"] == "player"
    assert signup.captain_status is CaptainStatus.PENDING
    assert signup.budget == 21.0

    # Simulate an admin picking the captain (the picking commands come later).
    await db._conn.execute(
        "UPDATE signup SET captain_status = 'picked', division_index = 1 WHERE id = ?", (signup.id,)
    )
    await db._conn.commit()

    # A no-op save keeps the pick...
    signup, kind, _ = await db.save_signup(t.id, 100, "bob", Role.CAPTAIN, fields(), None, 100)
    assert kind == "unchanged" and signup.captain_status is CaptainStatus.PICKED

    # ...a real edit sends the captain back to review.
    signup, kind, details = await db.save_signup(t.id, 100, "bob", Role.CAPTAIN, fields(division="A"), None, 100)
    assert kind == "edit" and details["captain_reset_from"] == "picked"
    assert signup.captain_status is CaptainStatus.PENDING and signup.division_index is None

    signup, kind, _ = await db.save_signup(t.id, 100, "bob", Role.PLAYER, fields(division="A"), utcnow(), 100)
    assert kind == "switch" and signup.captain_status is None


async def test_new_signup_requires_agreement(db):
    t = await db.active_tournament(GUILD)
    with pytest.raises(ValueError):
        await db.save_signup(t.id, 100, "bob", Role.PLAYER, fields(), None, 100)


async def test_open_tournaments_due(db):
    t = await db.active_tournament(GUILD)
    await db.update_config(GUILD, closes_at=utcnow() + timedelta(hours=1))
    await db.set_state(t.id, State.OPEN)
    assert await db.open_tournaments_due(utcnow()) == []
    due = await db.open_tournaments_due(utcnow() + timedelta(hours=2))
    assert [x.id for x in due] == [t.id]


async def test_left_server_flag(db):
    t = await db.active_tournament(GUILD)
    await db.save_signup(t.id, 100, "bob", Role.PLAYER, fields(), utcnow(), 100)
    flagged = await db.set_left_server(t.id, 100, True)
    assert flagged is not None and flagged.left_server
    assert await db.set_left_server(t.id, 100, True) is None
    assert await db.set_left_server(t.id, 999, True) is None


async def _captain(db, tournament_id, user_id, nickname):
    signup, _, _ = await db.save_signup(tournament_id, user_id, nickname.lower(), Role.CAPTAIN, fields(nickname), utcnow(), user_id)
    return signup


async def test_captain_status_capacity_and_log(db):
    t = await db.active_tournament(GUILD)
    a = await _captain(db, t.id, 1, "CapA")
    b = await _captain(db, t.id, 2, "CapB")
    player, _, _ = await db.save_signup(t.id, 3, "p", Role.PLAYER, fields("Plain"), utcnow(), 3)
    limits = {"division_count": 2, "capacity": 1}

    signup, details = await db.set_captain_status(a.id, CaptainStatus.PICKED, 1, 99, **limits)
    assert signup.captain_status is CaptainStatus.PICKED and signup.division_index == 1 and signup.status_set_by == 99
    assert details["from"] == "pending" and details["to_division"] == 1

    _, details = await db.set_captain_status(a.id, CaptainStatus.PICKED, 1, 99, **limits)
    assert details == {}  # unchanged: no log entry

    with pytest.raises(ActionRefused, match="already has 1"):
        await db.set_captain_status(b.id, CaptainStatus.PICKED, 1, 99, **limits)
    with pytest.raises(ActionRefused, match="valid division"):
        await db.set_captain_status(b.id, CaptainStatus.PICKED, 3, 99, **limits)
    with pytest.raises(ActionRefused, match="no longer a captain"):
        await db.set_captain_status(player.id, CaptainStatus.POOL, None, 99, **limits)

    signup, _ = await db.set_captain_status(b.id, CaptainStatus.POOL, 2, 99, **limits)
    assert signup.captain_status is CaptainStatus.POOL and signup.division_index is None
    assert await db.highest_used_division(t.id) == 1

    kinds = [c.kind for c in await db.changes_since(t.id, 0)]
    assert kinds == ["signup", "signup", "signup", "captain_status", "captain_status"]


async def test_divisions_and_rename(db):
    t = await db.active_tournament(GUILD)
    assert await db.division_names(t.id, 2) == ["Division 1", "Division 2"]
    await db.rename_division(t.id, 2, "Div B", 99)
    await db.rename_division(t.id, 2, "Division B", 99)
    assert await db.division_names(t.id, 3) == ["Division 1", "Division B", "Division 3"]
    assert (await db.active_tournament(GUILD)).revision == 2


async def test_export_tracking(db):
    t = await db.active_tournament(GUILD)
    await db.save_signup(t.id, 1, "a", Role.PLAYER, fields("A"), utcnow(), 1)
    t = await db.active_tournament(GUILD)
    await db.record_export(t.id, ExportType.CSV, t.revision, 99)
    latest = await db.latest_exports(t.id)
    assert latest[ExportType.CSV].revision == 1 and latest[ExportType.CSV].stale_notified_at is None
    assert await db.changes_since(t.id, 1) == []

    await db.save_signup(t.id, 2, "b", Role.PLAYER, fields("B"), utcnow(), 2)
    await db.record_config_change(t.id, 99, "team_size", "7")
    changes = await db.changes_since(t.id, 1)
    assert [c.kind for c in changes] == ["signup", "config"]

    await db.mark_stale_notified(latest[ExportType.CSV].id)
    assert (await db.latest_exports(t.id))[ExportType.CSV].stale_notified_at is not None
    await db.record_export(t.id, ExportType.CSV, 3, 99)
    assert (await db.latest_exports(t.id))[ExportType.CSV].stale_notified_at is None


async def test_archive_starts_a_fresh_tournament(db):
    t = await db.active_tournament(GUILD)
    await db.rename_division(t.id, 1, "Premier", 99)
    signup, _, _ = await db.save_signup(t.id, 1, "a", Role.PLAYER, fields("A"), utcnow(), 1)
    new = await db.archive_tournament(t.id)
    assert new.id != t.id and new.state is State.DRAFT and new.revision == 0
    assert (await db.active_tournament(GUILD)).id == new.id
    assert await db.list_active_signups(new.id) == []
    assert await db.division_names(new.id, 1) == ["Premier"]
    # The same nickname is free again in the new tournament.
    _, kind, _ = await db.save_signup(new.id, 1, "a", Role.PLAYER, fields("A"), utcnow(), 1)
    assert kind == "signup"


async def test_servers_are_isolated(db):
    """A test server and the real server share the bot and the database, never their data."""
    main, test = 111, 222
    t_main = await db.active_tournament(main)
    t_test = await db.active_tournament(test)
    assert t_main.id != t_test.id

    await db.update_config(test, title="Test cup", division_count=1)
    assert (await db.get_config(main)).title == "Draft Cup"

    # Same user and same nickname in both servers.
    await db.save_signup(t_main.id, 5, "bob", Role.PLAYER, fields("Bob"), utcnow(), 5)
    await db.save_signup(t_test.id, 5, "bob", Role.CAPTAIN, fields("Bob"), utcnow(), 5)
    assert (await db.get_active_signup(t_main.id, 5)).role is Role.PLAYER
    assert (await db.get_active_signup(t_test.id, 5)).role is Role.CAPTAIN
    assert [s.nickname for s in await db.find_signups(t_main.id, "bo")] == ["Bob"]

    await db.set_admin_role(test, 7, True)
    assert (await db.get_config(main)).admin_role_ids == set()

    await db.archive_tournament(t_test.id)
    assert (await db.active_tournament(main)).id == t_main.id


async def test_find_signups(db):
    t = await db.active_tournament(GUILD)
    await db.save_signup(t.id, 1, "zed_discord", Role.PLAYER, fields("Alpha"), utcnow(), 1)
    await db.save_signup(t.id, 2, "other", Role.PLAYER, fields("Beta"), utcnow(), 2)
    assert [s.nickname for s in await db.find_signups(t.id, "ALP")] == ["Alpha"]
    assert [s.nickname for s in await db.find_signups(t.id, "zed")] == ["Alpha"]
    assert len(await db.find_signups(t.id, "")) == 2


async def test_migrates_v1_database(tmp_path):
    import aiosqlite

    from draftcup import db as db_module

    path = str(tmp_path / "v1.db")
    async with aiosqlite.connect(path) as conn:
        await conn.executescript(db_module._MIGRATIONS[0])
        await conn.execute("PRAGMA user_version = 1")
        await conn.commit()
    database = await Database.open(path)
    try:
        t = await database.active_tournament(GUILD)
        await database.record_export(t.id, ExportType.CSV, 0, 1)
        assert ExportType.CSV in await database.latest_exports(t.id)
    finally:
        await database.close()
