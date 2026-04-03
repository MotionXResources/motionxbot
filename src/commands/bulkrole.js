const { SlashCommandBuilder } = require("discord.js");
const { ensureMemberPermissions, PermissionFlagsBits } = require("../utils/discord");

async function collectMembers(guild, filterMode, sourceRoleId) {
  await guild.members.fetch();
  return guild.members.cache.filter((member) => {
    if (sourceRoleId && !member.roles.cache.has(sourceRoleId)) {
      return false;
    }

    if (filterMode === "humans") {
      return !member.user.bot;
    }

    if (filterMode === "bots") {
      return member.user.bot;
    }

    return true;
  });
}

module.exports = {
  data: new SlashCommandBuilder()
    .setName("bulkrole")
    .setDescription("Apply or remove a role across many members.")
    .addSubcommand((subcommand) =>
      subcommand
        .setName("add")
        .setDescription("Add a target role to many members.")
        .addRoleOption((option) =>
          option.setName("target-role").setDescription("Role to add.").setRequired(true)
        )
        .addStringOption((option) =>
          option
            .setName("filter")
            .setDescription("Who should be targeted.")
            .setRequired(true)
            .addChoices(
              { name: "all", value: "all" },
              { name: "humans", value: "humans" },
              { name: "bots", value: "bots" }
            )
        )
        .addRoleOption((option) =>
          option.setName("source-role").setDescription("Optional source role filter.")
        ))
    .addSubcommand((subcommand) =>
      subcommand
        .setName("remove")
        .setDescription("Remove a target role from many members.")
        .addRoleOption((option) =>
          option.setName("target-role").setDescription("Role to remove.").setRequired(true)
        )
        .addStringOption((option) =>
          option
            .setName("filter")
            .setDescription("Who should be targeted.")
            .setRequired(true)
            .addChoices(
              { name: "all", value: "all" },
              { name: "humans", value: "humans" },
              { name: "bots", value: "bots" }
            )
        )
        .addRoleOption((option) =>
          option.setName("source-role").setDescription("Optional source role filter.")
        )),
  async execute(interaction) {
    const hasPermission = await ensureMemberPermissions(
      interaction,
      PermissionFlagsBits.ManageRoles,
      "`/bulkrole`"
    );
    if (!hasPermission) {
      return;
    }

    await interaction.deferReply({ ephemeral: true });

    const subcommand = interaction.options.getSubcommand();
    const targetRole = interaction.options.getRole("target-role", true);
    const filterMode = interaction.options.getString("filter", true);
    const sourceRole = interaction.options.getRole("source-role");
    const members = await collectMembers(interaction.guild, filterMode, sourceRole?.id);

    let changed = 0;
    for (const member of members.values()) {
      try {
        if (subcommand === "add" && !member.roles.cache.has(targetRole.id)) {
          await member.roles.add(targetRole);
          changed += 1;
        }

        if (subcommand === "remove" && member.roles.cache.has(targetRole.id)) {
          await member.roles.remove(targetRole);
          changed += 1;
        }
      } catch {
        continue;
      }
    }

    await interaction.editReply(
      `${subcommand === "add" ? "Added" : "Removed"} ${targetRole} for ${changed} member(s).`
    );
  }
};
