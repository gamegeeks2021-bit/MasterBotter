from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass
class ActivityConfig:
    # If any message is sent within this window, consider the guild "active".
    text_active_window_s: int = 120
    # If >= this many non-bot users are connected to voice, consider the guild "active".
    voice_active_min_users: int = 2
    # Spawn threshold multiplier when active (smaller => more spawns).
    active_threshold_multiplier: float = 0.5
    # Minimum threshold to avoid spam.
    min_threshold: int = 6


class ActivityController:
    """Tracks guild activity to dynamically increase spawn rate when active."""

    def __init__(self, *, cfg: ActivityConfig | None = None):
        self.cfg = cfg or ActivityConfig()
        self._last_text: dict[int, float] = {}
        self._voice_counts: dict[int, int] = {}

    def note_text(self, guild_id: int) -> None:
        self._last_text[guild_id] = time.time()

    def set_voice_count(self, guild_id: int, count: int) -> None:
        self._voice_counts[guild_id] = max(0, int(count))

    def is_active(self, guild_id: int) -> bool:
        now = time.time()
        last = self._last_text.get(guild_id, 0.0)
        text_active = (now - last) <= self.cfg.text_active_window_s
        voice_active = self._voice_counts.get(guild_id, 0) >= self.cfg.voice_active_min_users
        return bool(text_active or voice_active)

    def spawn_threshold(self, guild_id: int, base_threshold: int) -> int:
        if not self.is_active(guild_id):
            return int(base_threshold)
        thr = int(round(base_threshold * self.cfg.active_threshold_multiplier))
        return max(self.cfg.min_threshold, thr)
