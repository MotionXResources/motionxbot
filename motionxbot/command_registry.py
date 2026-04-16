from __future__ import annotations

import re
import traceback
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

import discord
from discord import app_commands

from .time_utils import format_duration, parse_duration, render_discord_timestamp
from .transfer import (
    HARD_LIMIT,
    PROGRESS_UPDATE_INTERVAL,
    build_summary,
    collect_forum_threads,
    collect_messages,
    copy_forum_to_thread,
    copy_forum_thread,
    copy_thread_to_channel,
    copy_thread_to_forum,
    filter_messages_for_transfer,
    repost_message,
)


def now_ms() -> int:
    return int(datetime.now(tz=timezone.utc).timestamp() * 1000)


def make_id() -> str:
    return uuid4().hex[:8]


def register_commands(bot) -> None:
    tree = bot.tree

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
            await bot.reply_ephemeral(interaction, message[:1900])
        except discord.HTTPException:
            pass

    channel_group = app_commands.Group(name="channel", description="Timed deletion tools for the current channel, thread, or forum.")

    @channel_group.command(name="delete-in", description="Delete this channel, thread, or forum after a delay.")
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
            await bot.reply_ephemeral(interaction, "This command can only be used in a server.")
            return
        if not await bot.ensure_permissions(interaction, "`/channel delete-in`", manage_channels=True):
            return

        target = bot.get_timer_target_from_context(interaction.channel)
        if target is None:
            await bot.reply_ephemeral(interaction, "Use this inside the channel, thread, or forum you want to schedule.")
            return
        if only_if_idle and isinstance(target, discord.ForumChannel):
            await bot.reply_ephemeral(interaction, "`only_if_idle` works for text channels and threads, not whole forums.")
            return

        delay_ms = parse_duration(after)
        if delay_ms is None or delay_ms < 60 * 1000:
            await bot.reply_ephemeral(interaction, "Use a deletion delay of at least `1m`.")
            return

        warning_offsets = bot.build_channel_timer_warning_offsets(delay_ms, warn_at)
        if warn_at is not None and not warning_offsets and delay_ms >= 2 * 60 * 1000:
            await bot.reply_ephemeral(interaction, "I couldn't parse `warn_at`. Use something like `1h,10m,1m`.")
            return

        guild_data = bot.store.get_guild_data(interaction.guild.id)
        guild_data["channelTimers"] = [
            item for item in guild_data.get("channelTimers", []) if str(item["channelId"]) != str(target.id)
        ]
        guild_data["channelTimers"].append(
            {
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
        )
        bot.store.save()
        warnings = ", ".join(format_duration(offset) for offset in warning_offsets) if warning_offsets else "none"
        delete_at = guild_data["channelTimers"][-1]["deleteAt"]
        await bot.reply_ephemeral(
            interaction,
            f"{target.mention} will delete {render_discord_timestamp(delete_at)}. Warnings: {warnings}. Idle-only: {'yes' if only_if_idle else 'no'}.",
        )

    @channel_group.command(name="delete-status", description="Show the deletion timer for this channel, thread, or forum.")
    async def channel_delete_status(interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await bot.reply_ephemeral(interaction, "This command can only be used in a server.")
            return
        target = bot.get_timer_target_from_context(interaction.channel)
        if target is None:
            await bot.reply_ephemeral(interaction, "Use this inside the channel, thread, or forum you want to inspect.")
            return
        timer = next((item for item in bot.store.get_guild_data(interaction.guild.id).get("channelTimers", []) if str(item.get("channelId")) == str(target.id)), None)
        if timer is None:
            await bot.reply_ephemeral(interaction, f"No deletion timer is active for {target.mention}.")
            return
        remaining_ms = max(0, int(timer["deleteAt"]) - now_ms())
        warnings = ", ".join(format_duration(int(offset)) for offset in timer.get("warningOffsetsMs") or []) or "none"
        await bot.reply_ephemeral(interaction, f"{target.mention} deletes {render_discord_timestamp(int(timer['deleteAt']))} ({format_duration(remaining_ms)} from now).\nWarnings: {warnings}")

    @channel_group.command(name="delete-cancel", description="Cancel the deletion timer for this channel, thread, or forum.")
    async def channel_delete_cancel(interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await bot.reply_ephemeral(interaction, "This command can only be used in a server.")
            return
        if not await bot.ensure_permissions(interaction, "`/channel delete-cancel`", manage_channels=True):
            return
        target = bot.get_timer_target_from_context(interaction.channel)
        if target is None:
            await bot.reply_ephemeral(interaction, "Use this inside the channel, thread, or forum you want to keep.")
            return
        guild_data = bot.store.get_guild_data(interaction.guild.id)
        before = len(guild_data.get("channelTimers", []))
        guild_data["channelTimers"] = [item for item in guild_data.get("channelTimers", []) if str(item["channelId"]) != str(target.id)]
        bot.store.save()
        await bot.reply_ephemeral(interaction, f"Deletion timer cancelled for {target.mention}." if len(guild_data["channelTimers"]) != before else f"No deletion timer was active for {target.mention}.")

    check_group = app_commands.Group(name="check", description="Check how many messages a user has sent.")

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
            await bot.reply_ephemeral(interaction, "This command can only be used in a server.")
            return
        if not await bot.ensure_permissions(interaction, "`/check messages`", manage_messages=True):
            return
        selected_sources = [item for item in (source_channel, source_thread, source_forum) if item is not None]
        if len(selected_sources) > 1:
            await bot.reply_ephemeral(interaction, "Choose only one source at a time.")
            return
        since_ms = parse_duration(since) if since else None
        if since and since_ms is None:
            await bot.reply_ephemeral(interaction, "I couldn't parse that duration. Try `1d`, `12h`, or `30m`.")
            return
        await bot.defer_ephemeral(interaction)
        counts, scanned_messages, scanned_sources = await bot.count_messages_by_author(
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
        await interaction.edit_original_response(content=f"{member.mention} sent **{total}** message(s) {bot.describe_check_scope(source_channel=source_channel, source_thread=source_thread, source_forum=source_forum)}{duration_label}.\nSources scanned: {scanned_sources}\nMessages scanned: {scanned_messages}")

    whisper_group = app_commands.Group(name="whisper", description="Send a bot-authored message anywhere you choose.")

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
            await bot.reply_ephemeral(interaction, "This command can only be used in a server.")
            return
        if not await bot.ensure_permissions(interaction, "`/whisper`", manage_messages=True):
            return
        selected_targets = [item for item in (target_channel, target_thread, target_forum, target_category, target_user) if item is not None]
        if len(selected_targets) != 1:
            await bot.reply_ephemeral(interaction, "Choose exactly one target: channel, thread, forum, category, or user.")
            return
        await bot.defer_ephemeral(interaction)
        delivered, delivered_labels = await bot.send_whisper_to_target(
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
        await interaction.edit_original_response(content="Nothing was delivered." if delivered == 0 else f"Delivered to {', '.join(delivered_labels[:10])}.")

    warn_group = app_commands.Group(name="warn", description="DM custom warnings to users.")

    @warn_group.command(name="send", description="Send a custom warning DM and save it to the warning log.")
    async def warn_send(interaction: discord.Interaction, member: discord.User, message: str, title: Optional[str] = None) -> None:
        if interaction.guild is None:
            await bot.reply_ephemeral(interaction, "This command can only be used in a server.")
            return
        if not await bot.ensure_permissions(interaction, "`/warn`", manage_messages=True):
            return
        subject = title or f"Warning from {interaction.guild.name}"
        body = f"**{subject}**\n\n{message}"
        dm_delivered = True
        try:
            await member.send(body, allowed_mentions=discord.AllowedMentions.none())
            bot.record_dm_log(user_id=member.id, direction="outbound", content=body, actor_id=interaction.user.id, context="warning")
        except discord.HTTPException:
            dm_delivered = False
        guild_data = bot.store.get_guild_data(interaction.guild.id)
        guild_data["warnings"].append({"id": make_id(), "userId": str(member.id), "actorId": str(interaction.user.id), "title": subject, "message": message, "dmDelivered": dm_delivered, "createdAt": now_ms()})
        guild_data["warnings"] = guild_data["warnings"][-250:]
        bot.store.save()
        await bot.reply_ephemeral(interaction, f"Warning sent to {member.mention}." if dm_delivered else f"Warning logged for {member.mention}, but the DM failed.")

    dmlog_group = app_commands.Group(name="dmlog", description="Inspect DMs handled by the bot.")

    @dmlog_group.command(name="user", description="Show DM history for one user.")
    async def dmlog_user(interaction: discord.Interaction, user: discord.User, limit: app_commands.Range[int, 1, 20] = 10) -> None:
        if interaction.guild is None:
            await bot.reply_ephemeral(interaction, "This command can only be used in a server.")
            return
        if not await bot.ensure_permissions(interaction, "`/dmlog`", manage_messages=True):
            return
        logs = [item for item in reversed(bot.store.data.get("dmLogs", [])) if str(item.get("userId")) == str(user.id)][:limit]
        if not logs:
            await bot.reply_ephemeral(interaction, f"No DM logs found for {user}.")
            return
        lines = [f"{item['direction']} - {render_discord_timestamp(item['createdAt'])} - {item.get('context') or 'dm'} - {str(item['content'])[:120]}" for item in logs]
        await bot.reply_ephemeral(interaction, "\n".join(lines))

    @dmlog_group.command(name="recent", description="Show the most recent DM activity handled by the bot.")
    async def dmlog_recent(interaction: discord.Interaction, limit: app_commands.Range[int, 1, 20] = 10) -> None:
        if interaction.guild is None:
            await bot.reply_ephemeral(interaction, "This command can only be used in a server.")
            return
        if not await bot.ensure_permissions(interaction, "`/dmlog`", manage_messages=True):
            return
        logs = list(reversed(bot.store.data.get("dmLogs", [])))[:limit]
        if not logs:
            await bot.reply_ephemeral(interaction, "No DM activity has been logged yet.")
            return
        lines = [f"{item['direction']} - <@{item['userId']}> - {render_discord_timestamp(item['createdAt'])} - {str(item['content'])[:100]}" for item in logs]
        await bot.reply_ephemeral(interaction, "\n".join(lines))

    audio_group = app_commands.Group(name="audio", description="Search audio files and manage submission review.")

    @audio_group.command(name="setup", description="Configure the private audio submission review system.")
    async def audio_setup(
        interaction: discord.Interaction,
        review_category: discord.CategoryChannel,
        reviewer_role: discord.Role,
        destination_channel: Optional[discord.TextChannel] = None,
        destination_thread: Optional[discord.Thread] = None,
        destination_forum: Optional[discord.ForumChannel] = None,
        log_channel: Optional[discord.TextChannel] = None,
        close_after: Optional[str] = None,
    ) -> None:
        if interaction.guild is None:
            await bot.reply_ephemeral(interaction, "This command can only be used in a server.")
            return
        if not await bot.ensure_permissions(interaction, "`/audio setup`", manage_guild=True):
            return
        destinations = [item for item in (destination_channel, destination_thread, destination_forum) if item is not None]
        if len(destinations) != 1:
            await bot.reply_ephemeral(interaction, "Choose exactly one destination: channel, thread, or forum.")
            return
        close_delay_ms = 10 * 60 * 1000
        if close_after:
            parsed_close = parse_duration(close_after)
            if parsed_close is None or parsed_close < 60 * 1000:
                await bot.reply_ephemeral(interaction, "Use a cleanup delay of at least `1m`.")
                return
            close_delay_ms = parsed_close
        destination = destinations[0]
        destination_type = "forum" if isinstance(destination, discord.ForumChannel) else "thread" if isinstance(destination, discord.Thread) else "channel"
        settings = bot.get_audio_review_settings(interaction.guild.id)
        settings.update(
            {
                "reviewCategoryId": str(review_category.id),
                "reviewerRoleId": str(reviewer_role.id),
                "destinationType": destination_type,
                "destinationId": str(destination.id),
                "logChannelId": str(log_channel.id) if log_channel else None,
                "closeDelayMs": close_delay_ms,
            }
        )
        bot.store.save()
        await interaction.response.send_message(embed=bot.build_audio_review_settings_embed(interaction.guild, settings), ephemeral=True)

    @audio_group.command(name="settings", description="Show the current audio submission review settings.")
    async def audio_settings(interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await bot.reply_ephemeral(interaction, "This command can only be used in a server.")
            return
        if not await bot.ensure_permissions(interaction, "`/audio settings`", manage_guild=True):
            return
        settings = bot.get_audio_review_settings(interaction.guild.id)
        await interaction.response.send_message(embed=bot.build_audio_review_settings_embed(interaction.guild, settings), ephemeral=True)

    @audio_group.command(name="submit", description="Open your private audio submission review channel.")
    async def audio_submit(interaction: discord.Interaction, title: Optional[str] = None, notes: Optional[str] = None) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await bot.reply_ephemeral(interaction, "This command can only be used in a server.")
            return
        settings = bot.get_audio_review_settings(interaction.guild.id)
        if not settings.get("reviewCategoryId") or not settings.get("reviewerRoleId") or not settings.get("destinationId"):
            await bot.reply_ephemeral(interaction, "Audio review is not configured yet. Ask an admin to run `/audio setup`.")
            return
        guild_data = bot.store.get_guild_data(interaction.guild.id)
        existing = next((item for item in guild_data.get("audioSubmissions", []) if str(item.get("submitterId")) == str(interaction.user.id) and item.get("status") not in {"approved", "denied", "closed"}), None)
        if existing:
            await bot.reply_ephemeral(interaction, f"You already have an open submission in <#{existing['channelId']}>.")
            return
        category = interaction.guild.get_channel(int(settings["reviewCategoryId"]))
        reviewer_role = interaction.guild.get_role(int(settings["reviewerRoleId"]))
        if not isinstance(category, discord.CategoryChannel) or reviewer_role is None:
            await bot.reply_ephemeral(interaction, "The configured review category or reviewer role no longer exists. Run `/audio setup` again.")
            return
        safe_name = re.sub(r"[^a-z0-9-]+", "-", f"audio-{interaction.user.display_name}".lower()).strip("-")[:90] or "audio-submission"
        overwrites = {
            interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, attach_files=True, embed_links=True),
            reviewer_role: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, attach_files=True, embed_links=True),
        }
        if interaction.guild.me is not None:
            overwrites[interaction.guild.me] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_channels=True, manage_messages=True, attach_files=True, embed_links=True)
        review_channel = await interaction.guild.create_text_channel(name=safe_name, category=category, topic=f"Audio submission review for {interaction.user} ({interaction.user.id})", overwrites=overwrites, reason=f"Audio submission opened by {interaction.user}")
        submission = {"id": make_id(), "channelId": str(review_channel.id), "submitterId": str(interaction.user.id), "title": title or f"Audio from {interaction.user.display_name}", "notes": (notes or "").strip() or None, "status": "waiting", "audioMessageId": None, "reviewMessageId": None, "attachmentNames": [], "createdAt": now_ms()}
        guild_data.setdefault("audioSubmissions", []).append(submission)
        guild_data["audioSubmissions"] = guild_data["audioSubmissions"][-300:]
        bot.store.save()
        await review_channel.send(embed=bot.build_submission_open_embed(interaction.user, submission["title"], submission.get("notes")))
        await bot.update_submission_review_message(interaction.guild, review_channel, submission, interaction.user, state="waiting")
        bot.store.save()
        await bot.reply_ephemeral(interaction, f"Your private submission room is ready: {review_channel.mention}")

    @audio_group.command(name="status", description="Show the submission status for the current private review channel.")
    async def audio_status(interaction: discord.Interaction) -> None:
        if interaction.guild is None or not isinstance(interaction.channel, discord.TextChannel):
            await bot.reply_ephemeral(interaction, "Run this inside a submission review channel.")
            return
        submission = bot.get_audio_submission_for_channel(interaction.guild.id, interaction.channel.id)
        if submission is None:
            await bot.reply_ephemeral(interaction, "This channel is not an active audio review room.")
            return
        creator = interaction.guild.get_member(int(submission["submitterId"])) or await bot.fetch_user(int(submission["submitterId"]))
        await interaction.response.send_message(embed=bot.build_submission_review_embed(submission, creator, state=str(submission.get("status") or "waiting")), ephemeral=True)

    @audio_group.command(name="approve", description="Approve the current review channel's audio and post it to the configured destination.")
    async def audio_approve(interaction: discord.Interaction, note: Optional[str] = None) -> None:
        if interaction.guild is None or not isinstance(interaction.channel, discord.TextChannel) or not isinstance(interaction.user, discord.Member):
            await bot.reply_ephemeral(interaction, "Run this inside the private review channel.")
            return
        settings = bot.get_audio_review_settings(interaction.guild.id)
        if not bot.user_can_review_audio(interaction.user, settings):
            await bot.reply_ephemeral(interaction, "Only reviewers or admins can approve submissions.")
            return
        submission = bot.get_audio_submission_for_channel(interaction.guild.id, interaction.channel.id)
        if submission is None:
            await bot.reply_ephemeral(interaction, "This channel is not an active audio review room.")
            return
        if not submission.get("audioMessageId"):
            await bot.reply_ephemeral(interaction, "No audio has been uploaded in this review room yet.")
            return
        await bot.defer_ephemeral(interaction)
        destination_label, jump_url = await bot.post_submission_to_destination(interaction.guild, submission, interaction.user)
        creator = interaction.guild.get_member(int(submission["submitterId"])) or await bot.fetch_user(int(submission["submitterId"]))
        submission["status"] = "approved"
        submission["reviewedBy"] = str(interaction.user.id)
        submission["decisionAt"] = now_ms()
        submission["decisionNote"] = note
        submission["destinationLabel"] = destination_label
        submission["destinationJumpUrl"] = jump_url
        await bot.update_submission_review_message(interaction.guild, interaction.channel, submission, creator, state="approved", reviewer=interaction.user, decision_note=note)
        try:
            body = f"Your audio submission **{submission['title']}** was approved and posted to {destination_label}."
            if jump_url:
                body += f"\nOpen it here: {jump_url}"
            if note:
                body += f"\nReviewer note: {note}"
            await creator.send(body, allowed_mentions=discord.AllowedMentions.none())
            bot.record_dm_log(user_id=creator.id, direction="outbound", content=body, actor_id=interaction.user.id, context="audio-approval")
        except discord.HTTPException:
            pass
        await bot.log_audio_review_event(interaction.guild, settings, f"Approved submission `{submission['id']}` from <@{submission['submitterId']}> and posted it to {destination_label}.")
        await bot.schedule_submission_channel_cleanup(interaction.guild, interaction.channel.id, interaction.user.id, int(settings.get("closeDelayMs") or 10 * 60 * 1000), "MotionXBot submission review cleanup")
        bot.store.save()
        await interaction.edit_original_response(content=f"Approved and posted to {destination_label}. This review channel will be cleaned up soon.")

    @audio_group.command(name="deny", description="Deny the current review channel's submission.")
    async def audio_deny(interaction: discord.Interaction, reason: str) -> None:
        if interaction.guild is None or not isinstance(interaction.channel, discord.TextChannel) or not isinstance(interaction.user, discord.Member):
            await bot.reply_ephemeral(interaction, "Run this inside the private review channel.")
            return
        settings = bot.get_audio_review_settings(interaction.guild.id)
        if not bot.user_can_review_audio(interaction.user, settings):
            await bot.reply_ephemeral(interaction, "Only reviewers or admins can deny submissions.")
            return
        submission = bot.get_audio_submission_for_channel(interaction.guild.id, interaction.channel.id)
        if submission is None:
            await bot.reply_ephemeral(interaction, "This channel is not an active audio review room.")
            return
        creator = interaction.guild.get_member(int(submission["submitterId"])) or await bot.fetch_user(int(submission["submitterId"]))
        submission["status"] = "denied"
        submission["reviewedBy"] = str(interaction.user.id)
        submission["decisionAt"] = now_ms()
        submission["decisionNote"] = reason
        await bot.update_submission_review_message(interaction.guild, interaction.channel, submission, creator, state="denied", reviewer=interaction.user, decision_note=reason)
        try:
            body = f"Your audio submission **{submission['title']}** was denied.\nReason: {reason}"
            await creator.send(body, allowed_mentions=discord.AllowedMentions.none())
            bot.record_dm_log(user_id=creator.id, direction="outbound", content=body, actor_id=interaction.user.id, context="audio-denial")
        except discord.HTTPException:
            pass
        await bot.log_audio_review_event(interaction.guild, settings, f"Denied submission `{submission['id']}` from <@{submission['submitterId']}>. Reason: {reason}")
        await bot.schedule_submission_channel_cleanup(interaction.guild, interaction.channel.id, interaction.user.id, int(settings.get("closeDelayMs") or 10 * 60 * 1000), "MotionXBot submission review cleanup")
        bot.store.save()
        await bot.reply_ephemeral(interaction, "Submission denied. This review channel will be cleaned up soon.")

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
            await bot.reply_ephemeral(interaction, "This command can only be used in a server.")
            return
        selected_sources = [item for item in (source_channel, source_thread, source_forum) if item is not None]
        if len(selected_sources) > 1:
            await bot.reply_ephemeral(interaction, "Choose only one source override at a time.")
            return
        normalized_query = query.strip()
        if not normalized_query:
            await bot.reply_ephemeral(interaction, "Enter part of the audio filename you want to search for.")
            return
        await bot.defer_ephemeral(interaction)
        source_obj = selected_sources[0] if selected_sources else bot.get_default_audio_search_source(interaction.channel)
        if source_obj is None:
            await interaction.edit_original_response(content="Run this in the channel or thread you want to search, or choose a source override.")
            return
        matches = await bot.search_audio_in_source(source_obj, normalized_query, include_bots=True, interaction=interaction)
        if not matches:
            await interaction.edit_original_response(content=f"No audio attachments matching `{normalized_query}` were found in {source_obj.mention}.")
            return
        await bot.send_audio_search_results_interaction(interaction, normalized_query, matches[:limit])

    transfer_group = app_commands.Group(name="transfer", description="Copy messages and files between channels, threads, and forums.")

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
            await bot.reply_ephemeral(interaction, "This command can only be used in a server.")
            return
        if not await bot.ensure_permissions(interaction, "`/transfer`", manage_messages=True):
            return
        if source.id == target.id:
            await bot.reply_ephemeral(interaction, "Source and target channels must be different.")
            return
        if mp3_only and audio_only:
            await bot.reply_ephemeral(interaction, "Choose either `mp3_only` or `audio_only`, not both.")
            return
        await bot.defer_ephemeral(interaction)
        messages = await collect_messages(source, all_messages=False, limit=limit, before=before, include_bots=include_bots, on_progress=None)
        messages = filter_messages_for_transfer(messages, mp3_only, audio_only)
        if not messages:
            await interaction.edit_original_response(content="No transferable messages were found.")
            return
        copied = 0
        failures: list[str] = []
        assert bot.http_session is not None
        for message in messages:
            try:
                sent = await repost_message(target, message, bot.http_session, mp3_only=mp3_only, audio_only=audio_only)
                if sent:
                    copied += 1
            except Exception as error:  # noqa: BLE001
                failures.append(f"`{message.id}`: {error}")
            if copied and copied % PROGRESS_UPDATE_INTERVAL == 0:
                await interaction.edit_original_response(content=f"Transferring from {source.mention} to {target.mention}...\nCopied: {copied}/{len(messages)}\nFailed: {len(failures)}")
        await interaction.edit_original_response(content=build_summary(copied=copied, failures=failures, source=source, target=target, scanned=len(messages), requested_all=False))

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
            await bot.reply_ephemeral(interaction, "This command can only be used in a server.")
            return
        if not await bot.ensure_permissions(interaction, "`/transfer`", manage_messages=True):
            return
        if source.id == target.id:
            await bot.reply_ephemeral(interaction, "Source and target channels must be different.")
            return
        if mp3_only and audio_only:
            await bot.reply_ephemeral(interaction, "Choose either `mp3_only` or `audio_only`, not both.")
            return
        await bot.defer_ephemeral(interaction)
        last_progress_at = 0.0

        async def progress(scanned: int, queued: int) -> None:
            nonlocal last_progress_at
            if bot.loop.time() - last_progress_at < 2.5:
                return
            last_progress_at = bot.loop.time()
            await interaction.edit_original_response(content=f"Scanning {source.mention}...\nScanned: {scanned}\nQueued for transfer: {queued}")

        messages = await collect_messages(source, all_messages=True, limit=None, before=before, include_bots=include_bots, on_progress=progress)
        messages = filter_messages_for_transfer(messages, mp3_only, audio_only)
        if not messages:
            await interaction.edit_original_response(content="No transferable messages were found.")
            return
        copied = 0
        failures: list[str] = []
        assert bot.http_session is not None
        for message in messages:
            try:
                sent = await repost_message(target, message, bot.http_session, mp3_only=mp3_only, audio_only=audio_only)
                if sent:
                    copied += 1
            except Exception as error:  # noqa: BLE001
                failures.append(f"`{message.id}`: {error}")
            if copied and copied % PROGRESS_UPDATE_INTERVAL == 0:
                await interaction.edit_original_response(content=f"Transferring from {source.mention} to {target.mention}...\nCopied: {copied}/{len(messages)}\nFailed: {len(failures)}")
        await interaction.edit_original_response(content=build_summary(copied=copied, failures=failures, source=source, target=target, scanned=len(messages), requested_all=True))

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
            await bot.reply_ephemeral(interaction, "This command can only be used in a server.")
            return
        if not await bot.ensure_permissions(interaction, "`/transfer`", manage_messages=True):
            return
        if source.id == target.id:
            await bot.reply_ephemeral(interaction, "Source and target channels must be different.")
            return
        if mp3_only and audio_only:
            await bot.reply_ephemeral(interaction, "Choose either `mp3_only` or `audio_only`, not both.")
            return
        await bot.defer_ephemeral(interaction)
        last_progress_at = 0.0

        async def progress(found: int, archived_scanned: int) -> None:
            nonlocal last_progress_at
            if bot.loop.time() - last_progress_at < 2.5:
                return
            last_progress_at = bot.loop.time()
            await interaction.edit_original_response(content=f"Scanning forum {source.mention}...\nForum posts found: {found}\nArchived scanned: {archived_scanned}")

        threads = await collect_forum_threads(source, progress)
        if not threads:
            await interaction.edit_original_response(content=f"No forum posts were found in {source.mention}.")
            return
        copied = 0
        failures: list[str] = []
        assert bot.http_session is not None
        for thread in threads:
            try:
                transferred = await copy_forum_thread(source, target, thread, include_bots, bot.http_session, mp3_only=mp3_only, audio_only=audio_only)
                if transferred:
                    copied += 1
            except Exception as error:  # noqa: BLE001
                failures.append(f"`{thread.id}` ({thread.name}): {error}")
            if copied and copied % 5 == 0:
                await interaction.edit_original_response(content=f"Copying forum {source.mention} to {target.mention}...\nCopied posts: {copied}/{len(threads)}\nFailed: {len(failures)}")
        await interaction.edit_original_response(content=build_summary(copied=copied, failures=failures, source=source, target=target, scanned=len(threads), requested_all=True, unit_label="forum post thread(s)"))

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
            await bot.reply_ephemeral(interaction, "This command can only be used in a server.")
            return
        if not await bot.ensure_permissions(interaction, "`/transfer`", manage_messages=True):
            return
        if mp3_only and audio_only:
            await bot.reply_ephemeral(interaction, "Choose either `mp3_only` or `audio_only`, not both.")
            return
        source_thread_from_id = await bot.resolve_thread_reference(interaction.guild, source_thread_id) if source_thread_id else None
        target_thread_from_id = await bot.resolve_thread_reference(interaction.guild, target_thread_id) if target_thread_id else None
        if source_thread_id and source_thread_from_id is None:
            await bot.reply_ephemeral(interaction, "I couldn't resolve that source thread ID or link.")
            return
        if target_thread_id and target_thread_from_id is None:
            await bot.reply_ephemeral(interaction, "I couldn't resolve that target thread ID or link.")
            return
        selected_sources = [item for item in (source_thread, source_thread_from_id, source_forum) if item is not None]
        selected_targets = [item for item in (target_thread, target_thread_from_id, target_forum) if item is not None]
        if len(selected_sources) != 1 or len(selected_targets) != 1:
            await bot.reply_ephemeral(interaction, "Choose exactly one source and one target.")
            return
        source_obj = selected_sources[0]
        target_obj = selected_targets[0]
        if source_obj.id == target_obj.id:
            await bot.reply_ephemeral(interaction, "Source and target must be different.")
            return
        await bot.defer_ephemeral(interaction)
        assert bot.http_session is not None
        failures: list[str] = []
        try:
            resolved_source_thread = source_thread or source_thread_from_id
            resolved_target_thread = target_thread or target_thread_from_id
            if source_forum is not None and target_forum is not None:
                copied = 0
                threads = await collect_forum_threads(source_forum, None)
                for thread in threads:
                    transferred = await copy_forum_thread(source_forum, target_forum, thread, include_bots, bot.http_session, mp3_only=mp3_only, audio_only=audio_only)
                    if transferred:
                        copied += 1
                unit_label = "forum post thread(s)"
                target_label = target_forum.mention
            elif source_forum is not None and resolved_target_thread is not None:
                copied = await copy_forum_to_thread(source_forum, resolved_target_thread, include_bots, bot.http_session, mp3_only=mp3_only, audio_only=audio_only)
                unit_label = "message(s)"
                target_label = resolved_target_thread.mention
            elif resolved_source_thread is not None and target_forum is not None:
                source_parent_forum = resolved_source_thread.parent if isinstance(resolved_source_thread.parent, discord.ForumChannel) else None
                transferred = await copy_thread_to_forum(resolved_source_thread, target_forum, include_bots, bot.http_session, source_parent_forum, mp3_only=mp3_only, audio_only=audio_only)
                copied = 1 if transferred else 0
                unit_label = "thread(s)"
                target_label = target_forum.mention
            else:
                copied = await copy_thread_to_channel(resolved_source_thread, resolved_target_thread, include_bots, bot.http_session, mp3_only=mp3_only, audio_only=audio_only)
                unit_label = "message(s)"
                target_label = resolved_target_thread.mention
        except Exception as error:  # noqa: BLE001
            failures.append(str(error))
            copied = 0
            unit_label = "message(s)"
            target_label = target_obj.mention
        await interaction.edit_original_response(content="\n".join([f"Copied {copied} {unit_label} from {source_obj.mention} to {target_label}.", f"Failed: {len(failures)}", *failures[:10]]))

    tree.add_command(channel_group)
    tree.add_command(check_group)
    tree.add_command(whisper_group)
    tree.add_command(warn_group)
    tree.add_command(dmlog_group)
    tree.add_command(audio_group)
    tree.add_command(transfer_group)
