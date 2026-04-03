const { SlashCommandBuilder, ChannelType } = require("discord.js");
const { getGuildData, saveStore } = require("../utils/store");
const { ensureMemberPermissions, PermissionFlagsBits } = require("../utils/discord");

module.exports = {
  data: new SlashCommandBuilder()
    .setName("logchannel")
    .setDescription("Configure the bot's audit log channel.")
    .addSubcommand((subcommand) =>
      subcommand
        .setName("set")
        .setDescription("Set the log channel.")
        .addChannelOption((option) =>
          option
            .setName("channel")
            .setDescription("Channel used for audit logs.")
            .addChannelTypes(ChannelType.GuildText, ChannelType.GuildAnnouncement)
            .setRequired(true)
        ))
    .addSubcommand((subcommand) =>
      subcommand.setName("clear").setDescription("Clear the log channel."))
    .addSubcommand((subcommand) =>
      subcommand.setName("show").setDescription("Show the current log channel.")),
  async execute(interaction) {
    const subcommand = interaction.options.getSubcommand();
    const guildData = getGuildData(interaction.guildId);

    if (subcommand === "show") {
      await interaction.reply({
        content: guildData.logChannelId
          ? `Current log channel: <#${guildData.logChannelId}>`
          : "No log channel configured.",
        ephemeral: true
      });
      return;
    }

    const hasPermission = await ensureMemberPermissions(
      interaction,
      PermissionFlagsBits.ManageGuild,
      "`/logchannel`"
    );
    if (!hasPermission) {
      return;
    }

    if (subcommand === "set") {
      guildData.logChannelId = interaction.options.getChannel("channel", true).id;
      saveStore();
      await interaction.reply({
        content: `Log channel set to <#${guildData.logChannelId}>.`,
        ephemeral: true
      });
      return;
    }

    guildData.logChannelId = null;
    saveStore();
    await interaction.reply({
      content: "Log channel cleared.",
      ephemeral: true
    });
  }
};
