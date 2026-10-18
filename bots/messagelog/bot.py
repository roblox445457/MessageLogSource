"""MessageLog: a Discord server message logging bot.

Discord only provides deleted/edited content when it is available to the bot
(usually through its message cache). This bot records the data Discord exposes;
it does not claim to recover uncached content or deleted attachment files.
"""

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from database import DEFAULT_EVENTS, Database

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("messagelog")

BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")
SUPPORT_URL = os.getenv("SUPPORT_URL", "").strip()
BOT_VERSION = os.getenv("BOT_VERSION", "1.0.0")


def env_id(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    try:
        return int(value.strip())
    except ValueError:
        log.warning("Ignoring invalid integer value for %s", name)
        return default


def env_id_set(name: str, defaults: set[int]) -> set[int]:
    value = os.getenv(name)
    if value is None:
        return set(defaults)
    try:
        return {int(item.strip()) for item in value.split(",") if item.strip()}
    except ValueError:
        log.warning("Ignoring invalid ID list for %s", name)
        return set(defaults)


FEEDBACK_GUILD_ID = env_id("FEEDBACK_GUILD_ID", 1561421141636157541)
FEEDBACK_CHANNEL_ID = env_id("FEEDBACK_CHANNEL_ID", 1561421143636971562)
FEEDBACK_OWNER_IDS = env_id_set(
    "FEEDBACK_OWNER_IDS",
    {1230660770749087796, 1354784283796967444},
)
FEEDBACK_EMOJI = discord.PartialEmoji(name="feedback", id=1561422992112877638)
FEEDBACK_COOLDOWN_SECONDS = 24 * 60 * 60
STARTED_AT = time.monotonic()
DB = Database()

CHARCOAL = discord.Colour(0x2B2D31)
BLUE = discord.Colour(0x5865F2)
GREEN = discord.Colour(0x248046)
AMBER = discord.Colour(0xD99A21)
RED = discord.Colour(0xDA373C)

EMOJI = {
    "config": "<:config:1561414242240831570>",
    "info": "<:info:1561415081038708736>",
    "member": "<:member:1561415094943092736>",
    "support": "<:support:1561414031255011480>",
    "message": "<:message:1561415443120656504>",
}
BUTTON_EMOJI = {
    "config": discord.PartialEmoji(name="config", id=1561414242240831570),
    "info": discord.PartialEmoji(name="info", id=1561415081038708736),
    "member": discord.PartialEmoji(name="member", id=1561415094943092736),
    "support": discord.PartialEmoji(name="support", id=1561414031255011480),
    "message": discord.PartialEmoji(name="message", id=1561415443120656504),
}
EVENT_LABELS = {
    "message_create": "New message",
    "message_delete": "Message deleted",
    "message_edit": "Message edited",
    "bulk_delete": "Bulk deletion",
    "member_join": "Member joined",
    "member_leave": "Member left",
    "config_change": "Configuration changed",
}

intents = discord.Intents.default()
intents.guilds = True
intents.messages = True
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)
_log_send_locks: dict[int, asyncio.Lock] = {}
_snipe_cache: dict[tuple[int, int], dict[str, Any]] = {}
_feedback_views_loaded = False


def clip(value: str | None, limit: int = 1000) -> str:
    if value is None or value == "":
        return "_No message content was available._"
    value = discord.utils.escape_markdown(value)
    return value if len(value) <= limit else value[: limit - 3] + "..."


def stamp(value: str | None) -> str:
    if not value:
        return "Unknown"
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return f"<t:{int(dt.timestamp())}:F>"
    except (TypeError, ValueError, OverflowError):
        return "Unknown"


def attachments_from_message(message: discord.Message) -> list[dict[str, Any]]:
    return [
        {
            "id": item.id,
            "filename": item.filename,
            "url": item.url,
            "content_type": item.content_type,
            "size": item.size,
        }
        for item in message.attachments
    ]


def attachments_from_payload(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "id": item.get("id"),
            "filename": item.get("filename", "attachment"),
            "url": item.get("url"),
            "content_type": item.get("content_type"),
            "size": item.get("size"),
        }
        for item in items
    ]


def attachment_fingerprint(items: list[dict[str, Any]]) -> tuple[tuple[Any, ...], ...]:
    return tuple(sorted(
        (
            str(item.get("id") or ""),
            str(item.get("filename") or ""),
            str(item.get("url") or ""),
            str(item.get("content_type") or ""),
            int(item.get("size") or 0),
        )
        for item in items
    ))


def verified_edit_changes(
    before: discord.Message | None, payload: dict[str, Any]
) -> dict[str, Any] | None:
    """Return changed, comparable fields only when a cached before-state exists."""
    if before is None:
        return None

    content_changed = (
        "content" in payload
        and payload["content"] is not None
        and payload["content"] != before.content
    )
    old_attachments = attachments_from_message(before)
    new_attachments = (
        attachments_from_payload(payload["attachments"])
        if "attachments" in payload
        else old_attachments
    )
    attachments_changed = (
        "attachments" in payload
        and attachment_fingerprint(new_attachments)
        != attachment_fingerprint(old_attachments)
    )
    if not content_changed and not attachments_changed:
        return None

    return {
        "content": payload.get("content", before.content),
        "previous_content": before.content,
        "attachments": new_attachments,
        "content_changed": content_changed,
        "attachments_changed": attachments_changed,
    }


def new_embed(title: str, description: str | None = None, color=CHARCOAL) -> discord.Embed:
    embed = discord.Embed(title=title, description=description, colour=color)
    embed.timestamp = datetime.now(timezone.utc)
    return embed


def event_embed(row: dict[str, Any], guild: discord.Guild | None = None) -> discord.Embed:
    event_type = row.get("event_type", "message_delete")
    is_edit = event_type == "message_edit"
    color = AMBER if is_edit else (RED if event_type in {"message_delete", "bulk_delete"} else BLUE)
    embed = discord.Embed(
        title=f"{EMOJI['message']}  {EVENT_LABELS.get(event_type, event_type.replace('_', ' ').title())}",
        colour=color,
    )
    embed.timestamp = datetime.fromisoformat(row["event_at"])
    if guild and guild.icon:
        embed.set_author(name=guild.name, icon_url=guild.icon.url)
    elif row.get("author_name"):
        embed.set_author(name=row["author_name"])
    if row.get("metadata", {}).get("avatar_url"):
        embed.set_thumbnail(url=row["metadata"]["avatar_url"])

    author = row.get("author_name") or "Unknown user"
    author_id = row.get("author_id")
    author_value = f"{author} (`{author_id}`)" if author_id else author
    channel = row.get("channel_name") or "Unknown channel"
    channel_id = row.get("channel_id")
    channel_value = f"#{channel} (`{channel_id}`)" if channel_id else channel
    embed.add_field(name="Author", value=author_value[:1024], inline=True)
    embed.add_field(name="Channel", value=channel_value[:1024], inline=True)
    if row.get("metadata", {}).get("message_created_at"):
        embed.add_field(
            name="Message sent",
            value=stamp(row["metadata"]["message_created_at"]),
            inline=True,
        )
    if row.get("message_id"):
        embed.add_field(name="Message ID", value=f"`{row['message_id']}`", inline=True)
    embed.add_field(name="Record ID", value=f"`{row['id']}`", inline=True)

    if is_edit:
        embed.add_field(name="Previous content", value=clip(row.get("previous_content")), inline=False)
        embed.add_field(name="Updated content", value=clip(row.get("content")), inline=False)
    elif event_type == "config_change":
        embed.description = clip(row.get("content"), 1800)
    elif event_type in {"member_join", "member_leave"}:
        embed.description = clip(row.get("content"), 1800)
    else:
        embed.add_field(name="Content", value=clip(row.get("content")), inline=False)

    attachments = row.get("attachments") or []
    if attachments:
        lines = []
        for attachment in attachments[:5]:
            name = discord.utils.escape_markdown(str(attachment.get("filename") or "attachment"))
            url = attachment.get("url")
            lines.append(f"[{name}]({url})" if url else name)
        if len(attachments) > 5:
            lines.append(f"…and {len(attachments) - 5} more")
        embed.add_field(name="Attachments", value="\n".join(lines)[:1024], inline=False)
    metadata = row.get("metadata") or {}
    if metadata.get("note"):
        embed.add_field(name="Note", value=str(metadata["note"])[:1024], inline=False)
    if metadata.get("jump_url"):
        embed.add_field(name="Original", value=f"[Open message]({metadata['jump_url']})", inline=True)
    embed.set_footer(text="MessageLog • Stored event data")
    return embed


def config_embed(guild: discord.Guild, config: dict[str, Any]) -> discord.Embed:
    channel = guild.get_channel(config.get("channel_id")) if config.get("channel_id") else None
    channel_text = channel.mention if isinstance(channel, discord.TextChannel) else "Not configured"
    active = [label for key, label in (
        ("message_create", "New messages"),
        ("message_delete", "Message deletions"),
        ("message_edit", "Message edits"),
        ("bulk_delete", "Bulk deletions"),
        ("member_join", "Member joins"),
        ("member_leave", "Member leaves"),
    ) if config["event_types"].get(key, False)]
    embed = new_embed(f"{EMOJI['config']}  Configuration")
    embed.description = (
        "Choose which events to log. New-message logging is optional and starts off."
    )
    embed.add_field(name="Logging channel", value=channel_text, inline=True)
    embed.add_field(
        name="Status",
        value="Enabled" if config["enabled"] and channel else (
            "Enabled · channel unavailable" if config["enabled"] else "Disabled"
        ),
        inline=True,
    )
    embed.add_field(name="Tracked events", value=", ".join(active) or "None", inline=False)
    embed.add_field(name="Retention", value=f"{config['retention_days']} days", inline=True)
    embed.set_footer(text=f"{guild.name} • Settings are saved automatically")
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    return embed


async def interaction_admin(interaction: discord.Interaction) -> bool:
    if not isinstance(interaction.user, discord.Member):
        return False
    permissions = interaction.user.guild_permissions
    if permissions.administrator or permissions.manage_guild:
        return True
    message = "This action requires **Manage Server** or **Administrator**."
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)
    return False


async def interaction_can_read_logs(interaction: discord.Interaction) -> bool:
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return False
    member = interaction.user
    if member.guild_permissions.administrator or member.guild_permissions.manage_guild:
        return True
    config = await DB.get_config(interaction.guild.id)
    channel = interaction.guild.get_channel(config.get("channel_id")) if config.get("channel_id") else None
    if isinstance(channel, discord.TextChannel) and channel.permissions_for(member).view_channel:
        return True
    message = "You need **Manage Server** or access to the configured log channel to view stored logs."
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)
    return False


async def resolve_log_channel(guild: discord.Guild, channel_id: int | None) -> discord.TextChannel | None:
    channel = guild.get_channel(channel_id) if channel_id else None
    if isinstance(channel, discord.TextChannel):
        return channel
    if channel_id:
        try:
            fetched = await guild.fetch_channel(channel_id)
            return fetched if isinstance(fetched, discord.TextChannel) else None
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None
    return None


async def persist_and_post(
    guild: discord.Guild,
    event_type: str,
    *,
    message_id: int | None = None,
    author_id: int | None = None,
    author_name: str | None = None,
    channel_id: int | None = None,
    channel_name: str | None = None,
    content: str | None = None,
    previous_content: str | None = None,
    attachments: list[dict[str, Any]] | None = None,
    metadata: dict[str, Any] | None = None,
    event_at: str | None = None,
) -> int:
    log_id = await DB.add_log(
        guild_id=guild.id,
        event_type=event_type,
        message_id=message_id,
        author_id=author_id,
        author_name=author_name,
        channel_id=channel_id,
        channel_name=channel_name,
        content=content,
        previous_content=previous_content,
        attachments=attachments,
        metadata=metadata,
        event_at=event_at,
    )
    row = await DB.get_log(guild.id, log_id)
    if row:
        await post_stored_log(guild, row)
    return log_id


async def post_stored_log(guild: discord.Guild, row: dict[str, Any]) -> None:
    lock = _log_send_locks.setdefault(guild.id, asyncio.Lock())
    async with lock:
        config = await DB.get_config(guild.id)
        if not config.get("channel_id"):
            return
        if row["event_type"] != "config_change" and not config.get("enabled"):
            return
        if row["event_type"] != "config_change" and not config["event_types"].get(row["event_type"], False):
            return
        channel = await resolve_log_channel(guild, config["channel_id"])
        if not channel:
            await DB.update_config(guild.id, channel_id=None, enabled=False)
            log.warning(
                "Configured log channel is missing; disabled logging for guild %s",
                guild.id,
            )
            return
        permissions = channel.permissions_for(guild.me) if guild.me else None
        if not permissions or not (
            permissions.view_channel and permissions.send_messages and permissions.embed_links
        ):
            await DB.update_config(guild.id, enabled=False)
            log.warning(
                "Missing log-channel permissions; disabled logging for guild %s",
                guild.id,
            )
            return
        try:
            await channel.send(
                embed=event_embed(row, guild),
                view=LogActionsView(bot, row["id"]),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.NotFound:
            await DB.update_config(guild.id, channel_id=None, enabled=False)
            log.warning(
                "Discord no longer has the configured log channel; disabled logging for guild %s",
                guild.id,
            )
        except discord.Forbidden:
            await DB.update_config(guild.id, enabled=False)
            log.warning(
                "Discord denied access to the configured log channel; disabled logging for guild %s",
                guild.id,
            )
        except discord.HTTPException:
            log.exception("Failed to post a log in guild %s", guild.id)


async def record_config_change(
    guild: discord.Guild, actor: discord.abc.User, description: str
) -> None:
    await persist_and_post(
        guild,
        "config_change",
        author_id=actor.id,
        author_name=str(actor),
        content=description,
        metadata={"avatar_url": actor.display_avatar.url},
    )


async def create_log_channel(guild: discord.Guild) -> discord.TextChannel:
    me = guild.me
    if not me or not me.guild_permissions.manage_channels:
        raise PermissionError("The bot needs **Manage Channels** to create a log channel.")
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        me: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, embed_links=True, read_message_history=True
        ),
    }
    return await guild.create_text_channel(
        "message-logs",
        topic="Private message event logs managed by MessageLog.",
        overwrites=overwrites,
        reason="MessageLog logging channel setup",
    )


async def setup_channel(guild: discord.Guild, channel: discord.TextChannel) -> str | None:
    me = guild.me
    if not me:
        return "I could not resolve my server member record."
    perms = channel.permissions_for(me)
    if not (perms.view_channel and perms.send_messages and perms.embed_links):
        return "I need **View Channel**, **Send Messages**, and **Embed Links** in that channel."
    config = await DB.update_config(guild.id, channel_id=channel.id)
    return None


async def send_log_list(
    interaction: discord.Interaction,
    rows: list[dict[str, Any]],
    title: str,
    *,
    empty: str = "No matching records are available.",
) -> None:
    if not rows:
        await interaction.response.send_message(
            embed=new_embed(f"{EMOJI['message']}  {title}", empty), ephemeral=True
        )
        return
    view = LogPager(bot, rows, title)
    await interaction.response.send_message(
        embed=view.current_embed(interaction.guild), view=view, ephemeral=True
    )


class LogActionsView(discord.ui.View):
    def __init__(self, client: commands.Bot, log_id: int):
        super().__init__(timeout=900)
        self.client = client
        self.log_id = log_id

    async def fetch_row(self, interaction: discord.Interaction) -> dict[str, Any] | None:
        if not interaction.guild or not await interaction_can_read_logs(interaction):
            return None
        return await DB.get_log(interaction.guild.id, self.log_id)

    @discord.ui.button(
        label="View Message",
        style=discord.ButtonStyle.secondary,
        emoji=BUTTON_EMOJI["message"],
    )
    async def view_message(self, interaction: discord.Interaction, _: discord.ui.Button):
        row = await self.fetch_row(interaction)
        if not row:
            if not interaction.response.is_done():
                await interaction.response.send_message("That stored log is no longer available.", ephemeral=True)
            return
        embed = event_embed(row, interaction.guild)
        embed.title = f"{EMOJI['message']}  Stored message details"
        embed.description = (
            "This view uses data captured by MessageLog. "
            "A deleted message cannot be reopened in Discord."
        )
        edits = await DB.query(
            "guild_id = ? AND message_id = ? AND event_type = ?",
            (interaction.guild.id, row.get("message_id"), "message_edit"),
            limit=10,
        ) if row.get("message_id") else []
        if edits:
            for index, edit in enumerate(reversed(edits[-3:]), start=1):
                history = (
                    f"**Previous:** {clip(edit.get('previous_content'), 450)}\n"
                    f"**Updated:** {clip(edit.get('content'), 450)}"
                )
                embed.add_field(
                    name=f"Edit {index} · {stamp(edit.get('event_at'))}",
                    value=history[:1024],
                    inline=False,
                )
            if len(edits) > 3:
                embed.add_field(name="Edit history", value="Showing the 3 most recent recorded edit events.", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(
        label="View User",
        style=discord.ButtonStyle.secondary,
        emoji=BUTTON_EMOJI["member"],
    )
    async def view_user(self, interaction: discord.Interaction, _: discord.ui.Button):
        row = await self.fetch_row(interaction)
        if not row:
            if not interaction.response.is_done():
                await interaction.response.send_message("That stored log is no longer available.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        member = interaction.guild.get_member(row["author_id"]) if row.get("author_id") else None
        user = member
        if not user and row.get("author_id"):
            try:
                user = await interaction.guild.fetch_member(row["author_id"])
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                user = None
        display_name = member.display_name if member else (row.get("author_name") or "Unknown user")
        user_embed = new_embed(f"{EMOJI['member']}  Member details", color=BLUE)
        user_embed.add_field(name="Name", value=display_name, inline=True)
        user_embed.add_field(name="User ID", value=f"`{row.get('author_id') or 'Unknown'}`", inline=True)
        if user:
            user_embed.set_thumbnail(url=user.display_avatar.url)
            if isinstance(user, discord.Member):
                user_embed.add_field(name="Joined server", value=stamp(user.joined_at.isoformat() if user.joined_at else None), inline=False)
        history = await DB.search_logs(
            interaction.guild.id, author_id=row.get("author_id"), limit=5
        ) if row.get("author_id") else []
        user_embed.add_field(name="Recent stored activity", value=f"{len(history)} record(s) shown in this server.", inline=False)
        view = MemberActivityView(self.client, row.get("author_id")) if row.get("author_id") else None
        await interaction.followup.send(embed=user_embed, view=view, ephemeral=True)

    @discord.ui.button(
        label="View Commands",
        style=discord.ButtonStyle.primary,
        emoji=BUTTON_EMOJI["info"],
    )
    async def view_commands(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_message(
            embed=help_embed("Messages"), view=HelpView(), ephemeral=True
        )


class LogPager(discord.ui.View):
    def __init__(self, client: commands.Bot, rows: list[dict[str, Any]], title: str):
        super().__init__(timeout=300)
        self.client = client
        self.rows = rows
        self.title = title
        self.index = 0
        self._sync_buttons()

    def _sync_buttons(self):
        self.previous.disabled = self.index <= 0
        self.next.disabled = self.index >= len(self.rows) - 1

    def current_embed(self, guild: discord.Guild | None) -> discord.Embed:
        row = self.rows[self.index]
        embed = event_embed(row, guild)
        embed.title = f"{EMOJI['message']}  {self.title}"
        embed.set_footer(text=f"Record {self.index + 1} of {len(self.rows)} • MessageLog")
        return embed

    @discord.ui.button(
        label="Previous",
        style=discord.ButtonStyle.secondary,
        emoji=BUTTON_EMOJI["message"],
    )
    async def previous(self, interaction: discord.Interaction, _: discord.ui.Button):
        self.index = max(0, self.index - 1)
        self._sync_buttons()
        await interaction.response.edit_message(
            embed=self.current_embed(interaction.guild), view=self
        )

    @discord.ui.button(
        label="Next",
        style=discord.ButtonStyle.secondary,
        emoji=BUTTON_EMOJI["message"],
    )
    async def next(self, interaction: discord.Interaction, _: discord.ui.Button):
        self.index = min(len(self.rows) - 1, self.index + 1)
        self._sync_buttons()
        await interaction.response.edit_message(
            embed=self.current_embed(interaction.guild), view=self
        )

    @discord.ui.button(
        label="Details",
        style=discord.ButtonStyle.primary,
        emoji=BUTTON_EMOJI["info"],
    )
    async def details(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not await interaction_can_read_logs(interaction):
            return
        row = self.rows[self.index]
        await interaction.response.send_message(
            embed=event_embed(row, interaction.guild),
            view=LogActionsView(self.client, row["id"]),
            ephemeral=True,
        )

    @discord.ui.button(
        label="Close",
        style=discord.ButtonStyle.danger,
        emoji=BUTTON_EMOJI["support"],
    )
    async def close(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.edit_message(content="Log browser closed.", embed=None, view=None)


class MemberActivityView(discord.ui.View):
    def __init__(self, client: commands.Bot, member_id: int):
        super().__init__(timeout=300)
        self.client = client
        self.member_id = member_id

    @discord.ui.button(
        label="Browse recent logs",
        style=discord.ButtonStyle.primary,
        emoji=BUTTON_EMOJI["message"],
    )
    async def browse(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not interaction.guild or not await interaction_can_read_logs(interaction):
            return
        rows = await DB.search_logs(
            interaction.guild.id, author_id=self.member_id, limit=10
        )
        if not rows:
            await interaction.response.send_message(
                "No stored activity is available for this member.", ephemeral=True
            )
            return
        view = LogPager(self.client, rows, "Member activity")
        await interaction.response.send_message(
            embed=view.current_embed(interaction.guild), view=view, ephemeral=True
        )


def help_embed(category: str = "Overview") -> discord.Embed:
    categories = {
        "Setup": (
            "`/set channel #logs` — choose the destination.\n"
            "`/create channel` — create a private destination.\n"
            "`/remove channel` — remove it and pause logging.\n"
            "`/channel` — inspect the current configuration.\n\n"
            "**Permission:** Manage Server or Administrator."
        ),
        "Logging": (
            "`/logs enable` — start the selected event logging.\n"
            "`/logs disable` — pause new event logging.\n"
            "`/logs status` — inspect status and tracked events.\n"
            "`/logs test` — send a test embed to the destination.\n"
            "`/config` — choose events per server; new-message logging starts off.\n\n"
            "**Permission:** Manage Server or Administrator for changes; "
            "Manage Server or log-channel access to view status."
        ),
        "Messages": (
            "`/message view record_id:123` — open a stored record.\n"
            "`/message search query:keyword member:@user` — filter records.\n"
            "`/message history member:@user` — browse activity.\n"
            "`/message edits message_id:123456789` — review edits.\n"
            "`/message deleted` — browse deletion records.\n"
            "`/snipe` — privately view the latest cached deletion in this channel.\n\n"
            "**Permission:** Manage Server or access to the configured log channel."
        ),
        "Members": (
            "`/member info member:@user` — view server member details.\n"
            "`/member logs member:@user` — browse stored activity.\n"
            "`/member count` — show the current server member count.\n\n"
            "**Permission:** Member information is available to server members; "
            "stored logs require Manage Server or log-channel access."
        ),
        "General": (
            "`/help` — open this interactive command guide.\n"
            "`/info` — show version, uptime, and latency.\n"
            "`/support` — open command and setup help.\n"
            "`/feedback` — send feedback to the MessageLog team.\n"
            "`/config` — open interactive server settings.\n\n"
            "`/config` requires Manage Server or Administrator. "
            "New-message logging can be enabled per server and is off by default."
        ),
    }
    embed = new_embed(f"{EMOJI['info']}  MessageLog command guide")
    embed.description = categories.get(category, "Choose a category to browse available commands.")
    embed.set_footer(text="Administrative commands require Manage Server or Administrator.")
    return embed


class HelpSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(
                label=label,
                value=key,
                description=f"Browse {key.lower()} commands",
                emoji=BUTTON_EMOJI[emoji],
            )
            for key, label, emoji in (
                ("Setup", "Setup", "config"),
                ("Logging", "Logging", "config"),
                ("Messages", "Messages", "message"),
                ("Members", "Members", "member"),
                ("General", "General", "info"),
            )
        ]
        super().__init__(placeholder="Choose a command category", options=options, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            embed=help_embed(self.values[0]), view=self.view
        )


class HelpView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
        self.add_item(HelpSelect())

    @discord.ui.button(
        label="Close",
        style=discord.ButtonStyle.secondary,
        emoji=BUTTON_EMOJI["support"],
        row=1,
    )
    async def close(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.edit_message(content="Help closed.", embed=None, view=None)

    @discord.ui.button(
        label="Send Feedback",
        style=discord.ButtonStyle.success,
        emoji=FEEDBACK_EMOJI,
        row=1,
    )
    async def send_feedback(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(FeedbackModal())


def support_embed() -> discord.Embed:
    embed = new_embed(f"{EMOJI['support']}  MessageLog Support")
    embed.description = (
        "Choose **Commands** for the full guide or **Setup** for the first steps."
    )
    embed.add_field(
        name="Quick start",
        value="`/create channel` creates a private log channel and enables logging.\n"
        "`/set channel` selects an existing channel.\n"
        "`/logs test` checks that MessageLog can post there.",
        inline=False,
    )
    if SUPPORT_URL:
        embed.add_field(
            name="Support resources",
            value=f"[Open MessageLog Support]({SUPPORT_URL})",
            inline=False,
        )
    else:
        embed.add_field(
            name="Support resources",
            value="Use the buttons below for the command guide and setup help.",
            inline=False,
        )
    embed.set_footer(text="MessageLog • Support and command help")
    return embed


class SupportView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
        if SUPPORT_URL.startswith(("https://", "http://")):
            self.add_item(
                discord.ui.Button(
                    label="Support Server",
                    style=discord.ButtonStyle.link,
                    url=SUPPORT_URL,
                    emoji=BUTTON_EMOJI["support"],
                )
            )

    @discord.ui.button(
        label="Commands",
        style=discord.ButtonStyle.primary,
        emoji=BUTTON_EMOJI["info"],
    )
    async def commands(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_message(
            embed=help_embed(), view=HelpView(), ephemeral=True
        )

    @discord.ui.button(
        label="Setup",
        style=discord.ButtonStyle.secondary,
        emoji=BUTTON_EMOJI["config"],
    )
    async def setup(self, interaction: discord.Interaction, _: discord.ui.Button):
        embed = new_embed(f"{EMOJI['config']}  Getting started")
        embed.description = (
            "1. Run `/create channel` to create a private destination, "
            "or `/set channel` to select one.\n"
            "2. Run `/logs enable` if logging is not already enabled.\n"
            "3. Run `/logs test` to verify the bot can post.\n\n"
            "Use `/config` to choose which event types to track. "
            "Server changes require Manage Server or Administrator."
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(
        label="Close",
        style=discord.ButtonStyle.secondary,
        emoji=BUTTON_EMOJI["support"],
    )
    async def close(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.edit_message(content="Support closed.", embed=None, view=None)

    @discord.ui.button(
        label="Send Feedback",
        style=discord.ButtonStyle.success,
        emoji=FEEDBACK_EMOJI,
    )
    async def send_feedback(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(FeedbackModal())


class SetChannelModal(discord.ui.Modal, title="Set logging channel"):
    channel_id = discord.ui.TextInput(
        label="Text channel ID",
        placeholder="Paste the numeric channel ID",
        min_length=5,
        max_length=22,
    )

    async def on_submit(self, interaction: discord.Interaction):
        if not interaction.guild or not await interaction_admin(interaction):
            return
        try:
            channel_id = int(str(self.channel_id).strip())
        except ValueError:
            await interaction.response.send_message("Enter a numeric text channel ID.", ephemeral=True)
            return
        channel = interaction.guild.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(
                "I couldn't find an accessible text channel with that ID.", ephemeral=True
            )
            return
        error = await setup_channel(interaction.guild, channel)
        if error:
            await interaction.response.send_message(error, ephemeral=True)
            return
        await interaction.response.send_message(f"Logging channel set to {channel.mention}.", ephemeral=True)
        await record_config_change(interaction.guild, interaction.user, f"Logging channel set to {channel.mention}.")


class ConfigEventSelect(discord.ui.Select):
    def __init__(self, event_types: dict[str, bool]):
        labels = [
            ("message_create", "New messages", "Log new messages; off by default."),
            ("message_delete", "Message deletions", "Log deleted messages when Discord provides them."),
            ("message_edit", "Message edits", "Log only verified text or attachment changes."),
            ("bulk_delete", "Bulk deletions", "Log messages removed in a bulk-delete action."),
            ("member_join", "Member joins", "Log when a member joins this server."),
            ("member_leave", "Member leaves", "Log when a member leaves this server."),
        ]
        options = [
            discord.SelectOption(
                label=label,
                value=key,
                description=description,
                default=bool(event_types.get(key, False)),
            )
            for key, label, description in labels
        ]
        super().__init__(
            placeholder="Choose which events to track",
            min_values=0,
            max_values=len(options),
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        if not interaction.guild or not await interaction_admin(interaction):
            return
        selected = set(self.values)
        config = await DB.get_config(interaction.guild.id)
        events = {key: key in selected for key in DEFAULT_EVENTS}
        await DB.update_config(interaction.guild.id, event_types=events)
        for option in self.options:
            option.default = option.value in selected
        await interaction.response.edit_message(
            embed=config_embed(interaction.guild, await DB.get_config(interaction.guild.id)),
            view=self.view,
        )
        await record_config_change(interaction.guild, interaction.user, "Updated tracked event types.")


class ConfigView(discord.ui.View):
    def __init__(self, config: dict[str, Any] | None = None):
        super().__init__(timeout=300)
        self.config = config or {
            "enabled": False,
            "event_types": dict(),
        }
        self.add_item(ConfigEventSelect(self.config["event_types"]))
        self.toggle_logging.label = "Disable Logging" if self.config["enabled"] else "Enable Logging"

    async def refresh(self, interaction: discord.Interaction):
        config = await DB.get_config(interaction.guild.id)
        self.toggle_logging.label = "Disable Logging" if config["enabled"] else "Enable Logging"
        await interaction.response.edit_message(
            embed=config_embed(interaction.guild, config), view=self
        )

    @discord.ui.button(
        label="Enable Logging",
        style=discord.ButtonStyle.success,
        emoji=BUTTON_EMOJI["config"],
    )
    async def toggle_logging(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not interaction.guild or not await interaction_admin(interaction):
            return
        await interaction.response.defer()
        config = await DB.get_config(interaction.guild.id)
        new_state = not config["enabled"]
        if new_state and not await resolve_log_channel(interaction.guild, config.get("channel_id")):
            await interaction.followup.send(
                "Set a logging channel before enabling logging.", ephemeral=True
            )
            return
        await DB.update_config(interaction.guild.id, enabled=new_state)
        updated = await DB.get_config(interaction.guild.id)
        self.toggle_logging.label = "Disable Logging" if updated["enabled"] else "Enable Logging"
        await interaction.edit_original_response(
            embed=config_embed(interaction.guild, updated),
            view=self,
        )
        await record_config_change(interaction.guild, interaction.user, f"{'Enabled' if new_state else 'Disabled'} logging.")

    @discord.ui.button(
        label="Set Channel",
        style=discord.ButtonStyle.secondary,
        emoji=BUTTON_EMOJI["config"],
    )
    async def set_channel(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not interaction.guild or not await interaction_admin(interaction):
            return
        await interaction.response.send_modal(SetChannelModal())

    @discord.ui.button(
        label="Create Channel",
        style=discord.ButtonStyle.secondary,
        emoji=BUTTON_EMOJI["config"],
    )
    async def create_channel(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not interaction.guild or not await interaction_admin(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            channel = await create_log_channel(interaction.guild)
            await setup_channel(interaction.guild, channel)
            await DB.update_config(interaction.guild.id, enabled=True)
            await interaction.followup.send(f"Created and configured {channel.mention}.", ephemeral=True)
            await record_config_change(
                interaction.guild, interaction.user, f"Created logging channel {channel.mention}."
            )
        except (PermissionError, discord.Forbidden, discord.HTTPException) as exc:
            await interaction.followup.send(str(exc) or "I couldn't create the channel.", ephemeral=True)

    @discord.ui.button(
        label="View Commands",
        style=discord.ButtonStyle.secondary,
        emoji=BUTTON_EMOJI["info"],
    )
    async def view_commands(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_message(
            embed=help_embed("Setup"), view=HelpView(), ephemeral=True
        )


async def update_and_log(
    interaction: discord.Interaction, description: str, **changes: Any
) -> dict[str, Any]:
    config = await DB.update_config(interaction.guild.id, **changes)
    await record_config_change(interaction.guild, interaction.user, description)
    return config


class FeedbackDeliveryError(RuntimeError):
    pass


async def resolve_feedback_channel() -> discord.TextChannel:
    guild = bot.get_guild(FEEDBACK_GUILD_ID)
    if guild is None:
        try:
            guild = await bot.fetch_guild(FEEDBACK_GUILD_ID)
        except discord.HTTPException as exc:
            raise FeedbackDeliveryError(
                "The configured feedback server is unavailable to the bot."
            ) from exc

    channel = bot.get_channel(FEEDBACK_CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(FEEDBACK_CHANNEL_ID)
        except discord.HTTPException as exc:
            raise FeedbackDeliveryError(
                "The configured feedback channel could not be found."
            ) from exc
    if not isinstance(channel, discord.TextChannel) or channel.guild.id != FEEDBACK_GUILD_ID:
        raise FeedbackDeliveryError(
            "The configured feedback destination must be a text channel in the feedback server."
        )

    bot_member = guild.me
    if bot_member is None and bot.user is not None:
        bot_member = guild.get_member(bot.user.id)
    if bot_member is None:
        raise FeedbackDeliveryError(
            "The bot could not confirm its permissions in the feedback server."
        )
    permissions = channel.permissions_for(bot_member)
    if not (permissions.view_channel and permissions.send_messages and permissions.embed_links):
        raise FeedbackDeliveryError(
            "The bot needs View Channel, Send Messages, and Embed Links in the feedback channel."
        )
    return channel


def feedback_submission_embed(row: dict[str, Any]) -> discord.Embed:
    embed = new_embed(f"{EMOJI['support']}  MessageLog feedback", color=BLUE)
    embed.description = row["content"][:4000]
    embed.add_field(
        name="Submitted by",
        value=f"{row['user_name']} (`{row['user_id']}`)",
        inline=False,
    )
    embed.add_field(
        name="Origin",
        value=row.get("source_guild_name") or "Direct message",
        inline=True,
    )
    embed.add_field(name="Submission", value=f"`#{row['id']}`", inline=True)
    embed.set_footer(text=f"Submitted {stamp(row.get('submitted_at'))}")
    return embed


async def deliver_feedback(feedback_id: int) -> None:
    row = await DB.get_feedback(feedback_id)
    if not row or row.get("delivery_message_id"):
        return
    channel = await resolve_feedback_channel()
    view = FeedbackReplyView(feedback_id)
    try:
        message = await channel.send(
            embed=feedback_submission_embed(row),
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except discord.HTTPException as exc:
        raise FeedbackDeliveryError(
            "Discord did not accept the feedback message."
        ) from exc
    await DB.mark_feedback_delivered(feedback_id, message.id)
    bot.add_view(view, message_id=message.id)


class FeedbackModal(discord.ui.Modal, title="Send feedback to MessageLog"):
    feedback = discord.ui.TextInput(
        label="Your feedback",
        placeholder="Tell us what is working or what could be better.",
        style=discord.TextStyle.paragraph,
        min_length=5,
        max_length=1800,
        required=True,
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            content = str(self.feedback).strip()
            if len(content) < 5:
                await interaction.followup.send(
                    "Please enter at least five non-space characters of feedback.",
                    ephemeral=True,
                )
                return
            feedback_id, remaining = await DB.create_feedback(
                user_id=interaction.user.id,
                user_name=str(interaction.user),
                source_guild_id=interaction.guild.id if interaction.guild else None,
                source_guild_name=interaction.guild.name if interaction.guild else None,
                content=content,
                cooldown_seconds=FEEDBACK_COOLDOWN_SECONDS,
            )
            if feedback_id is None:
                hours, minutes = divmod((remaining + 59) // 60, 60)
                duration = f"{hours}h" + (f" {minutes}m" if minutes else "")
                await interaction.followup.send(
                    f"Thanks for the feedback you already sent. You can submit again in "
                    f"about {duration} (one submission every 24 hours).",
                    ephemeral=True,
                )
                return
            try:
                await deliver_feedback(feedback_id)
            except FeedbackDeliveryError:
                log.exception("Feedback #%s is saved but could not be routed", feedback_id)
                await interaction.followup.send(
                    f"Your feedback was saved as submission **#{feedback_id}**, but I "
                    "couldn’t forward it to the team right now. It will be retried when "
                    "the bot reconnects. Your 24-hour submission limit is in effect.",
                    ephemeral=True,
                )
                return
            await interaction.followup.send(
                f"Thank you. Your feedback was sent to the MessageLog team as "
                f"submission **#{feedback_id}**.",
                ephemeral=True,
            )
        except Exception:
            log.exception("Could not save a feedback submission")
            await interaction.followup.send(
                "I couldn't save your feedback right now. Please try again later.",
                ephemeral=True,
            )


class FeedbackReplyModal(discord.ui.Modal):
    def __init__(self, feedback_id: int):
        super().__init__(title=f"Reply to feedback #{feedback_id}")
        self.feedback_id = feedback_id
        self.reply_text = discord.ui.TextInput(
            label="Your response",
            placeholder="Write a helpful reply for the person who submitted this.",
            style=discord.TextStyle.paragraph,
            min_length=1,
            max_length=1500,
            required=True,
        )
        self.add_item(self.reply_text)

    async def on_submit(self, interaction: discord.Interaction):
        if interaction.user.id not in FEEDBACK_OWNER_IDS:
            await interaction.response.send_message(
                "Only configured MessageLog owners can reply to feedback.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        row = await DB.get_feedback(self.feedback_id)
        if not row:
            await interaction.followup.send(
                "That feedback submission is no longer available.", ephemeral=True
            )
            return
        if row.get("replied_at"):
            await interaction.followup.send(
                "A reply has already been sent for this submission.", ephemeral=True
            )
            return

        reply = str(self.reply_text).strip()
        user = bot.get_user(row["user_id"])
        try:
            if user is None:
                user = await bot.fetch_user(row["user_id"])
            dm = new_embed(
                f"{EMOJI['support']}  A reply to your MessageLog feedback",
                f"The MessageLog team has responded to your feedback:\n\n{reply}",
                color=BLUE,
            )
            dm.add_field(
                name="Your feedback",
                value=clip(row["content"], 1000),
                inline=False,
            )
            dm.set_footer(text=f"Feedback submission #{self.feedback_id}")
            await user.send(
                embed=dm,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            await interaction.followup.send(
                "I couldn't deliver the DM. The feedback submission is still saved and "
                "the reply button remains available; the user may need to allow DMs "
                "from the bot.",
                ephemeral=True,
            )
            return

        try:
            await DB.mark_feedback_replied(
                self.feedback_id,
                reply,
                interaction.user.id,
            )
        except Exception:
            log.exception(
                "DM delivered for feedback #%s, but its reply status was not saved",
                self.feedback_id,
            )
            await interaction.followup.send(
                "The DM was delivered, but I couldn't save the reply status. "
                "Avoid sending another reply for this submission.",
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            "Your reply was sent to the feedback submitter by DM.", ephemeral=True
        )


class FeedbackReplyView(discord.ui.View):
    def __init__(self, feedback_id: int):
        super().__init__(timeout=None)
        self.feedback_id = feedback_id
        reply_button = discord.ui.Button(
            label="Reply to user",
            style=discord.ButtonStyle.primary,
            emoji=FEEDBACK_EMOJI,
            custom_id=f"messagelog:feedback:reply:{feedback_id}",
        )
        reply_button.callback = self.open_reply
        self.add_item(reply_button)

    async def open_reply(self, interaction: discord.Interaction):
        if interaction.user.id not in FEEDBACK_OWNER_IDS:
            await interaction.response.send_message(
                "Only configured MessageLog owners can reply to feedback.",
                ephemeral=True,
            )
            return
        row = await DB.get_feedback(self.feedback_id)
        if not row:
            await interaction.response.send_message(
                "That feedback submission is no longer available.", ephemeral=True
            )
            return
        if row.get("replied_at"):
            await interaction.response.send_message(
                "A reply has already been sent for this submission.", ephemeral=True
            )
            return
        await interaction.response.send_modal(FeedbackReplyModal(self.feedback_id))


def cache_snipe(
    guild: discord.Guild,
    channel_id: int,
    message_id: int,
    message: discord.Message | None,
) -> None:
    channel = guild.get_channel(channel_id)
    author = message.author if message else None
    cached_attachments = attachments_from_message(message) if message else []
    note = None
    if not message:
        note = (
            "This deleted message was not in the bot's cache. Discord did not provide "
            "its text, author, or attachments."
        )
    elif not message.content and not cached_attachments:
        note = "Discord provided no text or attachments for this deleted message."
    _snipe_cache[(guild.id, channel_id)] = {
        "id": None,
        "guild_id": guild.id,
        "message_id": message_id,
        "event_type": "message_delete",
        "author_id": author.id if author else None,
        "author_name": str(author) if author else None,
        "channel_id": channel_id,
        "channel_name": getattr(channel, "name", None),
        "content": message.content if message else None,
        "previous_content": None,
        "attachments": cached_attachments,
        "event_at": datetime.now(timezone.utc).isoformat(),
        "metadata": {
            "note": note,
            "avatar_url": author.display_avatar.url if author else None,
            "message_created_at": message.created_at.isoformat() if message else None,
            "jump_url": message.jump_url if message else None,
        },
    }


# ---- Slash command groups -------------------------------------------------
set_group = app_commands.Group(name="set", description="Set server logging options")
create_group = app_commands.Group(name="create", description="Create MessageLog resources")
remove_group = app_commands.Group(name="remove", description="Remove MessageLog settings")
logs_group = app_commands.Group(name="logs", description="Control and inspect message logging")
message_group = app_commands.Group(name="message", description="Search and browse stored message logs")
member_group = app_commands.Group(name="member", description="Inspect server members and their logs")


@set_group.command(name="channel", description="Set the existing text channel for message logs")
@app_commands.default_permissions(manage_guild=True)
@app_commands.describe(channel="The text channel where MessageLog should post")
async def set_channel_command(interaction: discord.Interaction, channel: discord.TextChannel):
    if not interaction.guild or not await interaction_admin(interaction):
        return
    error = await setup_channel(interaction.guild, channel)
    if error:
        await interaction.response.send_message(error, ephemeral=True)
        return
    await interaction.response.send_message(f"Logging channel set to {channel.mention}.", ephemeral=True)
    await record_config_change(interaction.guild, interaction.user, f"Logging channel set to {channel.mention}.")


@create_group.command(name="channel", description="Create and configure a private message-log channel")
@app_commands.default_permissions(manage_guild=True)
async def create_channel_command(interaction: discord.Interaction):
    if not interaction.guild or not await interaction_admin(interaction):
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        channel = await create_log_channel(interaction.guild)
        await setup_channel(interaction.guild, channel)
        await DB.update_config(interaction.guild.id, enabled=True)
        await interaction.followup.send(f"Created and configured {channel.mention}.", ephemeral=True)
        await record_config_change(interaction.guild, interaction.user, f"Created logging channel {channel.mention}.")
    except (PermissionError, discord.Forbidden, discord.HTTPException) as exc:
        await interaction.followup.send(str(exc) or "I couldn't create the channel.", ephemeral=True)


@remove_group.command(name="channel", description="Remove the configured logging channel")
@app_commands.default_permissions(manage_guild=True)
async def remove_channel_command(interaction: discord.Interaction):
    if not interaction.guild or not await interaction_admin(interaction):
        return
    await DB.update_config(interaction.guild.id, channel_id=None, enabled=False)
    await interaction.response.send_message("Logging channel removed; logging is now disabled.", ephemeral=True)
    await record_config_change(interaction.guild, interaction.user, "Removed the logging channel and disabled logging.")


@bot.tree.command(name="channel", description="Display the current logging channel and configuration")
async def channel_command(interaction: discord.Interaction):
    if not interaction.guild or not await interaction_can_read_logs(interaction):
        return
    await interaction.response.send_message(
        embed=config_embed(interaction.guild, await DB.get_config(interaction.guild.id)),
        ephemeral=True,
    )


@logs_group.command(name="enable", description="Enable event logging for this server")
@app_commands.default_permissions(manage_guild=True)
async def logs_enable(interaction: discord.Interaction):
    if not interaction.guild or not await interaction_admin(interaction):
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    config = await DB.get_config(interaction.guild.id)
    channel = await resolve_log_channel(interaction.guild, config.get("channel_id"))
    if not channel:
        await interaction.followup.send(
            "No accessible logging channel is configured. Use `/set channel` or `/create channel` first.",
            ephemeral=True,
        )
        return
    await DB.update_config(interaction.guild.id, enabled=True)
    await interaction.followup.send(f"Logging enabled in {channel.mention}.", ephemeral=True)
    await record_config_change(interaction.guild, interaction.user, "Enabled event logging.")


@logs_group.command(name="disable", description="Pause event logging for this server")
@app_commands.default_permissions(manage_guild=True)
async def logs_disable(interaction: discord.Interaction):
    if not interaction.guild or not await interaction_admin(interaction):
        return
    await DB.update_config(interaction.guild.id, enabled=False)
    await interaction.response.send_message("Logging disabled. Stored records remain available until retention expiry.", ephemeral=True)
    await record_config_change(interaction.guild, interaction.user, "Disabled event logging.")


@logs_group.command(name="status", description="Show logging state and enabled event types")
async def logs_status(interaction: discord.Interaction):
    if not interaction.guild or not await interaction_can_read_logs(interaction):
        return
    await interaction.response.send_message(
        embed=config_embed(interaction.guild, await DB.get_config(interaction.guild.id)),
        ephemeral=True,
    )


@logs_group.command(name="test", description="Send a test embed to the configured log channel")
@app_commands.default_permissions(manage_guild=True)
async def logs_test(interaction: discord.Interaction):
    if not interaction.guild or not await interaction_admin(interaction):
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    config = await DB.get_config(interaction.guild.id)
    channel = await resolve_log_channel(interaction.guild, config.get("channel_id"))
    if not channel:
        await interaction.followup.send(
            "No accessible logging channel is configured. Use `/set channel` or `/create channel` first.",
            ephemeral=True,
        )
        return
    try:
        embed = new_embed(f"{EMOJI['info']}  MessageLog test", "This test confirms that I can post embeds here.", GREEN)
        embed.set_footer(text=f"Requested by {interaction.user}")
        lock = _log_send_locks.setdefault(interaction.guild.id, asyncio.Lock())
        async with lock:
            await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        await interaction.followup.send(f"Test log sent to {channel.mention}.", ephemeral=True)
    except discord.NotFound:
        await DB.update_config(interaction.guild.id, channel_id=None, enabled=False)
        await interaction.followup.send("The configured channel no longer exists; logging is disabled. Choose another channel.", ephemeral=True)
    except discord.Forbidden:
        await DB.update_config(interaction.guild.id, enabled=False)
        await interaction.followup.send("I don't have permission to post in that channel; logging is disabled until permissions are fixed.", ephemeral=True)
    except discord.HTTPException:
        await interaction.followup.send("Discord couldn't accept the test message. Try again shortly.", ephemeral=True)


@message_group.command(name="view", description="View a stored log record by its record ID")
@app_commands.describe(record_id="The record number shown in the bot's stored-log browser")
async def message_view(interaction: discord.Interaction, record_id: app_commands.Range[int, 1]):
    if not interaction.guild or not await interaction_can_read_logs(interaction):
        return
    row = await DB.get_log(interaction.guild.id, record_id)
    if not row:
        await interaction.response.send_message("No stored log record has that ID in this server.", ephemeral=True)
        return
    await interaction.response.send_message(
        embed=event_embed(row, interaction.guild),
        view=LogActionsView(bot, row["id"]),
        ephemeral=True,
    )


@message_group.command(name="search", description="Search stored events by text and optional filters")
@app_commands.describe(
    query="Text found in the current or previous message content",
    member="Limit results to a server member",
    channel="Limit results to a text channel",
    event_type="Optional event category",
    limit="Maximum number of records (1–25)",
)
@app_commands.choices(event_type=[
    app_commands.Choice(name="New messages", value="message_create"),
    app_commands.Choice(name="Deleted messages", value="message_delete"),
    app_commands.Choice(name="Edited messages", value="message_edit"),
    app_commands.Choice(name="Bulk deletions", value="bulk_delete"),
    app_commands.Choice(name="Configuration changes", value="config_change"),
])
async def message_search(
    interaction: discord.Interaction,
    query: app_commands.Range[str, 1, 100] | None = None,
    member: discord.Member | None = None,
    channel: discord.TextChannel | None = None,
    event_type: app_commands.Choice[str] | None = None,
    limit: app_commands.Range[int, 1, 25] = 10,
):
    if not interaction.guild or not await interaction_can_read_logs(interaction):
        return
    rows = await DB.search_logs(
        interaction.guild.id,
        query=query,
        author_id=member.id if member else None,
        channel_id=channel.id if channel else None,
        event_type=event_type.value if event_type else None,
        limit=limit,
    )
    await send_log_list(interaction, rows, "Search results", empty="No records matched those filters.")


@message_group.command(name="history", description="Browse stored events for a member or channel")
@app_commands.describe(member="Member whose events to browse", channel="Channel whose events to browse")
async def message_history(
    interaction: discord.Interaction,
    member: discord.Member | None = None,
    channel: discord.TextChannel | None = None,
    limit: app_commands.Range[int, 1, 25] = 10,
):
    if not interaction.guild or not await interaction_can_read_logs(interaction):
        return
    if not member and not channel:
        await interaction.response.send_message("Choose a member, a channel, or both.", ephemeral=True)
        return
    clauses = ["guild_id = ?"]
    params: list[Any] = [interaction.guild.id]
    if member:
        clauses.append("author_id = ?")
        params.append(member.id)
    if channel:
        clauses.append("channel_id = ?")
        params.append(channel.id)
    rows = await DB.query(" AND ".join(clauses), tuple(params), limit=limit)
    await send_log_list(interaction, rows, "Message history")


@message_group.command(name="edits", description="Browse edits recorded for a Discord message ID")
@app_commands.describe(message_id="The original Discord message ID")
async def message_edits(interaction: discord.Interaction, message_id: str):
    if not interaction.guild or not await interaction_can_read_logs(interaction):
        return
    try:
        parsed_id = int(message_id.strip())
    except ValueError:
        await interaction.response.send_message("Message ID must be numeric.", ephemeral=True)
        return
    rows = await DB.query(
        "guild_id = ? AND message_id = ? AND event_type = ?",
        (interaction.guild.id, parsed_id, "message_edit"),
        limit=25,
    )
    await send_log_list(interaction, rows, "Recorded edit history", empty="No stored edits were found for that message ID.")


@message_group.command(name="deleted", description="Browse stored deleted-message records")
async def message_deleted(
    interaction: discord.Interaction,
    limit: app_commands.Range[int, 1, 25] = 10,
):
    if not interaction.guild or not await interaction_can_read_logs(interaction):
        return
    rows = await DB.query(
        "guild_id = ? AND event_type IN (?, ?)",
        (interaction.guild.id, "message_delete", "bulk_delete"),
        limit=limit,
    )
    await send_log_list(interaction, rows, "Deleted messages")


@member_group.command(name="info", description="Display useful information about a server member")
async def member_info(interaction: discord.Interaction, member: discord.Member):
    embed = new_embed(f"{EMOJI['member']}  Member information", color=BLUE)
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="Display name", value=member.display_name, inline=True)
    embed.add_field(name="User ID", value=f"`{member.id}`", inline=True)
    embed.add_field(name="Created", value=stamp(member.created_at.isoformat()), inline=True)
    embed.add_field(name="Joined", value=stamp(member.joined_at.isoformat() if member.joined_at else None), inline=True)
    roles = [role.mention for role in reversed(member.roles) if role.name != "@everyone"]
    embed.add_field(name="Roles", value=", ".join(roles[:10]) or "None", inline=False)
    embed.set_footer(text=f"{interaction.guild.name} • Member information")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@member_group.command(name="logs", description="Browse stored message-log activity for a member")
async def member_logs(interaction: discord.Interaction, member: discord.Member, limit: app_commands.Range[int, 1, 25] = 10):
    if not interaction.guild or not await interaction_can_read_logs(interaction):
        return
    rows = await DB.search_logs(interaction.guild.id, author_id=member.id, limit=limit)
    await send_log_list(interaction, rows, f"Activity for {member.display_name}")


@member_group.command(name="count", description="Show the current server member count")
async def member_count(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("This command is only available in a server.", ephemeral=True)
        return
    embed = new_embed(f"{EMOJI['member']}  Member count", color=BLUE)
    embed.add_field(name="Members", value=f"{interaction.guild.member_count:,}", inline=True)
    embed.add_field(name="Server", value=interaction.guild.name, inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="help", description="Open the interactive MessageLog command guide")
async def help_command(interaction: discord.Interaction):
    await interaction.response.send_message(embed=help_embed(), view=HelpView(), ephemeral=True)


@bot.tree.command(name="info", description="Display MessageLog version, uptime, and latency")
async def info_command(interaction: discord.Interaction):
    uptime = int(time.monotonic() - STARTED_AT)
    hours, remainder = divmod(uptime, 3600)
    minutes, seconds = divmod(remainder, 60)
    embed = new_embed(f"{EMOJI['info']}  MessageLog", "A server utility for message-event logging.", color=BLUE)
    embed.add_field(name="Version", value=BOT_VERSION, inline=True)
    embed.add_field(name="Uptime", value=f"{hours}h {minutes}m {seconds}s", inline=True)
    embed.add_field(name="Gateway latency", value=f"{round((bot.latency or 0) * 1000)} ms", inline=True)
    embed.set_footer(text="MessageLog")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="support", description="Show MessageLog support resources")
async def support_command(interaction: discord.Interaction):
    await interaction.response.send_message(
        embed=support_embed(),
        view=SupportView(),
        ephemeral=True,
    )


@bot.tree.command(name="feedback", description="Send feedback to the MessageLog team")
async def feedback_command(interaction: discord.Interaction):
    await interaction.response.send_modal(FeedbackModal())


@bot.tree.command(
    name="snipe",
    description="Privately view the latest deleted message captured in this channel",
)
async def snipe_command(interaction: discord.Interaction):
    if not interaction.guild or not interaction.channel_id:
        await interaction.response.send_message(
            "This command is only available in a server channel.", ephemeral=True
        )
        return
    row = _snipe_cache.get((interaction.guild.id, interaction.channel_id))
    if not row:
        await interaction.response.send_message(
            "No deleted message has been captured in this channel since the bot started. "
            "Deleted content is only available when Discord had the message in the bot's cache.",
            ephemeral=True,
        )
        return
    embed = event_embed(row, interaction.guild)
    embed.title = "Latest deleted message"
    embed.set_footer(text="Snipe • Captured from the bot's live message cache")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="config", description="Open the interactive server logging configuration panel")
@app_commands.default_permissions(manage_guild=True)
async def config_command(interaction: discord.Interaction):
    if not interaction.guild or not await interaction_admin(interaction):
        return
    await interaction.response.send_message(
        embed=config_embed(interaction.guild, await DB.get_config(interaction.guild.id)),
        view=ConfigView(await DB.get_config(interaction.guild.id)),
        ephemeral=True,
    )


@bot.listen("on_message")
async def log_new_member_message(message: discord.Message):
    guild = message.guild
    if guild is None or message.author.bot or message.webhook_id is not None:
        return
    config = await DB.get_config(guild.id)
    if not config["enabled"] or not config["event_types"].get("message_create", False):
        return

    channel = message.channel
    author = message.author
    attachments = attachments_from_message(message)
    await persist_and_post(
        guild,
        "message_create",
        message_id=message.id,
        author_id=author.id,
        author_name=str(author),
        channel_id=channel.id,
        channel_name=getattr(channel, "name", None),
        content=message.content or None,
        attachments=attachments,
        metadata={
            "note": None if message.content else "No message text was provided by Discord.",
            "avatar_url": author.display_avatar.url,
            "jump_url": message.jump_url,
            "message_created_at": message.created_at.isoformat(),
        },
        event_at=message.created_at.isoformat(),
    )


@bot.event
async def on_raw_message_delete(payload: discord.RawMessageDeleteEvent):
    guild = bot.get_guild(payload.guild_id) if payload.guild_id else None
    if not guild:
        return
    message = payload.cached_message
    channel = guild.get_channel(payload.channel_id)
    cache_snipe(guild, payload.channel_id, payload.message_id, message)
    config = await DB.get_config(guild.id)
    if not config["enabled"] or not config["event_types"].get("message_delete", False):
        return
    content = message.content if message else None
    author = message.author if message else None
    metadata = {
        "note": None if message else (
            "Message was not in the bot's cache; Discord did not provide its content or author."
        ),
        "avatar_url": author.display_avatar.url if author else None,
        "jump_url": message.jump_url if message else None,
        "message_created_at": message.created_at.isoformat() if message else None,
    }
    await persist_and_post(
        guild,
        "message_delete",
        message_id=payload.message_id,
        author_id=author.id if author else None,
        author_name=str(author) if author else None,
        channel_id=payload.channel_id,
        channel_name=getattr(channel, "name", None),
        content=content,
        attachments=attachments_from_message(message) if message else [],
        metadata=metadata,
    )


@bot.event
async def on_raw_bulk_message_delete(payload: discord.RawBulkMessageDeleteEvent):
    guild = bot.get_guild(payload.guild_id) if payload.guild_id else None
    if not guild:
        return
    channel = guild.get_channel(payload.channel_id)
    cached_messages = list(payload.cached_messages)
    if cached_messages:
        latest_cached = max(
            cached_messages,
            key=lambda message: (message.created_at, message.id),
        )
        cache_snipe(guild, payload.channel_id, latest_cached.id, latest_cached)
    elif payload.message_ids:
        cache_snipe(
            guild,
            payload.channel_id,
            max(payload.message_ids),
            None,
        )
    config = await DB.get_config(guild.id)
    if not config["enabled"] or not config["event_types"].get("bulk_delete", False):
        return
    cached = {message.id: message for message in payload.cached_messages}
    for message_id in payload.message_ids:
        message = cached.get(message_id)
        author = message.author if message else None
        await persist_and_post(
            guild,
            "bulk_delete",
            message_id=message_id,
            author_id=author.id if author else None,
            author_name=str(author) if author else None,
            channel_id=payload.channel_id,
            channel_name=getattr(channel, "name", None),
            content=message.content if message else None,
            attachments=attachments_from_message(message) if message else [],
            metadata={
                "note": None if message else "This message was not cached; content is unavailable.",
                "avatar_url": author.display_avatar.url if author else None,
                "jump_url": message.jump_url if message else None,
                "message_created_at": message.created_at.isoformat() if message else None,
                "batch_size": len(payload.message_ids),
            },
        )


@bot.event
async def on_raw_message_edit(payload: discord.RawMessageUpdateEvent):
    guild = bot.get_guild(payload.guild_id) if payload.guild_id else None
    if not guild:
        return
    before = payload.cached_message
    data = payload.data
    changes = verified_edit_changes(before, data)
    if changes is None:
        log.debug(
            "Ignoring message update without a verifiable content or attachment change "
            "(guild=%s, message=%s)",
            guild.id,
            payload.message_id,
        )
        return
    config = await DB.get_config(guild.id)
    if not config["enabled"] or not config["event_types"].get("message_edit", False):
        return
    author = before.author if before else None
    author_data = data.get("author") or {}
    author_id = author.id if author else author_data.get("id")
    author_name = str(author) if author else author_data.get("username")
    channel = guild.get_channel(payload.channel_id)
    avatar_url = author.display_avatar.url if author else None
    if changes["content_changed"] and changes["attachments_changed"]:
        change_note = "Message text and attachments changed."
    elif changes["attachments_changed"]:
        change_note = "Attachments changed; message text was unchanged."
    else:
        change_note = None
    await persist_and_post(
        guild,
        "message_edit",
        message_id=payload.message_id,
        author_id=int(author_id) if author_id else None,
        author_name=author_name,
        channel_id=payload.channel_id,
        channel_name=getattr(channel, "name", None),
        content=changes["content"],
        previous_content=changes["previous_content"],
        attachments=changes["attachments"],
        metadata={
            "note": change_note,
            "avatar_url": avatar_url,
            "jump_url": before.jump_url if before else None,
            "message_created_at": before.created_at.isoformat() if before else data.get("timestamp"),
        },
    )


@bot.event
async def on_member_join(member: discord.Member):
    config = await DB.get_config(member.guild.id)
    if config["enabled"] and config["event_types"].get("member_join", False):
        await persist_and_post(
            member.guild,
            "member_join",
            author_id=member.id,
            author_name=str(member),
            content=f"{member.mention} joined the server.",
            metadata={"avatar_url": member.display_avatar.url},
        )


@bot.event
async def on_member_remove(member: discord.Member):
    config = await DB.get_config(member.guild.id)
    if config["enabled"] and config["event_types"].get("member_leave", False):
        await persist_and_post(
            member.guild,
            "member_leave",
            author_id=member.id,
            author_name=str(member),
            content=f"{discord.utils.escape_markdown(str(member))} left the server.",
            metadata={"avatar_url": member.display_avatar.url},
        )


@bot.event
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        message = "You need **Manage Server** or **Administrator** to use that command."
    elif isinstance(error, app_commands.CommandOnCooldown):
        message = "That command is being used too quickly. Please try again shortly."
    else:
        log.exception("Application command failed", exc_info=error)
        message = "That command failed unexpectedly. Please try again; the bot operator can check the logs."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except discord.HTTPException:
        log.exception("Could not return an application-command error response")


# Groups are registered after their subcommands have been defined.
for command_group in (set_group, create_group, remove_group, logs_group, message_group, member_group):
    bot.tree.add_command(command_group)


@bot.event
async def on_ready():
    if not getattr(bot, "_messagelog_synced", False):
        try:
            synced = await bot.tree.sync()
            bot._messagelog_synced = True
            log.info("Registered %d global application commands", len(synced))
        except discord.HTTPException:
            log.exception("Command registration failed")
    global _feedback_views_loaded
    try:
        if not _feedback_views_loaded:
            for row in await DB.get_pending_feedback_replies():
                bot.add_view(
                    FeedbackReplyView(row["id"]),
                    message_id=row["delivery_message_id"],
                )
            _feedback_views_loaded = True
        for row in await DB.get_undelivered_feedback():
            try:
                await deliver_feedback(row["id"])
            except FeedbackDeliveryError as exc:
                log.warning(
                    "Feedback #%s remains saved but undelivered: %s",
                    row["id"],
                    exc,
                )
    except Exception:
        log.exception("Feedback delivery recovery failed")
    log.info("Logged in as %s (id=%s)", bot.user, bot.user.id if bot.user else "unknown")


async def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            "DISCORD_BOT_TOKEN is missing. Copy .env.example to .env and set the token "
            "in your local environment, or add it as a Replit Secret."
        )
    await DB.initialize()
    async with bot:
        await bot.start(BOT_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
