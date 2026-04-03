const { SlashCommandBuilder, ChannelType } = require("discord.js");
const { getGuildData, saveStore } = require("../utils/store");
const { ensureMemberPermissions, PermissionFlagsBits } = require("../utils/discord");

module.exports = {
  data: new SlashCommandBuilder()
    .setName("tag")
    .setDescription("Manage reusable text snippets.")
    .addSubcommand((subcommand) =>
      subcommand
        .setName("create")
        .setDescription("Create a tag.")
        .addStringOption((option) =>
          option.setName("name").setDescription("Short tag name.").setRequired(true)
        )
        .addStringOption((option) =>
          option.setName("content").setDescription("Tag content.").setRequired(true)
        ))
    .addSubcommand((subcommand) =>
      subcommand
        .setName("update")
        .setDescription("Update a tag.")
        .addStringOption((option) =>
          option.setName("name").setDescription("Existing tag name.").setRequired(true)
        )
        .addStringOption((option) =>
          option.setName("content").setDescription("New content.").setRequired(true)
        ))
    .addSubcommand((subcommand) =>
      subcommand
        .setName("post")
        .setDescription("Post a tag into a channel.")
        .addStringOption((option) =>
          option.setName("name").setDescription("Tag name.").setRequired(true)
        )
        .addChannelOption((option) =>
          option
            .setName("channel")
            .setDescription("Target channel.")
            .addChannelTypes(ChannelType.GuildText, ChannelType.GuildAnnouncement)
        ))
    .addSubcommand((subcommand) =>
      subcommand.setName("list").setDescription("List all tags."))
    .addSubcommand((subcommand) =>
      subcommand
        .setName("delete")
        .setDescription("Delete a tag.")
        .addStringOption((option) =>
          option.setName("name").setDescription("Tag name.").setRequired(true)
        )),
  async execute(interaction) {
    const subcommand = interaction.options.getSubcommand();
    const guildData = getGuildData(interaction.guildId);

    if (subcommand === "list") {
      const names = Object.keys(guildData.tags).sort();
      await interaction.reply({
        content: names.length ? names.map((name) => `• ${name}`).join("\n") : "No tags saved yet.",
        ephemeral: true
      });
      return;
    }

    const hasPermission = await ensureMemberPermissions(
      interaction,
      PermissionFlagsBits.ManageGuild,
      "`/tag`"
    );
    if (!hasPermission) {
      return;
    }

    const name = interaction.options.getString("name", true).toLowerCase().trim();

    if (subcommand === "create" || subcommand === "update") {
      guildData.tags[name] = interaction.options.getString("content", true);
      saveStore();
      await interaction.reply({
        content: `Tag \`${name}\` ${subcommand === "create" ? "created" : "updated"}.`,
        ephemeral: true
      });
      return;
    }

    if (subcommand === "delete") {
      if (!guildData.tags[name]) {
        await interaction.reply({
          content: `Tag \`${name}\` does not exist.`,
          ephemeral: true
        });
        return;
      }

      delete guildData.tags[name];
      saveStore();
      await interaction.reply({
        content: `Tag \`${name}\` deleted.`,
        ephemeral: true
      });
      return;
    }

    if (subcommand === "post") {
      const targetChannel = interaction.options.getChannel("channel") || interaction.channel;
      const content = guildData.tags[name];

      if (!content) {
        await interaction.reply({
          content: `Tag \`${name}\` does not exist.`,
          ephemeral: true
        });
        return;
      }

      await targetChannel.send({ content });
      await interaction.reply({
        content: `Posted tag \`${name}\` in ${targetChannel}.`,
        ephemeral: true
      });
    }
  }
};
