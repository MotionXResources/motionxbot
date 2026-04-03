const { SlashCommandBuilder, ChannelType } = require("discord.js");
const { getGuildData, saveStore } = require("../utils/store");
const { buildBasePlaceholders, parseAssignments, renderTemplateText } = require("../utils/render");
const { ensureMemberPermissions, PermissionFlagsBits } = require("../utils/discord");

module.exports = {
  data: new SlashCommandBuilder()
    .setName("template")
    .setDescription("Manage reusable templated messages.")
    .addSubcommand((subcommand) =>
      subcommand
        .setName("create")
        .setDescription("Create a template.")
        .addStringOption((option) =>
          option.setName("name").setDescription("Template name.").setRequired(true)
        )
        .addStringOption((option) =>
          option
            .setName("content")
            .setDescription("Template body with placeholders like {user} or {date}.")
            .setRequired(true)
        ))
    .addSubcommand((subcommand) =>
      subcommand
        .setName("update")
        .setDescription("Update a template.")
        .addStringOption((option) =>
          option.setName("name").setDescription("Template name.").setRequired(true)
        )
        .addStringOption((option) =>
          option.setName("content").setDescription("New template body.").setRequired(true)
        ))
    .addSubcommand((subcommand) =>
      subcommand.setName("list").setDescription("List templates."))
    .addSubcommand((subcommand) =>
      subcommand
        .setName("delete")
        .setDescription("Delete a template.")
        .addStringOption((option) =>
          option.setName("name").setDescription("Template name.").setRequired(true)
        ))
    .addSubcommand((subcommand) =>
      subcommand
        .setName("send")
        .setDescription("Render and send a template.")
        .addStringOption((option) =>
          option.setName("name").setDescription("Template name.").setRequired(true)
        )
        .addChannelOption((option) =>
          option
            .setName("channel")
            .setDescription("Destination channel.")
            .addChannelTypes(ChannelType.GuildText, ChannelType.GuildAnnouncement)
        )
        .addStringOption((option) =>
          option
            .setName("values")
            .setDescription("Extra placeholders like owner=Alex,priority=high.")
        )),
  async execute(interaction) {
    const subcommand = interaction.options.getSubcommand();
    const guildData = getGuildData(interaction.guildId);

    if (subcommand === "list") {
      const names = Object.keys(guildData.templates).sort();
      await interaction.reply({
        content: names.length
          ? `${names.map((name) => `• ${name}`).join("\n")}\n\nBuilt-ins: {user}, {channel}, {server}, {date}, {time}, {iso_date}`
          : "No templates saved yet.",
        ephemeral: true
      });
      return;
    }

    const hasPermission = await ensureMemberPermissions(
      interaction,
      PermissionFlagsBits.ManageGuild,
      "`/template`"
    );
    if (!hasPermission) {
      return;
    }

    const name = interaction.options.getString("name", true).toLowerCase().trim();

    if (subcommand === "create" || subcommand === "update") {
      guildData.templates[name] = interaction.options.getString("content", true);
      saveStore();
      await interaction.reply({
        content: `Template \`${name}\` ${subcommand === "create" ? "created" : "updated"}.`,
        ephemeral: true
      });
      return;
    }

    if (subcommand === "delete") {
      if (!guildData.templates[name]) {
        await interaction.reply({
          content: `Template \`${name}\` does not exist.`,
          ephemeral: true
        });
        return;
      }

      delete guildData.templates[name];
      saveStore();
      await interaction.reply({
        content: `Template \`${name}\` deleted.`,
        ephemeral: true
      });
      return;
    }

    if (subcommand === "send") {
      const template = guildData.templates[name];
      if (!template) {
        await interaction.reply({
          content: `Template \`${name}\` does not exist.`,
          ephemeral: true
        });
        return;
      }

      const targetChannel = interaction.options.getChannel("channel") || interaction.channel;
      const placeholders = {
        ...buildBasePlaceholders({
          guild: interaction.guild,
          channel: targetChannel,
          user: interaction.user
        }),
        ...parseAssignments(interaction.options.getString("values"))
      };

      const content = renderTemplateText(template, placeholders);
      await targetChannel.send({ content });

      await interaction.reply({
        content: `Template \`${name}\` sent to ${targetChannel}.`,
        ephemeral: true
      });
    }
  }
};
