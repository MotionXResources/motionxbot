const { SlashCommandBuilder } = require("discord.js");
const { getGuildData } = require("../utils/store");

module.exports = {
  data: new SlashCommandBuilder()
    .setName("botstatus")
    .setDescription("Show a quick automation snapshot for this server."),
  async execute(interaction) {
    const guildData = getGuildData(interaction.guildId);
    const lines = [
      `Reminders queued: ${guildData.reminders.length}`,
      `Jobs configured: ${guildData.jobs.length}`,
      `Tags saved: ${Object.keys(guildData.tags).length}`,
      `Templates saved: ${Object.keys(guildData.templates).length}`,
      `Checklists saved: ${Object.keys(guildData.checklists).length}`,
      `Todos tracked: ${guildData.todos.length}`,
      `Approval requests: ${Object.keys(guildData.approvals).length}`,
      `Autoroles: ${guildData.autoRoles.length}`,
      `Heartbeat: ${guildData.heartbeat ? "configured" : "off"}`,
      `Log channel: ${guildData.logChannelId ? `<#${guildData.logChannelId}>` : "not set"}`
    ];

    await interaction.reply({
      content: lines.join("\n"),
      ephemeral: true
    });
  }
};
