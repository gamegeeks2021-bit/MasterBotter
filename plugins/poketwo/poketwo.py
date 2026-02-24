from __future__ import annotations

import os
import json
from pathlib import Path
from datetime import datetime, timezone, timedelta

import discord
from discord import app_commands
from discord.ext import commands


STATE_DIR = Path("data")
STATE_DIR.mkdir(exist_ok=True)
STATE_FILE = STATE_DIR / "poketwo_profiles.json"


def _load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _component_v2_hub_embed(build: str, has_profile: bool) -> discord.Embed:
    title = "POKÉTWO HUB"
    desc = "Use the buttons below. Slash-first. No legacy prefix required."
    lines = []
    lines.append("---separator---")
    if not has_profile:
        lines.append("Pick your starter to begin. [Start]")
    else:
        lines.append("View your current status and starter. [Summary]")
    lines.append("---separator---")
    lines.append("Browse items and basics. [Shop]")
    lines.append("---separator---")
    lines.append("View your collection. [My Pokémon]")
    lines.append("---separator---")
    lines.append("Trade (requires a profile). [Trade]")
    embed = discord.Embed(title=title, description=desc + "\n\n" + "\n".join(lines))
    embed.set_footer(text=f"Build {build}")
    return embed


# Minimal starter roster by generation (3 starters each)
STARTERS = {
    "Gen 1": ["Bulbasaur", "Charmander", "Squirtle"],
    "Gen 2": ["Chikorita", "Cyndaquil", "Totodile"],
    "Gen 3": ["Treecko", "Torchic", "Mudkip"],
    "Gen 4": ["Turtwig", "Chimchar", "Piplup"],
    "Gen 5": ["Snivy", "Tepig", "Oshawott"],
    "Gen 6": ["Chespin", "Fennekin", "Froakie"],
    "Gen 7": ["Rowlet", "Litten", "Popplio"],
    "Gen 8": ["Grookey", "Scorbunny", "Sobble"],
    "Gen 9": ["Sprigatito", "Fuecoco", "Quaxly"],
}


class GenSelect(discord.ui.Select):
    def __init__(self, parent: "StarterFlowView"):
        self.parent = parent
        options = [discord.SelectOption(label=k, value=k) for k in STARTERS.keys()]
        super().__init__(placeholder="Choose a generation…", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=False)
        gen = self.values[0]
        self.parent.set_generation(gen)
        await interaction.followup.edit_message(message_id=interaction.message.id, view=self.parent)


class StarterFlowView(discord.ui.View):
    def __init__(self, bot: commands.Bot, user_id: int):
        super().__init__(timeout=900)
        self.bot = bot
        self.user_id = user_id
        self.gen: str | None = None
        self.add_item(GenSelect(self))
        self._render_starter_buttons()

    def set_generation(self, gen: str) -> None:
        self.gen = gen
        # remove existing buttons except the select
        self.clear_items()
        self.add_item(GenSelect(self))
        self._render_starter_buttons()

    def _render_starter_buttons(self) -> None:
        if not self.gen:
            return
        starters = STARTERS[self.gen]
        # row 1 is safe (row 0 contains the select)
        for i, name in enumerate(starters):
            btn = discord.ui.Button(label=name, style=discord.ButtonStyle.success, row=1)
            btn.callback = self._make_pick_cb(name)
            self.add_item(btn)

    def _make_pick_cb(self, starter: str):
        async def _cb(interaction: discord.Interaction):
            await interaction.response.defer(ephemeral=True, thinking=True)
            if interaction.user.id != self.user_id:
                await interaction.followup.send("This selection menu is not for you.", ephemeral=True)
                return

            state = _load_state()
            uid = str(self.user_id)
            now = datetime.now(timezone.utc)

            # store profile
            state[uid] = {
                "starter": starter,
                "picked_at": now.isoformat(),
                "level": 1,
                "leveled_at": None,
            }
            _save_state(state)

            msg = (
                f"Starter selected: **{starter}**.\n\n"
                "You can change your mind **within the same day** or **until your starter levels up**."
            )
            await interaction.followup.send(msg, ephemeral=True)
        return _cb


class PoketwoHubView(discord.ui.View):
    def __init__(self, bot: commands.Bot, user_id: int, has_profile: bool):
        super().__init__(timeout=900)
        self.bot = bot
        self.user_id = user_id
        self.has_profile = has_profile

        if not has_profile:
            self.start_btn = discord.ui.Button(label="Start", style=discord.ButtonStyle.success, row=0)
            self.start_btn.callback = self._start_cb
            self.add_item(self.start_btn)
        else:
            self.summary_btn = discord.ui.Button(label="Summary", style=discord.ButtonStyle.primary, row=0)
            self.summary_btn.callback = self._summary_cb
            self.add_item(self.summary_btn)

        self.shop_btn = discord.ui.Button(label="Shop", style=discord.ButtonStyle.secondary, row=0)
        self.shop_btn.callback = self._shop_cb
        self.add_item(self.shop_btn)

        self.myp_btn = discord.ui.Button(label="My Pokémon", style=discord.ButtonStyle.secondary, row=0)
        self.myp_btn.callback = self._myp_cb
        self.add_item(self.myp_btn)

        self.trade_btn = discord.ui.Button(label="Trade", style=discord.ButtonStyle.secondary, row=0)
        self.trade_btn.callback = self._trade_cb
        self.add_item(self.trade_btn)

    async def _start_cb(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=False)
        v = StarterFlowView(self.bot, interaction.user.id)
        embed = discord.Embed(
            title="STARTER SELECTION",
            description="TITLE\nDescription\n---separator---\nSelect a generation, then choose your starter.",
        )
        await interaction.followup.send(embed=embed, view=v, ephemeral=True)

    async def _summary_cb(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=False)
        state = _load_state()
        p = state.get(str(interaction.user.id))
        if not p:
            await interaction.followup.send("No profile found. Use **/pokemon** → Start.", ephemeral=True)
            return
        starter = p.get("starter", "Unknown")
        level = p.get("level", 1)
        embed = discord.Embed(title="POKÉTWO SUMMARY", description=f"Starter: **{starter}**\nLevel: **{level}**")
        await interaction.followup.send(embed=embed, ephemeral=True)

    async def _shop_cb(self, interaction: discord.Interaction):
        await interaction.response.send_message("Shop is not implemented in this clean rebuild yet.", ephemeral=True)

    async def _myp_cb(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=False)
        state = _load_state()
        p = state.get(str(interaction.user.id))
        if not p:
            await interaction.followup.send("No profile found. Use **/pokemon** → Start.", ephemeral=True)
            return
        await interaction.followup.send(f"Starter: **{p.get('starter','Unknown')}**", ephemeral=True)

    async def _trade_cb(self, interaction: discord.Interaction):
        await interaction.response.send_message("Trade is not implemented in this clean rebuild yet.", ephemeral=True)


class PoketwoCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="pokemon", description="Open the Pokétwo hub (slash-first).")
    async def pokemon(self, interaction: discord.Interaction):
        # Must ACK immediately to avoid Unknown interaction
        await interaction.response.defer(ephemeral=True, thinking=False)

        state = _load_state()
        has_profile = str(interaction.user.id) in state

        embed = _component_v2_hub_embed(os.getenv("BOT_BUILD_ID", "D.x"), has_profile)
        view = PoketwoHubView(self.bot, interaction.user.id, has_profile)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(PoketwoCog(bot))
