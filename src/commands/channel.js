const { SlashCommandBuilder } = require("discord.js");
const { ensureMemberPermissions, PermissionFlagsBits } = require("../utils/discord");

module.exports = {
  data: new SlashCommandBuilder()
    .setName("channel")
    .setDescription("Manage the current channel quickly.")
    .addSubcommand((subcommand) =>
      subcommand.setName("lock").setDescription("Prevent @everyone from sending messages here."))
    .addSubcommand((subcommand) =>
      subcommand.setName("unlock").setDescription("Allow @everyone to send messages here again."))
    .addSubcommand((subcommand) =>
      subcommand
        .setName("slowmode")
        .setDescription("Set slowmode in seconds.")
        .addIntegerOption((option) =>
          option.setName("seconds").setDescription("Slowmode seconds.").setRequired(true)
        ))
    .addSubcommand((subcommand) =>
      subcommand
        .setName("archive")
        .setDescription("Rename and lock the current channel as archived.")
        .addStringOption((option) =>
          option.setName("prefix").setDescription("Archive prefix. Default: archived")
        )),
  async execute(interaction) {
    const hasPermission = await ensureMemberPermissions(
      interaction,
      PermissionFlagsBits.ManageChannels,
      "`/channel`"
    );
    if (!hasPermission) {
      return;
    }

    const channel = interaction.channel;
    const everyoneRole = interaction.guild.roles.everyone;
    const subcommand = interaction.options.getSubcommand();

    if (subcommand === "lock") {
      await channel.permissionOverwrites.edit(everyoneRole, { SendMessages: false });
      await interaction.reply({
        content: `${channel} locked.`,
        ephemeral: true
      });
      return;
    }

    if (subcommand === "unlock") {
      await channel.permissionOverwrites.edit(everyoneRole, { SendMessages: true });
      await interaction.reply({
        content: `${channel} unlocked.`,
        ephemeral: true
      });
      return;
    }

    if (subcommand === "slowmode") {
      const seconds = interaction.options.getInteger("seconds", true);
      await channel.setRateLimitPerUser(seconds);
      await interaction.reply({
        content: `Slowmode set to ${seconds} second(s) in ${channel}.`,
        ephemeral: true
      });
      return;
    }

    if (subcommand === "archive") {
      const prefix = interaction.options.getString("prefix") || "archived";
      await channel.setName(`${prefix}-${channel.name}`.slice(0, 100));
      await channel.permissionOverwrites.edit(everyoneRole, { SendMessages: false });
      await interaction.reply({
        content: `${channel} archived.`,
        ephemeral: true
      });
    }
  }
};
