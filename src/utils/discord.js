const { ChannelType, PermissionFlagsBits } = require("discord.js");
const { getGuildData } = require("./store");

async function ensureMemberPermissions(interaction, permissions, label = "the requested action") {
  if (interaction.memberPermissions?.has(permissions)) {
    return true;
  }

  await interaction.reply({
    content: `You need the right Discord permissions to use ${label}.`,
    ephemeral: true
  });
  return false;
}

function ensureTextChannel(channel) {
  if (!channel) {
    return false;
  }

  if (
    channel.type === ChannelType.GuildText ||
    channel.type === ChannelType.PublicThread ||
    channel.type === ChannelType.PrivateThread ||
    channel.type === ChannelType.AnnouncementThread ||
    channel.type === ChannelType.GuildAnnouncement
  ) {
    return true;
  }

  return channel.isTextBased?.() && !channel.isDMBased?.();
}

async function resolveGuildTextChannel(guild, channelId) {
  if (!guild || !channelId) {
    return null;
  }

  const cached = guild.channels.cache.get(channelId);
  if (ensureTextChannel(cached)) {
    return cached;
  }

  try {
    const fetched = await guild.channels.fetch(channelId);
    return ensureTextChannel(fetched) ? fetched : null;
  } catch {
    return null;
  }
}

async function logToGuild(client, guildId, message) {
  const guild = client.guilds.cache.get(guildId) || (await client.guilds.fetch(guildId).catch(() => null));
  if (!guild) {
    return;
  }

  const guildData = getGuildData(guildId);
  if (!guildData.logChannelId) {
    return;
  }

  const channel = await resolveGuildTextChannel(guild, guildData.logChannelId);
  if (!channel) {
    return;
  }

  await channel.send({ content: message }).catch(() => null);
}

module.exports = {
  ensureMemberPermissions,
  ensureTextChannel,
  resolveGuildTextChannel,
  logToGuild,
  PermissionFlagsBits
};
