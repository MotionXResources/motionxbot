from __future__ import annotations

import io
from typing import Awaitable, Callable

import aiohttp
import discord

MAX_ATTACHMENTS_PER_MESSAGE = 10
MAX_CONTENT_LENGTH = 2000
HARD_LIMIT = 100
DEFAULT_UPLOAD_LIMIT_BYTES = 8 * 1024 * 1024
PROGRESS_UPDATE_INTERVAL = 25
AUDIO_FILE_EXTENSIONS = {
    ".aac",
    ".alac",
    ".flac",
    ".m4a",
    ".mp3",
    ".ogg",
    ".opus",
    ".wav",
    ".wma",
}


def split_content(text: str | None, first_chunk_budget: int) -> list[str]:
    normalized = str(text or "").strip()
    if not normalized:
        return []

    chunks: list[str] = []
    remaining = normalized
    budget = max(1, first_chunk_budget)

    while remaining:
        if len(remaining) <= budget:
            chunks.append(remaining)
            break

        split_index = remaining.rfind("\n", 0, budget)
        if split_index < budget // 2:
            split_index = remaining.rfind(" ", 0, budget)
        if split_index < budget // 2:
            split_index = budget

        chunks.append(remaining[:split_index].rstrip())
        remaining = remaining[split_index:].lstrip()
        budget = MAX_CONTENT_LENGTH

    return [chunk for chunk in chunks if chunk]


def split_text_by_limit(text: str) -> list[str]:
    return split_content(text, MAX_CONTENT_LENGTH)


def build_attachment_caption(author_id: int, filenames: list[str]) -> str:
    names = ", ".join(filenames)
    return f"{names} by <@{author_id}>"


def build_text_caption(author_id: int) -> str:
    return f"by <@{author_id}>"


def build_creator_caption(author_id: int) -> str:
    return f"created by <@{author_id}>"


def build_thread_source_label(source_thread: discord.Thread) -> str:
    parent_label = str(source_thread.parent) if source_thread.parent else "#forum"
    return f"{parent_label} / {source_thread.name}"


def get_message_snapshots(message: discord.Message) -> list[discord.MessageSnapshot]:
    return list(getattr(message, "message_snapshots", []) or [])


def get_effective_attachments(message: discord.Message) -> list[discord.Attachment]:
    attachments: list[discord.Attachment] = []
    seen_ids: set[int] = set()

    for attachment in list(message.attachments):
        if attachment.id in seen_ids:
            continue
        seen_ids.add(attachment.id)
        attachments.append(attachment)

    for snapshot in get_message_snapshots(message):
        for attachment in snapshot.attachments:
            if attachment.id in seen_ids:
                continue
            seen_ids.add(attachment.id)
            attachments.append(attachment)

    return attachments


def get_effective_content(message: discord.Message) -> str:
    base_content = (message.content or "").strip()
    if base_content:
        return base_content

    snapshot_contents = [
        snapshot.content.strip()
        for snapshot in get_message_snapshots(message)
        if snapshot.content and snapshot.content.strip()
    ]
    return "\n\n".join(snapshot_contents)


def get_message_creator_id(message: discord.Message) -> int | None:
    for snapshot in get_message_snapshots(message):
        cached_message = snapshot.cached_message
        if cached_message is not None and cached_message.author is not None:
            return cached_message.author.id

    if message.reference and isinstance(message.reference.resolved, discord.Message):
        return message.reference.resolved.author.id

    return message.author.id if message.author else None


def is_audio_attachment(attachment: discord.Attachment) -> bool:
    filename = attachment.filename.strip().lower()
    content_type = (attachment.content_type or "").lower()
    return any(filename.endswith(extension) for extension in AUDIO_FILE_EXTENSIONS) or content_type.startswith("audio/")


def is_mp3_attachment(attachment: discord.Attachment) -> bool:
    filename = attachment.filename.strip().lower()
    content_type = (attachment.content_type or "").lower()
    return filename.endswith(".mp3") or content_type in {"audio/mpeg", "audio/mp3"}


def is_audio_only_transfer(mp3_only: bool, audio_only: bool) -> bool:
    return mp3_only or audio_only


def matches_transfer_filter(
    attachment: discord.Attachment,
    mp3_only: bool,
    audio_only: bool,
) -> bool:
    if mp3_only:
        return is_mp3_attachment(attachment)
    if audio_only:
        return is_audio_attachment(attachment)
    return True


def filter_messages_for_transfer(
    messages: list[discord.Message], mp3_only: bool, audio_only: bool = False
) -> list[discord.Message]:
    if not is_audio_only_transfer(mp3_only, audio_only):
        return messages
    return [
        message
        for message in messages
        if any(matches_transfer_filter(attachment, mp3_only, audio_only) for attachment in get_effective_attachments(message))
    ]


def is_transferable_message(message: discord.Message, include_bots: bool) -> bool:
    if message.is_system():
        return False
    if not include_bots and message.author.bot:
        return False
    return True


async def build_attachment_batches(
    session: aiohttp.ClientSession,
    message: discord.Message,
    upload_limit_bytes: int,
    mp3_only: bool = False,
    audio_only: bool = False,
) -> tuple[list[list[discord.File]], list[list[str]], list[str]]:
    batches: list[list[discord.File]] = []
    filename_batches: list[list[str]] = []
    skipped: list[str] = []
    current_batch: list[discord.File] = []
    current_filenames: list[str] = []
    current_batch_bytes = 0

    attachments = [
        attachment
        for attachment in get_effective_attachments(message)
        if matches_transfer_filter(attachment, mp3_only, audio_only)
    ]

    for attachment in attachments:
        if attachment.size > upload_limit_bytes:
            skipped.append(f"{attachment.filename} (file exceeds the upload limit for reposting)")
            continue

        if (
            len(current_batch) >= MAX_ATTACHMENTS_PER_MESSAGE
            or current_batch_bytes + attachment.size > upload_limit_bytes
        ):
            if current_batch:
                batches.append(current_batch)
                filename_batches.append(current_filenames)
            current_batch = []
            current_filenames = []
            current_batch_bytes = 0

        async with session.get(attachment.url) as response:
            if response.status != 200:
                skipped.append(f"{attachment.filename} (download failed with {response.status})")
                continue
            payload = await response.read()

        current_batch_bytes += len(payload)
        current_batch.append(discord.File(io.BytesIO(payload), filename=attachment.filename))
        current_filenames.append(attachment.filename)

    if current_batch:
        batches.append(current_batch)
        filename_batches.append(current_filenames)

    return batches, filename_batches, skipped


async def send_skipped_attachment_notes(
    target_channel: discord.abc.Messageable, skipped: list[str]
) -> None:
    if not skipped:
        return

    note = f"Skipped attachment(s): {', '.join(skipped)}"
    for chunk in split_text_by_limit(note):
        await target_channel.send(
            chunk,
            allowed_mentions=discord.AllowedMentions.none(),
        )


async def repost_message(
    target_channel: discord.abc.Messageable,
    message: discord.Message,
    session: aiohttp.ClientSession,
    source_label: str | None = None,
    mp3_only: bool = False,
    audio_only: bool = False,
) -> bool:
    del source_label
    audio_transfer_only = is_audio_only_transfer(mp3_only, audio_only)
    content_chunks = split_content("" if audio_transfer_only else get_effective_content(message), MAX_CONTENT_LENGTH)
    upload_limit = getattr(message.guild, "filesize_limit", DEFAULT_UPLOAD_LIMIT_BYTES)
    creator_id = get_message_creator_id(message) or message.author.id
    file_batches, filename_batches, skipped = await build_attachment_batches(
        session,
        message,
        upload_limit,
        mp3_only=mp3_only,
        audio_only=audio_only,
    )
    first_file_batch = file_batches.pop(0) if file_batches else []
    first_filenames = filename_batches.pop(0) if filename_batches else []

    if not content_chunks and not first_file_batch:
        if not audio_transfer_only:
            await send_skipped_attachment_notes(target_channel, skipped)
        return False

    if audio_transfer_only:
        await target_channel.send(
            files=first_file_batch,
            allowed_mentions=discord.AllowedMentions.none(),
        )
    else:
        first_body = content_chunks.pop(0) if content_chunks else ""
        footer = (
            build_attachment_caption(creator_id, first_filenames)
            if first_file_batch
            else build_text_caption(creator_id)
        )
        first_payload = f"{first_body}\n\n{footer}" if first_body else footer
        await target_channel.send(
            first_payload,
            files=first_file_batch,
            allowed_mentions=discord.AllowedMentions.none(),
        )

        for chunk in content_chunks:
            await target_channel.send(chunk, allowed_mentions=discord.AllowedMentions.none())

    for batch, batch_filenames in zip(file_batches, filename_batches):
        if audio_transfer_only:
            del batch_filenames
            await target_channel.send(
                files=batch,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        else:
            await target_channel.send(
                build_attachment_caption(creator_id, batch_filenames),
                files=batch,
                allowed_mentions=discord.AllowedMentions.none(),
            )

    if audio_transfer_only:
        await target_channel.send(
            build_creator_caption(creator_id),
            allowed_mentions=discord.AllowedMentions.none(),
        )
    else:
        await send_skipped_attachment_notes(target_channel, skipped)
    return True


async def collect_messages(
    source: discord.abc.Messageable,
    *,
    all_messages: bool,
    limit: int | None,
    before: str | None,
    include_bots: bool,
    on_progress: Callable[[int, int], Awaitable[None]] | None,
) -> list[discord.Message]:
    collected: list[discord.Message] = []
    scanned = 0
    before_obj = discord.Object(id=int(before)) if before else None
    history_limit = None if all_messages else limit

    async for message in source.history(limit=history_limit, before=before_obj, oldest_first=False):
        scanned += 1
        if is_transferable_message(message, include_bots):
            collected.append(message)

        if on_progress and scanned % HARD_LIMIT == 0:
            await on_progress(scanned, len(collected))

    return sorted(collected, key=lambda item: item.created_at)


async def collect_forum_threads(
    source_forum: discord.ForumChannel,
    on_progress: Callable[[int, int], Awaitable[None]] | None,
) -> list[discord.Thread]:
    threads_by_id: dict[int, discord.Thread] = {}
    active_threads = await source_forum.guild.active_threads()

    for thread in active_threads:
        if thread.parent_id == source_forum.id:
            threads_by_id[thread.id] = thread

    archived_scanned = 0
    async for thread in source_forum.archived_threads(limit=None):
        threads_by_id[thread.id] = thread
        archived_scanned += 1
        if on_progress and archived_scanned % HARD_LIMIT == 0:
            await on_progress(len(threads_by_id), archived_scanned)

    return sorted(threads_by_id.values(), key=lambda item: item.created_at or discord.utils.utcnow())


def map_forum_tags(
    source_thread: discord.Thread,
    source_forum: discord.ForumChannel | None,
    target_forum: discord.ForumChannel,
) -> list[discord.ForumTag]:
    if source_forum is None:
        return []

    source_tags = {tag.id: tag.name.strip().lower() for tag in source_forum.available_tags}
    target_tags = {tag.name.strip().lower(): tag for tag in target_forum.available_tags}
    tag_ids = getattr(source_thread, "applied_tags", [])

    mapped: list[discord.ForumTag] = []
    for tag_id in tag_ids:
        source_name = source_tags.get(tag_id)
        if not source_name:
            continue
        target_tag = target_tags.get(source_name)
        if target_tag:
            mapped.append(target_tag)

    return mapped


async def create_forum_post(
    target_forum: discord.ForumChannel,
    source_forum: discord.ForumChannel | None,
    source_thread: discord.Thread,
    starter_message: discord.Message | None,
    session: aiohttp.ClientSession,
    mp3_only: bool = False,
    audio_only: bool = False,
) -> discord.Thread:
    audio_transfer_only = is_audio_only_transfer(mp3_only, audio_only)
    applied_tags = map_forum_tags(source_thread, source_forum, target_forum)
    upload_limit = getattr(target_forum.guild, "filesize_limit", DEFAULT_UPLOAD_LIMIT_BYTES)
    creator_id = get_message_creator_id(starter_message) if starter_message else None

    if starter_message:
        starter_chunks = split_content("" if audio_transfer_only else get_effective_content(starter_message), MAX_CONTENT_LENGTH)
        file_batches, filename_batches, skipped = await build_attachment_batches(
            session,
            starter_message,
            upload_limit,
            mp3_only=mp3_only,
            audio_only=audio_only,
        )
    else:
        starter_chunks = [
            "*Original starter message was unavailable, so this forum post was recreated from the remaining thread history.*"
        ]
        file_batches, filename_batches, skipped = [], [], []

    first_batch = file_batches.pop(0) if file_batches else []
    first_filenames = filename_batches.pop(0) if filename_batches else []
    first_chunk = starter_chunks.pop(0) if starter_chunks else ""
    first_footer = (
        build_attachment_caption(creator_id or starter_message.author.id, first_filenames)
        if starter_message and first_batch
        else build_text_caption(creator_id or starter_message.author.id)
        if starter_message and not audio_transfer_only
        else ""
    )
    first_payload = f"{first_chunk}\n\n{first_footer}" if first_chunk and first_footer else first_chunk or first_footer
    if audio_transfer_only and first_batch and not first_payload:
        first_payload = None
    created = await target_forum.create_thread(
        name=(source_thread.name or f"copied-thread-{source_thread.id}")[:100],
        auto_archive_duration=source_thread.auto_archive_duration,
        slowmode_delay=getattr(source_thread, "slowmode_delay", 0) or None,
        content=first_payload or ("*No text content.*" if not audio_transfer_only else None),
        files=first_batch,
        applied_tags=applied_tags,
        allowed_mentions=discord.AllowedMentions.none(),
    )

    new_thread = created.thread
    for chunk in starter_chunks:
        await new_thread.send(chunk, allowed_mentions=discord.AllowedMentions.none())

    for batch, batch_filenames in zip(file_batches, filename_batches):
        if audio_transfer_only:
            del batch_filenames
            await new_thread.send(
                files=batch,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        else:
            await new_thread.send(
                build_attachment_caption(creator_id or starter_message.author.id, batch_filenames)
                if starter_message
                else f"{source_thread.name} by @unknown",
                files=batch,
                allowed_mentions=discord.AllowedMentions.none(),
            )

    if audio_transfer_only and starter_message and first_batch:
        await new_thread.send(
            build_creator_caption(creator_id or starter_message.author.id),
            allowed_mentions=discord.AllowedMentions.none(),
        )
    elif not audio_transfer_only:
        await send_skipped_attachment_notes(new_thread, skipped)
    return new_thread


async def copy_forum_thread(
    source_forum: discord.ForumChannel,
    target_forum: discord.ForumChannel,
    source_thread: discord.Thread,
    include_bots: bool,
    session: aiohttp.ClientSession,
    mp3_only: bool = False,
    audio_only: bool = False,
) -> bool:
    return await copy_thread_to_forum(
        source_thread,
        target_forum,
        include_bots,
        session,
        source_forum,
        mp3_only,
        audio_only,
    )


async def copy_thread_to_forum(
    source_thread: discord.Thread,
    target_forum: discord.ForumChannel,
    include_bots: bool,
    session: aiohttp.ClientSession,
    source_forum: discord.ForumChannel | None = None,
    mp3_only: bool = False,
    audio_only: bool = False,
) -> bool:
    messages = await collect_messages(
        source_thread,
        all_messages=True,
        limit=None,
        before=None,
        include_bots=include_bots,
        on_progress=None,
    )
    messages = filter_messages_for_transfer(messages, mp3_only, audio_only)
    if not messages:
        return False

    starter_message = messages.pop(0) if messages else None
    created_thread = await create_forum_post(
        target_forum,
        source_forum,
        source_thread,
        starter_message,
        session,
        mp3_only=mp3_only,
        audio_only=audio_only,
    )
    source_label = build_thread_source_label(source_thread)

    for message in messages:
        await repost_message(
            created_thread,
            message,
            session,
            source_label,
            mp3_only=mp3_only,
            audio_only=audio_only,
        )

    kwargs = {"archived": source_thread.archived, "locked": source_thread.locked}
    if getattr(source_thread, "applied_tags", None):
        kwargs["applied_tags"] = map_forum_tags(source_thread, source_forum, target_forum)
    await created_thread.edit(**kwargs)
    return True


async def copy_thread_to_channel(
    source_thread: discord.Thread,
    target_channel: discord.abc.Messageable,
    include_bots: bool,
    session: aiohttp.ClientSession,
    mp3_only: bool = False,
    audio_only: bool = False,
    thread_label: str | None = None,
) -> int:
    messages = await collect_messages(
        source_thread,
        all_messages=True,
        limit=None,
        before=None,
        include_bots=include_bots,
        on_progress=None,
    )
    messages = filter_messages_for_transfer(messages, mp3_only, audio_only)
    if not messages:
        return 0

    source_label = build_thread_source_label(source_thread)
    copied = 0
    if thread_label and not is_audio_only_transfer(mp3_only, audio_only):
        await target_channel.send(
            thread_label,
            allowed_mentions=discord.AllowedMentions.none(),
        )
    for message in messages:
        sent = await repost_message(
            target_channel,
            message,
            session,
            source_label,
            mp3_only=mp3_only,
            audio_only=audio_only,
        )
        if sent:
            copied += 1
    return copied


async def copy_forum_to_thread(
    source_forum: discord.ForumChannel,
    target_channel: discord.abc.Messageable,
    include_bots: bool,
    session: aiohttp.ClientSession,
    mp3_only: bool = False,
    audio_only: bool = False,
) -> int:
    threads = await collect_forum_threads(source_forum, None)
    copied = 0

    for thread in threads:
        copied += await copy_thread_to_channel(
            thread,
            target_channel,
            include_bots,
            session,
            mp3_only=mp3_only,
            audio_only=audio_only,
            thread_label=f"**Thread:** {thread.name}",
        )

    return copied


def build_summary(
    *,
    copied: int,
    failures: list[str],
    source: discord.abc.GuildChannel,
    target: discord.abc.GuildChannel,
    scanned: int,
    requested_all: bool,
    unit_label: str = "message(s)",
) -> str:
    lines = [
        f"{'Transferred' if requested_all else 'Copied'} {copied} {unit_label} from {source} to {target}.",
        f"Scanned: {scanned}",
        f"Failed: {len(failures)}",
    ]
    if failures:
        lines.extend(failures[:10])
    return "\n".join(lines)
