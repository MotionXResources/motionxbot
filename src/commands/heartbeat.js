const { SlashCommandBuilder, ChannelType } = require("discord.js");
const { getGuildData, saveStore } = require("../utils/store");
const { parseDuration, formatDuration, renderDiscordTimestamp } = require("../utils/time");
const { ensureMemberPermissions, PermissionFlagsBits } = require("../utils/discord");

module.exports = {
  data: new SlashCommandBuilder()
    .setName("heartbeat")
    .setDescription("Configure recurring heartbeat messages.")
    .addSubcommand((subcommand) =>
      subcommand
        .setName("set")
        .setDescription("Enable recurring heartbeat posts.")
        .addChannelOption((option) =>
          option
            .setName("channel")
            .setDescription("Target channel.")
            .addChannelTypes(ChannelType.GuildText, ChannelType.GuildAnnouncement)
            .setRequired(true)
        )
        .addStringOption((option) =>
          option.setName("every").setDescription("Interval like 1h or 12h.").setRequired(true)
        )
        .addStringOption((option) =>
          option
            .setName("message")
            .setDescription("Heartbeat message. Built-ins: {server}, {date}, {time}.")
        ))
    .addSubcommand((subcommand) =>
      subcommand.setName("status").setDescription("Show heartbeat status."))
    .addSubcommand((subcommand) =>
      subcommand.setName("clear").setDescription("Disable heartbeat messages.")),
  async execute(interaction) {
    const subcommand = interaction.options.getSubcommand();
    const guildData = getGuildData(interaction.guildId);

    if (subcommand === "status") {
      if (!guildData.heartbeat) {
        await interaction.reply({
          content: "Heartbeat is not configured.",
          ephemeral: true
        });
        return;
      }

      await interaction.reply({
        content: `Heartbeat: ${guildData.heartbeat.enabled ? "enabled" : "disabled"} • every ${formatDuration(guildData.heartbeat.intervalMs)} • next run ${renderDiscordTimestamp(guildData.heartbeat.nextRunAt)} • channel <#${guildData.heartbeat.channelId}>`,
        ephemeral: true
      });
      return;
    }

    const hasPermission = await ensureMemberPermissions(
      interaction,
      PermissionFlagsBits.ManageGuild,
      "`/heartbeat`"
    );
    if (!hasPermission) {
      return;
    }

    if (subcommand === "clear") {
      guildData.heartbeat = null;
      saveStore();
      await interaction.reply({
        content: "Heartbeat disabled.",
        ephemeral: true
      });
      return;
    }

    const intervalMs = parseDuration(interaction.options.getString("every", true));
    if (!intervalMs || intervalMs < 60 * 1000) {
      await interaction.reply({
        content: "Use a heartbeat interval of at least `1m`.",
        ephemeral: true
      });
      return;
    }

    guildData.heartbeat = {
      channelId: interaction.options.getChannel("channel", true).id,
      intervalMs,
      nextRunAt: Date.now() + intervalMs,
      lastRunAt: null,
      enabled: true,
      message:
        interaction.options.getString("message") ||
        "Heartbeat check for {server} at {date} {time}."
    };
    saveStore();

    await interaction.reply({
      content: `Heartbeat enabled. First post at ${renderDiscordTimestamp(guildData.heartbeat.nextRunAt)}.`,
      ephemeral: true
    });
  }
};
