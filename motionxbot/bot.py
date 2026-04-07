from __future__ import annotations

from datetime import datetime, timezone
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
    copy_forum_thread,
    copy_thread_to_channel,
    repost_message,
)


def now_ms() -> int:
    return int(datetime.now(tz=timezone.utc).timestamp() * 1000)


def make_id() -> str:
    return uuid4().hex[:8]


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

    def register_app_commands(self) -> None:
        tree = self.tree

        @app_commands.command(
            name="automation-help",
            description="See the bot's automation command categories.",
        )
        async def automation_help(interaction: discord.Interaction) -> None:
            lines = [
                "**Scheduling:** `/reminder`, `/job`, `/heartbeat`",
                "**Reusable content:** `/tag`, `/template`",
                "**Operational tracking:** `/checklist`, `/todo`, `/approval`",
                "**Server automation:** `/autorole`, `/bulkrole`, `/channel`, `/cleanup`, `/logchannel`, `/transfer messages`, `/transfer all`, `/transfer forum`, `/transfer thread`",
                "**Bot status:** `/botstatus`",
                "",
                "Most time fields accept compact durations like `15m`, `2h`, `1d`, or `1h30m`.",
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
                f"Autoroles: {len(guild_data['autoRoles'])}",
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

        transfer_group = app_commands.Group(name="transfer", description="Copy batches of messages and attachments between channels.")

        @transfer_group.command(name="messages", description="Copy recent messages from one text channel to another.")
        async def transfer_messages(
            interaction: discord.Interaction,
            source: discord.TextChannel,
            target: discord.TextChannel,
            limit: app_commands.Range[int, 1, HARD_LIMIT],
            before: Optional[str] = None,
            include_bots: bool = False,
        ) -> None:
            if interaction.guild is None:
                await self.reply_ephemeral(interaction, "This command can only be used in a server.")
                return
            if not await self.ensure_permissions(interaction, "`/transfer`", manage_messages=True):
                return
            if source.id == target.id:
                await self.reply_ephemeral(interaction, "Source and target channels must be different.")
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
                    await repost_message(target, message, self.http_session)
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
        ) -> None:
            if interaction.guild is None:
                await self.reply_ephemeral(interaction, "This command can only be used in a server.")
                return
            if not await self.ensure_permissions(interaction, "`/transfer`", manage_messages=True):
                return
            if source.id == target.id:
                await self.reply_ephemeral(interaction, "Source and target channels must be different.")
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
                    await repost_message(target, message, self.http_session)
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
        ) -> None:
            if interaction.guild is None:
                await self.reply_ephemeral(interaction, "This command can only be used in a server.")
                return
            if not await self.ensure_permissions(interaction, "`/transfer`", manage_messages=True):
                return
            if source.id == target.id:
                await self.reply_ephemeral(interaction, "Source and target channels must be different.")
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
                    await copy_forum_thread(source, target, thread, include_bots, self.http_session)
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

        @transfer_group.command(name="thread", description="Copy one specific thread into a channel, thread, or forum.")
        async def transfer_thread(
            interaction: discord.Interaction,
            source: discord.Thread,
            target_channel: Optional[discord.TextChannel] = None,
            target_thread: Optional[discord.Thread] = None,
            target_forum: Optional[discord.ForumChannel] = None,
            include_bots: bool = True,
        ) -> None:
            if interaction.guild is None:
                await self.reply_ephemeral(interaction, "This command can only be used in a server.")
                return
            if not await self.ensure_permissions(interaction, "`/transfer`", manage_messages=True):
                return

            selected_targets = [item for item in (target_channel, target_thread, target_forum) if item is not None]
            if len(selected_targets) != 1:
                await self.reply_ephemeral(
                    interaction,
                    "Choose exactly one target: a text channel, a thread, or a forum.",
                )
                return

            await self.defer_ephemeral(interaction)
            assert self.http_session is not None
            failures: list[str] = []

            try:
                if target_forum is not None:
                    if not isinstance(source.parent, discord.ForumChannel):
                        await interaction.edit_original_response(
                            content="That source thread is not inside a forum, so it cannot be recreated as a forum post."
                        )
                        return
                    await copy_forum_thread(source.parent, target_forum, source, include_bots, self.http_session)
                    copied = 1
                    unit_label = "thread"
                    target_label = target_forum.mention
                else:
                    destination = target_thread or target_channel
                    copied = await copy_thread_to_channel(source, destination, include_bots, self.http_session)
                    unit_label = "message(s)"
                    target_label = destination.mention
            except Exception as error:  # noqa: BLE001
                failures.append(str(error))
                copied = 0
                unit_label = "message(s)"
                target_label = selected_targets[0].mention

            await interaction.edit_original_response(
                content="\n".join(
                    [
                        f"Copied {copied} {unit_label} from {source.mention} to {target_label}.",
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
        tree.add_command(autorole_group)
        tree.add_command(bulkrole_group)
        tree.add_command(channel_group)
        tree.add_command(cleanup_group)
        tree.add_command(logchannel_group)
        tree.add_command(heartbeat_group)
        tree.add_command(transfer_group)


async def run_bot() -> None:
    bot = MotionXBot()
    async with bot:
        await bot.start(bot.config.token)
