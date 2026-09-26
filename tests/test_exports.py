import csv
import io
import json

from factories import T0, captain, make_signup

from draftcup import exports
from draftcup.models import CaptainStatus, ChangeRecord, Format, GuildConfig

NAMES = ["Division 1", "Division 2"]


def config(**kwargs) -> GuildConfig:
    return GuildConfig(guild_id=1, title="Draft Cup #12", **kwargs)


def picked(nickname, division_index, **kwargs):
    return captain(nickname, CaptainStatus.PICKED, division_index, **kwargs)


def test_slug():
    assert exports.slug("Draft Cup #12") == "draft-cup-12"
    assert exports.slug("###") == "draftcup"


def test_pool_includes_unpicked_captains_only():
    signups = [make_signup("P1"), picked("C1", 1), captain("C2", CaptainStatus.POOL), captain("C3")]
    assert [s.nickname for s in exports.pool(signups)] == ["P1", "C2"]


def test_tournament_export():
    signups = [
        make_signup("Alice", player_class="arc", division="A"),
        make_signup("Bob", player_class="cav", division=""),
        picked("CapOne", 1, player_class="cav", division="A"),
        picked("CapTwo", 1, player_class="inf", division="C"),
        captain("Spare", CaptainStatus.POOL, player_class="arc", division="D"),
    ]
    result = exports.build_tournament(config(), signups, NAMES)
    assert result.errors == []
    data = json.loads(result.files["draft-cup-12.draftcup.json"])
    assert data["title"] == "Draft Cup #12"
    assert data["format"] == "captainPick"
    assert data["tierMinimums"] == [2.0, 1.5, 1.0, 0.5, 0.1]
    assert data["players"] == [
        {"name": "Alice", "classes": ["arc"], "tier": 1},
        {"name": "Bob", "classes": ["cav"], "tier": 5},
        {"name": "Spare", "classes": ["arc"], "tier": 4},
    ]
    assert data["divisions"] == [{
        "name": "Division 1",
        "teamSize": 6,
        "captains": [
            {"name": "CapOne", "class": "cav", "budget": 18.5},
            {"name": "CapTwo", "class": "inf", "budget": 22.0},
        ],
    }]
    assert "id" not in data and "session" not in data
    # Division 2 is empty, division 1 is short of 8 captains, and the pool is short of 12 players.
    assert any("Division 2 has no captains" in w for w in result.warnings)
    assert any("2/8" in w for w in result.warnings)
    assert any("won't be full" in w for w in result.warnings)


def test_random_pick_keeps_tiers():
    signups = [make_signup("Alice", division="C"), picked("C1", 1), picked("C2", 1)]
    data = json.loads(exports.build_tournament(config(format=Format.RANDOM_PICK), signups, NAMES).files["draft-cup-12.draftcup.json"])
    assert data["format"] == "randomPick"
    assert data["players"][0]["tier"] == 3


def test_tournament_export_refused():
    signups = [make_signup("P"), captain("Undecided"), picked("Lonely", 2)]
    result = exports.build_tournament(config(), signups, NAMES)
    assert result.files == {}
    assert any("haven't been accepted or rejected yet: Undecided" in e for e in result.errors)
    assert any("Division 2 has only 1 captain" in e for e in result.errors)


def test_tournament_export_needs_a_division():
    result = exports.build_tournament(config(), [make_signup("P")], NAMES)
    assert result.errors == ["No captain has been accepted into a division yet."]


def test_player_list_skips_pending_captains():
    signups = [make_signup("P1", division="D"), captain("Pending"), captain("Pooled", CaptainStatus.POOL)]
    result = exports.build_player_list(config(), signups)
    data = json.loads(result.files["draft-cup-12.players.json"])
    assert data == {"players": [
        {"name": "P1", "classes": ["inf"], "tier": 4},
        {"name": "Pooled", "classes": ["inf"], "tier": 2},
    ]}
    assert "divisions" not in data
    assert "Pending" in result.warnings[0]


def test_csv_export():
    signups = [make_signup("Big Bob", igl=True), picked("Cap", 2, player_class="arc", division="B"), captain("Pooled", CaptainStatus.POOL)]
    result = exports.build_csv(config(), signups, ["North", "South"])
    players = result.files["draft-cup-12.players.csv"]
    assert players.startswith(b"\xef\xbb\xbf")  # BOM for Excel
    rows = list(csv.DictReader(io.StringIO(players.decode("utf-8-sig"))))
    assert [(r["nickname"], r["source"], r["igl"]) for r in rows] == [("Big Bob", "player", "yes"), ("Pooled", "captain-pool", "no")]
    assert rows[0]["created_at"] == "2026-10-01 12:00:00 UTC"

    captains = list(csv.DictReader(io.StringIO(result.files["draft-cup-12.captains.csv"].decode("utf-8-sig"))))
    assert [(r["nickname"], r["captain_status"], r["division"], r["budget"]) for r in captains] == [
        ("Cap", "picked", "South", "20.0"),
        ("Pooled", "pool", "", "21.0"),
    ]


def test_coverage_counts_surplus_captains_as_players():
    cfg = config(captains_per_division=2, team_size=5, division_count=2)
    signups = [make_signup(f"P{i}") for i in range(12)] + [captain(f"C{i}") for i in range(3)]
    one, two = exports.coverage(cfg, signups)
    assert (one.captains_needed, one.captains_available, one.players_needed, one.players_available) == (2, 3, 10, 13)
    assert (two.captains_needed, two.captains_available, two.players_needed, two.players_available) == (4, 3, 20, 12)


def test_summarize_changes():
    def change(kind, **details):
        return ChangeRecord(revision=1, time=T0, actor_id=None, signup_id=None, kind=kind, details=details)

    changes = [change("signup", role="player"), change("signup", role="player"), change("withdraw"), change("captain_status"), change("config")]
    assert exports.summarize_changes(changes) == "2 new players, 1 withdrawal, 1 captain decision, 1 settings change"
    assert exports.summarize_changes([]) == "no changes"
