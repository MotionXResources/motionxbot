const { AttachmentBuilder, ChannelType, SlashCommandBuilder } = require("discord.js");
const { ensureMemberPermissions, PermissionFlagsBits } = require("../utils/discord");

const MAX_ATTACHMENTS_PER_MESSAGE = 10;
const MAX_CONTENT_LENGTH = 2000;
const MAX_TOTAL_ATTACHMENT_BYTES = 24 * 1024 * 1024;
const HARD_LIMIT = 100;

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

function buildHeader(message) {
  return [
    `**Copied from** ${message.channel} • <t:${Math.floor(message.createdTimestamp / 1000)}:f>`,
    `**Original author:** ${message.author.tag} (${message.author.id})`
  ].join("\n");
}

async function downloadAttachments(message) {
  const builders = [];
  const skipped = [];
  let totalBytes = 0;

  for (const attachment of message.attachments.values()) {
    if (builders.length >= MAX_ATTACHMENTS_PER_MESSAGE) {
      skipped.push(`${attachment.name || attachment.id} (too many attachments on one Discord message)`);
      continue;
    }

    if (totalBytes + attachment.size > MAX_TOTAL_ATTACHMENT_BYTES) {
      skipped.push(`${attachment.name || attachment.id} (too large to safely bundle in one repost)`);
      continue;
    }

    const response = await fetch(attachment.url);
    if (!response.ok) {
      skipped.push(`${attachment.name || attachment.id} (download failed with ${response.status})`);
      continue;
    }

    const arrayBuffer = await response.arrayBuffer();
    totalBytes += arrayBuffer.byteLength;
    builders.push(
      new AttachmentBuilder(Buffer.from(arrayBuffer), {
        name: attachment.name || `attachment-${attachment.id}`
      })
    );
  }

  return {
    files: builders,
    skipped
  };
}

async function repostMessage(targetChannel, message) {
  const header = buildHeader(message);
  const text = message.content || "";
  const firstChunkBudget = Math.max(1, MAX_CONTENT_LENGTH - header.length - 2);
  const contentChunks = splitContent(text, firstChunkBudget);
  const { files, skipped } = await downloadAttachments(message);

  if (!contentChunks.length && !files.length) {
    await targetChannel.send({
      content: `${header}\n*No plain-text content. Unsupported embeds/stickers are not copied.*`,
      allowedMentions: { parse: [] }
    });
  } else {
    const firstBody = contentChunks.shift();
    await targetChannel.send({
      content: firstBody ? `${header}\n\n${firstBody}` : header,
      files,
      allowedMentions: { parse: [] }
    });

    for (const chunk of contentChunks) {
      await targetChannel.send({
        content: chunk,
        allowedMentions: { parse: [] }
      });
    }
  }

  if (skipped.length) {
    const note = `Skipped attachment(s): ${skipped.join(", ")}`;
    for (const chunk of splitContent(note, MAX_CONTENT_LENGTH)) {
      await targetChannel.send({
        content: chunk,
        allowedMentions: { parse: [] }
      });
    }
  }
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
    ),
  async execute(interaction) {
    const hasPermission = await ensureMemberPermissions(
      interaction,
      PermissionFlagsBits.ManageMessages,
      "`/transfer messages`"
    );
    if (!hasPermission) {
      return;
    }

    await interaction.deferReply({ ephemeral: true });

    const source = interaction.options.getChannel("source", true);
    const target = interaction.options.getChannel("target", true);
    const limit = interaction.options.getInteger("limit", true);
    const before = interaction.options.getString("before");
    const includeBots = interaction.options.getBoolean("include-bots") || false;

    if (source.id === target.id) {
      await interaction.editReply("Source and target channels must be different.");
      return;
    }

    const fetched = await source.messages.fetch({
      limit,
      ...(before ? { before } : {})
    });

    const messages = [...fetched.values()]
      .filter((message) => !message.system)
      .filter((message) => includeBots || !message.author.bot)
      .filter((message) => message.content || message.attachments.size > 0)
      .sort((left, right) => left.createdTimestamp - right.createdTimestamp);

    if (!messages.length) {
      await interaction.editReply(
        "No copyable messages were found. Make sure the bot can read the source channel and that the channel actually has plain-text content or attachments."
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
    }

    const summary = [
      `Copied ${copied} message(s) from ${source} to ${target}.`,
      failures.length ? `Failed: ${failures.length}` : "Failed: 0"
    ];

    if (failures.length) {
      summary.push(...failures.slice(0, 10));
    }

    await interaction.editReply(summary.join("\n"));
  }
};
