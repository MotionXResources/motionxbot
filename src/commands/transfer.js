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

function buildHeader(message) {
  return [
    `**Copied from** ${message.channel} - <t:${Math.floor(message.createdTimestamp / 1000)}:f>`,
    `**Original author:** ${message.author.tag} (${message.author.id})`
  ].join("\n");
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

async function repostMessage(targetChannel, message) {
  const header = buildHeader(message);
  const text = message.content || "";
  const firstChunkBudget = Math.max(1, MAX_CONTENT_LENGTH - header.length - 2);
  const contentChunks = splitContent(text, firstChunkBudget);
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

  if (skipped.length) {
    const note = `Skipped attachment(s): ${skipped.join(", ")}`;
    for (const chunk of splitTextByLimit(note)) {
      await targetChannel.send({
        content: chunk,
        allowedMentions: { parse: [] }
      });
    }
  }
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

function buildSummary({ copied, failures, source, target, scanned, requestedAll }) {
  const summary = [
    `${requestedAll ? "Transferred" : "Copied"} ${copied} message(s) from ${source} to ${target}.`,
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
    const requestedAll = subcommand === "all";
    const source = interaction.options.getChannel("source", true);
    const target = interaction.options.getChannel("target", true);
    const limit = requestedAll ? null : interaction.options.getInteger("limit", true);
    const before = interaction.options.getString("before");
    const includeBotsOption = interaction.options.getBoolean("include-bots");
    const includeBots = requestedAll ? includeBotsOption ?? true : includeBotsOption || false;

    if (source.id === target.id) {
      await interaction.editReply("Source and target channels must be different.");
      return;
    }

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
