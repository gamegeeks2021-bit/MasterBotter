from __future__ import annotations

from typing import Any, Dict

import discord
from discord.ext import commands

from plugins.settings.registry import SettingsRegistry, SettingFeature, FeatureAction


PACK_META = {
    "id": "poketwo",
    "name": "poketwo",
    "version": "1.0.0",
    "description": "Poketwo gameplay (integrated).",
    "category": "Games",
    "category_description": "Game systems integrated into this bot.",
}


async def setup(bot: commands.Bot, registry: SettingsRegistry) -> None:
    # Ensure poketwo hub is loaded (it creates bot.poketwo_manager).
    if not bot.extensions.get("plugins.poketwo.poketwo"):
        await bot.load_extension("plugins.poketwo.poketwo")

    def status() -> str:
        mgr = getattr(bot, "poketwo_manager", None)
        if mgr is None:
            return "❌ Disabled"
        return mgr.status_line()

    async def handler(interaction: discord.Interaction, ctx: Dict[str, Any]) -> dict | None:
        mgr = getattr(bot, "poketwo_manager", None)
        if mgr is None:
            return {"op": "respond", "payload": {"content": "Poketwo manager not loaded.", "ephemeral": True}}

        action = (ctx.get("action") or "toggle").lower().strip()

        if action == "toggle":
            res = await (mgr.disable() if mgr.is_enabled() else mgr.enable())
            if not res.ok:
                return {"op": "respond", "payload": {"content": res.message, "ephemeral": True}}
            return None

        if action == "refresh":
            res = await mgr.refresh()
            return {"op": "respond", "payload": {"content": res.message, "ephemeral": True}}

        return None

    # Register into settings.
    registry.register(
        SettingFeature(
            feature_id=PACK_META["id"],
            label="Poketwo",
            description=PACK_META["description"],
            category=PACK_META["category"],
            category_description=PACK_META["category_description"],
            handler=handler,
            status=status,
            actions=[
                FeatureAction("refresh", "Refresh", style="success", row=1),
            ],
        )
    )
