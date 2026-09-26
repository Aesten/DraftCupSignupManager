"""What happens after data changes: admin notices, captain cards, the dashboard and the public post."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .models import Role, Signup
from .notify import captain_status_line, signup_change_line
from .views import captain_card

if TYPE_CHECKING:
    from .bot import DraftCupBot


async def signup_changed(
    bot: DraftCupBot, guild_id: int, signup: Signup, kind: str, details: dict[str, Any], actor_id: int | None
) -> None:
    """After a signup was created, edited, switched, withdrawn, or its member left/rejoined."""
    if kind == "unchanged":
        return
    line = signup_change_line(
        signup.user_id, actor_id, kind, details, signup.player_class, signup.highest_division
    )
    await bot.feed.line(guild_id, line)
    if signup.role is Role.CAPTAIN or signup.review_message_id is not None:
        await captain_card.sync_card(bot, guild_id, signup)
    bot.refresher.request(guild_id)  # dashboard, and the public post's counts


async def captain_status_changed(
    bot: DraftCupBot, guild_id: int, details: dict[str, Any], actor_id: int, division_names: list[str]
) -> None:
    await bot.feed.line(guild_id, captain_status_line(details, actor_id, division_names))
    bot.refresher.request(guild_id)


async def tournament_changed(bot: DraftCupBot, guild_id: int) -> None:
    """After a settings or open/closed state change: refresh the dashboard and the public post."""
    bot.refresher.request(guild_id)
