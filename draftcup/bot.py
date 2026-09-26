"""Bot wiring: database, cogs, persistent views, command sync."""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from .db import Database
from .permissions import NotAdmin
from .settings import Settings
from .views.signup_post import SignupPostView

log = logging.getLogger(__name__)

EXTENSIONS = ("draftcup.cogs.admin", "draftcup.cogs.lifecycle")


class DraftCupBot(commands.Bot):
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
        self.tree.on_error = self.on_app_command_error

    async def setup_hook(self) -> None:
        self.db = await Database.open(self.settings.database_path)
        for extension in EXTENSIONS:
            await self.load_extension(extension)
        # One instance handles the buttons of every server's signup post, including posts made
        # before a restart (the custom IDs are fixed).
        self.add_view(SignupPostView())

        if self.settings.dev_guild_id:
            guild = discord.Object(self.settings.dev_guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        synced = await self.tree.sync()
        log.info("Synced %d slash commands", len(synced))

    async def on_ready(self) -> None:
        log.info("Logged in as %s in %d server(s)", self.user, len(self.guilds))

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
