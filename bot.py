from __future__ import annotations

import os
import sys
import logging
import re
import shutil
import subprocess
import tempfile
from fnmatch import fnmatch
from pathlib import Path
from datetime import datetime, timezone

import discord
from discord.ext import commands
from dotenv import load_dotenv, find_dotenv

VERSION = "D.8"
os.environ["BOT_BUILD_ID"] = VERSION

# Load .env if present, but do NOT override host-provided variables.
dotenv_path = Path(__file__).with_name(".env")
if dotenv_path.exists():
    load_dotenv(dotenv_path=dotenv_path, override=False)
else:
    load_dotenv(find_dotenv(usecwd=True), override=False)

# Keep this name exactly (project rule).
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper().strip()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("bot")


# ============================
# BOOT GITHUB PUSH (FAIL HARD)
# ============================
# Runs BEFORE connecting to Discord.
# If push fails, the process exits with an exception (fail hard).

EXCLUDE_DIRS = {
    ".git",
    "__pycache__",
    "data",
    "sprites",
    "poketwo_data",
    "cache",
    "logs",
}

EXCLUDE_PATH_PREFIXES = {
    "plugins/poketwo/data",
    "plugins/poketwo/sprites",
}

EXCLUDE_FILE_PATTERNS = (
    "*.pyc",
    "*.log",
    "*.sqlite",
    "*.db",
    ".env",
)

# Tripwire patterns to avoid pushing secrets by accident.
SECRET_PATTERNS = [
    re.compile(r"BOT_TOKEN\s*=", re.I),
    re.compile(r"mongodb\+srv://", re.I),
    re.compile(r"redis(s)?://", re.I),
    re.compile(r"GITHUB_TOKEN\s*=", re.I),
]


def _run(cmd: list[str], cwd: Path, *, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=str(cwd), text=True, capture_output=True, check=check)


def _is_excluded(rel_posix: str) -> bool:
    # Never include .env or any *.env-like file.
    if rel_posix == ".env" or rel_posix.endswith(".env"):
        return True

    for pfx in EXCLUDE_PATH_PREFIXES:
        if rel_posix == pfx or rel_posix.startswith(pfx + "/"):
            return True

    parts = rel_posix.split("/")
    if parts and parts[0] in EXCLUDE_DIRS:
        return True

    name = Path(rel_posix).name
    for pat in EXCLUDE_FILE_PATTERNS:
        if fnmatch(name, pat):
            return True

    return False


def _copy_code_only(src_root: Path, dst_root: Path) -> None:
    for p in src_root.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(src_root).as_posix()
        if _is_excluded(rel):
            continue
        out = dst_root / p.relative_to(src_root)
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, out)


def _scan_for_secrets(root: Path) -> list[str]:
    hits: list[str] = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if p.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".zip"}:
            continue
        try:
            data = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for rx in SECRET_PATTERNS:
            if rx.search(data):
                hits.append(f"{p.relative_to(root).as_posix()} matched {rx.pattern}")
                break
    return hits


def boot_github_push_fail_hard() -> None:
    """Push a code-only snapshot to GitHub at process start.

    Fail hard: if GitHub creds are missing OR the push fails, raise.

    Credentials must exist in the REAL .env (excluded from GitHub):
      - GITHUB_REPO=owner/repo
      - GITHUB_BRANCH=main
      - GITHUB_TOKEN=github_pat_...
    """

    repo = os.getenv("GITHUB_REPO", "").strip()
    branch = os.getenv("GITHUB_BRANCH", "main").strip()
    token = os.getenv("GITHUB_TOKEN", "").strip()

    if not repo or not branch or not token:
        raise RuntimeError(
            "Boot GitHub Push failed: missing GITHUB_REPO / GITHUB_BRANCH / GITHUB_TOKEN in runtime .env."
        )

    src_root = Path.cwd().resolve()

    with tempfile.TemporaryDirectory(prefix="codeonly_") as td:
        snap = Path(td) / "repo"
        snap.mkdir(parents=True, exist_ok=True)

        _copy_code_only(src_root, snap)

        hits = _scan_for_secrets(snap)
        if hits:
            msg = "Boot GitHub Push blocked: potential secrets detected in snapshot:\n" + "\n".join(hits[:25])
            if len(hits) > 25:
                msg += f"\n...and {len(hits) - 25} more"
            raise RuntimeError(msg)

        _run(["git", "init"], cwd=snap)
        _run(["git", "checkout", "-b", branch], cwd=snap)

        author_name = os.getenv("GITHUB_AUTHOR_NAME", "Master Botter").strip()
        author_email = os.getenv("GITHUB_AUTHOR_EMAIL", "bot@users.noreply.github.com").strip()
        _run(["git", "config", "user.name", author_name], cwd=snap)
        _run(["git", "config", "user.email", author_email], cwd=snap)

        remote_url = f"https://x-access-token:{token}@github.com/{repo}.git"
        _run(["git", "remote", "add", "origin", remote_url], cwd=snap)

        _run(["git", "add", "-A"], cwd=snap)
        status = _run(["git", "status", "--porcelain"], cwd=snap).stdout.strip()
        if not status:
            log.info("boot_github_push: no changes in snapshot")
            return

        _run(["git", "commit", "-m", f"Boot sync (build {VERSION})"], cwd=snap)

        push = _run(["git", "push", "--force", "origin", branch], cwd=snap, check=False)
        if push.returncode != 0:
            raise RuntimeError(
                "Boot GitHub Push failed:\n"
                f"STDOUT:\n{push.stdout[-2000:]}\n"
                f"STDERR:\n{push.stderr[-2000:]}"
            )

        commit = _run(["git", "rev-parse", "HEAD"], cwd=snap).stdout.strip()
        log.info("boot_github_push: pushed branch=%s commit=%s", branch, commit)


def build_intents() -> discord.Intents:
    intents = discord.Intents.all()
    return intents


class MasterBot(commands.Bot):
    def __init__(self) -> None:
        super().__init__(
            command_prefix=commands.when_mentioned_or("!"),  # legacy only; primary is slash
            intents=build_intents(),
        )
        self.boot_utc = datetime.now(timezone.utc)

    async def setup_hook(self) -> None:
        # Packs
        await self.load_extension("plugins.poketwo.poketwo")
        await self.load_extension("plugins.system.github_sync")

        # Sync slash commands
        dev_gid = os.getenv("DEV_GUILD_ID", "").strip()
        try:
            if dev_gid.isdigit():
                await self.tree.sync(guild=discord.Object(id=int(dev_gid)))
                log.info("app_commands: synced (dev guild)")
            else:
                await self.tree.sync()
                log.info("app_commands: synced (global)")
        except Exception:
            log.exception("app_commands: sync failed")

    async def on_ready(self) -> None:
        log.info("boot: version=%s python=%s discord.py=%s", VERSION, sys.version.split()[0], discord.__version__)
        log.info("ready: logged in as %s (%s)", self.user, getattr(self.user, "id", "n/a"))
        log.info("ready: guilds=%s latency_ms=%.0f", len(self.guilds), self.latency * 1000.0)


def main() -> None:
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("BOT_TOKEN missing. Put it in .env (local) or host env variables.")

    # Push immediately on boot (fail hard).
    boot_github_push_fail_hard()

    bot = MasterBot()
    bot.run(token)


if __name__ == "__main__":
    main()
