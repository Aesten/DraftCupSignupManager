"""Process settings read from environment variables (spec §11)."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    discord_token: str
    database_path: str = "draftcup.db"
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> Settings:
        token = os.environ.get("DISCORD_TOKEN", "").strip()
        if not token:
            raise SystemExit("DISCORD_TOKEN is not set (see .env.example).")
        return cls(
            discord_token=token,
            database_path=os.environ.get("DATABASE_PATH", "").strip() or cls.database_path,
            log_level=os.environ.get("LOG_LEVEL", "").strip().upper() or cls.log_level,
        )
