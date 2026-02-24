"""Master Bot (C.43)

Project recipe is stored in `RECIPE_INTERNAL.txt` (not surfaced by the bot).
"""

from __future__ import annotations

import logging
import aiohttp
import structlog
import os
import sys
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv, find_dotenv
from pathlib import Path

# Load environment variables from .env (if present) before reading BOT_TOKEN.
# IMPORTANT: do NOT override existing environment variables provided by the host panel.
# If override=True, an incomplete/old .env can clobber panel-provided secrets.
dotenv_path = Path(__file__).resolve().parent / ".env"
if dotenv_path.exists():
    load_dotenv(dotenv_path=dotenv_path, override=False)
else:
    load_dotenv(find_dotenv(usecwd=True), override=False)


# Keep this name exactly.
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

VERSION = "D.4"

# Expose build id for other modules (e.g., startup/status embeds).
# Force it to match this deployment so the startup message always shows the
# correct build even if the panel has an older env var.
os.environ["BOT_BUILD_ID"] = VERSION

# ---------------------------------------------------------------------------
# Compatibility patch: umongo==3.0.0b10 uses `if not db:` checks, but
# pymongo Database objects intentionally raise NotImplementedError for
# truthiness. Poketwo's umongo models hit this during command checks.
#
# We patch Database.__bool__ to return True so legacy truthiness checks work.
# This is a narrow, pragmatic runtime shim (no env changes).
# ---------------------------------------------------------------------------
def _patch_pymongo_database_truthiness() -> None:
    try:
        from pymongo.database import Database  # type: ignore

        # Avoid re-patching if already handled elsewhere.
        if getattr(Database, "__patched_truthiness__", False):
            return

        def _db_bool(self) -> bool:  # noqa: ANN001
            return True

        Database.__bool__ = _db_bool  # type: ignore
        Database.__nonzero__ = _db_bool  # type: ignore[attr-defined]
        setattr(Database, "__patched_truthiness__", True)
    except Exception:
        # If pymongo isn't installed yet or API differs, fail silently.
        return


_patch_pymongo_database_truthiness()

# ----------------------------
# Console log styling (ANSI)
# ----------------------------
class _Ansi:
    RESET = "\x1b[0m"
    DIM = "\x1b[2m"
    BOLD = "\x1b[1m"
    RED = "\x1b[31m"
    GREEN = "\x1b[32m"
    YELLOW = "\x1b[33m"
    BLUE = "\x1b[34m"
    MAGENTA = "\x1b[35m"
    CYAN = "\x1b[36m"
    GRAY = "\x1b[90m"

_LEVEL_COLOR = {
    "DEBUG": _Ansi.GRAY,
    "INFO": _Ansi.CYAN,
    "WARNING": _Ansi.YELLOW,
    "ERROR": _Ansi.RED,
    "CRITICAL": _Ansi.RED + _Ansi.BOLD,
}

class ColorFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        level = record.levelname
        color = _LEVEL_COLOR.get(level, "")
        record.levelname = f"{color}{level:<8}{_Ansi.RESET}"
        return super().format(record)

def setup_logging() -> None:
    # PebbleHost console usually supports ANSI; if not, it will just show codes.
    level = os.getenv("LOG_LEVEL", "INFO").upper().strip()
    if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        level = "INFO"

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(ColorFormatter(
        fmt="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

setup_logging()
log = logging.getLogger("bot")

def build_intents() -> discord.Intents:
    # All intents (including privileged). You must also enable privileged intents
    # in the Discord Developer Portal for them to actually be delivered.
    return discord.Intents.all()

def _diag_intents(intents: discord.Intents) -> str:
    parts = []
    # A few high-signal ones:
    parts.append(f"members={intents.members}")
    parts.append(f"presences={intents.presences}")
    parts.append(f"message_content={intents.message_content}")
    parts.append(f"guilds={intents.guilds}")
    parts.append(f"messages={intents.messages}")
    parts.append(f"reactions={intents.reactions}")
    return ", ".join(parts)

class Bot(commands.AutoShardedBot):
    def __init__(self) -> None:
        self.boot_utc = datetime.now(timezone.utc)
        # Poketwo vendor expects these attributes on the bot.
        self.cluster_idx = int(os.getenv("POKETWO_CLUSTER_IDX", "0") or "0")
        # Pokétwo's vendored Context expects `bot.log.bind(...)` (structlog style).
        # Provide it even if the rest of the bot uses stdlib logging.
        if not hasattr(self, "log"): 
            self.log = structlog.get_logger("bot")

        # Note: discord.py manages shard state internally; `.shards` is a read-only property.
        intents = build_intents()

        # Text prefixes are required for Pokétwo's mention/prefix gameplay.
        # - Keep "!" for any existing text commands.
        # - Add "!p" as a dedicated short prefix for Pokétwo.
        # - Always allow @mention as a prefix.
        async def _prefixes(bot: commands.Bot, message: discord.Message):
            return commands.when_mentioned_or("!p ", "!p")(bot, message)

        super().__init__(
            command_prefix=_prefixes,
            intents=intents,
        )

        # Pokétwo vendor cogs expect a menus cache on the bot instance.
        # If missing, text commands like `!p help` can crash.
        try:
            from expiringdict import ExpiringDict  # type: ignore

            self.menus = ExpiringDict(max_len=300, max_age_seconds=300)  # type: ignore[attr-defined]
        except Exception:
            self.menus = {}  # type: ignore[attr-defined]

        log.info("boot: version=%s python=%s discord.py=%s", VERSION, sys.version.split()[0], discord.__version__)
        log.info("boot: intents: %s", _diag_intents(intents))

    async def setup_hook(self) -> None:
        # Shared aiohttp session (Poketwo and other packs may use this)
        if getattr(self, "http_session", None) is None:
            self.http_session = aiohttp.ClientSession()

        log.info("boot: loading extension plugins.settings.setting")
        await self.load_extension("plugins.settings.setting")
        log.info("boot: extension loaded")

        # Poketwo hub (single slash command: /poketwo)
        log.info("boot: loading extension plugins.poketwo.poketwo")
        # Poketwo is loaded via the Settings Feature Manager (plugins.settings.setting)
        log.info("boot: poketwo extension loaded")

        # NOTE: We intentionally avoid a global sync during startup.
        # Global commands can linger and appear as duplicates alongside guild
        # commands. We clear/sync explicitly in on_ready.

    async def on_ready(self) -> None:
        # Diagnostics snapshot on each ready
        try:
            guild_count = len(self.guilds)
        except Exception:
            guild_count = -1
        log.info("ready: logged in as %s (%s)", self.user, self.user.id if self.user else "n/a")
        log.info("ready: guilds=%s latency_ms=%.0f", guild_count, (self.latency * 1000.0))

        # COMMAND PUBLISHING STRATEGY
        # Keep this simple to avoid "duplicate commands" and avoid breaking interactions.
        #
        # - If DEV_GUILD_ID is set, sync ONLY to that guild (instant updates).
        # - Otherwise, sync globally.
        try:
            dev_gid = os.getenv("DEV_GUILD_ID", "").strip()
            if dev_gid.isdigit():
                await self.tree.sync(guild=discord.Object(id=int(dev_gid)))
                log.info("ready: app_commands: synced (dev guild)")
            else:
                await self.tree.sync()
                log.info("ready: app_commands: synced (global)")
        except Exception:
            log.exception("ready: app_commands: failed syncing")


    async def on_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:  # type: ignore[name-defined]
        # Keep errors visible in logs; UI responses are handled by handlers.
        log.exception("app_command_error: %r", error)

    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        # Suppress noisy "CommandNotFound" errors (especially around the !p router).
        if isinstance(error, commands.CommandNotFound):
            return
        log.exception("command_error: %s", error)

    async def close(self) -> None:
        # Gracefully close shared aiohttp session used by some feature packs.
        try:
            sess = getattr(self, "http_session", None)
            if sess is not None and not sess.closed:
                await sess.close()
        except Exception:
            pass
        await super().close()

def main() -> None:
    load_dotenv(dotenv_path=Path(__file__).with_name(".env"), override=True)
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("BOT_TOKEN is missing. Put it in .env or panel environment variables.")

    bot = Bot()
    bot.run(token)

if __name__ == "__main__":
    main()
