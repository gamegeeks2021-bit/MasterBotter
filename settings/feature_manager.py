from __future__ import annotations

import importlib
import pkgutil
import sys
from dataclasses import dataclass
from typing import Dict, List, Tuple

from discord.ext import commands

from .registry import SettingsRegistry

FEATURES_PKG = "plugins.settings.features"

@dataclass
class LoadedFeature:
    feature_id: str
    module_name: str  # plugins.settings.features.<id>.pack

class SettingsFeatureManager:
    def __init__(self, bot: commands.Bot, registry: SettingsRegistry) -> None:
        self.bot = bot
        self.registry = registry
        self.loaded: Dict[str, LoadedFeature] = {}

    def _modname(self, feature_id: str) -> str:
        return f"{FEATURES_PKG}.{feature_id}.pack"

    def discover(self) -> list[str]:
        pkg = importlib.import_module(FEATURES_PKG)
        ids: list[str] = []
        for m in pkgutil.iter_modules(pkg.__path__, FEATURES_PKG + "."):
            ids.append(m.name.split(".")[-1])
        return ids

    async def load_all(self) -> List[Tuple[str, str, str]]:
        loaded_meta: List[Tuple[str, str, str]] = []
        for fid in self.discover():
            loaded_meta.append(await self.load(fid))
        return loaded_meta

    async def load(self, feature_id: str) -> Tuple[str, str, str]:
        mod_name = self._modname(feature_id)
        mod = importlib.import_module(mod_name)

        meta = getattr(mod, "PACK_META", {}) or {}
        fid = meta.get("id") or feature_id
        label = meta.get("name") or fid
        version = meta.get("version") or "0.0.0"

        setup = getattr(mod, "setup", None)
        if not callable(setup):
            raise RuntimeError(f"{mod_name} missing async setup(bot, registry)")

        await setup(self.bot, self.registry)
        self.loaded[fid] = LoadedFeature(fid, mod_name)
        return (fid, label, version)

    async def unload(self, feature_id: str) -> None:
        info = self.loaded.get(feature_id)
        mod_name = info.module_name if info else self._modname(feature_id)

        mod = sys.modules.get(mod_name)
        if mod is not None:
            teardown = getattr(mod, "teardown", None)
            if callable(teardown):
                await teardown(self.bot, self.registry)

        self.registry.unregister(feature_id)
        sys.modules.pop(mod_name, None)
        self.loaded.pop(feature_id, None)

    async def reload_all(self) -> List[Tuple[str, str, str]]:
        # Unload everything currently loaded, then re-discover and load.
        for fid in list(self.loaded.keys()):
            await self.unload(fid)
        return await self.load_all()

    async def reload(self, feature_id: str) -> Tuple[str, str, str]:
        await self.unload(feature_id)
        return await self.load(feature_id)
