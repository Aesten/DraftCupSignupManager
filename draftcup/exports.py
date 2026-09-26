"""Export builders (spec §9). Pure functions: signups in, file content out."""

from __future__ import annotations

import csv
import io
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from .models import CaptainStatus, ChangeRecord, GuildConfig, Role, Signup

DEFAULT_TIER_MINIMUMS = [2.0, 1.5, 1.0, 0.5, 0.1]
MIN_CAPTAINS_PER_DIVISION = 2  # the auction app can't start a division with fewer


@dataclass
class ExportResult:
    files: dict[str, bytes] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def slug(title: str) -> str:
    value = re.sub(r"[^A-Za-z0-9]+", "-", title).strip("-").lower()
    return value or "draftcup"


def pool(signups: list[Signup]) -> list[Signup]:
    """Players plus captain candidates who weren't picked, in signup order (spec §2, Pool)."""
    return [
        s for s in signups
        if s.withdrawn_at is None and (s.role is Role.PLAYER or s.captain_status is CaptainStatus.POOL)
    ]


def captain_candidates(signups: list[Signup]) -> list[Signup]:
    return [s for s in signups if s.withdrawn_at is None and s.role is Role.CAPTAIN]


def pending_captains(signups: list[Signup]) -> list[Signup]:
    return [s for s in captain_candidates(signups) if s.captain_status is CaptainStatus.PENDING]


def _pool_entry(signup: Signup) -> dict[str, Any]:
    return {"name": signup.nickname, "classes": [signup.player_class], "tier": signup.tier}


def _csv_bytes(header: list[str], rows: list[list[Any]]) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(header)
    writer.writerows(rows)
    # UTF-8 with BOM so Excel detects the encoding.
    return buffer.getvalue().encode("utf-8-sig")


def _iso(value) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S UTC") if value else ""


_COMMON_HEADER = ["nickname", "discord_id", "discord_username", "steam_url", "class", "highest_division", "tier", "igl"]
_TRAILING_HEADER = ["agreed_at", "created_at", "updated_at", "left_server"]


def _common(signup: Signup) -> list[Any]:
    return [
        signup.nickname, str(signup.user_id), signup.username, signup.steam_url, signup.player_class,
        signup.highest_division, signup.tier, "yes" if signup.igl else "no",
    ]


def _trailing(signup: Signup) -> list[Any]:
    return [_iso(signup.agreed_at), _iso(signup.created_at), _iso(signup.updated_at), "yes" if signup.left_server else ""]


def build_csv(config: GuildConfig, signups: list[Signup], division_names: list[str]) -> ExportResult:
    """players.csv and captains.csv, for manual checks (spec §9.1)."""
    players = [
        _common(s) + ["player" if s.role is Role.PLAYER else "captain-pool"] + _trailing(s)
        for s in pool(signups)
    ]
    captains = []
    for s in captain_candidates(signups):
        division = division_names[s.division_index - 1] if s.division_index and s.division_index <= len(division_names) else ""
        status = s.captain_status.value if s.captain_status else ""
        captains.append(_common(s) + [status, division, f"{s.budget:.1f}"] + _trailing(s))
    prefix = slug(config.title)
    return ExportResult(files={
        f"{prefix}.players.csv": _csv_bytes(_COMMON_HEADER + ["source"] + _TRAILING_HEADER, players),
        f"{prefix}.captains.csv": _csv_bytes(
            _COMMON_HEADER + ["captain_status", "division", "budget"] + _TRAILING_HEADER, captains
        ),
    })


def _json_bytes(data: dict[str, Any]) -> bytes:
    return (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _pending_warning(pending: list[Signup]) -> str:
    names = ", ".join(s.nickname for s in pending)
    return f"{len(pending)} captain candidate(s) still pending and left out: {names}."


def build_player_list(config: GuildConfig, signups: list[Signup]) -> ExportResult:
    """Pool-only JSON for late signups, imported with "Import…" in the app's pool (spec §9.2)."""
    result = ExportResult()
    pending = pending_captains(signups)
    if pending:
        result.warnings.append(_pending_warning(pending))
    data = {"players": [_pool_entry(s) for s in pool(signups)]}
    result.files[f"{slug(config.title)}.players.json"] = _json_bytes(data)
    return result


def build_tournament(config: GuildConfig, signups: list[Signup], division_names: list[str]) -> ExportResult:
    """The full `.draftcup.json` file (spec §9.3). Refused (no files) when `errors` is non-empty."""
    result = ExportResult()
    pending = pending_captains(signups)
    if pending:
        result.errors.append(
            f"{len(pending)} captain candidate(s) are still pending: "
            + ", ".join(s.nickname for s in pending)
            + ". Pick them into a division or move them to the pool."
        )

    by_division: dict[int, list[Signup]] = {i: [] for i in range(1, len(division_names) + 1)}
    for s in captain_candidates(signups):
        if s.captain_status is CaptainStatus.PICKED and s.division_index is not None:
            if s.division_index not in by_division:
                result.errors.append(f"{s.nickname} is in division {s.division_index}, which no longer exists.")
                continue
            by_division[s.division_index].append(s)

    players = pool(signups)
    divisions = []
    picked_total = 0
    for index, captains in by_division.items():
        name = division_names[index - 1]
        if not captains:
            result.warnings.append(f"{name} has no captains and is left out of the file.")
            continue
        if len(captains) < MIN_CAPTAINS_PER_DIVISION:
            result.errors.append(f"{name} has only {len(captains)} captain; the auction needs at least {MIN_CAPTAINS_PER_DIVISION}.")
        elif len(captains) < config.captains_per_division:
            result.warnings.append(f"{name} has {len(captains)}/{config.captains_per_division} captains.")
        picked_total += len(captains)
        divisions.append({
            "name": name,
            "teamSize": config.team_size,
            "halfBudgetCapAtStart": config.half_budget_cap,
            "captains": [{"name": s.nickname, "class": s.player_class, "budget": s.budget} for s in captains],
        })
    if not divisions:
        result.errors.append("No division has captains yet.")

    needed = picked_total * config.team_size
    if divisions and len(players) < needed:
        result.warnings.append(
            f"The pool has {len(players)} players but {picked_total} teams × {config.team_size} need {needed}; "
            "some teams won't be full."
        )

    if result.errors:
        return result
    data = {
        "title": config.title,
        "format": config.format.value,
        "tierMinimums": DEFAULT_TIER_MINIMUMS,
        "players": [_pool_entry(s) for s in players],
        "divisions": divisions,
    }
    result.files[f"{slug(config.title)}.draftcup.json"] = _json_bytes(data)
    return result


# ------------------------------------------------------------------- statistics


@dataclass
class Coverage:
    divisions: int
    captains_needed: int
    captains_available: int
    players_needed: int
    players_available: int


def coverage(config: GuildConfig, signups: list[Signup]) -> list[Coverage]:
    """Needs versus signups for 1…division_count divisions (spec §6.1).

    Captain candidates not yet moved to the pool count as captains, and any surplus beyond the captains
    needed counts as players, since unpicked captains go to the pool.
    """
    candidates = [s for s in captain_candidates(signups) if s.captain_status is not CaptainStatus.POOL]
    base_players = len(pool(signups))
    rows = []
    for d in range(1, config.division_count + 1):
        captains_needed = d * config.captains_per_division
        surplus = max(0, len(candidates) - captains_needed)
        rows.append(Coverage(
            divisions=d,
            captains_needed=captains_needed,
            captains_available=len(candidates),
            players_needed=captains_needed * config.team_size,
            players_available=base_players + surplus,
        ))
    return rows


_CHANGE_LABELS = {
    # kind: (singular, plural)
    "signup:player": ("new player", "new players"),
    "signup:captain": ("new captain candidate", "new captain candidates"),
    "withdraw": ("withdrawal", "withdrawals"),
    "switch": ("role switch", "role switches"),
    "edit": ("edit", "edits"),
    "captain_status": ("captain decision", "captain decisions"),
    "member": ("member left or rejoined", "members left or rejoined"),
    "settings": ("settings change", "settings changes"),
}


def summarize_changes(changes: list[ChangeRecord]) -> str:
    """Short summary for stale-export notices, e.g. "2 new players, 1 withdrawal"."""
    counts: Counter[str] = Counter()
    for change in changes:
        if change.kind == "signup":
            key = f"signup:{change.details.get('role', 'player')}"
        elif change.kind in ("left_server", "rejoined_server"):
            key = "member"
        elif change.kind in _CHANGE_LABELS:
            key = change.kind
        else:
            key = "settings"
        counts[key] += 1
    parts = [f"{n} {_CHANGE_LABELS[key][0 if n == 1 else 1]}" for key, n in counts.items()]
    return ", ".join(parts) or "no changes"
