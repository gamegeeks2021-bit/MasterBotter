from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import discord

from plugins.settings.registry import SettingFeature

PACK_META = {
    "id": "exp",
    "name": "Experience",
    "version": "0.1.2",
    "description": "Enable or disable the EXP system for this server.",
    "category": "Progression",
    "category_description": "Leveling, EXP, ranks, and progression systems.",
}

_FEATURE_DIR = Path(__file__).resolve().parent
_DATA_DIR = _FEATURE_DIR / "_data"
_CONFIG_FILE = _DATA_DIR / "config.json"

_DEFAULT = {"enabled": True}


def _load() -> Dict[str, Any]:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not _CONFIG_FILE.exists():
        _CONFIG_FILE.write_text(json.dumps(_DEFAULT, indent=2), encoding="utf-8")
        return dict(_DEFAULT)
    return json.loads(_CONFIG_FILE.read_text(encoding="utf-8"))


def _save(cfg: Dict[str, Any]) -> None:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    _CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def ui_status() -> str:
    cfg = _load()
    return "✅ Enabled" if bool(cfg.get("enabled", True)) else "❌ Disabled"


async def _handler(interaction: discord.Interaction, payload: Dict[str, Any]) -> None:
    cfg = _load()
    cfg["enabled"] = not bool(cfg.get("enabled", True))
    _save(cfg)


async def setup(bot, settings_registry) -> None:
    settings_registry.register(
        SettingFeature(
            feature_id=PACK_META["id"],
            label=PACK_META["name"],
            description=PACK_META["description"],
            category=PACK_META.get("category", "General"),
            category_description=PACK_META.get("category_description", "No description provided."),
            handler=_handler,
            status=ui_status,
        )
    )


async def teardown(bot, settings_registry) -> None:
    settings_registry.unregister(PACK_META["id"])
