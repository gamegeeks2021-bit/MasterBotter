from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands


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

SECRET_PATTERNS = [
    re.compile(r"BOT_TOKEN\s*=", re.I),
    re.compile(r"mongodb\+srv://", re.I),
    re.compile(r"redis(s)?://", re.I),
    re.compile(r"github_pat_", re.I),
    re.compile(r"ghp_", re.I),
]


def _run(cmd: list[str], cwd: Path, *, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=str(cwd), text=True, capture_output=True, check=check)


def _matches_glob(name: str, pattern: str) -> bool:
    from fnmatch import fnmatch
    return fnmatch(name, pattern)


def _is_excluded(rel_posix: str) -> bool:
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
        if _matches_glob(name, pat):
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


class GitHubSync(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    github = app_commands.Group(name="github", description="GitHub automation")

    @github.command(name="sync", description="Push code-only snapshot to GitHub (admin only).")
    async def sync(self, interaction: discord.Interaction):
        if not interaction.guild or not interaction.user.guild_permissions.administrator:  # type: ignore[attr-defined]
            await interaction.response.send_message("Admin only.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        repo = os.getenv("GITHUB_REPO", "").strip()
        branch = os.getenv("GITHUB_BRANCH", "main").strip()
        token = os.getenv("GITHUB_TOKEN", "").strip()

        if not repo or not token:
            await interaction.followup.send("Missing GITHUB_REPO or GITHUB_TOKEN in your real .env.", ephemeral=True)
            return

        src_root = Path.cwd().resolve()

        with tempfile.TemporaryDirectory(prefix="codeonly_") as td:
            snap = Path(td) / "repo"
            snap.mkdir(parents=True, exist_ok=True)

            _copy_code_only(src_root, snap)

            hits = _scan_for_secrets(snap)
            if hits:
                msg = "Blocked: potential secrets detected:\n" + "\n".join(hits[:20])
                if len(hits) > 20:
                    msg += f"\n...and {len(hits) - 20} more"
                await interaction.followup.send(msg, ephemeral=True)
                return

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
                await interaction.followup.send("No changes to sync (snapshot identical).", ephemeral=True)
                return

            build = os.getenv("BOT_BUILD_ID", "").strip()
            msg = "Sync code snapshot"
            if build:
                msg += f" (build {build})"
            _run(["git", "commit", "-m", msg], cwd=snap)

            push = _run(["git", "push", "--force", "origin", branch], cwd=snap, check=False)
            if push.returncode != 0:
                await interaction.followup.send(
                    f"Push failed:\nSTDOUT:\n{push.stdout[-1500:]}\nSTDERR:\n{push.stderr[-1500:]}",
                    ephemeral=True,
                )
                return

            commit = _run(["git", "rev-parse", "HEAD"], cwd=snap).stdout.strip()
            await interaction.followup.send(f"Synced.\nBranch: `{branch}`\nCommit: `{commit}`", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(GitHubSync(bot))
