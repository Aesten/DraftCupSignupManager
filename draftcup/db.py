"""SQLite persistence (spec §10)."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import fields as dataclass_fields
from datetime import date, datetime, timezone
from typing import Any

import aiosqlite

from . import rules, timeutil
from .models import (
    CaptainStatus, ChangeRecord, ExportRecord, ExportType, Format, GuildConfig, Role, Signup, State, Tournament,
)

_SCHEMA_V1 = """
CREATE TABLE guild_config (
    guild_id              INTEGER PRIMARY KEY,
    title                 TEXT    NOT NULL DEFAULT 'Draft Cup',
    format                TEXT    NOT NULL DEFAULT 'captainPick',
    captains_per_division INTEGER NOT NULL DEFAULT 8,
    team_size             INTEGER NOT NULL DEFAULT 6,
    division_count        INTEGER NOT NULL DEFAULT 2,
    half_budget_cap       INTEGER NOT NULL DEFAULT 1,
    timezone              TEXT    NOT NULL DEFAULT 'UTC',
    tournament_date       TEXT,
    auction_date          TEXT,
    closes_at             TEXT,
    rules_url             TEXT,
    signup_channel_id     INTEGER,
    admin_channel_id      INTEGER,
    signup_message_id     INTEGER,
    status_message_id     INTEGER
);

CREATE TABLE admin_role (
    guild_id INTEGER NOT NULL,
    role_id  INTEGER NOT NULL,
    PRIMARY KEY (guild_id, role_id)
);

CREATE TABLE admin_user (
    guild_id INTEGER NOT NULL,
    user_id  INTEGER NOT NULL,
    PRIMARY KEY (guild_id, user_id)
);

CREATE TABLE tournament (
    id          INTEGER PRIMARY KEY,
    guild_id    INTEGER NOT NULL,
    state       TEXT    NOT NULL DEFAULT 'draft',
    revision    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT    NOT NULL,
    archived_at TEXT
);
CREATE UNIQUE INDEX tournament_active ON tournament (guild_id) WHERE archived_at IS NULL;

CREATE TABLE division (
    tournament_id INTEGER NOT NULL REFERENCES tournament (id),
    idx           INTEGER NOT NULL,
    name          TEXT    NOT NULL,
    PRIMARY KEY (tournament_id, idx)
);

CREATE TABLE signup (
    id                INTEGER PRIMARY KEY,
    tournament_id     INTEGER NOT NULL REFERENCES tournament (id),
    user_id           INTEGER NOT NULL,
    username          TEXT    NOT NULL,
    role              TEXT    NOT NULL,
    nickname          TEXT    NOT NULL,
    steam_url         TEXT    NOT NULL,
    player_class      TEXT    NOT NULL,
    highest_division  TEXT    NOT NULL DEFAULT '',
    igl               INTEGER NOT NULL,
    agreed_at         TEXT    NOT NULL,
    created_at        TEXT    NOT NULL,
    updated_at        TEXT    NOT NULL,
    captain_status    TEXT,
    division_index    INTEGER,
    status_set_by     INTEGER,
    withdrawn_at      TEXT,
    left_server       INTEGER NOT NULL DEFAULT 0,
    review_message_id INTEGER
);
CREATE UNIQUE INDEX signup_active_user ON signup (tournament_id, user_id) WHERE withdrawn_at IS NULL;
CREATE UNIQUE INDEX signup_active_nickname ON signup (tournament_id, lower(nickname)) WHERE withdrawn_at IS NULL;

CREATE TABLE change_log (
    id            INTEGER PRIMARY KEY,
    tournament_id INTEGER NOT NULL REFERENCES tournament (id),
    revision      INTEGER NOT NULL,
    time          TEXT    NOT NULL,
    actor_id      INTEGER,
    signup_id     INTEGER,
    kind          TEXT    NOT NULL,
    details       TEXT    NOT NULL DEFAULT '{}'
);

CREATE TABLE export_log (
    id            INTEGER PRIMARY KEY,
    tournament_id INTEGER NOT NULL REFERENCES tournament (id),
    type          TEXT    NOT NULL,
    revision      INTEGER NOT NULL,
    time          TEXT    NOT NULL,
    actor_id      INTEGER
);
"""

# Each entry upgrades the schema by one version; never edit an entry once released.
_MIGRATIONS = (
    _SCHEMA_V1,
    # v2: export tracker remembers when it warned that an export went stale (spec §9.4).
    "ALTER TABLE export_log ADD COLUMN stale_notified_at TEXT;",
    # v3: dates are picked as days; signups close at close_time on close_date (server timezone).
    """
    ALTER TABLE guild_config ADD COLUMN close_date TEXT;
    ALTER TABLE guild_config ADD COLUMN close_time TEXT NOT NULL DEFAULT '23:59';
    UPDATE guild_config SET
        tournament_date = substr(tournament_date, 1, 10),
        auction_date = substr(auction_date, 1, 10),
        close_date = substr(closes_at, 1, 10);
    """,
)
SCHEMA_VERSION = len(_MIGRATIONS)

# Columns of guild_config that map 1:1 to GuildConfig attributes.
_CONFIG_COLUMNS = (
    "title", "format", "captains_per_division", "team_size", "division_count", "half_budget_cap",
    "timezone", "tournament_date", "auction_date", "close_date", "close_time", "closes_at", "rules_url",
    "signup_channel_id", "admin_channel_id", "signup_message_id", "status_message_id",
)
_CONFIG_DATETIMES = {"closes_at"}
_CONFIG_DATES = {"tournament_date", "auction_date", "close_date"}
# closes_at is derived from these; it is recomputed whenever one of them changes.
_CLOSE_KEYS = {"close_date", "close_time", "timezone"}


class NicknameTaken(Exception):
    def __init__(self, nickname: str) -> None:
        super().__init__(f"The nickname **{nickname}** is already taken.")
        self.nickname = nickname


class ActionRefused(Exception):
    """A change that can't be applied in the current state; the message is shown to the admin."""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _dt_to_db(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).isoformat() if value else None


def _dt_from_db(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


class Database:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn
        # One shared connection means one shared transaction: every write goes through this lock so a
        # commit never includes another coroutine's half-done changes. It also makes read-then-write
        # sequences (uniqueness checks, revision bumps) atomic.
        self._lock = asyncio.Lock()

    @classmethod
    async def open(cls, path: str) -> Database:
        conn = await aiosqlite.connect(path)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA foreign_keys = ON")
        await conn.execute("PRAGMA journal_mode = WAL")
        db = cls(conn)
        await db._migrate()
        return db

    async def close(self) -> None:
        await self._conn.close()

    async def _migrate(self) -> None:
        async with self._conn.execute("PRAGMA user_version") as cur:
            (version,) = await cur.fetchone()
        if version > SCHEMA_VERSION:
            raise RuntimeError(f"Database schema v{version} is newer than this bot (v{SCHEMA_VERSION}).")
        for target in range(version + 1, SCHEMA_VERSION + 1):
            await self._conn.executescript(_MIGRATIONS[target - 1])
            await self._conn.execute(f"PRAGMA user_version = {target}")
            await self._conn.commit()

    async def _fetchone(self, sql: str, params: tuple[Any, ...] = ()) -> aiosqlite.Row | None:
        async with self._conn.execute(sql, params) as cur:
            return await cur.fetchone()

    async def _fetchall(self, sql: str, params: tuple[Any, ...] = ()) -> list[aiosqlite.Row]:
        async with self._conn.execute(sql, params) as cur:
            return list(await cur.fetchall())

    # ----------------------------------------------------------------- config

    async def get_config(self, guild_id: int) -> GuildConfig:
        row = await self._fetchone("SELECT * FROM guild_config WHERE guild_id = ?", (guild_id,))
        if row is None:
            async with self._lock:
                await self._conn.execute(
                    "INSERT OR IGNORE INTO guild_config (guild_id, timezone) VALUES (?, ?)",
                    (guild_id, timeutil.DEFAULT_TIMEZONE),
                )
                await self._conn.commit()
            row = await self._fetchone("SELECT * FROM guild_config WHERE guild_id = ?", (guild_id,))
            assert row is not None
        values: dict[str, Any] = {}
        for column in _CONFIG_COLUMNS:
            value = row[column]
            if column in _CONFIG_DATETIMES:
                value = _dt_from_db(value)
            elif column in _CONFIG_DATES:
                value = date.fromisoformat(value) if value else None
            values[column] = value
        values["format"] = Format(values["format"])
        values["half_budget_cap"] = bool(values["half_budget_cap"])
        roles = await self._fetchall("SELECT role_id FROM admin_role WHERE guild_id = ?", (guild_id,))
        users = await self._fetchall("SELECT user_id FROM admin_user WHERE guild_id = ?", (guild_id,))
        return GuildConfig(
            guild_id=guild_id,
            admin_role_ids=frozenset(r["role_id"] for r in roles),
            admin_user_ids=frozenset(r["user_id"] for r in users),
            **values,
        )

    async def update_config(self, guild_id: int, **changes: Any) -> GuildConfig:
        unknown = set(changes) - set(_CONFIG_COLUMNS) | ({"closes_at"} & set(changes))
        if unknown:
            raise ValueError(f"Unknown or derived config keys: {', '.join(sorted(unknown))}")
        current = await self.get_config(guild_id)  # also ensures the row exists
        if changes.keys() & _CLOSE_KEYS:
            close_date = changes.get("close_date", current.close_date)
            close_time = changes.get("close_time", current.close_time)
            tz_name = changes.get("timezone", current.timezone)
            changes["closes_at"] = timeutil.closing_moment(close_date, close_time, tz_name) if close_date else None
        if changes:
            values = []
            for key, value in changes.items():
                if key in _CONFIG_DATETIMES:
                    value = _dt_to_db(value)
                elif key in _CONFIG_DATES:
                    value = value.isoformat() if value else None
                elif isinstance(value, bool):
                    value = int(value)
                values.append(value)
            assignments = ", ".join(f"{key} = ?" for key in changes)
            async with self._lock:
                await self._conn.execute(
                    f"UPDATE guild_config SET {assignments} WHERE guild_id = ?", (*values, guild_id)
                )
                await self._conn.commit()
        return await self.get_config(guild_id)

    async def set_admin_role(self, guild_id: int, role_id: int, enabled: bool) -> None:
        if enabled:
            sql = "INSERT OR IGNORE INTO admin_role (guild_id, role_id) VALUES (?, ?)"
        else:
            sql = "DELETE FROM admin_role WHERE guild_id = ? AND role_id = ?"
        async with self._lock:
            await self._conn.execute(sql, (guild_id, role_id))
            await self._conn.commit()

    async def set_admin_user(self, guild_id: int, user_id: int, enabled: bool) -> None:
        if enabled:
            sql = "INSERT OR IGNORE INTO admin_user (guild_id, user_id) VALUES (?, ?)"
        else:
            sql = "DELETE FROM admin_user WHERE guild_id = ? AND user_id = ?"
        async with self._lock:
            await self._conn.execute(sql, (guild_id, user_id))
            await self._conn.commit()

    async def replace_admins(self, guild_id: int, *, role_ids: set[int] | None = None, user_ids: set[int] | None = None) -> None:
        """Replaces the organiser roles and/or users with exactly these (None leaves that list as is)."""
        async with self._lock:
            if role_ids is not None:
                await self._conn.execute("DELETE FROM admin_role WHERE guild_id = ?", (guild_id,))
                await self._conn.executemany(
                    "INSERT INTO admin_role (guild_id, role_id) VALUES (?, ?)", [(guild_id, r) for r in role_ids]
                )
            if user_ids is not None:
                await self._conn.execute("DELETE FROM admin_user WHERE guild_id = ?", (guild_id,))
                await self._conn.executemany(
                    "INSERT INTO admin_user (guild_id, user_id) VALUES (?, ?)", [(guild_id, u) for u in user_ids]
                )
            await self._conn.commit()

    # ------------------------------------------------------------- tournament

    @staticmethod
    def _tournament(row: aiosqlite.Row) -> Tournament:
        return Tournament(
            id=row["id"],
            guild_id=row["guild_id"],
            state=State(row["state"]),
            revision=row["revision"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    async def active_tournament(self, guild_id: int) -> Tournament:
        """Returns the server's current tournament, creating an empty one if needed."""
        async with self._lock:
            row = await self._fetchone(
                "SELECT * FROM tournament WHERE guild_id = ? AND archived_at IS NULL", (guild_id,)
            )
            if row is None:
                await self._conn.execute(
                    "INSERT INTO tournament (guild_id, created_at) VALUES (?, ?)", (guild_id, _dt_to_db(utcnow()))
                )
                await self._conn.commit()
                row = await self._fetchone(
                    "SELECT * FROM tournament WHERE guild_id = ? AND archived_at IS NULL", (guild_id,)
                )
            assert row is not None
            return self._tournament(row)

    async def find_active_tournament(self, guild_id: int) -> Tournament | None:
        """Like active_tournament, but never creates one (for events from servers that may not use the bot)."""
        row = await self._fetchone("SELECT * FROM tournament WHERE guild_id = ? AND archived_at IS NULL", (guild_id,))
        return self._tournament(row) if row else None

    async def set_state(self, tournament_id: int, state: State) -> None:
        async with self._lock:
            await self._conn.execute("UPDATE tournament SET state = ? WHERE id = ?", (state.value, tournament_id))
            await self._conn.commit()

    async def open_tournaments_due(self, now: datetime) -> list[Tournament]:
        """Open tournaments whose server's closes_at has passed (spec §8, scheduled close)."""
        rows = await self._fetchall(
            """
            SELECT t.* FROM tournament t JOIN guild_config c ON c.guild_id = t.guild_id
            WHERE t.archived_at IS NULL AND t.state = 'open'
              AND c.closes_at IS NOT NULL AND c.closes_at <= ?
            """,
            (_dt_to_db(now),),
        )
        return [self._tournament(row) for row in rows]

    async def _record_change(
        self, tournament_id: int, actor_id: int | None, signup_id: int | None, kind: str, details: dict[str, Any]
    ) -> int:
        """Bumps the tournament revision and logs the change. The caller commits."""
        await self._conn.execute("UPDATE tournament SET revision = revision + 1 WHERE id = ?", (tournament_id,))
        row = await self._fetchone("SELECT revision FROM tournament WHERE id = ?", (tournament_id,))
        assert row is not None
        await self._conn.execute(
            "INSERT INTO change_log (tournament_id, revision, time, actor_id, signup_id, kind, details)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (tournament_id, row["revision"], _dt_to_db(utcnow()), actor_id, signup_id, kind, json.dumps(details)),
        )
        return row["revision"]

    # ----------------------------------------------------------------- signup

    @staticmethod
    def _signup(row: aiosqlite.Row) -> Signup:
        return Signup(
            id=row["id"],
            tournament_id=row["tournament_id"],
            user_id=row["user_id"],
            username=row["username"],
            role=Role(row["role"]),
            nickname=row["nickname"],
            steam_url=row["steam_url"],
            player_class=row["player_class"],
            highest_division=row["highest_division"],
            igl=bool(row["igl"]),
            agreed_at=datetime.fromisoformat(row["agreed_at"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            captain_status=CaptainStatus(row["captain_status"]) if row["captain_status"] else None,
            division_index=row["division_index"],
            status_set_by=row["status_set_by"],
            withdrawn_at=_dt_from_db(row["withdrawn_at"]),
            left_server=bool(row["left_server"]),
            review_message_id=row["review_message_id"],
        )

    async def get_signup(self, signup_id: int) -> Signup | None:
        row = await self._fetchone("SELECT * FROM signup WHERE id = ?", (signup_id,))
        return self._signup(row) if row else None

    async def get_active_signup(self, tournament_id: int, user_id: int) -> Signup | None:
        row = await self._fetchone(
            "SELECT * FROM signup WHERE tournament_id = ? AND user_id = ? AND withdrawn_at IS NULL",
            (tournament_id, user_id),
        )
        return self._signup(row) if row else None

    async def list_active_signups(self, tournament_id: int) -> list[Signup]:
        rows = await self._fetchall(
            "SELECT * FROM signup WHERE tournament_id = ? AND withdrawn_at IS NULL ORDER BY created_at",
            (tournament_id,),
        )
        return [self._signup(row) for row in rows]

    async def _nickname_taken(self, tournament_id: int, nickname: str, exclude_id: int | None) -> bool:
        row = await self._fetchone(
            "SELECT id FROM signup WHERE tournament_id = ? AND lower(nickname) = lower(?)"
            " AND withdrawn_at IS NULL AND id IS NOT ?",
            (tournament_id, nickname, exclude_id),
        )
        return row is not None

    async def save_signup(
        self,
        tournament_id: int,
        user_id: int,
        username: str,
        role: Role,
        signup_fields: rules.SignupFields,
        agreed_at: datetime | None,
        actor_id: int,
    ) -> tuple[Signup, str, dict[str, Any]]:
        """Creates or updates the user's active signup.

        `agreed_at` is the time the user accepted the agreement step; None keeps the stored one (plain edit).
        Returns the saved signup, the change kind ("signup", "edit", "switch" or "unchanged") and the
        details written to the change log. Raises NicknameTaken.
        """
        async with self._lock:
            existing = await self.get_active_signup(tournament_id, user_id)
            exclude_id = existing.id if existing else None
            if await self._nickname_taken(tournament_id, signup_fields.nickname, exclude_id):
                raise NicknameTaken(signup_fields.nickname)

            now = utcnow()
            values = {
                "username": username,
                "role": role.value,
                "nickname": signup_fields.nickname,
                "steam_url": signup_fields.steam_url,
                "player_class": signup_fields.player_class,
                "highest_division": signup_fields.highest_division,
                "igl": int(signup_fields.igl),
            }

            if existing is None:
                if agreed_at is None:
                    raise ValueError("A new signup needs an agreement time.")
                values |= {
                    "tournament_id": tournament_id,
                    "user_id": user_id,
                    "agreed_at": _dt_to_db(agreed_at),
                    "created_at": _dt_to_db(now),
                    "updated_at": _dt_to_db(now),
                    "captain_status": CaptainStatus.PENDING.value if role is Role.CAPTAIN else None,
                }
                columns = ", ".join(values)
                placeholders = ", ".join("?" for _ in values)
                try:
                    cur = await self._conn.execute(
                        f"INSERT INTO signup ({columns}) VALUES ({placeholders})", tuple(values.values())
                    )
                except sqlite3.IntegrityError as exc:
                    raise NicknameTaken(signup_fields.nickname) from exc
                signup_id = cur.lastrowid
                kind = "signup"
                details: dict[str, Any] = {"role": role.value, "nickname": signup_fields.nickname}
            else:
                signup_id = existing.id
                role_changed = existing.role is not role
                data_changed = existing.fields != signup_fields
                if not role_changed and not data_changed and agreed_at is None:
                    return existing, "unchanged", {}
                if agreed_at is not None:
                    values["agreed_at"] = _dt_to_db(agreed_at)
                values["updated_at"] = _dt_to_db(now)
                # Editing or switching a captain candidate sends them back to review (spec §5.2).
                if role is Role.CAPTAIN and (role_changed or data_changed):
                    values |= {"captain_status": CaptainStatus.PENDING.value, "division_index": None, "status_set_by": None}
                elif role is Role.PLAYER:
                    values |= {"captain_status": None, "division_index": None, "status_set_by": None}
                assignments = ", ".join(f"{key} = ?" for key in values)
                try:
                    await self._conn.execute(
                        f"UPDATE signup SET {assignments} WHERE id = ?", (*values.values(), signup_id)
                    )
                except sqlite3.IntegrityError as exc:
                    raise NicknameTaken(signup_fields.nickname) from exc
                kind = "switch" if role_changed else "edit"
                details = {
                    "role": role.value,
                    "nickname": signup_fields.nickname,
                    "changed": _changed_fields(existing.fields, signup_fields),
                }
                if role_changed:
                    details["from_role"] = existing.role.value
                if existing.captain_status in (CaptainStatus.PICKED, CaptainStatus.POOL) and (role_changed or data_changed):
                    details["captain_reset_from"] = existing.captain_status.value

            await self._record_change(tournament_id, actor_id, signup_id, kind, details)
            await self._conn.commit()
            saved = await self.get_signup(signup_id)
            assert saved is not None
            return saved, kind, details

    async def withdraw_signup(self, signup_id: int, actor_id: int) -> Signup | None:
        """Soft-deletes an active signup. Returns None if it was already withdrawn."""
        async with self._lock:
            signup = await self.get_signup(signup_id)
            if signup is None or signup.withdrawn_at is not None:
                return None
            now = utcnow()
            await self._conn.execute(
                "UPDATE signup SET withdrawn_at = ?, updated_at = ? WHERE id = ?",
                (_dt_to_db(now), _dt_to_db(now), signup_id),
            )
            await self._record_change(
                signup.tournament_id, actor_id, signup_id, "withdraw",
                {"role": signup.role.value, "nickname": signup.nickname},
            )
            await self._conn.commit()
            return await self.get_signup(signup_id)

    async def set_left_server(self, tournament_id: int, user_id: int, left: bool) -> Signup | None:
        """Flags the user's active signup when they leave (or rejoin) the server."""
        async with self._lock:
            signup = await self.get_active_signup(tournament_id, user_id)
            if signup is None or signup.left_server == left:
                return None
            await self._conn.execute("UPDATE signup SET left_server = ? WHERE id = ?", (int(left), signup.id))
            await self._record_change(
                tournament_id, None, signup.id, "left_server" if left else "rejoined_server",
                {"nickname": signup.nickname},
            )
            await self._conn.commit()
            return await self.get_signup(signup.id)

    async def find_signups(self, tournament_id: int, text: str, limit: int = 25) -> list[Signup]:
        """Active signups whose nickname or Discord username contains `text` (for autocomplete)."""
        pattern = f"%{text.strip().lower()}%"
        rows = await self._fetchall(
            "SELECT * FROM signup WHERE tournament_id = ? AND withdrawn_at IS NULL"
            " AND (lower(nickname) LIKE ? OR lower(username) LIKE ?) ORDER BY lower(nickname) LIMIT ?",
            (tournament_id, pattern, pattern, limit),
        )
        return [self._signup(row) for row in rows]

    async def set_review_message(self, signup_id: int, message_id: int | None) -> None:
        """Remembers the captain review card of a signup. Not a data change: no revision bump."""
        async with self._lock:
            await self._conn.execute("UPDATE signup SET review_message_id = ? WHERE id = ?", (message_id, signup_id))
            await self._conn.commit()

    # -------------------------------------------------------- captain picking

    async def set_captain_status(
        self,
        signup_id: int,
        status: CaptainStatus,
        division_index: int | None,
        actor_id: int,
        *,
        division_count: int,
        capacity: int,
    ) -> tuple[Signup, dict[str, Any]]:
        """Sets a captain candidate's status (spec §6.2). Raises ActionRefused.

        Returns the signup and the change-log details; details are empty when nothing changed.
        """
        if status is CaptainStatus.PICKED:
            if division_index is None or not 1 <= division_index <= division_count:
                raise ActionRefused("Pick a valid division.")
        else:
            division_index = None
        async with self._lock:
            signup = await self.get_signup(signup_id)
            if signup is None or signup.withdrawn_at is not None:
                raise ActionRefused("This signup was withdrawn.")
            if signup.role is not Role.CAPTAIN:
                raise ActionRefused(f"**{signup.nickname}** is no longer a captain candidate.")
            tournament = await self._fetchone("SELECT archived_at FROM tournament WHERE id = ?", (signup.tournament_id,))
            if tournament is None or tournament["archived_at"] is not None:
                raise ActionRefused("This signup belongs to an archived tournament.")
            if signup.captain_status is status and signup.division_index == division_index:
                return signup, {}
            if status is CaptainStatus.PICKED:
                row = await self._fetchone(
                    "SELECT count(*) AS n FROM signup WHERE tournament_id = ? AND withdrawn_at IS NULL"
                    " AND captain_status = 'picked' AND division_index = ? AND id != ?",
                    (signup.tournament_id, division_index, signup_id),
                )
                assert row is not None
                if row["n"] >= capacity:
                    raise ActionRefused(f"That division already has {capacity} captains.")
            await self._conn.execute(
                "UPDATE signup SET captain_status = ?, division_index = ?, status_set_by = ? WHERE id = ?",
                (status.value, division_index, actor_id, signup_id),
            )
            details = {
                "nickname": signup.nickname,
                "from": signup.captain_status.value if signup.captain_status else None,
                "from_division": signup.division_index,
                "to": status.value,
                "to_division": division_index,
            }
            await self._record_change(signup.tournament_id, actor_id, signup_id, "captain_status", details)
            await self._conn.commit()
            updated = await self.get_signup(signup_id)
            assert updated is not None
            return updated, details

    async def highest_used_division(self, tournament_id: int) -> int:
        row = await self._fetchone(
            "SELECT max(division_index) AS m FROM signup WHERE tournament_id = ? AND withdrawn_at IS NULL"
            " AND captain_status = 'picked'",
            (tournament_id,),
        )
        return (row["m"] if row else None) or 0

    # -------------------------------------------------------------- divisions

    async def division_names(self, tournament_id: int, count: int) -> list[str]:
        """Names of divisions 1…count; unnamed ones are called "Division N"."""
        rows = await self._fetchall("SELECT idx, name FROM division WHERE tournament_id = ?", (tournament_id,))
        names = {row["idx"]: row["name"] for row in rows}
        return [names.get(i, f"Division {i}") for i in range(1, count + 1)]

    async def rename_division(self, tournament_id: int, index: int, name: str, actor_id: int) -> None:
        async with self._lock:
            await self._conn.execute(
                "INSERT INTO division (tournament_id, idx, name) VALUES (?, ?, ?)"
                " ON CONFLICT (tournament_id, idx) DO UPDATE SET name = excluded.name",
                (tournament_id, index, name),
            )
            await self._record_change(tournament_id, actor_id, None, "division_rename", {"index": index, "name": name})
            await self._conn.commit()

    # ------------------------------------------------------ config & lifecycle

    async def record_config_change(self, tournament_id: int, actor_id: int, key: str, value: str) -> None:
        """Config changes that alter export content count as changes for the export tracker."""
        async with self._lock:
            await self._record_change(tournament_id, actor_id, None, "config", {"key": key, "value": value})
            await self._conn.commit()

    async def archive_tournament(self, tournament_id: int) -> Tournament:
        """Archives the tournament and returns the new, empty one (division names are carried over)."""
        async with self._lock:
            row = await self._fetchone("SELECT guild_id FROM tournament WHERE id = ?", (tournament_id,))
            assert row is not None
            now = _dt_to_db(utcnow())
            await self._conn.execute("UPDATE tournament SET archived_at = ? WHERE id = ?", (now, tournament_id))
            cur = await self._conn.execute(
                "INSERT INTO tournament (guild_id, created_at) VALUES (?, ?)", (row["guild_id"], now)
            )
            await self._conn.execute(
                "INSERT INTO division (tournament_id, idx, name) SELECT ?, idx, name FROM division WHERE tournament_id = ?",
                (cur.lastrowid, tournament_id),
            )
            await self._conn.commit()
            new = await self._fetchone("SELECT * FROM tournament WHERE id = ?", (cur.lastrowid,))
            assert new is not None
            return self._tournament(new)

    # ---------------------------------------------------------------- exports

    @staticmethod
    def _export(row: aiosqlite.Row) -> ExportRecord:
        return ExportRecord(
            id=row["id"],
            type=ExportType(row["type"]),
            revision=row["revision"],
            time=datetime.fromisoformat(row["time"]),
            actor_id=row["actor_id"],
            stale_notified_at=_dt_from_db(row["stale_notified_at"]),
        )

    async def record_export(self, tournament_id: int, export_type: ExportType, revision: int, actor_id: int) -> None:
        """`revision` is the one the export was built from, read before building it."""
        async with self._lock:
            await self._conn.execute(
                "INSERT INTO export_log (tournament_id, type, revision, time, actor_id) VALUES (?, ?, ?, ?, ?)",
                (tournament_id, export_type.value, revision, _dt_to_db(utcnow()), actor_id),
            )
            await self._conn.commit()

    async def latest_exports(self, tournament_id: int) -> dict[ExportType, ExportRecord]:
        rows = await self._fetchall(
            "SELECT * FROM export_log WHERE id IN (SELECT max(id) FROM export_log WHERE tournament_id = ? GROUP BY type)",
            (tournament_id,),
        )
        return {record.type: record for record in map(self._export, rows)}

    async def mark_stale_notified(self, export_id: int) -> None:
        async with self._lock:
            await self._conn.execute(
                "UPDATE export_log SET stale_notified_at = ? WHERE id = ?", (_dt_to_db(utcnow()), export_id)
            )
            await self._conn.commit()

    async def changes_since(self, tournament_id: int, revision: int) -> list[ChangeRecord]:
        rows = await self._fetchall(
            "SELECT * FROM change_log WHERE tournament_id = ? AND revision > ? ORDER BY revision",
            (tournament_id, revision),
        )
        return [
            ChangeRecord(
                revision=row["revision"],
                time=datetime.fromisoformat(row["time"]),
                actor_id=row["actor_id"],
                signup_id=row["signup_id"],
                kind=row["kind"],
                details=json.loads(row["details"]),
            )
            for row in rows
        ]

    async def active_tournaments(self) -> list[Tournament]:
        rows = await self._fetchall("SELECT * FROM tournament WHERE archived_at IS NULL")
        return [self._tournament(row) for row in rows]


def _changed_fields(before: rules.SignupFields, after: rules.SignupFields) -> list[str]:
    return [f.name for f in dataclass_fields(before) if getattr(before, f.name) != getattr(after, f.name)]
