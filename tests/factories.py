"""Builders for in-memory test data."""

from datetime import datetime, timezone
from itertools import count

from draftcup.models import CaptainStatus, Role, Signup

_ids = count(1)
T0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


def make_signup(
    nickname: str,
    role: Role = Role.PLAYER,
    player_class: str = "inf",
    division: str = "B",
    status: CaptainStatus | None = None,
    division_index: int | None = None,
    igl: bool = False,
) -> Signup:
    signup_id = next(_ids)
    if role is Role.CAPTAIN and status is None:
        status = CaptainStatus.PENDING
    return Signup(
        id=signup_id,
        tournament_id=1,
        user_id=1000 + signup_id,
        username=nickname.lower().replace(" ", ""),
        role=role,
        nickname=nickname,
        steam_url=f"https://steamcommunity.com/id/{nickname.replace(' ', '')}/",
        player_class=player_class,
        highest_division=division,
        igl=igl,
        agreed_at=T0,
        created_at=T0,
        updated_at=T0,
        captain_status=status,
        division_index=division_index,
        status_set_by=None,
        withdrawn_at=None,
        left_server=False,
        review_message_id=None,
    )


def captain(nickname: str, status: CaptainStatus = CaptainStatus.PENDING, division_index: int | None = None, **kwargs) -> Signup:
    return make_signup(nickname, Role.CAPTAIN, status=status, division_index=division_index, **kwargs)
