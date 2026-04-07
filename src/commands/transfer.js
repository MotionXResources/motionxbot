const { AttachmentBuilder, ChannelType, SlashCommandBuilder } = require("discord.js");
const { ensureMemberPermissions, PermissionFlagsBits } = require("../utils/discord");

const MAX_ATTACHMENTS_PER_MESSAGE = 10;
const MAX_CONTENT_LENGTH = 2000;
const HARD_LIMIT = 100;
const DEFAULT_UPLOAD_LIMIT_BYTES = 8 * 1024 * 1024;
const PROGRESS_UPDATE_INTERVAL = 25;

function splitContent(text, firstChunkBudget) {
  const normalized = String(text || "").trim();
  if (!normalized) {
    return [];
  }

  const chunks = [];
  let remaining = normalized;
  let budget = Math.max(1, firstChunkBudget);

  while (remaining.length > 0) {
    if (remaining.length <= budget) {
      chunks.push(remaining);
      break;
    }

    let splitIndex = remaining.lastIndexOf("\n", budget);
    if (splitIndex < Math.floor(budget / 2)) {
      splitIndex = remaining.lastIndexOf(" ", budget);
    }
    if (splitIndex < Math.floor(budget / 2)) {
      splitIndex = budget;
    }

    chunks.push(remaining.slice(0, splitIndex).trimEnd());
    remaining = remaining.slice(splitIndex).trimStart();
    budget = MAX_CONTENT_LENGTH;
  }

  return chunks.filter(Boolean);
}

function splitTextByLimit(text) {
  return splitContent(text, MAX_CONTENT_LENGTH);
}

function buildHeader({ sourceLabel, timestamp, authorTag, authorId }) {
  return [
    `**Copied from** ${sourceLabel} - <t:${Math.floor(timestamp / 1000)}:f>`,
    `**Original author:** ${authorTag} (${authorId})`
  ].join("\n");
}

function buildThreadSourceLabel(sourceThread) {
  const parentLabel = sourceThread.parent ? sourceThread.parent.toString() : "#forum";
  return `${parentLabel} / ${sourceThread.name}`;
}

function isTransferableMessage(message, includeBots) {
  if (message.system) {
    return false;
  }

  if (!includeBots && message.author.bot) {
    return false;
  }

  return true;
}

async function buildAttachmentBatches(message, uploadLimitBytes) {
  const batches = [];
  const skipped = [];
  let currentBatch = [];
  let currentBatchBytes = 0;

  for (const attachment of message.attachments.values()) {
    if (attachment.size > uploadLimitBytes) {
      skipped.push(`${attachment.name || attachment.id} (file exceeds the upload limit for reposting)`);
      continue;
    }

    if (
      currentBatch.length >= MAX_ATTACHMENTS_PER_MESSAGE ||
      currentBatchBytes + attachment.size > uploadLimitBytes
    ) {
      if (currentBatch.length) {
        batches.push(currentBatch);
      }
      currentBatch = [];
      currentBatchBytes = 0;
    }

    const response = await fetch(attachment.url);
    if (!response.ok) {
      skipped.push(`${attachment.name || attachment.id} (download failed with ${response.status})`);
      continue;
    }

    const arrayBuffer = await response.arrayBuffer();
    currentBatchBytes += arrayBuffer.byteLength;
    currentBatch.push(
      new AttachmentBuilder(Buffer.from(arrayBuffer), {
        name: attachment.name || `attachment-${attachment.id}`
      })
    );
  }

  if (currentBatch.length) {
    batches.push(currentBatch);
  }

  return {
    fileBatches: batches,
    skipped
  };
}

async function sendSkippedAttachmentNotes(targetChannel, skipped) {
  if (!skipped.length) {
    return;
  }

  const note = `Skipped attachment(s): ${skipped.join(", ")}`;
  for (const chunk of splitTextByLimit(note)) {
    await targetChannel.send({
      content: chunk,
      allowedMentions: { parse: [] }
    });
  }
}

async function repostMessage(targetChannel, message, sourceLabel = message.channel.toString()) {
  const header = buildHeader({
    sourceLabel,
    timestamp: message.createdTimestamp,
    authorTag: message.author.tag,
    authorId: message.author.id
  });
  const firstChunkBudget = Math.max(1, MAX_CONTENT_LENGTH - header.length - 2);
  const contentChunks = splitContent(message.content || "", firstChunkBudget);
  const uploadLimitBytes = targetChannel.guild?.maximumUploadLimit || DEFAULT_UPLOAD_LIMIT_BYTES;
  const { fileBatches, skipped } = await buildAttachmentBatches(message, uploadLimitBytes);
  const firstFileBatch = fileBatches.shift() || [];

  if (!contentChunks.length && !firstFileBatch.length) {
    await targetChannel.send({
      content: `${header}\n*No plain-text content. Unsupported embeds, stickers, and some special message types are not copied exactly.*`,
      allowedMentions: { parse: [] }
    });
  } else {
    const firstBody = contentChunks.shift();
    await targetChannel.send({
      content: firstBody ? `${header}\n\n${firstBody}` : header,
      files: firstFileBatch,
      allowedMentions: { parse: [] }
    });

    for (const chunk of contentChunks) {
      await targetChannel.send({
        content: chunk,
        allowedMentions: { parse: [] }
      });
    }
  }

  for (const batch of fileBatches) {
    await targetChannel.send({
      content: `Attachment continuation for original message \`${message.id}\`.`,
      files: batch,
      allowedMentions: { parse: [] }
    });
  }

  await sendSkippedAttachmentNotes(targetChannel, skipped);
}

async function collectMessages(source, { all, limit, before, includeBots, onProgress }) {
  if (!all) {
    const fetched = await source.messages.fetch({
      limit,
      ...(before ? { before } : {})
    });

    return [...fetched.values()]
      .filter((message) => isTransferableMessage(message, includeBots))
      .sort((left, right) => left.createdTimestamp - right.createdTimestamp);
  }

  const collected = [];
  let cursor = before || null;
  let scanned = 0;

  while (true) {
    const fetched = await source.messages.fetch({
      limit: HARD_LIMIT,
      ...(cursor ? { before: cursor } : {})
    });

    if (!fetched.size) {
      break;
    }

    const pageMessages = [...fetched.values()];
    scanned += pageMessages.length;
    collected.push(
      ...pageMessages.filter((message) => isTransferableMessage(message, includeBots))
    );

    cursor = pageMessages[pageMessages.length - 1].id;

    if (onProgress) {
      await onProgress(scanned, collected.length);
    }

    if (fetched.size < HARD_LIMIT) {
      break;
    }
  }

  return collected.sort((left, right) => left.createdTimestamp - right.createdTimestamp);
}

async function collectForumThreads(sourceForum, onProgress) {
  const threadsById = new Map();
  const active = await sourceForum.threads.fetchActive();

  for (const thread of active.threads.values()) {
    threadsById.set(thread.id, thread);
  }

  let archivedPage = await sourceForum.threads.fetchArchived({
    type: "public",
    limit: HARD_LIMIT
  });

  let scannedArchived = 0;

  while (archivedPage?.threads?.size) {
    const pageThreads = [...archivedPage.threads.values()];
    for (const thread of pageThreads) {
      threadsById.set(thread.id, thread);
    }

    scannedArchived += pageThreads.length;
    if (onProgress) {
      await onProgress(threadsById.size, scannedArchived);
    }

    if (!archivedPage.hasMore || pageThreads.length < HARD_LIMIT) {
      break;
    }

    archivedPage = await sourceForum.threads.fetchArchived({
      type: "public",
      limit: HARD_LIMIT,
      before: pageThreads[pageThreads.length - 1]
    });
  }

  return [...threadsById.values()].sort((left, right) => left.createdTimestamp - right.createdTimestamp);
}

function mapForumTags(sourceThread, sourceForum, targetForum) {
  const sourceTags = new Map(
    sourceForum.availableTags.map((tag) => [tag.id, tag.name.trim().toLowerCase()])
  );
  const targetTags = new Map(
    targetForum.availableTags.map((tag) => [tag.name.trim().toLowerCase(), tag.id])
  );

  return sourceThread.appliedTags
    .map((tagId) => sourceTags.get(tagId))
    .filter(Boolean)
    .map((tagName) => targetTags.get(tagName))
    .filter(Boolean);
}

async function createForumPost(targetForum, sourceForum, sourceThread, starterMessage) {
  const sourceLabel = buildThreadSourceLabel(sourceThread);
  const uploadLimitBytes = targetForum.guild?.maximumUploadLimit || DEFAULT_UPLOAD_LIMIT_BYTES;
  const appliedTags = mapForumTags(sourceThread, sourceForum, targetForum);

  const baseHeader = starterMessage
    ? buildHeader({
        sourceLabel,
        timestamp: starterMessage.createdTimestamp,
        authorTag: starterMessage.author.tag,
        authorId: starterMessage.author.id
      })
    : `**Copied from** ${sourceLabel}`;

  const fallbackBody = "*Original starter message was unavailable, so this forum post was recreated from the remaining thread history.*";
  const starterText = starterMessage?.content || "";
  const starterChunks = splitContent(
    starterText || fallbackBody,
    Math.max(1, MAX_CONTENT_LENGTH - baseHeader.length - 2)
  );
  const { fileBatches, skipped } = starterMessage
    ? await buildAttachmentBatches(starterMessage, uploadLimitBytes)
    : { fileBatches: [], skipped: [] };

  const firstFileBatch = fileBatches.shift() || [];
  const firstChunk = starterChunks.shift();
  const threadName = sourceThread.name.slice(0, 100) || `copied-thread-${sourceThread.id}`;

  const createdThread = await targetForum.threads.create({
    name: threadName,
    autoArchiveDuration: sourceThread.autoArchiveDuration,
    rateLimitPerUser: sourceThread.rateLimitPerUser,
    appliedTags,
    message: {
      content: firstChunk ? `${baseHeader}\n\n${firstChunk}` : baseHeader,
      files: firstFileBatch,
      allowedMentions: { parse: [] }
    }
  });

  for (const chunk of starterChunks) {
    await createdThread.send({
      content: chunk,
      allowedMentions: { parse: [] }
    });
  }

  for (const batch of fileBatches) {
    await createdThread.send({
      content: `Attachment continuation for original starter message in \`${sourceThread.name}\`.`,
      files: batch,
      allowedMentions: { parse: [] }
    });
  }

  await sendSkippedAttachmentNotes(createdThread, skipped);

  return createdThread;
}

async function copyForumThread(sourceForum, targetForum, sourceThread, includeBots) {
  let starterMessage = await sourceThread.fetchStarterMessage().catch(() => null);
  const threadMessages = await collectMessages(sourceThread, {
    all: true,
    includeBots,
    onProgress: null
  });

  if (!starterMessage && threadMessages.length) {
    starterMessage = threadMessages.shift();
  } else if (starterMessage && threadMessages[0]?.id === starterMessage.id) {
    threadMessages.shift();
  }

  const createdThread = await createForumPost(targetForum, sourceForum, sourceThread, starterMessage);
  const sourceLabel = buildThreadSourceLabel(sourceThread);

  for (const message of threadMessages) {
    await repostMessage(createdThread, message, sourceLabel);
  }

  if (sourceThread.locked) {
    await createdThread.setLocked(true).catch(() => null);
  }

  if (sourceThread.archived) {
    await createdThread.setArchived(true).catch(() => null);
  }
}

function buildSummary({ copied, failures, source, target, scanned, requestedAll, unitLabel = "message(s)" }) {
  const summary = [
    `${requestedAll ? "Transferred" : "Copied"} ${copied} ${unitLabel} from ${source} to ${target}.`,
    `Scanned: ${scanned}`,
    failures.length ? `Failed: ${failures.length}` : "Failed: 0"
  ];

  if (failures.length) {
    summary.push(...failures.slice(0, 10));
  }

  return summary.join("\n");
}

module.exports = {
  data: new SlashCommandBuilder()
    .setName("transfer")
    .setDescription("Copy batches of messages and attachments between channels.")
    .addSubcommand((subcommand) =>
      subcommand
        .setName("messages")
        .setDescription("Copy recent messages from one text channel to another.")
        .addChannelOption((option) =>
          option
            .setName("source")
            .setDescription("Channel to copy from.")
            .addChannelTypes(ChannelType.GuildText, ChannelType.GuildAnnouncement)
            .setRequired(true)
        )
        .addChannelOption((option) =>
          option
            .setName("target")
            .setDescription("Channel to copy into.")
            .addChannelTypes(ChannelType.GuildText, ChannelType.GuildAnnouncement)
            .setRequired(true)
        )
        .addIntegerOption((option) =>
          option
            .setName("limit")
            .setDescription("How many recent messages to copy, max 100.")
            .setRequired(true)
            .setMinValue(1)
            .setMaxValue(HARD_LIMIT)
        )
        .addStringOption((option) =>
          option
            .setName("before")
            .setDescription("Optional message id. Copy messages before this one.")
        )
        .addBooleanOption((option) =>
          option
            .setName("include-bots")
            .setDescription("Whether to copy messages authored by bots too.")
        )
    )
    .addSubcommand((subcommand) =>
      subcommand
        .setName("all")
        .setDescription("Copy the full available history from one text channel to another.")
        .addChannelOption((option) =>
          option
            .setName("source")
            .setDescription("Channel to copy from.")
            .addChannelTypes(ChannelType.GuildText, ChannelType.GuildAnnouncement)
            .setRequired(true)
        )
        .addChannelOption((option) =>
          option
            .setName("target")
            .setDescription("Channel to copy into.")
            .addChannelTypes(ChannelType.GuildText, ChannelType.GuildAnnouncement)
            .setRequired(true)
        )
        .addStringOption((option) =>
          option
            .setName("before")
            .setDescription("Optional message id. Only copy messages before this id.")
        )
        .addBooleanOption((option) =>
          option
            .setName("include-bots")
            .setDescription("Whether to include bot-authored messages. Default: true.")
        )
    )
    .addSubcommand((subcommand) =>
      subcommand
        .setName("forum")
        .setDescription("Copy every forum post thread from one forum channel to another.")
        .addChannelOption((option) =>
          option
            .setName("source")
            .setDescription("Forum channel to copy from.")
            .addChannelTypes(ChannelType.GuildForum)
            .setRequired(true)
        )
        .addChannelOption((option) =>
          option
            .setName("target")
            .setDescription("Forum channel to copy into.")
            .addChannelTypes(ChannelType.GuildForum)
            .setRequired(true)
        )
        .addBooleanOption((option) =>
          option
            .setName("include-bots")
            .setDescription("Whether to include bot-authored messages. Default: true.")
        )
    ),
  async execute(interaction) {
    const hasPermission = await ensureMemberPermissions(
      interaction,
      PermissionFlagsBits.ManageMessages,
      "`/transfer`"
    );
    if (!hasPermission) {
      return;
    }

    await interaction.deferReply({ ephemeral: true });

    const subcommand = interaction.options.getSubcommand();
    const source = interaction.options.getChannel("source", true);
    const target = interaction.options.getChannel("target", true);
    const includeBotsOption = interaction.options.getBoolean("include-bots");

    if (source.id === target.id) {
      await interaction.editReply("Source and target channels must be different.");
      return;
    }

    if (subcommand === "forum") {
      const includeBots = includeBotsOption ?? true;
      let lastProgressAt = 0;
      const sourceThreads = await collectForumThreads(source, async (collected, archivedScanned) => {
        if (Date.now() - lastProgressAt < 2500) {
          return;
        }

        lastProgressAt = Date.now();
        await interaction.editReply(
          `Scanning forum ${source}...\nForum posts found: ${collected}\nArchived pages scanned: ${archivedScanned}`
        );
      });

      if (!sourceThreads.length) {
        await interaction.editReply(`No forum posts were found in ${source}.`);
        return;
      }

      let copied = 0;
      const failures = [];

      for (const thread of sourceThreads) {
        try {
          await copyForumThread(source, target, thread, includeBots);
          copied += 1;
        } catch (error) {
          failures.push(`\`${thread.id}\` (${thread.name}): ${error.message}`);
        }

        if (copied % 5 === 0) {
          await interaction.editReply(
            `Copying forum ${source} to ${target}...\nCopied posts: ${copied}/${sourceThreads.length}\nFailed: ${failures.length}`
          );
        }
      }

      await interaction.editReply(
        buildSummary({
          copied,
          failures,
          source,
          target,
          scanned: sourceThreads.length,
          requestedAll: true,
          unitLabel: "forum post thread(s)"
        })
      );
      return;
    }

    const requestedAll = subcommand === "all";
    const limit = requestedAll ? null : interaction.options.getInteger("limit", true);
    const before = interaction.options.getString("before");
    const includeBots = requestedAll ? includeBotsOption ?? true : includeBotsOption || false;

    let lastProgressAt = 0;
    const messages = await collectMessages(source, {
      all: requestedAll,
      limit,
      before,
      includeBots,
      onProgress: async (scanned, collected) => {
        if (Date.now() - lastProgressAt < 2500) {
          return;
        }

        lastProgressAt = Date.now();
        await interaction.editReply(
          `Scanning ${source}...\nScanned: ${scanned}\nQueued for transfer: ${collected}`
        );
      }
    });

    if (!messages.length) {
      await interaction.editReply(
        "No transferable messages were found. Make sure the bot can read the source channel and its history."
      );
      return;
    }

    let copied = 0;
    const failures = [];

    for (const message of messages) {
      try {
        await repostMessage(target, message);
        copied += 1;
      } catch (error) {
        failures.push(`\`${message.id}\`: ${error.message}`);
      }

      if (copied % PROGRESS_UPDATE_INTERVAL === 0) {
        await interaction.editReply(
          `Transferring from ${source} to ${target}...\nCopied: ${copied}/${messages.length}\nFailed: ${failures.length}`
        );
      }
    }

    await interaction.editReply(
      buildSummary({
        copied,
        failures,
        source,
        target,
        scanned: messages.length,
        requestedAll
      })
    );
  }
};
