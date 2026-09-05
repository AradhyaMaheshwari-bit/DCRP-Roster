"""
bot.py
------
DCRP-Roster — Discord entry point.

Loads cogs, syncs slash commands to the configured guild, logs ready.
Run with:
    .venv\\Scripts\\python bot.py
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

import discord
from discord.ext import commands

from config import get_settings


def _build_logging() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


def _build_bot() -> commands.Bot:
    settings = get_settings()
    intents = discord.Intents.default()
    intents.members = True  # required for fetch_member / role assignment
    intents.guilds = True
    bot = commands.Bot(
        command_prefix=commands.when_mentioned,
        intents=intents,
    )
    return bot


async def main() -> None:
    _build_logging()
    log = logging.getLogger("bot")
    settings = get_settings()

    bot = _build_bot()

    @bot.event
    async def on_ready() -> None:
        log.info("Logged in as %s (id=%s)", bot.user, bot.user.id if bot.user else "?")
        # Sync slash commands to the configured guild.
        try:
            synced = await bot.tree.sync(guild=discord.Object(id=settings.guild_id))
            log.info("Synced %d commands to guild %d", len(synced), settings.guild_id)
        except Exception as exc:  # noqa: BLE001
            log.exception("Slash command sync failed: %s", exc)

    # Load cogs.
    for cog in ("cogs.role_request",):
        try:
            await bot.load_extension(cog)
        except Exception as exc:  # noqa: BLE001
            log.exception("Failed to load cog %s: %s", cog, exc)
            raise

    token = settings.discord_token
    if not token:
        log.error("DISCORD_TOKEN not set. Fill it into .env and re-run.")
        sys.exit(2)
    await bot.start(token)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
