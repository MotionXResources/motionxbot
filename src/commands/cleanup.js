const { SlashCommandBuilder } = require("discord.js");
const { ensureMemberPermissions, PermissionFlagsBits } = require("../utils/discord");

async function purgeMessages(channel, predicate, limit) {
  const fetched = await channel.messages.fetch({ limit: Math.min(limit, 100) });
  const cutoff = Date.now() - 14 * 24 * 60 * 60 * 1000;
  const deletable = fetched.filter((message) => predicate(message) && message.createdTimestamp > cutoff);
  if (!deletable.size) {
    return 0;
  }

  await channel.bulkDelete(deletable, true);
  return deletable.size;
}

module.exports = {
  data: new SlashCommandBuilder()
    .setName("cleanup")
    .setDescription("Clean up recent messages.")
    .addSubcommand((subcommand) =>
      subcommand
        .setName("bot")
        .setDescription("Delete recent bot messages in this channel.")
        .addIntegerOption((option) =>
          option
            .setName("limit")
            .setDescription("How many recent messages to scan, max 100.")
            .setRequired(true)
        ))
    .addSubcommand((subcommand) =>
      subcommand
        .setName("user")
        .setDescription("Delete recent messages from one user in this channel.")
        .addUserOption((option) =>
          option.setName("member").setDescription("User whose messages to delete.").setRequired(true)
        )
        .addIntegerOption((option) =>
          option
            .setName("limit")
            .setDescription("How many recent messages to scan, max 100.")
            .setRequired(true)
        )),
  async execute(interaction) {
    const hasPermission = await ensureMemberPermissions(
      interaction,
      PermissionFlagsBits.ManageMessages,
      "`/cleanup`"
    );
    if (!hasPermission) {
      return;
    }

    await interaction.deferReply({ ephemeral: true });

    const subcommand = interaction.options.getSubcommand();
    const limit = interaction.options.getInteger("limit", true);

    let removed = 0;
    if (subcommand === "bot") {
      removed = await purgeMessages(interaction.channel, (message) => message.author.bot, limit);
    }

    if (subcommand === "user") {
      const member = interaction.options.getUser("member", true);
      removed = await purgeMessages(
        interaction.channel,
        (message) => message.author.id === member.id,
        limit
      );
    }

    await interaction.editReply(`Deleted ${removed} message(s). Discord only bulk-deletes messages newer than 14 days.`);
  }
};
