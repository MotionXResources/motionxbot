const { SlashCommandBuilder } = require("discord.js");

module.exports = {
  data: new SlashCommandBuilder()
    .setName("automation-help")
    .setDescription("See the bot's automation command categories."),
  async execute(interaction) {
    const lines = [
      "**Scheduling:** `/reminder`, `/job`, `/heartbeat`",
      "**Reusable content:** `/tag`, `/template`",
      "**Operational tracking:** `/checklist`, `/todo`, `/approval`",
      "**Server automation:** `/autorole`, `/bulkrole`, `/channel`, `/cleanup`, `/logchannel`, `/transfer`",
      "**Bot status:** `/botstatus`",
      "",
      "Most time fields accept compact durations like `15m`, `2h`, `1d`, or `1h30m`."
    ];

    await interaction.reply({
      content: lines.join("\n"),
      ephemeral: true
    });
  }
};
