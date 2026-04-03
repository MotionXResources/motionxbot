const { SlashCommandBuilder, ChannelType } = require("discord.js");
const { randomUUID } = require("node:crypto");
const { getGuildData, saveStore } = require("../utils/store");
const { parseDuration, formatDuration, renderDiscordTimestamp } = require("../utils/time");
const { ensureMemberPermissions, PermissionFlagsBits } = require("../utils/discord");

module.exports = {
  data: new SlashCommandBuilder()
    .setName("job")
    .setDescription("Manage recurring scheduled jobs.")
    .addSubcommand((subcommand) =>
      subcommand
        .setName("create")
        .setDescription("Create a recurring scheduled message.")
        .addStringOption((option) =>
          option.setName("name").setDescription("Job name.").setRequired(true)
        )
        .addChannelOption((option) =>
          option
            .setName("channel")
            .setDescription("Target text channel.")
            .addChannelTypes(ChannelType.GuildText, ChannelType.GuildAnnouncement)
            .setRequired(true)
        )
        .addStringOption((option) =>
          option.setName("message").setDescription("Message to send.").setRequired(true)
        )
        .addStringOption((option) =>
          option.setName("every").setDescription("Interval like 30m or 6h.").setRequired(true)
        )
        .addStringOption((option) =>
          option.setName("start-in").setDescription("Optional start delay like 10m.")
        ))
    .addSubcommand((subcommand) =>
      subcommand.setName("list").setDescription("List all scheduled jobs."))
    .addSubcommand((subcommand) =>
      subcommand
        .setName("pause")
        .setDescription("Pause a job.")
        .addStringOption((option) =>
          option.setName("id").setDescription("Job id from /job list.").setRequired(true)
        ))
    .addSubcommand((subcommand) =>
      subcommand
        .setName("resume")
        .setDescription("Resume a paused job.")
        .addStringOption((option) =>
          option.setName("id").setDescription("Job id from /job list.").setRequired(true)
        ))
    .addSubcommand((subcommand) =>
      subcommand
        .setName("remove")
        .setDescription("Remove a job.")
        .addStringOption((option) =>
          option.setName("id").setDescription("Job id from /job list.").setRequired(true)
        ))
    .addSubcommand((subcommand) =>
      subcommand
        .setName("run-now")
        .setDescription("Force a job to run on the next scheduler tick.")
        .addStringOption((option) =>
          option.setName("id").setDescription("Job id from /job list.").setRequired(true)
        )),
  async execute(interaction) {
    const hasPermission = await ensureMemberPermissions(
      interaction,
      PermissionFlagsBits.ManageGuild,
      "`/job`"
    );
    if (!hasPermission) {
      return;
    }

    const subcommand = interaction.options.getSubcommand();
    const guildData = getGuildData(interaction.guildId);

    if (subcommand === "create") {
      const intervalMs = parseDuration(interaction.options.getString("every", true));
      const startDelay = parseDuration(interaction.options.getString("start-in") || "0");

      if (!intervalMs || intervalMs < 60 * 1000) {
        await interaction.reply({
          content: "Use an interval of at least `1m` for recurring jobs.",
          ephemeral: true
        });
        return;
      }

      if (startDelay === null) {
        await interaction.reply({
          content: "I couldn't parse `start-in`. Try `10m` or leave it blank.",
          ephemeral: true
        });
        return;
      }

      const job = {
        id: randomUUID().slice(0, 8),
        name: interaction.options.getString("name", true),
        channelId: interaction.options.getChannel("channel", true).id,
        message: interaction.options.getString("message", true),
        intervalMs,
        nextRunAt: Date.now() + startDelay,
        lastRunAt: null,
        enabled: true,
        createdBy: interaction.user.id
      };

      guildData.jobs.push(job);
      saveStore();

      await interaction.reply({
        content: `Job \`${job.id}\` created. Next run: ${renderDiscordTimestamp(job.nextRunAt)}. Repeats every ${formatDuration(intervalMs)}.`,
        ephemeral: true
      });
      return;
    }

    if (subcommand === "list") {
      if (!guildData.jobs.length) {
        await interaction.reply({
          content: "No scheduled jobs yet.",
          ephemeral: true
        });
        return;
      }

      const lines = guildData.jobs
        .sort((left, right) => left.nextRunAt - right.nextRunAt)
        .map((job) =>
          `\`${job.id}\` • ${job.enabled ? "active" : "paused"} • ${renderDiscordTimestamp(job.nextRunAt)} • every ${formatDuration(job.intervalMs)} • ${job.name}`
        );

      await interaction.reply({
        content: lines.join("\n"),
        ephemeral: true
      });
      return;
    }

    const id = interaction.options.getString("id", true);
    const job = guildData.jobs.find((item) => item.id === id);

    if (!job) {
      await interaction.reply({
        content: `No job found for id \`${id}\`.`,
        ephemeral: true
      });
      return;
    }

    if (subcommand === "pause") {
      job.enabled = false;
      saveStore();
      await interaction.reply({
        content: `Job \`${id}\` paused.`,
        ephemeral: true
      });
      return;
    }

    if (subcommand === "resume") {
      job.enabled = true;
      job.nextRunAt = Date.now() + job.intervalMs;
      saveStore();
      await interaction.reply({
        content: `Job \`${id}\` resumed. Next run: ${renderDiscordTimestamp(job.nextRunAt)}.`,
        ephemeral: true
      });
      return;
    }

    if (subcommand === "remove") {
      guildData.jobs = guildData.jobs.filter((item) => item.id !== id);
      saveStore();
      await interaction.reply({
        content: `Job \`${id}\` removed.`,
        ephemeral: true
      });
      return;
    }

    if (subcommand === "run-now") {
      job.nextRunAt = Date.now();
      job.enabled = true;
      saveStore();
      await interaction.reply({
        content: `Job \`${id}\` queued to run on the next scheduler tick.`,
        ephemeral: true
      });
    }
  }
};
