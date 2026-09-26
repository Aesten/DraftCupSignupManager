"""SQLite persistence (spec §10)."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import fields as dataclass_fields
from datetime import datetime, timezone
from typing import Any

import aiosqlite

from . import rules
from .models import CaptainStatus, Format, GuildConfig, Role, Signup, State, Tournament

SCHEMA_VERSION = 1

_SCHEMA = """
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

# Columns of guild_config that map 1:1 to GuildConfig attributes.
_CONFIG_COLUMNS = (
    "title", "format", "captains_per_division", "team_size", "division_count", "half_budget_cap",
    "timezone", "tournament_date", "auction_date", "closes_at", "rules_url",
    "signup_channel_id", "admin_channel_id", "signup_message_id", "status_message_id",
)
_CONFIG_DATETIMES = {"tournament_date", "auction_date", "closes_at"}


class NicknameTaken(Exception):
    def __init__(self, nickname: str) -> None:
        super().__init__(f"The nickname **{nickname}** is already taken.")
        self.nickname = nickname


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
        if version == 0:
            await self._conn.executescript(_SCHEMA)
            await self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            await self._conn.commit()
        elif version > SCHEMA_VERSION:
            raise RuntimeError(f"Database schema v{version} is newer than this bot (v{SCHEMA_VERSION}).")

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
                await self._conn.execute("INSERT OR IGNORE INTO guild_config (guild_id) VALUES (?)", (guild_id,))
                await self._conn.commit()
            row = await self._fetchone("SELECT * FROM guild_config WHERE guild_id = ?", (guild_id,))
            assert row is not None
        values: dict[str, Any] = {}
        for column in _CONFIG_COLUMNS:
            value = row[column]
            if column in _CONFIG_DATETIMES:
                value = _dt_from_db(value)
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
        unknown = set(changes) - set(_CONFIG_COLUMNS)
        if unknown:
            raise ValueError(f"Unknown config keys: {', '.join(sorted(unknown))}")
        await self.get_config(guild_id)  # ensures the row exists
        if changes:
            values = []
            for key, value in changes.items():
                if key in _CONFIG_DATETIMES:
                    value = _dt_to_db(value)
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


def _changed_fields(before: rules.SignupFields, after: rules.SignupFields) -> list[str]:
    return [f.name for f in dataclass_fields(before) if getattr(before, f.name) != getattr(after, f.name)]
