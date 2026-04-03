const { SlashCommandBuilder } = require("discord.js");
const { randomUUID } = require("node:crypto");
const { getGuildData, saveStore } = require("../utils/store");
const { parseDuration, formatDuration, renderDiscordTimestamp } = require("../utils/time");

module.exports = {
  data: new SlashCommandBuilder()
    .setName("reminder")
    .setDescription("Manage one-time reminders.")
    .addSubcommand((subcommand) =>
      subcommand
        .setName("create")
        .setDescription("Create a reminder.")
        .addStringOption((option) =>
          option.setName("message").setDescription("Reminder message.").setRequired(true)
        )
        .addStringOption((option) =>
          option.setName("in").setDescription("Delay like 10m, 2h, 1d.").setRequired(true)
        )
        .addBooleanOption((option) =>
          option.setName("dm").setDescription("Deliver in DM instead of channel.")
        ))
    .addSubcommand((subcommand) =>
      subcommand.setName("list").setDescription("List active reminders."))
    .addSubcommand((subcommand) =>
      subcommand
        .setName("cancel")
        .setDescription("Cancel a reminder.")
        .addStringOption((option) =>
          option.setName("id").setDescription("Reminder id from /reminder list.").setRequired(true)
        ))
    .addSubcommand((subcommand) =>
      subcommand
        .setName("snooze")
        .setDescription("Delay an existing reminder.")
        .addStringOption((option) =>
          option.setName("id").setDescription("Reminder id from /reminder list.").setRequired(true)
        )
        .addStringOption((option) =>
          option.setName("for").setDescription("Extra time like 15m or 1h.").setRequired(true)
        )),
  async execute(interaction) {
    const subcommand = interaction.options.getSubcommand();
    const guildData = getGuildData(interaction.guildId);

    if (subcommand === "create") {
      const message = interaction.options.getString("message", true);
      const delayRaw = interaction.options.getString("in", true);
      const dm = interaction.options.getBoolean("dm") || false;
      const delayMs = parseDuration(delayRaw);

      if (!delayMs) {
        await interaction.reply({
          content: "I couldn't parse that delay. Use values like `15m`, `2h`, or `1d`.",
          ephemeral: true
        });
        return;
      }

      const dueAt = Date.now() + delayMs;
      const reminder = {
        id: randomUUID().slice(0, 8),
        userId: interaction.user.id,
        channelId: interaction.channelId,
        message,
        delivery: dm ? "dm" : "channel",
        dueAt,
        createdAt: Date.now()
      };

      guildData.reminders.push(reminder);
      saveStore();

      await interaction.reply({
        content: `Reminder \`${reminder.id}\` queued for ${renderDiscordTimestamp(dueAt)} (${formatDuration(delayMs)}).`,
        ephemeral: true
      });
      return;
    }

    if (subcommand === "list") {
      const reminders = guildData.reminders
        .filter((item) => item.userId === interaction.user.id)
        .sort((left, right) => left.dueAt - right.dueAt);

      if (!reminders.length) {
        await interaction.reply({
          content: "You do not have any active reminders.",
          ephemeral: true
        });
        return;
      }

      const lines = reminders.map((reminder) =>
        `\`${reminder.id}\` • ${renderDiscordTimestamp(reminder.dueAt)} • ${reminder.delivery} • ${reminder.message}`
      );

      await interaction.reply({
        content: lines.join("\n"),
        ephemeral: true
      });
      return;
    }

    if (subcommand === "cancel") {
      const id = interaction.options.getString("id", true);
      const before = guildData.reminders.length;
      guildData.reminders = guildData.reminders.filter(
        (reminder) => !(reminder.id === id && reminder.userId === interaction.user.id)
      );

      if (guildData.reminders.length === before) {
        await interaction.reply({
          content: `No reminder found for id \`${id}\`.`,
          ephemeral: true
        });
        return;
      }

      saveStore();
      await interaction.reply({
        content: `Reminder \`${id}\` cancelled.`,
        ephemeral: true
      });
      return;
    }

    if (subcommand === "snooze") {
      const id = interaction.options.getString("id", true);
      const delayMs = parseDuration(interaction.options.getString("for", true));
      const reminder = guildData.reminders.find(
        (item) => item.id === id && item.userId === interaction.user.id
      );

      if (!delayMs) {
        await interaction.reply({
          content: "Use a valid snooze duration like `20m` or `1h`.",
          ephemeral: true
        });
        return;
      }

      if (!reminder) {
        await interaction.reply({
          content: `No reminder found for id \`${id}\`.`,
          ephemeral: true
        });
        return;
      }

      reminder.dueAt += delayMs;
      saveStore();

      await interaction.reply({
        content: `Reminder \`${id}\` snoozed until ${renderDiscordTimestamp(reminder.dueAt)}.`,
        ephemeral: true
      });
    }
  }
};
