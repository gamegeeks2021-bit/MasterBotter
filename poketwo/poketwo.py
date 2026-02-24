from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
import sys
from dataclasses import dataclass
from pathlib import Path
from types import MethodType
from typing import Any, Callable, Coroutine, Optional

import aiohttp
import discord
from dotenv import find_dotenv, load_dotenv
from discord import app_commands
from discord.ext import commands

from .activity import ActivityController

log = logging.getLogger("bot.poketwo")

def _sanitize_redis_url(raw: str) -> str:
    """Accept a normal redis URL, or a copied CLI command like: 'redis-cli -u redis://...'."""
    if not raw:
        return raw
    s = raw.strip()
    # Common mistake: pasting a CLI command instead of the URL
    if s.startswith('redis-cli'):
        # Try to extract the -u URL portion
        parts = s.split()
        if '-u' in parts:
            try:
                s = parts[parts.index('-u') + 1].strip()
            except Exception:
                pass
    # Do NOT auto-upgrade redis:// -> rediss://.
    # Providers may offer both TLS and non-TLS endpoints; using the wrong scheme
    # can cause SSL errors (e.g., WRONG_VERSION_NUMBER).
    return s


def _data_file() -> Path:
    base = Path(__file__).parent
    d = base / "_data"
    d.mkdir(exist_ok=True)
    return d / "state.json"


def _load_state() -> dict[str, Any]:
    p = _data_file()
    if not p.exists():
        return {"enabled": False}
    try:
        obj = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass
    return {"enabled": False}


def _save_state(state: dict[str, Any]) -> None:
    _data_file().write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


@dataclass
class LoadResult:
    ok: bool
    message: str


class PoketwoManager:
    """Loads/unloads the vendored Poketwo cogs into the existing bot instance."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.vendor_root = (Path(__file__).parent / "vendor").resolve()
        self.poketwo_root = (self.vendor_root / "poketwo").resolve()
        self._bootstrapped = False
        self._loaded_modules: list[str] = []

        # Activity controller used by the spawning cog patch.
        self.activity = ActivityController()
        setattr(self.bot, "poketwo_activity", self.activity)

    def is_enabled(self) -> bool:
        return bool(_load_state().get("enabled", False))

    def status_line(self) -> str:
        return "✅ Enabled" if self.is_enabled() else "❌ Disabled"

    def _ensure_vendor_path(self) -> None:
        # vendor_root provides `config` and `data`; poketwo_root provides `cogs` and `helpers`.
        if str(self.vendor_root) not in sys.path:
            sys.path.insert(0, str(self.vendor_root))
        if str(self.poketwo_root) not in sys.path:
            sys.path.insert(0, str(self.poketwo_root))

    def _bootstrap_bot(self) -> None:
        if self._bootstrapped:
            return

        self._ensure_vendor_path()

        # Ensure .env (if present) is loaded before reading Poketwo env vars.
        # This keeps the toggle working even if host-provided env vars are missing.
        try:
            env_path = find_dotenv(usecwd=True)
            if env_path:
                load_dotenv(env_path, override=False)
        except Exception:
            pass

        # Provide basic attributes Poketwo cogs expect.
        if not hasattr(self.bot, "cooldown_users"):
            self.bot.cooldown_users = {}  # type: ignore[attr-defined]
        if not hasattr(self.bot, "cooldown_guilds"):
            self.bot.cooldown_guilds = {}  # type: ignore[attr-defined]
        if not hasattr(self.bot, "guild_counter"):
            self.bot.guild_counter = {}  # type: ignore[attr-defined]

        # Vendor Pokétwo expects a cache for reaction menus/paginators.
        # Missing this causes AttributeError in some commands (e.g., help).
        if not hasattr(self.bot, "menus"):
            try:
                from expiringdict import ExpiringDict
                self.bot.menus = ExpiringDict(max_len=300, max_age_seconds=300)  # type: ignore[attr-defined]
            except Exception:
                self.bot.menus = {}  # type: ignore[attr-defined]

        # Config module is vendored as vendor/config.py
        cfg = __import__("config")

        # ---- Sync config from environment every boot ----
        # The vendored config reads env vars at import time. If the hosting
        # platform injects env vars late (or you update them and restart),
        # cfg.* may remain empty unless we refresh from os.environ.
        cfg.DATABASE_URI = os.getenv("POKETWO_DATABASE_URI", getattr(cfg, "DATABASE_URI", ""))
        cfg.DATABASE_NAME = os.getenv("POKETWO_DATABASE_NAME", getattr(cfg, "DATABASE_NAME", ""))
        # Prefer dedicated Pokétwo redis var; fall back to REDIS_URL if present.
        # This does NOT require changing your env; it simply uses what already exists.
        cfg.REDIS_URL = (
            os.getenv("POKETWO_REDIS_URL", "").strip()
            or os.getenv("REDIS_URL", "").strip()
            or getattr(cfg, "REDIS_URL", "")
        )

        # Keep SSL flag consistent with scheme.
        if isinstance(cfg.REDIS_URL, str) and cfg.REDIS_URL:
            if cfg.REDIS_URL.startswith("rediss://"):
                cfg.REDIS_SSL = True
            elif cfg.REDIS_URL.startswith("redis://"):
                cfg.REDIS_SSL = False

        self.bot.config = cfg  # type: ignore[attr-defined]

        # The upstream Poketwo bot class exposes convenience properties
        # (mongo/redis/data/lang/ipc) that forward to cogs. Our master bot is a
        # plain discord.py Bot, so add these properties dynamically once.
        cls = self.bot.__class__

        # Ensure bot.redis is writable.
        # Vendor cogs.redis assigns `bot.redis = RedisCompat(...)` at runtime.
        # If the bot class has a read-only property (no setter), assignment fails.
        existing_redis = getattr(cls, "redis", None)
        if not isinstance(existing_redis, property) or existing_redis.fset is None:
            def _get_redis(this):  # type: ignore
                if hasattr(this, "_poketwo_redis"):
                    return getattr(this, "_poketwo_redis")
                cog = this.get_cog("Redis")
                return getattr(cog, "pool", None) if cog is not None else None

            def _set_redis(this, value):  # type: ignore
                setattr(this, "_poketwo_redis", value)

            setattr(cls, "redis", property(_get_redis, _set_redis))

        if not hasattr(cls, "mongo"):
            @property
            def mongo(self):  # type: ignore
                return self.get_cog("Mongo")
            cls.mongo = mongo  # type: ignore

        # Some vendor tasks expect bot.shards even if using non-sharded commands.Bot.
        if not hasattr(cls, "shards"):
            @property
            def shards(self):  # type: ignore
                conn = getattr(self, "_connection", None)
                return getattr(conn, "shards", {}) if conn is not None else {}
            cls.shards = shards  # type: ignore
        if not hasattr(cls, "data"):
            @property
            def data(self):  # type: ignore
                return self.get_cog("Data")
            cls.data = data  # type: ignore
        if not hasattr(cls, "lang"):
            @property
            def lang(self):  # type: ignore
                return self.get_cog("Lang")
            cls.lang = lang  # type: ignore
        if not hasattr(cls, "ipc"):
            @property
            def ipc(self):  # type: ignore
                return self.get_cog("IPC")
            cls.ipc = ipc  # type: ignore

        if not hasattr(self.bot, "cluster_idx"):
            self.bot.cluster_idx = getattr(cfg, "CLUSTER_IDX", 0)

        # Several upstream Poketwo cogs expect a shared aiohttp session on the bot.
        # (e.g. shop/weekend checks and other HTTP requests.)
        if not hasattr(self.bot, "http_session") or getattr(self.bot, "http_session", None) is None:
            self.bot.http_session = aiohttp.ClientSession()  # type: ignore[attr-defined]

        # Embed helpers (colors come from vendored helpers.constants)
        helpers = __import__("helpers")
        constants = helpers.constants

        class _PinkEmbed(discord.Embed):
            def __init__(self, **kwargs):
                color = kwargs.pop("color", constants.PINK)
                super().__init__(**kwargs, color=color)

        class _BlueEmbed(discord.Embed):
            def __init__(self, **kwargs):
                color = kwargs.pop("color", constants.BLUE)
                super().__init__(**kwargs, color=color)

        self.bot.Embed = _PinkEmbed  # type: ignore[attr-defined]
        self.bot.BlueEmbed = _BlueEmbed  # type: ignore[attr-defined]

        # Localization helpers
        async def _get_context(self: commands.Bot, message: discord.Message, *, cls=None):
            PoketwoContext = helpers.context.PoketwoContext
            return await commands.Bot.get_context(self, message, cls=PoketwoContext)

        self.bot.get_context = MethodType(_get_context, self.bot)  # type: ignore[assignment]

        def _(self: commands.Bot, message_id: str, **kwargs: Any) -> str:
            lang_cog = self.get_cog("Lang")
            if lang_cog is None:
                return message_id
            return lang_cog.fluent.format_value(message_id, kwargs)

        self.bot._ = MethodType(_, self.bot)  # type: ignore[attr-defined]

        # Minimal i18n embed helper expected by Pokétwo vendor cogs
        def _localized_embed(self, message_id: str, **kwargs):
            t = getattr(self, "_", lambda k, **kw: k)
            title = t(f"{message_id}.title", **kwargs)
            desc = t(f"{message_id}.description", **kwargs)
            embed = discord.Embed(
                title=None if title.startswith(message_id) else title,
                description=None if desc.startswith(message_id) else desc,
            )
            footer = t(f"{message_id}.footer", **kwargs)
            if not footer.startswith(message_id):
                embed.set_footer(text=footer)
            return embed

        self.bot.localized_embed = MethodType(_localized_embed, self.bot)  # type: ignore[attr-defined]

        self._bootstrapped = True

    async def enable(self) -> LoadResult:
        self._bootstrap_bot()

        # Upstream Poketwo expects cluster attributes on the bot.
        # This integration runs as a single process => cluster 0.
        if not hasattr(self.bot, "cluster_idx"):
            setattr(self.bot, "cluster_idx", 0)
        if not hasattr(self.bot, "cluster_name"):
            setattr(self.bot, "cluster_name", str(getattr(self.bot, "cluster_idx", 0)))

        # Validate env config.
        cfg = self.bot.config  # type: ignore[attr-defined]
        # Normalize common copy/paste mistakes (e.g. 'redis-cli -u redis://...')
        if hasattr(cfg, 'REDIS_URL'):
            cfg.REDIS_URL = _sanitize_redis_url(getattr(cfg, 'REDIS_URL', ''))
        if getattr(cfg, 'REDIS_URL', '') and not (str(cfg.REDIS_URL).startswith('redis://') or str(cfg.REDIS_URL).startswith('rediss://') or str(cfg.REDIS_URL).startswith('unix://')):
            return LoadResult(False, 'REDIS_URL must be a redis URL (redis://, rediss://, or unix://).')
        if not getattr(cfg, "DATABASE_URI", ""):
            return LoadResult(False, "Missing POKETWO_DATABASE_URI")
        if not getattr(cfg, "REDIS_URL", ""):
            return LoadResult(False, "Missing POKETWO_REDIS_URL (or REDIS_URL)")

        # Load all Poketwo cogs.
        try:
            cogs = __import__("cogs")
            to_load = [n for n in list(getattr(cogs, "default", ())) if n != "pride_2023"]
            # Ensure core services are loaded before feature cogs.
            core = [n for n in ("config", "mongo", "redis", "data") if n in to_load]
            rest = [n for n in to_load if n not in set(core)]
            to_load = core + rest
        except Exception as e:
            return LoadResult(False, f"Failed to import Poketwo cogs: {type(e).__name__}")

        loaded = []
        for name in to_load:
            mod = f"cogs.{name}"
            if mod in self._loaded_modules:
                continue
            try:
                await self.bot.load_extension(mod)
                loaded.append(mod)
                self._loaded_modules.append(mod)
            except commands.ExtensionAlreadyLoaded:
                continue
            except Exception as e:
                log.exception("poketwo: failed loading %s", mod)
                # best-effort rollback of what we loaded in this enable()
                for m in reversed(loaded):
                    try:
                        await self.bot.unload_extension(m)
                    except Exception:
                        pass
                    if m in self._loaded_modules:
                        self._loaded_modules.remove(m)
                return LoadResult(False, f"Failed loading {mod}: {type(e).__name__}")

        st = _load_state()
        st["enabled"] = True
        _save_state(st)

        # ---- UX patch ----
        # If the vendor help command renders raw localization keys (e.g. "bot-help-embed.title"),
        # provide a clean, Component-v2-friendly fallback help.
        self._install_help_fallback()

        return LoadResult(True, f"Enabled ({len(loaded)} modules)")

    def _install_help_fallback(self) -> None:
        """Replace vendor `help` text command with a stable fallback.

        Vendor help relies on Fluent locale bundles. If locale files aren't present or
        aren't loading correctly in this integration, the output becomes raw message IDs.
        The fallback keeps gameplay usable while slash/buttons become the primary interface.
        """
        try:
            existing = self.bot.get_command("help")
            if existing is not None:
                self.bot.remove_command("help")

            @commands.command(name="help")
            async def _poketwo_help(ctx: commands.Context):
                embed = discord.Embed(
                    title="Pokétwo help",
                    description=(
                        "Use **/pokemon** for the hub (buttons + quick actions).\n"
                        "Prefix gameplay is also available using **!p**.\n\n"
                        "**Common commands**\n"
                        "• `!p start` — create your profile and pick a starter\n"
                        "• `!p catch` — catch the current spawn\n"
                        "• `!p pokemon` — view your Pokémon list\n"
                        "• `!p shop` — open the shop\n"
                        "• `!p hint` — show a hint for the spawn\n"
                        "• `!p market` — browse the market\n\n"
                        "Tip: use the hub’s dropdown to jump to a command."
                    ),
                )
                await ctx.send(embed=embed)

            # Register as a normal prefix command.
            self.bot.add_command(_poketwo_help)
        except Exception:
            # Never break enable() if help patch fails.
            return

    async def disable(self) -> LoadResult:
        # Unload in reverse order.
        for mod in list(reversed(self._loaded_modules)):
            try:
                await self.bot.unload_extension(mod)
            except Exception:
                pass
        self._loaded_modules.clear()

        st = _load_state()
        st["enabled"] = False
        _save_state(st)
        return LoadResult(True, "Disabled")

    async def refresh(self) -> LoadResult:
        if self.is_enabled():
            await self.disable()
            return await self.enable()
        return LoadResult(True, "Not enabled")


class _VoiceActivityCog(commands.Cog):
    def __init__(self, bot: commands.Bot, mgr: PoketwoManager):
        self.bot = bot
        self.mgr = mgr

    def _recount(self, guild: discord.Guild) -> int:
        count = 0
        for vc in guild.voice_channels:
            for m in vc.members:
                if not m.bot:
                    count += 1
        return count

    @commands.Cog.listener()
    async def on_ready(self):
        for g in self.bot.guilds:
            self.mgr.activity.set_voice_count(g.id, self._recount(g))

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        if member.bot or member.guild is None:
            return
        self.mgr.activity.set_voice_count(member.guild.id, self._recount(member.guild))


class PoketwoView(discord.ui.View):
    def __init__(self, mgr: PoketwoManager):
        super().__init__(timeout=900)
        self.mgr = mgr
        self._render()

    def _render(self):
        self.clear_items()

        self.add_item(discord.ui.Button(label="Refresh", style=discord.ButtonStyle.success, custom_id="poketwo:refresh"))
        is_on = self.mgr.is_enabled()
        label = "Toggle [ON]" if is_on else "Toggle [OFF]"
        style = discord.ButtonStyle.success if is_on else discord.ButtonStyle.danger
        self.add_item(discord.ui.Button(label=label, style=style, custom_id="poketwo:toggle"))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # Only allow users who can manage guild to toggle.
        if interaction.user is None or interaction.guild is None:
            return False
        if isinstance(interaction.user, discord.Member):
            return interaction.user.guild_permissions.manage_guild
        return False

    @discord.ui.button(label="_", style=discord.ButtonStyle.secondary, disabled=True)
    async def _dummy(self, interaction: discord.Interaction, button: discord.ui.Button):
        pass


class PoketwoHubView(discord.ui.View):
    def __init__(
        self,
        bot: commands.Bot,
        mgr: PoketwoManager,
        *,
        is_admin: bool,
        has_profile: bool,
    ):
        super().__init__(timeout=900)
        self.bot = bot
        self.mgr = mgr
        self.is_admin = is_admin
        self.has_profile = has_profile

        # Onboarding button becomes "Summary" after a profile exists.
        if self.has_profile:
            self.start_btn = discord.ui.Button(label="Summary", style=discord.ButtonStyle.primary)
        else:
            self.start_btn = discord.ui.Button(label="Start", style=discord.ButtonStyle.success)
        self.start_btn.callback = self._start_cb  # type: ignore
        self.add_item(self.start_btn)

        self.commands_btn = discord.ui.Button(label="Commands", style=discord.ButtonStyle.secondary)
        self.commands_btn.callback = self._commands_cb  # type: ignore
        self.add_item(self.commands_btn)

        self.help_btn = discord.ui.Button(label="Help", style=discord.ButtonStyle.primary)
        self.help_btn.callback = self._help_cb  # type: ignore
        self.add_item(self.help_btn)

        # Search as a dropdown (short descriptions).
        self.search_select = discord.ui.Select(
            placeholder="Search commands…",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(label="Catch", value="catch", description="Catch the current spawn"),
                discord.SelectOption(label="Pokemon", value="pokemon", description="View your Pokémon list"),
                discord.SelectOption(label="Shop", value="shop", description="Open the shop"),
                discord.SelectOption(label="Trade", value="trade", description="Trade with another user"),
                discord.SelectOption(label="Market", value="market", description="Browse/list market offers"),
                discord.SelectOption(label="Incense", value="incense", description="Use incense (spawns)"),
            ],
        )
        self.search_select.callback = self._search_cb  # type: ignore
        self.add_item(self.search_select)

        # Admin-only: settings button (Discord Administrator permission).
        self.admin_btn = discord.ui.Button(label="Admin Settings", style=discord.ButtonStyle.danger)
        self.admin_btn.callback = self._admin_cb  # type: ignore
        self.add_item(self.admin_btn)

    async def _invoke_text_command(self, interaction: discord.Interaction, name: str, *args: str) -> None:
        """Invoke a Pokétwo *prefix* command from a UI button.

        Button interactions are not tied to an app-command invocation, so
        `Context.from_interaction()` raises `ValueError('interaction does not have command data')`.

        We synthesize a minimal discord.Message as if the clicking user typed
        the command, then run the normal command parsing/invocation pipeline.
        """
        if interaction.channel is None:
            return

        # Acknowledge quickly to avoid "The application did not respond" on button clicks.
        # The invoked Pokétwo command itself will send its normal (non-ephemeral) output.
        try:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True, thinking=False)
        except Exception:
            pass

        # Ensure Pokétwo is enabled so its cogs/commands exist.
        mgr = getattr(self.bot, "poketwo_manager", None)
        if mgr is not None:
            st = _load_state()
            if not st.get("enabled", False):
                res = await mgr.enable()
                if not res.ok:
                    try:
                        await interaction.followup.send(f"Pokétwo failed to enable: {res.message}", ephemeral=True)
                    except Exception:
                        pass
                    return

        if self.bot.get_command(name) is None:
            try:
                await interaction.followup.send(
                    f"Pokétwo command `{name}` is not available on this build.",
                    ephemeral=True,
                )
            except Exception:
                pass
            return

        content = "!p " + name
        if args:
            content += " " + " ".join(args)

        state = self.bot._connection  # type: ignore[attr-defined]
        now = datetime.now(timezone.utc)
        data = {
            "id": str(interaction.id),
            "content": content,
            "timestamp": now.isoformat(),
            "edited_timestamp": None,
            "tts": False,
            "mention_everyone": False,
            "mentions": [],
            "mention_roles": [],
            "attachments": [],
            "embeds": [],
            "pinned": False,
            "type": 0,
            "author": {
                "id": str(interaction.user.id),
                "username": interaction.user.name,
                "discriminator": interaction.user.discriminator,
                "avatar": interaction.user.avatar.key if interaction.user.avatar else None,
                "bot": False,
            },
            "channel_id": str(interaction.channel.id),
        }
        if interaction.guild is not None:
            data["guild_id"] = str(interaction.guild.id)

        msg = discord.Message(state=state, channel=interaction.channel, data=data)  # type: ignore
        ctx = await self.bot.get_context(msg)  # type: ignore[arg-type]
        try:
            await self.bot.invoke(ctx)
        except Exception as e:
            try:
                await interaction.followup.send(f"Command failed: {type(e).__name__}: {e}", ephemeral=True)
            except Exception:
                pass

    async def _start_cb(self, interaction: discord.Interaction):
        """Onboarding / summary.

        - If the user has no profile: show starter picker and run `!p pick <name>`.
        - If the user already started: show a short summary (profile).
        """

        # Ensure Pokétwo is enabled so Mongo/Redis cogs exist.
        mgr = getattr(self.bot, "poketwo_manager", None)
        if mgr is not None and not mgr.is_enabled():
            res = await mgr.enable()
            if not res.ok:
                try:
                    await interaction.response.send_message(f"Pokétwo failed to enable: {res.message}", ephemeral=True)
                except Exception:
                    pass
                return

        # If profile exists, treat this as Summary.
        if self.has_profile:
            await self._invoke_text_command(interaction, "profile")
            return

        # New user onboarding: show starters and run `pick`.
        starters_by_gen = [
            ("Generation I (Kanto)", ["Bulbasaur", "Charmander", "Squirtle"]),
            ("Generation II (Johto)", ["Chikorita", "Cyndaquil", "Totodile"]),
            ("Generation III (Hoenn)", ["Treecko", "Torchic", "Mudkip"]),
            ("Generation IV (Sinnoh)", ["Turtwig", "Chimchar", "Piplup"]),
            ("Generation V (Unova)", ["Snivy", "Tepig", "Oshawott"]),
            ("Generation VI (Kalos)", ["Chespin", "Fennekin", "Froakie"]),
            ("Generation VII (Alola)", ["Rowlet", "Litten", "Popplio"]),
            ("Generation VIII (Galar)", ["Grookey", "Scorbunny", "Sobble"]),
            ("Generation IX (Paldea)", ["Sprigatito", "Fuecoco", "Quaxly"]),
        ]

        embed = discord.Embed(
            title="Choose your starter",
            description=(
                "Pick one starter to create your profile.\n"
                "---\n"
                "Each generation has three buttons below.\n"
                "---\n"
                "After you pick, you can change your mind **until the end of today** "
                "or **until your starter levels up** (whichever happens first).\n"
            ),
        )

        for gen, mons in starters_by_gen:
            embed.add_field(
                name=gen,
                value=f"Select one: **{mons[0]}**, **{mons[1]}**, **{mons[2]}**",
                inline=False,
            )

        class _StarterView(discord.ui.View):
            def __init__(self, parent: "PoketwoHubView"):
                super().__init__(timeout=900)
                self.parent = parent

                # One row per generation, 3 buttons each (Discord allows up to 5 per row).
                for row_idx, (gen, mons) in enumerate(starters_by_gen):
                    for mon in mons:
                        btn = discord.ui.Button(
                            label=mon,
                            style=discord.ButtonStyle.secondary,
                            row=row_idx,
                            custom_id=f"poketwo:starter:{mon.lower()}",
                        )

                        async def _cb(i: discord.Interaction, mon_name: str = mon):
                            # Run vendor flow (includes ToS confirmation).
                            await self.parent._invoke_text_command(i, "pick", mon_name)

                            # Notify about the change window (informational; enforcement is separate).
                            try:
                                await i.followup.send(
                                    "Starter selected. You can change your starter **until the end of today** "
                                    "or **until your starter levels up**.",
                                    ephemeral=True,
                                )
                            except Exception:
                                pass

                            # Flip hub to summary state for subsequent clicks.
                            self.parent.has_profile = True
                            try:
                                self.parent.start_btn.label = "Summary"
                                self.parent.start_btn.style = discord.ButtonStyle.primary
                                await i.message.edit(view=self.parent)
                            except Exception:
                                pass

                        btn.callback = _cb  # type: ignore
                        self.add_item(btn)

        v = _StarterView(self)
        await interaction.response.send_message(embed=embed, view=v, ephemeral=True)

    async def _help_cb(self, interaction: discord.Interaction):
        emb = discord.Embed(
            title="Pokétwo gameplay",
            description=(
                """**Full interface:** use prefix commands with `!p` (example: `!p start`).
"""
                """**Buttons:** this `/pokemon` menu runs common actions for you.

"""
                """If Pokémon images are missing, upload the Pokétwo `data/` assets pack."""
            ),
        )
        emb.add_field(
            name="Common commands",
            value=(
                """• `!p start`
• `!p help`
• `!p pokemon`
• `!p inventory` / `!p bag`
• `!p shop`
• `!p trade`
• `!p profile`"""
            ),
            inline=False,
        )
        await interaction.response.send_message(embed=emb, ephemeral=True)

    async def _commands_cb(self, interaction: discord.Interaction):
        await self._invoke_text_command(interaction, "help")

    async def _search_cb(self, interaction: discord.Interaction):
        key = None
        if hasattr(self, "search_select") and getattr(self.search_select, "values", None):
            key = self.search_select.values[0]
        if not key:
            await interaction.response.send_message("Pick a keyword from the dropdown.", ephemeral=True)
            return
        await interaction.response.send_message(
            f"Example: `!p {key}`\nTip: full gameplay uses `!p …`.",
            ephemeral=True,
        )

    async def _admin_cb(self, interaction: discord.Interaction):
        is_admin = bool(
            getattr(getattr(interaction, "user", None), "guild_permissions", None)
            and interaction.user.guild_permissions.administrator
        )
        if not is_admin:
            await interaction.response.send_message(
                "Admin Settings requires Discord Administrator permission.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            "Admin Settings UI is not implemented yet in this build.\n"
            "For now, use Pokétwo admin commands via `!p` (example: `!p help`).",
            ephemeral=True,
        )


class PoketwoHub(commands.Cog):
    def __init__(self, bot: commands.Bot, mgr: PoketwoManager):
        self.bot = bot
        self.mgr = mgr

    @app_commands.command(name="pokemon", description="Open the Pokétwo Hub UI")
    async def pokemon(self, interaction: discord.Interaction):
        # Ensure Pokétwo is enabled so Mongo/Redis exist; hub depends on them.
        if not self.mgr.is_enabled():
            res = await self.mgr.enable()
            if not res.ok:
                await interaction.response.send_message(
                    f"Pokétwo failed to enable: {res.message}",
                    ephemeral=True,
                )
                return

        has_profile = False
        try:
            mongo = self.bot.get_cog("Mongo")
            if mongo is not None and interaction.user is not None:
                # Member exists after `!p pick` inserts into db.member.
                member_doc = await mongo.fetch_member_info(interaction.user)  # type: ignore[arg-type]
                has_profile = member_doc is not None
        except Exception:
            has_profile = False

        is_admin = bool(
            getattr(getattr(interaction, "user", None), "guild_permissions", None)
            and interaction.user.guild_permissions.administrator
        )

        embed = discord.Embed(
            title="Pokétwo Hub",
            description=(
                "Onboard and run common actions here.\n"
                "Full gameplay uses `!p …` (example: `!p catch`).\n"
                "---\n"
                f"{'Create your profile and choose a starter.' if not has_profile else 'View your profile and next steps.'} "
                f"**[{ 'Start' if not has_profile else 'Summary' }]**\n"
                "---\n"
                "Show common commands + examples. **[Commands]**\n"
                "---\n"
                "Explain the gameplay loop + next steps. **[Help]**\n"
                "---\n"
                "Use the dropdown below to jump to a keyword. **[Search]**"
            ),
        )
        if is_admin:
            embed.description += "\n---\nVisible to Discord Administrators only. **[Admin Settings]**"

        view = PoketwoHubView(self.bot, self.mgr, is_admin=is_admin, has_profile=has_profile)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    # Alias kept to prevent stale command pickers from throwing CommandNotFound.
    # (Discord clients can cache removed commands for a while.)
    @app_commands.command(name="poketwo", description="(Deprecated) Open the Pokétwo Hub UI")
    async def poketwo_alias(self, interaction: discord.Interaction):
        await self.pokemon(interaction)

    # Common convenience slash commands (wrappers around the real text commands).
    # These are intentionally thin: they invoke the vendored command as-if the user typed "!p <cmd>".
    async def _invoke_text(self, interaction: discord.Interaction, cmd: str, *args: str) -> None:
        if interaction.channel is None:
            return
        try:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True, thinking=False)
        except Exception:
            pass

        # Ensure enabled.
        st = _load_state()
        if not st.get("enabled", False):
            res = await self.mgr.enable()
            if not res.ok:
                try:
                    await interaction.followup.send(f"Pokétwo failed to enable: {res.message}", ephemeral=True)
                except Exception:
                    pass
                return

        # Synthesize a message for get_context().
        class _SyntheticMessage:
            __slots__ = ("content", "author", "channel", "guild", "id", "created_at")
            def __init__(self, *, content, author, channel, guild):
                self.content = content
                self.author = author
                self.channel = channel
                self.guild = guild
                self.id = 0
                self.created_at = discord.utils.utcnow()

        arg_str = (" " + " ".join(args)) if args else ""
        fake = _SyntheticMessage(
            content=f"!p {cmd}{arg_str}",
            author=interaction.user,
            channel=interaction.channel,
            guild=interaction.guild,
        )
        ctx = await self.bot.get_context(fake)  # type: ignore[arg-type]
        if ctx.command is None:
            try:
                await interaction.followup.send(f"Pokétwo command `{cmd}` is not available on this build.", ephemeral=True)
            except Exception:
                pass
            return
        try:
            await self.bot.invoke(ctx)
        except Exception as e:
            try:
                await interaction.followup.send(f"Command failed: {type(e).__name__}: {e}", ephemeral=True)
            except Exception:
                pass

    @app_commands.command(name="shop", description="Open the Pokétwo shop")
    async def shop(self, interaction: discord.Interaction):
        await self._invoke_text(interaction, "shop")

    @app_commands.command(name="trade", description="Trade with another user")
    async def trade(self, interaction: discord.Interaction, user: discord.Member | None = None):
        if user is None:
            await self._invoke_text(interaction, "trade")
        else:
            await self._invoke_text(interaction, "trade", str(user.id))

    @app_commands.command(name="profile", description="Show your Pokétwo profile")
    async def profile(self, interaction: discord.Interaction, user: discord.Member | None = None):
        if user is None:
            await self._invoke_text(interaction, "profile")
        else:
            await self._invoke_text(interaction, "profile", str(user.id))

    @app_commands.command(name="mypokemon", description="Show your Pokémon")
    async def mypokemon(self, interaction: discord.Interaction):
        await self._invoke_text(interaction, "pokemon")


async def setup(bot: commands.Bot) -> None:

    mgr = getattr(bot, "poketwo_manager", None)
    if mgr is None:
        mgr = PoketwoManager(bot)
        bot.poketwo_manager = mgr  # type: ignore[attr-defined]

    # discord.py 2.6 (and most versions) do not accept a `name=` kwarg for add_cog.
    # Cog name defaults to Cog.qualified_name, which for this class is "_VoiceActivityCog".
    if bot.get_cog("_VoiceActivityCog") is None:
        await bot.add_cog(_VoiceActivityCog(bot, mgr))

    if bot.get_cog("PoketwoHub") is None:
        await bot.add_cog(PoketwoHub(bot, mgr))

    # Auto-enable if state says enabled.
    if mgr.is_enabled():
        res = await mgr.enable()
        log.info("poketwo: auto-enable -> %s", res.message)
