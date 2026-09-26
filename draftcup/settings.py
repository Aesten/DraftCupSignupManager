"""Process settings read from environment variables (spec §11)."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    discord_token: str
    database_path: str = "draftcup.db"
    log_level: str = "INFO"
    # Optional: also sync slash commands to this server instantly, which is handy while developing.
    dev_guild_id: int | None = None

    @classmethod
    def from_env(cls) -> Settings:
        token = os.environ.get("DISCORD_TOKEN", "").strip()
        if not token:
            raise SystemExit("DISCORD_TOKEN is not set (see .env.example).")
        dev_guild = os.environ.get("DEV_GUILD_ID", "").strip()
        return cls(
            discord_token=token,
            database_path=os.environ.get("DATABASE_PATH", "").strip() or cls.database_path,
            log_level=os.environ.get("LOG_LEVEL", "").strip().upper() or cls.log_level,
            dev_guild_id=int(dev_guild) if dev_guild else None,
        )
