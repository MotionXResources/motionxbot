const { SlashCommandBuilder } = require("discord.js");
const { randomUUID } = require("node:crypto");
const { getGuildData, saveStore } = require("../utils/store");
const { ensureMemberPermissions, PermissionFlagsBits, logToGuild } = require("../utils/discord");

module.exports = {
  data: new SlashCommandBuilder()
    .setName("approval")
    .setDescription("Track internal approval requests.")
    .addSubcommand((subcommand) =>
      subcommand
        .setName("create")
        .setDescription("Create a new approval request.")
        .addStringOption((option) =>
          option.setName("title").setDescription("Short request title.").setRequired(true)
        )
        .addStringOption((option) =>
          option.setName("details").setDescription("What needs approval?").setRequired(true)
        ))
    .addSubcommand((subcommand) =>
      subcommand.setName("list").setDescription("List approval requests."))
    .addSubcommand((subcommand) =>
      subcommand
        .setName("approve")
        .setDescription("Approve a request.")
        .addStringOption((option) =>
          option.setName("id").setDescription("Approval id.").setRequired(true)
        ))
    .addSubcommand((subcommand) =>
      subcommand
        .setName("reject")
        .setDescription("Reject a request.")
        .addStringOption((option) =>
          option.setName("id").setDescription("Approval id.").setRequired(true)
        )
        .addStringOption((option) =>
          option.setName("reason").setDescription("Reason for rejection.")
        )),
  async execute(interaction, client) {
    const subcommand = interaction.options.getSubcommand();
    const guildData = getGuildData(interaction.guildId);

    if (subcommand === "list") {
      const requests = Object.values(guildData.approvals).sort((left, right) => right.createdAt - left.createdAt);
      await interaction.reply({
        content: requests.length
          ? requests
              .map(
                (request) =>
                  `\`${request.id}\` • ${request.status} • <@${request.requestedBy}> • ${request.title}`
              )
              .join("\n")
          : "No approval requests yet.",
        ephemeral: true
      });
      return;
    }

    if (subcommand === "create") {
      const request = {
        id: randomUUID().slice(0, 8),
        title: interaction.options.getString("title", true),
        details: interaction.options.getString("details", true),
        requestedBy: interaction.user.id,
        createdAt: Date.now(),
        status: "pending",
        decidedBy: null,
        decidedAt: null,
        decisionNote: null
      };

      guildData.approvals[request.id] = request;
      saveStore();
      await logToGuild(
        client,
        interaction.guildId,
        `Approval request \`${request.id}\` created by <@${interaction.user.id}>: **${request.title}**`
      );

      await interaction.reply({
        content: `Approval request \`${request.id}\` created.`,
        ephemeral: true
      });
      return;
    }

    const hasPermission = await ensureMemberPermissions(
      interaction,
      PermissionFlagsBits.ManageGuild,
      "`/approval approve` or `/approval reject`"
    );
    if (!hasPermission) {
      return;
    }

    const id = interaction.options.getString("id", true);
    const request = guildData.approvals[id];
    if (!request) {
      await interaction.reply({
        content: `Approval request \`${id}\` does not exist.`,
        ephemeral: true
      });
      return;
    }

    request.status = subcommand === "approve" ? "approved" : "rejected";
    request.decidedBy = interaction.user.id;
    request.decidedAt = Date.now();
    request.decisionNote = interaction.options.getString("reason") || null;
    saveStore();

    await logToGuild(
      client,
      interaction.guildId,
      `Approval request \`${id}\` ${request.status} by <@${interaction.user.id}>.`
    );

    await interaction.reply({
      content: `Approval request \`${id}\` marked ${request.status}.`,
      ephemeral: true
    });
  }
};
