from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Tuple

import discord
from discord.ext import commands

from plugins.settings.registry import SettingsRegistry, SettingFeature, FeatureAction


PACK_META = {
    "id": "voice_announcements",
    "name": "voice_announcements",
    "version": "0.0.7",
    "description": "Voice channel join/leave embed announcements.",
    "category": "Events",
    "category_description": "Event notifications and announcements.",
}

CATEGORY = "Events"
CATEGORY_DESCRIPTION = (
    "Voice-related event tools for join/leave activity. "
    "Configure announcement templates for voice channel joins and leaves."
)

DEFAULT_STATE: Dict[str, Any] = {
    "enabled": True,
    "join_title": "",
    "join_body": "{user_mention} joined the voice channel.",
    "leave_title": "",
    "leave_body": "{user_mention} left the voice channel.",
}

def _data_file() -> Path:
    base = Path(__file__).parent
    data_dir = base / "_data"
    data_dir.mkdir(exist_ok=True)
    return data_dir / "state.json"

def load_state() -> Dict[str, Any]:
    path = _data_file()
    if not path.exists():
        return DEFAULT_STATE.copy()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}
    merged = DEFAULT_STATE.copy()
    merged.update(data)
    return merged

def save_state(state: Dict[str, Any]) -> None:
    _data_file().write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

def _render_text(tpl: str, member: discord.Member, *, for_title: bool) -> str:
    if tpl is None:
        tpl = ""
    if for_title:
        # Mention tokens in embed title can show raw <@id>, so render as @DisplayName there.
        safe_name = member.display_name.replace("@", "")
        return tpl.replace("{user_mention}", f"@{safe_name}")
    # Clickable mention in body (description)
    return tpl.replace("{user_mention}", member.mention)

def _get_templates(kind: str, state: Dict[str, Any]) -> Tuple[str, str]:
    if kind == "join":
        return (str(state.get("join_title", "")), str(state.get("join_body", "")))
    return (str(state.get("leave_title", "")), str(state.get("leave_body", "")))

def _normalize_blank(s: str) -> str:
    return (s or "").strip()

def _validate_non_empty(title: str, body: str) -> bool:
    return bool(_normalize_blank(title) or _normalize_blank(body))

class EditJoinModal(discord.ui.Modal):
    def __init__(self, state: Dict[str, Any]):
        super().__init__(title="Edit Join Template", timeout=300)
        self.title_in = discord.ui.TextInput(
            label="Title (optional)",
            style=discord.TextStyle.short,
            default=str(state.get("join_title", "")),
            required=False,
            max_length=256,
        )
        self.body_in = discord.ui.TextInput(
            label="Body (optional, supports {user_mention})",
            style=discord.TextStyle.paragraph,
            default=str(state.get("join_body", "")),
            required=False,
            max_length=1900,
        )
        self.add_item(self.title_in)
        self.add_item(self.body_in)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        title = str(self.title_in.value or "")
        body = str(self.body_in.value or "")
        if not _validate_non_empty(title, body):
            await interaction.response.send_message("You must provide a title or body (or both).", ephemeral=True)
            return
        s = load_state()
        s["join_title"] = title
        s["join_body"] = body
        save_state(s)
        await interaction.response.send_message("Saved.", ephemeral=True)

class EditLeaveModal(discord.ui.Modal):
    def __init__(self, state: Dict[str, Any]):
        super().__init__(title="Edit Leave Template", timeout=300)
        self.title_in = discord.ui.TextInput(
            label="Title (optional)",
            style=discord.TextStyle.short,
            default=str(state.get("leave_title", "")),
            required=False,
            max_length=256,
        )
        self.body_in = discord.ui.TextInput(
            label="Body (optional, supports {user_mention})",
            style=discord.TextStyle.paragraph,
            default=str(state.get("leave_body", "")),
            required=False,
            max_length=1900,
        )
        self.add_item(self.title_in)
        self.add_item(self.body_in)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        title = str(self.title_in.value or "")
        body = str(self.body_in.value or "")
        if not _validate_non_empty(title, body):
            await interaction.response.send_message("You must provide a title or body (or both).", ephemeral=True)
            return
        s = load_state()
        s["leave_title"] = title
        s["leave_body"] = body
        save_state(s)
        await interaction.response.send_message("Saved.", ephemeral=True)

class VoiceAnnouncementsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        if member.bot:
            return

        state = load_state()
        if not state.get("enabled", True):
            return

        if before.channel is None and after.channel is not None:
            channel = after.channel
            kind = "join"
            color = discord.Color.green()
        elif before.channel is not None and after.channel is None:
            channel = before.channel
            kind = "leave"
            color = discord.Color.red()
        else:
            return

        if channel is None:
            return

        title_tpl, body_tpl = _get_templates(kind, state)
        if not _validate_non_empty(title_tpl, body_tpl):
            return

        title = _render_text(title_tpl, member, for_title=True).strip() or None
        body = _render_text(body_tpl, member, for_title=False).strip() or None

        embed = discord.Embed(title=title, description=body, color=color)
        embed.set_thumbnail(url=member.display_avatar.url)

        try:
            await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except (discord.Forbidden, discord.HTTPException):
            return

async def setup(bot: commands.Bot, registry: SettingsRegistry) -> None:
    existing = bot.get_cog("VoiceAnnouncementsCog")
    if existing is not None:
        await bot.remove_cog("VoiceAnnouncementsCog")

    await bot.add_cog(VoiceAnnouncementsCog(bot))

    def status() -> str:
        s = load_state()
        return "✅ Enabled" if s.get("enabled", True) else "❌ Disabled"

    async def handler(interaction: discord.Interaction, ctx: Dict[str, Any]) -> dict | None:
        action = (ctx.get("action") or "toggle").lower().strip()

        if action == "toggle":
            s = load_state()
            s["enabled"] = not s.get("enabled", True)
            save_state(s)
            return None

        if action == "edit_join":
            s = load_state()
            return {"op": "modal", "modal": EditJoinModal(s)}

        if action == "edit_leave":
            s = load_state()
            return {"op": "modal", "modal": EditLeaveModal(s)}

        if action == "preview_join":
            s = load_state()
            member = interaction.user if isinstance(interaction.user, discord.Member) else None
            if member is None and interaction.guild is not None:
                member = interaction.guild.get_member(interaction.user.id)
            if member is None:
                return {"op": "respond", "payload": {"content": "Cannot preview (member not found).", "ephemeral": True}}
            t, b = _get_templates("join", s)
            t2 = _render_text(t, member, for_title=True).strip() or "(no title)"
            b2 = _render_text(b, member, for_title=False).strip() or "(no body)"
            return {"op": "respond", "payload": {"content": f"Title: {t2}\nBody: {b2}", "ephemeral": True}}

        if action == "preview_leave":
            s = load_state()
            member = interaction.user if isinstance(interaction.user, discord.Member) else None
            if member is None and interaction.guild is not None:
                member = interaction.guild.get_member(interaction.user.id)
            if member is None:
                return {"op": "respond", "payload": {"content": "Cannot preview (member not found).", "ephemeral": True}}
            t, b = _get_templates("leave", s)
            t2 = _render_text(t, member, for_title=True).strip() or "(no title)"
            b2 = _render_text(b, member, for_title=False).strip() or "(no body)"
            return {"op": "respond", "payload": {"content": f"Title: {t2}\nBody: {b2}", "ephemeral": True}}

        return None

    registry.register(SettingFeature(
        feature_id="voice_announcements",
        label="Voice Announcements",
        description=(
            "Posts an embed announcement when users join or leave a voice channel.\n\n"
            "Editable:\n"
            "- Join title (optional) + body (optional)\n"
            "- Leave title (optional) + body (optional)\n\n"
            "At least one of title/body must be filled.\n\n"
            "Supported placeholder:\n"
            "`{user_mention}` (clickable mention in body; no ping)"
        ),
        category=CATEGORY,
        category_description=CATEGORY_DESCRIPTION,
        handler=handler,
        status=status,
        actions=[
            FeatureAction("edit_join", "Edit Join Template", style="secondary", row=1),
            FeatureAction("edit_leave", "Edit Leave Template", style="secondary", row=1),
            FeatureAction("preview_join", "Preview Join", style="primary", row=2),
            FeatureAction("preview_leave", "Preview Leave", style="primary", row=2),
        ],
    ))
