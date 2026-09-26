"""Data classes shared by the database layer and the Discord layer."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum

from . import rules


class Role(StrEnum):
    PLAYER = "player"
    CAPTAIN = "captain"

    @property
    def label(self) -> str:
        return "Player" if self is Role.PLAYER else "Captain"


class CaptainStatus(StrEnum):
    PENDING = "pending"
    PICKED = "picked"
    POOL = "pool"


class State(StrEnum):
    DRAFT = "draft"
    OPEN = "open"
    CLOSED = "closed"


class Format(StrEnum):
    CAPTAIN_PICK = "captainPick"
    RANDOM_PICK = "randomPick"

    @property
    def label(self) -> str:
        return "Captain Pick" if self is Format.CAPTAIN_PICK else "Random Pick"


@dataclass
class GuildConfig:
    guild_id: int
    title: str = "Draft Cup"
    format: Format = Format.CAPTAIN_PICK
    captains_per_division: int = 8
    team_size: int = 6
    division_count: int = 2
    half_budget_cap: bool = True
    timezone: str = "Europe/Paris"
    tournament_date: date | None = None
    auction_date: date | None = None
    # Signups close on close_date at close_time (server timezone); closes_at is that moment in UTC,
    # kept up to date by the database layer.
    close_date: date | None = None
    close_time: str = "23:59"
    closes_at: datetime | None = None
    rules_url: str | None = None
    signup_channel_id: int | None = None
    admin_channel_id: int | None = None
    signup_message_id: int | None = None
    status_message_id: int | None = None
    admin_role_ids: frozenset[int] = field(default_factory=frozenset)
    admin_user_ids: frozenset[int] = field(default_factory=frozenset)


@dataclass
class Tournament:
    id: int
    guild_id: int
    state: State
    revision: int
    created_at: datetime


@dataclass
class Signup:
    id: int
    tournament_id: int
    user_id: int
    username: str
    role: Role
    nickname: str
    steam_url: str
    player_class: str
    highest_division: str
    igl: bool
    agreed_at: datetime
    created_at: datetime
    updated_at: datetime
    captain_status: CaptainStatus | None
    division_index: int | None
    status_set_by: int | None
    withdrawn_at: datetime | None
    left_server: bool
    review_message_id: int | None

    @property
    def fields(self) -> rules.SignupFields:
        return rules.SignupFields(
            nickname=self.nickname,
            steam_url=self.steam_url,
            player_class=self.player_class,
            highest_division=self.highest_division,
            igl=self.igl,
        )

    @property
    def tier(self) -> int:
        return rules.tier_for(self.highest_division)

    @property
    def budget(self) -> float:
        return rules.budget_for(self.highest_division, self.player_class)


class ExportType(StrEnum):
    CSV = "csv"
    PLAYERS = "players"
    TOURNAMENT = "tournament"

    @property
    def label(self) -> str:
        return {"csv": "CSV", "players": "Player list", "tournament": "Tournament file"}[self.value]


@dataclass
class ExportRecord:
    id: int
    type: ExportType
    revision: int
    time: datetime
    actor_id: int | None
    stale_notified_at: datetime | None


@dataclass
class ChangeRecord:
    revision: int
    time: datetime
    actor_id: int | None
    signup_id: int | None
    kind: str
    details: dict
