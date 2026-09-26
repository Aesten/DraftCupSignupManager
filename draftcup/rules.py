"""Signup validation and derived values (spec §4).

Pure functions only: no Discord or database code, so everything here is unit-tested.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

CLASSES = ("inf", "arc", "cav")
CLASS_LABELS = {"inf": "Infantry", "arc": "Archer", "cav": "Cavalry"}

NICKNAME_MIN = 2
NICKNAME_MAX = 24
_NICKNAME_RE = re.compile(r"^[A-Za-z0-9 _-]+$")

_STEAM_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?steamcommunity\.com/"
    r"(?:(?P<kind_id>id)/(?P<custom>[A-Za-z0-9_-]{2,32})|(?P<kind_profiles>profiles)/(?P<steamid>7656\d{13}))"
    r"/?$",
    re.IGNORECASE,
)

# Tier and budget tables (spec §4.1). Anything not listed (other letters or empty) uses the fallback.
TIER_BY_DIVISION = {"A": 1, "B": 2, "C": 3, "D": 4}
TIER_FALLBACK = 5

BUDGET_BASE = 20.0
BUDGET_BONUS_BY_DIVISION = {"A": 0.0, "B": 1.0, "C": 2.0, "D": 3.0}
BUDGET_BONUS_FALLBACK = 4.0
BUDGET_CLASS_MODIFIER = {"inf": 0.0, "arc": -1.0, "cav": -1.5}


class ValidationError(Exception):
    """Raised with a user-facing message when one field is invalid."""


def normalize_nickname(raw: str) -> str:
    nickname = " ".join(raw.split())
    if len(nickname) < NICKNAME_MIN or len(nickname) > NICKNAME_MAX:
        raise ValidationError(f"Nickname must be {NICKNAME_MIN}–{NICKNAME_MAX} characters long.")
    if not _NICKNAME_RE.match(nickname):
        raise ValidationError("Nickname may only contain letters, digits, spaces, `_` and `-`.")
    return nickname


def normalize_steam_url(raw: str) -> str:
    match = _STEAM_RE.match(raw.strip())
    if not match:
        raise ValidationError(
            "Steam profile must look like `https://steamcommunity.com/id/<name>` "
            "or `https://steamcommunity.com/profiles/7656…` (17 digits)."
        )
    if match["kind_id"]:
        return f"https://steamcommunity.com/id/{match['custom']}/"
    return f"https://steamcommunity.com/profiles/{match['steamid']}/"


def normalize_division(raw: str) -> str:
    """Returns an uppercase letter, or an empty string when the player never played competitive."""
    division = raw.strip().upper()
    if division and not (len(division) == 1 and "A" <= division <= "Z"):
        raise ValidationError("Highest division must be a single letter (A–Z), or left empty.")
    return division


def normalize_class(raw: str | None) -> str:
    if raw not in CLASSES:
        raise ValidationError("Pick a class: Infantry, Archer or Cavalry.")
    return raw


def normalize_igl(raw: str | None) -> bool:
    if raw not in ("yes", "no"):
        raise ValidationError("Answer whether you can lead in game (IGL).")
    return raw == "yes"


def tier_for(division: str) -> int:
    return TIER_BY_DIVISION.get(division, TIER_FALLBACK)


def budget_for(division: str, player_class: str) -> float:
    bonus = BUDGET_BONUS_BY_DIVISION.get(division, BUDGET_BONUS_FALLBACK)
    return round(BUDGET_BASE + bonus + BUDGET_CLASS_MODIFIER[player_class], 1)


@dataclass(frozen=True)
class SignupForm:
    """Raw values as typed in the signup modal. Also used to pre-fill it again."""

    nickname: str = ""
    steam_url: str = ""
    player_class: str | None = None
    highest_division: str = ""
    igl: str | None = None


@dataclass(frozen=True)
class SignupFields:
    """Validated, normalised signup fields."""

    nickname: str
    steam_url: str
    player_class: str
    highest_division: str
    igl: bool

    def to_form(self) -> SignupForm:
        return SignupForm(
            nickname=self.nickname,
            steam_url=self.steam_url,
            player_class=self.player_class,
            highest_division=self.highest_division,
            igl="yes" if self.igl else "no",
        )


def validate_form(form: SignupForm) -> tuple[SignupFields | None, list[str]]:
    """Validates every field and collects all errors, so the user sees them in one go."""
    errors: list[str] = []
    values: dict[str, object] = {}
    checks = (
        ("nickname", normalize_nickname, form.nickname),
        ("steam_url", normalize_steam_url, form.steam_url),
        ("player_class", normalize_class, form.player_class),
        ("highest_division", normalize_division, form.highest_division),
        ("igl", normalize_igl, form.igl),
    )
    for name, normalize, raw in checks:
        try:
            values[name] = normalize(raw)
        except ValidationError as exc:
            errors.append(str(exc))
    if errors:
        return None, errors
    return SignupFields(**values), []  # type: ignore[arg-type]
