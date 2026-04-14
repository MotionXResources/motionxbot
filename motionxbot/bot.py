from __future__ import annotations

import re
import traceback
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Tuple
from uuid import uuid4

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

from .config import load_config
from .health import start_health_server
from .rendering import build_base_placeholders, parse_assignments, render_template_text
from .store import Store
from .time_utils import format_duration, parse_duration, render_discord_timestamp
from .transfer import (
    HARD_LIMIT,
    PROGRESS_UPDATE_INTERVAL,
    build_summary,
    collect_forum_threads,
    collect_messages,
    copy_forum_to_thread,
    copy_forum_thread,
    copy_thread_to_forum,
    copy_thread_to_channel,
    filter_messages_for_transfer,
    get_effective_attachments,
    get_message_creator_id,
    is_audio_attachment,
    repost_message,
)


def now_ms() -> int:
    return int(datetime.now(tz=timezone.utc).timestamp() * 1000)


def make_id() -> str:
    return uuid4().hex[:8]


CREATOR_CAPTION_RE = re.compile(r"^created by <@!?(\d+)>$", re.IGNORECASE)
CHANNEL_TIMER_WARNING_STEPS = [
    7 * 24 * 60 * 60 * 1000,
    24 * 60 * 60 * 1000,
    6 * 60 * 60 * 1000,
    60 * 60 * 1000,
    30 * 60 * 1000,
    10 * 60 * 1000,
    5 * 60 * 1000,
    60 * 1000,
]


def parse_duration_list(raw: Optional[str]) -> Optional[list[int]]:
    if raw is None:
        return None

    values: list[int] = []
    for part in raw.split(","):
        parsed = parse_duration(part.strip())
        if parsed is None:
            return None
        values.append(parsed)

    deduped = sorted({value for value in values if value > 0}, reverse=True)
    return deduped


class MotionXBot(commands.Bot):
    def __init__(self) -> None:
        self.config = load_config()
        intents = discord.Intents.default()
        intents.guilds = True
        intents.guild_messages = True
        intents.message_content = True
        intents.members = True
        intents.dm_messages = True
        super().__init__(command_prefix="!", intents=intents)

        self.store = Store(self.config.store_path)
        self.http_session: Optional[aiohttp.ClientSession] = None
        self.health_runner = None
        self.started_at = 0.0
        self.commands_synced = False

    async def setup_hook(self) -> None:
        if not self.config.token:
            raise RuntimeError("Missing DISCORD_TOKEN in your environment.")

        self.http_session = aiohttp.ClientSession()
        self.health_runner = await start_health_server(self, self.config.port)
        self.register_app_commands()

        if not self.scheduler.is_running():
            self.scheduler.start()

    async def close(self) -> None:
        if self.scheduler.is_running():
            self.scheduler.cancel()
        if self.health_runner is not None:
            await self.health_runner.cleanup()
        if self.http_session is not None:
            await self.http_session.close()
        await super().close()

    async def on_ready(self) -> None:
        self.started_at = self.loop.time()
        print(f"Logged in as {self.user}")
        if self.user:
            await self.change_presence(
                activity=discord.Activity(
                    type=discord.ActivityType.watching,
                    name=self.config.bot_status,
                )
            )

        if not self.commands_synced:
            if self.config.guild_id:
                guild = discord.Object(id=int(self.config.guild_id))
                self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
                print(f"Synced {len(synced)} guild commands to {self.config.guild_id}")
            else:
                synced = await self.tree.sync()
                print(f"Synced {len(synced)} global commands")
            self.commands_synced = True

    async def on_member_join(self, member: discord.Member) -> None:
        guild_data = self.store.get_guild_data(member.guild.id)
        if not guild_data["autoRoles"]:
            return

        applied_roles: list[str] = []
        for role_id in guild_data["autoRoles"]:
            role = member.guild.get_role(int(role_id))
            if role is None:
                continue
            try:
                await member.add_roles(role, reason="MotionXBot autorole")
                applied_roles.append(role.name)
            except discord.HTTPException:
                continue

        if applied_roles:
            await self.log_to_guild(
                member.guild.id,
                f"Auto-role applied to {member}: {', '.join(applied_roles)}",
            )
            self.store.save()

    async def log_to_guild(self, guild_id: int, message: str) -> None:
        guild = self.get_guild(guild_id)
        if guild is None:
            return

        guild_data = self.store.get_guild_data(guild_id)
        channel_id = guild_data.get("logChannelId")
        if not channel_id:
            return

        channel = await self.resolve_text_channel(guild, int(channel_id))
        if channel is None:
            return

        try:
            await channel.send(message, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            return

    async def resolve_text_channel(
        self, guild: discord.Guild, channel_id: int
    ) -> Optional[discord.abc.Messageable]:
        channel = guild.get_channel(channel_id) or guild.get_thread(channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(channel_id)
            except discord.HTTPException:
                return None

        if isinstance(channel, (discord.TextChannel, discord.Thread)):
            return channel
        return None

    async def resolve_timer_target(self, guild: discord.Guild, channel_id: int) -> Optional[Any]:
        channel = guild.get_channel(channel_id) or guild.get_thread(channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(channel_id)
            except discord.HTTPException:
                return None

        if isinstance(channel, (discord.TextChannel, discord.Thread, discord.ForumChannel)):
            return channel
        return None

    def get_timer_target_from_context(self, channel: Any) -> Optional[Any]:
        if isinstance(channel, (discord.TextChannel, discord.Thread, discord.ForumChannel)):
            return channel
        return None

    def build_channel_timer_warning_offsets(
        self,
        duration_ms: int,
        custom_steps: Optional[str] = None,
    ) -> list[int]:
        if custom_steps is not None:
            parsed_steps = parse_duration_list(custom_steps)
            if parsed_steps is None:
                return []
            steps = parsed_steps
        else:
            steps = CHANNEL_TIMER_WARNING_STEPS
        return [step for step in steps if 0 < step < duration_ms]

    def build_channel_timer_placeholders(
        self,
        guild: discord.Guild,
        channel: Any,
        delete_at_ms: int,
        remaining_ms: int,
    ) -> dict[str, str]:
        placeholders = build_base_placeholders(guild=guild, channel=channel, user=self.user)
        placeholders.update(
            {
                "remaining": format_duration(max(remaining_ms, 0)),
                "delete_at": render_discord_timestamp(delete_at_ms),
                "channel_mention": getattr(channel, "mention", f"#{getattr(channel, 'name', 'channel')}"),
                "channel_name": getattr(channel, "name", "channel"),
            }
        )
        return placeholders

    async def send_channel_timer_warning(
        self,
        guild: discord.Guild,
        channel: Any,
        timer: dict[str, Any],
        remaining_ms: int,
    ) -> None:
        template = str(
            timer.get("warningMessage")
            or "{channel_mention} will be deleted in {remaining} at {delete_at}."
        )
        content = render_template_text(
            template,
            self.build_channel_timer_placeholders(guild, channel, int(timer["deleteAt"]), remaining_ms),
        )

        if isinstance(channel, (discord.TextChannel, discord.Thread)):
            try:
                await channel.send(content, allowed_mentions=discord.AllowedMentions.none())
                return
            except discord.HTTPException:
                pass

        await self.log_to_guild(
            guild.id,
            f"Timer warning for {getattr(channel, 'mention', timer['channelId'])}: {content}",
        )

    async def delete_scheduled_channel(
        self,
        guild: discord.Guild,
        channel: Any,
        timer: dict[str, Any],
    ) -> None:
        placeholders = self.build_channel_timer_placeholders(guild, channel, int(timer["deleteAt"]), 0)
        final_note = str(timer.get("finalNote") or "").strip()
        channel_label = getattr(channel, "mention", f"#{getattr(channel, 'name', timer['channelId'])}")
        reason = str(timer.get("reason") or "MotionXBot scheduled deletion")

        if final_note:
            await self.log_to_guild(
                guild.id,
                render_template_text(final_note, placeholders),
            )

        await channel.delete(reason=reason[:512])
        await self.log_to_guild(
            guild.id,
            f"Deleted {channel_label} on schedule set by <@{timer['createdBy']}>.",
        )

    async def collect_count_sources(
        self,
        guild: discord.Guild,
        *,
        source_channel: Optional[discord.TextChannel] = None,
        source_thread: Optional[discord.Thread] = None,
        source_forum: Optional[discord.ForumChannel] = None,
    ) -> list[Any]:
        if source_thread is not None:
            return [source_thread]
        if source_channel is not None:
            return [source_channel]
        if source_forum is not None:
            return await collect_forum_threads(source_forum, None)

        sources: list[Any] = []
        seen_ids: set[int] = set()

        def add_source(item: Any) -> None:
            item_id = getattr(item, "id", None)
            if item_id is None or item_id in seen_ids:
                return
            seen_ids.add(item_id)
            sources.append(item)

        for channel in sorted(guild.text_channels, key=lambda item: (item.position, item.id)):
            add_source(channel)

        for thread in sorted(guild.threads, key=lambda item: item.id):
            add_source(thread)

        for channel in guild.channels:
            if not isinstance(channel, (discord.TextChannel, discord.ForumChannel)):
                continue
            try:
                async for thread in channel.archived_threads(limit=None):
                    add_source(thread)
            except (AttributeError, discord.Forbidden, discord.HTTPException):
                continue

        return sources

    def describe_check_scope(
        self,
        *,
        source_channel: Optional[discord.TextChannel] = None,
        source_thread: Optional[discord.Thread] = None,
        source_forum: Optional[discord.ForumChannel] = None,
    ) -> str:
        if source_thread is not None:
            return f"in {source_thread.mention}"
        if source_channel is not None:
            return f"in {source_channel.mention}"
        if source_forum is not None:
            return f"in {source_forum.mention}"
        return "across the server"

    async def count_messages_by_author(
        self,
        guild: discord.Guild,
        *,
        source_channel: Optional[discord.TextChannel] = None,
        source_thread: Optional[discord.Thread] = None,
        source_forum: Optional[discord.ForumChannel] = None,
        target_user_id: Optional[int] = None,
        since_ms: Optional[int] = None,
        interaction: Optional[discord.Interaction] = None,
    ) -> tuple[dict[int, int], int, int]:
        sources = await self.collect_count_sources(
            guild,
            source_channel=source_channel,
            source_thread=source_thread,
            source_forum=source_forum,
        )
        counts: dict[int, int] = {}
        scanned_messages = 0
        after_time = (
            datetime.now(tz=timezone.utc) - timedelta(milliseconds=since_ms)
            if since_ms is not None
            else None
        )
        last_progress_at = 0.0

        for index, source in enumerate(sources, start=1):
            try:
                async for message in source.history(limit=None, after=after_time, oldest_first=False):
                    scanned_messages += 1
                    if target_user_id is not None and message.author.id != target_user_id:
                        continue
                    counts[message.author.id] = counts.get(message.author.id, 0) + 1

                    if (
                        interaction is not None
                        and self.loop.time() - last_progress_at >= 2.5
                    ):
                        last_progress_at = self.loop.time()
                        await interaction.edit_original_response(
                            content=(
                                f"Checking messages {self.describe_check_scope(source_channel=source_channel, source_thread=source_thread, source_forum=source_forum)}...\n"
                                f"Sources scanned: {index}/{len(sources)}\n"
                                f"Messages scanned: {scanned_messages}\n"
                                f"Matches: {sum(counts.values())}"
                            )
                        )
            except (discord.Forbidden, discord.HTTPException):
                continue

        return counts, scanned_messages, len(sources)

    def record_dm_log(
        self,
        *,
        user_id: int,
        direction: str,
        content: str,
        actor_id: Optional[int] = None,
        context: Optional[str] = None,
    ) -> None:
        entry = {
            "id": make_id(),
            "userId": str(user_id),
            "direction": direction,
            "content": content,
            "actorId": str(actor_id) if actor_id is not None else None,
            "context": context,
            "createdAt": now_ms(),
        }
        self.store.append_dm_log(entry)

    def build_forum_post_name(self, message: str, fallback: str = "Bot Whisper") -> str:
        first_line = (message.strip().splitlines()[0] if message.strip() else fallback).strip()
        return (first_line[:100] or fallback)[:100]

    def get_category_targets(
        self,
        category: discord.CategoryChannel,
    ) -> list[discord.abc.GuildChannel]:
        return sorted(
            [
                channel
                for channel in category.guild.channels
                if getattr(channel, "category_id", None) == category.id
                and isinstance(channel, (discord.TextChannel, discord.ForumChannel))
            ],
            key=lambda item: (item.position, item.id),
        )

    async def send_whisper_to_target(
        self,
        *,
        source_guild: discord.Guild,
        content: str,
        actor: discord.abc.User,
        target_channel: Optional[discord.TextChannel] = None,
        target_thread: Optional[discord.Thread] = None,
        target_forum: Optional[discord.ForumChannel] = None,
        target_category: Optional[discord.CategoryChannel] = None,
        target_user: Optional[discord.User] = None,
        title: Optional[str] = None,
    ) -> tuple[int, list[str]]:
        delivered_labels: list[str] = []
        payload_title = title or self.build_forum_post_name(content, fallback=f"Whisper from {actor.display_name}")

        if target_channel is not None:
            await target_channel.send(content, allowed_mentions=discord.AllowedMentions.none())
            delivered_labels.append(target_channel.mention)
            return 1, delivered_labels

        if target_thread is not None:
            await target_thread.send(content, allowed_mentions=discord.AllowedMentions.none())
            delivered_labels.append(target_thread.mention)
            return 1, delivered_labels

        if target_forum is not None:
            created = await target_forum.create_thread(
                name=payload_title,
                content=content,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            delivered_labels.append(created.thread.mention)
            return 1, delivered_labels

        if target_user is not None:
            await target_user.send(content, allowed_mentions=discord.AllowedMentions.none())
            self.record_dm_log(
                user_id=target_user.id,
                direction="outbound",
                content=content,
                actor_id=actor.id,
                context="whisper",
            )
            delivered_labels.append(f"DM to {target_user}")
            return 1, delivered_labels

        if target_category is not None:
            delivered = 0
            for channel in self.get_category_targets(target_category):
                if isinstance(channel, discord.TextChannel):
                    await channel.send(content, allowed_mentions=discord.AllowedMentions.none())
                    delivered += 1
                    delivered_labels.append(channel.mention)
                elif isinstance(channel, discord.ForumChannel):
                    created = await channel.create_thread(
                        name=payload_title,
                        content=content,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    delivered += 1
                    delivered_labels.append(created.thread.mention)
            return delivered, delivered_labels

        raise RuntimeError("No whisper target was provided.")

    def build_whisper_log_entry(
        self,
        *,
        actor_id: int,
        content: str,
        delivered_labels: list[str],
        title: Optional[str],
    ) -> dict[str, Any]:
        return {
            "id": make_id(),
            "actorId": str(actor_id),
            "content": content,
            "targets": delivered_labels,
            "title": title,
            "createdAt": now_ms(),
        }

    def matches_auto_response(self, message: discord.Message, rule: dict[str, Any]) -> bool:
        content = (message.content or "").strip().lower()
        trigger = str(rule.get("trigger") or "").strip().lower()
        if not content or not trigger:
            return False
        channel_id = rule.get("channelId")
        if channel_id and str(message.channel.id) != str(channel_id):
            return False
        match_mode = str(rule.get("matchMode") or "contains")
        if match_mode == "exact":
            return content == trigger
        if match_mode == "starts_with":
            return content.startswith(trigger)
        return trigger in content

    async def ensure_permissions(
        self,
        interaction: discord.Interaction,
        label: str,
        **permissions: bool,
    ) -> bool:
        perms = interaction.permissions
        if perms is not None and all(getattr(perms, name, False) == value for name, value in permissions.items()):
            return True

        await self.reply_ephemeral(
            interaction,
            f"You need the right Discord permissions to use {label}.",
        )
        return False

    async def reply_ephemeral(self, interaction: discord.Interaction, content: str) -> None:
        if interaction.response.is_done():
            await interaction.followup.send(content, ephemeral=True)
        else:
            await interaction.response.send_message(content, ephemeral=True)

    async def defer_ephemeral(self, interaction: discord.Interaction) -> None:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)

    def extract_discord_id(self, raw_value: str) -> Optional[int]:
        stripped = raw_value.strip()
        if stripped.isdigit():
            return int(stripped)

        digit_groups = re.findall(r"\d+", stripped)
        return int(digit_groups[-1]) if digit_groups else None

    async def resolve_thread_reference(
        self,
        guild: discord.Guild,
        raw_value: str,
    ) -> Optional[discord.Thread]:
        thread_id = self.extract_discord_id(raw_value.strip())
        if thread_id is None:
            return None

        thread = guild.get_thread(thread_id)
        if isinstance(thread, discord.Thread):
            return thread

        channel = guild.get_channel(thread_id)
        if isinstance(channel, discord.Thread):
            return channel

        try:
            channel = await self.fetch_channel(thread_id)
        except discord.HTTPException:
            return None

        return channel if isinstance(channel, discord.Thread) else None

    def get_default_audio_search_source(
        self, channel: Any
    ) -> Optional[discord.abc.Messageable]:
        if isinstance(channel, (discord.TextChannel, discord.Thread)):
            return channel
        return None

    def describe_audio_location(self, message: discord.Message) -> str:
        channel = message.channel
        if isinstance(channel, discord.Thread):
            parent = channel.parent.mention if channel.parent else "#forum"
            return f"{parent} / {channel.mention}"
        if isinstance(channel, discord.TextChannel):
            return channel.mention
        return str(channel)

    def collect_audio_matches(
        self,
        messages: list[discord.Message],
        query: str,
    ) -> list[tuple[discord.Message, discord.Attachment, Optional[int]]]:
        normalized_query = query.strip().lower()
        matches: list[tuple[discord.Message, discord.Attachment, Optional[int]]] = []
        for index in range(len(messages) - 1, -1, -1):
            message = messages[index]
            creator_id = self.resolve_audio_creator_id(messages, index)
            for attachment in get_effective_attachments(message):
                if not is_audio_attachment(attachment):
                    continue
                if normalized_query not in attachment.filename.lower():
                    continue
                matches.append((message, attachment, creator_id))
        return matches

    def resolve_audio_creator_id(
        self,
        messages: list[discord.Message],
        index: int,
    ) -> Optional[int]:
        message = messages[index]
        direct_creator_id = get_message_creator_id(message)
        if not message.author.bot:
            return direct_creator_id

        for next_index in range(index + 1, min(len(messages), index + 8)):
            next_message = messages[next_index]
            caption_match = CREATOR_CAPTION_RE.fullmatch((next_message.content or "").strip())
            if caption_match:
                return int(caption_match.group(1))

            has_audio = any(is_audio_attachment(attachment) for attachment in get_effective_attachments(next_message))
            if next_message.author.bot and has_audio and not (next_message.content or "").strip():
                continue
            if not get_effective_attachments(next_message) and not (next_message.content or "").strip():
                continue
            break

        return direct_creator_id

    def build_audio_result_embed(
        self,
        query: str,
        match: tuple[discord.Message, discord.Attachment, Optional[int]],
        index: int,
        total: int,
    ) -> discord.Embed:
        message, attachment, creator_id = match
        channel = message.channel
        location_label = "Thread" if isinstance(channel, discord.Thread) else "Channel"
        creator_value = f"<@{creator_id}>" if creator_id else message.author.mention
        embed = discord.Embed(
            title=attachment.filename,
            description=f"Audio search result for `{query}`",
            color=discord.Color.blurple(),
            timestamp=message.created_at,
        )
        embed.add_field(name="Creator", value=creator_value, inline=True)
        embed.add_field(name=location_label, value=self.describe_audio_location(message), inline=True)
        embed.add_field(
            name="Posted",
            value=render_discord_timestamp(int(message.created_at.timestamp() * 1000)),
            inline=True,
        )
        embed.set_footer(text=f"Result {index} of {total}")
        if message.author.display_avatar:
            embed.set_author(
                name=str(message.author),
                icon_url=message.author.display_avatar.url,
            )
        return embed

    def build_audio_result_view(
        self,
        match: tuple[discord.Message, discord.Attachment, Optional[int]],
    ) -> discord.ui.View:
        message, attachment, _creator_id = match
        channel = message.channel
        open_label = "Open Thread" if isinstance(channel, discord.Thread) else "Open Message"
        view = discord.ui.View()
        view.add_item(
            discord.ui.Button(
                label="Download Audio",
                style=discord.ButtonStyle.link,
                url=attachment.url,
            )
        )
        view.add_item(
            discord.ui.Button(
                label=open_label,
                style=discord.ButtonStyle.link,
                url=message.jump_url,
            )
        )
        return view

    async def send_audio_search_results_interaction(
        self,
        interaction: discord.Interaction,
        query: str,
        matches: list[tuple[discord.Message, discord.Attachment, Optional[int]]],
    ) -> None:
        total = len(matches)
        first_embed = self.build_audio_result_embed(query, matches[0], 1, total)
        first_view = self.build_audio_result_view(matches[0])

        if interaction.response.is_done():
            await interaction.edit_original_response(content=None, embed=first_embed, view=first_view)
        else:
            await interaction.response.send_message(embed=first_embed, view=first_view, ephemeral=True)

        for index, match in enumerate(matches[1:], start=2):
            await interaction.followup.send(
                embed=self.build_audio_result_embed(query, match, index, total),
                view=self.build_audio_result_view(match),
                ephemeral=True,
            )

    async def send_audio_search_results_message(
        self,
        message: discord.Message,
        query: str,
        matches: list[tuple[discord.Message, discord.Attachment, Optional[int]]],
    ) -> None:
        total = len(matches)
        for index, match in enumerate(matches, start=1):
            await message.channel.send(
                embed=self.build_audio_result_embed(query, match, index, total),
                view=self.build_audio_result_view(match),
                allowed_mentions=discord.AllowedMentions.none(),
            )

    async def search_audio_in_source(
        self,
        source_obj: discord.abc.Messageable,
        query: str,
        *,
        include_bots: bool,
        interaction: Optional[discord.Interaction] = None,
    ) -> list[tuple[discord.Message, discord.Attachment, Optional[int]]]:
        normalized_query = query.strip()
        matches: list[tuple[discord.Message, discord.Attachment, Optional[int]]] = []
        last_progress_at = 0.0

        if isinstance(source_obj, (discord.TextChannel, discord.Thread)):
            source_label = source_obj.mention

            async def progress(scanned: int, queued: int) -> None:
                del queued
                nonlocal last_progress_at
                if interaction is None or self.loop.time() - last_progress_at < 2.5:
                    return
                last_progress_at = self.loop.time()
                await interaction.edit_original_response(
                    content=f"Scanning {source_label} for audio named `{normalized_query}`...\nScanned: {scanned}"
                )

            messages = await collect_messages(
                source_obj,
                all_messages=True,
                limit=None,
                before=None,
                include_bots=include_bots,
                on_progress=progress if interaction is not None else None,
            )
            matches = self.collect_audio_matches(messages, normalized_query)
        elif isinstance(source_obj, discord.ForumChannel):
            async def forum_progress(found: int, archived_scanned: int) -> None:
                nonlocal last_progress_at
                if interaction is None or self.loop.time() - last_progress_at < 2.5:
                    return
                last_progress_at = self.loop.time()
                await interaction.edit_original_response(
                    content=(
                        f"Scanning forum {source_obj.mention} for audio named `{normalized_query}`...\n"
                        f"Threads found: {found}\nArchived scanned: {archived_scanned}"
                    )
                )

            threads = await collect_forum_threads(source_obj, forum_progress if interaction is not None else None)
            scanned_threads = 0
            scanned_messages = 0
            for thread in threads:
                thread_messages = await collect_messages(
                    thread,
                    all_messages=True,
                    limit=None,
                    before=None,
                    include_bots=include_bots,
                    on_progress=None,
                )
                scanned_threads += 1
                scanned_messages += len(thread_messages)
                matches.extend(self.collect_audio_matches(thread_messages, normalized_query))
                if interaction is not None and self.loop.time() - last_progress_at >= 2.5:
                    last_progress_at = self.loop.time()
                    await interaction.edit_original_response(
                        content=(
                            f"Searching forum {source_obj.mention} for audio named `{normalized_query}`...\n"
                            f"Threads scanned: {scanned_threads}/{len(threads)}\n"
                            f"Messages scanned: {scanned_messages}\n"
                            f"Matches found: {len(matches)}"
                        )
                    )

        matches.sort(key=lambda item: item[0].created_at, reverse=True)
        return matches

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return

        normalized = message.content.strip()
        lowered = normalized.lower()
        if message.guild is None:
            self.record_dm_log(
                user_id=message.author.id,
                direction="inbound",
                content=message.content,
                context="dm",
            )

        if lowered == "mtxaudios" or lowered.startswith("mtxaudios "):
            query = normalized[len("mtxaudios") :].strip()
            if not query:
                await message.reply(
                    "Use `mtxaudios your-audio-name` in the channel or thread you want to search.",
                    mention_author=False,
                )
            elif message.guild is None:
                await message.reply(
                    "This command only works in a server.",
                    mention_author=False,
                )
            else:
                source_obj = self.get_default_audio_search_source(message.channel)
                if source_obj is None:
                    await message.reply(
                        "Run `mtxaudios ...` inside a text channel or thread.",
                        mention_author=False,
                    )
                else:
                    matches = await self.search_audio_in_source(
                        source_obj,
                        query,
                        include_bots=True,
                    )
                    if not matches:
                        await message.reply(
                            f"No audio attachments matching `{query}` were found here.",
                            mention_author=False,
                        )
                    else:
                        await self.send_audio_search_results_message(message, query, matches[:5])

        if message.guild is not None:
            guild_data = self.store.get_guild_data(message.guild.id)
            for rule in guild_data.get("autoResponses", []):
                if not rule.get("enabled", True):
                    continue
                if not self.matches_auto_response(message, rule):
                    continue
                try:
                    await message.channel.send(
                        str(rule.get("response") or ""),
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                except discord.HTTPException:
                    pass
                break

        await self.process_commands(message)

    @tasks.loop(seconds=15)
    async def scheduler(self) -> None:
        for guild_id in list(self.store.data["guilds"].keys()):
            guild = self.get_guild(int(guild_id))
            if guild is None:
                continue

            guild_data = self.store.get_guild_data(guild_id)
            current_ms = now_ms()
            await self.process_reminders(guild, guild_data, current_ms)
            await self.process_jobs(guild, guild_data, current_ms)
            await self.process_heartbeat(guild, guild_data, current_ms)
            await self.process_channel_timers(guild, guild_data, current_ms)

        self.store.save()

    @scheduler.before_loop
    async def before_scheduler(self) -> None:
        await self.wait_until_ready()

    async def process_reminders(self, guild: discord.Guild, guild_data: dict[str, Any], current_ms: int) -> None:
        due = [item for item in guild_data["reminders"] if item["dueAt"] <= current_ms]
        if not due:
            return

        for reminder in due:
            try:
                if reminder["delivery"] == "dm":
                    user = await self.fetch_user(int(reminder["userId"]))
                    await user.send(f"Reminder: {reminder['message']}")
                else:
                    channel = await self.resolve_text_channel(guild, int(reminder["channelId"]))
                    if channel is not None:
                        await channel.send(
                            f"<@{reminder['userId']}> reminder: {reminder['message']}",
                            allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
                        )
            except discord.HTTPException as error:
                reminder["dueAt"] = current_ms + 15 * 60 * 1000
                reminder["lastError"] = str(error)
                continue

            guild_data["reminders"] = [
                item for item in guild_data["reminders"] if item["id"] != reminder["id"]
            ]
            await self.log_to_guild(
                guild.id,
                f"Reminder delivered for <@{reminder['userId']}>: {reminder['message']}",
            )

    async def process_jobs(self, guild: discord.Guild, guild_data: dict[str, Any], current_ms: int) -> None:
        for job in guild_data["jobs"]:
            if not job.get("enabled") or job["nextRunAt"] > current_ms:
                continue

            channel = await self.resolve_text_channel(guild, int(job["channelId"]))
            if channel is None:
                job["nextRunAt"] = current_ms + int(job["intervalMs"])
                continue

            placeholders = build_base_placeholders(guild=guild, channel=channel, user=self.user)
            try:
                await channel.send(
                    render_template_text(job["message"], placeholders),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException:
                continue

            job["lastRunAt"] = current_ms
            job["nextRunAt"] = current_ms + int(job["intervalMs"])
            await self.log_to_guild(guild.id, f"Scheduled job `{job['name']}` ran in {channel}.")

    async def process_heartbeat(
        self, guild: discord.Guild, guild_data: dict[str, Any], current_ms: int
    ) -> None:
        heartbeat = guild_data.get("heartbeat")
        if not heartbeat or not heartbeat.get("enabled") or heartbeat["nextRunAt"] > current_ms:
            return

        channel = await self.resolve_text_channel(guild, int(heartbeat["channelId"]))
        if channel is None:
            heartbeat["nextRunAt"] = current_ms + int(heartbeat["intervalMs"])
            return

        placeholders = build_base_placeholders(guild=guild, channel=channel, user=self.user)
        try:
            await channel.send(
                render_template_text(heartbeat["message"], placeholders),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            return

        heartbeat["lastRunAt"] = current_ms
        heartbeat["nextRunAt"] = current_ms + int(heartbeat["intervalMs"])

    async def process_channel_timers(
        self,
        guild: discord.Guild,
        guild_data: dict[str, Any],
        current_ms: int,
    ) -> None:
        timers = list(guild_data.get("channelTimers") or [])
        if not timers:
            return

        remaining_timers: list[dict[str, Any]] = []
        for timer in timers:
            channel = await self.resolve_timer_target(guild, int(timer["channelId"]))
            if channel is None:
                continue

            delete_at = int(timer["deleteAt"])
            if timer.get("deleteIfEmpty"):
                try:
                    latest_message = None
                    async for candidate in channel.history(limit=1):
                        latest_message = candidate
                    if latest_message is not None and latest_message.created_at.timestamp() * 1000 > int(timer["createdAt"]):
                        await self.log_to_guild(
                            guild.id,
                            f"Cancelled idle-only deletion for {getattr(channel, 'mention', timer['channelId'])} because new activity was detected.",
                        )
                        continue
                except (AttributeError, discord.Forbidden, discord.HTTPException):
                    remaining_timers.append(timer)
                    continue

            remaining_ms = delete_at - current_ms
            warned_offsets = {int(offset) for offset in timer.get("warnedOffsetsMs") or []}
            for offset in sorted((int(value) for value in timer.get("warningOffsetsMs") or []), reverse=True):
                if offset in warned_offsets or remaining_ms > offset or remaining_ms <= 0:
                    continue
                try:
                    await self.send_channel_timer_warning(guild, channel, timer, remaining_ms)
                    warned_offsets.add(offset)
                except discord.HTTPException:
                    continue

            timer["warnedOffsetsMs"] = sorted(warned_offsets, reverse=True)

            if current_ms < delete_at:
                remaining_timers.append(timer)
                continue

            try:
                await self.delete_scheduled_channel(guild, channel, timer)
            except discord.HTTPException as error:
                timer["deleteAt"] = current_ms + 15 * 60 * 1000
                timer["lastError"] = str(error)
                remaining_timers.append(timer)

        guild_data["channelTimers"] = remaining_timers

    def register_app_commands(self) -> None:
        tree = self.tree

        @tree.error
        async def on_app_command_error(
            interaction: discord.Interaction,
            error: app_commands.AppCommandError,
        ) -> None:
            command_name = interaction.command.qualified_name if interaction.command else "unknown"
            print(f"App command error in /{command_name}: {error}")
            traceback.print_exception(type(error), error, error.__traceback__)

            if isinstance(error, app_commands.CheckFailure):
                message = "You do not have permission to use that command here."
            elif isinstance(error, app_commands.CommandOnCooldown):
                message = "That command is on cooldown right now. Try again in a moment."
            else:
                message = f"That command failed: {error}"

            try:
                await self.reply_ephemeral(interaction, message[:1900])
            except discord.HTTPException:
                pass

        @app_commands.command(
            name="automation-help",
            description="See the bot's automation command categories.",
        )
        async def automation_help(interaction: discord.Interaction) -> None:
            lines = [
                "**Scheduling:** `/reminder`, `/job`, `/heartbeat`",
                "**Reusable content:** `/tag`, `/template`",
                "**Operational tracking:** `/checklist`, `/todo`, `/approval`, `/check`, `/warn`, `/note`, `/dmlog`",
                "**Server automation:** `/autorole`, `/bulkrole`, `/channel`, `/cleanup`, `/logchannel`, `/transfer messages`, `/transfer all`, `/transfer forum`, `/transfer thread`, `/audio search`, `/whisper`, `/autoresponse`, `/timeout`, `mtxaudios <query>`",
                "**Bot status:** `/botstatus`",
                "",
                "Most time fields accept compact durations like `15m`, `2h`, `1d`, or `1h30m`.",
                "Use `/channel delete-in` to put a timed warning + deletion countdown on the current channel or thread.",
                "Transfer commands also support `mp3_only` and `audio_only` toggles for file-only audio copies.",
            ]
            await self.reply_ephemeral(interaction, "\n".join(lines))

        @app_commands.command(
            name="botstatus",
            description="Show a quick automation snapshot for this server.",
        )
        async def botstatus(interaction: discord.Interaction) -> None:
            if interaction.guild is None:
                await self.reply_ephemeral(interaction, "This command can only be used in a server.")
                return

            guild_data = self.store.get_guild_data(interaction.guild.id)
            lines = [
                f"Reminders queued: {len(guild_data['reminders'])}",
                f"Jobs configured: {len(guild_data['jobs'])}",
                f"Tags saved: {len(guild_data['tags'])}",
                f"Templates saved: {len(guild_data['templates'])}",
                f"Checklists saved: {len(guild_data['checklists'])}",
                f"Todos tracked: {len(guild_data['todos'])}",
                f"Approval requests: {len(guild_data['approvals'])}",
                f"Warnings logged: {len(guild_data['warnings'])}",
                f"Mod notes: {len(guild_data['modNotes'])}",
                f"Autoresponses: {len(guild_data['autoResponses'])}",
                f"Whispers logged: {len(guild_data['whispers'])}",
                f"Autoroles: {len(guild_data['autoRoles'])}",
                f"Channel timers: {len(guild_data['channelTimers'])}",
                f"Heartbeat: {'configured' if guild_data['heartbeat'] else 'off'}",
                f"Log channel: <#{guild_data['logChannelId']}>" if guild_data["logChannelId"] else "Log channel: not set",
            ]
            await self.reply_ephemeral(interaction, "\n".join(lines))

        reminder_group = app_commands.Group(name="reminder", description="Manage one-time reminders.")

        @reminder_group.command(name="create", description="Create a reminder.")
        @app_commands.rename(delay="in")
        async def reminder_create(
            interaction: discord.Interaction,
            message: str,
            delay: str,
            dm: bool = False,
        ) -> None:
            if interaction.guild is None or interaction.channel is None:
                await self.reply_ephemeral(interaction, "This command can only be used in a server.")
                return

            delay_ms = parse_duration(delay)
            if delay_ms is None:
                await self.reply_ephemeral(
                    interaction,
                    "I couldn't parse that delay. Use values like `15m`, `2h`, or `1d`.",
                )
                return

            guild_data = self.store.get_guild_data(interaction.guild.id)
            due_at = now_ms() + delay_ms
            reminder = {
                "id": make_id(),
                "userId": str(interaction.user.id),
                "channelId": str(interaction.channel.id),
                "message": message,
                "delivery": "dm" if dm else "channel",
                "dueAt": due_at,
                "createdAt": now_ms(),
            }
            guild_data["reminders"].append(reminder)
            self.store.save()
            await self.reply_ephemeral(
                interaction,
                f"Reminder `{reminder['id']}` queued for {render_discord_timestamp(due_at)} ({format_duration(delay_ms)}).",
            )

        @reminder_group.command(name="list", description="List active reminders.")
        async def reminder_list(interaction: discord.Interaction) -> None:
            if interaction.guild is None:
                await self.reply_ephemeral(interaction, "This command can only be used in a server.")
                return

            guild_data = self.store.get_guild_data(interaction.guild.id)
            reminders = [
                item
                for item in guild_data["reminders"]
                if str(item["userId"]) == str(interaction.user.id)
            ]
            reminders.sort(key=lambda item: item["dueAt"])
            if not reminders:
                await self.reply_ephemeral(interaction, "You do not have any active reminders.")
                return

            lines = [
                f"`{item['id']}` - {render_discord_timestamp(item['dueAt'])} - {item['delivery']} - {item['message']}"
                for item in reminders
            ]
            await self.reply_ephemeral(interaction, "\n".join(lines))

        @reminder_group.command(name="cancel", description="Cancel a reminder.")
        async def reminder_cancel(interaction: discord.Interaction, id: str) -> None:
            if interaction.guild is None:
                await self.reply_ephemeral(interaction, "This command can only be used in a server.")
                return

            guild_data = self.store.get_guild_data(interaction.guild.id)
            before = len(guild_data["reminders"])
            guild_data["reminders"] = [
                item
                for item in guild_data["reminders"]
                if not (item["id"] == id and str(item["userId"]) == str(interaction.user.id))
            ]
            if len(guild_data["reminders"]) == before:
                await self.reply_ephemeral(interaction, f"No reminder found for id `{id}`.")
                return
            self.store.save()
            await self.reply_ephemeral(interaction, f"Reminder `{id}` cancelled.")

        @reminder_group.command(name="snooze", description="Delay an existing reminder.")
        @app_commands.rename(extra="for")
        async def reminder_snooze(interaction: discord.Interaction, id: str, extra: str) -> None:
            if interaction.guild is None:
                await self.reply_ephemeral(interaction, "This command can only be used in a server.")
                return

            delay_ms = parse_duration(extra)
            if delay_ms is None:
                await self.reply_ephemeral(interaction, "Use a valid snooze duration like `20m` or `1h`.")
                return

            guild_data = self.store.get_guild_data(interaction.guild.id)
            reminder = next(
                (
                    item
                    for item in guild_data["reminders"]
                    if item["id"] == id and str(item["userId"]) == str(interaction.user.id)
                ),
                None,
            )
            if reminder is None:
                await self.reply_ephemeral(interaction, f"No reminder found for id `{id}`.")
                return

            reminder["dueAt"] += delay_ms
            self.store.save()
            await self.reply_ephemeral(
                interaction,
                f"Reminder `{id}` snoozed until {render_discord_timestamp(reminder['dueAt'])}.",
            )

        job_group = app_commands.Group(name="job", description="Manage recurring scheduled jobs.")

        @job_group.command(name="create", description="Create a recurring scheduled message.")
        async def job_create(
            interaction: discord.Interaction,
            name: str,
            channel: discord.TextChannel,
            message: str,
            every: str,
            start_in: Optional[str] = None,
        ) -> None:
            if interaction.guild is None:
                await self.reply_ephemeral(interaction, "This command can only be used in a server.")
                return
            if not await self.ensure_permissions(interaction, "`/job`", manage_guild=True):
                return

            interval_ms = parse_duration(every)
            start_delay = parse_duration(start_in or "0")
            if interval_ms is None or interval_ms < 60 * 1000:
                await self.reply_ephemeral(interaction, "Use an interval of at least `1m` for recurring jobs.")
                return
            if start_delay is None:
                await self.reply_ephemeral(interaction, "I couldn't parse `start-in`. Try `10m` or leave it blank.")
                return

            guild_data = self.store.get_guild_data(interaction.guild.id)
            job = {
                "id": make_id(),
                "name": name,
                "channelId": str(channel.id),
                "message": message,
                "intervalMs": interval_ms,
                "nextRunAt": now_ms() + start_delay,
                "lastRunAt": None,
                "enabled": True,
                "createdBy": str(interaction.user.id),
            }
            guild_data["jobs"].append(job)
            self.store.save()
            await self.reply_ephemeral(
                interaction,
                f"Job `{job['id']}` created. Next run: {render_discord_timestamp(job['nextRunAt'])}. Repeats every {format_duration(interval_ms)}.",
            )

        @job_group.command(name="list", description="List all scheduled jobs.")
        async def job_list(interaction: discord.Interaction) -> None:
            if interaction.guild is None:
                await self.reply_ephemeral(interaction, "This command can only be used in a server.")
                return
            if not await self.ensure_permissions(interaction, "`/job`", manage_guild=True):
                return

            guild_data = self.store.get_guild_data(interaction.guild.id)
            if not guild_data["jobs"]:
                await self.reply_ephemeral(interaction, "No scheduled jobs yet.")
                return

            lines = [
                f"`{job['id']}` - {'active' if job['enabled'] else 'paused'} - {render_discord_timestamp(job['nextRunAt'])} - every {format_duration(job['intervalMs'])} - {job['name']}"
                for job in sorted(guild_data["jobs"], key=lambda item: item["nextRunAt"])
            ]
            await self.reply_ephemeral(interaction, "\n".join(lines))

        async def mutate_job(interaction: discord.Interaction, id: str, action: str) -> None:
            if interaction.guild is None:
                await self.reply_ephemeral(interaction, "This command can only be used in a server.")
                return
            if not await self.ensure_permissions(interaction, "`/job`", manage_guild=True):
                return

            guild_data = self.store.get_guild_data(interaction.guild.id)
            job = next((item for item in guild_data["jobs"] if item["id"] == id), None)
            if job is None:
                await self.reply_ephemeral(interaction, f"No job found for id `{id}`.")
                return

            if action == "pause":
                job["enabled"] = False
                response = f"Job `{id}` paused."
            elif action == "resume":
                job["enabled"] = True
                job["nextRunAt"] = now_ms() + int(job["intervalMs"])
                response = f"Job `{id}` resumed. Next run: {render_discord_timestamp(job['nextRunAt'])}."
            elif action == "remove":
                guild_data["jobs"] = [item for item in guild_data["jobs"] if item["id"] != id]
                response = f"Job `{id}` removed."
            else:
                job["enabled"] = True
                job["nextRunAt"] = now_ms()
                response = f"Job `{id}` queued to run on the next scheduler tick."

            self.store.save()
            await self.reply_ephemeral(interaction, response)

        @job_group.command(name="pause", description="Pause a job.")
        async def job_pause(interaction: discord.Interaction, id: str) -> None:
            await mutate_job(interaction, id, "pause")

        @job_group.command(name="resume", description="Resume a paused job.")
        async def job_resume(interaction: discord.Interaction, id: str) -> None:
            await mutate_job(interaction, id, "resume")

        @job_group.command(name="remove", description="Remove a job.")
        async def job_remove(interaction: discord.Interaction, id: str) -> None:
            await mutate_job(interaction, id, "remove")

        @job_group.command(name="run-now", description="Force a job to run on the next scheduler tick.")
        async def job_run_now(interaction: discord.Interaction, id: str) -> None:
            await mutate_job(interaction, id, "run-now")

        tag_group = app_commands.Group(name="tag", description="Manage reusable text snippets.")

        @tag_group.command(name="list", description="List all tags.")
        async def tag_list(interaction: discord.Interaction) -> None:
            if interaction.guild is None:
                await self.reply_ephemeral(interaction, "This command can only be used in a server.")
                return
            names = sorted(self.store.get_guild_data(interaction.guild.id)["tags"].keys())
            await self.reply_ephemeral(interaction, "\n".join(f"- {name}" for name in names) if names else "No tags saved yet.")

        async def upsert_tag(interaction: discord.Interaction, name: str, content: str, action: str) -> None:
            if interaction.guild is None:
                await self.reply_ephemeral(interaction, "This command can only be used in a server.")
                return
            if not await self.ensure_permissions(interaction, "`/tag`", manage_guild=True):
                return
            guild_data = self.store.get_guild_data(interaction.guild.id)
            normalized = name.lower().strip()
            guild_data["tags"][normalized] = content
            self.store.save()
            await self.reply_ephemeral(interaction, f"Tag `{normalized}` {action}.")

        @tag_group.command(name="create", description="Create a tag.")
        async def tag_create(interaction: discord.Interaction, name: str, content: str) -> None:
            await upsert_tag(interaction, name, content, "created")

        @tag_group.command(name="update", description="Update a tag.")
        async def tag_update(interaction: discord.Interaction, name: str, content: str) -> None:
            await upsert_tag(interaction, name, content, "updated")

        @tag_group.command(name="delete", description="Delete a tag.")
        async def tag_delete(interaction: discord.Interaction, name: str) -> None:
            if interaction.guild is None:
                await self.reply_ephemeral(interaction, "This command can only be used in a server.")
                return
            if not await self.ensure_permissions(interaction, "`/tag`", manage_guild=True):
                return
            guild_data = self.store.get_guild_data(interaction.guild.id)
            normalized = name.lower().strip()
            if normalized not in guild_data["tags"]:
                await self.reply_ephemeral(interaction, f"Tag `{normalized}` does not exist.")
                return
            del guild_data["tags"][normalized]
            self.store.save()
            await self.reply_ephemeral(interaction, f"Tag `{normalized}` deleted.")

        @tag_group.command(name="post", description="Post a tag into a channel.")
        async def tag_post(
            interaction: discord.Interaction,
            name: str,
            channel: Optional[discord.TextChannel] = None,
        ) -> None:
            if interaction.guild is None or interaction.channel is None:
                await self.reply_ephemeral(interaction, "This command can only be used in a server.")
                return
            if not await self.ensure_permissions(interaction, "`/tag`", manage_guild=True):
                return
            guild_data = self.store.get_guild_data(interaction.guild.id)
            normalized = name.lower().strip()
            content = guild_data["tags"].get(normalized)
            if not content:
                await self.reply_ephemeral(interaction, f"Tag `{normalized}` does not exist.")
                return
            target_channel = channel or interaction.channel
            await target_channel.send(content, allowed_mentions=discord.AllowedMentions.none())
            await self.reply_ephemeral(interaction, f"Posted tag `{normalized}` in {target_channel}.")

        template_group = app_commands.Group(name="template", description="Manage reusable templated messages.")

        @template_group.command(name="list", description="List templates.")
        async def template_list(interaction: discord.Interaction) -> None:
            if interaction.guild is None:
                await self.reply_ephemeral(interaction, "This command can only be used in a server.")
                return
            names = sorted(self.store.get_guild_data(interaction.guild.id)["templates"].keys())
            if not names:
                await self.reply_ephemeral(interaction, "No templates saved yet.")
                return
            await self.reply_ephemeral(
                interaction,
                "\n".join(f"- {name}" for name in names)
                + "\n\nBuilt-ins: {user}, {channel}, {server}, {date}, {time}, {iso_date}",
            )

        async def upsert_template(interaction: discord.Interaction, name: str, content: str, action: str) -> None:
            if interaction.guild is None:
                await self.reply_ephemeral(interaction, "This command can only be used in a server.")
                return
            if not await self.ensure_permissions(interaction, "`/template`", manage_guild=True):
                return
            guild_data = self.store.get_guild_data(interaction.guild.id)
            normalized = name.lower().strip()
            guild_data["templates"][normalized] = content
            self.store.save()
            await self.reply_ephemeral(interaction, f"Template `{normalized}` {action}.")

        @template_group.command(name="create", description="Create a template.")
        async def template_create(interaction: discord.Interaction, name: str, content: str) -> None:
            await upsert_template(interaction, name, content, "created")

        @template_group.command(name="update", description="Update a template.")
        async def template_update(interaction: discord.Interaction, name: str, content: str) -> None:
            await upsert_template(interaction, name, content, "updated")

        @template_group.command(name="delete", description="Delete a template.")
        async def template_delete(interaction: discord.Interaction, name: str) -> None:
            if interaction.guild is None:
                await self.reply_ephemeral(interaction, "This command can only be used in a server.")
                return
            if not await self.ensure_permissions(interaction, "`/template`", manage_guild=True):
                return
            guild_data = self.store.get_guild_data(interaction.guild.id)
            normalized = name.lower().strip()
            if normalized not in guild_data["templates"]:
                await self.reply_ephemeral(interaction, f"Template `{normalized}` does not exist.")
                return
            del guild_data["templates"][normalized]
            self.store.save()
            await self.reply_ephemeral(interaction, f"Template `{normalized}` deleted.")

        @template_group.command(name="send", description="Render and send a template.")
        async def template_send(
            interaction: discord.Interaction,
            name: str,
            channel: Optional[discord.TextChannel] = None,
            values: Optional[str] = None,
        ) -> None:
            if interaction.guild is None or interaction.channel is None:
                await self.reply_ephemeral(interaction, "This command can only be used in a server.")
                return
            if not await self.ensure_permissions(interaction, "`/template`", manage_guild=True):
                return
            guild_data = self.store.get_guild_data(interaction.guild.id)
            normalized = name.lower().strip()
            template = guild_data["templates"].get(normalized)
            if not template:
                await self.reply_ephemeral(interaction, f"Template `{normalized}` does not exist.")
                return
            target_channel = channel or interaction.channel
            placeholders = build_base_placeholders(
                guild=interaction.guild,
                channel=target_channel,
                user=interaction.user,
            )
            placeholders.update(parse_assignments(values))
            rendered = render_template_text(template, placeholders)
            await target_channel.send(rendered, allowed_mentions=discord.AllowedMentions.none())
            await self.reply_ephemeral(interaction, f"Template `{normalized}` sent to {target_channel}.")

        checklist_group = app_commands.Group(name="checklist", description="Manage team checklists.")

        def render_checklist(name: str, checklist: dict[str, Any]) -> str:
            items = "\n".join(
                f"{'[x]' if item['done'] else '[ ]'} {index + 1}. {item['text']}"
                for index, item in enumerate(checklist["items"])
            )
            return f"**{name}**\n{items or 'No items yet.'}"

        @checklist_group.command(name="list", description="List checklist names.")
        async def checklist_list(interaction: discord.Interaction) -> None:
            if interaction.guild is None:
                await self.reply_ephemeral(interaction, "This command can only be used in a server.")
                return
            names = sorted(self.store.get_guild_data(interaction.guild.id)["checklists"].keys())
            await self.reply_ephemeral(interaction, "\n".join(f"- {name}" for name in names) if names else "No checklists saved yet.")

        @checklist_group.command(name="show", description="Show a checklist.")
        async def checklist_show(interaction: discord.Interaction, name: str) -> None:
            if interaction.guild is None:
                await self.reply_ephemeral(interaction, "This command can only be used in a server.")
                return
            normalized = name.lower().strip()
            checklist = self.store.get_guild_data(interaction.guild.id)["checklists"].get(normalized)
            await self.reply_ephemeral(
                interaction,
                render_checklist(normalized, checklist) if checklist else f"Checklist `{normalized}` does not exist.",
            )

        @checklist_group.command(name="create", description="Create a checklist from pipe-delimited items.")
        async def checklist_create(interaction: discord.Interaction, name: str, items: str) -> None:
            if interaction.guild is None:
                await self.reply_ephemeral(interaction, "This command can only be used in a server.")
                return
            if not await self.ensure_permissions(interaction, "`/checklist`", manage_guild=True):
                return
            normalized = name.lower().strip()
            parsed_items = [
                {"text": item.strip(), "done": False}
                for item in items.split("|")
                if item.strip()
            ]
            guild_data = self.store.get_guild_data(interaction.guild.id)
            guild_data["checklists"][normalized] = {
                "items": parsed_items,
                "createdBy": str(interaction.user.id),
                "createdAt": now_ms(),
            }
            self.store.save()
            await self.reply_ephemeral(
                interaction,
                f"Checklist `{normalized}` created with {len(parsed_items)} item(s).",
            )

        async def get_checklist_for_mutation(interaction: discord.Interaction, name: str) -> Tuple[str, Optional[dict[str, Any]]]:
            normalized = name.lower().strip()
            guild_data = self.store.get_guild_data(interaction.guild.id)
            return normalized, guild_data["checklists"].get(normalized)

        @checklist_group.command(name="add-item", description="Add an item to a checklist.")
        async def checklist_add_item(interaction: discord.Interaction, name: str, item: str) -> None:
            if interaction.guild is None:
                await self.reply_ephemeral(interaction, "This command can only be used in a server.")
                return
            if not await self.ensure_permissions(interaction, "`/checklist`", manage_guild=True):
                return
            normalized, checklist = await get_checklist_for_mutation(interaction, name)
            if checklist is None:
                await self.reply_ephemeral(interaction, f"Checklist `{normalized}` does not exist.")
                return
            checklist["items"].append({"text": item, "done": False})
            self.store.save()
            await self.reply_ephemeral(interaction, f"Added a new item to `{normalized}`.")

        @checklist_group.command(name="done", description="Mark one checklist item as done.")
        async def checklist_done(interaction: discord.Interaction, name: str, index: int) -> None:
            if interaction.guild is None:
                await self.reply_ephemeral(interaction, "This command can only be used in a server.")
                return
            if not await self.ensure_permissions(interaction, "`/checklist`", manage_guild=True):
                return
            normalized, checklist = await get_checklist_for_mutation(interaction, name)
            if checklist is None:
                await self.reply_ephemeral(interaction, f"Checklist `{normalized}` does not exist.")
                return
            slot = index - 1
            if slot < 0 or slot >= len(checklist["items"]):
                await self.reply_ephemeral(interaction, "That checklist item number does not exist.")
                return
            checklist["items"][slot]["done"] = True
            self.store.save()
            await self.reply_ephemeral(interaction, f"Marked item {index} in `{normalized}` as done.")

        @checklist_group.command(name="reset", description="Reset every item in a checklist.")
        async def checklist_reset(interaction: discord.Interaction, name: str) -> None:
            if interaction.guild is None:
                await self.reply_ephemeral(interaction, "This command can only be used in a server.")
                return
            if not await self.ensure_permissions(interaction, "`/checklist`", manage_guild=True):
                return
            normalized, checklist = await get_checklist_for_mutation(interaction, name)
            if checklist is None:
                await self.reply_ephemeral(interaction, f"Checklist `{normalized}` does not exist.")
                return
            for item in checklist["items"]:
                item["done"] = False
            self.store.save()
            await self.reply_ephemeral(interaction, f"Checklist `{normalized}` reset.")

        @checklist_group.command(name="delete", description="Delete a checklist.")
        async def checklist_delete(interaction: discord.Interaction, name: str) -> None:
            if interaction.guild is None:
                await self.reply_ephemeral(interaction, "This command can only be used in a server.")
                return
            if not await self.ensure_permissions(interaction, "`/checklist`", manage_guild=True):
                return
            normalized = name.lower().strip()
            guild_data = self.store.get_guild_data(interaction.guild.id)
            if normalized not in guild_data["checklists"]:
                await self.reply_ephemeral(interaction, f"Checklist `{normalized}` does not exist.")
                return
            del guild_data["checklists"][normalized]
            self.store.save()
            await self.reply_ephemeral(interaction, f"Checklist `{normalized}` deleted.")

        todo_group = app_commands.Group(name="todo", description="Manage shared operational todos.")

        @todo_group.command(name="list", description="List todos.")
        async def todo_list(interaction: discord.Interaction) -> None:
            if interaction.guild is None:
                await self.reply_ephemeral(interaction, "This command can only be used in a server.")
                return
            todos = list(self.store.get_guild_data(interaction.guild.id)["todos"])
            if not todos:
                await self.reply_ephemeral(interaction, "No todos saved yet.")
                return
            todos.sort(key=lambda item: (item["done"], item.get("dueAt") or 10**18))
            lines = []
            for todo in todos:
                assignee = f"<@{todo['assigneeId']}>" if todo.get("assigneeId") else "unassigned"
                due = render_discord_timestamp(todo["dueAt"]) if todo.get("dueAt") else "no due date"
                lines.append(
                    f"`{todo['id']}` - {'done' if todo['done'] else 'open'} - {assignee} - {due} - {todo['title']}"
                )
            await self.reply_ephemeral(interaction, "\n".join(lines))

        @todo_group.command(name="add", description="Add a todo.")
        async def todo_add(
            interaction: discord.Interaction,
            title: str,
            assignee: Optional[discord.User] = None,
            due_in: Optional[str] = None,
        ) -> None:
            if interaction.guild is None:
                await self.reply_ephemeral(interaction, "This command can only be used in a server.")
                return
            if not await self.ensure_permissions(interaction, "`/todo`", manage_guild=True):
                return
            due_ms = parse_duration(due_in) if due_in else None
            if due_in and due_ms is None:
                await self.reply_ephemeral(interaction, "Use a valid due delay like `6h` or `3d`.")
                return
            todo = {
                "id": make_id(),
                "title": title,
                "assigneeId": str(assignee.id) if assignee else None,
                "createdBy": str(interaction.user.id),
                "createdAt": now_ms(),
                "dueAt": now_ms() + due_ms if due_ms else None,
                "done": False,
            }
            self.store.get_guild_data(interaction.guild.id)["todos"].append(todo)
            self.store.save()
            await self.reply_ephemeral(interaction, f"Todo `{todo['id']}` added.")

        async def mutate_todo(interaction: discord.Interaction, id: str, action: str) -> None:
            if interaction.guild is None:
                await self.reply_ephemeral(interaction, "This command can only be used in a server.")
                return
            if not await self.ensure_permissions(interaction, "`/todo`", manage_guild=True):
                return
            guild_data = self.store.get_guild_data(interaction.guild.id)
            todo = next((item for item in guild_data["todos"] if item["id"] == id), None)
            if todo is None:
                await self.reply_ephemeral(interaction, f"Todo `{id}` does not exist.")
                return
            if action == "done":
                todo["done"] = True
                todo["doneAt"] = now_ms()
                response = f"Todo `{id}` marked done."
            else:
                guild_data["todos"] = [item for item in guild_data["todos"] if item["id"] != id]
                response = f"Todo `{id}` removed."
            self.store.save()
            await self.reply_ephemeral(interaction, response)

        @todo_group.command(name="done", description="Mark a todo as done.")
        async def todo_done(interaction: discord.Interaction, id: str) -> None:
            await mutate_todo(interaction, id, "done")

        @todo_group.command(name="remove", description="Remove a todo.")
        async def todo_remove(interaction: discord.Interaction, id: str) -> None:
            await mutate_todo(interaction, id, "remove")

        approval_group = app_commands.Group(name="approval", description="Track internal approval requests.")

        @approval_group.command(name="list", description="List approval requests.")
        async def approval_list(interaction: discord.Interaction) -> None:
            if interaction.guild is None:
                await self.reply_ephemeral(interaction, "This command can only be used in a server.")
                return
            requests = sorted(
                self.store.get_guild_data(interaction.guild.id)["approvals"].values(),
                key=lambda item: item["createdAt"],
                reverse=True,
            )
            if not requests:
                await self.reply_ephemeral(interaction, "No approval requests yet.")
                return
            lines = [
                f"`{request['id']}` - {request['status']} - <@{request['requestedBy']}> - {request['title']}"
                for request in requests
            ]
            await self.reply_ephemeral(interaction, "\n".join(lines))

        @approval_group.command(name="create", description="Create a new approval request.")
        async def approval_create(interaction: discord.Interaction, title: str, details: str) -> None:
            if interaction.guild is None:
                await self.reply_ephemeral(interaction, "This command can only be used in a server.")
                return
            guild_data = self.store.get_guild_data(interaction.guild.id)
            request = {
                "id": make_id(),
                "title": title,
                "details": details,
                "requestedBy": str(interaction.user.id),
                "createdAt": now_ms(),
                "status": "pending",
                "decidedBy": None,
                "decidedAt": None,
                "decisionNote": None,
            }
            guild_data["approvals"][request["id"]] = request
            self.store.save()
            await self.log_to_guild(
                interaction.guild.id,
                f"Approval request `{request['id']}` created by <@{interaction.user.id}>: **{request['title']}**",
            )
            await self.reply_ephemeral(interaction, f"Approval request `{request['id']}` created.")

        async def decide_approval(
            interaction: discord.Interaction, id: str, status: str, reason: Optional[str] = None
        ) -> None:
            if interaction.guild is None:
                await self.reply_ephemeral(interaction, "This command can only be used in a server.")
                return
            if not await self.ensure_permissions(
                interaction,
                "`/approval approve` or `/approval reject`",
                manage_guild=True,
            ):
                return
            guild_data = self.store.get_guild_data(interaction.guild.id)
            request = guild_data["approvals"].get(id)
            if request is None:
                await self.reply_ephemeral(interaction, f"Approval request `{id}` does not exist.")
                return
            request["status"] = status
            request["decidedBy"] = str(interaction.user.id)
            request["decidedAt"] = now_ms()
            request["decisionNote"] = reason
            self.store.save()
            await self.log_to_guild(
                interaction.guild.id,
                f"Approval request `{id}` {status} by <@{interaction.user.id}>.",
            )
            await self.reply_ephemeral(interaction, f"Approval request `{id}` marked {status}.")

        @approval_group.command(name="approve", description="Approve a request.")
        async def approval_approve(interaction: discord.Interaction, id: str) -> None:
            await decide_approval(interaction, id, "approved")

        @approval_group.command(name="reject", description="Reject a request.")
        async def approval_reject(interaction: discord.Interaction, id: str, reason: Optional[str] = None) -> None:
            await decide_approval(interaction, id, "rejected", reason)

        autorole_group = app_commands.Group(name="autorole", description="Manage roles automatically assigned to new members.")

        @autorole_group.command(name="list", description="List current autoroles.")
        async def autorole_list(interaction: discord.Interaction) -> None:
            if interaction.guild is None:
                await self.reply_ephemeral(interaction, "This command can only be used in a server.")
                return
            role_mentions = [
                role.mention
                for role_id in self.store.get_guild_data(interaction.guild.id)["autoRoles"]
                if (role := interaction.guild.get_role(int(role_id))) is not None
            ]
            await self.reply_ephemeral(interaction, "\n".join(role_mentions) if role_mentions else "No autoroles configured.")

        @autorole_group.command(name="add", description="Add a role to the autorole list.")
        async def autorole_add(interaction: discord.Interaction, role: discord.Role) -> None:
            if interaction.guild is None:
                await self.reply_ephemeral(interaction, "This command can only be used in a server.")
                return
            if not await self.ensure_permissions(interaction, "`/autorole`", manage_roles=True):
                return
            guild_data = self.store.get_guild_data(interaction.guild.id)
            if str(role.id) not in guild_data["autoRoles"]:
                guild_data["autoRoles"].append(str(role.id))
                self.store.save()
            await self.reply_ephemeral(interaction, f"{role.mention} added to autoroles.")

        @autorole_group.command(name="remove", description="Remove a role from the autorole list.")
        async def autorole_remove(interaction: discord.Interaction, role: discord.Role) -> None:
            if interaction.guild is None:
                await self.reply_ephemeral(interaction, "This command can only be used in a server.")
                return
            if not await self.ensure_permissions(interaction, "`/autorole`", manage_roles=True):
                return
            guild_data = self.store.get_guild_data(interaction.guild.id)
            guild_data["autoRoles"] = [role_id for role_id in guild_data["autoRoles"] if role_id != str(role.id)]
            self.store.save()
            await self.reply_ephemeral(interaction, f"{role.mention} removed from autoroles.")

        bulkrole_group = app_commands.Group(name="bulkrole", description="Apply or remove a role across many members.")
        filter_choices = [
            app_commands.Choice(name="all", value="all"),
            app_commands.Choice(name="humans", value="humans"),
            app_commands.Choice(name="bots", value="bots"),
        ]

        async def collect_members(
            guild: discord.Guild,
            filter_mode: str,
            source_role: Optional[discord.Role],
        ) -> list[discord.Member]:
            members: list[discord.Member] = []
            async for member in guild.fetch_members(limit=None):
                if source_role and source_role not in member.roles:
                    continue
                if filter_mode == "humans" and member.bot:
                    continue
                if filter_mode == "bots" and not member.bot:
                    continue
                members.append(member)
            return members

        async def run_bulk_role(
            interaction: discord.Interaction,
            action: str,
            target_role: discord.Role,
            filter_value: str,
            source_role: Optional[discord.Role] = None,
        ) -> None:
            if interaction.guild is None:
                await self.reply_ephemeral(interaction, "This command can only be used in a server.")
                return
            if not await self.ensure_permissions(interaction, "`/bulkrole`", manage_roles=True):
                return
            await self.defer_ephemeral(interaction)
            members = await collect_members(interaction.guild, filter_value, source_role)
            changed = 0
            for member in members:
                try:
                    if action == "add" and target_role not in member.roles:
                        await member.add_roles(target_role, reason="MotionXBot bulkrole add")
                        changed += 1
                    elif action == "remove" and target_role in member.roles:
                        await member.remove_roles(target_role, reason="MotionXBot bulkrole remove")
                        changed += 1
                except discord.HTTPException:
                    continue
            await interaction.edit_original_response(
                content=f"{'Added' if action == 'add' else 'Removed'} {target_role.mention} for {changed} member(s)."
            )

        @bulkrole_group.command(name="add", description="Add a target role to many members.")
        @app_commands.choices(filter=filter_choices)
        async def bulkrole_add(
            interaction: discord.Interaction,
            target_role: discord.Role,
            filter: app_commands.Choice[str],
            source_role: Optional[discord.Role] = None,
        ) -> None:
            await run_bulk_role(interaction, "add", target_role, filter.value, source_role)

        @bulkrole_group.command(name="remove", description="Remove a target role from many members.")
        @app_commands.choices(filter=filter_choices)
        async def bulkrole_remove(
            interaction: discord.Interaction,
            target_role: discord.Role,
            filter: app_commands.Choice[str],
            source_role: Optional[discord.Role] = None,
        ) -> None:
            await run_bulk_role(interaction, "remove", target_role, filter.value, source_role)

        channel_group = app_commands.Group(name="channel", description="Manage the current channel quickly.")

        @channel_group.command(name="lock", description="Prevent @everyone from sending messages here.")
        async def channel_lock(interaction: discord.Interaction) -> None:
            if interaction.guild is None or not isinstance(interaction.channel, discord.TextChannel):
                await self.reply_ephemeral(interaction, "This command must be used in a text channel.")
                return
            if not await self.ensure_permissions(interaction, "`/channel`", manage_channels=True):
                return
            overwrite = interaction.channel.overwrites_for(interaction.guild.default_role)
            overwrite.send_messages = False
            await interaction.channel.set_permissions(interaction.guild.default_role, overwrite=overwrite)
            await self.reply_ephemeral(interaction, f"{interaction.channel.mention} locked.")

        @channel_group.command(name="unlock", description="Allow @everyone to send messages here again.")
        async def channel_unlock(interaction: discord.Interaction) -> None:
            if interaction.guild is None or not isinstance(interaction.channel, discord.TextChannel):
                await self.reply_ephemeral(interaction, "This command must be used in a text channel.")
                return
            if not await self.ensure_permissions(interaction, "`/channel`", manage_channels=True):
                return
            overwrite = interaction.channel.overwrites_for(interaction.guild.default_role)
            overwrite.send_messages = True
            await interaction.channel.set_permissions(interaction.guild.default_role, overwrite=overwrite)
            await self.reply_ephemeral(interaction, f"{interaction.channel.mention} unlocked.")

        @channel_group.command(name="slowmode", description="Set slowmode in seconds.")
        async def channel_slowmode(interaction: discord.Interaction, seconds: int) -> None:
            if interaction.guild is None or not isinstance(interaction.channel, discord.TextChannel):
                await self.reply_ephemeral(interaction, "This command must be used in a text channel.")
                return
            if not await self.ensure_permissions(interaction, "`/channel`", manage_channels=True):
                return
            await interaction.channel.edit(slowmode_delay=seconds)
            await self.reply_ephemeral(interaction, f"Slowmode set to {seconds} second(s) in {interaction.channel.mention}.")

        @channel_group.command(name="archive", description="Rename and lock the current channel as archived.")
        async def channel_archive(interaction: discord.Interaction, prefix: Optional[str] = None) -> None:
            if interaction.guild is None or not isinstance(interaction.channel, discord.TextChannel):
                await self.reply_ephemeral(interaction, "This command must be used in a text channel.")
                return
            if not await self.ensure_permissions(interaction, "`/channel`", manage_channels=True):
                return
            base_prefix = prefix or "archived"
            overwrite = interaction.channel.overwrites_for(interaction.guild.default_role)
            overwrite.send_messages = False
            await interaction.channel.edit(name=f"{base_prefix}-{interaction.channel.name}"[:100])
            await interaction.channel.set_permissions(interaction.guild.default_role, overwrite=overwrite)
            await self.reply_ephemeral(interaction, f"{interaction.channel.mention} archived.")

        @channel_group.command(name="delete-in", description="Schedule this current channel, thread, or forum to be deleted later.")
        @app_commands.rename(after="in")
        async def channel_delete_in(
            interaction: discord.Interaction,
            after: str,
            warning_message: Optional[str] = None,
            warn_at: Optional[str] = None,
            final_note: Optional[str] = None,
            only_if_idle: bool = False,
        ) -> None:
            if interaction.guild is None:
                await self.reply_ephemeral(interaction, "This command can only be used in a server.")
                return
            if not await self.ensure_permissions(interaction, "`/channel delete-in`", manage_channels=True):
                return

            target = self.get_timer_target_from_context(interaction.channel)
            if target is None:
                await self.reply_ephemeral(interaction, "Use this inside the channel, thread, or forum you want to schedule.")
                return
            if only_if_idle and isinstance(target, discord.ForumChannel):
                await self.reply_ephemeral(interaction, "`only_if_idle` currently works for text channels and threads, not whole forums.")
                return

            delay_ms = parse_duration(after)
            if delay_ms is None or delay_ms < 60 * 1000:
                await self.reply_ephemeral(interaction, "Use a deletion delay of at least `1m`.")
                return

            warning_offsets = self.build_channel_timer_warning_offsets(delay_ms, warn_at)
            if warn_at is not None and not warning_offsets and delay_ms >= 2 * 60 * 1000:
                await self.reply_ephemeral(interaction, "I couldn't parse `warn_at`. Use a comma list like `1h,10m,1m`.")
                return

            guild_data = self.store.get_guild_data(interaction.guild.id)
            timer = {
                "id": make_id(),
                "channelId": str(target.id),
                "createdBy": str(interaction.user.id),
                "createdAt": now_ms(),
                "deleteAt": now_ms() + delay_ms,
                "warningMessage": warning_message,
                "warningOffsetsMs": warning_offsets,
                "warnedOffsetsMs": [],
                "finalNote": final_note,
                "deleteIfEmpty": only_if_idle,
                "reason": f"MotionXBot scheduled deletion by {interaction.user}",
            }
            guild_data["channelTimers"] = [
                item for item in guild_data["channelTimers"] if str(item["channelId"]) != str(target.id)
            ]
            guild_data["channelTimers"].append(timer)
            self.store.save()

            warning_summary = ", ".join(format_duration(offset) for offset in warning_offsets) if warning_offsets else "none"
            suffix = (
                " Warnings will fall back to the log channel if this target cannot receive messages directly."
                if isinstance(target, discord.ForumChannel)
                else ""
            )
            await self.reply_ephemeral(
                interaction,
                f"{target.mention} is set to delete {render_discord_timestamp(timer['deleteAt'])}. "
                f"Warnings: {warning_summary}. Idle-only: {'yes' if only_if_idle else 'no'}.{suffix}",
            )

        @channel_group.command(name="delete-status", description="Show the active deletion timer for the current channel, thread, or forum.")
        async def channel_delete_status(interaction: discord.Interaction) -> None:
            if interaction.guild is None:
                await self.reply_ephemeral(interaction, "This command can only be used in a server.")
                return

            target = self.get_timer_target_from_context(interaction.channel)
            if target is None:
                await self.reply_ephemeral(interaction, "Use this inside the channel, thread, or forum you want to inspect.")
                return

            guild_data = self.store.get_guild_data(interaction.guild.id)
            timer = next((item for item in guild_data["channelTimers"] if str(item["channelId"]) == str(target.id)), None)
            if timer is None:
                await self.reply_ephemeral(interaction, f"No deletion timer is active for {target.mention}.")
                return

            remaining_ms = max(0, int(timer["deleteAt"]) - now_ms())
            warnings = ", ".join(format_duration(int(offset)) for offset in timer.get("warningOffsetsMs") or []) or "none"
            await self.reply_ephemeral(
                interaction,
                (
                    f"{target.mention} deletes {render_discord_timestamp(int(timer['deleteAt']))} "
                    f"({format_duration(remaining_ms)} from now).\n"
                    f"Warnings: {warnings}\n"
                    f"Idle-only: {'yes' if timer.get('deleteIfEmpty') else 'no'}"
                ),
            )

        @channel_group.command(name="delete-cancel", description="Cancel the deletion timer for the current channel, thread, or forum.")
        async def channel_delete_cancel(interaction: discord.Interaction) -> None:
            if interaction.guild is None:
                await self.reply_ephemeral(interaction, "This command can only be used in a server.")
                return
            if not await self.ensure_permissions(interaction, "`/channel delete-cancel`", manage_channels=True):
                return

            target = self.get_timer_target_from_context(interaction.channel)
            if target is None:
                await self.reply_ephemeral(interaction, "Use this inside the channel, thread, or forum you want to keep.")
                return

            guild_data = self.store.get_guild_data(interaction.guild.id)
            before = len(guild_data["channelTimers"])
            guild_data["channelTimers"] = [
                item for item in guild_data["channelTimers"] if str(item["channelId"]) != str(target.id)
            ]
            self.store.save()
            await self.reply_ephemeral(
                interaction,
                f"Deletion timer cancelled for {target.mention}."
                if len(guild_data["channelTimers"]) != before
                else f"No deletion timer was active for {target.mention}.",
            )

        cleanup_group = app_commands.Group(name="cleanup", description="Clean up recent messages.")

        async def purge_messages(channel: discord.TextChannel, predicate, limit: int) -> int:
            cutoff = discord.utils.utcnow().timestamp() - 14 * 24 * 60 * 60
            messages = [
                message
                async for message in channel.history(limit=min(limit, 100))
                if predicate(message) and message.created_at.timestamp() > cutoff
            ]
            if not messages:
                return 0
            await channel.delete_messages(messages)
            return len(messages)

        @cleanup_group.command(name="bot", description="Delete recent bot messages in this channel.")
        async def cleanup_bot(interaction: discord.Interaction, limit: int) -> None:
            if interaction.guild is None or not isinstance(interaction.channel, discord.TextChannel):
                await self.reply_ephemeral(interaction, "This command must be used in a text channel.")
                return
            if not await self.ensure_permissions(interaction, "`/cleanup`", manage_messages=True):
                return
            await self.defer_ephemeral(interaction)
            removed = await purge_messages(interaction.channel, lambda message: message.author.bot, limit)
            await interaction.edit_original_response(
                content=f"Deleted {removed} message(s). Discord only bulk-deletes messages newer than 14 days."
            )

        @cleanup_group.command(name="user", description="Delete recent messages from one user in this channel.")
        async def cleanup_user(interaction: discord.Interaction, member: discord.User, limit: int) -> None:
            if interaction.guild is None or not isinstance(interaction.channel, discord.TextChannel):
                await self.reply_ephemeral(interaction, "This command must be used in a text channel.")
                return
            if not await self.ensure_permissions(interaction, "`/cleanup`", manage_messages=True):
                return
            await self.defer_ephemeral(interaction)
            removed = await purge_messages(
                interaction.channel,
                lambda message: message.author.id == member.id,
                limit,
            )
            await interaction.edit_original_response(
                content=f"Deleted {removed} message(s). Discord only bulk-deletes messages newer than 14 days."
            )

        @cleanup_group.command(name="files", description="Delete recent attachment posts in this channel.")
        async def cleanup_files(interaction: discord.Interaction, limit: int) -> None:
            if interaction.guild is None or not isinstance(interaction.channel, discord.TextChannel):
                await self.reply_ephemeral(interaction, "This command must be used in a text channel.")
                return
            if not await self.ensure_permissions(interaction, "`/cleanup files`", manage_messages=True):
                return
            await self.defer_ephemeral(interaction)
            removed = await purge_messages(
                interaction.channel,
                lambda message: bool(get_effective_attachments(message)),
                limit,
            )
            await interaction.edit_original_response(
                content=f"Deleted {removed} file post(s). Discord only bulk-deletes messages newer than 14 days."
            )

        @cleanup_group.command(name="contains", description="Delete recent messages containing a chosen phrase in this channel.")
        async def cleanup_contains(
            interaction: discord.Interaction,
            text: str,
            limit: int,
        ) -> None:
            if interaction.guild is None or not isinstance(interaction.channel, discord.TextChannel):
                await self.reply_ephemeral(interaction, "This command must be used in a text channel.")
                return
            if not await self.ensure_permissions(interaction, "`/cleanup contains`", manage_messages=True):
                return
            needle = text.strip().lower()
            if not needle:
                await self.reply_ephemeral(interaction, "Enter the phrase you want to clean up.")
                return
            await self.defer_ephemeral(interaction)
            removed = await purge_messages(
                interaction.channel,
                lambda message: needle in (message.content or "").lower(),
                limit,
            )
            await interaction.edit_original_response(
                content=f"Deleted {removed} message(s) containing `{text}`. Discord only bulk-deletes messages newer than 14 days."
            )

        check_group = app_commands.Group(name="check", description="Check useful server activity stats.")

        @check_group.command(name="messages", description="Count how many messages one user sent in a server, channel, thread, or forum.")
        async def check_messages(
            interaction: discord.Interaction,
            member: discord.Member,
            source_channel: Optional[discord.TextChannel] = None,
            source_thread: Optional[discord.Thread] = None,
            source_forum: Optional[discord.ForumChannel] = None,
            since: Optional[str] = None,
        ) -> None:
            if interaction.guild is None:
                await self.reply_ephemeral(interaction, "This command can only be used in a server.")
                return
            if not await self.ensure_permissions(interaction, "`/check messages`", manage_messages=True):
                return

            selected_sources = [item for item in (source_channel, source_thread, source_forum) if item is not None]
            if len(selected_sources) > 1:
                await self.reply_ephemeral(interaction, "Choose only one source at a time.")
                return

            since_ms = None
            if since:
                since_ms = parse_duration(since)
                if since_ms is None:
                    await self.reply_ephemeral(interaction, "I couldn't parse that duration. Try `1d`, `12h`, or `30m`.")
                    return

            await self.defer_ephemeral(interaction)
            counts, scanned_messages, scanned_sources = await self.count_messages_by_author(
                interaction.guild,
                source_channel=source_channel,
                source_thread=source_thread,
                source_forum=source_forum,
                target_user_id=member.id,
                since_ms=since_ms,
                interaction=interaction,
            )
            total = counts.get(member.id, 0)
            duration_label = f" over the last {format_duration(since_ms)}" if since_ms is not None else ""
            await interaction.edit_original_response(
                content=(
                    f"{member.mention} sent **{total}** message(s) "
                    f"{self.describe_check_scope(source_channel=source_channel, source_thread=source_thread, source_forum=source_forum)}"
                    f"{duration_label}.\n"
                    f"Sources scanned: {scanned_sources}\n"
                    f"Messages scanned: {scanned_messages}"
                )
            )

        @check_group.command(name="leaderboard", description="Show the most active senders in a server, channel, thread, or forum.")
        async def check_leaderboard(
            interaction: discord.Interaction,
            source_channel: Optional[discord.TextChannel] = None,
            source_thread: Optional[discord.Thread] = None,
            source_forum: Optional[discord.ForumChannel] = None,
            since: Optional[str] = None,
            top: app_commands.Range[int, 1, 10] = 5,
        ) -> None:
            if interaction.guild is None:
                await self.reply_ephemeral(interaction, "This command can only be used in a server.")
                return
            if not await self.ensure_permissions(interaction, "`/check leaderboard`", manage_messages=True):
                return

            selected_sources = [item for item in (source_channel, source_thread, source_forum) if item is not None]
            if len(selected_sources) > 1:
                await self.reply_ephemeral(interaction, "Choose only one source at a time.")
                return

            since_ms = None
            if since:
                since_ms = parse_duration(since)
                if since_ms is None:
                    await self.reply_ephemeral(interaction, "I couldn't parse that duration. Try `1d`, `12h`, or `30m`.")
                    return

            await self.defer_ephemeral(interaction)
            counts, scanned_messages, scanned_sources = await self.count_messages_by_author(
                interaction.guild,
                source_channel=source_channel,
                source_thread=source_thread,
                source_forum=source_forum,
                target_user_id=None,
                since_ms=since_ms,
                interaction=interaction,
            )
            ranking = sorted(counts.items(), key=lambda item: item[1], reverse=True)[:top]
            if not ranking:
                await interaction.edit_original_response(
                    content=(
                        f"No messages were found {self.describe_check_scope(source_channel=source_channel, source_thread=source_thread, source_forum=source_forum)}."
                    )
                )
                return

            duration_label = f" over the last {format_duration(since_ms)}" if since_ms is not None else ""
            lines = [
                f"{index}. <@{user_id}> - {count}"
                for index, (user_id, count) in enumerate(ranking, start=1)
            ]
            await interaction.edit_original_response(
                content=(
                    f"Top senders {self.describe_check_scope(source_channel=source_channel, source_thread=source_thread, source_forum=source_forum)}"
                    f"{duration_label}:\n"
                    + "\n".join(lines)
                    + f"\n\nSources scanned: {scanned_sources}\nMessages scanned: {scanned_messages}"
                )
            )

        logchannel_group = app_commands.Group(name="logchannel", description="Configure the bot's audit log channel.")

        @logchannel_group.command(name="show", description="Show the current log channel.")
        async def logchannel_show(interaction: discord.Interaction) -> None:
            if interaction.guild is None:
                await self.reply_ephemeral(interaction, "This command can only be used in a server.")
                return
            guild_data = self.store.get_guild_data(interaction.guild.id)
            content = (
                f"Current log channel: <#{guild_data['logChannelId']}>"
                if guild_data["logChannelId"]
                else "No log channel configured."
            )
            await self.reply_ephemeral(interaction, content)

        @logchannel_group.command(name="set", description="Set the log channel.")
        async def logchannel_set(interaction: discord.Interaction, channel: discord.TextChannel) -> None:
            if interaction.guild is None:
                await self.reply_ephemeral(interaction, "This command can only be used in a server.")
                return
            if not await self.ensure_permissions(interaction, "`/logchannel`", manage_guild=True):
                return
            guild_data = self.store.get_guild_data(interaction.guild.id)
            guild_data["logChannelId"] = str(channel.id)
            self.store.save()
            await self.reply_ephemeral(interaction, f"Log channel set to {channel.mention}.")

        @logchannel_group.command(name="clear", description="Clear the log channel.")
        async def logchannel_clear(interaction: discord.Interaction) -> None:
            if interaction.guild is None:
                await self.reply_ephemeral(interaction, "This command can only be used in a server.")
                return
            if not await self.ensure_permissions(interaction, "`/logchannel`", manage_guild=True):
                return
            guild_data = self.store.get_guild_data(interaction.guild.id)
            guild_data["logChannelId"] = None
            self.store.save()
            await self.reply_ephemeral(interaction, "Log channel cleared.")

        heartbeat_group = app_commands.Group(name="heartbeat", description="Configure recurring heartbeat messages.")

        @heartbeat_group.command(name="status", description="Show heartbeat status.")
        async def heartbeat_status(interaction: discord.Interaction) -> None:
            if interaction.guild is None:
                await self.reply_ephemeral(interaction, "This command can only be used in a server.")
                return
            heartbeat = self.store.get_guild_data(interaction.guild.id).get("heartbeat")
            if not heartbeat:
                await self.reply_ephemeral(interaction, "Heartbeat is not configured.")
                return
            await self.reply_ephemeral(
                interaction,
                f"Heartbeat: {'enabled' if heartbeat['enabled'] else 'disabled'} - every {format_duration(heartbeat['intervalMs'])} - next run {render_discord_timestamp(heartbeat['nextRunAt'])} - channel <#{heartbeat['channelId']}>",
            )

        @heartbeat_group.command(name="set", description="Enable recurring heartbeat posts.")
        async def heartbeat_set(
            interaction: discord.Interaction,
            channel: discord.TextChannel,
            every: str,
            message: Optional[str] = None,
        ) -> None:
            if interaction.guild is None:
                await self.reply_ephemeral(interaction, "This command can only be used in a server.")
                return
            if not await self.ensure_permissions(interaction, "`/heartbeat`", manage_guild=True):
                return
            interval_ms = parse_duration(every)
            if interval_ms is None or interval_ms < 60 * 1000:
                await self.reply_ephemeral(interaction, "Use a heartbeat interval of at least `1m`.")
                return
            guild_data = self.store.get_guild_data(interaction.guild.id)
            guild_data["heartbeat"] = {
                "channelId": str(channel.id),
                "intervalMs": interval_ms,
                "nextRunAt": now_ms() + interval_ms,
                "lastRunAt": None,
                "enabled": True,
                "message": message or "Heartbeat check for {server} at {date} {time}.",
            }
            self.store.save()
            await self.reply_ephemeral(
                interaction,
                f"Heartbeat enabled. First post at {render_discord_timestamp(guild_data['heartbeat']['nextRunAt'])}.",
            )

        @heartbeat_group.command(name="clear", description="Disable heartbeat messages.")
        async def heartbeat_clear(interaction: discord.Interaction) -> None:
            if interaction.guild is None:
                await self.reply_ephemeral(interaction, "This command can only be used in a server.")
                return
            if not await self.ensure_permissions(interaction, "`/heartbeat`", manage_guild=True):
                return
            self.store.get_guild_data(interaction.guild.id)["heartbeat"] = None
            self.store.save()
            await self.reply_ephemeral(interaction, "Heartbeat disabled.")

        whisper_group = app_commands.Group(name="whisper", description="Send messages through the bot to channels, forums, threads, categories, or users.")

        @whisper_group.command(name="send", description="Send a bot-authored message to a chosen destination.")
        async def whisper_send(
            interaction: discord.Interaction,
            message: str,
            target_channel: Optional[discord.TextChannel] = None,
            target_thread: Optional[discord.Thread] = None,
            target_forum: Optional[discord.ForumChannel] = None,
            target_category: Optional[discord.CategoryChannel] = None,
            target_user: Optional[discord.User] = None,
            title: Optional[str] = None,
        ) -> None:
            if interaction.guild is None:
                await self.reply_ephemeral(interaction, "This command can only be used in a server.")
                return
            if not await self.ensure_permissions(interaction, "`/whisper`", manage_messages=True):
                return

            selected_targets = [
                item
                for item in (target_channel, target_thread, target_forum, target_category, target_user)
                if item is not None
            ]
            if len(selected_targets) != 1:
                await self.reply_ephemeral(
                    interaction,
                    "Choose exactly one target: a channel, thread, forum, category, or user.",
                )
                return

            await self.defer_ephemeral(interaction)
            delivered, delivered_labels = await self.send_whisper_to_target(
                source_guild=interaction.guild,
                content=message,
                actor=interaction.user,
                target_channel=target_channel,
                target_thread=target_thread,
                target_forum=target_forum,
                target_category=target_category,
                target_user=target_user,
                title=title,
            )
            if delivered == 0:
                await interaction.edit_original_response(
                    content="Nothing was delivered. If you targeted a category, make sure it has sendable text or forum channels."
                )
                return

            guild_data = self.store.get_guild_data(interaction.guild.id)
            guild_data["whispers"].append(
                self.build_whisper_log_entry(
                    actor_id=interaction.user.id,
                    content=message,
                    delivered_labels=delivered_labels,
                    title=title,
                )
            )
            guild_data["whispers"] = guild_data["whispers"][-200:]
            self.store.save()
            await interaction.edit_original_response(
                content=f"Delivered {delivered} whisper target(s): {', '.join(delivered_labels[:10])}"
            )

        @whisper_group.command(name="history", description="Show recent bot-authored whisper sends.")
        async def whisper_history(
            interaction: discord.Interaction,
            limit: app_commands.Range[int, 1, 15] = 10,
        ) -> None:
            if interaction.guild is None:
                await self.reply_ephemeral(interaction, "This command can only be used in a server.")
                return
            if not await self.ensure_permissions(interaction, "`/whisper`", manage_messages=True):
                return

            entries = list(reversed(self.store.get_guild_data(interaction.guild.id)["whispers"]))[:limit]
            if not entries:
                await self.reply_ephemeral(interaction, "No whisper sends have been recorded yet.")
                return

            lines = [
                f"`{entry['id']}` - {render_discord_timestamp(entry['createdAt'])} - {', '.join(entry['targets'][:3])} - {entry['content'][:80]}"
                for entry in entries
            ]
            await self.reply_ephemeral(interaction, "\n".join(lines))

        warn_group = app_commands.Group(name="warn", description="DM custom warnings and track them.")

        @warn_group.command(name="send", description="Send a custom warning DM and save it to the warning log.")
        async def warn_send(
            interaction: discord.Interaction,
            member: discord.User,
            message: str,
            title: Optional[str] = None,
        ) -> None:
            if interaction.guild is None:
                await self.reply_ephemeral(interaction, "This command can only be used in a server.")
                return
            if not await self.ensure_permissions(interaction, "`/warn`", manage_messages=True):
                return

            subject = title or f"Warning from {interaction.guild.name}"
            body = f"**{subject}**\n\n{message}"
            dm_delivered = True
            try:
                await member.send(body, allowed_mentions=discord.AllowedMentions.none())
                self.record_dm_log(
                    user_id=member.id,
                    direction="outbound",
                    content=body,
                    actor_id=interaction.user.id,
                    context="warning",
                )
            except discord.HTTPException:
                dm_delivered = False

            guild_data = self.store.get_guild_data(interaction.guild.id)
            guild_data["warnings"].append(
                {
                    "id": make_id(),
                    "userId": str(member.id),
                    "actorId": str(interaction.user.id),
                    "title": subject,
                    "message": message,
                    "dmDelivered": dm_delivered,
                    "createdAt": now_ms(),
                }
            )
            guild_data["warnings"] = guild_data["warnings"][-250:]
            self.store.save()
            await self.reply_ephemeral(
                interaction,
                f"Warning logged for {member.mention}. DM {'sent' if dm_delivered else 'failed to send'}.",
            )

        @warn_group.command(name="list", description="List warning history for one user.")
        async def warn_list(interaction: discord.Interaction, member: discord.User) -> None:
            if interaction.guild is None:
                await self.reply_ephemeral(interaction, "This command can only be used in a server.")
                return
            if not await self.ensure_permissions(interaction, "`/warn`", manage_messages=True):
                return

            warnings = [
                item
                for item in self.store.get_guild_data(interaction.guild.id)["warnings"]
                if str(item["userId"]) == str(member.id)
            ]
            warnings.sort(key=lambda item: item["createdAt"], reverse=True)
            if not warnings:
                await self.reply_ephemeral(interaction, f"No warnings recorded for {member.mention}.")
                return

            lines = [
                f"`{item['id']}` - {render_discord_timestamp(item['createdAt'])} - {item['title']} - DM {'yes' if item['dmDelivered'] else 'no'}"
                for item in warnings[:15]
            ]
            await self.reply_ephemeral(interaction, "\n".join(lines))

        @warn_group.command(name="clear", description="Clear one warning or all warnings for a user.")
        async def warn_clear(interaction: discord.Interaction, member: discord.User, id: Optional[str] = None) -> None:
            if interaction.guild is None:
                await self.reply_ephemeral(interaction, "This command can only be used in a server.")
                return
            if not await self.ensure_permissions(interaction, "`/warn`", manage_messages=True):
                return

            guild_data = self.store.get_guild_data(interaction.guild.id)
            before = len(guild_data["warnings"])
            if id:
                guild_data["warnings"] = [
                    item
                    for item in guild_data["warnings"]
                    if not (str(item["userId"]) == str(member.id) and item["id"] == id)
                ]
            else:
                guild_data["warnings"] = [
                    item for item in guild_data["warnings"] if str(item["userId"]) != str(member.id)
                ]
            removed = before - len(guild_data["warnings"])
            self.store.save()
            await self.reply_ephemeral(interaction, f"Removed {removed} warning(s) for {member.mention}.")

        dmlog_group = app_commands.Group(name="dmlog", description="Inspect inbound and outbound DMs handled by the bot.")

        @dmlog_group.command(name="user", description="Show DM history for one user.")
        async def dmlog_user(
            interaction: discord.Interaction,
            user: discord.User,
            limit: app_commands.Range[int, 1, 20] = 10,
        ) -> None:
            if interaction.guild is None:
                await self.reply_ephemeral(interaction, "This command can only be used in a server.")
                return
            if not await self.ensure_permissions(interaction, "`/dmlog`", manage_messages=True):
                return

            logs = [
                item for item in reversed(self.store.data.get("dmLogs", []))
                if str(item.get("userId")) == str(user.id)
            ][:limit]
            if not logs:
                await self.reply_ephemeral(interaction, f"No DM logs found for {user}.")
                return

            lines = [
                f"{item['direction']} - {render_discord_timestamp(item['createdAt'])} - {item.get('context') or 'dm'} - {str(item['content'])[:120]}"
                for item in logs
            ]
            await self.reply_ephemeral(interaction, "\n".join(lines))

        @dmlog_group.command(name="recent", description="Show the most recent DM activity handled by the bot.")
        async def dmlog_recent(
            interaction: discord.Interaction,
            limit: app_commands.Range[int, 1, 20] = 10,
        ) -> None:
            if interaction.guild is None:
                await self.reply_ephemeral(interaction, "This command can only be used in a server.")
                return
            if not await self.ensure_permissions(interaction, "`/dmlog`", manage_messages=True):
                return

            logs = list(reversed(self.store.data.get("dmLogs", [])))[:limit]
            if not logs:
                await self.reply_ephemeral(interaction, "No DM activity has been logged yet.")
                return

            lines = [
                f"{item['direction']} - <@{item['userId']}> - {render_discord_timestamp(item['createdAt'])} - {str(item['content'])[:100]}"
                for item in logs
            ]
            await self.reply_ephemeral(interaction, "\n".join(lines))

        note_group = app_commands.Group(name="note", description="Keep private moderation notes on members.")

        @note_group.command(name="add", description="Add a moderation note.")
        async def note_add(interaction: discord.Interaction, member: discord.User, note: str) -> None:
            if interaction.guild is None:
                await self.reply_ephemeral(interaction, "This command can only be used in a server.")
                return
            if not await self.ensure_permissions(interaction, "`/note`", manage_messages=True):
                return

            guild_data = self.store.get_guild_data(interaction.guild.id)
            entry = {
                "id": make_id(),
                "userId": str(member.id),
                "actorId": str(interaction.user.id),
                "note": note,
                "createdAt": now_ms(),
            }
            guild_data["modNotes"].append(entry)
            guild_data["modNotes"] = guild_data["modNotes"][-300:]
            self.store.save()
            await self.reply_ephemeral(interaction, f"Saved note `{entry['id']}` for {member.mention}.")

        @note_group.command(name="list", description="List moderation notes for one member.")
        async def note_list(interaction: discord.Interaction, member: discord.User) -> None:
            if interaction.guild is None:
                await self.reply_ephemeral(interaction, "This command can only be used in a server.")
                return
            if not await self.ensure_permissions(interaction, "`/note`", manage_messages=True):
                return

            notes = [
                item
                for item in self.store.get_guild_data(interaction.guild.id)["modNotes"]
                if str(item["userId"]) == str(member.id)
            ]
            notes.sort(key=lambda item: item["createdAt"], reverse=True)
            if not notes:
                await self.reply_ephemeral(interaction, f"No notes recorded for {member.mention}.")
                return

            lines = [
                f"`{item['id']}` - {render_discord_timestamp(item['createdAt'])} - {item['note'][:120]}"
                for item in notes[:15]
            ]
            await self.reply_ephemeral(interaction, "\n".join(lines))

        @note_group.command(name="remove", description="Remove a moderation note by id.")
        async def note_remove(interaction: discord.Interaction, id: str) -> None:
            if interaction.guild is None:
                await self.reply_ephemeral(interaction, "This command can only be used in a server.")
                return
            if not await self.ensure_permissions(interaction, "`/note`", manage_messages=True):
                return

            guild_data = self.store.get_guild_data(interaction.guild.id)
            before = len(guild_data["modNotes"])
            guild_data["modNotes"] = [item for item in guild_data["modNotes"] if item["id"] != id]
            self.store.save()
            await self.reply_ephemeral(
                interaction,
                "Note removed." if len(guild_data["modNotes"]) != before else f"No note found for id `{id}`.",
            )

        timeout_group = app_commands.Group(name="timeout", description="Quick member timeout controls.")

        @timeout_group.command(name="set", description="Timeout a member for a duration like 30m or 2d.")
        async def timeout_set(
            interaction: discord.Interaction,
            member: discord.Member,
            duration: str,
            reason: Optional[str] = None,
            dm: bool = False,
        ) -> None:
            if interaction.guild is None:
                await self.reply_ephemeral(interaction, "This command can only be used in a server.")
                return
            if not await self.ensure_permissions(interaction, "`/timeout`", moderate_members=True):
                return

            duration_ms = parse_duration(duration)
            if duration_ms is None or duration_ms <= 0:
                await self.reply_ephemeral(interaction, "Use a valid timeout like `30m`, `2h`, or `1d`.")
                return
            if duration_ms > 28 * 24 * 60 * 60 * 1000:
                await self.reply_ephemeral(interaction, "Discord only allows timeouts up to 28 days.")
                return

            until = discord.utils.utcnow() + timedelta(milliseconds=duration_ms)
            await member.timeout(until, reason=reason or f"Timed out by {interaction.user}")
            if dm:
                notice = f"You were timed out in **{interaction.guild.name}** for {format_duration(duration_ms)}."
                if reason:
                    notice += f"\nReason: {reason}"
                try:
                    await member.send(notice, allowed_mentions=discord.AllowedMentions.none())
                    self.record_dm_log(
                        user_id=member.id,
                        direction="outbound",
                        content=notice,
                        actor_id=interaction.user.id,
                        context="timeout",
                    )
                except discord.HTTPException:
                    pass
            await self.reply_ephemeral(
                interaction,
                f"{member.mention} timed out until {render_discord_timestamp(int(until.timestamp() * 1000))}.",
            )

        @timeout_group.command(name="clear", description="Remove a member timeout.")
        async def timeout_clear(
            interaction: discord.Interaction,
            member: discord.Member,
            reason: Optional[str] = None,
        ) -> None:
            if interaction.guild is None:
                await self.reply_ephemeral(interaction, "This command can only be used in a server.")
                return
            if not await self.ensure_permissions(interaction, "`/timeout`", moderate_members=True):
                return

            await member.timeout(None, reason=reason or f"Timeout cleared by {interaction.user}")
            await self.reply_ephemeral(interaction, f"Timeout removed for {member.mention}.")

        autoresponse_group = app_commands.Group(name="autoresponse", description="Reply automatically when trigger phrases appear.")
        auto_response_modes = [
            app_commands.Choice(name="contains", value="contains"),
            app_commands.Choice(name="exact", value="exact"),
            app_commands.Choice(name="starts_with", value="starts_with"),
        ]

        @autoresponse_group.command(name="add", description="Add an automatic text response rule.")
        @app_commands.choices(mode=auto_response_modes)
        async def autoresponse_add(
            interaction: discord.Interaction,
            trigger: str,
            response: str,
            mode: app_commands.Choice[str],
            channel: Optional[discord.TextChannel] = None,
        ) -> None:
            if interaction.guild is None:
                await self.reply_ephemeral(interaction, "This command can only be used in a server.")
                return
            if not await self.ensure_permissions(interaction, "`/autoresponse`", manage_guild=True):
                return

            guild_data = self.store.get_guild_data(interaction.guild.id)
            entry = {
                "id": make_id(),
                "trigger": trigger,
                "response": response,
                "matchMode": mode.value,
                "channelId": str(channel.id) if channel else None,
                "enabled": True,
                "createdAt": now_ms(),
            }
            guild_data["autoResponses"].append(entry)
            guild_data["autoResponses"] = guild_data["autoResponses"][-100:]
            self.store.save()
            await self.reply_ephemeral(interaction, f"Autoresponse `{entry['id']}` added.")

        @autoresponse_group.command(name="list", description="List autoresponse rules.")
        async def autoresponse_list(interaction: discord.Interaction) -> None:
            if interaction.guild is None:
                await self.reply_ephemeral(interaction, "This command can only be used in a server.")
                return
            rules = list(reversed(self.store.get_guild_data(interaction.guild.id)["autoResponses"]))
            if not rules:
                await self.reply_ephemeral(interaction, "No autoresponses configured.")
                return

            lines = [
                f"`{item['id']}` - {item['matchMode']} - {item['trigger']} - {('<#' + item['channelId'] + '>') if item['channelId'] else 'all channels'}"
                for item in rules[:20]
            ]
            await self.reply_ephemeral(interaction, "\n".join(lines))

        @autoresponse_group.command(name="remove", description="Remove an autoresponse by id.")
        async def autoresponse_remove(interaction: discord.Interaction, id: str) -> None:
            if interaction.guild is None:
                await self.reply_ephemeral(interaction, "This command can only be used in a server.")
                return
            if not await self.ensure_permissions(interaction, "`/autoresponse`", manage_guild=True):
                return

            guild_data = self.store.get_guild_data(interaction.guild.id)
            before = len(guild_data["autoResponses"])
            guild_data["autoResponses"] = [item for item in guild_data["autoResponses"] if item["id"] != id]
            self.store.save()
            await self.reply_ephemeral(
                interaction,
                "Autoresponse removed." if len(guild_data["autoResponses"]) != before else f"No autoresponse found for id `{id}`.",
            )

        audio_group = app_commands.Group(name="audio", description="Search audio attachments by filename.")

        @audio_group.command(name="search", description="Search for audio attachments by filename in a channel, thread, or forum.")
        async def audio_search(
            interaction: discord.Interaction,
            query: str,
            source_channel: Optional[discord.TextChannel] = None,
            source_thread: Optional[discord.Thread] = None,
            source_forum: Optional[discord.ForumChannel] = None,
            limit: app_commands.Range[int, 1, 5] = 5,
        ) -> None:
            if interaction.guild is None:
                await self.reply_ephemeral(interaction, "This command can only be used in a server.")
                return

            selected_sources = [item for item in (source_channel, source_thread, source_forum) if item is not None]
            if len(selected_sources) > 1:
                await self.reply_ephemeral(
                    interaction,
                    "Choose only one source override at a time.",
                )
                return

            normalized_query = query.strip()
            if not normalized_query:
                await self.reply_ephemeral(interaction, "Enter part of the audio filename you want to search for.")
                return

            await self.defer_ephemeral(interaction)
            source_obj = (
                selected_sources[0]
                if selected_sources
                else self.get_default_audio_search_source(interaction.channel)
            )
            if source_obj is None:
                await interaction.edit_original_response(
                    content="Run this in the channel or thread you want to search, or choose a source override."
                )
                return

            matches = await self.search_audio_in_source(
                source_obj,
                normalized_query,
                include_bots=True,
                interaction=interaction,
            )

            if not matches:
                await interaction.edit_original_response(
                    content=f"No audio attachments matching `{normalized_query}` were found in {source_obj.mention}."
                )
                return

            shown_matches = matches[:limit]
            await self.send_audio_search_results_interaction(
                interaction,
                normalized_query,
                shown_matches,
            )

        transfer_group = app_commands.Group(name="transfer", description="Copy batches of messages and attachments between channels.")

        @transfer_group.command(name="messages", description="Copy recent messages from one text channel to another.")
        async def transfer_messages(
            interaction: discord.Interaction,
            source: discord.TextChannel,
            target: discord.TextChannel,
            limit: app_commands.Range[int, 1, HARD_LIMIT],
            before: Optional[str] = None,
            include_bots: bool = False,
            mp3_only: bool = False,
            audio_only: bool = False,
        ) -> None:
            if interaction.guild is None:
                await self.reply_ephemeral(interaction, "This command can only be used in a server.")
                return
            if not await self.ensure_permissions(interaction, "`/transfer`", manage_messages=True):
                return
            if source.id == target.id:
                await self.reply_ephemeral(interaction, "Source and target channels must be different.")
                return
            if mp3_only and audio_only:
                await self.reply_ephemeral(interaction, "Choose either `mp3_only` or `audio_only`, not both.")
                return
            await self.defer_ephemeral(interaction)
            messages = await collect_messages(
                source,
                all_messages=False,
                limit=limit,
                before=before,
                include_bots=include_bots,
                on_progress=None,
            )
            messages = filter_messages_for_transfer(messages, mp3_only, audio_only)
            if not messages:
                await interaction.edit_original_response(
                    content="No transferable messages were found. Make sure the bot can read the source channel and its history."
                )
                return
            copied = 0
            failures: list[str] = []
            assert self.http_session is not None
            for message in messages:
                try:
                    sent = await repost_message(
                        target,
                        message,
                        self.http_session,
                        mp3_only=mp3_only,
                        audio_only=audio_only,
                    )
                    if sent:
                        copied += 1
                except Exception as error:  # noqa: BLE001
                    failures.append(f"`{message.id}`: {error}")
                if copied and copied % PROGRESS_UPDATE_INTERVAL == 0:
                    await interaction.edit_original_response(
                        content=f"Transferring from {source.mention} to {target.mention}...\nCopied: {copied}/{len(messages)}\nFailed: {len(failures)}"
                    )
            await interaction.edit_original_response(
                content=build_summary(
                    copied=copied,
                    failures=failures,
                    source=source,
                    target=target,
                    scanned=len(messages),
                    requested_all=False,
                )
            )

        @transfer_group.command(name="all", description="Copy the full available history from one text channel to another.")
        async def transfer_all(
            interaction: discord.Interaction,
            source: discord.TextChannel,
            target: discord.TextChannel,
            before: Optional[str] = None,
            include_bots: bool = True,
            mp3_only: bool = False,
            audio_only: bool = False,
        ) -> None:
            if interaction.guild is None:
                await self.reply_ephemeral(interaction, "This command can only be used in a server.")
                return
            if not await self.ensure_permissions(interaction, "`/transfer`", manage_messages=True):
                return
            if source.id == target.id:
                await self.reply_ephemeral(interaction, "Source and target channels must be different.")
                return
            if mp3_only and audio_only:
                await self.reply_ephemeral(interaction, "Choose either `mp3_only` or `audio_only`, not both.")
                return
            await self.defer_ephemeral(interaction)
            last_progress_at = 0.0

            async def progress(scanned: int, queued: int) -> None:
                nonlocal last_progress_at
                if self.loop.time() - last_progress_at < 2.5:
                    return
                last_progress_at = self.loop.time()
                await interaction.edit_original_response(
                    content=f"Scanning {source.mention}...\nScanned: {scanned}\nQueued for transfer: {queued}"
                )

            messages = await collect_messages(
                source,
                all_messages=True,
                limit=None,
                before=before,
                include_bots=include_bots,
                on_progress=progress,
            )
            messages = filter_messages_for_transfer(messages, mp3_only, audio_only)
            if not messages:
                await interaction.edit_original_response(
                    content="No transferable messages were found. Make sure the bot can read the source channel and its history."
                )
                return
            copied = 0
            failures: list[str] = []
            assert self.http_session is not None
            for message in messages:
                try:
                    sent = await repost_message(
                        target,
                        message,
                        self.http_session,
                        mp3_only=mp3_only,
                        audio_only=audio_only,
                    )
                    if sent:
                        copied += 1
                except Exception as error:  # noqa: BLE001
                    failures.append(f"`{message.id}`: {error}")
                if copied and copied % PROGRESS_UPDATE_INTERVAL == 0:
                    await interaction.edit_original_response(
                        content=f"Transferring from {source.mention} to {target.mention}...\nCopied: {copied}/{len(messages)}\nFailed: {len(failures)}"
                    )
            await interaction.edit_original_response(
                content=build_summary(
                    copied=copied,
                    failures=failures,
                    source=source,
                    target=target,
                    scanned=len(messages),
                    requested_all=True,
                )
            )

        @transfer_group.command(name="forum", description="Copy every forum post thread from one forum channel to another.")
        async def transfer_forum(
            interaction: discord.Interaction,
            source: discord.ForumChannel,
            target: discord.ForumChannel,
            include_bots: bool = True,
            mp3_only: bool = False,
            audio_only: bool = False,
        ) -> None:
            if interaction.guild is None:
                await self.reply_ephemeral(interaction, "This command can only be used in a server.")
                return
            if not await self.ensure_permissions(interaction, "`/transfer`", manage_messages=True):
                return
            if source.id == target.id:
                await self.reply_ephemeral(interaction, "Source and target channels must be different.")
                return
            if mp3_only and audio_only:
                await self.reply_ephemeral(interaction, "Choose either `mp3_only` or `audio_only`, not both.")
                return
            await self.defer_ephemeral(interaction)
            last_progress_at = 0.0

            async def progress(found: int, archived_scanned: int) -> None:
                nonlocal last_progress_at
                if self.loop.time() - last_progress_at < 2.5:
                    return
                last_progress_at = self.loop.time()
                await interaction.edit_original_response(
                    content=f"Scanning forum {source.mention}...\nForum posts found: {found}\nArchived scanned: {archived_scanned}"
                )

            threads = await collect_forum_threads(source, progress)
            if not threads:
                await interaction.edit_original_response(content=f"No forum posts were found in {source.mention}.")
                return
            copied = 0
            failures: list[str] = []
            assert self.http_session is not None
            for thread in threads:
                try:
                    transferred = await copy_forum_thread(
                        source,
                        target,
                        thread,
                        include_bots,
                        self.http_session,
                        mp3_only=mp3_only,
                        audio_only=audio_only,
                    )
                    if transferred:
                        copied += 1
                except Exception as error:  # noqa: BLE001
                    failures.append(f"`{thread.id}` ({thread.name}): {error}")
                if copied and copied % 5 == 0:
                    await interaction.edit_original_response(
                        content=f"Copying forum {source.mention} to {target.mention}...\nCopied posts: {copied}/{len(threads)}\nFailed: {len(failures)}"
                    )
            await interaction.edit_original_response(
                content=build_summary(
                    copied=copied,
                    failures=failures,
                    source=source,
                    target=target,
                    scanned=len(threads),
                    requested_all=True,
                    unit_label="forum post thread(s)",
                )
            )

        @transfer_group.command(name="thread", description="Copy any forum or thread into any forum or thread.")
        async def transfer_thread(
            interaction: discord.Interaction,
            source_thread: Optional[discord.Thread] = None,
            source_thread_id: Optional[str] = None,
            source_forum: Optional[discord.ForumChannel] = None,
            target_thread: Optional[discord.Thread] = None,
            target_thread_id: Optional[str] = None,
            target_forum: Optional[discord.ForumChannel] = None,
            include_bots: bool = True,
            mp3_only: bool = False,
            audio_only: bool = False,
        ) -> None:
            if interaction.guild is None:
                await self.reply_ephemeral(interaction, "This command can only be used in a server.")
                return
            if not await self.ensure_permissions(interaction, "`/transfer`", manage_messages=True):
                return
            if mp3_only and audio_only:
                await self.reply_ephemeral(interaction, "Choose either `mp3_only` or `audio_only`, not both.")
                return

            source_thread_from_id = None
            target_thread_from_id = None
            if source_thread_id:
                source_thread_from_id = await self.resolve_thread_reference(interaction.guild, source_thread_id)
                if source_thread_from_id is None:
                    await self.reply_ephemeral(
                        interaction,
                        "I couldn't resolve that source thread ID or link.",
                    )
                    return
            if target_thread_id:
                target_thread_from_id = await self.resolve_thread_reference(interaction.guild, target_thread_id)
                if target_thread_from_id is None:
                    await self.reply_ephemeral(
                        interaction,
                        "I couldn't resolve that target thread ID or link.",
                    )
                    return

            selected_sources = [
                item for item in (source_thread, source_thread_from_id, source_forum) if item is not None
            ]
            selected_targets = [
                item for item in (target_thread, target_thread_from_id, target_forum) if item is not None
            ]
            if len(selected_sources) != 1:
                await self.reply_ephemeral(
                    interaction,
                    "Choose exactly one source: either a forum, a thread, or a thread ID/link.",
                )
                return
            if len(selected_targets) != 1:
                await self.reply_ephemeral(
                    interaction,
                    "Choose exactly one target: either a forum, a thread, or a thread ID/link.",
                )
                return

            source_obj = selected_sources[0]
            target_obj = selected_targets[0]
            if source_obj.id == target_obj.id:
                await self.reply_ephemeral(interaction, "Source and target must be different.")
                return

            await self.defer_ephemeral(interaction)
            assert self.http_session is not None
            failures: list[str] = []

            try:
                resolved_source_thread = source_thread or source_thread_from_id
                resolved_target_thread = target_thread or target_thread_from_id

                if source_forum is not None and target_forum is not None:
                    copied = 0
                    threads = await collect_forum_threads(source_forum, None)
                    for thread in threads:
                        transferred = await copy_forum_thread(
                            source_forum,
                            target_forum,
                            thread,
                            include_bots,
                            self.http_session,
                            mp3_only=mp3_only,
                            audio_only=audio_only,
                        )
                        if transferred:
                            copied += 1
                    unit_label = "forum post thread(s)"
                    target_label = target_forum.mention
                elif source_forum is not None and resolved_target_thread is not None:
                    copied = await copy_forum_to_thread(
                        source_forum,
                        resolved_target_thread,
                        include_bots,
                        self.http_session,
                        mp3_only=mp3_only,
                        audio_only=audio_only,
                    )
                    unit_label = "message(s)"
                    target_label = resolved_target_thread.mention
                elif resolved_source_thread is not None and target_forum is not None:
                    source_parent_forum = (
                        resolved_source_thread.parent
                        if isinstance(resolved_source_thread.parent, discord.ForumChannel)
                        else None
                    )
                    transferred = await copy_thread_to_forum(
                        resolved_source_thread,
                        target_forum,
                        include_bots,
                        self.http_session,
                        source_parent_forum,
                        mp3_only=mp3_only,
                        audio_only=audio_only,
                    )
                    copied = 1 if transferred else 0
                    unit_label = "thread(s)"
                    target_label = target_forum.mention
                else:
                    copied = await copy_thread_to_channel(
                        resolved_source_thread,
                        resolved_target_thread,
                        include_bots,
                        self.http_session,
                        mp3_only=mp3_only,
                        audio_only=audio_only,
                    )
                    unit_label = "message(s)"
                    target_label = resolved_target_thread.mention
            except Exception as error:  # noqa: BLE001
                failures.append(str(error))
                copied = 0
                unit_label = "message(s)"
                target_label = target_obj.mention

            await interaction.edit_original_response(
                content="\n".join(
                    [
                        f"Copied {copied} {unit_label} from {source_obj.mention} to {target_label}.",
                        f"Failed: {len(failures)}",
                        *failures[:10],
                    ]
                )
            )

        tree.add_command(automation_help)
        tree.add_command(botstatus)
        tree.add_command(reminder_group)
        tree.add_command(job_group)
        tree.add_command(tag_group)
        tree.add_command(template_group)
        tree.add_command(checklist_group)
        tree.add_command(todo_group)
        tree.add_command(approval_group)
        tree.add_command(whisper_group)
        tree.add_command(warn_group)
        tree.add_command(dmlog_group)
        tree.add_command(note_group)
        tree.add_command(timeout_group)
        tree.add_command(autoresponse_group)
        tree.add_command(autorole_group)
        tree.add_command(bulkrole_group)
        tree.add_command(check_group)
        tree.add_command(audio_group)
        tree.add_command(channel_group)
        tree.add_command(cleanup_group)
        tree.add_command(logchannel_group)
        tree.add_command(heartbeat_group)
        tree.add_command(transfer_group)


async def run_bot() -> None:
    bot = MotionXBot()
    async with bot:
        await bot.start(bot.config.token)
