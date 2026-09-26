from datetime import timedelta

import pytest

from draftcup.db import Database, NicknameTaken, utcnow
from draftcup.models import CaptainStatus, Format, Role, State
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
