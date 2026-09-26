"""Bot wiring: database, cogs, persistent views, command sync."""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from .db import Database
from .health import channel_problems
from .notify import AdminFeed
from .permissions import BOT_PERMISSIONS, NotAdmin, invite_url
from .settings import Settings
from .views.captain_card import CaptainAction
from .views.dashboard import DashboardView, Refresher
from .views.signup_post import SignupPostView

log = logging.getLogger(__name__)

EXTENSIONS = ("draftcup.cogs.admin", "draftcup.cogs.management", "draftcup.cogs.lifecycle")


class DraftCupBot(commands.Bot):
    """One process serves every server the bot is in; all data is kept per server (guild ID)."""

    db: Database

    def __init__(self, settings: Settings) -> None:
        intents = discord.Intents.default()
        intents.members = True  # to notice signed-up members leaving (spec §6.3)
        super().__init__(
            command_prefix=commands.when_mentioned,  # unused: everything is slash commands and buttons
            intents=intents,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        self.settings = settings
        self.feed = AdminFeed(self)
        self.refresher = Refresher(self)
        self._channels_checked = False
        self.tree.on_error = self.on_app_command_error

    async def setup_hook(self) -> None:
        self.db = await Database.open(self.settings.database_path)
        for extension in EXTENSIONS:
            await self.load_extension(extension)
        # Persistent buttons, including on messages posted before a restart: one view instance handles
        # every server's signup post, another every dashboard (fixed custom IDs), and captain card
        # buttons carry their signup ID.
        self.add_view(SignupPostView())
        self.add_view(DashboardView())
        self.add_dynamic_items(CaptainAction)

        # Global commands: available in every server the bot is in (test and production alike).
        synced = await self.tree.sync()
        log.info("Synced %d slash commands", len(synced))
        # setup_hook runs once per process, after login, so the application ID is known here.
        if self.application_id is not None:
            log.info(
                "Invite link (permissions %d): %s", BOT_PERMISSIONS.value, invite_url(self.application_id)
            )

    async def on_ready(self) -> None:
        log.info("Logged in as %s in %d server(s)", self.user, len(self.guilds))
        # on_ready also fires after reconnects: check the channels once per process.
        if self._channels_checked:
            return
        self._channels_checked = True
        for guild in self.guilds:
            config = await self.db.get_config(guild.id)
            for problem in channel_problems(self, config):
                log.warning("Server %r (%s): %s", guild.name, guild.id, problem)

    async def close(self) -> None:
        await super().close()
        if hasattr(self, "db"):
            await self.db.close()

    async def on_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, (NotAdmin, app_commands.NoPrivateMessage)):
            message = str(error) or "You can't use this command here."
        else:
            log.exception("Command %s failed", interaction.command and interaction.command.qualified_name, exc_info=error)
            message = "Something went wrong. Check the bot logs."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
