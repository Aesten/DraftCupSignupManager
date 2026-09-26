from types import SimpleNamespace

import discord
import pytest

from draftcup import health, notify
from draftcup.models import GuildConfig


class FakeChannel:
    def __init__(self, name: str, permissions: discord.Permissions) -> None:
        self.name = name
        self.id = hash(name)
        self._permissions = permissions

    def permissions_for(self, member):
        return self._permissions


def bot_member():
    role = SimpleNamespace(name="DraftCup Bot", managed=True)
    return SimpleNamespace(roles=[role], top_role=role, display_name="DraftCup")


ALL = discord.Permissions(
    view_channel=True, send_messages=True, embed_links=True, attach_files=True, read_message_history=True, pin_messages=True
)


def test_missing_permissions_names_what_to_allow():
    hidden = FakeChannel("admin", discord.Permissions.none())
    me = bot_member()
    missing = health.missing_permissions(hidden, me, health.ADMIN_CHANNEL_PERMISSIONS)
    assert missing[0] == "View Channel" and "Pin Messages" in missing
    text = health.describe(hidden, me, missing)
    assert text.startswith("#admin: allow View Channel") and "@DraftCup Bot" in text
    assert health.missing_permissions(FakeChannel("ok", ALL), me, health.ADMIN_CHANNEL_PERMISSIONS) == []


def test_channel_problems():
    me = bot_member()
    channels = {1: FakeChannel("signups", ALL), 2: FakeChannel("admin", discord.Permissions(view_channel=True))}
    guild = SimpleNamespace(me=me, get_channel=channels.get)
    bot = SimpleNamespace(get_guild=lambda guild_id: guild)
    problems = health.channel_problems(bot, GuildConfig(guild_id=5, signup_channel_id=1, admin_channel_id=2))
    assert len(problems) == 1 and problems[0].startswith("admin channel #admin: allow Send Messages")
    problems = health.channel_problems(bot, GuildConfig(guild_id=5, signup_channel_id=3))
    assert problems == ["the signup channel no longer exists; run /setup again"]


class FailingChannel:
    """A text channel the bot can't post in, until `fixed` is set."""

    def __init__(self) -> None:
        self.id = 1
        self.fixed = False

    async def send(self, *args, **kwargs):
        if not self.fixed:
            response = SimpleNamespace(status=403, reason="Forbidden")
            raise discord.Forbidden(response, {"code": 50001, "message": "Missing Access"})
        return SimpleNamespace(content=args[0] if args else "", channel=self)


async def test_feed_reports_unreachable_admin_channel_once(monkeypatch, caplog):
    channel = FailingChannel()

    async def fake_admin_channel(bot, guild_id):
        return channel

    monkeypatch.setattr(notify, "admin_channel", fake_admin_channel)
    feed = notify.AdminFeed(bot=None)
    with caplog.at_level("INFO", logger="draftcup.notify"):
        await feed.line(7, "one")
        assert await feed.send(7, "two") is None
    assert "missing permissions" in feed.unreachable[7]
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1 and not any(r.exc_info for r in caplog.records)  # one line, no traceback

    channel.fixed = True
    with caplog.at_level("INFO", logger="draftcup.notify"):
        assert await feed.send(7, "three") is not None
    assert 7 not in feed.unreachable
    assert any("works again" in r.message for r in caplog.records)


@pytest.mark.parametrize("perm", list(health.ADMIN_CHANNEL_PERMISSIONS))
def test_permission_names_exist(perm):
    assert hasattr(discord.Permissions.none(), perm)
