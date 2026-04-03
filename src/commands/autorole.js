const { SlashCommandBuilder } = require("discord.js");
const { getGuildData, saveStore } = require("../utils/store");
const { ensureMemberPermissions, PermissionFlagsBits } = require("../utils/discord");

module.exports = {
  data: new SlashCommandBuilder()
    .setName("autorole")
    .setDescription("Manage roles automatically assigned to new members.")
    .addSubcommand((subcommand) =>
      subcommand
        .setName("add")
        .setDescription("Add a role to the autorole list.")
        .addRoleOption((option) =>
          option.setName("role").setDescription("Role to auto-assign.").setRequired(true)
        ))
    .addSubcommand((subcommand) =>
      subcommand
        .setName("remove")
        .setDescription("Remove a role from the autorole list.")
        .addRoleOption((option) =>
          option.setName("role").setDescription("Role to remove.").setRequired(true)
        ))
    .addSubcommand((subcommand) =>
      subcommand.setName("list").setDescription("List current autoroles.")),
  async execute(interaction) {
    const guildData = getGuildData(interaction.guildId);
    const subcommand = interaction.options.getSubcommand();

    if (subcommand === "list") {
      const roles = guildData.autoRoles
        .map((roleId) => interaction.guild.roles.cache.get(roleId))
        .filter(Boolean)
        .map((role) => role.toString());

      await interaction.reply({
        content: roles.length ? roles.join("\n") : "No autoroles configured.",
        ephemeral: true
      });
      return;
    }

    const hasPermission = await ensureMemberPermissions(
      interaction,
      PermissionFlagsBits.ManageRoles,
      "`/autorole`"
    );
    if (!hasPermission) {
      return;
    }

    const role = interaction.options.getRole("role", true);

    if (subcommand === "add") {
      if (!guildData.autoRoles.includes(role.id)) {
        guildData.autoRoles.push(role.id);
        saveStore();
      }

      await interaction.reply({
        content: `${role} added to autoroles.`,
        ephemeral: true
      });
      return;
    }

    guildData.autoRoles = guildData.autoRoles.filter((roleId) => roleId !== role.id);
    saveStore();
    await interaction.reply({
      content: `${role} removed from autoroles.`,
      ephemeral: true
    });
  }
};
