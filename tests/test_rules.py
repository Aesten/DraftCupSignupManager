import pytest

from draftcup import rules
from draftcup.rules import SignupForm, ValidationError


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Bob", "Bob"),
        ("  Big   Bob  ", "Big Bob"),
        ("x_Y-9", "x_Y-9"),
        ("a" * 24, "a" * 24),
        ("Bob\tX", "Bob X"),
    ],
)
def test_nickname_accepts(raw, expected):
    assert rules.normalize_nickname(raw) == expected


@pytest.mark.parametrize("raw", ["", "a", " a ", "a" * 25, "Bob!", "[TAG] Bob", "Bøb", "Bob."])
def test_nickname_rejects(raw):
    with pytest.raises(ValidationError):
        rules.normalize_nickname(raw)


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("https://steamcommunity.com/id/bob_the-archer/", "https://steamcommunity.com/id/bob_the-archer/"),
        ("http://www.steamcommunity.com/id/Bob", "https://steamcommunity.com/id/Bob/"),
        ("steamcommunity.com/profiles/76561198000000000", "https://steamcommunity.com/profiles/76561198000000000/"),
        ("  HTTPS://SteamCommunity.com/profiles/76561198000000000/  ", "https://steamcommunity.com/profiles/76561198000000000/"),
    ],
)
def test_steam_accepts(raw, expected):
    assert rules.normalize_steam_url(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "https://steamcommunity.com/",
        "https://steamcommunity.com/profiles/1234",
        "https://steamcommunity.com/profiles/12345678901234567",  # doesn't start with 7656
        "https://steamcommunity.com/id/bob/games",
        "https://evil.com/steamcommunity.com/id/bob",
        "https://steamcommunity.com.evil.com/id/bob",
    ],
)
def test_steam_rejects(raw):
    with pytest.raises(ValidationError):
        rules.normalize_steam_url(raw)


@pytest.mark.parametrize("raw, expected", [("", ""), (" ", ""), ("a", "A"), ("Z", "Z")])
def test_division_accepts(raw, expected):
    assert rules.normalize_division(raw) == expected


@pytest.mark.parametrize("raw", ["1", "AB", "é", "-"])
def test_division_rejects(raw):
    with pytest.raises(ValidationError):
        rules.normalize_division(raw)


@pytest.mark.parametrize("division, tier", [("A", 1), ("B", 2), ("C", 3), ("D", 4), ("E", 5), ("Z", 5), ("", 5)])
def test_tier(division, tier):
    assert rules.tier_for(division) == tier


@pytest.mark.parametrize(
    "division, player_class, budget",
    [
        ("A", "inf", 20.0),
        ("A", "cav", 18.5),
        ("B", "arc", 20.0),
        ("C", "cav", 20.5),
        ("D", "inf", 23.0),
        ("E", "inf", 24.0),
        ("", "arc", 23.0),
    ],
)
def test_budget(division, player_class, budget):
    assert rules.budget_for(division, player_class) == budget


def test_validate_form_collects_all_errors():
    fields, errors = rules.validate_form(SignupForm(nickname="!", steam_url="nope", highest_division="12"))
    assert fields is None
    assert len(errors) == 5


def test_validate_form_round_trip():
    form = SignupForm(
        nickname=" Big  Bob ",
        steam_url="steamcommunity.com/id/bigbob",
        player_class="cav",
        highest_division="c",
        igl="yes",
    )
    fields, errors = rules.validate_form(form)
    assert errors == []
    assert fields == rules.SignupFields("Big Bob", "https://steamcommunity.com/id/bigbob/", "cav", "C", True)
    assert rules.validate_form(fields.to_form()) == (fields, [])
