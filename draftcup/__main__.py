"""Entry point: `python -m draftcup`."""

from __future__ import annotations

import logging

from dotenv import load_dotenv

from .bot import DraftCupBot
from .settings import Settings


def main() -> None:
    load_dotenv()  # a .env file in the working directory is optional; real env vars win
    settings = Settings.from_env()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    bot = DraftCupBot(settings)
    bot.run(settings.discord_token, log_handler=None)


if __name__ == "__main__":
    main()
